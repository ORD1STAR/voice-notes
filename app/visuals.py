"""Diagrams derived from finished notes: a mind map, a process flow and a chart of key figures.

Everything is computed from the notes text itself, so nothing is invented and old notes
get diagrams too. Each diagram is returned as a small drawing (rectangles, text lines,
polylines) in two layouts, "wide" for large screens and the PDF, and "narrow" for phones.
The web page turns the drawing into SVG and the PDF export draws the same primitives.

A drawing looks like:
    {"w": 640, "h": 300, "items": [{"t": "rect", "x": 0, "y": 0, "w": 10, "h": 10, "r": 4, "role": "center"}, ...]}
Item types: rect, text (one line, baseline at y), line (polyline), arrow (filled triangle), circle.
"""
import re
from pathlib import Path

from .structure import is_context_dependent, short_phrase

WIDE = 640
NARROW = 340

FONT_DIR = Path(__file__).resolve().parent / "fonts"
_fonts: dict = {}


# ------------------------------------------------------------- measuring

def _font(bold: bool, size: float):
    key = (bold, round(size * 4))
    if key not in _fonts:
        font = None
        try:
            from PIL import ImageFont

            name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            font = ImageFont.truetype(str(FONT_DIR / name), max(int(size * 4), 4))
        except Exception:
            font = None
        _fonts[key] = font
    return _fonts[key]


def text_width(text: str, size: float, bold: bool = False) -> float:
    """Width of a text line in drawing units, measured with the bundled DejaVu Sans."""
    font = _font(bold, size)
    if font is None:
        return len(text) * size * (0.64 if bold else 0.6)
    return font.getlength(text) / 4.0


def wrap(text: str, size: float, max_w: float, max_lines: int, bold: bool = False) -> list[str]:
    """Break text into lines that fit max_w. The last line ends with "..." when text is cut."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    i = 0
    while i < len(words):
        w = words[i]
        trial = f"{cur} {w}".strip()
        if text_width(trial, size, bold) <= max_w or not cur:
            if not cur and text_width(w, size, bold) > max_w:
                # A single very long word: cut it.
                k = len(w)
                while k > 1 and text_width(w[:k] + "...", size, bold) > max_w:
                    k -= 1
                lines.append(w[:k] + "...")
                i += 1
                continue
            cur = trial
            i += 1
        else:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and text_width(last + "...", size, bold) > max_w:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = last.rstrip(",;:") + "..."
    return lines or [""]


# --------------------------------------------------------------- drawing

class Canvas:
    def __init__(self, w: float):
        self.w = w
        self.items: list[dict] = []
        self.bottom = 0.0

    def _grow(self, y):
        self.bottom = max(self.bottom, y)

    def rect(self, x, y, w, h, role, r=6):
        self.items.append({"t": "rect", "x": round(x, 1), "y": round(y, 1), "w": round(w, 1), "h": round(h, 1),
                           "r": r, "role": role})
        self._grow(y + h)

    def circle(self, cx, cy, r, role):
        self.items.append({"t": "circle", "x": round(cx, 1), "y": round(cy, 1), "r": r, "role": role})
        self._grow(cy + r)

    def text(self, x, y, text, size, role, bold=False, anchor="start"):
        self.items.append({"t": "text", "x": round(x, 1), "y": round(y, 1), "text": text, "size": size,
                           "bold": bold, "anchor": anchor, "role": role})
        self._grow(y + size * 0.3)

    def lines(self, x, top, lines, size, role, bold=False, leading=None, anchor="start"):
        """Draw several lines starting with the top of the first line at `top`."""
        leading = leading or size * 1.3
        for i, ln in enumerate(lines):
            self.text(x, top + size * 0.95 + i * leading, ln, size, role, bold, anchor)

    def line(self, pts, role="edge"):
        self.items.append({"t": "line", "pts": [[round(x, 1), round(y, 1)] for x, y in pts], "role": role})
        for _, y in pts:
            self._grow(y)

    def curve(self, x1, y1, x2, y2, role="edge"):
        """A smooth horizontal S-curve from (x1, y1) to (x2, y2) as a polyline."""
        n = 22
        pts = []
        for i in range(n + 1):
            t = i / n
            s = t * t * (3 - 2 * t)  # smoothstep
            pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * s))
        self.line(pts, role)

    def arrow(self, x, y, direction, role="arrow", size=6):
        """Filled triangle whose tip is at (x, y) and that points in `direction`."""
        if direction == "right":
            pts = [(x, y), (x - size * 1.4, y - size * 0.8), (x - size * 1.4, y + size * 0.8)]
        elif direction == "down":
            pts = [(x, y), (x - size * 0.8, y - size * 1.4), (x + size * 0.8, y - size * 1.4)]
        else:
            raise ValueError(direction)
        self.items.append({"t": "arrow", "pts": [[round(a, 1), round(b, 1)] for a, b in pts], "role": role})
        self._grow(y)

    def done(self, pad=2):
        return {"w": self.w, "h": round(self.bottom + pad, 1), "items": self.items}


# ------------------------------------------------------------ mind map

LEAF_SIZE = 12
BRANCH_SIZE = 13
CENTER_SIZE = 15
MAX_BRANCHES = 8
MAX_LEAVES = 3


def mindmap_data(notes: dict) -> dict | None:
    sections = notes.get("sections") or []
    if len(sections) < 3:
        return None
    branches = []
    per_branch = MAX_LEAVES if len(sections) <= 5 else MAX_LEAVES - 1
    for sec in sections[:MAX_BRANCHES]:
        leaves, seen = [], set()
        # points that stand on their own come first; "That is something we should fix" only fills a free place
        ordered = [b for b in sec["bullets"] if not is_context_dependent(b)] + [
            b for b in sec["bullets"] if is_context_dependent(b)]
        for b in ordered:
            p = short_phrase(_CUE.sub("", b.strip(), count=1).strip() or b, 9)
            key = p.lower()
            if p and key not in seen:
                seen.add(key)
                leaves.append(p)
            if len(leaves) == per_branch:
                break
        branches.append({"label": sec["heading"], "leaves": leaves})
    return {"center": notes["title"], "branches": branches, "total": len(sections)}


def _mindmap_wide(data: dict) -> dict:
    c = Canvas(WIDE)
    cw, bx, bw, lx = 120, 156, 152, 338
    lw = WIDE - lx - 2
    groups = []
    for br in data["branches"]:
        bl = wrap(br["label"], BRANCH_SIZE, bw - 20, 2, True)
        bh = len(bl) * 17 + 16
        leaves = []
        for leaf in br["leaves"]:
            ll = wrap(leaf, LEAF_SIZE, lw - 20, 2)
            leaves.append((ll, len(ll) * 16 + 12))
        leaves_h = sum(h for _, h in leaves) + 8 * max(len(leaves) - 1, 0)
        groups.append((bl, bh, leaves, max(bh, leaves_h)))
    y = 2.0
    layout = []
    for bl, bh, leaves, gh in groups:
        layout.append((y, bl, bh, leaves, gh))
        y += gh + 16
    total = y - 16 + 2
    cl = wrap(data["center"], CENTER_SIZE, cw - 20, 6, True)
    ch = len(cl) * 20 + 22
    cy = total / 2
    c.rect(2, cy - ch / 2, cw, ch, "center", 8)
    c.lines(2 + cw / 2, cy - ch / 2 + 11, cl, CENTER_SIZE, "center-text", True, 20, "middle")
    for top, bl, bh, leaves, gh in layout:
        by = top + gh / 2
        c.curve(2 + cw, cy, bx, by)
        c.rect(bx, by - bh / 2, bw, bh, "branch", 6)
        c.lines(bx + 10, by - bh / 2 + 8, bl, BRANCH_SIZE, "branch-text", True, 17)
        leaves_h = sum(h for _, h in leaves) + 8 * max(len(leaves) - 1, 0)
        ly = top + (gh - leaves_h) / 2
        for ll, lh in leaves:
            c.curve(bx + bw, by, lx, ly + lh / 2)
            c.rect(lx, ly, lw, lh, "leaf", 5)
            c.lines(lx + 10, ly + 6, ll, LEAF_SIZE, "leaf-text", False, 16)
            ly += lh + 8
    return c.done()


def _mindmap_narrow(data: dict) -> dict:
    c = Canvas(NARROW)
    cl = wrap(data["center"], CENTER_SIZE, NARROW - 28, 3, True)
    ch = len(cl) * 20 + 20
    c.rect(2, 2, NARROW - 4, ch, "center", 8)
    c.lines(NARROW / 2, 12, cl, CENTER_SIZE, "center-text", True, 20, "middle")
    y = 2 + ch + 18
    trunk_x = 16
    start = 2 + ch
    last_branch_y = y
    for br in data["branches"]:
        bl = wrap(br["label"], BRANCH_SIZE, NARROW - 44 - 20, 2, True)
        bh = len(bl) * 17 + 14
        c.rect(34, y, NARROW - 36, bh, "branch", 6)
        c.lines(44, y + 7, bl, BRANCH_SIZE, "branch-text", True, 17)
        c.line([(trunk_x, y + bh / 2), (34, y + bh / 2)])
        last_branch_y = y + bh / 2
        stem_x = 54
        yy = y + bh + 6
        leaf_ys = []
        for leaf in br["leaves"]:
            ll = wrap(leaf, LEAF_SIZE, NARROW - 74 - 20, 2)
            lh = len(ll) * 16 + 10
            c.rect(70, yy, NARROW - 72, lh, "leaf", 5)
            c.lines(80, yy + 5, ll, LEAF_SIZE, "leaf-text", False, 16)
            c.line([(stem_x, yy + lh / 2), (70, yy + lh / 2)])
            leaf_ys.append(yy + lh / 2)
            yy += lh + 6
        if leaf_ys:
            c.line([(stem_x, y + bh), (stem_x, leaf_ys[-1])])
        y = yy + 10
    c.line([(trunk_x, start), (trunk_x, last_branch_y)])
    return c.done()


# ----------------------------------------------------------------- flow

_CUE = re.compile(
    r"^(?:(?:so|and|well|okay|ok|now|also)\b[\s,]*)?(?:first(?:ly)?(?!\s+time)|second(?:ly)?|third(?:ly)?|fourth|fifth|then|"
    r"next(?!\s+(?:time|week|month|year|day|days|night|morning|evening|quarter|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday))|after that|afterwards?|finally|lastly|"
    r"last(?!\s+(?:time|week|month|year|day|days|night|morning|evening|quarter|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday))|once that(?: is done)?|step\s*\d+|\d{1,2}[.)]|d'abord|ensuite|puis|enfin|apr[eè]s|"
    r"primero|luego|despu[eé]s|finalmente)\b[\s,:.)-]*",
    re.IGNORECASE,
)
_HEADING_HINT = re.compile(
    r"\b(steps?|process|procedure|workflow|roadmap|timeline|pipeline|checklist|how to|"
    r"[eé]tapes?|processus|proc[eé]dure|pasos)\b",
    re.IGNORECASE,
)
MAX_STEPS = 7


def _step_label(bullet: str) -> str:
    text = _CUE.sub("", bullet.strip(), count=1).strip() or bullet.strip()
    return short_phrase(text, 11)


def _sequence(bullets: list[str]) -> list[str]:
    """The bullets from the first to the last one that carries a sequence word."""
    idx = [i for i, b in enumerate(bullets) if _CUE.match(b.strip())]
    if not idx:
        return []
    return bullets[idx[0]: idx[-1] + 1]


def flow_data(notes: dict) -> dict | None:
    best = None
    for sec in notes.get("sections") or []:
        bullets = sec["bullets"]
        cues = sum(1 for b in bullets if _CUE.match(b.strip()))
        hint = bool(_HEADING_HINT.search(sec["heading"]))
        seq = _sequence(bullets)
        if len(seq) >= 3 and (cues >= 2 or (hint and cues >= 1)):
            score = cues * 2 + (1 if hint else 0)
            if best is None or score > best[0]:
                best = (score, sec["heading"], seq)
    if best:
        _, heading, seq = best
        steps = [_step_label(b) for b in seq[:MAX_STEPS]]
        return {"title": heading, "steps": steps}
    # No single section reads as a sequence: look across the whole note.
    cued = [b for s in notes.get("sections") or [] for b in s["bullets"] if _CUE.match(b.strip())]
    if len(cued) >= 3:
        return {"title": "Order of events", "steps": [_step_label(b) for b in cued[:MAX_STEPS]]}
    return None


def _flow_wide(data: dict) -> dict:
    steps = data["steps"]
    n = len(steps)
    cols = 2 if n == 4 else min(n, 3)
    gap = 56
    bw = (WIDE - 4 - gap * (cols - 1)) / cols
    rows = [steps[i:i + cols] for i in range(0, n, cols)]
    c = Canvas(WIDE)
    y = 2.0
    prev = None
    k = 0
    for ri, row in enumerate(rows):
        wrapped = [wrap(s, 13, bw - 56, 4) for s in row]
        bh = max(len(w) for w in wrapped) * 17 + 24
        tops = []
        for ci, ln in enumerate(wrapped):
            k += 1
            x = 2 + ci * (bw + gap)
            c.rect(x, y, bw, bh, "step", 8)
            c.circle(x + 24, y + bh / 2, 11, "badge")
            c.text(x + 24, y + bh / 2 + 4.5, str(k), 12, "badge-text", True, "middle")
            c.lines(x + 44, y + (bh - len(ln) * 17) / 2 + 1, ln, 13, "step-text", False, 17)
            tops.append(x)
            if ci > 0:
                mid = y + bh / 2
                c.line([(x - gap + 8, mid), (x - 4, mid)])
                c.arrow(x - 2, mid, "right")
        if prev is not None:
            # Elbow from the bottom of the last box of the previous row to the first box of this one.
            px, pbottom = prev
            gy = (pbottom + y) / 2
            fx = 2 + bw / 2
            c.line([(px, pbottom), (px, gy), (fx, gy), (fx, y - 4)])
            c.arrow(fx, y - 1, "down")
        prev = (2 + (len(row) - 1) * (bw + gap) + bw / 2, y + bh)
        y += bh + 44
    return c.done()


def _flow_narrow(data: dict) -> dict:
    c = Canvas(NARROW)
    y = 2.0
    for k, s in enumerate(data["steps"], 1):
        ln = wrap(s, 13, NARROW - 4 - 56, 3)
        bh = len(ln) * 17 + 22
        c.rect(2, y, NARROW - 4, bh, "step", 8)
        c.circle(26, y + bh / 2, 11, "badge")
        c.text(26, y + bh / 2 + 4.5, str(k), 12, "badge-text", True, "middle")
        c.lines(46, y + (bh - len(ln) * 17) / 2 + 1, ln, 13, "step-text", False, 17)
        if k < len(data["steps"]):
            c.line([(NARROW / 2, y + bh), (NARROW / 2, y + bh + 18)])
            c.arrow(NARROW / 2, y + bh + 22, "down")
        y += bh + 26
    return c.done()


# ---------------------------------------------------------------- chart

_NUM = r"(\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
_UNITS = [
    ("%", r"\s?(?:%|percent|per cent|pour cent|por ciento)"),
    ("EUR", r"\s?(?:€|euros?|eur)\b"),
    ("USD", r"\s?(?:\$|dollars?|usd)\b"),
    ("GBP", r"\s?(?:£|pounds?|gbp)\b"),
    ("hours", r"\s?(?:hours?|hrs?|heures?|horas?)\b"),
    ("minutes", r"\s?(?:minutes?|mins?)\b"),
    ("days", r"\s?(?:days?|jours?|d[ií]as?)\b"),
    ("weeks", r"\s?(?:weeks?|semaines?|semanas?)\b"),
    ("months", r"\s?(?:months?|mois|meses)\b"),
    ("km", r"\s?(?:km|kilomet(?:er|re)s?)\b"),
    ("kg", r"\s?(?:kg|kilos?|kilograms?)\b"),
]
_PREFIX = {"EUR": "€", "USD": "$", "GBP": "£"}
_SYMBOL_BEFORE = {"EUR": r"€\s?", "USD": r"\$\s?", "GBP": r"£\s?"}
MAX_BARS = 8


def _to_number(token: str) -> float | None:
    t = token.strip()
    if re.fullmatch(r"\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?", t):
        t = t.replace(" ", "").replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _find_figures(text: str) -> list[tuple[str, float, int, int]]:
    """Every "number + unit" in a text, in reading order: (unit key, value, start, end)."""
    found = []
    for key, unit_re in _UNITS:
        patterns = [_NUM + unit_re]
        if key in _SYMBOL_BEFORE:
            patterns.append(_SYMBOL_BEFORE[key] + _NUM)
        for pat in patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                val = _to_number(m.group(1))
                if val is not None and val > 0:
                    found.append((key, val, m.start(), m.end()))
    found.sort(key=lambda f: f[2])
    out = []
    for f in found:  # a symbol and a word for the same amount can match twice
        if not out or f[2] >= out[-1][3]:
            out.append(f)
    return out


_LEAD = re.compile(r"^(?:\s*(?:,|and|for|on|of|to|in|is|are|at|per|:)\b|\s*[,:])+\s*", re.IGNORECASE)
_TRAIL = re.compile(r"(?:\s*(?:,|and|plus|or)\b|\s*[,;])+\s*$", re.IGNORECASE)


def _figure_labels(bullet: str, figures: list) -> list[str]:
    """One label per figure. A bullet with several figures names each by the words that follow it."""
    if len(figures) == 1:
        return [short_phrase(bullet, 12) or bullet]
    labels = []
    for i, (_, _, start, end) in enumerate(figures):
        stop = figures[i + 1][2] if i + 1 < len(figures) else len(bullet)
        seg = _TRAIL.sub("", _LEAD.sub("", bullet[end:stop])).strip(" .")
        if not seg and i == 0:
            seg = bullet[:start].strip(" ,.")
        labels.append(short_phrase(seg, 8) if seg else "")
    return labels


def _fmt_value(key: str, value: float) -> str:
    num = f"{value:,.0f}" if abs(value - round(value)) < 1e-9 else f"{value:,.2f}".rstrip("0").rstrip(".")
    if key == "%":
        return f"{num}%"
    if key in _PREFIX:
        return f"{_PREFIX[key]}{num}"
    return f"{num} {key}"


def chart_data(notes: dict) -> dict | None:
    groups: dict[str, list[tuple[str, float]]] = {}
    for sec in notes.get("sections") or []:
        for b in sec["bullets"]:
            if _CUE.match(b.strip()):
                continue  # the amounts and times inside process steps belong to the process diagram
            figures = _find_figures(b)
            if not figures:
                continue
            labels = _figure_labels(b, figures)
            for (key, value, _, _), label in zip(figures, labels):
                if not label:
                    continue
                groups.setdefault(key, [])
                if all(label.lower() != existing.lower() for existing, _ in groups[key]):
                    groups[key].append((label, value))
    best = None
    for key, items in groups.items():
        if len(items) >= 3 and len({v for _, v in items}) >= 2:
            if best is None or len(items) > len(groups[best]):
                best = key
    if best is None:
        return None
    items = groups[best][:MAX_BARS]
    return {
        "unit": best,
        "items": [{"label": label, "value": value, "text": _fmt_value(best, value)} for label, value in items],
    }


def _chart(data: dict, width: int) -> dict:
    items = data["items"]
    vmax = max(i["value"] for i in items)
    vw = max(text_width(i["text"], 12, True) for i in items) + 12
    c = Canvas(width)
    wide = width == WIDE
    label_w = 236 if wide else width
    area_x = 252 if wide else 2
    bar_max = width - area_x - vw - 4
    y = 2.0
    for it in items:
        lines = wrap(it["label"], 12, label_w - (16 if wide else 4), 2)
        blen = max(3.0, it["value"] / vmax * bar_max)
        if wide:
            row_h = max(len(lines) * 16, 22) + 12
            c.lines(2, y + (row_h - 12 - len(lines) * 16) / 2 + 2, lines, 12, "label", False, 16)
            by = y + (row_h - 12 - 16) / 2 + 1
        else:
            c.lines(2, y, lines, 12, "label", False, 16)
            by = y + len(lines) * 16 + 4
            row_h = len(lines) * 16 + 4 + 16 + 12
        c.rect(area_x, by, blen, 16, "bar", 3)
        c.text(area_x + blen + 8, by + 12.5, it["text"], 12, "value", True)
        y += row_h
    if wide:
        c.line([(area_x, 0), (area_x, y - 8)], "axis")
    return c.done()


# ---------------------------------------------------------------- entry

def build_visuals(notes: dict | None) -> list[dict]:
    """Return the diagrams that make sense for these notes (possibly none)."""
    if not notes or not notes.get("sections"):
        return []
    out = []
    mm = mindmap_data(notes)
    if mm:
        out.append({
            "kind": "mindmap",
            "title": "Mind map",
            "caption": ("The sections of the note and their main points."
                        if mm["total"] <= MAX_BRANCHES
                        else f"The first {MAX_BRANCHES} of {mm['total']} sections and their main points."),
            "alt": "Mind map of " + mm["center"] + " with the branches: " + ", ".join(b["label"] for b in mm["branches"]),
            "layouts": {"wide": _mindmap_wide(mm), "narrow": _mindmap_narrow(mm)},
        })
    fl = flow_data(notes)
    if fl:
        out.append({
            "kind": "flow",
            "title": "Process",
            "caption": fl["title"] if fl["title"] != "Order of events" else "The steps in the order they were described.",
            "alt": "Process in " + str(len(fl["steps"])) + " steps: " + "; ".join(fl["steps"]),
            "layouts": {"wide": _flow_wide(fl), "narrow": _flow_narrow(fl)},
        })
    ch = chart_data(notes)
    if ch:
        unit_name = {"%": "percentages", "EUR": "amounts in euros", "USD": "amounts in dollars",
                     "GBP": "amounts in pounds"}.get(ch["unit"], ch["unit"])
        out.append({
            "kind": "chart",
            "title": "Key figures",
            "caption": f"Values mentioned in the note, as {unit_name}.",
            "alt": "Bar chart of " + unit_name + ": " + "; ".join(f"{i['label']} {i['text']}" for i in ch["items"]),
            "layouts": {"wide": _chart(ch, WIDE), "narrow": _chart(ch, NARROW)},
        })
    return out
