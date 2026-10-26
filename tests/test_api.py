import time

import pytest
from fastapi.testclient import TestClient

from app import transcribe
from app.main import app

from conftest import SAMPLE_TEXT, needs_ffmpeg


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_groq(monkeypatch):
    calls = []

    def fake(path, client=None, retries=3):
        calls.append(path.name)
        return SAMPLE_TEXT

    monkeypatch.setattr(transcribe, "transcribe_file", fake)
    return calls


def wait_done(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/notes/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def upload(client, path, name=None):
    with open(path, "rb") as fh:
        return client.post("/api/notes", files={"file": (name or path.name, fh, "audio/wav")})


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Voice Notes" in r.text


def test_health(client):
    h = client.get("/api/health").json()
    assert h["groq_key"] is True
    assert h["max_upload_mb"] == 200
    assert h["ollama"]["available"] is False
    assert "test-key" not in str(h)


def test_upload_requires_api_key(client, monkeypatch, tmp_path):
    monkeypatch.delenv("GROQ_API_KEY")
    f = tmp_path / "a.wav"
    f.write_bytes(b"data")
    r = upload(client, f)
    assert r.status_code == 503
    assert "GROQ_API_KEY" in r.json()["error"]


@needs_ffmpeg
def test_upload_too_large(client, monkeypatch, make_audio):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    r = upload(client, make_audio(40))  # about 3 MB wav
    assert r.status_code == 413
    assert "MB" in r.json()["error"]
    assert client.get("/api/notes").json() == []


@needs_ffmpeg
def test_empty_upload_rejected(client, tmp_path):
    f = tmp_path / "empty.wav"
    f.write_bytes(b"")
    assert upload(client, f).status_code == 400


@needs_ffmpeg
def test_not_audio_gives_clear_error(client, tmp_path, fake_groq):
    f = tmp_path / "notes.txt"
    f.write_text("hello, this is only text")
    job = wait_done(client, upload(client, f).json()["id"])
    assert job["status"] == "error"
    assert "audio" in job["error"].lower()
    assert fake_groq == []


@needs_ffmpeg
def test_full_flow_and_exports(client, make_audio, fake_groq, monkeypatch):
    monkeypatch.setenv("CHUNK_SECONDS", "10")
    r = upload(client, make_audio(25), "standup.wav")
    assert r.status_code == 202
    job_id = r.json()["id"]
    job = wait_done(client, job_id)

    assert job["status"] == "done", job
    assert len(fake_groq) == 3  # 25 s in 10 s chunks
    assert job["engine"] == "rule-based"
    assert job["notes"]["sections"]
    assert job["transcript"].count("landing page") == 3
    assert job["filename"] == "standup.wav"

    listed = client.get("/api/notes").json()
    assert listed[0]["id"] == job_id and listed[0]["title"]

    md = client.get(f"/api/notes/{job_id}/export?format=md")
    assert md.status_code == 200 and md.text.startswith("# ")
    assert "attachment" in md.headers["content-disposition"] and ".md" in md.headers["content-disposition"]

    txt = client.get(f"/api/notes/{job_id}/export?format=txt&transcript=true")
    assert txt.status_code == 200 and "TRANSCRIPT" in txt.text

    pdf = client.get(f"/api/notes/{job_id}/export?format=pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert pdf.headers["content-type"] == "application/pdf"

    assert client.get(f"/api/notes/{job_id}/export?format=docx").status_code == 400

    assert client.delete(f"/api/notes/{job_id}").status_code == 204
    assert client.get(f"/api/notes/{job_id}").status_code == 404


@needs_ffmpeg
def test_groq_failure_is_reported(client, make_audio, monkeypatch):
    def boom(path, client=None, retries=3):
        raise transcribe.TranscriptionError("Groq rejected the API key. Check GROQ_API_KEY.")

    monkeypatch.setattr(transcribe, "transcribe_file", boom)
    job = wait_done(client, upload(client, make_audio(3)).json()["id"])
    assert job["status"] == "error"
    assert "Groq" in job["error"]
    assert "test-key-not-real" not in str(job)


def test_unknown_note(client):
    assert client.get("/api/notes/deadbeef").status_code == 404
    assert client.get("/api/notes/deadbeef/export?format=md").status_code == 404
    assert client.delete("/api/notes/deadbeef").status_code == 404


def test_export_before_ready_is_refused(client):
    from app import jobs

    job = jobs.new_job("x.wav")
    assert client.get(f"/api/notes/{job['id']}/export?format=md").status_code == 409


def test_jobs_survive_restart(client, make_audio, fake_groq):
    from app import jobs

    job_id = wait_done(client, upload(client, make_audio(3)).json()["id"])["id"]
    jobs.load_all()
    assert jobs.get(job_id)["status"] == "done"
