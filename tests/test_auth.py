"""The owner login: password hash, session cookie, logout, protected routes, rate limit, and the Groq key."""
import io
import re

import pytest
from fastapi.testclient import TestClient

from app import auth, jobs, structure
from app.main import app

from conftest import SAMPLE_TEXT

PASSWORD = "correct horse battery"
HASH = auth.hash_password(PASSWORD)
KEY = "test-key-not-real"


@pytest.fixture
def secured(monkeypatch):
    """The login is on and the owner password is PASSWORD. Other tests run with the login switched off."""
    monkeypatch.setenv("VOICE_NOTES_AUTH", "on")
    monkeypatch.setenv("VOICE_NOTES_PASSWORD_HASH", HASH)
    monkeypatch.delenv("VOICE_NOTES_TRUST_PROXY", raising=False)
    monkeypatch.delenv("VOICE_NOTES_LOGIN_MAX_FAILS", raising=False)
    auth.reset_state()
    yield
    auth.reset_state()


@pytest.fixture
def client(secured):
    with TestClient(app, follow_redirects=False) as c:
        yield c


def sign_in(c, password=PASSWORD, **kw):
    return c.post("/login", data={"password": password}, **kw)


def make_done():
    job = jobs.new_job("memo.wav")
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    jobs._update(job["id"], status="done", stage="done", progress=1.0, detail="Done", transcript=SAMPLE_TEXT,
                 notes=notes, engine=engine, seen=True)
    return job["id"]


# ------------------------------------------------------------------ the password hash

def test_hash_is_salted_and_never_contains_the_password():
    a, b = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
    assert a != b  # a new salt each time
    assert PASSWORD not in a and a.startswith("scrypt:")
    assert "$" not in a  # safe in shell and unit files
    assert auth.verify_password(PASSWORD, a) and auth.verify_password(PASSWORD, b)
    assert not auth.verify_password("wrong", a)
    assert not auth.verify_password(PASSWORD, "")
    assert not auth.verify_password(PASSWORD, "not a hash")
    assert not auth.verify_password(PASSWORD, "scrypt:x:y:z:q:w")


def test_the_command_prints_a_hash_from_standard_input(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough password\n"))
    assert auth.main() == 0
    out = capsys.readouterr().out.strip()
    assert auth.valid_hash_format(out) and auth.verify_password("a long enough password", out)
    assert "a long enough password" not in out


def test_the_command_refuses_a_short_password(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert auth.main() == 1
    assert capsys.readouterr().out == ""


def test_the_repository_holds_no_plain_password_setting():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "app" / "config.py").read_text() + (root / "app" / "auth.py").read_text()
    assert "VOICE_NOTES_PASSWORD\"" not in text and "VOICE_NOTES_PASSWORD'" not in text


# ------------------------------------------------------------------ access without a session

PROTECTED = [
    ("GET", "/api/health"), ("GET", "/api/notes"), ("GET", "/api/notes?q=x"), ("GET", "/api/tasks"),
    ("GET", "/api/export/all"), ("POST", "/api/notes"), ("PATCH", "/api/notes/abc"),
    ("PUT", "/api/notes/abc/notes"), ("PUT", "/api/notes/abc/pin"), ("POST", "/api/notes/abc/retry"),
    ("POST", "/api/notes/abc/restructure"), ("POST", "/api/notes/abc/seen"), ("DELETE", "/api/notes/abc"),
    ("GET", "/api/anything"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_api_routes_need_a_session(client, method, path):
    r = client.request(method, path)
    assert r.status_code == 401, (method, path, r.status_code)
    assert r.json() == {"error": "Sign in required."}


@pytest.mark.parametrize("path", ["/", "/index.html", "/tasks", "/data/x", "/%2e%2e/etc/passwd"])
def test_pages_send_a_visitor_to_the_login(client, path):
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_a_made_up_or_empty_cookie_is_not_a_session(client):
    client.cookies.set(auth.COOKIE_NAME, "made-up")
    assert client.get("/api/notes").status_code == 401
    client.cookies.set(auth.COOKIE_NAME, "")
    assert client.get("/api/notes").status_code == 401


def test_the_public_pages_work_without_a_session(client):
    nid = make_done()
    assert client.get(f"/api/notes/{nid}").status_code == 403  # a new note is private
    jobs.set_share(nid, "public")  # the share reads below are for a public note
    assert client.get("/login").status_code == 200
    for asset in ("/app.js", "/style.css", "/favicon.svg", "/fonts/InstrumentSans-latin.woff2"):
        assert client.get(asset).status_code == 200, asset
    share = client.get(f"/n/{nid}")
    assert share.status_code == 200 and "noindex" in share.headers["x-robots-tag"]
    # the two reads of the share view
    assert client.get(f"/api/notes/{nid}").status_code == 200
    assert client.get(f"/api/notes/{nid}/export?format=md").status_code == 200
    assert client.get("/api/notes/doesnotexist").status_code == 404
    # but the list, the edits and the opened mark stay private
    assert client.get("/api/notes").status_code == 401
    assert client.post(f"/api/notes/{nid}/seen").status_code == 401
    assert client.delete(f"/api/notes/{nid}").status_code == 401
    assert client.patch(f"/api/notes/{nid}", json={"title": "x"}).status_code == 401


def test_the_login_page_has_security_headers_and_is_not_cached(client):
    r = client.get("/login")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert 'type="password"' in r.text and 'action="/login"' in r.text
    assert "<script" not in r.text  # the strict script policy needs none


def test_a_blocked_request_still_carries_the_security_headers(client):
    r = client.get("/api/notes")
    assert r.headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------------ login

def test_the_right_password_opens_a_session(client):
    r = sign_in(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert auth.COOKIE_NAME in client.cookies
    assert client.get("/api/notes").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/api/health").status_code == 200
    # a signed in owner who opens the login page goes to the start page
    assert client.get("/login").status_code == 303


def test_the_session_cookie_is_http_only_same_site_lax_and_not_secure_over_http(client):
    cookie = sign_in(client).headers["set-cookie"]
    assert cookie.startswith(auth.COOKIE_NAME + "=")
    low = cookie.lower()
    assert "httponly" in low and "samesite=lax" in low and "path=/" in low and "max-age=" in low
    assert "; secure" not in low
    # the token is long and random, and is not the password
    token = re.match(rf"{auth.COOKIE_NAME}=([^;]+)", cookie).group(1)
    assert len(token) >= 32 and PASSWORD not in token


def test_the_cookie_is_secure_over_https(secured):
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        assert "; secure" in sign_in(c).headers["set-cookie"].lower()
        assert c.get("/api/notes").status_code == 200


def test_the_cookie_is_secure_behind_a_trusted_proxy_that_says_https(secured, monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_TRUST_PROXY", "1")
    with TestClient(app, follow_redirects=False) as c:
        r = sign_in(c, headers={"X-Forwarded-Proto": "https"})
        assert "; secure" in r.headers["set-cookie"].lower()
    monkeypatch.delenv("VOICE_NOTES_TRUST_PROXY")
    with TestClient(app, follow_redirects=False) as c:  # the header means nothing when no proxy is trusted
        r = sign_in(c, headers={"X-Forwarded-Proto": "https"})
        assert "; secure" not in r.headers["set-cookie"].lower()


def test_a_wrong_password_gives_no_session(client):
    r = sign_in(client, "not the password")
    assert r.status_code == 401 and "Wrong password" in r.text
    assert "set-cookie" not in r.headers and auth.COOKIE_NAME not in client.cookies
    assert client.get("/api/notes").status_code == 401
    assert sign_in(client, "").status_code == 401
    assert client.post("/login").status_code == 401  # no field at all
    assert sign_in(client, "x" * 5000).status_code == 401


def test_the_error_message_does_not_repeat_what_was_typed(client):
    r = sign_in(client, "<script>alert(1)</script>")
    assert "<script>alert" not in r.text


def test_the_session_ends_after_its_lifetime(client):
    sign_in(client)
    assert client.get("/api/notes").status_code == 200
    for key in list(auth._sessions):
        auth._sessions[key] = 1.0  # long ago
    assert client.get("/api/notes").status_code == 401


# ------------------------------------------------------------------ logout

def test_logout_ends_the_session_on_the_server(client):
    sign_in(client)
    token = client.cookies.get(auth.COOKIE_NAME)
    assert client.get("/api/notes").status_code == 200
    r = client.post("/api/logout")
    assert r.status_code == 204
    assert auth.COOKIE_NAME in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert client.get("/api/notes").status_code == 401
    # a copy of the old cookie is worth nothing either
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, token)
    assert client.get("/api/notes").status_code == 401


def test_logout_without_a_session_is_harmless(client):
    assert client.post("/api/logout").status_code == 204


def test_sign_in_again_after_logout(client):
    sign_in(client)
    client.post("/api/logout")
    assert sign_in(client).status_code == 303
    assert client.get("/api/notes").status_code == 200


# ------------------------------------------------------------------ rate limit

def test_failed_logins_are_limited_and_the_right_password_waits_too(client):
    for _ in range(5):
        assert sign_in(client, "nope").status_code == 401
    r = sign_in(client, "nope")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    blocked = sign_in(client)  # the right password does not help during the lock
    assert blocked.status_code == 429 and "set-cookie" not in blocked.headers
    assert client.get("/api/notes").status_code == 401


def test_a_success_clears_the_count(client):
    for _ in range(4):
        sign_in(client, "nope")
    assert sign_in(client).status_code == 303
    client.post("/api/logout")
    for _ in range(4):
        assert sign_in(client, "nope").status_code == 401
    assert sign_in(client).status_code == 303


def test_the_limit_can_be_set(client, monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_LOGIN_MAX_FAILS", "2")
    assert sign_in(client, "a").status_code == 401
    assert sign_in(client, "a").status_code == 401
    assert sign_in(client, "a").status_code == 429


def test_the_lock_ends_when_the_window_has_passed(client):
    for _ in range(5):
        sign_in(client, "nope")
    assert sign_in(client).status_code == 429
    for times in auth._failures.values():
        times[:] = [t - 100000 for t in times]
    auth._global_failures[:] = [t - 100000 for t in auth._global_failures]
    assert sign_in(client).status_code == 303


def test_a_forged_forwarded_address_does_not_get_around_the_limit(client):
    for i in range(5):
        sign_in(client, "nope", headers={"X-Forwarded-For": f"10.0.0.{i}"})
    assert sign_in(client, headers={"X-Forwarded-For": "10.9.9.9"}).status_code == 429


def test_with_a_trusted_proxy_each_address_has_its_own_count(secured, monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_TRUST_PROXY", "1")
    with TestClient(app, follow_redirects=False) as c:
        for _ in range(5):
            sign_in(c, "nope", headers={"X-Forwarded-For": "203.0.113.5"})
        assert sign_in(c, headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 429
        assert sign_in(c, headers={"X-Forwarded-For": "203.0.113.6"}).status_code == 303


def test_many_addresses_together_are_limited_too(client, monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_TRUST_PROXY", "1")
    for i in range(50):
        sign_in(client, "nope", headers={"X-Forwarded-For": f"198.51.100.{i}"})
    assert sign_in(client, headers={"X-Forwarded-For": "198.51.100.200"}).status_code == 429


# ------------------------------------------------------------------ no password configured

@pytest.mark.parametrize("value", [None, "", "plainpassword", "scrypt:broken"])
def test_without_a_valid_hash_nobody_can_sign_in(monkeypatch, value):
    monkeypatch.setenv("VOICE_NOTES_AUTH", "on")
    if value is None:
        monkeypatch.delenv("VOICE_NOTES_PASSWORD_HASH", raising=False)
    else:
        monkeypatch.setenv("VOICE_NOTES_PASSWORD_HASH", value)
    auth.reset_state()
    with TestClient(app, follow_redirects=False) as c:
        page = c.get("/login")
        assert page.status_code == 503 and "VOICE_NOTES_PASSWORD_HASH" in page.text
        assert sign_in(c, value or "").status_code == 503
        assert "set-cookie" not in sign_in(c, "plainpassword").headers
        assert c.get("/api/notes").status_code == 401
        assert c.get("/").status_code == 303


def test_a_session_does_not_survive_a_missing_hash(monkeypatch, secured):
    with TestClient(app, follow_redirects=False) as c:
        sign_in(c)
        assert c.get("/api/notes").status_code == 200
        monkeypatch.delenv("VOICE_NOTES_PASSWORD_HASH")
        assert c.get("/api/notes").status_code == 401


# ------------------------------------------------------------------ the Groq key

def test_the_groq_key_is_never_returned(client, monkeypatch):
    monkeypatch.setenv("USE_GROQ_CHAT", "1")  # the chat engine reads the key; its status must not show it
    structure.reset_groq_cache()
    nid = make_done()
    sign_in(client)
    urls = [
        "/api/health", "/api/notes", f"/api/notes/{nid}", "/api/tasks", "/api/export/all?format=md",
        f"/api/notes/{nid}/export?format=md", f"/api/notes/{nid}/export?format=txt",
        f"/api/notes/{nid}/export?format=pdf&transcript=true", "/api/notes/doesnotexist", "/api/notes?q=a",
        "/login", "/", "/app.js", "/style.css", "/n/" + nid, "/api/nothing-here",
    ]
    for url in urls:
        r = client.get(url)
        assert KEY.encode() not in r.content, url
        assert KEY.encode() not in str(r.headers).encode(), url
    health = client.get("/api/health").json()
    assert health["groq_key"] is True  # only whether a key exists
    assert KEY not in str(health)
    # a failed upload and a refused request do not echo it either
    for r in (client.post("/api/notes", files={"file": ("a.wav", b"", "audio/wav")}),
              client.post("/api/notes", files={"file": ("a.wav", b"x", "audio/wav")}, data={"language": "xx"}),
              client.post(f"/api/notes/{nid}/retry"), client.put(f"/api/notes/{nid}/pin", json={})):
        assert KEY.encode() not in r.content
    # and the same without a session
    client.post("/api/logout")
    for url in urls:
        assert KEY.encode() not in client.get(url).content, url


def test_the_key_does_not_appear_in_the_source_files():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in list((root / "app").rglob("*.py")) + list((root / "app" / "static").glob("*.*s")):
        assert KEY not in path.read_text(encoding="utf-8", errors="ignore"), path
