"""Browser tests for editing the notes, right to left text and the PDF button for scripts the PDF cannot draw.

The server runs on a data folder with finished notes written in advance, so no audio is needed. Every service
address points at a closed local port: nothing leaves the machine.
"""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

from conftest import FFMPEG  # noqa: E402
from test_ui import ROOT, free_port  # noqa: E402
from test_visuals_ui import chromium  # noqa: E402,F401  (fixture)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 390, "height": 844}

ARABIC_POINT = "تم تأجيل الإطلاق إلى الأسبوع المقبل."
CHINESE_POINT = "发布日期推迟到下周。"

NOTES = {
    "aaaa00000001": ("en", {
        "title": "Garden plan",
        "summary": "The garden will be planted in April. Seeds are bought first.",
        "key_points": ["Planting starts in April."],
        "actions": ["Buy seeds"],
        "sections": [
            {"heading": "Planting", "bullets": ["Beans go in first.", "Tomatoes wait for May."]},
            {"heading": "Tools", "bullets": ["The spade needs a new handle."]},
        ],
    }),
    "aaaa00000002": ("ar", {
        "title": "خطة الإطلاق",
        "summary": ARABIC_POINT,
        "key_points": [ARABIC_POINT],
        "actions": [],
        "sections": [{"heading": "الجدول", "bullets": [ARABIC_POINT, ARABIC_POINT + " 2"]}],
    }),
    "aaaa00000003": ("zh", {
        "title": "发布计划",
        "summary": CHINESE_POINT,
        "key_points": [],
        "actions": [],
        "sections": [{"heading": "时间", "bullets": [CHINESE_POINT, CHINESE_POINT + "。"]}],
    }),
}


def seed(data):
    for nid, (lang, notes) in NOTES.items():
        d = data / "jobs" / nid
        d.mkdir(parents=True)
        job = {
            "id": nid, "filename": "memo.m4a", "language": lang, "created": time.time(), "status": "done", "stage": "done",
            "stage_started": time.time(), "progress": 1.0, "detail": "Done", "error": None, "retryable": False,
            "duration": 61.0, "transcript": "A short transcript of the memo.", "notes": notes, "engine": "rule-based",
            "engine_detail": None, "plan": None,
        }
        (d / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    data = tmp_path_factory.mktemp("edit-data")
    seed(data)
    port = free_port()
    closed = "http://127.0.0.1:1"
    env = dict(
        os.environ,
        GROQ_API_KEY="test-key-not-real",
        GROQ_API_URL=closed + "/openai/v1/audio/transcriptions",
        GROQ_CHAT_URL=closed + "/openai/v1/chat/completions",
        GROQ_MODELS_URL=closed + "/openai/v1/models",
        OLLAMA_HOST_URL=closed,
        USE_OLLAMA="0",
        USE_GROQ_CHAT="0",
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


def open_seeded(chromium, base, nid, viewport=DESKTOP, scheme="light"):
    ctx = chromium.new_context(viewport=viewport, color_scheme=scheme)
    page = ctx.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.goto(f"{base}/#/note/{nid}")
    page.wait_for_selector("#view-result:not([hidden])", timeout=15000)
    return ctx, page, problems


# ------------------------------------------------------------------ editing

def test_edit_the_notes_and_save(chromium, stack):
    ctx, page, problems = open_seeded(chromium, stack, "aaaa00000001")
    try:
        assert page.locator("#edit-notes").is_visible() and not page.locator("#edit-form").is_visible()
        page.click("#edit-notes")
        assert page.locator("#edit-form").is_visible()
        assert not page.locator("#sections").is_visible() and not page.locator("#summary-block").is_visible()
        assert not page.locator("#rename").is_visible()
        assert page.evaluate("document.activeElement.id") == "edit-title"
        assert page.input_value("#edit-title") == "Garden plan"
        assert page.input_value("#edit-keypoints") == "Planting starts in April."
        assert page.locator(".edit-section").count() == 2
        assert page.locator(".edit-section legend").nth(1).inner_text() == "Section 2"
        assert page.locator("#edit-notes").is_disabled() and page.locator("#rebuild").is_disabled()

        # Escape leaves without saving and puts the focus back on the button
        page.fill("#edit-title", "Not saved")
        page.keyboard.press("Escape")
        assert not page.locator("#edit-form").is_visible()
        assert page.locator("#result-title").inner_text() == "Garden plan"
        assert page.evaluate("document.activeElement.id") == "edit-notes"

        # An empty title and a note without points are refused with a message
        page.click("#edit-notes")
        page.fill("#edit-title", "  ")
        page.click("#edit-save")
        assert "title cannot be empty" in page.locator("#edit-error").inner_text()
        assert page.evaluate("document.activeElement.id") == "edit-title"

        # Real edits: title, summary, a corrected bullet, a removed section, a new section and a new task
        page.fill("#edit-title", "Spring garden plan")
        page.fill("#edit-summary", "Planting starts in April, after the seeds are bought.")
        page.locator(".edit-section textarea").first.fill("Beans go in first.\n\nPeas go in with the beans.\n")
        page.locator(".edit-section").nth(1).get_by_role("button", name="Remove this section").click()
        assert page.locator(".edit-section").count() == 1
        page.click("#edit-add")
        assert page.locator(".edit-section").count() == 2
        assert page.evaluate("document.activeElement.id").startswith("edit-h-")
        page.keyboard.type("Watering")
        page.locator(".edit-section textarea").nth(1).fill("Water in the morning.")
        page.fill("#edit-actions", "Buy seeds\nOrder a hose")
        page.click("#edit-save")

        page.locator("#result-title", has_text="Spring garden plan").wait_for()
        assert not page.locator("#edit-form").is_visible()
        assert page.locator("#copy-status").inner_text() == "Changes saved."
        assert page.title() == "Spring garden plan - Voice Notes"
        assert page.locator("#summary").inner_text() == "Planting starts in April, after the seeds are bought."
        bullets = page.locator("#sections li").all_inner_texts()
        assert bullets == ["Beans go in first.", "Peas go in with the beans.", "Water in the morning."]
        assert page.locator("#sections h3").all_inner_texts() == ["Planting", "Watering"]
        assert page.locator("#actions-list li").all_inner_texts() == ["Buy seeds", "Order a hose"]
        assert page.locator("#result-meta").inner_text().count("Edited by you") == 1
        assert page.locator("#edit-notes").is_enabled() and page.locator("#rebuild").is_enabled()
        page.locator("#side-list", has_text="Spring garden plan").wait_for()

        # The exports and a reload show the edited text
        with page.expect_download() as dl:
            page.click("#exp-md")
        with open(dl.value.path(), encoding="utf-8") as fh:
            text = fh.read()
        assert text.startswith("# Spring garden plan") and "Peas go in with the beans." in text and "Tomatoes" not in text
        page.reload()
        page.locator("#result-title", has_text="Spring garden plan").wait_for()
        assert "Water in the morning." in page.locator("#sections").inner_text()

        # A rebuild of an edited note asks first and says that the edits will be replaced
        page.click("#rebuild")
        assert "changes you made by hand" in page.locator("#confirm-text").inner_text()
        page.click("#confirm-cancel")
        assert problems == []
    finally:
        ctx.close()


def test_a_note_without_any_point_cannot_be_saved(chromium, stack):
    ctx, page, problems = open_seeded(chromium, stack, "aaaa00000001")
    try:
        page.click("#edit-notes")
        for box in page.locator(".edit-section textarea").all():
            box.fill("")
        page.click("#edit-save")
        assert "at least one section" in page.locator("#edit-error").inner_text()
        assert page.locator("#edit-form").is_visible()
    finally:
        ctx.close()


def test_editing_on_a_phone_fits_the_screen(chromium, stack):
    ctx, page, problems = open_seeded(chromium, stack, "aaaa00000001", PHONE)
    try:
        page.click("#edit-notes")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        too_small = page.evaluate("""() => [...document.querySelectorAll('#edit-form input, #edit-form textarea, #edit-form button')]
            .filter(e => e.offsetParent !== null && e.getBoundingClientRect().height < 40).map(e => e.id || e.className)""")
        assert too_small == []
        right = page.evaluate("""() => [...document.querySelectorAll('#edit-form input, #edit-form textarea')]
            .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1).map(e => e.id)""")
        assert right == []
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_edit_form_text_is_readable(chromium, stack, scheme):
    ctx, page, problems = open_seeded(chromium, stack, "aaaa00000001", DESKTOP, scheme)
    try:
        page.click("#edit-notes")
        ratios = page.evaluate("""() => {
            function rgb(c) { const m = c.match(/[\\d.]+/g).map(Number); return m; }
            function lum(c) { const a = c.slice(0, 3).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
                return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2]; }
            function bg(e) { while (e) { const c = rgb(getComputedStyle(e).backgroundColor); if (c.length < 4 || c[3] > 0.5) return c; e = e.parentElement; } return [255, 255, 255]; }
            const out = [];
            for (const e of document.querySelectorAll('#edit-form label, #edit-form input, #edit-form textarea, #edit-form legend, .edit-intro')) {
                if (e.offsetParent === null) continue;
                const f = lum(rgb(getComputedStyle(e).color)), b = lum(bg(e));
                const r = (Math.max(f, b) + 0.05) / (Math.min(f, b) + 0.05);
                out.push([e.id || e.className || e.tagName, r]);
            }
            return out;
        }""")
        assert ratios and all(r >= 4.5 for _, r in ratios), ratios
    finally:
        ctx.close()


# ------------------------------------------------- direction, language, PDF

def test_arabic_notes_read_right_to_left(chromium, stack):
    ctx, page, problems = open_seeded(chromium, stack, "aaaa00000002")
    try:
        assert page.evaluate("getComputedStyle(document.querySelector('#sections li')).direction") == "rtl"
        assert page.evaluate("getComputedStyle(document.querySelector('#summary')).direction") == "rtl"
        assert page.evaluate("getComputedStyle(document.querySelector('#result-title')).direction") == "rtl"
        assert page.get_attribute("#panel-notes", "lang") == "ar"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.click("#tab-transcript")
        assert page.get_attribute("#panel-transcript", "lang") == "ar"
        # an English note is untouched: left to right and no lang attribute
        page.goto(f"{stack}/#/note/aaaa00000001")
        page.locator("#result-title", has_text="Garden plan").wait_for()
        assert page.evaluate("getComputedStyle(document.querySelector('#sections li')).direction") == "ltr"
        assert page.get_attribute("#panel-notes", "lang") == "en"
    finally:
        ctx.close()


@pytest.mark.parametrize("nid, word", [("aaaa00000002", "Arabic"), ("aaaa00000003", "Chinese")])
def test_pdf_button_explains_why_it_does_not_work(chromium, stack, nid, word):
    ctx, page, problems = open_seeded(chromium, stack, nid)
    try:
        assert page.get_attribute("#exp-pdf", "aria-disabled") == "true"
        assert word in page.get_attribute("#exp-pdf", "title")
        page.click("#exp-pdf", force=True)  # Playwright treats aria-disabled as disabled; a person can still click
        assert word in page.locator("#copy-status").inner_text()
        # Markdown still downloads, with every character
        with page.expect_download() as dl:
            page.click("#exp-md")
        with open(dl.value.path(), encoding="utf-8") as fh:
            assert "#" in fh.read()
        assert httpx.get(f"{stack}/api/notes/{nid}/export?format=pdf").status_code == 422
    finally:
        ctx.close()


def test_pdf_button_works_for_latin_notes(chromium, stack):
    ctx, page, problems = open_seeded(chromium, stack, "aaaa00000001")
    try:
        assert page.get_attribute("#exp-pdf", "aria-disabled") is None
        with page.expect_download() as dl:
            page.click("#exp-pdf")
        assert dl.value.suggested_filename.endswith(".pdf")
    finally:
        ctx.close()


def test_the_language_choice_is_remembered(chromium, stack):
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    try:
        page.goto(stack)
        assert page.input_value("#language") == "auto"
        page.select_option("#language", "fr")
        page.reload()
        assert page.input_value("#language") == "fr"
        page.select_option("#language", "auto")
        page.reload()
        assert page.input_value("#language") == "auto"
    finally:
        ctx.close()
