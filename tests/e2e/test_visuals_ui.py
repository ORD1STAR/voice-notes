"""Browser checks for the diagrams, the responsive layout and recording in the browser.

Diagram text is measured in the real browser: it must sit inside its box and inside the
canvas, keep readable contrast in light and dark mode, and nothing may overflow the page
at desktop, tablet or phone width. Chromium's fake microphone is used for the recording test.
"""
import os
import subprocess
import sys
import time

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

import fakes  # noqa: E402
from conftest import FFMPEG  # noqa: E402
from test_ui import ROOT, free_port, shot  # noqa: E402

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")


@pytest.fixture(scope="module")
def rich_stack(tmp_path_factory):
    data = tmp_path_factory.mktemp("rich-data")
    fake = fakes.start_fake(delay=0.2, notes=fakes.RICH_NOTES)
    port = free_port()
    env = dict(
        os.environ,
        GROQ_API_KEY="test-key-not-real",
        GROQ_API_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/audio/transcriptions",
        OLLAMA_HOST_URL=f"http://127.0.0.1:{fake.server_port}",
        OLLAMA_MODEL="qwen2.5:1.5b",
        USE_OLLAMA="1",
        USE_GROQ_CHAT="0",  # these tests cover the local model and the rules; the Groq path has its own file
        GROQ_CHAT_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/chat/completions",
        GROQ_MODELS_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/models",
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
def chromium():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        except Exception as exc:
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    path = tmp_path_factory.mktemp("audio") / "plan.m4a"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=12",
                    "-c:a", "aac", str(path)], check=True)
    return path


def open_note(chromium, base, sample, viewport, scheme="light", mobile=False):
    ctx = chromium.new_context(viewport=viewport, color_scheme=scheme, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page()
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.goto(base)
    page.set_input_files("#file", str(sample))
    page.click("#go")
    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    return ctx, page, problems


FIT_JS = """
() => {
  const out = [];
  document.querySelectorAll('.figure-body').forEach(body => {
    body.querySelectorAll('svg').forEach(svg => {
      const prev = svg.style.display;
      svg.style.display = 'block';           // look at both layouts, whichever one is showing
      const vb = svg.viewBox.baseVal;
      const rects = [...svg.querySelectorAll('rect')].map(r => ({
        x: +r.getAttribute('x'), y: +r.getAttribute('y'), w: +r.getAttribute('width'), h: +r.getAttribute('height')}));
      const label = body.parentElement.dataset.kind + '/' + svg.getAttribute('class').split(' ')[1];
      svg.querySelectorAll('text').forEach(t => {
        const b = t.getBBox();
        if (b.x < -0.5 || b.x + b.width > vb.width + 0.5) out.push(label + ' outside canvas: ' + t.textContent);
        if (b.y < -0.5 || b.y + b.height > vb.height + 0.5) out.push(label + ' outside canvas (y): ' + t.textContent);
        if (/(center|branch|leaf|step)-text/.test(t.getAttribute('class'))) {
          const box = rects.find(r => b.x >= r.x - 0.5 && b.x + b.width <= r.x + r.w + 0.5 &&
                                      b.y >= r.y - 0.5 && b.y + b.height <= r.y + r.h + 0.5);
          if (!box) out.push(label + ' text sticks out of its box: ' + t.textContent + ' ' + JSON.stringify([b.x, b.y, b.width, b.height]));
        }
      });
      svg.style.display = prev;
    });
  });
  return out;
}
"""

SVG_CONTRAST_JS = """
() => {
  function parse(c) { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(',').map(parseFloat); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; }
  function lum(c) { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); }
  function bgOf(el) { while (el) { const c = parse(getComputedStyle(el).backgroundColor);
    if (c && c.a > 0.5) return c; el = el.parentElement; } return {r: 255, g: 255, b: 255, a: 1}; }
  const bad = [];
  document.querySelectorAll('.figure-body').forEach(body => {
    body.querySelectorAll('svg').forEach(svg => {
      const prev = svg.style.display; svg.style.display = 'block';
      const shapes = [...svg.querySelectorAll('rect, circle')];   // document order = paint order
      const inside = (s, cx, cy) => s.tagName === 'circle'
        ? Math.hypot(cx - +s.getAttribute('cx'), cy - +s.getAttribute('cy')) <= +s.getAttribute('r')
        : cx >= +s.getAttribute('x') && cx <= +s.getAttribute('x') + +s.getAttribute('width') &&
          cy >= +s.getAttribute('y') && cy <= +s.getAttribute('y') + +s.getAttribute('height');
      svg.querySelectorAll('text').forEach(t => {
        const b = t.getBBox();
        const cx = b.x + b.width / 2, cy = b.y + b.height / 2;
        const box = shapes.filter(s => inside(s, cx, cy)).pop();
        const bg = box && parse(getComputedStyle(box).fill) || bgOf(svg);
        const fg = parse(getComputedStyle(t).fill);
        const l1 = lum(fg), l2 = lum(bg);
        const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        if (ratio < 4.5) bad.push(t.getAttribute('class') + ' "' + t.textContent.slice(0, 24) + '" ' + ratio.toFixed(2));
      });
      svg.style.display = prev;
    });
  });
  return bad;
}
"""

VIEWPORTS = [
    ("desktop", {"width": 1280, "height": 900}, False),
    ("tablet", {"width": 820, "height": 1000}, False),
    ("phone", {"width": 390, "height": 800}, True),
    ("small-phone", {"width": 320, "height": 640}, True),
]


@pytest.mark.parametrize("name,viewport,mobile", VIEWPORTS)
def test_diagrams_fit_and_page_does_not_overflow(chromium, rich_stack, sample, name, viewport, mobile):
    ctx, page, problems = open_note(chromium, rich_stack, sample, viewport, mobile=mobile)
    kinds = page.eval_on_selector_all(".figure", "els => els.map(e => e.dataset.kind)")
    assert kinds == ["mindmap", "flow", "chart"]
    assert page.evaluate(FIT_JS) == []

    # Nothing wider than the viewport, anywhere.
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    wide = page.evaluate("""() => {
      const w = window.innerWidth, bad = [];
      document.querySelectorAll('body *').forEach(e => {
        const r = e.getBoundingClientRect();
        if (!r.width || !r.height || e.closest('dialog:not([open])')) return;
        const cs = getComputedStyle(e);
        if (cs.position === 'fixed' || cs.visibility === 'hidden' || e.closest('.skip, .file-input')) return;
        if (r.right > w + 1 || r.left < -1) bad.push(e.tagName + '.' + e.className + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
      });
      return bad;
    }""")
    assert wide == [], wide

    # The layout that suits the available width is the one shown.
    shown = page.eval_on_selector_all(
        ".figure-body", "els => els.map(e => [...e.querySelectorAll('svg')].filter(s => getComputedStyle(s).display !== 'none').map(s => s.getAttribute('class').split(' ')[1]))")
    width = page.eval_on_selector(".figure-body", "e => e.getBoundingClientRect().width")
    expect = "vis-wide" if width > 37 * 16 else "vis-narrow"
    assert all(s == [expect] for s in shown), (width, shown)

    # Sidebar only on large screens
    assert page.locator(".rail-list").is_visible() == (viewport["width"] >= 1024)
    # Reading column stays at a comfortable measure
    assert page.eval_on_selector("#sections ul", "e => e.getBoundingClientRect().width") <= 46 * 16
    assert problems == [], problems  # no console errors, no CSP violations
    shot(page, f"rich-{name}.png")
    ctx.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_diagram_text_contrast(chromium, rich_stack, sample, scheme):
    ctx, page, problems = open_note(chromium, rich_stack, sample, {"width": 1280, "height": 900}, scheme=scheme)
    assert page.evaluate(SVG_CONTRAST_JS) == []
    shot(page, f"rich-{scheme}.png")
    ctx.close()


def test_rich_note_pdf_contains_the_diagrams(chromium, rich_stack, sample):
    ctx, page, _ = open_note(chromium, rich_stack, sample, {"width": 1280, "height": 900})
    with page.expect_download() as dl:
        page.click("#exp-pdf")
    with open(dl.value.path(), "rb") as fh:
        pdf = fh.read()
    assert pdf.startswith(b"%PDF-")
    if os.environ.get("SCREENSHOT_DIR"):
        with open(os.path.join(os.environ["SCREENSHOT_DIR"], "rich-export.pdf"), "wb") as fh:
            fh.write(pdf)
    ctx.close()


def test_recording_in_the_browser_makes_a_note(chromium, rich_stack):
    """A recording from MediaRecorder (WebM with no duration header) goes through the whole pipeline."""
    ctx = chromium.new_context(viewport={"width": 1280, "height": 900}, permissions=["microphone"])
    page = ctx.new_page()
    page.goto(rich_stack)
    assert page.locator("#rec-start").is_visible()
    page.click("#rec-start")
    page.wait_for_selector("#drop-rec:not([hidden])")
    page.locator("#rec-time", has_text="0:02").wait_for(timeout=10000)
    page.click("#rec-stop")
    page.wait_for_selector("#picked:not([hidden])")
    name = page.locator("#picked-name").inner_text()
    assert name.startswith("recording-") and name.endswith((".webm", ".m4a", ".ogg")), name
    assert page.locator("#drop-idle").is_visible()
    page.click("#go")
    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    assert page.locator("#result-title").inner_text() == "Quarterly launch plan"
    ctx.close()


def test_recording_can_be_cancelled(chromium, rich_stack):
    ctx = chromium.new_context(viewport={"width": 1280, "height": 900}, permissions=["microphone"])
    page = ctx.new_page()
    page.goto(rich_stack)
    page.click("#rec-start")
    page.wait_for_selector("#drop-rec:not([hidden])")
    page.click("#rec-cancel")
    page.wait_for_selector("#drop-idle:not([hidden])")
    assert not page.locator("#picked").is_visible()
    ctx.close()


def test_recording_denied_shows_a_clear_message(chromium, rich_stack):
    ctx = chromium.new_context()
    page = ctx.new_page()
    page.add_init_script(
        "navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('denied', 'NotAllowedError'));")
    page.goto(rich_stack)
    page.click("#rec-start")
    page.locator("#file-error").wait_for(timeout=10000)
    msg = page.locator("#file-error").inner_text()
    assert "blocked" in msg and "choose a file" in msg
    assert page.locator("#drop-idle").is_visible()
    ctx.close()


# ------------------------------------------------------------------ timeline

TIMELINE_NOTES = {
    "title": "Spring release schedule",
    "summary": "The team agrees the dates for the spring release and who sends what.",
    "sections": [
        {"heading": "Content", "bullets": [
            "Design finishes the landing page by Friday.",
            "Marketing sends the newsletter on March 15th.",
            "Legal review is due before 2 April 2027, which leaves very little room for changes."]},
        {"heading": "Release", "bullets": [
            "The release candidate is built next week.",
            "Support trains on the new screens by the end of the month."]},
        {"heading": "Team rhythm", "bullets": ["Meet every Monday morning.", "Keep the meeting short."]},
    ],
    "key_points": ["The landing page is due on Friday."],
    "actions": ["Book the review meeting"],
}


@pytest.fixture(scope="module")
def timeline_stack(tmp_path_factory):
    data = tmp_path_factory.mktemp("timeline-data")
    fake = fakes.start_fake(delay=0.2, notes=TIMELINE_NOTES)
    port = free_port()
    env = dict(
        os.environ,
        GROQ_API_KEY="test-key-not-real",
        GROQ_API_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/audio/transcriptions",
        OLLAMA_HOST_URL=f"http://127.0.0.1:{fake.server_port}",
        OLLAMA_MODEL="qwen2.5:1.5b",
        USE_OLLAMA="1",
        USE_GROQ_CHAT="0",
        GROQ_CHAT_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/chat/completions",
        GROQ_MODELS_URL=f"http://127.0.0.1:{fake.server_port}/openai/v1/models",
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


@pytest.mark.parametrize("name,viewport,mobile", [("desktop", {"width": 1280, "height": 900}, False),
                                                  ("phone", {"width": 390, "height": 800}, True)])
def test_timeline_is_drawn_and_fits(chromium, timeline_stack, sample, name, viewport, mobile):
    ctx, page, problems = open_note(chromium, timeline_stack, sample, viewport, mobile=mobile)
    kinds = page.eval_on_selector_all(".figure", "els => els.map(e => e.dataset.kind)")
    assert "timeline" in kinds, kinds
    assert page.evaluate(FIT_JS) == []
    assert page.evaluate(SVG_CONTRAST_JS) == []
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    title = page.locator('.figure[data-kind="timeline"] .figure-title').inner_text()
    assert title == "Dates and deadlines"
    assert problems == [], problems
    shot(page, f"timeline-{name}.png")
    ctx.close()
