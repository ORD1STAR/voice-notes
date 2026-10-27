"""Browser tests for renaming, searching, action items, retrying and the status shown while a note is made."""
import os
import re
import subprocess
import sys
import time

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

import fakes  # noqa: E402
from conftest import FFMPEG  # noqa: E402
from test_ui import ROOT, free_port  # noqa: E402
from test_visuals_ui import chromium, open_note, rich_stack, sample  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")

DESKTOP = {"width": 1280, "height": 900}


def start_server(tmp_path_factory, name, fake, **env_extra):
    data = tmp_path_factory.mktemp(name)
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
    env.update(env_extra)
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                return base, proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    pytest.fail("server did not start")


@pytest.fixture(scope="module")
def rules_stack(tmp_path_factory):
    """A server where Ollama is not running, as on the real server: the rule-based fallback is used."""
    fake = fakes.start_fake(delay=1.2)
    base, proc = start_server(tmp_path_factory, "rules-data", fake, OLLAMA_HOST_URL="http://127.0.0.1:1")
    yield base, fake
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


def post_note(base, path):
    with open(path, "rb") as fh:
        r = httpx.post(base + "/api/notes", files={"file": ("plan.m4a", fh, "audio/mp4")}, timeout=30)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def wait_note(base, note_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = httpx.get(f"{base}/api/notes/{note_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError("note did not finish")


def wait_idle(base, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if all(n["status"] not in ("queued", "processing") for n in httpx.get(base + "/api/notes").json()):
            return
        time.sleep(0.2)
    raise AssertionError("the server stayed busy")


def make_note(base, path, title=None):
    job = wait_note(base, post_note(base, path))
    assert job["status"] == "done", job
    if title:
        r = httpx.patch(f"{base}/api/notes/{job['id']}", json={"title": title})
        assert r.status_code == 200
    return job["id"]


# ------------------------------------------------------------------ rename

def test_rename_a_note(chromium, rich_stack, sample):
    ctx, page, problems = open_note(chromium, rich_stack, sample, DESKTOP)
    assert page.locator("#rename").is_visible() and not page.locator("#rename-form").is_visible()
    page.click("#rename")
    assert page.locator("#rename-form").is_visible() and not page.locator("#result-title").is_visible()
    assert page.evaluate("document.activeElement.id") == "rename-input"
    assert page.input_value("#rename-input") == "Quarterly launch plan"

    # Escape keeps the old title and gives the focus back to the button
    page.fill("#rename-input", "Not saved")
    page.keyboard.press("Escape")
    assert page.locator("#result-title").inner_text() == "Quarterly launch plan"
    assert page.evaluate("document.activeElement.id") == "rename"

    # An empty title is not accepted
    page.click("#rename")
    page.fill("#rename-input", "   ")
    page.keyboard.press("Enter")
    assert page.locator("#rename-form").is_visible()
    page.keyboard.press("Escape")

    # Enter saves; the page, the tab title, the list, the exports and a reload all show the new title
    page.click("#rename")
    page.fill("#rename-input", "Zebra launch plan")
    page.keyboard.press("Enter")
    page.locator("#result-title", has_text="Zebra launch plan").wait_for()
    assert page.title() == "Zebra launch plan - Voice Notes"
    page.locator("#side-list", has_text="Zebra launch plan").wait_for()
    assert page.locator("#copy-status").inner_text() == "Renamed."
    with page.expect_download() as dl:
        page.click("#exp-md")
    assert dl.value.suggested_filename == "zebra-launch-plan.md"
    with open(dl.value.path(), encoding="utf-8") as fh:
        assert fh.read().startswith("# Zebra launch plan")
    page.reload()
    page.locator("#result-title", has_text="Zebra launch plan").wait_for()
    assert problems == [], problems
    ctx.close()


# ------------------------------------------------------------------ search

def test_search_the_list(chromium, rich_stack, sample):
    quokka = make_note(rich_stack, sample, "Quokka garden plan")
    axolotl = make_note(rich_stack, sample, "Axolotl budget review")
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.goto(rich_stack)
    page.locator("#side-list li").first.wait_for()
    total = page.locator("#side-list li").count()
    assert total >= 2

    page.fill("#search", "quokka")
    page.locator("#search-status", has_text="1 note found").wait_for()
    expect(page.locator("#side-list li")).to_have_count(1)
    assert page.locator("#side-list mark").first.inner_text().lower() == "quokka"

    # Words inside the notes count too, a title match comes first, and the snippet says where the word is
    page.fill("#search", "budget")
    page.locator("#search-status", has_text="notes found").wait_for()
    first = page.locator("#side-list li").first.inner_text()
    assert "Axolotl budget review" in first
    assert page.locator("#side-list .row-snippet").count() >= 1
    assert "budget" in page.locator("#side-list .row-snippet").first.inner_text().lower()

    # Several words must all be present
    page.fill("#search", "quokka budget")
    page.locator("#search-status", has_text="notes found").wait_for()
    page.locator("#search-status", has_text="1 note found").wait_for()

    page.fill("#search", "xyzzyplugh")
    page.locator("#side-empty", has_text='No notes match "xyzzyplugh"').wait_for()
    assert not page.locator("#side-list").is_visible()

    # Escape clears the search and brings the whole list back
    page.keyboard.press("Escape")
    page.locator("#side-list li").nth(total - 1).wait_for()
    assert page.input_value("#search") == ""

    # A result opens its note
    page.fill("#search", "quokka")
    page.locator("#search-status", has_text="1 note found").wait_for()
    page.click("#side-list a.row")
    page.wait_for_selector("#view-result:not([hidden])")
    assert page.locator("#result-title").inner_text() == "Quokka garden plan"
    assert quokka in page.url and axolotl not in page.url
    assert problems == [], problems
    ctx.close()


def test_search_on_a_phone_uses_the_start_page_list(chromium, rich_stack, sample):
    make_note(rich_stack, sample, "Okapi trip notes")
    ctx = chromium.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.goto(rich_stack)
    assert page.locator("#search-recent").is_visible() and not page.locator("#search").is_visible()
    page.locator("#recent-list li").first.wait_for()
    page.fill("#search-recent", "okapi")
    expect(page.locator("#recent-list li")).to_have_count(1)
    assert "Okapi trip notes" in page.locator("#recent-list").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.eval_on_selector("#search-recent", "e => e.getBoundingClientRect().height") >= 44
    ctx.close()


# ----------------------------------------------------------- action items

def test_action_items_can_be_checked_and_exported(chromium, rich_stack, sample):
    ctx, page, problems = open_note(chromium, rich_stack, sample, DESKTOP)
    assert page.locator("#actions-block").is_visible()
    boxes = page.locator("#actions-list input")
    assert boxes.count() == 3
    assert page.locator("#actions-count").inner_text() == "0 of 3 done"
    assert page.eval_on_selector("#actions-list label", "e => e.getBoundingClientRect().height") >= 44
    boxes.first.check()
    assert page.locator("#actions-count").inner_text() == "1 of 3 done"
    page.reload()  # the ticks are remembered in this browser
    page.wait_for_selector("#view-result:not([hidden])")
    assert page.locator("#actions-list input").first.is_checked()
    assert page.locator("#actions-count").inner_text() == "1 of 3 done"

    with page.expect_download() as dl:
        page.click("#exp-md")
    with open(dl.value.path(), encoding="utf-8") as fh:
        md = fh.read()
    assert "## Action items\n\n- [ ] Call the venue about the launch date" in md
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    page.click("#copy")
    page.locator("#copy-status", has_text="copied").wait_for()
    assert "- [ ] Send the final copy to marketing" in page.evaluate("navigator.clipboard.readText()")
    assert problems == [], problems
    ctx.close()


# -------------------------------------------- status while a note is made

def test_the_page_says_which_method_builds_the_notes(chromium, rules_stack, sample):
    base, _ = rules_stack
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    page.goto(base)
    page.locator("#method").wait_for()
    assert "built-in rules" in page.locator("#method-title").inner_text()
    text = page.locator("#method-text").inner_text()
    assert "not running" in text and "Ollama" in text
    assert "rule-based" in page.locator("#engine-line").inner_text()
    ctx.close()


def test_progress_view_shows_method_elapsed_time_and_queue(chromium, rules_stack, sample):
    base, _ = rules_stack
    first = post_note(base, sample)
    second = post_note(base, sample)  # has to wait for the first one
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.goto(f"{base}/#/note/{second}")
    page.locator("#progress-detail", has_text="Waiting for 1 other note").wait_for(timeout=10000)
    assert "built-in rules" in page.locator("#progress-method-title").inner_text()
    wait_note(base, first)
    page.locator("#progress-detail", has_text="Transcribing").wait_for(timeout=20000)
    page.locator("#progress-elapsed", has_text="time so far").wait_for()
    assert re.search(r"\d:\d\d$", page.locator("#progress-elapsed").inner_text())
    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    note = page.locator("#engine-note")
    assert note.is_visible() and "built-in rules" in note.inner_text() and "not running" in note.inner_text()
    assert problems == [], problems
    ctx.close()


# ----------------------------------------------------------------- retry

def test_a_failed_note_can_be_tried_again(chromium, rules_stack, sample):
    base, fake = rules_stack
    wait_idle(base)
    fake.fail_next = 1
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    page.goto(base)
    page.set_input_files("#file", str(sample))
    page.click("#go")
    page.wait_for_selector("#view-error:not([hidden])", timeout=30000)
    assert "try again" in page.locator("#error-message").inner_text().lower()
    assert page.locator("#error-again").is_visible() and page.locator("#error-remove").is_visible()
    page.click("#error-again")
    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    assert page.locator("#sections h3").count() >= 1
    ctx.close()


def test_a_bad_file_offers_no_retry(chromium, rules_stack, tmp_path):
    base, _ = rules_stack
    bad = tmp_path / "notes.m4a"
    bad.write_text("this is not audio at all")
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    page.goto(base)
    page.set_input_files("#file", str(bad))
    page.click("#go")
    page.wait_for_selector("#view-error:not([hidden])", timeout=30000)
    assert not page.locator("#error-again").is_visible()
    assert page.locator("#error-retry").is_visible()
    ctx.close()
