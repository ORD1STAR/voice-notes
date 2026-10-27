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

    def fake(path, client=None, retries=3, language=None):
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
    def boom(path, client=None, retries=3, language=None):
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


# ------------------------------------------- rename, search, retry, status

def done_note(title, bullets, transcript="", filename="rec.m4a", actions=None, created=None):
    from app import jobs

    job = jobs.new_job(filename)
    notes = {"title": title, "summary": f"About {title}.", "sections": [{"heading": "Main", "bullets": bullets}],
             "actions": actions or []}
    fields = dict(status="done", stage="done", notes=notes, transcript=transcript, engine="rule-based")
    if created:
        fields["created"] = created
    jobs._update(job["id"], **fields)
    return job["id"]


def test_rename_changes_the_title_everywhere(client):
    nid = done_note("Old title", ["A point."])
    r = client.patch(f"/api/notes/{nid}", json={"title": "  A better\n title  "})
    assert r.status_code == 200 and r.json()["notes"]["title"] == "A better title"
    assert client.get("/api/notes").json()[0]["title"] == "A better title"
    assert client.get(f"/api/notes/{nid}/export?format=md").text.startswith("# A better title")
    cd = client.get(f"/api/notes/{nid}/export?format=pdf").headers["content-disposition"]
    assert "a-better-title.pdf" in cd


def test_rename_rejects_empty_and_unfinished(client):
    from app import jobs

    nid = done_note("Title", ["A point."])
    assert client.patch(f"/api/notes/{nid}", json={"title": "   "}).status_code == 400
    assert client.patch(f"/api/notes/{nid}", json={}).status_code == 422
    pending = jobs.new_job("x.wav")
    assert client.patch(f"/api/notes/{pending['id']}", json={"title": "Nope"}).status_code == 404
    assert client.patch("/api/notes/deadbeef", json={"title": "Nope"}).status_code == 404
    long = client.patch(f"/api/notes/{nid}", json={"title": "w" * 400}).json()["notes"]["title"]
    assert len(long) == 100


def test_search_looks_inside_the_notes_and_shows_where(client):
    a = done_note("Garden plan", ["Build a compost bin from pallets."], "we talked about the garden and the shed",
                  created=1000.0)
    b = done_note("Budget", ["Revenue is down."], "revenue and costs, also a mention of compost", created=2000.0)
    c = done_note("Other", ["Nothing relevant."], "", created=3000.0)
    all_ids = [n["id"] for n in client.get("/api/notes").json()]
    assert all_ids == [c, b, a]
    hits = client.get("/api/notes", params={"q": "compost"}).json()
    assert [h["id"] for h in hits] == [b, a]
    assert "compost" in hits[0]["snippet"].lower() and "compost" in hits[1]["snippet"].lower()
    assert [h["id"] for h in client.get("/api/notes", params={"q": "COMPOST pallets"}).json()] == [a]
    assert client.get("/api/notes", params={"q": "zebra"}).json() == []
    # a title match comes first, even if the note is older
    d = done_note("Compost guide", ["Layers."], created=500.0)
    assert [h["id"] for h in client.get("/api/notes", params={"q": "compost"}).json()][0] == d
    # a search on the action items and on the file name
    e = done_note("Chores", ["x"], actions=["Call the plumber"], filename="kitchen-sink.m4a")
    assert [h["id"] for h in client.get("/api/notes", params={"q": "plumber"}).json()] == [e]
    assert [h["id"] for h in client.get("/api/notes", params={"q": "kitchen-sink"}).json()] == [e]


def test_health_explains_the_structuring_method(client):
    h = client.get("/api/health").json()
    assert h["structuring"]["engine"] == "rule-based"
    assert "switched off" in h["structuring"]["reason"]  # the tests run with USE_OLLAMA=0


def test_job_status_has_queue_position_and_elapsed_time(client):
    from app import jobs

    first = jobs.new_job("one.wav")
    second = jobs.new_job("two.wav")
    body = client.get(f"/api/notes/{second['id']}").json()
    assert body["ahead"] == 1 and body["stage_elapsed"] >= 0
    assert client.get(f"/api/notes/{first['id']}").json()["ahead"] == 0
    nid = done_note("T", ["b"])
    assert client.get(f"/api/notes/{nid}").json()["ahead"] == 0


@needs_ffmpeg
def test_failed_transcription_can_be_tried_again(client, make_audio, monkeypatch):
    from app import jobs

    state = {"fail": True}

    def flaky(path, client=None, retries=3, language=None):
        if state["fail"]:
            raise transcribe.TranscriptionError("Groq did not answer in time.")
        return SAMPLE_TEXT

    monkeypatch.setattr(transcribe, "transcribe_file", flaky)
    job_id = upload(client, make_audio(3)).json()["id"]
    job = wait_done(client, job_id)
    assert job["status"] == "error" and job["retryable"] is True and "try again" in job["error"].lower()
    assert jobs.upload_path(job_id).exists()  # the recording is kept for the retry

    state["fail"] = False
    assert client.post(f"/api/notes/{job_id}/retry").status_code == 202
    job = wait_done(client, job_id)
    assert job["status"] == "done" and job["error"] is None and job["notes"]["sections"]
    assert not jobs.upload_path(job_id).exists()  # cleaned up after success
    assert client.post(f"/api/notes/{job_id}/retry").status_code == 409


@needs_ffmpeg
def test_a_bad_file_is_not_offered_a_retry(client, tmp_path, fake_groq):
    f = tmp_path / "notes.txt"
    f.write_text("hello, this is only text")
    job_id = upload(client, f).json()["id"]
    job = wait_done(client, job_id)
    assert job["status"] == "error" and job["retryable"] is False
    assert client.post(f"/api/notes/{job_id}/retry").status_code == 409
    assert client.post("/api/notes/deadbeef/retry").status_code == 404


def test_an_interrupted_note_can_be_resumed_from_its_transcript(client, monkeypatch):
    from app import audio, jobs

    job = jobs.new_job("late.wav")
    jobs._update(job["id"], status="processing", stage="structuring", transcript=SAMPLE_TEXT)
    jobs.load_all()  # the server restarts
    again = jobs.get(job["id"])
    assert again["status"] == "error" and again["retryable"] is True
    monkeypatch.setattr(audio, "probe_duration", lambda p: (_ for _ in ()).throw(AssertionError("must not convert again")))
    assert client.post(f"/api/notes/{job['id']}/retry").status_code == 202
    assert wait_done(client, job["id"])["status"] == "done"


def test_old_failed_uploads_are_removed_at_startup():
    import time

    from app import jobs

    job = jobs.new_job("old.wav")
    jobs.upload_path(job["id"]).write_bytes(b"audio")
    jobs._update(job["id"], status="error", error="x", retryable=True, created=time.time() - 8 * 86400)
    jobs.load_all()
    assert not jobs.upload_path(job["id"]).exists()
    assert jobs.get(job["id"])["retryable"] is False


def test_a_waiting_note_already_knows_how_it_will_be_structured(client):
    from app import jobs

    job = jobs.new_job("w.wav")
    jobs.run(job["id"])  # no upload: it fails at once, but the plan is recorded first
    assert jobs.get(job["id"])["plan"]["engine"] == "rule-based"


def test_progress_text_names_the_builtin_rules(client, make_audio, fake_groq):
    job = wait_done(client, upload(client, make_audio(3)).json()["id"])
    assert job["plan"]["engine"] == "rule-based" and job["plan"]["reason"]


# ------------------------------------------------ key points and rebuilding

def test_older_notes_get_key_points_in_the_api_and_exports(client):
    from transcripts import MEETING

    from app import jobs, structure

    rules = structure.structure_rules(MEETING)
    bullets = [b for s in rules["sections"] for b in s["bullets"]]
    nid = done_note("Old meeting", bullets, transcript=MEETING)
    body = client.get(f"/api/notes/{nid}").json()
    assert body["notes"]["key_points"]
    assert all(k in bullets for k in body["notes"]["key_points"])
    assert "key_points" not in jobs.get(nid)["notes"]  # nothing is rewritten on disk
    assert "## Key points" in client.get(f"/api/notes/{nid}/export?format=md").text


def test_rebuild_writes_the_notes_again_and_keeps_a_chosen_title(client):
    nid = done_note("Old title", ["An old point."], transcript=SAMPLE_TEXT)
    assert client.patch(f"/api/notes/{nid}", json={"title": "My own title"}).status_code == 200
    assert client.post(f"/api/notes/{nid}/restructure").status_code == 202
    job = wait_done(client, nid)
    assert job["status"] == "done" and job["rebuild_error"] is None
    assert job["notes"]["title"] == "My own title"
    bullets = [b for s in job["notes"]["sections"] for b in s["bullets"]]
    assert any("landing page" in b for b in bullets) and "An old point." not in bullets
    assert job["engine"] == "rule-based" and "key_points" in job["notes"]


def test_a_failed_rebuild_keeps_the_earlier_notes(client, monkeypatch):
    from app import structure

    def boom(text, on_progress=None, language="auto", report=None):
        raise structure.StructureError("The model fell over.")

    nid = done_note("Fine as it is", ["A kept point."], transcript=SAMPLE_TEXT)
    monkeypatch.setattr(structure, "structure_transcript", boom)
    assert client.post(f"/api/notes/{nid}/restructure").status_code == 202
    job = wait_done(client, nid)
    assert job["status"] == "done"
    assert job["notes"]["title"] == "Fine as it is" and job["notes"]["sections"][0]["bullets"] == ["A kept point."]
    assert "could not be rebuilt" in job["rebuild_error"] and "The model fell over." in job["rebuild_error"]


def test_rebuild_is_refused_when_it_cannot_work(client):
    from app import jobs

    assert client.post("/api/notes/deadbeef/restructure").status_code == 404
    pending = jobs.new_job("x.wav")
    assert client.post(f"/api/notes/{pending['id']}/restructure").status_code == 409
    no_transcript = done_note("T", ["b"], transcript="")
    assert client.post(f"/api/notes/{no_transcript}/restructure").status_code == 409


def test_a_rebuild_interrupted_by_a_restart_keeps_the_notes():
    from app import jobs

    nid = done_note("Safe", ["A point."], transcript=SAMPLE_TEXT)
    jobs._update(nid, status="processing", stage="structuring", restructure=True)
    jobs.load_all()
    job = jobs.get(nid)
    assert job["status"] == "done" and job["notes"]["title"] == "Safe" and "restarted" in job["rebuild_error"]
