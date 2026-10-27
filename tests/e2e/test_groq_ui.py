"""Browser tests for the language selector and for notes written through Groq (a fake service, no network)."""
import subprocess

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

import fakes  # noqa: E402
from conftest import FFMPEG  # noqa: E402
from test_notes_ui import DESKTOP, start_server, wait_idle  # noqa: E402
from test_visuals_ui import chromium  # noqa: E402,F401  (fixture)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")

LANGUAGE_OPTIONS = ["Auto", "English", "French", "Arabic", "Spanish", "German", "Italian", "Portuguese", "Turkish", "Chinese"]


@pytest.fixture(scope="module")
def groq_stack(tmp_path_factory):
    """Groq answers for transcription and for the notes; Ollama is not running, so the rules are the last resort."""
    fake = fakes.start_fake(delay=0.2)
    base, proc = start_server(tmp_path_factory, "groq-data", fake, USE_GROQ_CHAT="1", OLLAMA_HOST_URL="http://127.0.0.1:1",
                              CHUNK_SECONDS="30")
    yield base, fake
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


@pytest.fixture(scope="module")
def short_audio(tmp_path_factory):
    path = tmp_path_factory.mktemp("short-audio") / "memo.m4a"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=4",
                    "-c:a", "aac", str(path)], check=True)
    return path


def new_page(chromium, base):
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.goto(base)
    return ctx, page, problems


def test_language_selector_sits_with_the_file_choice_and_defaults_to_auto(chromium, groq_stack):
    base, _ = groq_stack
    ctx, page, problems = new_page(chromium, base)
    select = page.locator("#language")
    assert select.is_visible() and select.input_value() == "auto"
    assert page.locator("label[for=language]").inner_text() == "Language of the recording"
    assert [o.strip() for o in page.locator("#language option").all_inner_texts()] == LANGUAGE_OPTIONS
    assert [o.get_attribute("value") for o in page.locator("#language option").all()] == [
        "auto", "en", "fr", "ar", "es", "de", "it", "pt", "tr", "zh"]
    # it is in the same block as the "Choose a file" button
    assert page.evaluate("document.getElementById('language').closest('#drop-idle') === document.getElementById('choose').closest('#drop-idle')")
    # using the selector must not open the file picker, the button still does
    opened = []
    page.on("filechooser", lambda chooser: opened.append(chooser))
    select.click()
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    assert opened == []
    page.click("#choose")
    page.wait_for_timeout(400)
    assert len(opened) == 1
    assert problems == [], problems
    ctx.close()


def test_status_card_names_groq_and_the_backup(chromium, groq_stack):
    base, _ = groq_stack
    ctx, page, _ = new_page(chromium, base)
    page.locator("#method").wait_for()
    assert page.locator("#method-title").inner_text() == "Notes are written by Groq llama-3.3-70b-versatile"
    assert "built-in rules take over" in page.locator("#method-text").inner_text()
    sync_api.expect(page.locator("#engine-line")).to_contain_text("Groq llama-3.3-70b-versatile")
    ctx.close()


def upload_and_wait(page, audio, language=None):
    if language:
        page.select_option("#language", language)
    page.set_input_files("#file", str(audio))
    page.click("#go")
    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)


def test_chosen_language_reaches_whisper_the_prompt_and_the_result(chromium, groq_stack, short_audio):
    base, fake = groq_stack
    wait_idle(base)
    ctx, page, problems = new_page(chromium, base)
    upload_and_wait(page, short_audio, "fr")
    meta = page.locator("#result-meta").inner_text()
    assert "Language: French" in meta and "Structured with Groq llama-3.3-70b-versatile" in meta
    assert fake.languages[-1] == "fr"
    system = fake.chat_requests[-1]["messages"][0]["content"]
    assert "The recording is in French" in system
    assert page.locator("#result-title").inner_text() == "Website launch planning"
    assert page.locator("#engine-note").is_hidden()
    assert problems == [], problems
    ctx.close()


def test_auto_leaves_the_language_out_of_the_transcription(chromium, groq_stack, short_audio):
    base, fake = groq_stack
    wait_idle(base)
    ctx, page, _ = new_page(chromium, base)
    upload_and_wait(page, short_audio)
    assert fake.languages[-1] is None
    assert "Language: Auto" in page.locator("#result-meta").inner_text()
    assert "language of the recording" in fake.chat_requests[-1]["messages"][0]["content"]
    ctx.close()


def test_when_groq_fails_the_page_says_so_and_rebuild_brings_groq_back(chromium, groq_stack, short_audio):
    base, fake = groq_stack
    wait_idle(base)
    fake.chat_fail = True
    ctx, page, problems = new_page(chromium, base)
    try:
        upload_and_wait(page, short_audio, "en")
    finally:
        fake.chat_fail = False
    assert "Structured with rules" in page.locator("#result-meta").inner_text()
    note = page.locator("#engine-note")
    assert note.is_visible() and "Groq returned an error" in note.inner_text() and "built-in rules" in note.inner_text()

    page.click("#rebuild")
    page.click("#confirm-delete")
    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    page.locator("#result-meta", has_text="Structured with Groq").wait_for(timeout=20000)
    assert page.locator("#engine-note").is_hidden()
    assert "Language: English" in page.locator("#result-meta").inner_text()
    assert problems == [], problems
    ctx.close()
