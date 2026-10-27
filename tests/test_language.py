"""The language field: start page choice, upload, transcription, structuring and storage."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import jobs, languages, structure, transcribe
from app.main import app
from conftest import SAMPLE_TEXT, needs_ffmpeg
from test_groq_structuring import FR_NOTES, FR_TRANSCRIPT, FakeNet, status


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def transcribed(monkeypatch):
    """Replace the Whisper call and record the language it was given."""
    seen = []

    def fake(path, client=None, retries=3, language=None):
        seen.append(language)
        return FR_TRANSCRIPT

    monkeypatch.setattr(transcribe, "transcribe_file", fake)
    return seen


def wait_done(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/notes/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def upload(client, path, **fields):
    with open(path, "rb") as fh:
        return client.post("/api/notes", files={"file": (path.name, fh, "audio/wav")}, data=fields)


# ---------------------------------------------------------------- the list

def test_language_list_has_auto_first_and_the_common_languages():
    assert list(languages.LANGUAGES) == ["auto", "en", "fr", "ar", "es", "de", "it", "pt", "tr", "zh"]
    assert languages.LANGUAGES["auto"] == "Auto"


def test_normalize_accepts_codes_and_refuses_the_rest():
    assert languages.normalize(None) == "auto" and languages.normalize("") == "auto"
    assert languages.normalize(" FR ") == "fr"
    for bad in ("klingon", "french", "fr-FR", "xx", "../etc"):
        with pytest.raises(ValueError):
            languages.normalize(bad)
    assert languages.lenient("klingon") == "auto" and languages.name("fr") == "French"


# ----------------------------------------------------------- transcription

@pytest.fixture
def chunk(tmp_path):
    p = tmp_path / "c.mp3"
    p.write_bytes(b"fake audio bytes")
    return p


def send(chunk, **kw):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": "bonjour"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert transcribe.transcribe_file(chunk, client=client, **kw) == "bonjour"
    return seen["body"]


def test_whisper_gets_the_language_only_when_one_is_chosen(chunk):
    assert b'name="language"' not in send(chunk, language="auto")
    body = send(chunk, language="fr")
    assert b'name="language"' in body and b"\r\n\r\nfr\r\n" in body
    assert b'name="language"' not in send(chunk)  # nothing set anywhere: Whisper detects the language


def test_server_default_language_applies_only_when_nothing_was_chosen(chunk, monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_LANGUAGE", "de")
    assert b"\r\n\r\nde\r\n" in send(chunk)
    assert b'name="language"' not in send(chunk, language="auto")


def test_every_chunk_is_sent_with_the_language(chunk, monkeypatch):
    seen = []

    def fake(path, client=None, retries=3, language=None):
        seen.append(language)
        return "a b c"

    monkeypatch.setattr(transcribe, "transcribe_file", fake)
    transcribe.transcribe_chunks([chunk, chunk], language="es")
    assert seen == ["es", "es"]


# --------------------------------------------------------------------- API

@needs_ffmpeg
def test_language_defaults_to_auto(client, make_audio, transcribed, monkeypatch):
    r = upload(client, make_audio(2))
    assert r.status_code == 202
    job = wait_done(client, r.json()["id"])
    assert job["status"] == "done"
    assert job["language"] == "auto" and job["language_name"] == "Auto"
    assert transcribed == ["auto"]  # the transcription step leaves the parameter out for "auto"


@needs_ffmpeg
def test_chosen_language_is_passed_to_transcription_and_stored(client, make_audio, transcribed):
    r = upload(client, make_audio(2), language="fr")
    assert r.status_code == 202
    job = wait_done(client, r.json()["id"])
    assert transcribed == ["fr"]
    assert job["language"] == "fr" and job["language_name"] == "French"
    saved = json.loads((jobs._job_dir(job["id"]) / "job.json").read_text(encoding="utf-8"))
    assert saved["language"] == "fr"
    jobs.load_all()  # and it survives a restart
    assert client.get(f"/api/notes/{job['id']}").json()["language"] == "fr"


@needs_ffmpeg
def test_language_is_case_insensitive_and_empty_means_auto(client, make_audio, transcribed):
    assert wait_done(client, upload(client, make_audio(2), language="AR").json()["id"])["language"] == "ar"
    assert wait_done(client, upload(client, make_audio(2), language="").json()["id"])["language"] == "auto"


@needs_ffmpeg
def test_invalid_language_is_refused_and_nothing_is_stored(client, make_audio, transcribed):
    r = upload(client, make_audio(2), language="klingon")
    assert r.status_code == 400
    assert "language" in r.json()["error"] and "auto, en, fr, ar, es, de, it, pt, tr, zh" in r.json()["error"]
    assert client.get("/api/notes").json() == [] and transcribed == []


def test_invalid_language_is_refused_before_anything_else(client, tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"data")
    assert upload(client, f, language="xx").status_code == 400


def test_old_notes_without_a_language_read_as_auto(client):
    job = jobs.new_job("old.wav")
    jobs._jobs[job["id"]].pop("language", None)
    body = client.get(f"/api/notes/{job['id']}").json()
    assert body["language"] == "auto" and body["language_name"] == "Auto"


def test_health_lists_the_languages(client):
    h = client.get("/api/health").json()
    assert [x["code"] for x in h["languages"]] == list(languages.LANGUAGES)
    assert h["groq_chat"]["enabled"] is False  # switched off in the tests


# ------------------------------------------------- structuring from the API

@needs_ffmpeg
def test_groq_notes_in_the_chosen_language_and_a_rebuild(client, make_audio, transcribed, monkeypatch):
    net = FakeNet(monkeypatch)
    h = client.get("/api/health").json()
    assert h["groq_chat"]["enabled"] is True and h["structuring"]["engine"] == "groq:llama-3.3-70b-versatile"
    assert "test-key" not in str(h)

    job = wait_done(client, upload(client, make_audio(2), language="fr").json()["id"])
    assert job["status"] == "done"
    assert job["engine"] == "groq:llama-3.3-70b-versatile" and job["engine_detail"] is None
    assert job["notes"]["title"] == FR_NOTES["title"]
    assert "Write all the notes in French" in net.chat_calls[0]["messages"][0]["content"]

    assert client.post(f"/api/notes/{job['id']}/restructure").status_code == 202
    again = wait_done(client, job["id"])
    assert again["status"] == "done" and again["engine"].startswith("groq:") and again["language"] == "fr"
    assert len(net.chat_calls) == 2
    assert "Write all the notes in French" in net.chat_calls[1]["messages"][0]["content"]


@needs_ffmpeg
def test_when_groq_fails_the_note_is_still_made_and_says_why(client, make_audio, monkeypatch):
    monkeypatch.setattr(transcribe, "transcribe_file", lambda *a, **k: SAMPLE_TEXT)
    FakeNet(monkeypatch, chat=status(500))
    job = wait_done(client, upload(client, make_audio(2)).json()["id"])
    assert job["status"] == "done" and job["engine"] == "rule-based"
    assert "Groq returned an error (500)" in job["engine_detail"]
    assert job["notes"]["sections"]


@needs_ffmpeg
def test_a_rebuild_falls_back_to_the_rules_when_groq_fails(client, make_audio, transcribed, monkeypatch):
    net = FakeNet(monkeypatch)
    job = wait_done(client, upload(client, make_audio(2), language="fr").json()["id"])
    net.chat = httpx.ConnectError("down")
    assert client.post(f"/api/notes/{job['id']}/restructure").status_code == 202
    again = wait_done(client, job["id"])
    assert again["status"] == "done" and again["engine"] == "rule-based" and again["notes"]["sections"]


def test_structure_receives_the_language(monkeypatch):
    seen = {}

    def fake(text, on_progress=None, language="auto", report=None):
        seen["language"] = language
        return structure.structure_rules(text), "rule-based"

    monkeypatch.setattr(structure, "structure_transcript", fake)
    job = jobs.new_job("x.wav", "tr")
    jobs._update(job["id"], transcript=SAMPLE_TEXT, status="processing")
    jobs.run(job["id"])
    assert seen["language"] == "tr" and jobs.get(job["id"])["status"] == "done"


# ------------------------------------------------ codes for Whisper, wording, guessing

def test_chinese_and_arabic_reach_whisper_as_zh_and_ar(chunk):
    for code in ("zh", "ar"):
        assert f"\r\n\r\n{code}\r\n".encode() in send(chunk, language=code)
    assert b"\r\n\r\nzh\r\n" in send(chunk, language=" ZH ")  # tidied, never sent as typed


def test_an_unknown_language_is_never_sent_to_whisper(chunk):
    assert b'name="language"' not in send(chunk, language="klingon")
    assert b'name="language"' not in send(chunk, language="zh-CN")


def test_detect_uses_the_chosen_language_first():
    assert languages.detect("The team will meet and we have a plan for this", "fr") == "fr"


def test_detect_guesses_from_script_and_common_words():
    assert languages.detect("The team will meet on Monday and we have a plan for this project.") == "en"
    assert languages.detect("Nous avons une r\u00e9union pour le projet et il faut que tout soit pr\u00eat dans la semaine.") == "fr"
    assert languages.detect("El equipo tiene una reuni\u00f3n para el proyecto pero tambi\u00e9n hay mucho por hacer.") == "es"
    assert languages.detect("Wir haben ein Treffen und es ist nicht so einfach mit der Planung f\u00fcr das Projekt.") == "de"
    assert languages.detect("\u0627\u0644\u0641\u0631\u064a\u0642 \u0633\u064a\u062c\u062a\u0645\u0639 \u064a\u0648\u0645 \u0627\u0644\u0627\u062b\u0646\u064a\u0646 \u0644\u0645\u0646\u0627\u0642\u0634\u0629 \u0627\u0644\u0645\u0634\u0631\u0648\u0639") == "ar"
    assert languages.detect("\u56e2\u961f\u5c06\u5728\u661f\u671f\u4e00\u5f00\u4f1a\u8ba8\u8bba\u65b0\u7684\u7f51\u7ad9\u53d1\u5e03\u8ba1\u5212") == "zh"


def test_detect_says_none_when_unsure():
    assert languages.detect("") is None and languages.detect("ok") is None
    assert languages.detect("Plan 12 34 56 78 90 A B C D E F G") is None


def test_wording_exists_for_every_language_and_falls_back_to_english():
    keys = set(languages._EN)
    for code in languages.NATIVE_NAMES:
        assert keys <= set(languages._STRINGS[code]), code
    assert languages.text("summary", "fr") == "R\u00e9sum\u00e9"
    assert languages.text("summary", "xx") == "Summary" and languages.text("summary", None) == "Summary"


def test_dates_and_durations_are_written_for_the_reader():
    import datetime

    d = datetime.datetime(2026, 3, 5, 10, 0)
    assert languages.format_date(d, "en") == "5 March 2026"
    assert languages.format_date(d, "fr") == "5 mars 2026"
    assert languages.format_date(d, "es") == "5 de marzo de 2026"
    assert languages.format_date(d, "de") == "5. M\u00e4rz 2026"
    assert languages.format_date(d, "zh") == "2026\u5e743\u67085\u65e5"
    assert languages.format_date(d, "ar") == "2026-03-05"
    assert languages.format_duration(45) == "45 s" and languages.format_duration(754) == "13 min"
    assert languages.format_duration(3900) == "1 h 05 min"
    assert languages.format_duration(None) == "" and languages.format_duration(0) == ""
