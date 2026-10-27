"""The share link route, the 'not opened yet' flag of finished notes, and work that goes on without a browser."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import jobs, structure, transcribe
from app.main import app

from conftest import SAMPLE_TEXT, needs_ffmpeg


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_done(seen: bool = False, name: str = "memo.wav") -> str:
    """A finished note, built without any audio."""
    job = jobs.new_job(name)
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    jobs._update(job["id"], status="done", stage="done", progress=1.0, detail="Done", transcript=SAMPLE_TEXT,
                 notes=notes, engine=engine, seen=seen)
    return job["id"]


def row(client, note_id):
    return next(r for r in client.get("/api/notes").json() if r["id"] == note_id)


def wait_for(check, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


# ------------------------------------------------------------------ seen flag

def test_new_jobs_start_unseen_but_only_finished_notes_get_a_dot(client):
    job = jobs.new_job("a.wav")
    assert job["seen"] is False
    assert row(client, job["id"])["unseen"] is False  # still waiting: nothing to open yet
    nid = make_done(seen=False)
    assert row(client, nid)["unseen"] is True


def test_failed_notes_never_show_the_dot(client):
    job = jobs.new_job("a.wav")
    jobs._update(job["id"], status="error", error="x", retryable=False)
    assert row(client, job["id"])["unseen"] is False


def test_marking_seen_is_idempotent_and_persisted(client, tmp_path):
    nid = make_done()
    assert client.post(f"/api/notes/{nid}/seen").status_code == 204
    assert client.post(f"/api/notes/{nid}/seen").status_code == 204
    assert row(client, nid)["unseen"] is False
    saved = json.loads((jobs._job_dir(nid) / "job.json").read_text(encoding="utf-8"))
    assert saved["seen"] is True
    jobs.load_all()  # a restart keeps it
    assert row(client, nid)["unseen"] is False


def test_unknown_note_cannot_be_marked(client):
    r = client.post("/api/notes/deadbeef0000/seen")
    assert r.status_code == 404
    assert "error" in r.json()


def test_reading_a_note_through_the_api_does_not_mark_it(client):
    nid = make_done()
    assert client.get(f"/api/notes/{nid}").json()["status"] == "done"
    assert row(client, nid)["unseen"] is True


def test_old_notes_without_the_field_count_as_seen_and_are_not_rewritten(client):
    nid = make_done(seen=True)
    path = jobs._job_dir(nid) / "job.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["seen"]
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    jobs.load_all()
    assert row(client, nid)["unseen"] is False
    assert client.get(f"/api/notes/{nid}").json()["seen"] is True
    assert path.read_text(encoding="utf-8") == before  # no migration of the file


def test_rebuilding_a_note_you_already_opened_does_not_flag_it_again(client):
    nid = make_done(seen=True)
    assert client.post(f"/api/notes/{nid}/restructure").status_code == 202
    wait_for(lambda: jobs.get(nid)["status"] == "done" and not jobs.get(nid).get("restructure"))
    assert row(client, nid)["unseen"] is False


def test_rebuilding_a_note_nobody_opened_keeps_it_flagged(client):
    nid = make_done(seen=False)
    assert client.post(f"/api/notes/{nid}/restructure").status_code == 202
    wait_for(lambda: jobs.get(nid)["status"] == "done" and not jobs.get(nid).get("restructure"))
    assert row(client, nid)["unseen"] is True


def test_a_retried_note_is_new_again_when_it_completes(client):
    nid = make_done(seen=True)
    # a failed note that kept its transcript can be retried without any audio
    jobs._update(nid, status="error", error="x", retryable=True, notes=None, seen=True)
    assert row(client, nid)["unseen"] is False
    assert client.post(f"/api/notes/{nid}/retry").status_code == 202
    wait_for(lambda: jobs.get(nid)["status"] == "done")
    assert row(client, nid)["unseen"] is True


def test_search_rows_carry_the_flag_too(client):
    nid = make_done()
    rows = client.get("/api/notes", params={"q": "landing"}).json()
    assert [r["id"] for r in rows] == [nid] and rows[0]["unseen"] is True


# ----------------------------------------------------------- background work

@needs_ffmpeg
def test_a_note_keeps_being_prepared_with_no_client_watching(client, make_audio, monkeypatch):
    monkeypatch.setenv("CHUNK_SECONDS", "10")

    def slow(path, client=None, retries=3, language=None):
        time.sleep(0.3)
        return SAMPLE_TEXT

    monkeypatch.setattr(transcribe, "transcribe_file", slow)
    with open(make_audio(12), "rb") as fh:
        nid = client.post("/api/notes", files={"file": ("long.wav", fh, "audio/wav")}).json()["id"]

    first = row(client, nid)  # what a freshly opened tab sees
    assert first["status"] in ("queued", "processing") and first["unseen"] is False
    # nobody asks for the note itself, only the list is read, as a reloaded page would do
    done = wait_for(lambda: row(client, nid) if row(client, nid)["status"] == "done" else None)
    assert done["unseen"] is True
    assert jobs.get(nid)["notes"]["title"]


def test_a_restart_in_the_middle_of_a_job_is_reported_and_can_be_retried(client):
    job = jobs.new_job("a.wav")
    jobs.upload_path(job["id"]).write_bytes(b"audio")  # the upload is kept, so the note can be tried again
    jobs._update(job["id"], status="processing", stage="transcribing", progress=0.4)
    jobs.load_all()  # the server restarts
    j = client.get(f"/api/notes/{job['id']}").json()
    assert j["status"] == "error" and j["retryable"] is True and "restarted" in j["error"]
    assert row(client, job["id"])["unseen"] is False


# ------------------------------------------------------------ share link page

def test_share_route_serves_the_page_with_safe_headers(client):
    nid = make_done()
    r = client.get(f"/n/{nid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert 'name="robots"' in r.text and "noindex" in r.text
    assert r.text == client.get("/").text  # the same page; the script reads the address


@pytest.mark.parametrize("bad", ["doesnotexist", "a b", "x" * 300, "%00", "éè"])
def test_share_route_serves_the_page_for_unknown_or_odd_ids(client, bad):
    r = client.get(f"/n/{bad}")
    assert r.status_code == 200 and "Voice Notes" in r.text
    assert r.headers["x-robots-tag"] == "noindex, nofollow"


def test_home_page_is_not_marked_noindex_by_header_but_has_the_meta(client):
    r = client.get("/")
    assert "x-robots-tag" not in r.headers
    assert 'name="robots"' in r.text


def test_api_still_404s_for_unknown_ids(client):
    r = client.get("/api/notes/doesnotexist")
    assert r.status_code == 404 and r.json()["error"] == "This note does not exist."
