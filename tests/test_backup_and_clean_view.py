"""The backup of all notes, and notes saved with spoken framing that are cleaned when shown or exported."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import jobs, structure
from app.main import app
from conftest import SAMPLE_TEXT


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_note(title="Website launch", bullets=None, edited=False, name="memo.wav", created=None):
    job = jobs.new_job(name)
    notes = {
        "title": title,
        "summary": "Explique moi comment ça marche. Le lancement du site est prévu pour vendredi.",
        "key_points": ["Voilà.", "Le budget des annonces reste inchangé."],
        "sections": [{"heading": "Lancement", "bullets": bullets or [
            "Explique moi comment fonctionne l'export PDF.",
            "Écoute, la page d'accueil sera terminée vendredi.",
            "Voilà.",
            "Le budget des annonces reste inchangé."]}],
        "actions": ["Peux-tu envoyer le texte final à Anna avant jeudi ?"],
    }
    fields = dict(status="done", stage="done", progress=1.0, detail="Done", transcript=SAMPLE_TEXT, notes=notes,
                  engine="groq:llama-3.3-70b-versatile", seen=True, edited=edited)
    if created:
        fields["created"] = created
    jobs._update(job["id"], **fields)
    return job["id"]


# ------------------------------------------------------------ cleaned when shown


def test_old_notes_lose_their_spoken_framing_in_the_api(client):
    nid = make_note()
    notes = client.get(f"/api/notes/{nid}").json()["notes"]
    assert notes["sections"][0]["bullets"] == [
        "Comment fonctionne l'export PDF.",
        "La page d'accueil sera terminée vendredi.",
        "Le budget des annonces reste inchangé.",
    ]
    assert notes["summary"] == "Comment ça marche. Le lancement du site est prévu pour vendredi."
    assert notes["key_points"] == ["Le budget des annonces reste inchangé."]
    assert notes["actions"] == ["Envoyer le texte final à Anna avant jeudi."]
    assert notes["title"] == "Website launch"


def test_the_saved_note_is_not_rewritten_by_showing_it(client):
    nid = make_note()
    client.get(f"/api/notes/{nid}")
    assert jobs.get(nid)["notes"]["sections"][0]["bullets"][0].startswith("Explique moi")


def test_a_note_the_reader_edited_is_shown_as_written(client):
    nid = make_note(edited=True)
    notes = client.get(f"/api/notes/{nid}").json()["notes"]
    assert notes["sections"][0]["bullets"][0].startswith("Explique moi")


def test_exports_are_cleaned_too(client):
    nid = make_note()
    text = client.get(f"/api/notes/{nid}/export?format=md").text
    assert "Explique moi" not in text and "Écoute" not in text and "Voilà" not in text
    assert "La page d'accueil sera terminée vendredi." in text
    pdf = client.get(f"/api/notes/{nid}/export?format=pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_notes_that_would_be_left_empty_stay_as_they_are():
    notes = {"title": "T", "summary": "S", "sections": [{"heading": "H", "bullets": ["Voilà.", "Explique-moi."]}]}
    assert structure.unspoken_notes(notes) == notes


def test_clean_notes_come_back_unchanged():
    notes = structure.structure_rules(SAMPLE_TEXT)
    assert structure.unspoken_notes(notes) == notes


# ------------------------------------------------------------ the backup


def test_the_backup_is_a_zip_with_one_file_per_finished_note(client):
    a = make_note("Website launch", created=1_700_000_000)
    b = make_note("Website launch", bullets=["Le site ouvre vendredi."], created=1_700_100_000)
    pending = jobs.new_job("later.wav")  # not finished: not in the backup
    r = client.get("/api/export/all?format=md")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert 'filename="voice-notes-' in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert len(names) == 2 and len(set(names)) == 2  # the same title twice still gives two files
    assert all(n.endswith(".md") and n[:4].isdigit() for n in names)
    joined = "\n".join(zf.read(n).decode("utf-8") for n in names)
    assert "Le site ouvre vendredi." in joined
    assert "Explique moi" not in joined
    assert a != b and pending["id"] not in joined


def test_the_backup_can_be_text_and_can_carry_the_transcripts(client):
    make_note()
    r = client.get("/api/export/all?format=txt&transcript=true")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    (name,) = zf.namelist()
    assert name.endswith(".txt")
    assert "Today we planned the launch" in zf.read(name).decode("utf-8")


def test_the_backup_refuses_other_formats_and_an_empty_library(client):
    assert client.get("/api/export/all?format=pdf").status_code == 400
    r = client.get("/api/export/all")
    assert r.status_code == 404 and "no finished notes" in r.json()["error"]
