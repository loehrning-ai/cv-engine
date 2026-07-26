#!/usr/bin/env python3
"""Reproducible capture of the README screenshots and the project preview card.

Run it from a checkout root:

    python3 tools/screenshots/capture.py

What it does, in order:

1. Derives a **capture fixture** from ``content/cv.example.yaml`` (safe demo
   data) into a temporary directory, applying two edits: the brand accent and
   a set of fictional employer names. The header comment block is preserved
   verbatim by doing text substitution rather than a YAML round-trip — those
   comment lines are what the preview card is showing off.
2. Pins that fixture as the *only* document the app can see, via
   ``ONEPAGER_CV_FILE`` for the editor and ``--config`` for the PDF build.
   Neither the editor nor the engine will fall back to ``content/cv.yaml``.
3. Builds the one-page PDF and rasterises it with ``pdftoppm`` (``cv.png`` is
   never a browser screenshot — it is the real output).
4. Drives a headless Chromium over the running editor for the UI shots and the
   1696x1060 preview card.
5. **Proves** the captured output carries no real personal data: every string
   of rendered text (DOM of every page and frame, plus the PDF text layer) is
   checked against the identifying values in ``content/cv.yaml`` if that file
   exists. Any hit aborts the run and destroys the staging directory before
   anything is published.

Nothing is written into ``docs/screenshots/`` until step 5 passes. The run
stages every artifact in a temporary directory first.

Requires: playwright (with chromium installed), Pillow, and ``pdftoppm`` /
``pdftotext`` from poppler.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml
from PIL import Image

REPO = Path(__file__).resolve().parents[2]
CONTENT_DIR = REPO / "content"
EXAMPLE_CV = CONTENT_DIR / "cv.example.yaml"
REAL_CV = CONTENT_DIR / "cv.yaml"
OUT_DIR = REPO / "docs" / "screenshots"

sys.path.insert(0, str(REPO))
from tests.e2e.layout_helpers import (  # noqa: E402
    Viewport,
    auth_storage_state,
    goto_route,
    require_live_server,
)
import tests.e2e.layout_helpers as layout_helpers  # noqa: E402

DEFAULT_PORT = 5567

# Supabase pointed at a closed local port. It has to look *configured* — the
# login UI only renders the one-click demo button when ``/auth/config`` reports
# a URL and a key, and ``/auth/demo-login`` 503s without them — but it must not
# be *reachable*. The password grant then fails fast against a refused
# connection and the server issues its local demo token, while every data-plane
# query raises and falls back to the pinned fixture. Port 9 is discard.
_DEAD_SUPABASE_URL = "http://127.0.0.1:9"
_DEAD_SUPABASE_KEY = "sb_publishable_screenshot_capture_offline"

# Environment names blanked for the child process so the capture is hermetic.
# See ``start_server``.
_HERMETIC_BLANKS = (
    "SUPABASE_SERVICE_ROLE_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
)

# ── capture fixture ────────────────────────────────────────────────────────
#
# ``accent`` is a first-class user-configurable field, so painting the demo
# document in the brand copper is a real supported configuration rather than a
# costume. Paper, font, density and template keep their example defaults.
FIXTURE_ACCENT = "#a5370f"

# The shipped example uses the sitcom placeholder trio (Globex / Initech /
# Hooli), which reads as a joke in a hero image. These replacements are
# constructed names — each was checked against company registries and matches
# no real firm — that read like ordinary European tech employers.
FIXTURE_COMPANIES = {
    "Globex": "Nordhelm Systems",
    "Initech": "Brantfeld Analytics",
    "Hooli": "Krayfeld Logistik",
}

# ── README shots ───────────────────────────────────────────────────────────
README_VIEWPORT = Viewport(1440, 900)
README_SCALE = 2

# ── preview card ───────────────────────────────────────────────────────────
#
# 1696x1060 is exactly 2x the 848 CSS px at which the website detail page
# renders the figure. Capturing at PREVIEW_CSS_WIDTH and downscaling to that
# is what sets the apparent type size on the site: the effective factor is
# 848 / PREVIEW_CSS_WIDTH. Lower the CSS width to make the YAML keys bigger.
#
# The floor is set by the preview toolbar, not by legibility. Below ~1220 CSS
# px the flex row squeezes the ``1 page`` chip and clips its label — the one
# element the card exists to show. 1260 is the first width at which the chip
# reaches its natural 72 px and stays there, so a different font stack on
# another machine will not quietly reintroduce the clip. The height is the
# 16:10 partner of the width, so the crop is a no-op rather than a guess.
PREVIEW_CSS_WIDTH = 1260
PREVIEW_CSS_HEIGHT = 788
PREVIEW_SCALE = 2
PREVIEW_OUT_W = 1696
PREVIEW_OUT_H = 1060
PREVIEW_QUALITY = 82
PREVIEW_METHOD = 6
# The real render width on the website detail page; the acceptance check
# downscales the finished file to this and asks whether YAML keys survive.
ACCEPTANCE_WIDTH = 848


class CaptureError(RuntimeError):
    """Anything that should stop the run and bin the staged output."""


# ───────────────────────────────────────────────────────────────────────────
#  fixture
# ───────────────────────────────────────────────────────────────────────────


def write_fixture(dest: Path) -> Path:
    """Derive the capture fixture from the shipped example CV.

    Deliberately a text substitution, not a ``yaml.safe_load`` /
    ``yaml.safe_dump`` round-trip: the example's leading comment block states
    the one-page guarantee, and the preview card exists to show it.
    """
    if not EXAMPLE_CV.exists():
        raise CaptureError(f"missing example CV: {EXAMPLE_CV}")

    text = EXAMPLE_CV.read_text(encoding="utf-8")

    accent_sub, n = re.subn(
        r"^accent:.*$", f"accent: '{FIXTURE_ACCENT}'", text, count=1, flags=re.MULTILINE
    )
    if n != 1:
        raise CaptureError("could not find a top-level 'accent:' key in the example CV")
    text = accent_sub

    for old, new in FIXTURE_COMPANIES.items():
        text, n = re.subn(rf"^(\s*company:\s*){re.escape(old)}\s*$", rf"\g<1>{new}", text, flags=re.MULTILINE)
        if n != 1:
            raise CaptureError(f"expected exactly one 'company: {old}' line, found {n}")

    # Validate without writing back, so the comments survive.
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict) or not parsed.get("name"):
        raise CaptureError("derived fixture did not parse into a CV mapping")
    if parsed.get("accent") != FIXTURE_ACCENT:
        raise CaptureError("accent substitution did not take")

    dest.write_text(text, encoding="utf-8")
    return dest


# ───────────────────────────────────────────────────────────────────────────
#  PII proof
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PiiNeedles:
    literals: tuple[str, ...]
    digit_runs: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.literals and not self.digit_runs


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def collect_pii_needles() -> PiiNeedles:
    """Identifying strings from the operator's real CV, if it is on disk.

    Scoped to name, e-mail addresses and phone numbers on purpose. Broadening
    this to every field would false-positive on cities and job titles that the
    demo fixture legitimately shares with the real document.
    """
    if not REAL_CV.exists():
        return PiiNeedles((), ())

    try:
        doc = yaml.safe_load(REAL_CV.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CaptureError(f"could not parse {REAL_CV} to derive the PII guard: {exc}") from exc

    literals: set[str] = set()
    digit_runs: set[str] = set()

    name = str(doc.get("name") or "").strip()
    if name:
        literals.add(name)
        # A surname alone is identifying and shows up in headers on its own.
        literals.update(tok for tok in re.split(r"\s+", name) if len(tok) >= 4)

    for entry in doc.get("contact") or []:
        if not isinstance(entry, dict):
            continue
        for value in (entry.get("username"), entry.get("label")):
            value = str(value or "").strip()
            if not value:
                continue
            if "@" in value:
                literals.add(value)
            elif len(_digits(value)) >= 7:
                digit_runs.add(_digits(value))

    return PiiNeedles(tuple(sorted(literals)), tuple(sorted(digit_runs)))


def assert_no_pii(needles: PiiNeedles, sources: dict[str, str]) -> None:
    """Positive check on captured output. Raises on any hit."""
    if needles.empty:
        print("  · PII guard: content/cv.yaml absent or carries no identifiers — nothing to match")
        return

    hits: list[str] = []
    for label, text in sources.items():
        haystack = text.lower()
        haystack_digits = _digits(text)
        for literal in needles.literals:
            if literal.lower() in haystack:
                hits.append(f"{label}: literal {literal!r}")
        for run in needles.digit_runs:
            if run in haystack_digits:
                hits.append(f"{label}: digit run {run!r}")

    if hits:
        raise CaptureError(
            "REAL PERSONAL DATA FOUND IN CAPTURED OUTPUT — nothing will be published.\n  "
            + "\n  ".join(hits)
        )

    print(
        f"  · PII guard: {len(needles.literals)} literal(s) + {len(needles.digit_runs)} phone "
        f"digit-run(s) checked against {len(sources)} rendered text source(s) — clean"
    )


# ───────────────────────────────────────────────────────────────────────────
#  server
# ───────────────────────────────────────────────────────────────────────────


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def pick_port(preferred: int) -> int:
    for port in range(preferred, preferred + 40):
        if port_is_free(port):
            if port != preferred:
                print(f"  · port {preferred} was busy — bound {port} instead")
            return port
    raise CaptureError(f"no free port in {preferred}..{preferred + 39}")


def start_server(port: int, fixture: Path):
    """Run the editor app on ``port`` with the fixture pinned.

    ``server.main()`` hardcodes 5567, so the app object is imported and run
    directly rather than shelling out to the module — that keeps the port
    negotiable without touching server.py.
    """
    env = dict(os.environ)
    env["ONEPAGER_CV_FILE"] = str(fixture)  # absolute path replaces content/ entirely
    env["ONEPAGER_DEMO_MODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # Cut every route off the machine. A capture run must not be able to reach
    # a real backend or a model provider: whatever a cloud tenant happens to
    # hold would otherwise render straight into a published screenshot, and the
    # result would differ per operator. Setting the names here (rather than
    # deleting them) also neutralises a checked-out ``.env`` — python-dotenv
    # will not override a name already present in the environment. With
    # Supabase unreachable the app takes its local demo path and reads the
    # pinned fixture, which is exactly the state we want to photograph.
    env["SUPABASE_URL"] = _DEAD_SUPABASE_URL
    env["SUPABASE_PUBLISHABLE_KEY"] = _DEAD_SUPABASE_KEY
    env["SUPABASE_JWKS_URL"] = f"{_DEAD_SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    for key in _HERMETIC_BLANKS:
        env[key] = ""

    runner = (
        "import sys; sys.path.insert(0, 'tools/editor'); "
        "from server import app, CV_FILE; "
        "print('pinned CV:', CV_FILE, flush=True); "
        f"app.run(host='127.0.0.1', port={port}, debug=False, threaded=True, use_reloader=False)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", runner],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def wait_for_server(base_url: str, proc, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise CaptureError(f"editor exited early (code {proc.returncode}):\n{out}")
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as res:
                if res.status < 400:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(0.4)
    raise CaptureError(f"editor did not come up at {base_url}: {last}")


# ───────────────────────────────────────────────────────────────────────────
#  PDF
# ───────────────────────────────────────────────────────────────────────────


def build_pdf(fixture: Path, out_pdf: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / "engine" / "build.py"), "--config", str(fixture), "--output", str(out_pdf)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CaptureError(
            f"engine/build.py failed (code {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    if not out_pdf.exists():
        raise CaptureError(f"engine/build.py reported success but {out_pdf} is missing")


def rasterise_pdf(pdf: Path, out_png: Path, dpi: int = 150) -> None:
    if shutil.which("pdftoppm") is None:
        raise CaptureError("pdftoppm not found — install poppler")
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", "-singlefile", str(pdf), str(out_png.with_suffix(""))],
        check=True,
        capture_output=True,
    )
    if not out_png.exists():
        raise CaptureError(f"pdftoppm did not produce {out_png}")


def pdf_text(pdf: Path) -> str:
    if shutil.which("pdftotext") is None:
        return ""
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=False
    )
    return result.stdout or ""


# ───────────────────────────────────────────────────────────────────────────
#  browser
# ───────────────────────────────────────────────────────────────────────────


def frame_text(page) -> str:
    """Every scrap of rendered text on the page, including the preview iframe."""
    chunks: list[str] = []
    for frame in page.frames:
        try:
            chunks.append(frame.evaluate("() => document.body ? document.body.innerText : ''") or "")
        except Exception:  # noqa: BLE001 — a detached frame is not a failure
            continue
    return "\n".join(chunks)


def new_context(browser, viewport: Viewport, scale: int, theme: str = "light"):
    """A context the Viewport dataclass cannot express (it has no DPR field)."""
    context = browser.new_context(
        viewport={"width": viewport.width, "height": viewport.height},
        device_scale_factor=scale,
        reduced_motion="reduce",
        storage_state=auth_storage_state(browser),
        # Lets deterministic style/state tweaks bypass the app CSP.
        bypass_csp=True,
    )
    page = context.new_page()
    page.add_init_script(
        "localStorage.setItem('onepager.lang', 'en');"
        f"localStorage.setItem('cv.editor.theme', '{theme}');"
        "localStorage.setItem('cv.editor.tourCompleted', 'true');"
    )
    return context, page


def settle(page, ms: int = 1200) -> None:
    page.wait_for_timeout(ms)


def open_editor(page, viewport: Viewport) -> None:
    # goto_route performs the demo login click — the session lives in
    # sessionStorage, which Playwright storage_state does NOT carry.
    goto_route(page, "/app", viewport)
    page.wait_for_selector("#editor-pane", state="visible", timeout=30000)
    settle(page)


def select_view(page, view: str) -> None:
    page.click(f'.view-tab[data-view="{view}"]')
    page.wait_for_function(
        f"() => document.querySelector('.view-tab[data-view=\"{view}\"]')"
        ".getAttribute('aria-selected') === 'true'",
        timeout=15000,
    )
    settle(page, 800)
    park_pointer(page)


def park_pointer(page) -> None:
    """Park the cursor off every control so no hover tooltip is in the shot."""
    page.mouse.move(4, page.viewport_size["height"] - 4)
    settle(page, 700)


def select_preview_mode(page, mode: str) -> None:
    page.click(f'#mode-toggle .seg-btn[data-mode="{mode}"]')
    settle(page, 900)
    park_pointer(page)


def wait_for_one_page_chip(page) -> None:
    page.wait_for_selector("#page-count:not([hidden])", timeout=30000)
    page.wait_for_function(
        "() => { const t = document.querySelector('#page-count-text');"
        " const c = document.querySelector('#page-count');"
        " return t && c && /^1 page$/.test(t.textContent.trim()) && c.classList.contains('is-fit'); }",
        timeout=45000,
    )


def collapse_sidebar(page) -> None:
    page.keyboard.press("Meta+\\")
    page.wait_for_function(
        "() => { const s = document.querySelector('#sidebar');"
        " return s && s.classList.contains('collapsed'); }",
        timeout=15000,
    )
    settle(page, 700)


# ───────────────────────────────────────────────────────────────────────────
#  captures
# ───────────────────────────────────────────────────────────────────────────


def capture_readme_shots(browser, stage: Path, texts: dict[str, str]) -> list[str]:
    """The five full-window UI shots the README embeds."""
    produced: list[str] = []

    def shot(name: str, theme: str, prepare) -> None:
        context, page = new_context(browser, README_VIEWPORT, README_SCALE, theme=theme)
        try:
            open_editor(page, README_VIEWPORT)
            select_preview_mode(page, "paged")
            wait_for_one_page_chip(page)
            prepare(page)
            park_pointer(page)
            settle(page, 700)
            texts[f"dom:{name}"] = frame_text(page)
            (stage / name).write_bytes(page.screenshot())
            produced.append(name)
            print(f"  · {name}")
        finally:
            context.close()

    def form_view(page):
        select_view(page, "form")

    def form_experience(page):
        select_view(page, "form")
        page.evaluate(
            """
            () => {
              const heading = Array.from(document.querySelectorAll('#editor-pane *'))
                .find((el) => /experience/i.test((el.textContent || '').trim())
                  && el.children.length === 0);
              if (heading) heading.scrollIntoView({ block: 'start' });
            }
            """
        )

    def yaml_view(page):
        select_view(page, "yaml")

    def theme_picker(page):
        select_view(page, "form")
        page.click("#theme-btn")
        page.wait_for_selector("#theme-popover:not([hidden])", timeout=15000)

    shot("editor.png", "light", form_view)
    shot("editor-dark.png", "dark", form_view)
    shot("form-experience.png", "light", form_experience)
    shot("yaml-view.png", "light", yaml_view)
    shot("theme-picker.png", "light", theme_picker)
    return produced


def capture_preview_card(browser, stage: Path, texts: dict[str, str]) -> Path:
    """The 16:10 preview card: collapsed chrome, YAML left, paged preview right."""
    viewport = Viewport(PREVIEW_CSS_WIDTH, PREVIEW_CSS_HEIGHT)
    context, page = new_context(browser, viewport, PREVIEW_SCALE, theme="light")
    try:
        open_editor(page, viewport)
        collapse_sidebar(page)
        select_view(page, "yaml")
        select_preview_mode(page, "paged")
        wait_for_one_page_chip(page)
        # Keep the YAML scrolled to line 1: the header comment block is the
        # thing being quoted.
        page.evaluate(
            """
            () => {
              const cm = document.querySelector('.CodeMirror');
              if (cm && cm.CodeMirror) { cm.CodeMirror.scrollTo(0, 0); }
              const sc = document.querySelector('.CodeMirror-scroll');
              if (sc) sc.scrollTop = 0;
            }
            """
        )
        settle(page, 1200)
        texts["dom:preview-card"] = frame_text(page)
        raw = stage / "preview-card.raw.png"
        raw.write_bytes(page.screenshot())
        return raw
    finally:
        context.close()


def encode_preview_card(raw: Path, out: Path) -> None:
    """Crop the raw capture to 16:10 and encode the shipping WebP."""
    with Image.open(raw) as img:
        src = img.convert("RGB")
        target_h = round(src.width * PREVIEW_OUT_H / PREVIEW_OUT_W)
        if target_h > src.height:
            raise CaptureError(
                f"raw capture {src.width}x{src.height} is too short to crop to 16:10 "
                f"(needs {target_h}px)"
            )
        cropped = src.crop((0, 0, src.width, target_h))
        resized = cropped.resize((PREVIEW_OUT_W, PREVIEW_OUT_H), Image.LANCZOS)
        resized.save(out, "WEBP", quality=PREVIEW_QUALITY, method=PREVIEW_METHOD)


def acceptance_downscale(card: Path, out: Path) -> tuple[int, int]:
    """Render the card at the width the website detail page actually uses."""
    with Image.open(card) as img:
        height = round(img.height * ACCEPTANCE_WIDTH / img.width)
        img.convert("RGB").resize((ACCEPTANCE_WIDTH, height), Image.LANCZOS).save(out, "PNG")
        return ACCEPTANCE_WIDTH, height


# ───────────────────────────────────────────────────────────────────────────
#  main
# ───────────────────────────────────────────────────────────────────────────


def report(card: Path) -> None:
    sha = hashlib.sha256(card.read_bytes()).hexdigest()
    size = card.stat().st_size
    with Image.open(card) as img:
        decoded = img.size
    print("\n── preview-card.webp ──────────────────────────────────────────")
    print(f"  sha256    {sha}")
    print(f"  bytes     {size}")
    print(f"  decoded   {decoded[0]}x{decoded[1]}")
    print("───────────────────────────────────────────────────────────────")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="preferred editor port")
    parser.add_argument(
        "--card-only", action="store_true", help="skip the README shots, capture only the preview card"
    )
    parser.add_argument(
        "--keep-staging", action="store_true", help="leave the staging directory on disk for inspection"
    )
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    needles = collect_pii_needles()
    port = pick_port(args.port)
    base_url = f"http://127.0.0.1:{port}"

    workdir = Path(tempfile.mkdtemp(prefix="onepager-capture-"))
    stage = workdir / "stage"
    stage.mkdir()
    # Named after the file it is derived from, in its own temp directory: the
    # app titles the document from the filename stem, so this reads "Local Cv
    # Example" in the library rather than a scratch filename.
    fixture = workdir / "cv.example.yaml"
    proc = None
    texts: dict[str, str] = {}
    published = False

    try:
        print("Capture run")
        print(f"  · staging  {stage}")
        write_fixture(fixture)
        print(f"  · fixture  derived from content/cv.example.yaml (accent {FIXTURE_ACCENT}, "
              f"{len(FIXTURE_COMPANIES)} employer names replaced)")

        # PDF first: it does not need the server.
        pdf = workdir / "cv.pdf"
        build_pdf(fixture, pdf)
        rasterise_pdf(pdf, stage / "cv.png")
        texts["pdf:cv.pdf"] = pdf_text(pdf)
        print("  · cv.png (rasterised from the PDF at 150 dpi)")

        proc = start_server(port, fixture)
        wait_for_server(base_url, proc)
        # Point the shared helpers at this run's port before probing.
        layout_helpers.BASE_URL = base_url
        os.environ["ONEPAGER_E2E_BASE_URL"] = base_url
        require_live_server()
        print(f"  · editor up on {base_url}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                if not args.card_only:
                    capture_readme_shots(browser, stage, texts)
                raw = capture_preview_card(browser, stage, texts)
            finally:
                browser.close()

        card = stage / "preview-card.webp"
        encode_preview_card(raw, card)
        raw.unlink()
        print(f"  · preview-card.webp ({PREVIEW_OUT_W}x{PREVIEW_OUT_H}, "
              f"quality {PREVIEW_QUALITY}, method {PREVIEW_METHOD})")

        acceptance = stage / "preview-card.acceptance-848w.png"
        w, h = acceptance_downscale(card, acceptance)
        print(f"  · acceptance render {w}x{h} → {acceptance.name} (inspect it: the YAML keys must be legible)")

        # ── the gate ──────────────────────────────────────────────────────
        assert_no_pii(needles, texts)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for produced in sorted(stage.iterdir()):
            if produced.name.startswith("preview-card.acceptance"):
                continue
            shutil.copy2(produced, OUT_DIR / produced.name)
        published = True
        print(f"\n  published → {OUT_DIR.relative_to(REPO)}")
        report(OUT_DIR / "preview-card.webp")
        if args.keep_staging:
            print(f"  staging kept: {stage}")
            print(f"  acceptance render: {acceptance}")
        else:
            print("  acceptance render discarded with the staging dir "
                  "(re-run with --keep-staging to eyeball it)")
        return 0

    except CaptureError as exc:
        print(f"\nCAPTURE ABORTED\n{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — nothing may be published on any failure
        print(f"\nCAPTURE ABORTED ({type(exc).__name__})\n{exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if fixture.exists():
            fixture.unlink()
        if not args.keep_staging or not published:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
