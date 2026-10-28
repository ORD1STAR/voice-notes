"""Browser tests for the share mode: the control in the note view, and what a visitor sees for a private note."""
import os
import subprocess
import sys
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

import fakes  # noqa: E402
import httpx  # noqa: E402
from conftest import FFMPEG  # noqa: E402
from test_ui import ROOT, free_port  # noqa: E402
from test_visuals_ui import chromium, sample  # noqa: E402,F401  (fixtures)

from app import auth  # noqa: E402

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")

PASSWORD = "browser test password"
DESKTOP = {"width": 1280, "height": 900}


@pytest.fixture(scope="module")
def stack(tmp_path_factory, sample):
    """A server with the login on, and one finished note made by the signed in owner."""
    fake = fakes.start_fake()
    port = free_port()
    env = dict(
        os.environ,
        GROQ_API_KEY="test-key-not-real",
        GROQ_API_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/audio/transcriptions",
        OLLAMA_HOST_URL="http://127.0.0.1:1",
        USE_OLLAMA="0",
        USE_GROQ_CHAT="0",
        CHUNK_SECONDS="10",
        DATA_DIR=str(tmp_path_factory.mktemp("share-mode-data")),
        FFMPEG_BIN=FFMPEG,
        PORT=str(port),
        HOST="127.0.0.1",
        VOICE_NOTES_AUTH="on",
        VOICE_NOTES_PASSWORD_HASH=auth.hash_password(PASSWORD),
    )
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/login", timeout=1).status_code in (200, 503):
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    api = httpx.Client(base_url=base, timeout=30)
    assert api.post("/login", data={"password": PASSWORD}).status_code in (200, 303)
    with open(sample, "rb") as fh:
        r = api.post("/api/notes", files={"file": ("plan.m4a", fh, "audio/mp4")})
    assert r.status_code == 202, r.text
    note_id = r.json()["id"]
    for _ in range(300):
        if api.get(f"/api/notes/{note_id}").json()["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert api.get(f"/api/notes/{note_id}").json()["status"] == "done"
    yield base, note_id
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


def owner_page(chromium, base, path):
    ctx = chromium.new_context(viewport=DESKTOP, permissions=["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    page.goto(base + "/login")
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector("#view-start:not([hidden])")
    page.goto(base + path)
    return ctx, page


def visitor_page(chromium, base, path):
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + path)
    return ctx, page, errors


def test_the_mode_is_shown_and_can_be_switched(chromium, stack):
    base, nid = stack
    ctx, page = owner_page(chromium, base, f"/#/note/{nid}")
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator("#share-mode")).to_contain_text("Private")
    expect(page.locator("#share-toggle")).to_have_text("Make public")
    page.click("#share-toggle")
    expect(page.locator("#share-mode")).to_contain_text("Public")
    expect(page.locator("#share-toggle")).to_have_text("Make private")
    expect(page.locator("#copy-status")).to_contain_text("now public")
    page.reload()  # the mode is kept by the server
    expect(page.locator("#share-mode")).to_contain_text("Public")

    # a visitor with no session reads the public note, with no sharing controls
    vctx, vpage, errors = visitor_page(chromium, base, f"/n/{nid}")
    expect(vpage.locator("#view-result")).to_be_visible()
    expect(vpage.locator("#result-title")).not_to_be_empty()
    expect(vpage.locator("#share-row")).to_be_hidden()
    assert errors == []
    vctx.close()

    page.click("#share-toggle")
    expect(page.locator("#share-mode")).to_contain_text("Private")
    expect(page.locator("#copy-status")).to_contain_text("now private")

    # the same link now shows a clear message to the visitor, and no note
    vctx, vpage, errors = visitor_page(chromium, base, f"/n/{nid}")
    expect(vpage.locator("#view-missing")).to_be_visible()
    expect(vpage.locator("#missing-title")).to_have_text("This note is private")
    expect(vpage.locator("#missing-login")).to_be_visible()
    expect(vpage.locator("#view-result")).to_be_hidden()
    assert errors == []
    vctx.close()

    # the owner opens the private link and reads the note
    page.goto(f"{base}/n/{nid}")
    expect(page.locator("#view-result")).to_be_visible()
    ctx.close()


def test_a_private_link_for_one_person(chromium, stack):
    base, nid = stack
    ctx, page = owner_page(chromium, base, f"/#/note/{nid}")
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator("#share-mode")).to_contain_text("Private")
    expect(page.locator("#share-token-off")).to_be_hidden()
    page.click("#share-token-new")
    expect(page.locator("#copy-status")).to_contain_text("Private link copied")
    link = page.evaluate("navigator.clipboard.readText()")
    assert link.startswith(f"{base}/n/{nid}?t=")
    expect(page.locator("#share-token-off")).to_be_visible()

    vctx, vpage, errors = visitor_page(chromium, base, link[len(base):])
    expect(vpage.locator("#view-result")).to_be_visible()
    assert "t=" in vpage.get_attribute("#exp-md", "href")
    assert errors == []
    vctx.close()

    # a link with a wrong secret shows the private message
    vctx, vpage, _ = visitor_page(chromium, base, f"/n/{nid}?t=wrong")
    expect(vpage.locator("#missing-title")).to_have_text("This note is private")
    vctx.close()

    page.click("#share-token-off")
    expect(page.locator("#share-token-off")).to_be_hidden()
    vctx, vpage, _ = visitor_page(chromium, base, link[len(base):])
    expect(vpage.locator("#missing-title")).to_have_text("This note is private")
    vctx.close()
    ctx.close()
