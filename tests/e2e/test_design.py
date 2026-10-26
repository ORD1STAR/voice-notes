"""Layout and accessibility checks that do not depend on how a screenshot looks:
contrast, horizontal overflow and touch target size, in light and dark mode.
"""
import pytest

from test_ui import audio_file, browser, stack  # noqa: F401  (fixtures)

CONTRAST_JS = """
() => {
  function parse(c) {
    const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(',').map(s => parseFloat(s));
    return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
  }
  function lum(c) {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  }
  function bgOf(el) {
    while (el) {
      const c = parse(getComputedStyle(el).backgroundColor);
      if (c && c.a > 0.5) return c;
      el = el.parentElement;
    }
    return {r: 255, g: 255, b: 255, a: 1};
  }
  const bad = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (seen.has(el)) continue; seen.add(el);
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const fg = parse(cs.color), bg = bgOf(el);
    const l1 = lum(fg), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize);
    const need = size >= 24 ? 3 : 4.5;
    if (ratio < need) bad.push(el.tagName + '.' + el.className + ' "' + n.textContent.trim().slice(0, 30) + '" ' + ratio.toFixed(2));
  }
  return bad;
}
"""

SMALL_TARGETS_JS = """
() => {
  const bad = [];
  document.querySelectorAll('button, a.btn, .tabs button, summary, label.check, input[type=checkbox]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const box = el.tagName === 'INPUT' ? el.closest('label').getBoundingClientRect() : r;
    if (box.height < 40 || box.width < 40) bad.push(el.id || el.className || el.tagName);
  });
  return bad;
}
"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_contrast_in_both_color_schemes(browser, stack, audio_file, scheme):  # noqa: F811
    ctx = browser.new_context(color_scheme=scheme, viewport={"width": 1000, "height": 900})
    page = ctx.new_page()
    page.goto(stack)
    assert page.evaluate(CONTRAST_JS) == [], "start screen"
    page.set_input_files("#file", str(audio_file))
    page.click("#go")
    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    assert page.evaluate(CONTRAST_JS) == [], "result screen"
    page.click("#tab-transcript")
    assert page.evaluate(CONTRAST_JS) == [], "transcript tab"
    ctx.close()


def test_phone_has_no_overflow_and_large_touch_targets(browser, stack, audio_file):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 360, "height": 740}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.goto(stack)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.set_input_files("#file", str(audio_file))
    assert page.evaluate(SMALL_TARGETS_JS) == []
    page.click("#go")
    page.wait_for_selector("#view-result:not([hidden])", timeout=40000)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate(SMALL_TARGETS_JS) == []
    ctx.close()
