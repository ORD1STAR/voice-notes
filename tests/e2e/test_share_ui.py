"""Browser tests for the share link (full screen note view), the 'not opened yet' dot and background work."""
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

import fakes  # noqa: E402
import httpx  # noqa: E402
from conftest import FFMPEG  # noqa: E402
from test_design import CONTRAST_JS  # noqa: E402
from test_notes_ui import make_note, post_note, start_server, wait_idle, wait_note  # noqa: E402
from test_visuals_ui import chromium, sample  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 360, "height": 740}
BEFOREUNLOAD_JS = "() => { const e = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(e); return e.defaultPrevented; }"


@pytest.fixture(scope="module")
def share_stack(tmp_path_factory):
    fake = fakes.start_fake(delay=0.2)
    base, proc = start_server(tmp_path_factory, "share-data", fake)
    yield base
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


@pytest.fixture
def slow_stack(tmp_path_factory):
    """A fresh server where a note takes a few seconds, and nothing else is in the list."""
    fake = fakes.start_fake(delay=1.5)
    base, proc = start_server(tmp_path_factory, "slow-data", fake)
    yield base
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


@pytest.fixture(scope="module")
def shared_note(share_stack, sample):
    return make_note(share_stack, sample)


def open_page(chromium, url, viewport=DESKTOP, scheme="light", **kw):
    ctx = chromium.new_context(viewport=viewport, color_scheme=scheme,
                               permissions=["clipboard-read", "clipboard-write"], **kw)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    return ctx, page, errors


def dot_ids(page):
    return page.evaluate("() => Array.from(document.querySelectorAll('#side-list a:has(.dot)')).map(a => a.dataset.id)")


# ------------------------------------------------------------------ share view

def test_focus_view_shows_the_note_without_the_app_around_it(chromium, share_stack, shared_note):
    job = httpx.get(f"{share_stack}/api/notes/{shared_note}").json()
    ctx, page, errors = open_page(chromium, f"{share_stack}/n/{shared_note}")
    requests = []
    page.on("request", lambda r: requests.append(r.url))
    page.reload()
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator("#result-title")).to_have_text(job["notes"]["title"])
    expect(page.locator("#summary")).to_be_visible()
    assert page.locator("#sections section").count() == len(job["notes"]["sections"])
    assert page.title() == f"{job['notes']['title']} - Voice Notes"
    # no sidebar, no recorder or upload, no editing controls
    for hidden in (".rail", "#side-list", "#view-start", "#upload-form", "#rec-start", "#rename", "#edit-notes",
                   "#rebuild", "#delete", "#share", ".crumb", ".toolbar-end"):
        expect(page.locator(hidden).first).to_be_hidden()
    # the top bar: app name linking home, copy link
    expect(page.locator("#focus-bar")).to_be_visible()
    assert page.get_attribute("#focus-brand", "href") == "/"
    expect(page.locator("#share-top")).to_be_visible()
    # exports and the transcript tab stay
    for ident, fmt in (("exp-pdf", "pdf"), ("exp-md", "md"), ("exp-txt", "txt")):
        expect(page.locator(f"#{ident}")).to_be_visible()
        assert f"/api/notes/{shared_note}/export?format={fmt}" in page.get_attribute(f"#{ident}", "href")
    page.click("#tab-transcript")
    expect(page.locator("#transcript p").first).to_be_visible()
    # the list of other notes is never loaded here
    assert not [u for u in requests if u.rstrip("/").endswith("/api/notes")]
    assert errors == []
    ctx.close()


def test_focus_view_follows_the_color_scheme_and_has_no_horizontal_scroll(chromium, share_stack, shared_note):
    for scheme, viewport in (("light", DESKTOP), ("dark", DESKTOP), ("light", PHONE), ("dark", PHONE)):
        ctx, page, _ = open_page(chromium, f"{share_stack}/n/{shared_note}", viewport=viewport, scheme=scheme)
        expect(page.locator("#view-result")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (scheme, viewport)
        assert page.evaluate(CONTRAST_JS) == [], (scheme, viewport)
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert (bg == "rgb(16, 18, 22)") == (scheme == "dark")
        ctx.close()


def test_focus_view_prints_without_the_app_chrome(chromium, share_stack, shared_note):
    ctx, page, _ = open_page(chromium, f"{share_stack}/n/{shared_note}")
    expect(page.locator("#view-result")).to_be_visible()
    page.emulate_media(media="print")
    for hidden in ("#focus-bar", ".toolbar", ".tabs", ".foot"):
        expect(page.locator(hidden).first).to_be_hidden()
    expect(page.locator("#result-title")).to_be_visible()
    ctx.close()


def test_unknown_and_malformed_ids_show_a_friendly_page(chromium, share_stack):
    for bad in ("0123456789ab", "not-a-note", "a%20b", "x" * 120):
        ctx, page, errors = open_page(chromium, f"{share_stack}/n/{bad}")
        expect(page.locator("#view-missing")).to_be_visible()
        expect(page.locator("#missing-title")).to_have_text("This note does not exist")
        expect(page.locator(".rail")).to_be_hidden()
        expect(page.locator("#share-top")).to_be_hidden()
        assert errors == []
        assert page.get_attribute("#missing-home", "href") == "/"
        ctx.close()
    ctx, page, _ = open_page(chromium, f"{share_stack}/n/0123456789ab")
    page.click("#missing-home")
    expect(page.locator("#view-start")).to_be_visible()
    assert page.url.rstrip("/") == share_stack
    ctx.close()


def test_copy_link_in_the_focus_view_and_in_the_normal_view(chromium, share_stack, shared_note):
    want = f"{share_stack}/n/{shared_note}"
    ctx, page, _ = open_page(chromium, want)
    page.click("#share-top")
    expect(page.locator("#copy-status")).to_have_text("Link copied.")
    expect(page.locator("#share-top")).to_have_text("Link copied")
    assert page.evaluate("navigator.clipboard.readText()") == want
    ctx.close()

    ctx, page, _ = open_page(chromium, f"{share_stack}/#/note/{shared_note}")
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator(".rail")).to_be_visible()
    expect(page.locator("#share-top")).to_be_hidden()
    page.click("#share")
    expect(page.locator("#copy-status")).to_have_text("Link copied.")
    assert page.evaluate("navigator.clipboard.readText()") == want
    ctx.close()


def test_copy_link_falls_back_to_showing_the_address(chromium, share_stack, shared_note):
    ctx, page, _ = open_page(chromium, f"{share_stack}/#/note/{shared_note}")
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: undefined});")
    page.reload()
    expect(page.locator("#view-result")).to_be_visible()
    page.evaluate("document.execCommand = () => false")
    page.click("#share")
    expect(page.locator("#copy-status")).to_contain_text(f"{share_stack}/n/{shared_note}")
    ctx.close()


def test_normal_deep_links_keep_working(chromium, share_stack, shared_note):
    ctx, page, errors = open_page(chromium, f"{share_stack}/#/note/{shared_note}")
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator("#edit-notes")).to_be_visible()
    expect(page.locator("#rename")).to_be_visible()
    page.click("#brand")
    expect(page.locator("#view-start")).to_be_visible()
    assert errors == []
    ctx.close()


def test_focus_view_of_a_note_in_progress_swaps_to_the_note_when_done(chromium, slow_stack, sample):
    note_id = post_note(slow_stack, sample)
    ctx, page, errors = open_page(chromium, f"{slow_stack}/n/{note_id}")
    expect(page.locator("#view-progress")).to_be_visible()
    expect(page.locator(".rail")).to_be_hidden()
    expect(page.locator("#leave-text")).to_contain_text("updates by itself")
    expect(page.locator("#view-result")).to_be_visible(timeout=60000)
    expect(page.locator("#result-title")).not_to_be_empty()
    assert errors == []
    # the reader watched it finish: no dot for this note
    wait_idle(slow_stack)
    row = next(r for r in httpx.get(slow_stack + "/api/notes").json() if r["id"] == note_id)
    assert row["unseen"] is False
    ctx.close()


# ------------------------------------------------------------------ the dot

def test_unseen_dot_shows_for_a_finished_note_and_goes_when_it_is_opened(chromium, slow_stack, sample):
    note_id = make_note(slow_stack, sample)  # finished by the API: nobody was looking
    assert httpx.get(slow_stack + "/api/notes").json()[0]["unseen"] is True
    ctx, page, errors = open_page(chromium, slow_stack + "/")
    dot = page.locator(f'#side-list a[data-id="{note_id}"] .dot')
    expect(dot).to_have_count(1)
    assert dot.get_attribute("aria-label") == "New, not opened yet"
    assert dot.get_attribute("role") == "img"
    assert dot.get_attribute("title") == "New, not opened yet"
    expect(dot).to_be_visible()
    assert page.title() == "(1) Voice Notes"
    page.click(f'#side-list a[data-id="{note_id}"]')
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator("#side-list .dot")).to_have_count(0)
    assert page.title().startswith("Website launch planning") or " - Voice Notes" in page.title()
    assert not page.title().startswith("(")
    page.reload()
    expect(page.locator("#side-list a.row")).to_have_count(1)
    expect(page.locator("#side-list .dot")).to_have_count(0)
    assert httpx.get(slow_stack + "/api/notes").json()[0]["unseen"] is False
    assert errors == []
    ctx.close()


def test_opening_a_note_in_the_focus_view_clears_its_dot(chromium, slow_stack, sample):
    note_id = make_note(slow_stack, sample)
    ctx, page, _ = open_page(chromium, f"{slow_stack}/n/{note_id}")
    expect(page.locator("#view-result")).to_be_visible()
    page.wait_for_timeout(600)
    assert httpx.get(slow_stack + "/api/notes").json()[0]["unseen"] is False
    ctx.close()


def test_no_dot_for_a_note_you_watched_being_made(chromium, slow_stack, sample):
    ctx, page, errors = open_page(chromium, slow_stack + "/")
    page.set_input_files("#file", str(sample))
    page.click("#go")
    expect(page.locator("#view-progress")).to_be_visible()
    expect(page.locator("#view-result")).to_be_visible(timeout=60000)
    page.wait_for_timeout(800)
    expect(page.locator("#side-list a.row")).to_have_count(1)
    expect(page.locator("#side-list .dot")).to_have_count(0)
    assert httpx.get(slow_stack + "/api/notes").json()[0]["unseen"] is False
    assert errors == []
    ctx.close()


def test_a_note_that_finishes_while_you_read_another_gets_its_dot(chromium, slow_stack, sample):
    first = make_note(slow_stack, sample)
    second = post_note(slow_stack, sample)
    ctx, page, _ = open_page(chromium, f"{slow_stack}/#/note/{first}")
    expect(page.locator("#view-result")).to_be_visible()
    expect(page.locator(f'#side-list a[data-id="{second}"] .row-meta')).to_contain_text("Working")
    # the list refreshes on its own while a note is being made
    expect(page.locator(f'#side-list a[data-id="{second}"] .dot')).to_have_count(1, timeout=40000)
    assert dot_ids(page) == [second]
    assert page.title().startswith("(1) ")
    ctx.close()


def test_returning_to_the_tab_refreshes_the_list(chromium, slow_stack, sample):
    ctx, page, _ = open_page(chromium, slow_stack + "/")
    expect(page.locator("#side-empty")).to_be_visible()
    note_id = make_note(slow_stack, sample)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator(f'#side-list a[data-id="{note_id}"] .dot')).to_have_count(1)
    ctx.close()


def test_closing_the_tab_after_the_upload_does_not_stop_the_note(chromium, slow_stack, sample):
    ctx, page, _ = open_page(chromium, slow_stack + "/")
    page.set_input_files("#file", str(sample))
    page.click("#go")
    expect(page.locator("#leave-text")).to_contain_text("close this tab")  # shown once the server has the file
    note_id = httpx.get(slow_stack + "/api/notes").json()[0]["id"]
    ctx.close()  # the tab is gone
    job = wait_note(slow_stack, note_id)
    assert job["status"] == "done"
    ctx, page, _ = open_page(chromium, slow_stack + "/")
    expect(page.locator(f'#side-list a[data-id="{note_id}"] .dot')).to_have_count(1)
    ctx.close()


def test_reopening_the_app_resumes_the_progress_of_a_running_note(chromium, slow_stack, sample):
    note_id = post_note(slow_stack, sample)
    ctx, page, _ = open_page(chromium, f"{slow_stack}/#/note/{note_id}")
    expect(page.locator("#view-progress")).to_be_visible()
    expect(page.locator("#view-start")).to_be_hidden()
    expect(page.locator("#progress-detail")).not_to_have_text("Starting")
    expect(page.locator("#leave-text")).to_contain_text("close this tab")
    expect(page.locator("#view-result")).to_be_visible(timeout=60000)
    ctx.close()


# ------------------------------------------------------------- leaving the page

def test_leaving_is_only_guarded_while_the_file_is_being_sent(chromium, share_stack, sample):
    ctx, page, _ = open_page(chromium, share_stack + "/")
    held = []
    page.route("**/api/notes", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    assert page.evaluate(BEFOREUNLOAD_JS) is False  # nothing is being sent
    page.set_input_files("#file", str(sample))
    page.click("#go")
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(100)
    assert held, "the upload never started"
    expect(page.locator("#view-progress")).to_be_visible()
    expect(page.locator("#leave-text")).to_contain_text("Keep this tab open")
    assert page.evaluate(BEFOREUNLOAD_JS) is True
    held[0].continue_()
    expect(page.locator("#view-result")).to_be_visible(timeout=60000)
    assert page.evaluate(BEFOREUNLOAD_JS) is False
    ctx.close()


def test_enter_cannot_start_a_second_upload(chromium, share_stack, sample):
    ctx, page, _ = open_page(chromium, share_stack + "/")
    before = len(httpx.get(share_stack + "/api/notes").json())
    held = []
    page.route("**/api/notes", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    page.set_input_files("#file", str(sample))
    page.click("#go")
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(100)
    page.evaluate("document.getElementById('upload-form').requestSubmit()")
    page.wait_for_timeout(300)
    assert len(held) == 1
    held[0].continue_()
    expect(page.locator("#view-result")).to_be_visible(timeout=60000)
    assert len(httpx.get(share_stack + "/api/notes").json()) == before + 1
    ctx.close()


# --------------------------------------------------------------- dot contrast

def test_the_dot_is_visible_in_both_color_schemes(chromium, slow_stack, sample):
    make_note(slow_stack, sample)
    js = """
    () => {
      function parse(c) { const p = c.match(/[\\d.]+/g).map(Number); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; }
      function lum(c) { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
        return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); }
      function bgOf(el) { while (el) { const c = parse(getComputedStyle(el).backgroundColor); if (c.a > 0.5) return c; el = el.parentElement; } return {r:255,g:255,b:255}; }
      const dot = document.querySelector('#side-list .dot');
      const cs = getComputedStyle(dot);
      const bg = bgOf(dot.parentElement);
      const fill = parse(cs.backgroundColor), edge = parse(cs.borderTopColor);
      const ratio = (a, b) => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05);
      return {fill: ratio(fill, bg), edge: ratio(edge, bg), width: dot.getBoundingClientRect().width, blue: fill.b > fill.r};
    }
    """
    for scheme in ("light", "dark"):
        ctx, page, _ = open_page(chromium, slow_stack + "/", scheme=scheme)
        expect(page.locator("#side-list .dot")).to_have_count(1)
        got = page.evaluate(js)
        assert got["blue"] and got["width"] >= 8, (scheme, got)
        assert max(got["fill"], got["edge"]) >= 3.0, (scheme, got)
        ctx.close()
