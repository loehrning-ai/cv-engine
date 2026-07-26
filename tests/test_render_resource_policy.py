"""Canaries for the fail-closed WeasyPrint resource policy."""
from __future__ import annotations

import base64
import io
import socket
import sys
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

from render import content, resource_policy  # noqa: E402


OWNER = "00000000-0000-0000-0000-000000000001"
OTHER_OWNER = "00000000-0000-0000-0000-000000000002"


def _cv_yaml(photo: object | None = None) -> str:
    model: dict[str, object] = {
        "name": "Resource Canary",
        "contact": [{"label": "resource@example.test"}],
        "experience": [
            {
                "role": "Engineer",
                "company": "Example",
                "location": "Berlin",
                "start": "2024",
                "end": "Present",
                "bullets": ["Kept rendering local."],
            }
        ],
    }
    if photo is not None:
        model["photo"] = photo
    return yaml.safe_dump(model, sort_keys=False)


def _load(tmp_path: Path, photo: object, *, owner_id: str | None = None):
    path = tmp_path / "cv.yaml"
    path.write_text(_cv_yaml(photo), encoding="utf-8")
    return content.load(path, owner_id=owner_id)


def _png_data_uri() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (30, 60, 90)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.mark.parametrize(
    "source",
    [
        "http://127.0.0.1:9/photo.png",
        "https://example.test/photo.webp",
        "file:///etc/passwd",
        "/etc/passwd",
        "../secret.png",
        "design/photos/owner/../../secret.png",
        r"C:\Windows\secret.png",
        r"design\photos\owner\photo.png",
        "design/photos/owner/photo.svg",
        "design/photos/owner/photo.png?cache=1",
        "design/photos/%2e%2e/secret.png",
    ],
)
def test_content_rejects_remote_file_absolute_and_traversal_photos(
    tmp_path: Path,
    source: str,
) -> None:
    with pytest.raises(ValueError, match="photo:"):
        _load(tmp_path, source)


def test_content_accepts_bounded_data_raster_and_safe_relative_raster(
    tmp_path: Path,
) -> None:
    assert _load(tmp_path, _png_data_uri())["photo"].startswith("data:image/png")
    relative = "design/photos/local/photo.webp"
    assert _load(tmp_path, relative)["photo"] == relative


def test_authenticated_owner_photo_path_is_exact_and_tenant_isolated(
    tmp_path: Path,
) -> None:
    own = f"design/photos/{OWNER}/photo.jpg"
    other = f"design/photos/{OTHER_OWNER}/photo.jpg"
    assert _load(tmp_path, own, owner_id=OWNER)["photo"] == own
    with pytest.raises(ValueError, match="authenticated owner's exact"):
        _load(tmp_path, other, owner_id=OWNER)
    with pytest.raises(ValueError, match="authenticated owner's exact"):
        _load(tmp_path, "design/screenshots/profile.jpg", owner_id=OWNER)


def test_authenticated_owner_may_still_use_validated_data_raster(
    tmp_path: Path,
) -> None:
    assert _load(tmp_path, _png_data_uri(), owner_id=OWNER)["photo"].startswith(
        "data:image/png"
    )


def test_fetcher_blocks_http_before_any_socket_call(monkeypatch) -> None:
    """An actual WeasyPrint render cannot reach even a loopback HTTP target."""
    pytest.importorskip("weasyprint")
    from weasyprint import HTML
    from weasyprint.urls import FatalURLFetchingError

    socket_calls: list[tuple[tuple, dict]] = []

    def trap_socket(*args, **kwargs):
        socket_calls.append((args, kwargs))
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket, "create_connection", trap_socket)
    fetcher = resource_policy.make_url_fetcher()
    html = '<html><body><img src="http://127.0.0.1:9/canary.png"></body></html>'
    with pytest.raises(FatalURLFetchingError):
        HTML(string=html, url_fetcher=fetcher).write_pdf()
    assert socket_calls == []


def test_fetcher_rejects_arbitrary_file_url() -> None:
    fetcher = resource_policy.make_url_fetcher()
    with pytest.raises(resource_policy.ResourceBlocked, match="not allowlisted"):
        fetcher(Path("/etc/passwd").as_uri())


def test_fetcher_rejects_unbound_and_non_photo_data_resources() -> None:
    photo = _png_data_uri()
    unbound = resource_policy.make_url_fetcher()
    with pytest.raises(resource_policy.ResourceBlocked, match="not the validated photo"):
        unbound(photo)

    bound = resource_policy.make_url_fetcher(photo_source=photo)
    response = bound(photo)
    response.close()
    with pytest.raises(resource_policy.ResourceBlocked, match="not the validated photo"):
        bound("data:text/plain;base64,SGVsbG8=")


def test_fetcher_allows_only_the_validated_exact_photo(tmp_path: Path) -> None:
    own = tmp_path / "design" / "photos" / OWNER / "photo.png"
    other = tmp_path / "design" / "photos" / OTHER_OWNER / "photo.png"
    own.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    own.write_bytes(b"owner-photo")
    other.write_bytes(b"other-photo")
    source = f"design/photos/{OWNER}/photo.png"
    fetcher = resource_policy.make_url_fetcher(
        photo_source=source,
        owner_id=OWNER,
        repo_root=tmp_path,
    )

    response = fetcher(own.as_uri())
    try:
        assert response.read() == b"owner-photo"
    finally:
        response.close()
    with pytest.raises(resource_policy.ResourceBlocked, match="not allowlisted"):
        fetcher(other.as_uri())


def test_pdf_store_threads_authenticated_and_explicit_owner(
    monkeypatch,
) -> None:
    import pdf_store
    from flask import Flask, g
    from render.pdf_service import RenderedPdf

    seen: list[str | None] = []

    def fake_render(_yaml, **kwargs):
        seen.append(kwargs.get("owner_id"))
        return RenderedPdf(False, 1, b"%PDF", density="normal")

    monkeypatch.setattr(pdf_store, "render_cv_pdf", fake_render)
    app = Flask(__name__)
    with app.test_request_context("/"):
        g.owner_id = OWNER
        pdf_store.render_cv(_cv_yaml())
    pdf_store.render_cv(_cv_yaml(), owner_id=OTHER_OWNER)
    assert seen == [OWNER, OTHER_OWNER]


def test_preview_owner_parameter_rejects_cross_tenant_photo(tmp_path: Path) -> None:
    import render_preview

    path = tmp_path / "cv.yaml"
    path.write_text(
        _cv_yaml(f"design/photos/{OTHER_OWNER}/photo.png"),
        encoding="utf-8",
    )
    html = render_preview.render_cv_html(path, owner_id=OWNER)
    assert "authenticated owner" in html
    assert "<main" not in html


def test_preview_inlines_exact_owner_photo_without_public_asset_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import render_preview
    from PIL import Image

    repo = tmp_path / "repo"
    photo = repo / "design" / "photos" / OWNER / "photo.png"
    photo.parent.mkdir(parents=True)
    Image.new("RGB", (3, 3), (20, 40, 60)).save(photo, "PNG")
    cv_path = tmp_path / "cv.yaml"
    cv_path.write_text(
        _cv_yaml(f"design/photos/{OWNER}/photo.png"),
        encoding="utf-8",
    )
    monkeypatch.setattr(render_preview, "REPO", repo)

    html = render_preview.render_cv_html(cv_path, owner_id=OWNER)

    assert 'src="data:image/png;base64,' in html
    assert f"design/photos/{OWNER}" not in html
    assert "/asset/design/photos/" not in html


def test_template_thumbnail_omits_private_photo_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import render_preview
    from PIL import Image

    repo = tmp_path / "repo"
    photo = repo / "design" / "photos" / OWNER / "photo.png"
    photo.parent.mkdir(parents=True)
    Image.new("RGB", (3, 3), (20, 40, 60)).save(photo, "PNG")
    cv_path = tmp_path / "cv.yaml"
    cv_path.write_text(
        _cv_yaml(f"design/photos/{OWNER}/photo.png"),
        encoding="utf-8",
    )
    monkeypatch.setattr(render_preview, "REPO", repo)

    html = render_preview.render_cv_html(
        cv_path,
        owner_id=OWNER,
        thumbnail=True,
    )

    assert "data:image/" not in html
    assert f"design/photos/{OWNER}" not in html
    assert 'class="photo"' not in html
