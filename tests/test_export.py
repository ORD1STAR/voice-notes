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
        "summary": "Résumé avec des accents et un tiret - long.",
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


# ------------------------------------------------------ diagrams and layout

from fakes import RICH_NOTES  # noqa: E402


def rich_job(**extra):
    return {**JOB, "notes": RICH_NOTES, **extra}


def pages(data: bytes) -> int:
    import re

    return len(re.findall(rb"/Type\s*/Page\b(?!s)", data))


def test_pdf_with_diagrams_is_valid_and_bigger_than_plain():
    with_diagrams = export.to_pdf(rich_job())
    plain = export.to_pdf(JOB)
    assert with_diagrams.startswith(b"%PDF-") and with_diagrams.rstrip().endswith(b"%%EOF")
    assert len(with_diagrams) > len(plain)


def test_pdf_diagrams_do_not_break_other_fonts_or_languages(monkeypatch):
    french = {**RICH_NOTES, "title": "Plan de lancement \u00e9t\u00e9", "sections": [
        {"heading": "D\u00e9marche", "bullets": ["D'abord, v\u00e9rifier le contenu.", "Ensuite, publier la page.", "Enfin, annoncer."]},
        {"heading": "Budget", "bullets": ["Pub: 5000 euros.", "Contenu: 2500 euros.", "Outils: 800 euros."]},
        {"heading": "Equipe", "bullets": ["R\u00e9union le lundi."]},
    ]}
    assert export.to_pdf({**JOB, "notes": french}).startswith(b"%PDF-")
    monkeypatch.setattr(export, "FONT_DIRS", [])
    assert export.to_pdf({**JOB, "notes": french}).startswith(b"%PDF-")


def test_pdf_long_note_with_diagrams_and_transcript_paginates():
    sections = [{"heading": f"Topic {i}", "bullets": [f"Point {i} " + "word " * 25, f"Another {i} " + "word " * 25,
                                                      f"Rate {i} is {i + 3}%"]} for i in range(14)]
    job = {**JOB, "notes": {**JOB["notes"], "sections": sections}, "transcript": "A sentence. " * 400}
    data = export.to_pdf(job, include_transcript=True)
    assert pages(data) >= 5


def test_footer_shows_page_numbers():
    data = export.to_pdf(rich_job(), include_transcript=False)
    # Page count alias is replaced by the real total, so the placeholder must not survive.
    assert b"{nb}" not in data


def test_transcript_is_split_into_paragraphs_in_text_exports():
    job = {**JOB, "transcript": " ".join(f"Sentence number {i} is here." for i in range(80))}
    md = export.to_markdown(job, include_transcript=True)
    body = md.split("## Transcript", 1)[1].strip()
    assert body.count("\n\n") >= 2
    txt = export.to_text(job, include_transcript=True)
    assert txt.split("TRANSCRIPT", 1)[1].strip().count("\n\n") >= 2


# ------------------------------------------------------------ action items

def action_job():
    return {**JOB, "notes": {**JOB["notes"], "actions": ["Call Anna about the delivery", "Order the seeds"]}}


def test_actions_appear_as_task_lists_in_text_exports():
    md = export.to_markdown(action_job())
    assert "## Action items\n\n- [ ] Call Anna about the delivery\n- [ ] Order the seeds" in md
    assert md.index("## Summary") < md.index("## Action items") < md.index("## Website")
    txt = export.to_text(action_job())
    assert "ACTION ITEMS\n\n[ ] Call Anna about the delivery\n[ ] Order the seeds" in txt
    assert "Action items" not in export.to_markdown(JOB)


def test_pdf_with_actions_is_valid_and_longer():
    with_actions = export.to_pdf(action_job())
    assert with_actions.startswith(b"%PDF-") and with_actions.rstrip().endswith(b"%%EOF")
    assert len(with_actions) > len(export.to_pdf(JOB))


# -------------------------------------------------------------- key points

def key_job():
    return {**JOB, "notes": {**JOB["notes"], "key_points": ["The landing page is due on Friday.", "The ad budget stays the same."],
                             "actions": ["Call Anna about the delivery"]}}


def test_key_points_sit_between_the_summary_and_the_action_items():
    md = export.to_markdown(key_job())
    assert "## Key points\n\n- The landing page is due on Friday.\n- The ad budget stays the same." in md
    assert md.index("## Summary") < md.index("## Key points") < md.index("## Action items") < md.index("## Website")
    txt = export.to_text(key_job())
    assert "KEY POINTS\n\n- The landing page is due on Friday." in txt
    assert txt.index("SUMMARY") < txt.index("KEY POINTS") < txt.index("ACTION ITEMS")
    assert "Key points" not in export.to_markdown(JOB)  # none stored, none shown


def test_pdf_with_key_points_is_valid_and_longer():
    with_points = export.to_pdf(key_job())
    assert with_points.startswith(b"%PDF-") and with_points.rstrip().endswith(b"%%EOF")
    assert len(with_points) > len(export.to_pdf({**JOB, "notes": {**JOB["notes"], "actions": ["Call Anna about the delivery"]}}))


def test_older_notes_get_key_points_when_exported():
    from transcripts import MEETING

    from app import structure

    rules = structure.structure_rules(MEETING)
    old = {"title": rules["title"], "summary": rules["summary"], "sections": rules["sections"]}  # saved without key_points
    job = {**JOB, "notes": old}
    md, _ = export.render(job, "md")
    assert b"## Key points" in md
    assert "key_points" not in job["notes"]  # the stored note is not touched
