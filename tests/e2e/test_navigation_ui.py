"""Browser tests for the outline of a note, reading time, back to top, keyboard shortcuts and the backup link."""
import io
import time
import zipfile

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

import fakes  # noqa: E402
import httpx  # noqa: E402
from conftest import FFMPEG  # noqa: E402
from test_design import CONTRAST_JS  # noqa: E402
from test_notes_ui import make_note, start_server  # noqa: E402
from test_visuals_ui import chromium, sample  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 360, "height": 700}


@pytest.fixture(scope="module")
def nav_stack(tmp_path_factory):
    fake = fakes.start_fake(delay=0.1, notes=fakes.RICH_NOTES)  # four sections, key points, tasks and diagrams
    base, proc = start_server(tmp_path_factory, "nav-data", fake)
    yield base
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


@pytest.fixture(scope="module")
def nav_note(nav_stack, sample):
    return make_note(nav_stack, sample)


def open_page(chromium, url, viewport=DESKTOP, scheme="light"):
    ctx = chromium.new_context(viewport=viewport, color_scheme=scheme, permissions=["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    return ctx, page, errors


def until(page, script, arg=None, timeout=10.0):
    """Poll a function in the page (the page's security policy forbids wait_for_function)."""
    end = time.time() + timeout
    while time.time() < end:
        value = page.evaluate(script, arg)
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {script}")


# ------------------------------------------------------------------ outline and reading time


def test_a_note_with_sections_has_an_outline_that_jumps_to_each_part(chromium, nav_stack, nav_note):
    ctx, page, errors = open_page(chromium, f"{nav_stack}/#/note/{nav_note}", viewport={"width": 1280, "height": 500})
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator("#outline")).to_be_visible()
    labels = page.locator("#outline-list .chip").all_inner_texts()
    headings = page.locator("#sections h3").all_inner_texts()
    assert labels[0] == "Summary" and labels[-len(headings):] == headings and len(headings) == 4
    assert "Key points" in labels and "Action items" in labels
    assert page.evaluate("window.scrollY") == 0
    page.locator("#outline-list .chip", has_text="Growth targets").click()
    until(page, "() => window.scrollY > 100")
    until(page, "() => document.activeElement === document.querySelectorAll('#sections section')[2]")
    assert errors == []
    ctx.close()


def test_the_outline_is_in_the_share_view_and_readable_in_both_color_schemes(chromium, nav_stack, nav_note):
    for scheme in ("light", "dark"):
        for viewport in (DESKTOP, PHONE):
            ctx, page, errors = open_page(chromium, f"{nav_stack}/n/{nav_note}", viewport=viewport, scheme=scheme)
            expect(page.locator("#outline")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (scheme, viewport)
            assert page.evaluate(CONTRAST_JS) == [], (scheme, viewport)
            heights = page.eval_on_selector_all("#outline-list .chip", "els => els.map(e => e.getBoundingClientRect().height)")
            assert heights and min(heights) >= 44
            assert errors == []
            ctx.close()


def test_a_short_note_has_no_outline(chromium, nav_stack, sample):
    note = make_note(nav_stack, sample)
    body = {"title": "Two parts", "summary": "Short.", "key_points": [], "actions": [],
            "sections": [{"heading": "One", "bullets": ["First point."]}, {"heading": "Two", "bullets": ["Second point."]}]}
    assert httpx.put(f"{nav_stack}/api/notes/{note}/notes", json=body).status_code == 200
    ctx, page, _ = open_page(chromium, f"{nav_stack}/#/note/{note}")
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator("#outline")).to_be_hidden()
    ctx.close()


def test_the_reading_time_is_in_the_details(chromium, nav_stack, nav_note):
    ctx, page, _ = open_page(chromium, f"{nav_stack}/#/note/{nav_note}")
    expect(page.locator("#result-meta")).to_contain_text("1 min read")
    ctx.close()


# ------------------------------------------------------------------ back to top


def test_back_to_top_appears_after_scrolling_and_returns(chromium, nav_stack, nav_note):
    ctx, page, _ = open_page(chromium, f"{nav_stack}/#/note/{nav_note}", viewport=PHONE)
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator("#to-top")).to_be_hidden()
    page.evaluate("document.querySelector('#sections').style.paddingBottom = '3000px'")
    page.evaluate("window.scrollTo(0, 1500)")
    expect(page.locator("#to-top")).to_be_visible()
    assert page.eval_on_selector("#to-top", "e => e.getBoundingClientRect().height") >= 44
    page.click("#to-top")
    until(page, "() => window.scrollY < 5")
    expect(page.locator("#to-top")).to_be_hidden()
    ctx.close()


# ------------------------------------------------------------------ keyboard


def test_shortcuts_search_new_note_neighbours_edit_and_help(chromium, nav_stack, nav_note, sample):
    make_note(nav_stack, sample)  # a newer note, so that next and previous have somewhere to go
    ctx, page, errors = open_page(chromium, f"{nav_stack}/#/note/{nav_note}")
    expect(page.locator("#view-result")).to_be_visible()
    page.locator("#result-title").focus()

    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "search"
    page.keyboard.type("nnjk?e")  # typing in the search box must not trigger shortcuts
    assert page.input_value("#search") == "nnjk?e"
    expect(page.locator("#keys")).not_to_be_visible()
    page.fill("#search", "")
    page.locator("#result-title").focus()

    # the list is newest first: "k" goes to a newer note, "j" back to this one
    mine = page.evaluate("location.hash")
    listed = ("h => { const a = document.querySelector('#side-list a[aria-current=page]');"
              " return !!a && '#/note/' + a.dataset.id === h && location.hash === h; }")
    until(page, listed, mine)  # the list knows where we are
    page.keyboard.press("k")
    until(page, "h => location.hash !== h", mine)
    newer = page.evaluate("location.hash")
    until(page, listed, newer)  # wait for the page to settle on the newer note before the next key
    page.locator("#result-title").focus()
    page.keyboard.press("j")
    until(page, "h => location.hash === h", mine)

    expect(page.locator("#result-title")).to_be_visible()
    page.locator("#result-title").focus()
    page.keyboard.press("e")
    expect(page.locator("#edit-form")).to_be_visible()
    page.goto(f"{nav_stack}/#/note/{nav_note}")
    page.reload()
    expect(page.locator("#view-result")).to_be_visible()
    page.locator("#result-title").focus()
    page.keyboard.press("n")
    until(page, "() => document.body.dataset.view === 'start'")

    page.keyboard.press("?")
    expect(page.locator("#keys")).to_be_visible()
    page.keyboard.press("n")  # a dialog is open: shortcuts are off
    expect(page.locator("#keys")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#keys")).not_to_be_visible()
    page.click("#keys-open")
    expect(page.locator("#keys")).to_be_visible()
    page.click("#keys-close")
    expect(page.locator("#keys")).not_to_be_visible()
    assert errors == []
    ctx.close()


def test_the_share_view_only_has_the_help_shortcut(chromium, nav_stack, nav_note):
    ctx, page, _ = open_page(chromium, f"{nav_stack}/n/{nav_note}")
    expect(page.locator("#view-result")).to_be_visible()
    page.locator("#result-title").focus()
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "result-title"
    page.keyboard.press("e")
    expect(page.locator("#edit-form")).to_be_hidden()
    page.keyboard.press("?")
    expect(page.locator("#keys")).to_be_visible()
    ctx.close()


# ------------------------------------------------------------------ backup link


def test_the_backup_link_downloads_a_zip_of_the_notes(chromium, nav_stack, nav_note):
    ctx, page, _ = open_page(chromium, f"{nav_stack}/")
    expect(page.locator("#backup")).to_be_visible()
    href = page.get_attribute("#backup", "href")
    r = page.request.get(nav_stack + href)
    assert r.status == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.body())).namelist()
    assert names and all(n.endswith(".md") for n in names)
    ctx.close()
    ctx, page, _ = open_page(chromium, f"{nav_stack}/n/{nav_note}")
    expect(page.locator("#backup")).to_be_hidden()
    ctx.close()


def test_the_backup_link_is_hidden_when_there_are_no_notes(chromium, tmp_path_factory):
    fake = fakes.start_fake()
    base, proc = start_server(tmp_path_factory, "empty-nav-data", fake)
    try:
        ctx, page, _ = open_page(chromium, base + "/")
        expect(page.locator("#view-start")).to_be_visible()
        expect(page.locator("#backup")).to_be_hidden()
        ctx.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        fake.shutdown()
