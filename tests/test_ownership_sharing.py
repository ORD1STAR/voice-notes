"""Step 2 of the security work: notes belong to the owner, and each note is private or public when it is shared."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, jobs, structure, transcribe
from app.main import app

from conftest import SAMPLE_TEXT, needs_ffmpeg

PASSWORD = "correct horse battery"
HASH = auth.hash_password(PASSWORD)


@pytest.fixture
def secured(monkeypatch):
    """The login is on; the owner password is PASSWORD."""
    monkeypatch.setenv("VOICE_NOTES_AUTH", "on")
    monkeypatch.setenv("VOICE_NOTES_PASSWORD_HASH", HASH)
    monkeypatch.delenv("VOICE_NOTES_TRUST_PROXY", raising=False)
    monkeypatch.delenv("VOICE_NOTES_LOGIN_MAX_FAILS", raising=False)
    auth.reset_state()
    yield
    auth.reset_state()


@pytest.fixture
def visitor(secured):
    """A client with no session."""
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def owner(secured, visitor):
    """A second client that has signed in. The visitor keeps having no cookie."""
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/login", data={"password": PASSWORD}).status_code == 303
        yield c


def make_done(owner_name: str = jobs.OWNER, name: str = "memo.wav") -> str:
    job = jobs.new_job(name, "auto", owner_name)
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    jobs._update(job["id"], status="done", stage="done", progress=1.0, detail="Done", transcript=SAMPLE_TEXT,
                 notes=notes, engine=engine, seen=True)
    return job["id"]


def saved(nid: str) -> dict:
    return json.loads((jobs._job_dir(nid) / "job.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ (a) the owner field

def test_new_notes_carry_the_owner_and_are_private():
    job = jobs.new_job("a.wav")
    assert job["owner"] == jobs.OWNER and job["share"] == "private" and job["share_token"] is None
    assert saved(job["id"])["owner"] == jobs.OWNER and saved(job["id"])["share"] == "private"


@needs_ffmpeg
def test_an_uploaded_note_gets_the_owner_and_is_private(owner, make_audio, monkeypatch):
    monkeypatch.setattr(transcribe, "transcribe_file", lambda path, client=None, retries=3, language=None: SAMPLE_TEXT)
    with open(make_audio(1), "rb") as fh:
        nid = owner.post("/api/notes", files={"file": ("a.wav", fh, "audio/wav")}).json()["id"]
    assert jobs.get(nid)["owner"] == jobs.OWNER and jobs.get(nid)["share"] == "private"
    end = time.time() + 30
    while jobs.get(nid)["status"] != "done" and time.time() < end:
        time.sleep(0.05)
    body = owner.get(f"/api/notes/{nid}").json()
    assert body["owner"] == jobs.OWNER and body["share"] == "private"


def test_old_notes_default_to_the_owner_and_private_without_rewriting_the_file(owner, visitor):
    nid = make_done()
    path = jobs._job_dir(nid) / "job.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("owner", "share", "share_token"):
        data.pop(key, None)
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    jobs.load_all()  # the server starts and finds a note from before the fields existed
    assert path.read_text(encoding="utf-8") == before  # read does not rewrite the file
    got = jobs.get(nid)
    assert got["owner"] == jobs.OWNER and got["share"] == "private"
    assert [r["id"] for r in owner.get("/api/notes").json()] == [nid]
    assert owner.get(f"/api/notes/{nid}").json()["share"] == "private"
    # and the old note is not readable by anyone but the owner
    r = visitor.get(f"/api/notes/{nid}")
    assert r.status_code == 403 and r.json()["private"] is True
    assert visitor.get(f"/api/notes/{nid}/export?format=md").status_code == 403
    assert path.read_text(encoding="utf-8") == before


def test_an_unknown_share_value_in_a_file_counts_as_private(owner, visitor):
    nid = make_done()
    path = jobs._job_dir(nid) / "job.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["share"] = "everyone"
    path.write_text(json.dumps(data), encoding="utf-8")
    jobs.load_all()
    assert jobs.get(nid)["share"] == "private"
    assert visitor.get(f"/api/notes/{nid}").status_code == 403


# ------------------------------------------------------------------ the list and its content need the session

def test_the_list_and_its_content_are_not_reachable_without_the_session(visitor):
    nid = make_done()
    for url in ("/api/notes", "/api/notes?q=landing", "/api/tasks", "/api/export/all?format=md"):
        r = visitor.get(url)
        assert r.status_code == 401 and r.json() == {"error": "Sign in required."}, url
    # the content of a note, in each way to ask for it
    for url in (f"/api/notes/{nid}", f"/api/notes/{nid}/export?format=md", f"/api/notes/{nid}/export?format=txt",
                f"/api/notes/{nid}/export?format=pdf&transcript=true"):
        r = visitor.get(url)
        assert r.status_code == 403, url
        assert "landing" not in r.text.lower() and "website" not in r.text.lower(), url
    # the edits
    assert visitor.put(f"/api/notes/{nid}/notes", json={"title": "x", "sections": []}).status_code == 401
    assert visitor.patch(f"/api/notes/{nid}", json={"title": "x"}).status_code == 401
    assert visitor.put(f"/api/notes/{nid}/pin", json={"pinned": True}).status_code == 401
    assert visitor.delete(f"/api/notes/{nid}").status_code == 401
    assert jobs.get(nid)["notes"]["title"] != "x"


def test_the_owner_sees_the_list_search_export_and_edit(owner):
    nid = make_done()
    assert [r["id"] for r in owner.get("/api/notes").json()] == [nid]
    assert [r["id"] for r in owner.get("/api/notes", params={"q": "landing"}).json()] == [nid]
    assert owner.get("/api/tasks").status_code == 200
    assert owner.get("/api/export/all?format=md").status_code == 200
    assert owner.get(f"/api/notes/{nid}/export?format=md").status_code == 200
    body = {"title": "Edited title", "summary": "s", "sections": [{"heading": "h", "bullets": ["b"]}]}
    assert owner.put(f"/api/notes/{nid}/notes", json=body).status_code == 200
    assert owner.get(f"/api/notes/{nid}").json()["notes"]["title"] == "Edited title"


def test_notes_of_another_account_are_invisible_and_cannot_be_changed(owner):
    mine = make_done()
    theirs = make_done("someone-else", "theirs.wav")
    assert [r["id"] for r in owner.get("/api/notes").json()] == [mine]
    assert owner.get("/api/notes", params={"q": "landing"}).json()[0]["id"] == mine
    assert len(owner.get("/api/notes", params={"q": "landing"}).json()) == 1
    assert [t["id"] for t in owner.get("/api/tasks").json()] == [mine]
    zip_bytes = owner.get("/api/export/all?format=md").content
    import io, zipfile
    assert len(zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()) == 1
    # a private note of another account: refused for reading, absent for every change
    assert owner.get(f"/api/notes/{theirs}").status_code == 403
    assert owner.get(f"/api/notes/{theirs}/export?format=md").status_code == 403
    body = {"title": "x", "sections": [{"heading": "h", "bullets": ["b"]}]}
    assert owner.put(f"/api/notes/{theirs}/notes", json=body).status_code == 404
    assert owner.patch(f"/api/notes/{theirs}", json={"title": "x"}).status_code == 404
    assert owner.put(f"/api/notes/{theirs}/pin", json={"pinned": True}).status_code == 404
    assert owner.put(f"/api/notes/{theirs}/share", json={"mode": "public"}).status_code == 404
    assert owner.post(f"/api/notes/{theirs}/share/token").status_code == 404
    assert owner.post(f"/api/notes/{theirs}/seen").status_code == 404
    assert owner.post(f"/api/notes/{theirs}/restructure").status_code == 404
    assert owner.post(f"/api/notes/{theirs}/retry").status_code == 404
    assert owner.delete(f"/api/notes/{theirs}").status_code == 404
    assert jobs.get(theirs) is not None and jobs.get(theirs)["share"] == "private"


# ------------------------------------------------------------------ (b) share modes

def test_a_private_share_is_refused_without_the_session_and_opens_for_the_owner(visitor, owner):
    nid = make_done()
    page = visitor.get(f"/n/{nid}")  # the page itself is only the empty shell
    assert page.status_code == 200 and "Website launch" not in page.text
    r = visitor.get(f"/api/notes/{nid}")
    assert r.status_code == 403
    assert r.json()["private"] is True and "private" in r.json()["error"].lower()
    assert r.headers["cache-control"] == "no-store"
    assert visitor.get(f"/api/notes/{nid}/export?format=md").status_code == 403
    assert owner.get(f"/api/notes/{nid}").status_code == 200
    assert owner.get(f"/api/notes/{nid}/export?format=md").status_code == 200


def test_a_public_share_works_without_the_session(visitor, owner):
    nid = make_done()
    assert owner.put(f"/api/notes/{nid}/share", json={"mode": "public"}).status_code == 200
    r = visitor.get(f"/api/notes/{nid}")
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["notes"]["title"]
    assert r.json()["share"] == "public"
    assert "owner" not in r.json() and "share_token" not in r.json()
    md = visitor.get(f"/api/notes/{nid}/export?format=md")
    assert md.status_code == 200 and md.content
    assert visitor.get(f"/api/notes/{nid}/export?format=pdf").status_code in (200, 422)
    # being public does not open anything else
    assert visitor.get("/api/notes").status_code == 401
    assert visitor.get("/api/tasks").status_code == 401
    assert visitor.get("/api/export/all").status_code == 401
    assert visitor.put(f"/api/notes/{nid}/notes", json={"title": "x", "sections": []}).status_code == 401
    assert visitor.put(f"/api/notes/{nid}/share", json={"mode": "private"}).status_code == 401
    assert visitor.delete(f"/api/notes/{nid}").status_code == 401


def test_the_mode_switch_works_both_ways_and_is_saved(owner, visitor):
    nid = make_done()
    assert owner.get(f"/api/notes/{nid}").json()["share"] == "private"
    assert owner.get("/api/notes").json()[0]["share"] == "private"
    r = owner.put(f"/api/notes/{nid}/share", json={"mode": "public"})
    assert r.status_code == 200 and r.json()["share"] == "public"
    assert saved(nid)["share"] == "public"
    assert owner.get("/api/notes").json()[0]["share"] == "public"
    assert visitor.get(f"/api/notes/{nid}").status_code == 200
    jobs.load_all()  # a restart keeps the mode
    assert owner.get(f"/api/notes/{nid}").json()["share"] == "public"
    r = owner.put(f"/api/notes/{nid}/share", json={"mode": "private"})
    assert r.status_code == 200 and r.json()["share"] == "private"
    assert visitor.get(f"/api/notes/{nid}").status_code == 403


def test_the_mode_switch_checks_its_input_and_needs_the_session(owner, visitor):
    nid = make_done()
    for bad in ({"mode": "everyone"}, {"mode": ""}, {"mode": None}, {}, {"mode": "PUBLIC"}):
        assert owner.put(f"/api/notes/{nid}/share", json=bad).status_code in (400, 422), bad
    assert jobs.get(nid)["share"] == "private"
    assert owner.put("/api/notes/doesnotexist/share", json={"mode": "public"}).status_code == 404
    assert visitor.put(f"/api/notes/{nid}/share", json={"mode": "public"}).status_code == 401
    assert jobs.get(nid)["share"] == "private"


def test_a_signed_out_owner_loses_access_to_a_private_note(owner):
    nid = make_done()
    assert owner.get(f"/api/notes/{nid}").status_code == 200
    owner.post("/api/logout")
    assert owner.get(f"/api/notes/{nid}").status_code == 403


def test_an_unknown_note_is_still_not_found(visitor):
    assert visitor.get("/api/notes/doesnotexist").status_code == 404


# ------------------------------------------------------------------ (c) the secret in a private link

def test_a_private_link_secret_opens_one_note_without_a_login(owner, visitor):
    nid = make_done()
    other = make_done(name="other.wav")
    r = owner.post(f"/api/notes/{nid}/share/token")
    assert r.status_code == 200
    token = r.json()["share_token"]
    assert len(token) >= 16 and r.json()["share"] == "private"
    assert owner.get(f"/api/notes/{nid}").json()["share_token"] == token
    assert saved(nid)["share_token"] == token
    # with the secret: read and export of that note
    ok = visitor.get(f"/api/notes/{nid}", params={"t": token})
    assert ok.status_code == 200 and "share_token" not in ok.json() and "owner" not in ok.json()
    assert visitor.get(f"/api/notes/{nid}/export?format=md", params={"t": token}).status_code == 200
    # without it, with a wrong one, with it on another note, or for the list: refused
    assert visitor.get(f"/api/notes/{nid}").status_code == 403
    assert visitor.get(f"/api/notes/{nid}", params={"t": "wrong"}).status_code == 403
    assert visitor.get(f"/api/notes/{nid}", params={"t": ""}).status_code == 403
    assert visitor.get(f"/api/notes/{other}", params={"t": token}).status_code == 403
    assert visitor.get("/api/notes", params={"t": token}).status_code == 401
    assert visitor.put(f"/api/notes/{nid}/notes", params={"t": token}, json={"title": "x", "sections": []}).status_code == 401
    assert visitor.delete(f"/api/notes/{nid}", params={"t": token}).status_code == 401
    # the secret is not in the list rows
    assert token not in owner.get("/api/notes").text
    # a new secret replaces the old one
    new = owner.post(f"/api/notes/{nid}/share/token").json()["share_token"]
    assert new != token
    assert visitor.get(f"/api/notes/{nid}", params={"t": token}).status_code == 403
    assert visitor.get(f"/api/notes/{nid}", params={"t": new}).status_code == 200
    # removing it closes the link
    gone = owner.delete(f"/api/notes/{nid}/share/token")
    assert gone.status_code == 200 and gone.json()["share_token"] is None
    assert visitor.get(f"/api/notes/{nid}", params={"t": new}).status_code == 403


def test_the_secret_endpoints_need_the_session(visitor):
    nid = make_done()
    assert visitor.post(f"/api/notes/{nid}/share/token").status_code == 401
    assert visitor.delete(f"/api/notes/{nid}/share/token").status_code == 401
    assert jobs.get(nid)["share_token"] is None


def test_the_secret_never_goes_to_a_visitor_of_a_public_note(owner, visitor):
    nid = make_done()
    token = owner.post(f"/api/notes/{nid}/share/token").json()["share_token"]
    owner.put(f"/api/notes/{nid}/share", json={"mode": "public"})
    body = visitor.get(f"/api/notes/{nid}")
    assert body.status_code == 200 and token not in body.text
