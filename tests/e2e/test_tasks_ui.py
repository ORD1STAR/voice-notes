"""Browser test for the list of open tasks across all notes."""
import json

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

from conftest import FFMPEG  # noqa: E402
from test_notes_ui import DESKTOP, make_note  # noqa: E402
from test_visuals_ui import chromium, rich_stack, sample  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")

TASKS = ["Call the bank on Monday", "Send the invoice to Anna"]


def note_with_tasks(base, sample_path, title):
    nid = make_note(base, sample_path, title)
    job = httpx.get(f"{base}/api/notes/{nid}").json()
    body = {"title": title, "summary": job["notes"]["summary"], "key_points": [], "actions": TASKS,
            "sections": job["notes"]["sections"]}
    r = httpx.put(f"{base}/api/notes/{nid}/notes", json=body)
    assert r.status_code == 200, r.text
    return nid


def test_open_tasks_of_all_notes(chromium, rich_stack, sample):
    nid = note_with_tasks(rich_stack, sample, "Tasks test note")
    ctx = chromium.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    try:
        page.goto(rich_stack)
        page.locator("#side-list li").first.wait_for()
        assert page.locator("#tasks-link").is_visible()
        page.click("#tasks-link")
        page.wait_for_selector("#view-tasks:not([hidden])")
        assert page.evaluate("document.activeElement.id") == "tasks-title"
        assert page.title().endswith("Open tasks - Voice Notes")
        assert page.locator("#tasks-link").get_attribute("aria-current") == "page"
        group = page.locator("#tasks-list .task-group", has_text="Tasks test note")
        group.wait_for()
        expect(group.locator("input")).to_have_count(2)
        expect(group.locator(".glance-note")).to_have_text("2 of 2 open")
        assert "open task" in page.locator("#tasks-summary").inner_text()

        # ticking updates the count at once, keeps the row in place and is remembered for the note
        group.locator("label", has_text="Call the bank on Monday").locator("input").check()
        expect(group.locator(".glance-note")).to_have_text("1 of 2 open")
        assert json.loads(page.evaluate(f"localStorage.getItem('vn-actions:{nid}')")) == ["Call the bank on Monday"]
        expect(group.locator("input")).to_have_count(2)

        # after a reload finished tasks are hidden until asked for
        page.reload()
        group = page.locator("#tasks-list .task-group", has_text="Tasks test note")
        group.wait_for()
        expect(group.locator("input")).to_have_count(1)
        assert "Send the invoice to Anna" in group.inner_text()
        page.check("#tasks-show-done")
        expect(group.locator("input")).to_have_count(2)
        expect(group.locator("input:checked")).to_have_count(1)
        page.uncheck("#tasks-show-done")

        # the title opens the note, where the same task is ticked
        group.locator("a.task-note").click()
        page.wait_for_selector("#view-result:not([hidden])")
        assert page.locator("#tasks-link").get_attribute("aria-current") is None
        assert page.locator("#actions-list input").first.is_checked()
        assert not page.locator("#actions-list input").nth(1).is_checked()
        page.go_back()
        page.wait_for_selector("#view-tasks:not([hidden])")

        # tick the last one: nothing is left for this note
        page.locator("#tasks-list .task-group", has_text="Tasks test note").locator("input").first.check()
        expect(page.locator("#tasks-list .task-group", has_text="Tasks test note").locator(".glance-note")).to_have_text("0 of 2 open")
        assert problems == [], problems
    finally:
        httpx.delete(f"{rich_stack}/api/notes/{nid}")
        ctx.close()


def test_open_tasks_on_a_phone(chromium, rich_stack, sample):
    nid = note_with_tasks(rich_stack, sample, "Tasks phone note")
    ctx = chromium.new_context(viewport={"width": 390, "height": 800}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    try:
        page.goto(rich_stack)
        assert page.locator("#tasks-link-start").is_visible() and not page.locator("#tasks-link").is_visible()
        assert page.eval_on_selector("#tasks-link-start", "e => e.getBoundingClientRect().height") >= 44
        page.click("#tasks-link-start")
        page.wait_for_selector("#view-tasks:not([hidden])")
        group = page.locator("#tasks-list .task-group", has_text="Tasks phone note")
        group.wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.eval_on_selector("#tasks-list .todo label", "e => e.getBoundingClientRect().height") >= 44
        page.click("#tasks-back")
        page.wait_for_selector("#view-start:not([hidden])")
    finally:
        httpx.delete(f"{rich_stack}/api/notes/{nid}")
        ctx.close()
