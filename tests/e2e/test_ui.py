"""Browser tests. They need Playwright with Chromium and ffmpeg, and skip otherwise.

    pip install -r requirements-dev.txt
    playwright install chromium
    pytest tests/e2e

Set SCREENSHOT_DIR to also save screenshots of the main screens.
"""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

import fakes  # noqa: E402
from conftest import FFMPEG  # noqa: E402

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")

SHOTS = os.environ.get("SCREENSHOT_DIR")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    fake = fakes.start_fake(delay=2.0)  # slower than the page's 1.5 s polling, so each step is seen
    port = free_port()
    env = dict(
        os.environ,
        GROQ_API_KEY="test-key-not-real",
        GROQ_API_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/audio/transcriptions",
        OLLAMA_HOST_URL=f"http://127.0.0.1:{fake.server_port}",
        OLLAMA_MODEL="qwen2.5:1.5b",
        USE_OLLAMA="1",
        CHUNK_SECONDS="10",
        DATA_DIR=str(data),
        FFMPEG_BIN=FFMPEG,
        PORT=str(port),
        HOST="127.0.0.1",
    )
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser, stack):
    ctx = browser.new_context(viewport={"width": 1100, "height": 900}, accept_downloads=True,
                              permissions=["clipboard-read", "clipboard-write"])
    pg = ctx.new_page()
    pg.goto(stack)
    yield pg
    ctx.close()


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "weekly-planning.m4a"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=25",
                    "-c:a", "aac", str(path)], check=True)
    return path


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / name), full_page=True)


def test_empty_state_and_layout(page):
    assert page.locator("h1").first.inner_text().startswith("Turn a voice note")
    assert page.locator("#side-empty").is_visible()
    assert page.locator("#go").is_disabled() or not page.locator("#picked").is_visible()
    sync_api.expect(page.locator("#engine-line")).to_contain_text("local model qwen2.5:1.5b")  # filled in after /api/health
    shot(page, "home.png")


def test_rejects_wrong_and_oversized_files(page, tmp_path):
    txt = tmp_path / "readme.txt"
    txt.write_text("not audio")
    page.set_input_files("#file", str(txt))
    assert "does not look like an audio" in page.locator("#file-error").inner_text()
    assert not page.locator("#picked").is_visible()

    big = tmp_path / "huge.wav"
    with open(big, "wb") as fh:
        fh.truncate(201 * 1024 * 1024)
    page.set_input_files("#file", str(big))
    assert "limit is 200 MB" in page.locator("#file-error").inner_text()


def test_keyboard_reaches_file_picker(page):
    reached = []
    for _ in range(6):
        page.keyboard.press("Tab")
        reached.append(page.evaluate("document.activeElement.id"))
        if reached[-1] == "file":
            break
    assert reached[0] == "" and "file" in reached, reached  # the skip link has no id


def test_full_flow_upload_to_exports(page, audio_file, tmp_path):
    page.set_input_files("#file", str(audio_file))
    assert page.locator("#picked-name").inner_text() == "weekly-planning.m4a"
    shot(page, "selected.png")
    page.click("#go")

    page.wait_for_selector("#view-progress:not([hidden])")
    # (Playwright's wait_for_function needs eval, which the page's content security policy forbids.)
    page.locator("#progress-detail", has_text="Transcribing part 2").wait_for(timeout=20000)
    shot(page, "progress.png")

    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    assert page.locator("#result-title").inner_text() == "Website launch planning"
    assert page.locator("#sections h3").count() == 3
    assert "Design finishes the landing page" in page.locator("#sections").inner_text()
    assert "qwen2.5:1.5b" in page.locator("#result-meta").inner_text()
    assert re.search(r"#/note/[0-9a-f]+$", page.url)
    shot(page, "notes.png")

    # Transcript tab, with arrow key navigation
    page.focus("#tab-notes")
    page.keyboard.press("ArrowRight")
    assert page.locator("#panel-transcript").is_visible()
    transcript = page.locator("#transcript").inner_text()
    assert transcript.count("landing page") == 1 and "Monday morning" in transcript
    shot(page, "transcript.png")
    page.click("#tab-notes")

    # Exports
    with page.expect_download() as dl:
        page.click("#exp-md")
    md = Path(dl.value.path()).read_text()
    assert dl.value.suggested_filename == "website-launch-planning.md"
    assert md.startswith("# Website launch planning") and "- Meet every Monday morning." in md
    assert "## Transcript" not in md

    with page.expect_download() as dl:
        page.click("#exp-txt")
    txt = Path(dl.value.path()).read_text()
    assert dl.value.suggested_filename.endswith(".txt") and "SUMMARY" in txt

    page.check("#with-transcript")
    with page.expect_download() as dl:
        page.click("#exp-pdf")
    pdf = Path(dl.value.path()).read_bytes()
    assert dl.value.suggested_filename.endswith(".pdf")
    assert pdf.startswith(b"%PDF-") and len(pdf) > 3000
    if SHOTS:
        Path(SHOTS, "sample-export.pdf").write_bytes(pdf)

    with page.expect_download() as dl:
        page.click("#exp-md")
    assert "## Transcript" in Path(dl.value.path()).read_text()

    # Copy
    page.click("#copy")
    page.locator("#copy-status", has_text="copied").wait_for()
    assert "copied" in page.locator("#copy-status").inner_text().lower()
    assert page.evaluate("navigator.clipboard.readText()").startswith("# Website launch planning")

    # The note is in the recent list and a reload keeps it
    page.reload()
    sync_api.expect(page.locator("#result-title")).to_have_text("Website launch planning")  # filled in after the fetch
    sync_api.expect(page.locator(".figure[data-kind=mindmap]")).to_have_count(1)
    page.click("#back")
    page.wait_for_selector("#side-list:not([hidden])")
    assert "Website launch planning" in page.locator("#side-list").inner_text()
    shot(page, "home-with-notes.png")

    # Delete, with the confirmation dialog: keeping the note first, then deleting it
    page.click("#side-list a.row")
    page.wait_for_selector("#view-result:not([hidden])")
    page.click("#delete")
    page.click("#confirm-cancel")
    assert page.locator("#view-result").is_visible()
    page.click("#delete")
    page.click("#confirm-delete")
    page.wait_for_selector("#view-start:not([hidden])")
    page.wait_for_selector("#side-empty:not([hidden])")


def test_server_error_is_shown_clearly(browser, stack, audio_file, monkeypatch):
    """A wrong API key on the fake service produces a readable error screen."""
    # The fake accepts only the right key; point the page at a server with a wrong one.
    fake = fakes.start_fake(delay=0.0, key="a-different-key")
    port = free_port()
    env = dict(os.environ, GROQ_API_KEY="wrong-key", GROQ_API_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/audio/transcriptions",
               USE_OLLAMA="0", DATA_DIR=str(audio_file.parent / "data2"), FFMPEG_BIN=FFMPEG,
               PORT=str(port), HOST="127.0.0.1")
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                httpx.get(base + "/api/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        ctx = browser.new_context()
        pg = ctx.new_page()
        pg.goto(base)
        assert "rule-based" in pg.locator("#engine-line").inner_text()
        pg.set_input_files("#file", str(audio_file))
        pg.click("#go")
        pg.wait_for_selector("#view-error:not([hidden])", timeout=20000)
        assert "Groq rejected the API key" in pg.locator("#error-message").inner_text()
        shot(pg, "error.png")
        # A failed note can be removed from the error screen and then leaves the list.
        assert pg.locator("#error-remove").is_visible()
        assert "Failed" in pg.locator("#side-list").inner_text()
        pg.click("#error-remove")
        pg.click("#confirm-delete")
        pg.wait_for_selector("#view-start:not([hidden])")
        pg.wait_for_selector("#side-empty:not([hidden])")
        ctx.close()
    finally:
        proc.terminate()
        fake.shutdown()


def test_mobile_layout(browser, stack, audio_file):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, device_scale_factor=2, is_mobile=True,
                              has_touch=True)
    pg = ctx.new_page()
    pg.goto(stack)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(pg, "mobile-home.png")
    pg.set_input_files("#file", str(audio_file))
    pg.click("#go")
    pg.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(pg, "mobile-notes.png")
    ctx.close()
