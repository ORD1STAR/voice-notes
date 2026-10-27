"""Export notes as Markdown, plain text or PDF."""
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from . import languages, visuals
from .structure import ensure_key_points, paragraphs

FONT_DIRS = [
    Path(__file__).resolve().parent / "fonts",
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
    Path("/Library/Fonts"),
]

FORMATS = {
    "md": ("text/markdown; charset=utf-8", "md"),
    "txt": ("text/plain; charset=utf-8", "txt"),
    "pdf": ("application/pdf", "pdf"),
}


# Scripts that the bundled PDF fonts cannot draw correctly (no shaping, no right to left layout, no CJK glyphs).
_UNDRAWABLE = (
    ("Arabic", re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")),
    ("Hebrew", re.compile(r"[\u0590-\u05ff\ufb1d-\ufb4f]")),
    ("Chinese, Japanese or Korean", re.compile(r"[\u1100-\u11ff\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]")),
    ("Thai", re.compile(r"[\u0e00-\u0e7f]")),
    ("Indic", re.compile(r"[\u0900-\u0dff]")),
)


def pdf_problem(job: dict, include_transcript: bool = False) -> str | None:
    """Why a PDF of this note would be wrong, or None. Mostly Arabic or Chinese text would come out as boxes."""
    notes = job.get("notes") or {}
    parts = [notes.get("title") or "", notes.get("summary") or ""]
    parts += list(notes.get("key_points") or []) + list(notes.get("actions") or [])
    for sec in notes.get("sections") or []:
        parts.append(sec.get("heading") or "")
        parts += list(sec.get("bullets") or [])
    if include_transcript:
        parts.append((job.get("transcript") or "")[:3000])
    text = " ".join(parts)
    letters = sum(1 for c in text if c.isalpha())
    if not letters:
        return None
    for name, pattern in _UNDRAWABLE:
        if len(pattern.findall(text)) / letters > 0.15:
            return (f"The PDF cannot show {name} text yet, so it would come out as empty boxes. "
                    "Use Markdown or Text, which keep every character.")
    return None


_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(title: str, ext: str) -> str:
    base = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip().replace(" ", "-").lower()
    base = re.sub(r"-{2,}", "-", base)[:60].strip("-") or "note"
    if base in _RESERVED_NAMES:  # Windows will not save a file called "con.pdf"
        base += "-note"
    return f"{base}.{ext}"


# --------------------------------------------------------------- shared pieces

def _created(job: dict):
    try:
        return datetime.fromtimestamp(float(job.get("created", 0)))
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def _date(job: dict, lang: str = "en") -> str:
    when = _created(job)
    return languages.format_date(when, lang) if when else ""


def _language_code(job: dict) -> str | None:
    """The language of the note: the one chosen for the recording, else a guess from its words (None if unsure)."""
    return languages.notes_language(job.get("notes"), job.get("language"))


def _cover_facts(job: dict, lang: str | None, pdf: bool = False) -> dict:
    """What the cover shows besides the title: date, language, duration, source file (missing ones left out)."""
    code = lang or "en"
    name = ""
    if lang:
        # The PDF fonts cannot draw Arabic or Chinese letters, so a PDF names those languages in English.
        name = languages.LANGUAGES[lang] if pdf and lang in ("ar", "zh") else languages.NATIVE_NAMES[lang]
    return {
        "date": _date(job, code),
        "language": name,
        "duration": languages.format_duration(job.get("duration")),
        "source": job.get("filename") or "",
    }


def _transcript_paragraphs(job: dict) -> list[str]:
    return paragraphs(job.get("transcript") or "") or []


def _title(notes: dict) -> str:
    return (notes.get("title") or "").strip() or "Untitled note"


def _meta_line(facts: dict) -> str:
    return " | ".join(x for x in (facts["date"], facts["language"], facts["duration"], facts["source"]) if x)


def to_markdown(job: dict, include_transcript: bool = False) -> str:
    notes = job["notes"]
    lang = _language_code(job)
    w = lambda key: languages.text(key, lang or "en")  # noqa: E731
    lines = [f"# {_title(notes)}", ""]
    meta = _meta_line(_cover_facts(job, lang)).replace("*", r"\*")
    if meta:
        lines += [f"*{meta}*", ""]
    if notes.get("summary"):
        lines += [f"## {w('summary')}", "", notes["summary"], ""]
    if notes.get("key_points"):
        lines += [f"## {w('key_points')}", ""] + [f"- {k}" for k in notes["key_points"]] + [""]
    if notes.get("actions"):
        lines += [f"## {w('actions')}", ""] + [f"- [ ] {a}" for a in notes["actions"]] + [""]
    for sec in notes.get("sections") or []:
        lines += [f"## {sec.get('heading') or ''}", ""]
        lines += [f"- {b}" for b in sec.get("bullets") or []]
        lines.append("")
    if include_transcript and job.get("transcript"):
        lines += [f"## {w('transcript')}", ""]
        for p in _transcript_paragraphs(job):
            lines += [p, ""]
    return "\n".join(lines).rstrip() + "\n"


def to_text(job: dict, include_transcript: bool = False) -> str:
    notes = job["notes"]
    lang = _language_code(job)
    w = lambda key: languages.text(key, lang or "en").upper()  # noqa: E731
    title = _title(notes)
    lines = [title.upper(), "=" * min(len(title), 72)]
    meta = _meta_line(_cover_facts(job, lang))
    if meta:
        lines.append(meta)
    lines.append("")
    if notes.get("summary"):
        lines += [w("summary"), "", notes["summary"], ""]
    if notes.get("key_points"):
        lines += [w("key_points"), ""] + [f"- {k}" for k in notes["key_points"]] + [""]
    if notes.get("actions"):
        lines += [w("actions"), ""] + [f"[ ] {a}" for a in notes["actions"]] + [""]
    for sec in notes.get("sections") or []:
        heading = sec.get("heading") or ""
        lines += [heading, "-" * min(len(heading), 72)]
        lines += [f"- {b}" for b in sec.get("bullets") or []]
        lines.append("")
    if include_transcript and job.get("transcript"):
        lines += [w("transcript"), ""]
        for p in _transcript_paragraphs(job):
            lines += [p, ""]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------- PDF
# Colours follow the web page (src/style.css): the same blue accent, soft blue panels and warm grey rules.

INK = (21, 23, 28)
MUTED = (86, 91, 102)
ACCENT = (36, 71, 214)
ACCENT_SOFT = (228, 233, 251)
RULE = (220, 216, 203)
PAPER_TINT = (243, 241, 234)
WHITE = (255, 255, 255)
EDGE = (127, 140, 192)
LINE_STRONG = (189, 184, 168)

# role -> (fill, stroke, text color) for the diagram primitives
ROLE = {
    "center": (ACCENT, None, WHITE),
    "center-text": (None, None, WHITE),
    "branch": (ACCENT_SOFT, ACCENT, INK),
    "branch-text": (None, None, INK),
    "leaf": (WHITE, LINE_STRONG, INK),
    "leaf-text": (None, None, INK),
    "step": (PAPER_TINT, LINE_STRONG, INK),
    "step-text": (None, None, INK),
    "badge": (ACCENT, None, WHITE),
    "badge-text": (None, None, WHITE),
    "bar": (ACCENT, None, INK),
    "label": (None, None, INK),
    "value": (None, None, INK),
    "edge": (None, EDGE, None),
    "axis": (None, LINE_STRONG, None),
    "arrow": (EDGE, None, None),
}

PAGE_MARGIN = 20
BOX_INDENT = 7
MAX_VISUAL_MM = 195  # tallest drawing; a card around it still fits on one page (257 mm of text height)
CARD_PAD = 5
CARD_HEAD = 13

_ZERO_WIDTH = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff\u00ad"))
_CORE_FONT_MAP = {ord(a): b for a, b in {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u2026": "...",
    "\u2022": "-", "\u20ac": "EUR", "\u2192": "->", "\u00a0": " ",
}.items()}
_glyphs: dict[str, frozenset | None] = {}


def _find_font(name: str) -> str | None:
    for d in FONT_DIRS:
        p = d / name
        if p.exists():
            return str(p)
    return None


def _glyph_set(font_path: str) -> frozenset | None:
    """The characters a font file can draw (None when it cannot be read: nothing is filtered then)."""
    if font_path not in _glyphs:
        try:
            from fontTools.ttLib import TTFont

            _glyphs[font_path] = frozenset(TTFont(font_path, lazy=True).getBestCmap())
        except Exception:
            _glyphs[font_path] = None
    return _glyphs[font_path]


class _Pdf(FPDF):
    footer_title = ""
    page_word = "Page"
    to_latin = staticmethod(lambda s: s)

    def fit(self, text: str, width: float) -> str:
        """Shorten a one line text with "..." so that it is not wider than `width` in the current font."""
        if self.get_string_width(text) <= width:
            return text
        while text and self.get_string_width(text + "...") > width:
            text = text[:-1]
        return text.rstrip() + "..."

    def footer(self):
        self.set_y(-14)
        y = self.get_y()
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(self.l_margin, y - 1.5, self.w - self.r_margin, y - 1.5)
        self.set_font(self.sans, size=7.5)
        self.set_text_color(*MUTED)
        total = self.w - self.l_margin - self.r_margin
        right_w = 32
        self.cell(total - right_w, 5, self.fit(self.to_latin(self.footer_title), total - right_w - 4), align="L")
        self.cell(right_w, 5, f"{self.to_latin(self.page_word)} {self.page_no()} / {{nb}}", align="R")


def _setup_fonts(pdf: _Pdf) -> tuple[str, str]:
    """Register Unicode fonts when available, else fall back to the core fonts."""
    files = {n: _find_font(n) for n in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf")}
    if all(files.values()):
        pdf.add_font("Sans", "", files["DejaVuSans.ttf"])
        pdf.add_font("Sans", "B", files["DejaVuSans-Bold.ttf"])
        pdf.add_font("Serif", "", files["DejaVuSerif.ttf"])
        pdf.add_font("Serif", "B", files["DejaVuSerif-Bold.ttf"])
        pdf.uni = True
        sans_set, serif_set = _glyph_set(files["DejaVuSans.ttf"]), _glyph_set(files["DejaVuSerif.ttf"])
        pdf.glyphs = sans_set & serif_set if sans_set and serif_set else None  # drawable in both families
        return "Sans", "Serif"
    pdf.uni = False
    pdf.glyphs = None
    return "helvetica", "times"


def _draw_visual(pdf: _Pdf, vis: dict, x0: float, y0: float, scale: float, sans: str, t) -> None:
    """Draw a diagram (wide layout) with its top left corner at (x0, y0), in millimetres."""
    lay = vis["layouts"]["wide"]
    pdf.set_line_width(0.25)
    for it in lay["items"]:
        kind = it["t"]
        fill, stroke, color = ROLE.get(it["role"], (None, None, INK))
        if kind == "rect":
            x, y, w, h = x0 + it["x"] * scale, y0 + it["y"] * scale, it["w"] * scale, it["h"] * scale
            if fill:
                pdf.set_fill_color(*fill)
            if stroke:
                pdf.set_draw_color(*stroke)
            style = "DF" if fill and stroke else "F" if fill else "D"
            pdf.set_line_width(0.25)
            pdf.rect(x, y, w, h, style=style, round_corners=True, corner_radius=min(it["r"] * scale, h / 2))
        elif kind == "circle":
            r = it["r"] * scale
            pdf.set_fill_color(*fill)
            pdf.ellipse(x0 + it["x"] * scale - r, y0 + it["y"] * scale - r, 2 * r, 2 * r, style="F")
        elif kind == "line":
            pdf.set_draw_color(*(stroke or EDGE))
            pts = [(x0 + px * scale, y0 + py * scale) for px, py in it["pts"]]
            pdf.set_line_width(0.3)
            pdf.polyline(pts)
        elif kind == "arrow":
            pdf.set_fill_color(*(fill or EDGE))
            pts = [(x0 + px * scale, y0 + py * scale) for px, py in it["pts"]]
            pdf.polyline(pts, polygon=True, style="F")
        elif kind == "text":
            pdf.set_font(sans, "B" if it["bold"] else "", it["size"] * scale * 72 / 25.4)
            pdf.set_text_color(*(color or INK))
            txt = t(it["text"])
            x = x0 + it["x"] * scale
            if it["anchor"] == "middle":
                x -= pdf.get_string_width(txt) / 2
            pdf.text(x, y0 + it["y"] * scale, txt)


def _visual_size(vis: dict, width: float) -> tuple[float, float, float]:
    """(scale in mm per drawing unit, drawing height, card height) for a diagram inside a card of this width."""
    lay = vis["layouts"]["wide"]
    scale = (width - 2 * CARD_PAD) / lay["w"]
    if lay["h"] * scale > MAX_VISUAL_MM:
        scale = MAX_VISUAL_MM / lay["h"]
    h = lay["h"] * scale
    return scale, h, 2 * CARD_PAD + CARD_HEAD + h


def _title_size(title: str) -> float:
    n = len(title)
    return 22 if n <= 45 else 19 if n <= 80 else 16


def to_pdf(job: dict, include_transcript: bool = False) -> bytes:
    notes = job["notes"]
    lang = _language_code(job)
    code = lang or "en"
    words = lambda key: languages.text(key, code)  # noqa: E731
    title = _title(notes)

    pdf = _Pdf(format="A4", unit="mm")
    sans, serif = _setup_fonts(pdf)
    pdf.sans = sans

    def t(text: str) -> str:
        """Text as the PDF fonts can draw it: no invisible marks, no emoji or other missing glyphs."""
        text = unicodedata.normalize("NFC", str(text or "")).translate(_ZERO_WIDTH)
        text = re.sub(r"[\t\r\n\u2028\u2029]+", " ", text)
        if pdf.uni:
            have = pdf.glyphs
            return "".join(c for c in text if c == " " or (unicodedata.category(c)[0] != "C" and (have is None or ord(c) in have)))
        return text.translate(_CORE_FONT_MAP).encode("latin-1", "replace").decode("latin-1")

    pdf.to_latin = t
    pdf.page_word = words("page")
    pdf.footer_title = title
    pdf.alias_nb_pages()
    pdf.set_margins(PAGE_MARGIN, 20, PAGE_MARGIN)
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_title(title)
    pdf.set_author("Voice Notes")
    pdf.set_creator("Voice Notes")
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    left = pdf.l_margin
    facts = _cover_facts(job, lang, pdf=True)

    # Cover block: masthead, title, then language, length and source file
    pdf.set_font(sans, "B", 8)
    pdf.set_text_color(*ACCENT)
    pdf.set_char_spacing(1.2)
    pdf.cell(width / 2, 5, t("VOICE NOTES"), align="L")
    pdf.set_char_spacing(0)
    pdf.set_font(sans, size=8)
    pdf.set_text_color(*MUTED)
    pdf.cell(width / 2, 5, t(facts["date"]), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_text_color(*INK)
    size = _title_size(title)
    pdf.set_font(serif, "B", size)
    pdf.multi_cell(width, 9.5 if size > 19 else 8.5, t(title), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.8)
    pdf.line(left, pdf.get_y(), left + 22, pdf.get_y())
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.25)
    pdf.line(left + 22, pdf.get_y(), left + width, pdf.get_y())
    pdf.ln(4)
    cols = [(words("language"), facts["language"], 38), (words("duration"), facts["duration"], 30)]
    cols = [c for c in cols if c[1]]
    if facts["source"]:
        cols.append((words("source"), facts["source"], width - sum(c[2] for c in cols)))
    if cols:
        y0, x = pdf.get_y(), left
        for label_text, value, col_w in cols:
            pdf.set_xy(x, y0)
            pdf.set_font(sans, "B", 6.5)
            pdf.set_text_color(*ACCENT)
            pdf.set_char_spacing(0.8)
            pdf.cell(col_w, 4, t(label_text.upper()))
            pdf.set_char_spacing(0)
            pdf.set_xy(x, y0 + 4)
            pdf.set_font(sans, size=9)
            pdf.set_text_color(*INK)
            pdf.cell(col_w, 5, pdf.fit(t(value), col_w - 4))
            x += col_w
        pdf.set_y(y0 + 13)
    else:
        pdf.ln(2)

    def label(text: str):
        pdf.set_font(sans, "B", 8)
        pdf.set_text_color(*ACCENT)
        pdf.set_char_spacing(1.0)
        pdf.cell(0, 5, t(text.upper()), new_x="LMARGIN", new_y="NEXT")
        pdf.set_char_spacing(0)
        pdf.ln(1.5)

    # Summary in a tinted panel
    if notes.get("summary"):
        pad = 5
        pdf.set_font(serif, size=11)
        text_h = pdf.multi_cell(width - 2 * pad - 1, 6, t(notes["summary"]), align="L", dry_run=True, output="HEIGHT")
        box_h = text_h + 2 * pad + 6
        if pdf.will_page_break(box_h):
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_fill_color(*ACCENT_SOFT)
        pdf.rect(left, y, width, box_h, style="F")
        pdf.set_fill_color(*ACCENT)
        pdf.rect(left, y, 1.2, box_h, style="F")
        pdf.set_xy(left + pad + 1, y + pad)
        pdf.set_font(sans, "B", 7.5)
        pdf.set_text_color(*ACCENT)
        pdf.set_char_spacing(1.0)
        pdf.cell(0, 4, t(words("summary").upper()), new_x="LEFT", new_y="NEXT")
        pdf.set_char_spacing(0)
        pdf.set_x(left + pad + 1)
        pdf.set_font(serif, size=11)
        pdf.set_text_color(*INK)
        pdf.multi_cell(width - 2 * pad - 1, 6, t(notes["summary"]), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(y + box_h + 9)

    # Key points: the few things a reader must not miss
    if notes.get("key_points"):
        indent_k = 6
        pdf.set_font(serif, size=11.5)
        first_h = pdf.multi_cell(width - indent_k, 6.2, t(notes["key_points"][0]), align="L", dry_run=True, output="HEIGHT")
        if pdf.will_page_break(first_h + 14):
            pdf.add_page()
        label(words("key_points"))
        for k in notes["key_points"]:
            pdf.set_font(serif, size=11.5)
            h = pdf.multi_cell(width - indent_k, 6.2, t(k), align="L", dry_run=True, output="HEIGHT")
            if pdf.will_page_break(h):
                pdf.add_page()
            y = pdf.get_y()
            pdf.set_fill_color(*ACCENT)
            pdf.rect(left + 0.4, y + 2.2, 1.9, 1.9, style="F")
            pdf.set_text_color(*INK)
            pdf.set_xy(left + indent_k, y)
            pdf.multi_cell(width - indent_k, 6.2, t(k), align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.6)
        pdf.ln(7)

    # Action items with empty check boxes
    if notes.get("actions"):
        pdf.set_font(sans, size=10)
        first_h = pdf.multi_cell(width - BOX_INDENT, 5.6, t(notes["actions"][0]), align="L", dry_run=True, output="HEIGHT")
        if pdf.will_page_break(first_h + 14):
            pdf.add_page()
        label(words("actions"))
        pdf.set_font(sans, size=10)
        for a in notes["actions"]:
            h = pdf.multi_cell(width - BOX_INDENT, 5.6, t(a), align="L", dry_run=True, output="HEIGHT")
            if pdf.will_page_break(h):
                pdf.add_page()
            y = pdf.get_y()
            pdf.set_draw_color(*ACCENT)
            pdf.set_line_width(0.35)
            pdf.rect(left + 0.2, y + 0.9, 3.4, 3.4, style="D", round_corners=True, corner_radius=0.6)
            pdf.set_text_color(*INK)
            pdf.set_xy(left + BOX_INDENT, y)
            pdf.multi_cell(width - BOX_INDENT, 5.6, t(a), align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.2)
        pdf.ln(7)

    # Diagrams, each in a card with its title and caption
    vis_list = visuals.build_visuals(notes, job.get("language"))
    if vis_list:
        # the heading never sits alone at the bottom of a page
        if pdf.will_page_break(10 + _visual_size(vis_list[0], width)[2]):
            pdf.add_page()
        label(words("glance"))
        pdf.ln(1)
        for vis in vis_list:
            scale, h, card_h = _visual_size(vis, width)
            if pdf.will_page_break(card_h + 2):
                pdf.add_page()
            y = pdf.get_y()
            inner_w = width - 2 * CARD_PAD
            pdf.set_fill_color(*WHITE)
            pdf.set_draw_color(*RULE)
            pdf.set_line_width(0.3)
            pdf.rect(left, y, width, card_h, style="DF", round_corners=True, corner_radius=2)
            pdf.set_xy(left + CARD_PAD, y + CARD_PAD)
            pdf.set_font(sans, "B", 9.5)
            pdf.set_text_color(*INK)
            pdf.cell(inner_w, 5, pdf.fit(t(vis["title"]), inner_w))
            pdf.set_xy(left + CARD_PAD, y + CARD_PAD + 5)
            pdf.set_font(sans, size=8)
            pdf.set_text_color(*MUTED)
            pdf.cell(inner_w, 4.5, pdf.fit(t(vis["caption"]), inner_w))
            lay_w = vis["layouts"]["wide"]["w"] * scale
            _draw_visual(pdf, vis, left + CARD_PAD + (inner_w - lay_w) / 2, y + CARD_PAD + CARD_HEAD, scale, sans, t)
            pdf.set_y(y + card_h + 7)
        pdf.ln(1)

    # Sections
    indent = 6
    for n, sec in enumerate(notes.get("sections") or [], 1):
        heading = sec.get("heading") or ""
        bullets = sec.get("bullets") or [""]
        pdf.set_font(serif, "B", 13)
        head_h = pdf.multi_cell(width - 12, 7, t(heading), align="L", dry_run=True, output="HEIGHT")
        pdf.set_font(sans, size=10)
        first_h = pdf.multi_cell(width - indent, 5.6, t(bullets[0]), align="L", dry_run=True, output="HEIGHT")
        if pdf.will_page_break(head_h + first_h + 6):
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_font(sans, "B", 8)
        pdf.set_text_color(*ACCENT)
        pdf.set_xy(left, y + 1.2)
        pdf.cell(12, 5, f"{n:02d}")
        pdf.set_font(serif, "B", 13)
        pdf.set_text_color(*INK)
        pdf.set_xy(left + 12, y)
        pdf.multi_cell(width - 12, 7, t(heading), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)
        pdf.set_font(sans, size=10)
        for b in sec.get("bullets") or []:
            h = pdf.multi_cell(width - indent, 5.6, t(b), align="L", dry_run=True, output="HEIGHT")
            if pdf.will_page_break(h):
                pdf.add_page()
            y = pdf.get_y()
            pdf.set_fill_color(*ACCENT)
            pdf.ellipse(left + 1.2, y + 2.0, 1.6, 1.6, style="F")
            pdf.set_text_color(*INK)
            pdf.set_xy(left + indent, y)
            pdf.multi_cell(width - indent, 5.6, t(b), align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.2)
        pdf.ln(5)

    if include_transcript and job.get("transcript"):
        pdf.add_page()
        label(words("transcript"))
        pdf.set_font(sans, size=9.5)
        pdf.set_text_color(*INK)
        for p in _transcript_paragraphs(job):
            pdf.multi_cell(width, 5.4, t(p), align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)

    return bytes(pdf.output())


def render(job: dict, fmt: str, include_transcript: bool = False) -> tuple[bytes, str]:
    """Return (content, mime type)."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format: {fmt}")
    if job.get("notes"):
        job = {**job, "notes": ensure_key_points(job["notes"])}  # older notes get key points from their bullets
    if fmt == "md":
        return to_markdown(job, include_transcript).encode("utf-8"), FORMATS[fmt][0]
    if fmt == "txt":
        return to_text(job, include_transcript).encode("utf-8"), FORMATS[fmt][0]
    return to_pdf(job, include_transcript), FORMATS[fmt][0]
