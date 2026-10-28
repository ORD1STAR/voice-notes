"""The list of open tasks across all finished notes."""
import pytest
from fastapi.testclient import TestClient

from app import jobs, structure
from app.main import app

from conftest import SAMPLE_TEXT


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_done(title, created, actions, edited=False):
    job = jobs.new_job("memo.wav")
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    notes["title"] = title
    notes["actions"] = actions
    jobs._update(job["id"], status="done", stage="done", progress=1.0, detail="Done", transcript=SAMPLE_TEXT,
                 notes=notes, engine=engine, seen=True, created=created, edited=edited)
    return job["id"]


def test_no_tasks_gives_an_empty_list(client):
    make_done("Nothing to do", 1000, [])
    assert client.get("/api/tasks").json() == []


def test_tasks_are_listed_per_note_newest_note_first(client):
    old = make_done("Old", 1000, ["Call the bank on Monday"])
    new = make_done("New", 2000, ["Send the invoice to Anna", "Book the room"])
    make_done("Empty", 3000, [])
    data = client.get("/api/tasks").json()
    assert [g["id"] for g in data] == [new, old]
    assert data[0]["title"] == "New" and data[0]["actions"] == ["Send the invoice to Anna", "Book the room"]
    assert data[1]["actions"] == ["Call the bank on Monday"]
    assert data[0]["pinned"] is False


def test_notes_that_are_not_finished_are_left_out(client):
    waiting = jobs.new_job("later.wav")
    jobs._update(waiting["id"], notes={"title": "x", "summary": "", "sections": [], "actions": ["Not yet"]})
    assert client.get("/api/tasks").json() == []


def test_blank_tasks_are_dropped(client):
    make_done("Blanks", 1000, ["Call Anna", "   ", ""])
    assert client.get("/api/tasks").json()[0]["actions"] == ["Call Anna"]


def test_spoken_framing_is_removed_from_tasks_of_old_notes_but_not_from_edited_ones(client):
    make_done("Machine", 1000, ["Thanks for listening.", "Call Anna about the delivery"])
    make_done("By hand", 2000, ["Thanks for listening."], edited=True)
    data = {g["title"]: g["actions"] for g in client.get("/api/tasks").json()}
    assert data["Machine"] == ["Call Anna about the delivery"]
    assert data["By hand"] == ["Thanks for listening."]


def test_a_deleted_note_has_no_tasks_any_more(client):
    nid = make_done("Gone", 1000, ["Call Anna"])
    assert client.delete(f"/api/notes/{nid}").status_code == 204
    assert client.get("/api/tasks").json() == []


def test_the_tasks_page_is_served_by_the_single_page(client):
    assert client.get("/").status_code == 200
