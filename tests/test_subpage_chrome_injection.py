"""Plan 108 stage 2 — shared subpage chrome, injected at serve time.

The .cl-topbar markup exists exactly once, in
``tools/editor/static/_chrome.html``. The four subpages carry a
``__CHROME_TOPBAR__`` placeholder that ``server.py`` substitutes per
route (same serve-time pattern as the landing ``__PUBLIC_BASE_URL__``).
These tests pin the injection contract per route so chrome drift between
pages is structurally impossible, and prove the failure mode is loud.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

ROUTES = {
    "/jobs": {
        "title_key": "jobs.title",
        "title_text": "Jobs catalog",
        "credit_id": 'id="jb-credit"',
        "current_href": 'href="/jobs" class="cl-back" aria-current="page"',
    },
    "/cover-letters": {
        "title_key": "cl.title",
        "title_text": "Cover letters",
        "credit_id": 'id="cl-credit"',
        "current_href": 'href="/cover-letters" class="cl-back" aria-current="page"',
    },
    "/tailor": {
        "title_key": "app.tailor",
        "title_text": "ATS score &amp; review",
        "credit_id": 'id="tl-credit"',
        "current_href": 'href="/tailor" class="cl-back" aria-current="page"',
    },
    "/account": {
        "title_key": "account.title",
        "title_text": "Settings &amp; account",
        "credit_id": None,
        "current_href": 'href="/account" class="cl-back" aria-current="page"',
    },
    "/settings": {
        "title_key": "account.title",
        "title_text": "Settings &amp; account",
        "credit_id": None,
        "current_href": 'href="/account" class="cl-back" aria-current="page"',
    },
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_subpage_routes_serve_injected_shared_chrome(client, route):
    expected = ROUTES[route]
    res = client.get(route)
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # The placeholder never leaks to the browser; the chrome is present.
    assert "__CHROME_TOPBAR__" not in html
    assert "__CHROME_" not in html, "unsubstituted chrome token leaked"
    assert 'class="cl-topbar"' in html
    assert html.count('class="cl-topbar"') == 1

    # Per-route parameterisation.
    assert f'data-i18n="{expected["title_key"]}"' in html
    assert expected["title_text"] in html
    assert expected["current_href"] in html
    assert html.count('aria-current="page"') == 1
    if expected["credit_id"]:
        assert expected["credit_id"] in html

    # Shared invariants every page inherits from the fragment.
    assert 'data-i18n-attr="aria-label:nav.applications"' in html
    assert 'data-i18n-attr="aria-label:nav.account"' in html
    assert 'id="theme-toggle"' in html


def test_account_route_keeps_privacy_and_logout_extras(client):
    html = client.get("/account").get_data(as_text=True)
    assert 'id="ac-logout"' in html
    assert 'href="/privacy"' in html
    assert "ac-topbar-extra" in html


def test_missing_placeholder_fails_loudly(server_module):
    with pytest.raises(RuntimeError, match="__CHROME_TOPBAR__"):
        server_module._serve_subpage(
            "privacy.html",
            title_icon="shield",
            title_key="nav.privacy",
            title_text="Privacy",
            current="account",
            right_lead="",
        )


def test_unknown_current_marker_fails_loudly(server_module):
    with pytest.raises(ValueError, match="current-page marker"):
        server_module._subpage_chrome_html(
            title_icon="mail",
            title_key="cl.title",
            title_text="Cover letters",
            current="nonsense",
            right_lead="",
        )
