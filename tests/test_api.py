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


def test_security_headers_and_download_name(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_favicon_and_fonts_are_served(client):
    assert client.get("/favicon.svg").status_code == 200
    f = client.get("/fonts/InstrumentSans-latin.woff2")
    assert f.status_code == 200 and f.content[:4] == b"wOF2"


def test_pending_job_hides_transcript_and_done_job_has_visuals(client):
    from app import jobs

    pending = jobs.new_job("x.wav")
    body = client.get(f"/api/notes/{pending['id']}").json()
    assert body["transcript"] is None and body["visuals"] == []

    done = jobs.new_job("y.wav")
    from fakes import RICH_NOTES

    jobs._update(done["id"], status="done", stage="done", notes=RICH_NOTES, transcript="One. Two. Three.", engine="rule-based")
    body = client.get(f"/api/notes/{done['id']}").json()
    assert {v["kind"] for v in body["visuals"]} == {"mindmap", "flow", "chart"}
    for v in body["visuals"]:
        assert set(v["layouts"]) == {"wide", "narrow"}
    assert body["transcript_paragraphs"] == ["One. Two. Three."]
    pdf = client.get(f"/api/notes/{done['id']}/export?format=pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    cd = pdf.headers["content-disposition"]
    assert 'filename="quarterly-launch-plan.pdf"' in cd and "filename*=UTF-8''quarterly-launch-plan.pdf" in cd


def test_download_name_for_non_latin_title(client):
    from app import jobs

    job = jobs.new_job("z.wav")
    notes = {"title": "\u65e5\u672c\u8a9e", "summary": "", "sections": [{"heading": "h", "bullets": ["b"]}]}
    jobs._update(job["id"], status="done", stage="done", notes=notes, transcript="t", engine="rule-based")
    cd = client.get(f"/api/notes/{job['id']}/export?format=md").headers["content-disposition"]
    assert 'filename="note.md"' in cd


def test_note_deleted_while_queued_is_not_processed(monkeypatch, tmp_path):
    from app import audio, jobs

    job = jobs.new_job("gone.wav")
    assert jobs.delete(job["id"])
    called = []
    monkeypatch.setattr(audio, "probe_duration", lambda p: called.append(p) or 1.0)
    jobs.run(job["id"])
    assert called == []
    assert not (tmp_path / "data" / "jobs" / job["id"]).exists()


def test_static_files_are_revalidated_but_fonts_are_cached(client):
    assert client.get("/style.css").headers["cache-control"] == "no-cache"
    assert client.get("/app.js").headers["cache-control"] == "no-cache"
    assert "max-age" in client.get("/fonts/InstrumentSans-latin.woff2").headers["cache-control"]
