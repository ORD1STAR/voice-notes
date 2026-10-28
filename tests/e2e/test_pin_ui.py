"""Browser test for pinning a note to the top of the list."""
import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

from conftest import FFMPEG  # noqa: E402
from test_notes_ui import DESKTOP, make_note  # noqa: E402
from test_visuals_ui import chromium, rich_stack, sample  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")


def test_pin_a_note_to_the_top_and_unpin_it(chromium, rich_stack, sample):
    older = make_note(rich_stack, sample, "Pin test older note")
    make_note(rich_stack, sample, "Pin test newer note")
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    try:
        page.goto(f"{rich_stack}/#/note/{older}")
        page.wait_for_selector("#view-result:not([hidden])")
        assert page.locator("#pin").inner_text() == "Pin to top"
        assert "newer note" in page.locator("#side-list li").first.inner_text()
        assert page.locator("#side-list .pin-tag").count() == 0

        page.click("#pin")
        page.locator("#copy-status", has_text="Pinned to the top of the list.").wait_for()
        assert page.locator("#pin").inner_text() == "Unpin"
        first = page.locator("#side-list li").first
        expect(first).to_contain_text("Pin test older note")
        expect(first.locator(".pin-tag")).to_have_text("Pinned")

        # the pin is kept on the server
        page.reload()
        page.wait_for_selector("#view-result:not([hidden])")
        assert page.locator("#pin").inner_text() == "Unpin"
        expect(page.locator("#side-list li").first).to_contain_text("Pin test older note")

        # the button is not offered on a shared note
        page.goto(f"{rich_stack}/n/{older}")
        page.wait_for_selector("#view-result:not([hidden])")
        assert not page.locator("#pin").is_visible()

        page.goto(f"{rich_stack}/#/note/{older}")
        page.wait_for_selector("#view-result:not([hidden])")
        page.click("#pin")
        page.locator("#copy-status", has_text="Unpinned.").wait_for()
        assert page.locator("#pin").inner_text() == "Pin to top"
        expect(page.locator("#side-list li").first).to_contain_text("Pin test newer note")
        assert page.locator("#side-list .pin-tag").count() == 0
        assert problems == [], problems
    finally:
        httpx.put(f"{rich_stack}/api/notes/{older}/pin", json={"pinned": False})
        ctx.close()


def test_pinning_on_a_phone(chromium, rich_stack, sample):
    note = make_note(rich_stack, sample, "Pin test phone note with a rather long title to check that the buttons wrap")
    ctx = chromium.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    try:
        page.goto(f"{rich_stack}/#/note/{note}")
        page.wait_for_selector("#view-result:not([hidden])")
        assert page.locator("#pin").is_visible() and page.locator("#rename").is_visible()
        assert page.eval_on_selector("#pin", "e => e.getBoundingClientRect().height") >= 44
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.click("#pin")
        page.locator("#copy-status", has_text="Pinned to the top of the list.").wait_for()
        page.click("#back")
        page.locator("#recent-list .pin-tag").first.wait_for()
        expect(page.locator("#recent-list li").first).to_contain_text("Pin test phone note")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        httpx.put(f"{rich_stack}/api/notes/{note}/pin", json={"pinned": False})
        ctx.close()
