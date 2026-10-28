"""Pinning a finished note to the top of the list."""
import json

import pytest
from fastapi.testclient import TestClient

from app import jobs, structure
from app.main import app

from conftest import SAMPLE_TEXT


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_done(title: str, created: float) -> str:
    job = jobs.new_job("memo.wav")
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    notes["title"] = title
    jobs._update(job["id"], status="done", stage="done", progress=1.0, detail="Done", transcript=SAMPLE_TEXT,
                 notes=notes, engine=engine, seen=True, created=created)
    return job["id"]


def titles(client, query=""):
    return [r["title"] for r in client.get("/api/notes", params={"q": query}).json()]


def pin(client, note_id, pinned=True):
    return client.put(f"/api/notes/{note_id}/pin", json={"pinned": pinned})


def test_notes_are_not_pinned_by_default(client):
    nid = make_done("Alpha", 1000)
    assert client.get("/api/notes").json()[0]["pinned"] is False
    assert client.get(f"/api/notes/{nid}").json()["pinned"] is False


def test_a_pinned_note_goes_to_the_top_whatever_its_date(client):
    old = make_done("Old note", 1000)
    make_done("Middle note", 2000)
    make_done("New note", 3000)
    assert titles(client) == ["New note", "Middle note", "Old note"]
    r = pin(client, old)
    assert r.status_code == 200 and r.json()["pinned"] is True
    assert titles(client) == ["Old note", "New note", "Middle note"]
    assert client.get("/api/notes").json()[0]["pinned"] is True
    assert client.get(f"/api/notes/{old}").json()["pinned"] is True


def test_several_pinned_notes_keep_the_newest_first_among_themselves(client):
    a = make_done("A", 1000)
    b = make_done("B", 2000)
    make_done("C", 3000)
    pin(client, a)
    pin(client, b)
    assert titles(client) == ["B", "A", "C"]


def test_unpinning_puts_the_note_back_by_date(client):
    old = make_done("Old note", 1000)
    make_done("New note", 3000)
    pin(client, old)
    r = pin(client, old, False)
    assert r.status_code == 200 and r.json()["pinned"] is False
    assert titles(client) == ["New note", "Old note"]


def test_pinning_twice_changes_nothing(client):
    old = make_done("Old note", 1000)
    make_done("New note", 3000)
    pin(client, old)
    pin(client, old)
    assert titles(client) == ["Old note", "New note"]


def test_the_pin_survives_a_restart(client):
    old = make_done("Old note", 1000)
    make_done("New note", 3000)
    pin(client, old)
    saved = json.loads((jobs._job_dir(old) / "job.json").read_text(encoding="utf-8"))
    assert saved["pinned"] is True
    jobs.load_all()
    assert titles(client) == ["Old note", "New note"]


def test_search_still_puts_title_matches_first_then_pinned_notes(client):
    make_done("Garden budget", 1000)
    b = make_done("Garden notes", 2000)
    make_done("Weekly plan", 3000)  # all three talk about the budget in their text
    assert titles(client, "budget") == ["Garden budget", "Weekly plan", "Garden notes"]
    pin(client, b)
    assert titles(client, "budget") == ["Garden budget", "Garden notes", "Weekly plan"]


def test_only_finished_known_notes_can_be_pinned(client):
    assert pin(client, "doesnotexist").status_code == 404
    waiting = jobs.new_job("later.wav")
    assert pin(client, waiting["id"]).status_code == 404
    assert client.get("/api/notes").json()[0]["pinned"] is False


def test_the_body_must_say_what_to_do(client):
    nid = make_done("Alpha", 1000)
    assert client.put(f"/api/notes/{nid}/pin", json={}).status_code == 422
    assert client.put(f"/api/notes/{nid}/pin", json={"pinned": "maybe"}).status_code == 422


def test_editing_and_renaming_keep_the_pin(client):
    nid = make_done("Alpha", 1000)
    pin(client, nid)
    assert client.patch(f"/api/notes/{nid}", json={"title": "Beta"}).json()["pinned"] is True
    assert titles(client)[0] == "Beta"
