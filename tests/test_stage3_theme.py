"""Stage 3 — light/dark editor theme + topbar toggle.

These tests pin the contract the design phase fixed:

  * The PRINT stylesheet (``css_base.build_css``) is theme-agnostic — it must
    NOT carry ``[data-theme]`` selectors, ``prefers-color-scheme`` queries, or
    any dark-mode token. The PDF stays print/light regardless of the editor's
    UI theme. (Hard rule from plan 002 Stage 3 + the Constraints section.)

  * The EDITOR stylesheet (``style.css``) resolves theming cleanly through only
    two explicit modes: ``[data-theme="light"]`` and ``[data-theme="dark"]``.

  * State tokens (``--accent-fg``, ``--warning`` …) exist in BOTH modes so the
    toggle actually flips sub-page chrome.

  * The no-flash mechanism (a render-blocking ``theme-init.js`` in <head>
    BEFORE the stylesheet) and the topbar toggle are present on every editor
    page: the editor + the four sub-pages.

The CSS/HTML assertions are static text checks — they need no browser and run
in the container alongside the rest of the suite. The live-browser behaviour
(toggle cycles, chrome flips, no flash on reload) is verified separately with
Playwright per the stage's VERIFY step.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from render import css_base  # noqa: E402

STATIC = REPO / "tools" / "editor" / "static"

# The editor pages that own the theme chrome (landing/privacy stay dark by
# design and are intentionally excluded).
EDITOR_PAGES = (
    "index.html",
    "tailor.html",
    "jobs.html",
    "cover-letters.html",
    "account.html",
)

# Dark-mode token *values* that must never appear in the print stylesheet.
_DARK_VALUE_MARKERS = ("#0a0b0f", "#111319", "#e6e8ef", "#4488ff")


def _print_css() -> str:
    return css_base.build_css("file:///tmp", "#111111", font="serif")


def _style_css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# PDF / print stylesheet stays light — no theme machinery leaks in.
# ---------------------------------------------------------------------------
class TestPrintCssIsThemeAgnostic:
    def test_no_data_theme_selectors(self) -> None:
        css = _print_css()
        assert "data-theme" not in css, (
            "print CSS must not branch on [data-theme] — the PDF is always light"
        )

    def test_no_prefers_color_scheme(self) -> None:
        css = _print_css()
        assert "prefers-color-scheme" not in css, (
            "print CSS must not react to the OS colour scheme"
        )

    @pytest.mark.parametrize("marker", _DARK_VALUE_MARKERS)
    def test_no_dark_token_values(self, marker: str) -> None:
        css = _print_css().lower()
        assert marker not in css, (
            f"dark-mode colour {marker!r} leaked into the print stylesheet"
        )

    def test_print_css_source_untouched_by_theme(self) -> None:
        # Belt-and-suspenders: the source file itself carries no theme hooks,
        # so no future edit can sneak dark mode into the PDF via css_base.py.
        src = (REPO / "engine" / "render" / "css_base.py").read_text(encoding="utf-8")
        assert "data-theme" not in src
        assert "prefers-color-scheme" not in src


# ---------------------------------------------------------------------------
# Editor stylesheet: clean, explicit cascade.
# ---------------------------------------------------------------------------
class TestEditorThemeCascade:
    def test_explicit_light_block_exists(self) -> None:
        css = _style_css()
        assert ':root[data-theme="light"]' in css
        assert "color-scheme: light;" in css.split(':root[data-theme="light"]', 1)[1].split("}", 1)[0]

    def test_explicit_dark_block_exists(self) -> None:
        css = _style_css()
        assert ':root[data-theme="dark"]' in css
        assert "color-scheme: dark;" in css.split(':root[data-theme="dark"]', 1)[1].split("}", 1)[0]

    def test_no_auto_theme_mode_in_editor_chrome(self) -> None:
        css = _style_css()
        theme_js = (STATIC / "theme-init.js").read_text(encoding="utf-8")
        assert '[data-theme="auto"]' not in css
        assert "#theme-toggle.is-auto" not in css
        assert '"auto"' not in theme_js
        assert "prefers-color-scheme" not in theme_js

    @pytest.mark.parametrize("token", ["--accent-fg", "--warning", "--danger", "--success"])
    def test_state_tokens_defined_in_both_modes(self, token: str) -> None:
        css = _style_css()
        light = css[css.index(':root[data-theme="light"]') :]
        light_block = light[: light.index("}")]
        dark = css[css.index(':root[data-theme="dark"]') :]
        dark_block = dark[: dark.index("}")]
        assert f"{token}:" in light_block, f"{token} missing from the light block"
        assert f"{token}:" in dark_block, f"{token} missing from the dark block"

    @pytest.mark.parametrize(
        "token", ["--success-soft", "--warning-soft", "--danger-fg", "--danger-hover"]
    )
    def test_subpage_state_tokens_are_tokenized(self, token: str) -> None:
        # These are consumed by the sub-pages (jobs/tailor/account). They must
        # be real tokens in both modes so the toggle flips them — not just
        # hardcoded fallbacks frozen to one mode.
        css = _style_css()
        light = css[css.index(':root[data-theme="light"]') :]
        light_block = light[: light.index("}")]
        assert f"{token}:" in light_block, f"{token} not tokenized for light mode"
        dark = css[css.index(':root[data-theme="dark"]') :]
        dark_block = dark[: dark.index("}")]
        assert f"{token}:" in dark_block, f"{token} not tokenized for dark mode"


# ---------------------------------------------------------------------------
# No-flash mechanism + toggle present on every editor page.
# ---------------------------------------------------------------------------
class TestNoFlashAndToggleWiring:
    def test_theme_init_module_exists(self) -> None:
        f = STATIC / "theme-init.js"
        assert f.is_file()
        src = f.read_text(encoding="utf-8")
        # It reads the persisted theme and sets data-theme synchronously.
        assert "cv.editor.theme" in src
        assert "setAttribute" in src and "data-theme" in src
        assert "OnePagerTheme" in src
        assert 'var MODES = ["light", "dark"]' in src
        assert "set: set" in src
        assert "safeSet(concrete)" in src
        assert "style.colorScheme" in src

    @pytest.mark.parametrize("page", EDITOR_PAGES)
    def test_no_flash_script_before_stylesheet(self, page: str) -> None:
        html = (STATIC / page).read_text(encoding="utf-8")
        assert "/static/theme-init.js" in html, f"{page} missing the no-flash script"
        # It must come BEFORE style.css so data-theme is set before paint.
        i_script = html.index("/static/theme-init.js")
        i_style = html.index("/static/style.css")
        assert i_script < i_style, (
            f"{page}: theme-init.js must load before style.css (no-flash)"
        )

    @pytest.mark.parametrize("page", EDITOR_PAGES)
    def test_toggle_button_present(self, page: str) -> None:
        html = (STATIC / page).read_text(encoding="utf-8")
        if page != "index.html":
            # Subpage chrome (incl. the toggle) is injected at serve time
            # from the shared fragment (plan 108 stage 2).
            html += (STATIC / "_chrome.html").read_text(encoding="utf-8")
        assert 'id="theme-toggle"' in html, f"{page} missing the theme toggle button"
        assert 'data-theme="auto"' not in html, f"{page} must not expose auto theme mode"

    def test_no_inline_script_added(self) -> None:
        # The no-flash restore must stay an EXTERNAL script: an inline <script>
        # would be blocked by the strict CSP. Assert none of the editor pages
        # introduced an inline (srcless) script.
        inline = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
        for page in EDITOR_PAGES:
            html = (STATIC / page).read_text(encoding="utf-8")
            assert not inline.search(html), (
                f"{page}: inline <script> would violate the CSP — keep theme "
                "restore in the external theme-init.js"
            )

    def test_account_settings_owns_app_theme_and_new_cv_defaults(self) -> None:
        account = (STATIC / "account.html").read_text(encoding="utf-8")
        account_js = (STATIC / "account.js").read_text(encoding="utf-8")
        app_js = (STATIC / "app.js").read_text(encoding="utf-8")
        palette = (STATIC / "command-palette.js").read_text(encoding="utf-8")

        assert 'id="settings"' in account
        assert "App theme controls the editor chrome only" in account
        assert "CV Appearance remains document-specific" in account
        assert 'data-theme-choice="light"' in account
        assert 'data-theme-choice="dark"' in account
        assert 'id="ac-default-accent"' in account
        assert 'id="ac-default-template"' in account
        assert 'id="ac-default-paper"' in account
        assert "cv.editor.new_cv_defaults" in account_js
        assert "Existing CVs were not changed" in account_js
        assert "applyNewCvDefaultsToActiveModel" in app_js
        assert "App theme settings" in palette
        assert 'window.location.href = "/account#settings"' in palette
