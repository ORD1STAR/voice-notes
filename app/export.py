"""Export notes as Markdown, plain text or PDF."""
import re
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

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


def to_markdown(job: dict, include_transcript: bool = False) -> str:
    notes = job["notes"]
    lines = [f"# {notes['title']}", ""]
    meta = " | ".join(x for x in (_date(job), job.get("filename", "")) if x)
    if meta:
        lines += [f"*{meta}*", ""]
    if notes.get("summary"):
        lines += ["## Summary", "", notes["summary"], ""]
    for sec in notes["sections"]:
        lines += [f"## {sec['heading']}", ""]
        lines += [f"- {b}" for b in sec["bullets"]]
        lines.append("")
    if include_transcript and job.get("transcript"):
        lines += ["## Transcript", "", job["transcript"], ""]
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
    for sec in notes["sections"]:
        lines += [sec["heading"], "-" * min(len(sec["heading"]), 72)]
        lines += [f"- {b}" for b in sec["bullets"]]
        lines.append("")
    if include_transcript and job.get("transcript"):
        lines += ["TRANSCRIPT", "", job["transcript"], ""]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------- PDF

INK = (29, 31, 30)
MUTED = (110, 112, 108)
ACCENT = (47, 93, 80)
RULE = (205, 200, 190)


def _find_font(name: str) -> str | None:
    for d in FONT_DIRS:
        p = d / name
        if p.exists():
            return str(p)
    return None


class _Pdf(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font(self.sans, size=8)
        self.set_text_color(*MUTED)
        self.cell(0, 6, str(self.page_no()), align="C")


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


def to_pdf(job: dict, include_transcript: bool = False) -> bytes:
    notes = job["notes"]
    pdf = _Pdf(format="A4", unit="mm")
    sans, serif = _setup_fonts(pdf)
    pdf.sans = sans

    def t(text: str) -> str:
        if pdf.uni:
            return text
        return text.encode("latin-1", "replace").decode("latin-1")

    pdf.set_margins(22, 22, 22)
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_title(notes["title"])
    pdf.set_author("Voice Notes")
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    pdf.set_text_color(*INK)
    pdf.set_font(serif, "B", 21)
    pdf.multi_cell(width, 9, t(notes["title"]), new_x="LMARGIN", new_y="NEXT")
    meta = "  |  ".join(x for x in (_date(job), job.get("filename", "")) if x)
    if meta:
        pdf.ln(1)
        pdf.set_font(sans, size=8.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(width, 5, t(meta), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(7)

    def label(text: str):
        pdf.set_font(sans, "B", 8)
        pdf.set_text_color(*ACCENT)
        pdf.cell(0, 5, t(text.upper()), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)

    if notes.get("summary"):
        label("Summary")
        pdf.set_font(serif, size=11)
        pdf.set_text_color(*INK)
        pdf.multi_cell(width, 6, t(notes["summary"]), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(7)

    indent = 6
    for sec in notes["sections"]:
        if pdf.get_y() > pdf.h - 55:
            pdf.add_page()
        pdf.set_font(serif, "B", 13)
        pdf.set_text_color(*INK)
        pdf.multi_cell(width, 7, t(sec["heading"]), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)
        pdf.set_font(sans, size=10)
        for b in sec["bullets"]:
            y = pdf.get_y()
            pdf.set_text_color(*ACCENT)
            pdf.set_x(pdf.l_margin)
            pdf.cell(indent, 5.6, "-" if not pdf.uni else "–")
            pdf.set_text_color(*INK)
            pdf.set_xy(pdf.l_margin + indent, y)
            pdf.multi_cell(width - indent, 5.6, t(b), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.2)
        pdf.ln(5)

    if include_transcript and job.get("transcript"):
        pdf.add_page()
        label("Transcript")
        pdf.set_font(sans, size=9.5)
        pdf.set_text_color(*INK)
        pdf.multi_cell(width, 5.4, t(job["transcript"]), new_x="LMARGIN", new_y="NEXT")

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
