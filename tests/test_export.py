import time

from app import export

JOB = {
    "id": "abc123",
    "filename": "meeting.m4a",
    "created": time.time(),
    "transcript": "Full transcript text goes here.",
    "notes": {
        "title": "Launch planning",
        "summary": "The team planned the launch.",
        "sections": [
            {"heading": "Website", "bullets": ["Finish the landing page", "Review copy"]},
            {"heading": "Budget", "bullets": ["Ads budget stays the same"]},
        ],
    },
}

UNICODE_JOB = {
    **JOB,
    "notes": {
        "title": "Réunion: café, naïve, 日本語, “quotes”",
        "summary": "Résumé avec des accents et un tiret – long.",
        "sections": [{"heading": "Été", "bullets": ["Ça fonctionne très bien", "Prix: 5 €"]}],
    },
}


def test_markdown_structure():
    md = export.to_markdown(JOB)
    assert md.startswith("# Launch planning\n")
    assert "## Summary\n\nThe team planned the launch." in md
    assert "## Website\n\n- Finish the landing page\n- Review copy" in md
    assert "Transcript" not in md


def test_markdown_with_transcript():
    md = export.to_markdown(JOB, include_transcript=True)
    assert md.rstrip().endswith("Full transcript text goes here.")
    assert "## Transcript" in md


def test_text_structure():
    txt = export.to_text(JOB)
    assert txt.startswith("LAUNCH PLANNING\n===============")
    assert "SUMMARY" in txt
    assert "Website\n-------\n- Finish the landing page" in txt
    assert "#" not in txt


def test_text_with_transcript():
    assert "TRANSCRIPT\n\nFull transcript text" in export.to_text(JOB, include_transcript=True)


def test_pdf_is_a_valid_pdf():
    data = export.to_pdf(JOB)
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    assert len(data) > 1500


def test_pdf_with_unicode_and_transcript():
    data = export.to_pdf(UNICODE_JOB, include_transcript=True)
    assert data.startswith(b"%PDF-")


def test_pdf_long_content_spans_pages():
    job = {**JOB, "notes": {**JOB["notes"], "sections": [
        {"heading": f"Section {i}", "bullets": ["A fairly long bullet point " * 6] * 6} for i in range(12)
    ]}}
    data = export.to_pdf(job)
    assert data.count(b"/Type /Page\n") >= 2 or data.count(b"/Type /Page") >= 3


def test_pdf_falls_back_to_core_fonts(monkeypatch):
    monkeypatch.setattr(export, "FONT_DIRS", [])
    data = export.to_pdf(UNICODE_JOB)
    assert data.startswith(b"%PDF-")


def test_render_dispatch_and_mime():
    for fmt, mime in (("md", "text/markdown"), ("txt", "text/plain"), ("pdf", "application/pdf")):
        content, got = export.render(JOB, fmt)
        assert got.startswith(mime) and content
    try:
        export.render(JOB, "docx")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_safe_filename():
    assert export.safe_filename("Launch planning!", "pdf") == "launch-planning.pdf"
    assert export.safe_filename("../../etc/passwd", "md") == "etcpasswd.md"
    assert export.safe_filename("???", "txt") == "note.txt"
