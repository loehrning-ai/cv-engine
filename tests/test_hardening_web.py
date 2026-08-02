"""Web-layer security hardening tests (e2e hardening batch).

Covers four fixes in the editor gateway:

  1. SSRF — CGNAT (RFC-6598) + deprecated IPv6 site-local (fec0::/10) are
     blocked, every resolved IP is validated, redirects are not followed.
  2. Photo upload — owner-namespaced storage, magic-byte type validation,
     decoded-dimension + byte-size caps, SVG rejection (data-URI bomb +
     cross-tenant overwrite).
  3. Security headers — CSP / nosniff / frame-ancestors + strict CORS +
     /app noindex, with the routes still serving.
  4. Log redaction — child-logger records + hyphenated provider key formats
     (sk-ant-…, sk-proj-…) are masked.

All external calls (DNS, HTTP, LLM) are mocked — no real network.
"""
from __future__ import annotations

import io
import logging
import struct
import sys
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))


@pytest.fixture
def disk_env(server_module, monkeypatch, tmp_path):
    """Redirect REPO (and themes/content dirs) to a temp tree for disk routes."""
    import json

    repo_tmp = tmp_path / "cv-test"
    (repo_tmp / "content").mkdir(parents=True)
    (repo_tmp / "themes").mkdir(parents=True)
    (repo_tmp / "design").mkdir(parents=True)
    (repo_tmp / "themes" / "default.json").write_text(
        json.dumps({"name": "Default", "accent": "#111111", "font": "serif", "density": "normal"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server_module, "REPO", repo_tmp)
    monkeypatch.setattr(server_module, "CONTENT_DIR", repo_tmp / "content")
    monkeypatch.setattr(server_module, "THEMES_DIR", repo_tmp / "themes")
    return repo_tmp


# ===========================================================================
# FIX 1 — SSRF hardening
# ===========================================================================
class TestSSRFIpClassification:
    """`_ip_is_public` must reject CGNAT + fec0::/10 and accept real public."""

    def test_cgnat_range_blocked(self):
        import ipaddress

        import ssrf

        # RFC-6598 100.64.0.0/10 — endpoints + an interior address.
        for addr in ("100.64.0.1", "100.64.0.0", "100.127.255.255", "100.100.100.100"):
            assert not ssrf._ip_is_public(ipaddress.ip_address(addr)), addr

    def test_just_outside_cgnat_allowed(self):
        import ipaddress

        import ssrf

        # 100.128.0.0 is the first address ABOVE the /10 — genuinely public.
        assert ssrf._ip_is_public(ipaddress.ip_address("100.128.0.0"))
        assert ssrf._ip_is_public(ipaddress.ip_address("8.8.8.8"))

    def test_ipv6_site_local_fec0_blocked(self):
        import ipaddress

        import ssrf

        for addr in ("fec0::", "fec0::1", "feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"):
            assert not ssrf._ip_is_public(ipaddress.ip_address(addr)), addr

    def test_existing_private_and_metadata_still_blocked(self):
        import ipaddress

        import ssrf

        for addr in ("10.0.0.5", "192.168.1.1", "172.16.0.1", "127.0.0.1",
                     "169.254.169.254", "::1", "fe80::1", "fc00::1"):
            assert not ssrf._ip_is_public(ipaddress.ip_address(addr)), addr

    def test_ipv4_mapped_cgnat_blocked(self):
        import ipaddress

        import ssrf

        # A private/CGNAT v4 smuggled inside an IPv4-mapped IPv6 host.
        assert not ssrf._ip_is_public(ipaddress.ip_address("::ffff:100.64.0.1"))
        assert not ssrf._ip_is_public(ipaddress.ip_address("::ffff:10.0.0.1"))


class TestSSRFAssertUrl:
    """`assert_safe_url` on bare IP literals — no DNS needed."""

    def test_cgnat_literal_blocked(self):
        import ssrf

        for url in ("http://100.64.0.1/", "http://100.127.255.255/x",
                    "http://100.64.0.0/"):
            with pytest.raises(ssrf.SSRFBlocked):
                ssrf.assert_safe_url(url)

    def test_fec0_literal_blocked(self):
        import ssrf

        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.assert_safe_url("http://[fec0::1]/")

    def test_public_literal_just_outside_cgnat_allowed(self):
        import ssrf

        assert ssrf.assert_safe_url("http://100.128.0.0/") == "100.128.0.0"

    def test_all_resolved_ips_validated(self, monkeypatch):
        """If a host resolves to BOTH a public and a private IP → blocked."""
        import socket

        import ssrf

        def _fake_getaddrinfo(host, *_a, **_k):
            # One public, one private — the private one must veto the URL.
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            ]

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.assert_safe_url("http://dual.example.com/")


class _FakeResp:
    """Minimal http.client response stand-in."""

    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n and n > 0 else self._body


class _FakeConn:
    """Records the host it was constructed with; never touches the network."""

    last_pin: str | None = None
    last_request: tuple | None = None

    def __init__(self, host, port=None, *a, **k):
        type(self).last_pin = host
        self._resp = _FakeResp(
            302, b"", {"Location": "http://169.254.169.254/latest/meta-data/"}
        )

    def request(self, method, path, headers=None, *a, **k):
        type(self).last_request = (method, path, headers)

    def getresponse(self):
        return self._resp

    def close(self):
        pass


class TestSSRFFetchHardening:
    def test_fetch_pins_validated_ip_and_preserves_host(self, monkeypatch):
        """safe_fetch_url connects to the VALIDATED IP, keeps the Host header."""
        import http.client
        import socket

        import ssrf

        def _fake_getaddrinfo(host, *_a, **_k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)

        captured = {}

        class _OkConn(_FakeConn):
            def __init__(self, host, port=None, *a, **k):
                captured["pin"] = host
                self._resp = _FakeResp(200, b"BODY-OK")

            def request(self, method, path, headers=None, *a, **k):
                captured["headers"] = headers

        monkeypatch.setattr(http.client, "HTTPConnection", _OkConn)
        out = ssrf.safe_fetch_url("http://www.example.com/theme.json")
        assert out == b"BODY-OK"
        # Connected to the pinned VALIDATED IP, not the hostname.
        assert captured["pin"] == "93.184.216.34"
        # Host header preserved so virtual hosting still works.
        assert captured["headers"]["Host"] == "www.example.com"

    def test_fetch_does_not_follow_redirect_to_internal(self, monkeypatch):
        """A 302 → internal Location is rejected, never chased."""
        import http.client
        import socket

        import ssrf

        def _fake_getaddrinfo(host, *_a, **_k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        monkeypatch.setattr(http.client, "HTTPConnection", _FakeConn)

        with pytest.raises(ssrf.HTTPStatusBlocked):
            ssrf.safe_fetch_url("http://www.example.com/redir")
        # The redirect was NOT followed: the connection was only ever made to
        # the validated public IP.
        assert _FakeConn.last_pin == "93.184.216.34"

    def test_https_fetch_uses_pinned_ip_without_invalid_server_hostname_kwarg(self, monkeypatch):
        import http.client
        import socket
        import ssl

        import ssrf

        def _fake_getaddrinfo(host, *_a, **_k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        captured = {}

        class _HttpsConn(_FakeConn):
            def __init__(self, host, port=None, *a, **k):
                captured["pin"] = host
                captured["kwargs"] = dict(k)
                self._resp = _FakeResp(200, b"HTTPS-OK")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        monkeypatch.setattr(ssl, "create_default_context", lambda: object())
        monkeypatch.setattr(http.client, "HTTPSConnection", _HttpsConn)

        out = ssrf.safe_fetch_url("https://www.example.com/theme.json")

        assert out == b"HTTPS-OK"
        assert captured["pin"] == "93.184.216.34"
        assert "server_hostname" not in captured["kwargs"]

    def test_fetch_rejects_oversized_body_instead_of_truncating(self, monkeypatch):
        import http.client
        import socket

        import ssrf

        def _fake_getaddrinfo(host, *_a, **_k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        class _LargeConn(_FakeConn):
            def __init__(self, host, port=None, *a, **k):
                type(self).last_pin = host
                self._resp = _FakeResp(200, b"abcdef")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fake_getaddrinfo)
        monkeypatch.setattr(http.client, "HTTPConnection", _LargeConn)

        with pytest.raises(ssrf.SSRFBlocked, match="too large"):
            ssrf.safe_fetch_url("http://www.example.com/large", max_bytes=5)

    def test_fetch_rejects_cgnat_before_connecting(self, monkeypatch):
        import http.client

        import ssrf

        def _boom(*_a, **_k):
            raise AssertionError("must not connect to a CGNAT literal")

        monkeypatch.setattr(http.client, "HTTPConnection", _boom)
        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.safe_fetch_url("http://100.64.0.1/")


# ===========================================================================
# FIX 2 — Photo upload hardening (magic bytes, dimensions, owner-scope, SVG)
# ===========================================================================
def _png_bytes(w: int, h: int, color=(10, 20, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 100, 50)).save(buf, format="JPEG")
    return buf.getvalue()


def _jpeg_with_exif(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    exif = Image.Exif()
    exif[270] = "private source metadata"
    exif[274] = 6  # rotate 90 degrees clockwise
    Image.new("RGB", (w, h), (20, 40, 60)).save(
        buf,
        format="JPEG",
        exif=exif,
    )
    return buf.getvalue()


def _forged_png_header(width: int, height: int) -> bytes:
    """A minimal PNG signature + IHDR claiming ``width``×``height``.

    Tiny on the wire but declares an enormous canvas — the shape of a real
    decompression bomb. The header-only dimension read must reject it without
    decoding any pixels.
    """
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr_data
    crc = zlib.crc32(chunk) & 0xFFFFFFFF
    ihdr = struct.pack(">I", len(ihdr_data)) + chunk + struct.pack(">I", crc)
    return sig + ihdr


class TestImageGuardUnit:
    def test_accepts_real_png_and_jpeg(self):
        import image_guard

        info = image_guard.validate_image_bytes(_png_bytes(120, 160))
        assert info.fmt == "PNG" and info.width == 120 and info.height == 160
        info2 = image_guard.validate_image_bytes(_jpeg_bytes(64, 64))
        assert info2.fmt == "JPEG"

    def test_rejects_svg(self):
        import image_guard

        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
        with pytest.raises(image_guard.ImageRejected):
            image_guard.validate_image_bytes(svg)

    def test_rejects_non_image(self):
        import image_guard

        with pytest.raises(image_guard.ImageRejected):
            image_guard.validate_image_bytes(b"this is definitely not an image at all")

    def test_rejects_oversized_bytes(self):
        import image_guard

        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (image_guard.MAX_PHOTO_BYTES + 10)
        with pytest.raises(image_guard.ImageRejected):
            image_guard.validate_image_bytes(big)

    def test_rejects_huge_declared_dimensions_header_only(self):
        """A tiny file claiming a giant canvas (the bomb) is rejected."""
        import image_guard

        forged = _forged_png_header(100000, 100000)  # 10 billion px, ~40 bytes
        assert len(forged) < 100  # genuinely tiny on the wire
        with pytest.raises(image_guard.ImageRejected):
            image_guard.validate_image_bytes(forged)

    def test_rejects_area_over_cap(self):
        """A REAL (decodable) image whose area exceeds the cap is rejected."""
        import image_guard

        # Solid-color 2500×2000 = 5 MP > 4 MP cap; compresses tiny as PNG.
        bomb = _png_bytes(2500, 2000)
        with pytest.raises(image_guard.ImageRejected):
            image_guard.validate_image_bytes(bomb)

    def test_rejects_per_dimension_over_cap(self):
        import image_guard

        forged = _forged_png_header(image_guard.MAX_IMAGE_DIMENSION + 1, 10)
        with pytest.raises(image_guard.ImageRejected):
            image_guard.validate_image_bytes(forged)


class TestDataUriGuard:
    def test_accepts_small_raster_data_uri(self):
        import base64

        import image_guard

        uri = "data:image/png;base64," + base64.b64encode(_png_bytes(40, 40)).decode()
        assert image_guard.is_data_uri(uri)
        info = image_guard.validate_data_uri_image(uri)
        assert info.fmt == "PNG"

    def test_rejects_svg_data_uri(self):
        import image_guard

        uri = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>"
        with pytest.raises(image_guard.ImageRejected):
            image_guard.validate_data_uri_image(uri)

    def test_rejects_bomb_data_uri(self):
        import base64

        import image_guard

        forged = _forged_png_header(80000, 80000)
        uri = "data:image/png;base64," + base64.b64encode(forged).decode()
        with pytest.raises(image_guard.ImageRejected):
            image_guard.validate_data_uri_image(uri)


class TestPhotoUploadRoute:
    def test_upload_png_is_owner_scoped(self, client, bypass_auth, fake_supabase, disk_env):
        res = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(_png_bytes(100, 120)), "headshot.png")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        path = res.get_json()["path"]
        # Stored under the owner-namespaced dir, NOT the shared design/photo.*.
        assert path == f"design/photos/{bypass_auth}/photo.png"
        assert (disk_env / "design" / "photos" / bypass_auth / "photo.png").exists()
        assert not (disk_env / "design" / "photo.png").exists()

    def test_upload_applies_orientation_and_strips_exif(
        self, client, bypass_auth, fake_supabase, disk_env
    ):
        from PIL import Image

        res = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(_jpeg_with_exif(20, 40)), "headshot.jpg")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        stored = (
            disk_env / "design" / "photos" / bypass_auth / "photo.jpg"
        )
        with Image.open(stored) as image:
            assert image.size == (40, 20)
            assert dict(image.getexif()) == {}
            assert "exif" not in image.info

    def test_atomic_write_failure_preserves_prior_photo(
        self, client, bypass_auth, fake_supabase, disk_env, server_module, monkeypatch
    ):
        owner_dir = disk_env / "design" / "photos" / bypass_auth
        owner_dir.mkdir(parents=True)
        prior = _png_bytes(24, 24, color=(1, 2, 3))
        old_path = owner_dir / "photo.png"
        old_path.write_bytes(prior)
        monkeypatch.setattr(
            server_module.os,
            "replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
        )

        res = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(_jpeg_bytes(30, 30)), "new.jpg")},
            content_type="multipart/form-data",
        )

        assert res.status_code == 503
        assert res.get_json()["code"] == "photo_storage_unavailable"
        assert old_path.read_bytes() == prior
        assert not (owner_dir / "photo.jpg").exists()
        assert list(owner_dir.glob(".photo-*.tmp")) == []

    def test_unlistable_documents_fail_the_photo_change_closed(
        self, client, bypass_auth, fake_supabase, disk_env, server_module, monkeypatch
    ):
        """An unreadable document store must block the photo, not skip cleanup.

        The shared photo participates in every CV render, so changing it has to
        delete every derived PDF first. Enumerating the CVs is how those PDFs
        are found: if the listing fails there is no way to know what the change
        would orphan, and silently proceeding would leave stale PDFs carrying
        the old photo. Fail the mutation closed and keep the prior photo.
        """
        owner_dir = disk_env / "design" / "photos" / bypass_auth
        owner_dir.mkdir(parents=True)
        prior = _png_bytes(24, 24, color=(4, 5, 6))
        old_path = owner_dir / "photo.png"
        old_path.write_bytes(prior)
        import pdf_store

        monkeypatch.setattr(server_module, "_artifact_cleanup_applies", lambda: True)
        monkeypatch.setattr(
            pdf_store,
            "delete_owner_cv_artifacts",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("documents unreachable")),
        )

        upload = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(_jpeg_bytes(30, 30)), "new.jpg")},
            content_type="multipart/form-data",
        )
        removal = client.delete("/api/asset/photo")

        for res in (upload, removal):
            assert res.status_code == 503
            assert res.get_json()["code"] == "artifact_cleanup_unavailable"
        assert old_path.read_bytes() == prior
        assert not (owner_dir / "photo.jpg").exists()

    def test_owner_photo_is_only_served_through_authenticated_api(
        self, client, bypass_auth, fake_supabase, disk_env
    ):
        owner_dir = disk_env / "design" / "photos" / bypass_auth
        owner_dir.mkdir(parents=True)
        owner_dir.joinpath("photo.png").write_bytes(_png_bytes(24, 24))

        assert client.get(f"/repo/design/photos/{bypass_auth}/photo.png").status_code == 404
        assert client.get(f"/asset/design/photos/{bypass_auth}/photo.png").status_code == 404
        res = client.get("/api/asset/photo/photo.png")
        assert res.status_code == 200
        assert res.content_type.startswith("image/png")

    def test_photo_disclosure_via_repo_prefix_bypass_is_blocked(self, client, bypass_auth, disk_env):
        """Plan 110 Stage 2 (empirically confirmed): the raw ``design/photos/``
        block in serve_asset/serve_repo is bypassable by prepending the
        repo-basename or absolute-REPO prefix, which ``_safe_repo_path`` strips
        BEFORE the block runs. The private photo must stay 404 on EVERY such
        normalisation variant — the block now lives on the cleaned path."""
        owner_dir = disk_env / "design" / "photos" / bypass_auth
        owner_dir.mkdir(parents=True)
        owner_dir.joinpath("photo.png").write_bytes(_png_bytes(24, 24))

        repo_base = disk_env.name  # e.g. the tmp repo dir name (== REPO.name)
        repo_abs = str(disk_env).lstrip("/")
        bypass_paths = [
            f"/asset/{repo_base}/design/photos/{bypass_auth}/photo.png",
            f"/repo/{repo_base}/design/photos/{bypass_auth}/photo.png",
            f"/asset/{repo_abs}/design/photos/{bypass_auth}/photo.png",
            f"/repo/{repo_abs}/design/photos/{bypass_auth}/photo.png",
        ]
        for p in bypass_paths:
            assert client.get(p).status_code == 404, f"LEAK via {p}"
        # A NON-photo design asset with the same prefix still resolves (the
        # block is scoped to design/photos/, not all of design/).
        (disk_env / "design" / "fonts").mkdir(parents=True, exist_ok=True)
        (disk_env / "design" / "fonts" / "probe.txt").write_text("ok")
        assert client.get(f"/asset/{repo_base}/design/fonts/probe.txt").status_code == 200

    def test_photo_routes_block_while_deletion_pending(self, client, bypass_auth, fake_supabase, disk_env):
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": fake_supabase.owner_id, "deletion_requested_at": "now"}
        )
        owner_dir = disk_env / "design" / "photos" / bypass_auth
        owner_dir.mkdir(parents=True)
        owner_dir.joinpath("photo.png").write_bytes(_png_bytes(24, 24))

        assert client.get("/api/asset/photo/photo.png").status_code == 409
        assert client.delete("/api/asset/photo").status_code == 409
        res = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(_png_bytes(24, 24)), "headshot.png")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 409
        assert owner_dir.joinpath("photo.png").exists()

    def test_account_deletion_keeps_photo_for_reversible_grace_window(self, client, bypass_auth, fake_supabase, disk_env):
        """Plan 110 Stage 3: the soft-delete REQUEST must NOT remove the on-disk
        photo — the 30-day grace window is reversible (cancel-delete restores
        the documents), and the photo is already unreachable while pending
        (api_get_photo → 409). Deleting it on request lost the headshot on a
        cancel (GDPR-reversibility bug). The file survives; the production
        service-role worker removes the exact owner directory after D+30 and
        before the Auth/database delete."""
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": fake_supabase.owner_id, "managed_credits_remaining": 1, "deletion_requested_at": None}
        )
        owner_dir = disk_env / "design" / "photos" / bypass_auth
        owner_dir.mkdir(parents=True)
        owner_dir.joinpath("photo.png").write_bytes(_png_bytes(24, 24))

        res = client.post("/api/account/delete", json={"confirm": "DELETE"})

        assert res.status_code == 200, res.get_data(as_text=True)
        # The photo is preserved so a cancel inside the grace window restores it.
        assert owner_dir.joinpath("photo.png").exists()

    def test_extension_does_not_decide_type(self, client, bypass_auth, disk_env):
        """A .png filename wrapping non-image bytes is rejected (magic bytes)."""
        res = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(b"not really a png"), "evil.png")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400

    def test_svg_upload_rejected(self, client, bypass_auth, disk_env):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>'
        res = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(svg), "logo.svg")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400

    def test_huge_dimension_upload_rejected(self, client, bypass_auth, disk_env):
        forged = _forged_png_header(90000, 90000)
        res = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(forged), "bomb.png")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400
        assert (
            disk_env / "design" / "photos" / bypass_auth / "photo.png"
        ).exists() is False

    def test_two_owners_do_not_overwrite(
        self, client, server_module, monkeypatch, fake_supabase, disk_env
    ):
        """Different owners get different paths — no cross-tenant clobber."""
        import auth as auth_mod
        from flask import g

        def _make_stub(owner_id):
            def _stub():
                from flask import request

                if request.method == "OPTIONS" or request.path.startswith(
                    ("/static/", "/asset/", "/repo/")
                ):
                    return None
                g.owner_id = owner_id
                g.email = "x@onepager.test"
                g.access_token = "stub"
                return None

            return _stub

        owner_a = "aaaaaaaa-0000-0000-0000-000000000001"
        owner_b = "bbbbbbbb-0000-0000-0000-000000000002"

        monkeypatch.setattr(auth_mod, "require_auth", _make_stub(owner_a))
        fake_supabase.owner_id = owner_a
        ra = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(_png_bytes(30, 30)), "a.png")},
            content_type="multipart/form-data",
        )
        monkeypatch.setattr(auth_mod, "require_auth", _make_stub(owner_b))
        fake_supabase.owner_id = owner_b
        rb = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(_jpeg_bytes(30, 30)), "b.jpg")},
            content_type="multipart/form-data",
        )
        assert ra.get_json()["path"] == f"design/photos/{owner_a}/photo.png"
        assert rb.get_json()["path"] == f"design/photos/{owner_b}/photo.jpg"
        # Both files coexist on disk — neither clobbered the other.
        assert (disk_env / "design" / "photos" / owner_a / "photo.png").exists()
        assert (disk_env / "design" / "photos" / owner_b / "photo.jpg").exists()


class TestCvYamlDataUriGuard:
    def test_save_rejects_bomb_data_uri_photo(self, client, fake_supabase):
        import base64

        forged = _forged_png_header(90000, 90000)
        uri = "data:image/png;base64," + base64.b64encode(forged).decode()
        body = (
            "name: Test\n"
            f"photo: '{uri}'\n"
            "contact:\n"
            "  - { label: x, href: 'mailto:x@example.com' }\n"
            "experience:\n"
            "  - role: Eng\n"
            "    company: Acme\n"
            "    location: x\n"
            "    start: '2020'\n"
            "    end: 'Present'\n"
            "    bullets:\n"
            "      - 'A bullet'\n"
        )
        res = client.post("/api/cv", json={"content": body})
        assert res.status_code == 400, res.get_data(as_text=True)

    def test_save_accepts_normal_path_photo(self, client, fake_supabase):
        body = (
            "name: Test\n"
            f"photo: design/photos/{fake_supabase.owner_id}/photo.jpg\n"
            "contact:\n"
            "  - { label: x, href: 'mailto:x@example.com' }\n"
            "experience:\n"
            "  - role: Eng\n"
            "    company: Acme\n"
            "    location: x\n"
            "    start: '2020'\n"
            "    end: 'Present'\n"
            "    bullets:\n"
            "      - 'A bullet'\n"
        )
        res = client.post("/api/cv", json={"content": body})
        assert res.status_code == 200, res.get_data(as_text=True)

    @pytest.mark.parametrize(
        "photo",
        [
            "design/photo.jpg",
            "design/photos/11111111-1111-1111-1111-111111111111/photo.jpg",
            "https://example.com/photo.jpg",
            "../secret.jpg",
        ],
    )
    def test_save_rejects_unsafe_path_photo(self, client, fake_supabase, photo):
        body = (
            "name: Test\n"
            f"photo: {photo}\n"
            "contact:\n"
            "  - { label: x, href: 'mailto:x@example.com' }\n"
            "experience:\n"
            "  - role: Eng\n"
            "    company: Acme\n"
            "    location: x\n"
            "    start: '2020'\n"
            "    end: 'Present'\n"
            "    bullets:\n"
            "      - 'A bullet'\n"
        )
        res = client.post("/api/cv", json={"content": body})
        assert res.status_code == 400, res.get_data(as_text=True)

    def test_build_rejects_stored_unsafe_path_photo(self, client, fake_supabase):
        doc_id = "11111111-1111-1111-1111-111111111111"
        fake_supabase.tables.setdefault("documents", []).append(
            {
                "id": doc_id,
                "owner_id": fake_supabase.owner_id,
                "type": "cv",
                "name": "Unsafe Photo",
                "yaml_blob": (
                    "name: Test\n"
                    "photo: https://example.com/photo.jpg\n"
                    "contact:\n"
                    "  - { label: x, href: 'mailto:x@example.com' }\n"
                    "experience:\n"
                    "  - role: Eng\n"
                    "    company: Acme\n"
                    "    location: x\n"
                    "    start: '2020'\n"
                    "    end: 'Present'\n"
                    "    bullets:\n"
                    "      - 'A bullet'\n"
                ),
                "deleted_at": None,
                "updated_at": 1,
            }
        )
        res = client.post(f"/api/build?id={doc_id}", json={})
        assert res.status_code == 400
        assert "photo" in res.get_json()["error"]


# ===========================================================================
# FIX 3 — Security headers + strict CORS + /app noindex
# ===========================================================================
class TestSecurityHeaders:
    def test_core_headers_on_landing(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert res.headers.get("X-Content-Type-Options") == "nosniff"
        assert res.headers.get("X-Frame-Options") == "DENY"
        csp = res.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "object-src 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_headers_on_healthz(self, client):
        res = client.get("/healthz")
        assert res.status_code == 200
        assert res.get_json() == {"ok": True}
        assert res.headers.get("X-Content-Type-Options") == "nosniff"
        assert "Content-Security-Policy" in res.headers

    def test_app_route_serves_and_noindex(self, client):
        res = client.get("/app")
        assert res.status_code == 200
        assert res.headers.get("X-Robots-Tag") == "noindex"
        csp = res.headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp
        assert "script-src-attr 'none'" in csp
        assert "cdn.jsdelivr.net" not in csp
        assert "unpkg.com" not in csp
        assert "frame-ancestors 'none'" in csp

    def test_api_unauth_is_401_with_headers(self, client):
        # No bearer token → the REAL guard returns 401 (routes still served).
        res = client.get("/api/cvs")
        assert res.status_code == 401
        assert res.headers.get("X-Content-Type-Options") == "nosniff"
        assert res.headers.get("X-Robots-Tag") == "noindex"
        assert res.headers.get("Cache-Control") == "private, no-store, max-age=0"
        assert res.headers.get("Pragma") == "no-cache"
        assert res.headers.get("Expires") == "0"

    @pytest.mark.parametrize(
        "path",
        (
            "/api/cv",
            "/api/cvs",
            "/api/account/export",
            "/api/asset/photo/photo.png",
            "/api/preview",
        ),
    )
    def test_personal_api_responses_are_never_cacheable(
        self, client, fake_supabase, path
    ):
        res = client.get(path)
        assert res.headers.get("Cache-Control") == "private, no-store, max-age=0"
        assert res.headers.get("Pragma") == "no-cache"
        assert res.headers.get("Expires") == "0"

    def test_preview_csp_keeps_pagedjs_same_origin(self, client, fake_supabase):
        """The preview uses local external scripts and never relaxes script CSP."""
        res = client.get("/api/preview")
        assert res.status_code == 200
        csp = res.headers.get("Content-Security-Policy", "")
        script_directive = next(
            (d for d in csp.split(";") if d.strip().startswith("script-src")), ""
        )
        assert script_directive.strip() == "script-src 'self'"
        assert "'unsafe-inline'" not in script_directive
        assert "unpkg.com" not in csp
        assert "cdn.jsdelivr.net" not in csp
        assert "data:" in csp  # inline CV photo + fonts

    def test_app_csp_does_not_relax_script_inline(self, client):
        """Non-preview pages keep a strict script-src (no 'unsafe-inline')."""
        res = client.get("/app")
        csp = res.headers.get("Content-Security-Policy", "")
        script_directive = next(
            (d for d in csp.split(";") if d.strip().startswith("script-src")), ""
        )
        assert "'unsafe-inline'" not in script_directive


class TestStrictCORS:
    def test_arbitrary_origin_not_reflected(self, client, fake_supabase):
        res = client.get(
            "/api/cvs", headers={"Origin": "https://evil.example.com"}
        )
        # Whatever the status, a hostile Origin must NEVER be echoed back.
        assert res.headers.get("Access-Control-Allow-Origin") != "https://evil.example.com"
        assert "Access-Control-Allow-Origin" not in res.headers

    def test_no_wildcard_cors(self, client):
        res = client.get("/", headers={"Origin": "https://evil.example.com"})
        assert res.headers.get("Access-Control-Allow-Origin") != "*"

    def test_configured_origin_is_allowed(self, server_module, monkeypatch):
        """An explicitly-configured PUBLIC_BASE_URL origin IS echoed."""
        allowed = "https://onepager.example.com"
        monkeypatch.setattr(
            server_module, "_ALLOWED_CORS_ORIGINS", frozenset({allowed})
        )
        client = server_module.app.test_client()
        res = client.get("/", headers={"Origin": allowed})
        assert res.headers.get("Access-Control-Allow-Origin") == allowed
        assert res.headers.get("Vary") == "Origin"

    def test_public_preview_font_allows_opaque_sandbox_origin(self, client):
        res = client.get("/asset/design/fonts/Inter-Regular.ttf")
        assert res.status_code == 200
        assert res.headers.get("Access-Control-Allow-Origin") == "*"

    def test_non_font_asset_never_gets_wildcard_cors(
        self, client, server_module, monkeypatch, tmp_path
    ):
        design = tmp_path / "design" / "icons"
        design.mkdir(parents=True)
        (design / "safe.svg").write_text("<svg/>", encoding="utf-8")
        monkeypatch.setattr(server_module, "REPO", tmp_path)
        res = client.get("/asset/design/icons/safe.svg")
        assert res.status_code == 200
        assert "Access-Control-Allow-Origin" not in res.headers


# ===========================================================================
# FIX 4 — Log redaction: child loggers + hyphenated key formats
# ===========================================================================
class _CaptureHandler(logging.Handler):
    """Collects the rendered message of every record it processes."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def root_with_capture():
    """Root logger with a capture handler; restored after the test."""
    root = logging.getLogger()
    cap = _CaptureHandler()
    prev_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    try:
        yield root, cap
    finally:
        root.removeHandler(cap)
        root.setLevel(prev_level)
        # Drop any redactor filters we installed so we don't leak across tests.
        import logredact

        root.filters[:] = [
            f for f in root.filters if not isinstance(f, logredact.KeyRedactionFilter)
        ]


class TestRedactFunction:
    def test_sk_ant_masked(self):
        import logredact

        s = "calling with sk-ant-api03-AbCdEf0123456789ZXCVbnmQWERTY done"
        out = logredact.redact(s)
        assert "sk-ant-api03-AbCdEf" not in out
        assert "REDACTED" in out

    def test_sk_proj_hyphenated_masked(self):
        import logredact

        # sk-proj- keys contain hyphens in the body — the old generic pattern
        # stopped at the first hyphen and left them exposed.
        synthetic_key = "-".join(
            ("sk", "proj", "AbC123", "def456", "GHI789jklMNO0123456789")
        )
        s = f"key={synthetic_key} trailing"
        out = logredact.redact(s)
        assert "sk-proj-AbC123-def456" not in out
        assert "REDACTED" in out

    def test_generic_sk_masked(self):
        import logredact

        out = logredact.redact("token sk-1234567890ABCDEFGHIJKLMNOP end")
        assert "REDACTED" in out
        assert "1234567890ABCDEFGHIJ" not in out

    def test_plain_prose_not_clobbered(self):
        import logredact

        # "sk-" as a short word fragment must survive untouched.
        assert logredact.redact("the sk-8 build") == "the sk-8 build"

    @pytest.mark.parametrize(
        "raw, forbidden",
        [
            (
                "Authorization: Bearer "
                + ".".join(
                    (
                        "eyJhbGciOiJIUzI1NiJ9",
                        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                        "signature123456",
                    )
                ),
                "eyJhbGciOiJIUzI1NiJ9",
            ),
            ("bearer abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz012345"),
            ("Cookie: li_at=secret-cookie; JSESSIONID=session-secret; session_token=token-secret", "secret-cookie"),
            ("Cookie: sid=session-secret; connect.sid=connect-secret; other=visible", "session-secret"),
            ("url=https://x.test/?li_at=secret-cookie&session_token=token-secret", "token-secret"),
            ("{'access_token': 'abcdefghijklmnopqrstuvwxyz012345'}", "abcdefghijklmnopqrstuvwxyz012345"),
            ("custom provider api_key=customtoken_1234567890abcdef", "customtoken_1234567890abcdef"),
        ],
    )
    def test_auth_session_tokens_masked(self, raw, forbidden):
        import logredact

        out = logredact.redact(raw)
        assert forbidden not in out
        assert "REDACTED" in out


class TestChildLoggerRedaction:
    def test_propagated_child_record_is_masked(self, root_with_capture):
        import logredact

        root, cap = root_with_capture
        logredact.install()  # must cover child loggers via handler-level filter

        child = logging.getLogger("onepager.child.deep")
        child.propagate = True
        child.info("leaking sk-ant-api03-SECRETSECRETSECRET0123456789 oops")

        assert cap.messages, "no record captured"
        joined = " ".join(cap.messages)
        assert "SECRETSECRETSECRET" not in joined
        assert "REDACTED" in joined

    def test_propagated_child_sk_proj_masked(self, root_with_capture):
        import logredact

        root, cap = root_with_capture
        logredact.install()

        child = logging.getLogger("some.other.module")
        child.warning("creds sk-proj-LONG-secret-value-0123456789ABCDEF here")

        joined = " ".join(cap.messages)
        assert "sk-proj-LONG-secret-value" not in joined
        assert "REDACTED" in joined

    def test_handler_added_after_install_still_redacts(self, root_with_capture):
        """A handler added AFTER install() inherits the redactor (patched addHandler)."""
        import logredact

        root, _cap = root_with_capture
        logredact.install()

        late = _CaptureHandler()
        root.addHandler(late)
        try:
            logging.getLogger("late.child").error(
                "boom sk-ant-api03-AFTERINSTALL0123456789XYZ end"
            )
            joined = " ".join(late.messages)
            assert "AFTERINSTALL0123456789" not in joined
            assert "REDACTED" in joined
        finally:
            root.removeHandler(late)

    def test_preexisting_child_handler_is_masked(self):
        import logredact

        child = logging.getLogger("onepager.preexisting.child")
        cap = _CaptureHandler()
        previous_handlers = list(child.handlers)
        previous_propagate = child.propagate
        child.handlers[:] = []
        child.propagate = False
        child.addHandler(cap)
        try:
            logredact.install()
            child.error("preexisting sk-proj-PREEXISTING-secret-value-0123456789ABCDEF")
            joined = " ".join(cap.messages)
            assert "PREEXISTING-secret-value" not in joined
            assert "REDACTED" in joined
        finally:
            child.handlers[:] = previous_handlers
            child.propagate = previous_propagate

    def test_exception_traceback_text_is_redacted(self, root_with_capture):
        import logredact

        root, _cap = root_with_capture
        logredact.install()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root.addHandler(handler)
        try:
            try:
                synthetic_jwt = ".".join(
                    (
                        "eyJhbGciOiJIUzI1NiJ9",
                        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                        "signature123456",
                    )
                )
                raise RuntimeError(f"Authorization: Bearer {synthetic_jwt}")
            except RuntimeError:
                logging.getLogger("onepager.trace").exception("provider failed")
            out = stream.getvalue()
            assert "eyJhbGciOiJIUzI1NiJ9" not in out
            assert "REDACTED" in out
        finally:
            root.removeHandler(handler)


class TestNonObjectJsonBodyIs400:
    """Plan 110 Stage 5: a non-object JSON body (array/string/number) is a
    client error — it must return 400, not crash the route with a 500 from
    calling .get() on a non-dict (`get_json(silent=True) or {}` let it through)."""

    def test_cv_post_array_body_is_400(self, client, fake_supabase):
        for body in ([1, 2, 3], "hello", 42):
            res = client.post("/api/cv", json=body)
            assert res.status_code == 400, f"{body!r} -> {res.status_code}"

    def test_cvs_post_array_body_is_400(self, client, fake_supabase):
        res = client.post("/api/cvs", json=[1, 2, 3])
        assert res.status_code == 400, res.get_data(as_text=True)

    def test_account_delete_non_object_body_is_400(self, client, bypass_auth, fake_supabase):
        """Follow-up: /api/account/delete used the legacy `get_json() or {}` and
        500'd on a non-object body (`42` -> int.get()). It now returns 400."""
        for body in ([1, 2, 3], "x", 42):
            res = client.post("/api/account/delete", json=body)
            assert res.status_code == 400, f"{body!r} -> {res.status_code}"
