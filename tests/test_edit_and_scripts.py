"""Editing the notes, PDF export for scripts the fonts cannot draw, and the Groq retry on broken JSON."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import export, jobs, structure
from app.main import app
from conftest import SAMPLE_TEXT
from test_groq_structuring import CHAT_URL, FR_NOTES, FR_TRANSCRIPT, FakeNet


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def done_note(title="Plan", bullets=("A point.",), transcript="", summary="A summary.", **extra):
    job = jobs.new_job("rec.m4a")
    notes = {"title": title, "summary": summary, "key_points": [], "actions": [],
             "sections": [{"heading": "Main", "bullets": list(bullets)}]}
    jobs._update(job["id"], status="done", stage="done", notes=notes, transcript=transcript, engine="rule-based", **extra)
    return job["id"]


def body(**over):
    data = {"title": "Edited title", "summary": "New summary.", "key_points": ["Key one."], "actions": ["Call Anna"],
            "sections": [{"heading": "Intro", "bullets": ["First point.", "Second point."]}]}
    data.update(over)
    return data


# ------------------------------------------------------------------ editing

def test_edit_replaces_the_text_and_marks_the_note_as_edited(client):
    nid = done_note()
    r = client.put(f"/api/notes/{nid}/notes", json=body())
    assert r.status_code == 200
    got = r.json()
    assert got["edited"] is True
    assert got["notes"]["title"] == "Edited title" and got["notes"]["summary"] == "New summary."
    assert got["notes"]["sections"] == [{"heading": "Intro", "bullets": ["First point.", "Second point."]}]
    assert got["notes"]["actions"] == ["Call Anna"] and got["notes"]["key_points"] == ["Key one."]
    assert client.get("/api/notes").json()[0]["title"] == "Edited title"
    md = client.get(f"/api/notes/{nid}/export?format=md").text
    assert "Second point." in md and "A point." not in md


def test_edit_survives_a_restart_and_empty_key_points_stay_empty(client):
    nid = done_note()
    assert client.put(f"/api/notes/{nid}/notes", json=body(key_points=[], actions=[])).status_code == 200
    jobs.load_all()
    got = client.get(f"/api/notes/{nid}").json()
    assert got["edited"] is True and got["notes"]["key_points"] == []  # not computed again behind the reader's back


def test_edit_cleans_the_text(client):
    nid = done_note()
    messy = body(title="  A\n  title  ", summary="Line one.\n\nLine   two.",
                 key_points=["- Same", "same", "", "  "],
                 actions=["[ ] Pay the bill", "* Pay the bill"],
                 sections=[{"heading": "", "bullets": ["  - spaced   out ", ""]}, {"heading": "Empty", "bullets": []},
                           "not a section"])
    notes = client.put(f"/api/notes/{nid}/notes", json=messy).json()["notes"]
    assert notes["title"] == "A title" and notes["summary"] == "Line one. Line two."
    assert notes["key_points"] == ["Same"] and notes["actions"] == ["Pay the bill"]
    assert notes["sections"] == [{"heading": "Notes", "bullets": ["spaced out"]}]


def test_edit_limits(client):
    nid = done_note()
    long = client.put(f"/api/notes/{nid}/notes", json=body(title="t" * 400, summary="s" * 5000,
                      sections=[{"heading": "h" * 300, "bullets": ["b" * 2000] * 2 + [f"b{i}" for i in range(60)]}])).json()["notes"]
    assert len(long["title"]) == 100 and len(long["summary"]) == 2000
    assert len(long["sections"][0]["heading"]) == 100
    assert len(long["sections"][0]["bullets"]) <= 30 and all(len(b) <= 600 for b in long["sections"][0]["bullets"])


def test_edit_is_refused_when_it_makes_no_sense(client):
    nid = done_note()
    assert client.put(f"/api/notes/{nid}/notes", json=body(title="   ")).status_code == 400
    assert client.put(f"/api/notes/{nid}/notes", json=body(sections=[])).status_code == 400
    assert client.put(f"/api/notes/{nid}/notes", json=body(sections=[{"heading": "x", "bullets": []}])).status_code == 400
    assert client.put(f"/api/notes/{nid}/notes", json=body(actions="Call Anna")).status_code == 422
    assert client.put(f"/api/notes/{nid}/notes", json={"summary": "no title"}).status_code == 422
    pending = jobs.new_job("x.wav")
    assert client.put(f"/api/notes/{pending['id']}/notes", json=body()).status_code == 404
    assert client.put("/api/notes/deadbeef/notes", json=body()).status_code == 404
    assert client.get(f"/api/notes/{nid}").json()["notes"]["title"] == "Plan"  # nothing changed


def test_a_rebuild_replaces_the_edit_but_keeps_the_chosen_title(client):
    import time

    nid = done_note(transcript=SAMPLE_TEXT)
    client.put(f"/api/notes/{nid}/notes", json=body(title="My own title"))
    assert client.post(f"/api/notes/{nid}/restructure").status_code == 202
    for _ in range(300):
        job = client.get(f"/api/notes/{nid}").json()
        if job["status"] in ("done", "error") and not job.get("restructure"):
            break
        time.sleep(0.1)
    assert job["status"] == "done" and job["notes"]["title"] == "My own title"
    assert job["edited"] is False
    assert any("landing page" in b for s in job["notes"]["sections"] for b in s["bullets"])


# --------------------------------------------------------- PDF and scripts

ARABIC = "مرحبا بكم في الاجتماع اليوم"
CHINESE = "今天我们讨论了新网站的发布计划"


def test_pdf_problem_names_the_script():
    def job(text):
        return {"notes": {"title": text, "summary": text, "sections": [{"heading": text, "bullets": [text, text]}]}}

    assert "Arabic" in export.pdf_problem(job(ARABIC))
    assert "Chinese" in export.pdf_problem(job(CHINESE))
    assert export.pdf_problem(job("Plan de lancement été, café naïve")) is None
    assert export.pdf_problem(job("План запуска сайта")) is None
    assert export.pdf_problem(job("Ανακοίνωση προγράμματος")) is None
    assert export.pdf_problem(job("12 34 56")) is None
    # a Latin note with one foreign word is fine
    mixed = {"notes": {"title": "Meeting notes", "summary": "We talked about the word " + CHINESE[:2] + " and the plan.",
                       "sections": [{"heading": "Plan", "bullets": ["Ship the page on Friday after the review."]}]}}
    assert export.pdf_problem(mixed) is None


@pytest.mark.parametrize("text, word", [(ARABIC, "Arabic"), (CHINESE, "Chinese")])
def test_pdf_is_refused_clearly_but_text_formats_still_work(client, text, word):
    nid = done_note(title=text, bullets=(text, text), summary=text)
    r = client.get(f"/api/notes/{nid}/export?format=pdf")
    assert r.status_code == 422 and word in r.json()["error"] and "Markdown" in r.json()["error"]
    assert client.get(f"/api/notes/{nid}").json()["pdf_problem"] == r.json()["error"]
    assert text in client.get(f"/api/notes/{nid}/export?format=md").text
    assert text in client.get(f"/api/notes/{nid}/export?format=txt").text


def test_latin_notes_have_no_pdf_problem_and_still_export(client):
    nid = done_note()
    assert client.get(f"/api/notes/{nid}").json()["pdf_problem"] is None
    r = client.get(f"/api/notes/{nid}/export?format=pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")
    pending = jobs.new_job("x.wav")
    assert client.get(f"/api/notes/{pending['id']}").json()["pdf_problem"] is None


# ------------------------------------------------------ Groq, broken JSON

def broken_json_once():
    state = {"n": 0}

    def chat(payload):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(400, json={"error": {"message": "Failed to generate JSON.", "code": "json_validate_failed"}},
                                  request=httpx.Request("POST", CHAT_URL))
        return FR_NOTES

    return chat, state


def test_groq_is_asked_again_when_the_model_breaks_its_json(monkeypatch):
    chat, state = broken_json_once()
    net = FakeNet(monkeypatch, chat=chat)
    notes, engine = structure.structure_transcript(FR_TRANSCRIPT, language="fr")
    assert engine.startswith("groq:") and state["n"] == 2 and len(net.chat_calls) == 2
    assert "proc" in notes["title"].lower()


def test_other_groq_400_errors_are_not_retried(monkeypatch):
    calls = {"n": 0}

    def chat(payload):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad request", "code": "invalid_request_error"}},
                              request=httpx.Request("POST", CHAT_URL))

    FakeNet(monkeypatch, chat=chat)
    notes, engine = structure.structure_transcript(FR_TRANSCRIPT, language="fr")
    assert engine == "rule-based" and calls["n"] == 1


def test_a_model_that_keeps_breaking_its_json_falls_back(monkeypatch):
    calls = {"n": 0}

    def chat(payload):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"code": "json_validate_failed"}}, request=httpx.Request("POST", CHAT_URL))

    FakeNet(monkeypatch, chat=chat)
    notes, engine = structure.structure_transcript(FR_TRANSCRIPT, language="fr")
    assert engine == "rule-based" and calls["n"] == 3


def test_section_similarity_uses_the_shared_stem():
    # the plural and the singular must count as the same word when sections are grouped
    a = ["The payment integration is late."]
    b = ["The payments integrations are late."]
    assert structure._similarity(a, b) > 0.9


# ------------------------------------------- a section that only lists tasks

TASK_SOURCE = (
    "The mobile release is delayed by two weeks because the payment integration is not stable. "
    "I need to ask Tom to profile the export code. Maria will send the job description by Wednesday. "
    "The next sync will be on the 14th."
)


def test_a_task_only_section_moves_into_the_checklist():
    raw = {"title": "Sync recap", "summary": "The mobile release is delayed by two weeks.",
           "sections": [
               {"heading": "Mobile release", "bullets": ["The release is delayed by two weeks because the payment integration is not stable."]},
               {"heading": "Action Items", "bullets": ["I need to ask Tom to profile the export code.",
                                                        "Maria will send the job description by Wednesday.",
                                                        "The next sync will be on the 14th."]}],
           "actions": []}
    out = structure.refine_model_notes(structure.normalize_notes(raw), TASK_SOURCE)
    headings = [s["heading"] for s in out["sections"]]
    assert "Mobile release" in headings and "Action Items" in headings  # the non-task bullet keeps its section
    action_section = next(s for s in out["sections"] if s["heading"] == "Action Items")
    assert action_section["bullets"] == ["The next sync will be on the 14th."]
    assert any("Tom" in a for a in out["actions"]) and any(a.startswith("Maria") for a in out["actions"])


def test_a_section_made_only_of_tasks_disappears():
    raw = {"title": "Sync recap", "summary": "The mobile release is delayed by two weeks.",
           "sections": [
               {"heading": "Mobile release", "bullets": ["The release is delayed by two weeks because the payment integration is not stable."]},
               {"heading": "Next steps", "bullets": ["I need to ask Tom to profile the export code.",
                                                     "Maria will send the job description by Wednesday."]}],
           "actions": []}
    out = structure.refine_model_notes(structure.normalize_notes(raw), TASK_SOURCE)
    assert [s["heading"] for s in out["sections"]] == ["Mobile release"]
    assert len(out["actions"]) == 2 and not any(a.lower().startswith("i need") for a in out["actions"])


def test_tasks_repeated_as_bullets_are_dropped_for_the_local_model_only():
    raw = {"title": "Sync recap", "summary": "The mobile release is delayed by two weeks.",
           "sections": [
               {"heading": "Mobile release", "bullets": ["The release is delayed by two weeks because the payment integration is not stable."]},
               {"heading": "Profile Export Code", "bullets": ["I need to ask Tom to profile the export code."]},
               {"heading": "Next sync", "bullets": ["The next sync will be on the 14th."]}],
           "actions": ["Ask Tom to profile the export code"]}
    local = structure.refine_model_notes(structure.normalize_notes(raw), TASK_SOURCE)
    assert [s["heading"] for s in local["sections"]] == ["Mobile release", "Next sync"]
    assert local["actions"] == ["Ask Tom to profile the export code"]
    strong = structure.refine_model_notes(structure.normalize_notes(raw), TASK_SOURCE, relaxed=True)
    assert [s["heading"] for s in strong["sections"]] == ["Mobile release", "Profile Export Code", "Next sync"]


def test_repeated_tasks_never_empty_the_notes():
    raw = {"title": "Calls", "summary": "Two calls.",
           "sections": [{"heading": "Calls", "bullets": ["I need to ask Tom to profile the export code."]}],
           "actions": ["Ask Tom to profile the export code"]}
    out = structure.refine_model_notes(structure.normalize_notes(raw), TASK_SOURCE)
    assert out["sections"][0]["bullets"] == ["I need to ask Tom to profile the export code."]
