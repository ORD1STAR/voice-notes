"""Diagrams derived from finished notes: a mind map, a process flow, a chart of key figures and a timeline of dates.

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

from . import languages
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
        return {"title": "Order of events", "generic": True, "steps": [_step_label(b) for b in cued[:MAX_STEPS]]}
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


def _fmt_value(key: str, value: float, lang: str = "en") -> str:
    num = f"{value:,.0f}" if abs(value - round(value)) < 1e-9 else f"{value:,.2f}".rstrip("0").rstrip(".")
    if key == "%":
        return f"{num}%"
    if key in _PREFIX:
        return f"{_PREFIX[key]}{num}"
    unit = languages.text("u_" + key, lang) if ("u_" + key) in languages._EN else key
    return f"{num} {unit}"


def chart_data(notes: dict, lang: str = "en") -> dict | None:
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
        "items": [{"label": label, "value": value, "text": _fmt_value(best, value, lang)} for label, value in items],
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


# ------------------------------------------------------------- timeline

MAX_TIMELINE = 8
MIN_TIMELINE = 3


def _month_table() -> dict[str, int]:
    names = (
        "january february march april may june july august september october november december",
        "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
        "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
        "januar februar märz april mai juni juli august september oktober november dezember",
        "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre",
        "janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro",
    )
    table: dict[str, int] = {}
    for line in names:
        for i, word in enumerate(line.split(), 1):
            table[word] = i
    table.update({"fevrier": 2, "aout": 8, "decembre": 12, "maerz": 3, "marco": 3, "setiembre": 9})
    return table


_MONTHS = _month_table()
_MONTH_RE = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))
# English words that are also ordinary words: only taken as a month when written with a capital letter.
_AMBIGUOUS_MONTHS = {"may", "march", "august"}

_DAY_MONTH = re.compile(
    rf"(?<![\d/.:-])(\d{{1,2}})(?:st|nd|rd|th|er|\.|º)?\s+(?:of\s+|de\s+|d['’]\s*)?({_MONTH_RE})\b"
    rf"(?:,?\s+(?:de\s+)?(\d{{4}}))?", re.IGNORECASE)
_MONTH_DAY = re.compile(
    rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?![\d:])(?:,?\s+(\d{{4}}))?", re.IGNORECASE)
_MONTH_YEAR = re.compile(rf"\b({_MONTH_RE})\s+(\d{{4}})\b", re.IGNORECASE)
_MONTH_ALONE = re.compile(
    r"\b(?:in|by|before|until|till|during|since|from|end of|early|late|mid|en|avant|d['’]ici|fin|début|debut|"
    r"hasta|antes de|a finales de|bis|im|entro|fino a|até)\s+(?:(?:de|d['’]|du)\s*)?"
    rf"({_MONTH_RE})\b", re.IGNORECASE)
_ISO_DATE = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_SLASH_DATE = re.compile(r"(?<![\d/.])(\d{1,2})([/.])(\d{1,2})\2(\d{4}|(?<=/)\d{2})(?!\d)")

_WEEKDAYS = (
    "monday tuesday wednesday thursday friday saturday sunday "
    "lundi mardi mercredi jeudi vendredi samedi dimanche "
    "lunes martes miércoles miercoles jueves viernes sábado sabado domingo "
    "montag dienstag mittwoch donnerstag freitag samstag sonntag "
    "lunedì martedì mercoledì giovedì venerdì sabato domenica"
)
_WEEKDAY = re.compile(r"\b(" + "|".join(sorted(_WEEKDAYS.split(), key=len, reverse=True)) + r")\b", re.IGNORECASE)
_FRENCH_WEEKDAYS = set("lundi mardi mercredi jeudi vendredi samedi dimanche".split())
_RECURRING = re.compile(r"(?:every|each|chaque|tous les|toutes les|cada|todos los|los|las|jeden|ogni|tutti i|alle)\s+$",
                        re.IGNORECASE)
_FRENCH_DEADLINE = re.compile(r"(?:avant|pour|d['’]ici|jusqu['’][aà]u?|ce|dès|vers)\s+(?:le\s+)?$",
                              re.IGNORECASE)

_NUMWORD = (r"(?:\d+|an?|one|two|three|four|five|six|seven|eight|ten|twelve|un|une|deux|trois|quatre|cinq|sept|huit|dix|"
            r"douze|uno|dos|tres|cuatro|cinco|seis|diez|doce|um|dois|einer|zwei|drei|vier|due)")
_RELATIVE = re.compile("|".join([
    r"\btomorrow\b",
    r"\bnext\s+(?:week|month|quarter|year)\b",
    r"\b(?:the\s+)?end\s+of\s+(?:the\s+|this\s+|next\s+)?(?:week|month|quarter|year)\b",
    rf"\bin\s+{_NUMWORD}\s+(?:days?|weeks?|months?|tagen|wochen|monaten)\b",
    r"\bdemain\b",
    r"\bapr[eè]s-demain\b",
    r"\b(?:la\s+)?semaine\s+prochaine\b",
    r"\b(?:le\s+)?mois\s+prochain\b",
    r"\b(?:le\s+)?trimestre\s+prochain\b",
    r"\bfin\s+(?:de\s+la\s+|du\s+|de\s+l['’])?(?:semaine|mois|trimestre|ann[eé]e)\b",
    rf"\bdans\s+{_NUMWORD}\s+(?:jours?|semaines?|mois|ans?)\b",
    r"(?<!la )(?<!esta )\bmañana\b",
    r"\bla\s+pr[oó]xima\s+semana\b",
    r"\bel\s+pr[oó]ximo\s+(?:mes|año)\b",
    r"\b(?:el\s+mes|la\s+semana)\s+que\s+viene\b",
    r"\b(?:a\s+)?(?:finales\s+de|fines\s+de|fin\s+de)\s+(?:mes|trimestre|año)\b",
    rf"\bdentro\s+de\s+{_NUMWORD}\s+(?:d[ií]as?|semanas?|meses|años?)\b",
    r"\bnächste[nr]?\s+(?:woche|monat|jahr)\b",
    r"\bende\s+(?:des\s+|der\s+|dieses\s+)?(?:woche|monats|monat|quartals|jahres)\b",
    r"\bdomani\b",
    r"\b(?:la\s+)?settimana\s+prossima\b",
    r"\b(?:il\s+)?mese\s+prossimo\b",
    r"\bfine\s+(?:del\s+)?(?:mese|settimana|anno|trimestre)\b",
    rf"\btra\s+{_NUMWORD}\s+(?:giorni|settimane|mesi)\b",
    r"\bamanhã\b",
    r"\b(?:na\s+)?pr[oó]xima\s+semana\b",
    r"\bpr[oó]ximo\s+m[eê]s\b",
    r"\bfim\s+do\s+m[eê]s\b",
    rf"\bdaqui\s+a\s+{_NUMWORD}\s+(?:dias|semanas|meses)\b",
    r"\bQ[1-4](?:\s+\d{4})?\b",
]), re.IGNORECASE)

# Little words that introduce a date ("by Friday", "avant le 15 mars"): they leave with the date.
_LEAD_WORDS = re.compile(
    r"(?:\b(?:by|on|before|until|till|for|from|due|at|no later than|the|avant|pour|d['’]ici|jusqu['’][aà]u?|"
    r"dès|le|el|la|para|hasta|antes de|antes del|am|bis|entro|fino a|em|no|na)\s+)+$", re.IGNORECASE)


_TRAILING_VERB = re.compile(r"\s+(?:is|are|was|were|will be|due|est|sont|sera|es|son|ser\u00e1|ist|sind|\u00e8|\u00e9|s\u00e3o)$",
                            re.IGNORECASE)


def _month_ok(word: str) -> bool:
    return not (word.lower() in _AMBIGUOUS_MONTHS and word[:1].islower())


def _nice(label: str) -> str:
    label = " ".join(label.split())
    return label[:1].upper() + label[1:]


def _find_when(text: str):
    """The first date or deadline in a sentence: (label, start, end, sort key or None), or None."""
    exact = []
    for m in _DAY_MONTH.finditer(text):
        day = int(m.group(1))
        if _month_ok(m.group(2)) and 1 <= day <= 31:
            exact.append((m.start(), m.end(), _nice(m.group(0)),
                          (int(m.group(3) or 0), _MONTHS[m.group(2).lower()], day)))
    for m in _MONTH_DAY.finditer(text):
        day = int(m.group(2))
        if _month_ok(m.group(1)) and 1 <= day <= 31:
            exact.append((m.start(), m.end(), _nice(m.group(0)),
                          (int(m.group(3) or 0), _MONTHS[m.group(1).lower()], day)))
    for m in _MONTH_YEAR.finditer(text):
        if _month_ok(m.group(1)):
            exact.append((m.start(), m.end(), _nice(m.group(0)), (int(m.group(2)), _MONTHS[m.group(1).lower()], 0)))
    for m in _ISO_DATE.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            exact.append((m.start(), m.end(), m.group(0), (y, mo, d)))
    for m in _SLASH_DATE.finditer(text):
        a, b, y = int(m.group(1)), int(m.group(3)), int(m.group(4))
        y = y + 2000 if y < 100 else y
        if not (1 <= a <= 31 and 1 <= b <= 31):
            continue
        key = (y, b, a) if a > 12 >= b else (y, a, b) if b > 12 >= a else None  # 03/04/2026 is ambiguous: no order
        exact.append((m.start(), m.end(), m.group(0), key))
    # A date without a day only counts when nothing more exact was found.
    loose = []
    for m in _MONTH_ALONE.finditer(text):
        if _month_ok(m.group(1)):
            loose.append((m.start(), m.end(), _nice(m.group(1)), (0, _MONTHS[m.group(1).lower()], 0)))
    for m in _WEEKDAY.finditer(text):
        word = m.group(1).lower()
        before = text[max(0, m.start() - 20):m.start()]
        if _RECURRING.search(before):
            continue
        if word in _FRENCH_WEEKDAYS and not (_FRENCH_DEADLINE.search(before)
                                              or re.match(r"\s+prochain\b", text[m.end():], re.IGNORECASE)):
            continue  # "le lundi" means every Monday in French
        loose.append((m.start(), m.end(), _nice(m.group(1)), None))
    for m in _RELATIVE.finditer(text):
        loose.append((m.start(), m.end(), _nice(re.sub(r"^(?:the)\s+", "", m.group(0), flags=re.IGNORECASE)), None))
    pool = exact or loose
    pool = [c for c in pool if not re.match(r"['’]s\b", text[c[1]:])]  # "next week's review" is not a date
    if not pool:
        return None
    return min(pool, key=lambda c: (c[0], -(c[1] - c[0])))


def _without_when(text: str, start: int, end: int) -> str:
    """The sentence with the date and the little word before it taken out."""
    head = text[:start]
    lead = _LEAD_WORDS.search(head)
    if lead:
        head = head[:lead.start()]
    rest = f"{head.rstrip()} {text[end:].lstrip()}".strip()
    rest = re.sub(r"\s+([,.;:])", r"\1", rest)
    rest = rest.strip(" ,;:.-")
    while True:  # "Legal review is due before 2 April" would otherwise end on "is"
        trimmed = _TRAILING_VERB.sub("", rest)
        if trimmed == rest:
            break
        rest = trimmed
    return rest[:1].upper() + rest[1:]


def timeline_data(notes: dict) -> dict | None:
    """Dated items (dates, weekdays, deadlines) found in the notes, or None when there are fewer than three."""
    sources = [b for s in notes.get("sections") or [] for b in s["bullets"]] + list(notes.get("actions") or [])
    items, seen = [], set()
    for text in sources:
        found = _find_when(text)
        if not found:
            continue
        start, end, label, key = found
        what = short_phrase(_without_when(text, start, end), 11)
        if len(re.findall(r"\w", what)) < 3 or what.lower() in seen:
            continue
        seen.add(what.lower())
        items.append({"when": label, "what": what, "key": key})
        if len(items) == MAX_TIMELINE:
            break
    if len(items) < MIN_TIMELINE:
        return None
    by_date = all(i["key"] for i in items)
    if by_date:
        items.sort(key=lambda i: i["key"])
    return {"items": [{"when": i["when"], "what": i["what"]} for i in items], "by_date": by_date}


def _timeline_wide(data: dict) -> dict:
    c = Canvas(WIDE)
    pill_x, pill_w, spine, box_x = 2, 134, 152, 178
    box_w = WIDE - box_x - 2
    rows, y = [], 2.0
    for it in data["items"]:
        wl = wrap(it["when"], BRANCH_SIZE, pill_w - 20, 2, True)
        tl = wrap(it["what"], LEAF_SIZE, box_w - 20, 3)
        ph, bh = len(wl) * 17 + 16, len(tl) * 16 + 12
        rh = max(ph, bh)
        rows.append((y, rh, wl, ph, tl, bh))
        y += rh + 14
    if len(rows) > 1:
        c.line([(spine, rows[0][0] + rows[0][1] / 2), (spine, rows[-1][0] + rows[-1][1] / 2)])
    for y, rh, wl, ph, tl, bh in rows:
        cy = y + rh / 2
        c.line([(pill_x + pill_w, cy), (spine - 5, cy)])
        c.line([(spine + 5, cy), (box_x, cy)])
        c.rect(pill_x, cy - ph / 2, pill_w, ph, "branch", 6)
        c.lines(pill_x + 10, cy - ph / 2 + 8, wl, BRANCH_SIZE, "branch-text", True, 17)
        c.circle(spine, cy, 5, "badge")
        c.rect(box_x, cy - bh / 2, box_w, bh, "leaf", 5)
        c.lines(box_x + 10, cy - bh / 2 + 6, tl, LEAF_SIZE, "leaf-text", False, 16)
    return c.done()


def _timeline_narrow(data: dict) -> dict:
    c = Canvas(NARROW)
    spine, x0 = 10, 28
    w_all = NARROW - x0 - 2
    rows, y = [], 2.0
    for it in data["items"]:
        wl = wrap(it["when"], BRANCH_SIZE, w_all - 20, 1, True)
        pw = min(w_all, max(text_width(line, BRANCH_SIZE, True) for line in wl) + 20)
        tl = wrap(it["what"], LEAF_SIZE, w_all - 20, 3)
        ph, bh = 17 + 16, len(tl) * 16 + 12
        rows.append((y, wl, pw, ph, tl, bh))
        y += ph + 6 + bh + 16
    if len(rows) > 1:
        c.line([(spine, rows[0][0] + rows[0][3] / 2), (spine, rows[-1][0] + rows[-1][3] / 2)])
    for y, wl, pw, ph, tl, bh in rows:
        c.line([(spine + 5, y + ph / 2), (x0, y + ph / 2)])
        c.rect(x0, y, pw, ph, "branch", 6)
        c.lines(x0 + 10, y + 8, wl, BRANCH_SIZE, "branch-text", True, 17)
        c.circle(spine, y + ph / 2, 5, "badge")
        c.rect(x0, y + ph + 6, w_all, bh, "leaf", 5)
        c.lines(x0 + 10, y + ph + 6 + 6, tl, LEAF_SIZE, "leaf-text", False, 16)
    return c.done()


# ---------------------------------------------------------------- entry

def build_visuals(notes: dict | None, language: str | None = "auto") -> list[dict]:
    """Return the diagrams that make sense for these notes (possibly none).

    Titles and captions are written in the language of the notes: the one chosen for the recording,
    else a guess from the words (English when unsure).
    """
    if not notes or not notes.get("sections"):
        return []
    lang = languages.notes_language(notes, language) or "en"

    def T(key: str, **kw) -> str:
        return languages.text(key, lang).format(**kw)

    out = []
    mm = mindmap_data(notes)
    if mm:
        branches = ", ".join(b["label"] for b in mm["branches"])
        out.append({
            "kind": "mindmap",
            "title": T("mm_title"),
            "caption": (T("mm_caption") if mm["total"] <= MAX_BRANCHES
                        else T("mm_caption_cut", n=MAX_BRANCHES, total=mm["total"])),
            "alt": T("mm_alt", center=mm["center"], branches=branches),
            "layouts": {"wide": _mindmap_wide(mm), "narrow": _mindmap_narrow(mm)},
        })
    fl = flow_data(notes)
    if fl:
        out.append({
            "kind": "flow",
            "title": T("flow_title"),
            "caption": T("flow_caption") if fl.get("generic") else fl["title"],
            "alt": T("flow_alt", n=len(fl["steps"]), steps="; ".join(fl["steps"])),
            "layouts": {"wide": _flow_wide(fl), "narrow": _flow_narrow(fl)},
        })
    ch = chart_data(notes, lang)
    if ch:
        key = "u_" + ch["unit"]
        unit_name = languages.text(key, lang) if key in languages._EN else ch["unit"]
        out.append({
            "kind": "chart",
            "title": T("chart_title"),
            "caption": T("chart_caption", unit=unit_name),
            "alt": T("chart_alt", unit=unit_name, items="; ".join(f"{i['label']} {i['text']}" for i in ch["items"])),
            "layouts": {"wide": _chart(ch, WIDE), "narrow": _chart(ch, NARROW)},
        })
    tl = timeline_data(notes)
    if tl:
        out.append({
            "kind": "timeline",
            "title": T("tl_title"),
            "caption": T("tl_caption_date") if tl["by_date"] else T("tl_caption"),
            "alt": T("tl_alt", n=len(tl["items"]), items="; ".join(f"{i['when']}: {i['what']}" for i in tl["items"])),
            "layouts": {"wide": _timeline_wide(tl), "narrow": _timeline_narrow(tl)},
        })
    return out
