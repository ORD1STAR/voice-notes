"""Export notes as Markdown, plain text or PDF."""
import re
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from . import visuals
from .structure import paragraphs

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


def safe_filename(title: str, ext: str) -> str:
    base = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip().replace(" ", "-").lower()
    base = re.sub(r"-{2,}", "-", base)[:60].strip("-") or "note"
    return f"{base}.{ext}"


def _date(job: dict) -> str:
    try:
        return datetime.fromtimestamp(float(job.get("created", 0))).strftime("%d %B %Y")
    except (ValueError, OSError, OverflowError):
        return ""


def _transcript_paragraphs(job: dict) -> list[str]:
    return paragraphs(job.get("transcript") or "") or []


def to_markdown(job: dict, include_transcript: bool = False) -> str:
    notes = job["notes"]
    lines = [f"# {notes['title']}", ""]
    meta = " | ".join(x for x in (_date(job), job.get("filename", "")) if x)
    if meta:
        lines += [f"*{meta}*", ""]
    if notes.get("summary"):
        lines += ["## Summary", "", notes["summary"], ""]
    if notes.get("actions"):
        lines += ["## Action items", ""] + [f"- [ ] {a}" for a in notes["actions"]] + [""]
    for sec in notes["sections"]:
        lines += [f"## {sec['heading']}", ""]
        lines += [f"- {b}" for b in sec["bullets"]]
        lines.append("")
    if include_transcript and job.get("transcript"):
        lines += ["## Transcript", ""]
        for p in _transcript_paragraphs(job):
            lines += [p, ""]
    return "\n".join(lines).rstrip() + "\n"


def to_text(job: dict, include_transcript: bool = False) -> str:
    notes = job["notes"]
    lines = [notes["title"].upper(), "=" * min(len(notes["title"]), 72)]
    meta = " | ".join(x for x in (_date(job), job.get("filename", "")) if x)
    if meta:
        lines.append(meta)
    lines.append("")
    if notes.get("summary"):
        lines += ["SUMMARY", "", notes["summary"], ""]
    if notes.get("actions"):
        lines += ["ACTION ITEMS", ""] + [f"[ ] {a}" for a in notes["actions"]] + [""]
    for sec in notes["sections"]:
        lines += [sec["heading"], "-" * min(len(sec["heading"]), 72)]
        lines += [f"- {b}" for b in sec["bullets"]]
        lines.append("")
    if include_transcript and job.get("transcript"):
        lines += ["TRANSCRIPT", ""]
        for p in _transcript_paragraphs(job):
            lines += [p, ""]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------- PDF

INK = (24, 26, 32)
MUTED = (98, 104, 116)
ACCENT = (36, 71, 214)
ACCENT_SOFT = (232, 237, 252)
RULE = (214, 218, 226)
PAPER_TINT = (247, 248, 252)
WHITE = (255, 255, 255)
EDGE = (140, 152, 190)

# role -> (fill, stroke, text color) for the diagram primitives
ROLE = {
    "center": (ACCENT, None, WHITE),
    "center-text": (None, None, WHITE),
    "branch": (ACCENT_SOFT, ACCENT, INK),
    "branch-text": (None, None, INK),
    "leaf": (WHITE, RULE, INK),
    "leaf-text": (None, None, INK),
    "step": (PAPER_TINT, RULE, INK),
    "step-text": (None, None, INK),
    "badge": (ACCENT, None, WHITE),
    "badge-text": (None, None, WHITE),
    "bar": (ACCENT, None, INK),
    "label": (None, None, INK),
    "value": (None, None, INK),
    "edge": (None, EDGE, None),
    "axis": (None, RULE, None),
    "arrow": (EDGE, None, None),
}

PAGE_MARGIN = 20
BOX_INDENT = 7
MAX_VISUAL_MM = 175


def _find_font(name: str) -> str | None:
    for d in FONT_DIRS:
        p = d / name
        if p.exists():
            return str(p)
    return None


class _Pdf(FPDF):
    footer_title = ""
    to_latin = staticmethod(lambda s: s)

    def footer(self):
        self.set_y(-13)
        self.set_font(self.sans, size=8)
        self.set_text_color(*MUTED)
        half = (self.w - self.l_margin - self.r_margin) / 2
        self.cell(half, 6, self.to_latin(self.footer_title), align="L")
        self.cell(half, 6, f"{self.page_no()} / {{nb}}", align="R")


def _setup_fonts(pdf: _Pdf) -> tuple[str, str]:
    """Register Unicode fonts when available, else fall back to the core fonts."""
    files = {n: _find_font(n) for n in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf")}
    if all(files.values()):
        pdf.add_font("Sans", "", files["DejaVuSans.ttf"])
        pdf.add_font("Sans", "B", files["DejaVuSans-Bold.ttf"])
        pdf.add_font("Serif", "", files["DejaVuSerif.ttf"])
        pdf.add_font("Serif", "B", files["DejaVuSerif-Bold.ttf"])
        pdf.uni = True
        return "Sans", "Serif"
    pdf.uni = False
    return "helvetica", "times"


def _draw_visual(pdf: _Pdf, vis: dict, x0: float, y0: float, width: float, scale: float, sans: str, t) -> None:
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


def to_pdf(job: dict, include_transcript: bool = False) -> bytes:
    notes = job["notes"]
    pdf = _Pdf(format="A4", unit="mm")
    sans, serif = _setup_fonts(pdf)
    pdf.sans = sans

    def t(text: str) -> str:
        if pdf.uni:
            return text
        return text.encode("latin-1", "replace").decode("latin-1")

    pdf.to_latin = t
    pdf.footer_title = notes["title"] if len(notes["title"]) <= 70 else notes["title"][:67].rstrip() + "..."
    pdf.alias_nb_pages()
    pdf.set_margins(PAGE_MARGIN, 20, PAGE_MARGIN)
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_title(notes["title"])
    pdf.set_author("Voice Notes")
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    left = pdf.l_margin

    # Masthead
    pdf.set_font(sans, "B", 8)
    pdf.set_text_color(*ACCENT)
    pdf.set_char_spacing(1.2)
    pdf.cell(width / 2, 5, t("VOICE NOTES"), align="L")
    pdf.set_char_spacing(0)
    pdf.set_font(sans, size=8)
    pdf.set_text_color(*MUTED)
    pdf.cell(width / 2, 5, t(_date(job)), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_text_color(*INK)
    pdf.set_font(serif, "B", 22)
    pdf.multi_cell(width, 9.5, t(notes["title"]), align="L", new_x="LMARGIN", new_y="NEXT")
    if job.get("filename"):
        pdf.ln(1)
        pdf.set_font(sans, size=8.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(width, 5, t(job["filename"]), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.8)
    pdf.line(left, pdf.get_y(), left + 22, pdf.get_y())
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.25)
    pdf.line(left + 22, pdf.get_y(), left + width, pdf.get_y())
    pdf.ln(8)

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
        pdf.cell(0, 4, t("SUMMARY"), new_x="LEFT", new_y="NEXT")
        pdf.set_char_spacing(0)
        pdf.set_x(left + pad + 1)
        pdf.set_font(serif, size=11)
        pdf.set_text_color(*INK)
        pdf.multi_cell(width - 2 * pad - 1, 6, t(notes["summary"]), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(y + box_h + 9)

    # Action items with empty check boxes
    if notes.get("actions"):
        if pdf.will_page_break(30):
            pdf.add_page()
        label("Action items")
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

    # Diagrams
    vis_list = visuals.build_visuals(notes)
    if vis_list:
        label("At a glance")
        pdf.ln(1)
        for vis in vis_list:
            lay = vis["layouts"]["wide"]
            scale = width / lay["w"]
            if lay["h"] * scale > MAX_VISUAL_MM:
                scale = MAX_VISUAL_MM / lay["h"]
            draw_w = lay["w"] * scale
            h = lay["h"] * scale
            head_h = 11
            if pdf.will_page_break(head_h + h + 4):
                pdf.add_page()
            pdf.set_font(sans, "B", 9.5)
            pdf.set_text_color(*INK)
            pdf.cell(0, 5, t(vis["title"]), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(sans, size=8)
            pdf.set_text_color(*MUTED)
            pdf.cell(0, 4.5, t(vis["caption"]), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
            y = pdf.get_y()
            _draw_visual(pdf, vis, left + (width - draw_w) / 2, y, width, scale, sans, t)
            pdf.set_y(y + h + 9)
        pdf.ln(1)

    # Sections
    indent = 6
    for n, sec in enumerate(notes["sections"], 1):
        pdf.set_font(serif, "B", 13)
        head_h = pdf.multi_cell(width - 12, 7, t(sec["heading"]), align="L", dry_run=True, output="HEIGHT")
        pdf.set_font(sans, size=10)
        first_h = pdf.multi_cell(width - indent, 5.6, t(sec["bullets"][0]), align="L", dry_run=True, output="HEIGHT")
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
        pdf.multi_cell(width - 12, 7, t(sec["heading"]), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)
        pdf.set_font(sans, size=10)
        for b in sec["bullets"]:
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
        label("Transcript")
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
    if fmt == "md":
        return to_markdown(job, include_transcript).encode("utf-8"), FORMATS[fmt][0]
    if fmt == "txt":
        return to_text(job, include_transcript).encode("utf-8"), FORMATS[fmt][0]
    return to_pdf(job, include_transcript), FORMATS[fmt][0]
