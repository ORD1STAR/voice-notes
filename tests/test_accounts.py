"""Email accounts: sign up, log in, the owner migration, the limits, and password recovery by a one-time token."""
import json
import logging
import re
import smtplib

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, jobs, structure
from app.config import settings
from app.main import app

from conftest import SAMPLE_TEXT

OWNER_EMAIL = "Owner@Example.com"
OWNER_PASSWORD = "the owner password"
USER_PASSWORD = "a long user password"
NEW_PASSWORD = "brand new password 1"
SMTP_SECRET = "smtp-secret-value"


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_AUTH", "on")
    monkeypatch.setenv("VOICE_NOTES_PASSWORD_HASH", auth.hash_password(OWNER_PASSWORD))
    monkeypatch.setenv("VOICE_NOTES_OWNER_EMAIL", OWNER_EMAIL)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "mailer@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", SMTP_SECRET)
    monkeypatch.delenv("VOICE_NOTES_TRUST_PROXY", raising=False)
    monkeypatch.delenv("VOICE_NOTES_LOGIN_MAX_FAILS", raising=False)
    auth.reset_state()
    yield
    auth.reset_state()


@pytest.fixture
def client(secured):
    with TestClient(app, follow_redirects=False) as c:
        yield c


def fresh():
    return TestClient(app, follow_redirects=False)


def sign_up(c, email="user@example.com", password=USER_PASSWORD, **kw):
    return c.post("/signup", data={"email": email, "password": password}, **kw)


def log_in(c, email="user@example.com", password=USER_PASSWORD, **kw):
    return c.post("/login", data={"email": email, "password": password}, **kw)


def forgot(c, email="user@example.com", **kw):
    return c.post("/forgot", data={"email": email}, **kw)


def token_from(box, index=-1):
    body = box.bodies()[index]
    found = re.search(r"^([A-Za-z0-9_-]{30,})$", body, re.M)
    assert found, body
    return found.group(1)


def make_done(owner_name=jobs.OWNER, name="memo.wav"):
    job = jobs.new_job(name, "auto", owner_name)
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    jobs._update(job["id"], status="done", stage="done", progress=1.0, detail="Done", transcript=SAMPLE_TEXT,
                 notes=notes, engine=engine, seen=True)
    return job["id"]


def accounts_file_text():
    return (settings.data_dir / "accounts.json").read_text(encoding="utf-8")


# ------------------------------------------------------------------ sign up

def test_sign_up_creates_an_account_signs_in_and_stores_only_a_hash(client):
    r = sign_up(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert auth.COOKIE_NAME in client.cookies
    assert client.get("/api/notes").json() == []
    stored = json.loads(accounts_file_text())["accounts"]["user@example.com"]
    assert stored["hash"].startswith("scrypt:") and auth.verify_password(USER_PASSWORD, stored["hash"])
    assert USER_PASSWORD not in accounts_file_text()
    assert (settings.data_dir / "accounts.json").stat().st_mode & 0o077 == 0  # owner read and write only


def test_the_same_password_gets_a_different_salt_for_each_account(client):
    sign_up(client, "a@example.com")
    sign_up(fresh(), "b@example.com")
    data = json.loads(accounts_file_text())["accounts"]
    assert data["a@example.com"]["hash"] != data["b@example.com"]["hash"]


def test_a_duplicate_email_is_refused_in_any_case(client):
    assert sign_up(client).status_code == 303
    other = fresh()
    r = sign_up(other, "USER@Example.COM", "another long password")
    assert r.status_code == 409 and "already exists" in r.text
    assert "set-cookie" not in r.headers
    assert len(json.loads(accounts_file_text())["accounts"]) == 2  # the owner (migrated) and the first user
    assert log_in(fresh(), "user@example.com", USER_PASSWORD).status_code == 303  # the first password still works


def test_the_owner_email_cannot_be_signed_up(client):
    r = sign_up(client, OWNER_EMAIL.lower(), "attacker password 1")
    assert r.status_code == 409 and "set-cookie" not in r.headers
    assert log_in(fresh(), OWNER_EMAIL, OWNER_PASSWORD).status_code == 303


@pytest.mark.parametrize("password", ["", "short", "123456789"])
def test_a_short_password_is_refused(client, password):
    r = sign_up(client, "user@example.com", password)
    assert r.status_code == 400 and "10 characters" in r.text and "set-cookie" not in r.headers
    assert not (settings.data_dir / "accounts.json").exists() or "user@example.com" not in accounts_file_text()


def test_ten_characters_are_enough(client):
    assert sign_up(client, "ten@example.com", "x" * 10).status_code == 303


@pytest.mark.parametrize("email", ["", "nobody", "a@b", "a b@example.com", "a@@example.com", "x@example.com\r\nBcc: z@z.zz",
                                   "<script>@example.com", "a" * 300 + "@example.com"])
def test_an_invalid_email_is_refused(client, email):
    r = sign_up(client, email)
    assert r.status_code == 400 and "set-cookie" not in r.headers
    assert "<script>@" not in r.text


def test_sign_up_can_be_turned_off(client, monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_SIGNUP", "off")
    assert client.get("/signup").status_code == 403
    assert sign_up(client).status_code == 403
    assert "/signup" not in client.get("/login").text
    assert not (settings.data_dir / "accounts.json").exists() or "user@example.com" not in accounts_file_text()
    assert log_in(fresh(), OWNER_EMAIL, OWNER_PASSWORD).status_code == 303  # the owner still signs in


def test_sign_up_is_on_by_default_and_linked_from_the_login_page(client):
    assert client.get("/signup").status_code == 200
    assert 'href="/signup"' in client.get("/login").text and 'href="/forgot"' in client.get("/login").text


def test_sign_up_is_limited_per_address(client):
    codes = [sign_up(client, f"u{i}@example.com").status_code for i in range(12)]
    assert codes[:10] == [303] * 10 and codes[10] == 429


# ------------------------------------------------------------------ a new account sees only its own notes

def test_a_new_account_cannot_see_the_notes_of_the_owner(client):
    mine = make_done()
    sign_up(client)
    own = make_done("user@example.com", "own.wav")
    assert [n["id"] for n in client.get("/api/notes").json()] == [own]
    assert [n["id"] for n in client.get("/api/notes", params={"q": "landing"}).json()] == [own]
    assert [t["id"] for t in client.get("/api/tasks").json()] == [own]
    assert client.get(f"/api/notes/{own}").status_code == 200
    assert client.get(f"/api/notes/{mine}").status_code == 403
    assert client.get(f"/api/notes/{mine}/export?format=md").status_code == 403
    assert client.put(f"/api/notes/{mine}/share", json={"mode": "public"}).status_code == 404
    assert client.delete(f"/api/notes/{mine}").status_code == 404
    assert client.patch(f"/api/notes/{mine}", json={"title": "x"}).status_code == 404
    assert jobs.get(mine) is not None and jobs.get(mine)["share"] == "private"


def test_an_account_note_is_invisible_to_the_owner_and_other_accounts(client):
    sign_up(client)
    own = make_done("user@example.com")
    owner = fresh()
    assert log_in(owner, OWNER_EMAIL, OWNER_PASSWORD).status_code == 303
    assert owner.get("/api/notes").json() == []
    assert owner.get(f"/api/notes/{own}").status_code == 403
    other = fresh()
    sign_up(other, "other@example.com")
    assert other.get("/api/notes").json() == []
    assert other.delete(f"/api/notes/{own}").status_code == 404


# ------------------------------------------------------------------ the owner account

def test_the_owner_keeps_the_notes_and_signs_in_with_the_email(client):
    nid = make_done()
    assert log_in(client, OWNER_EMAIL.upper(), OWNER_PASSWORD).status_code == 303
    assert [n["id"] for n in client.get("/api/notes").json()] == [nid]
    assert client.get(f"/api/notes/{nid}").json()["owner"] == jobs.OWNER
    stored = json.loads(accounts_file_text())["accounts"]["owner@example.com"]
    assert stored["owner"] is True and auth.verify_password(OWNER_PASSWORD, stored["hash"])
    assert OWNER_PASSWORD not in accounts_file_text()


def test_the_owner_cannot_sign_in_with_the_password_alone_once_the_email_is_set(client):
    assert client.post("/login", data={"password": OWNER_PASSWORD}).status_code == 401
    assert log_in(client, "someone@example.com", OWNER_PASSWORD).status_code == 401
    assert log_in(client, OWNER_EMAIL, "wrong password!").status_code == 401
    assert client.get("/api/notes").status_code == 401


def test_without_an_owner_email_the_hash_only_setup_still_works(secured, monkeypatch):
    monkeypatch.delenv("VOICE_NOTES_OWNER_EMAIL")
    nid = make_done()
    with fresh() as c:
        assert c.post("/login", data={"password": OWNER_PASSWORD}).status_code == 303
        assert [n["id"] for n in c.get("/api/notes").json()] == [nid]
    with fresh() as c:
        assert c.post("/login", data={"password": "nope nope nope"}).status_code == 401
        # accounts still work next to it
        assert sign_up(c).status_code == 303 and c.get("/api/notes").json() == []


def test_the_login_form_requires_the_email_only_when_the_owner_email_is_set(client, monkeypatch):
    assert re.search(r'<input id="email"[^>]*\brequired', client.get("/login").text)
    monkeypatch.delenv("VOICE_NOTES_OWNER_EMAIL")
    assert not re.search(r'<input id="email"[^>]*\brequired', client.get("/login").text)


# ------------------------------------------------------------------ log in, wrong password, rate limit

def test_log_in_with_email_and_password_uses_the_same_cookie(client):
    sign_up(fresh())
    cookie = log_in(client).headers["set-cookie"].lower()
    assert cookie.startswith(auth.COOKIE_NAME) and "httponly" in cookie and "samesite=lax" in cookie
    assert "max-age=" in cookie and "path=/" in cookie
    assert client.get("/api/notes").status_code == 200


def test_a_wrong_password_and_an_unknown_email_get_the_same_answer(client):
    sign_up(fresh())
    wrong = log_in(client, "user@example.com", "not the password")
    unknown = log_in(client, "nobody@example.com", "not the password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.text == unknown.text and "Wrong email or password" in wrong.text
    assert "set-cookie" not in wrong.headers and "set-cookie" not in unknown.headers


def test_failed_logins_for_one_address_are_limited(client):
    sign_up(fresh())
    for _ in range(5):
        assert log_in(client, "user@example.com", "nope nope nope").status_code == 401
    r = log_in(client, "user@example.com", USER_PASSWORD)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0 and "set-cookie" not in r.headers


def test_failed_logins_for_one_email_are_limited_from_any_address(secured, monkeypatch):
    monkeypatch.setenv("VOICE_NOTES_TRUST_PROXY", "1")
    with fresh() as c:
        sign_up(c, headers={"X-Forwarded-For": "203.0.113.200"})
        for i in range(5):
            assert log_in(c, "user@example.com", "nope nope nope",
                          headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code == 401
        # a new address, the right password, but the email is locked
        assert log_in(c, "user@example.com", USER_PASSWORD, headers={"X-Forwarded-For": "203.0.113.99"}).status_code == 429
        # another email from that address is not affected
        assert log_in(c, OWNER_EMAIL, OWNER_PASSWORD, headers={"X-Forwarded-For": "203.0.113.99"}).status_code == 303


def test_the_email_lock_ends_after_the_window(client):
    sign_up(fresh())
    for _ in range(5):
        log_in(client, "user@example.com", "nope nope nope")
    assert log_in(client).status_code == 429
    for times in auth._failures.values():
        times[:] = [t - 100000 for t in times]
    auth._global_failures[:] = [t - 100000 for t in auth._global_failures]
    assert log_in(client).status_code == 303


def test_a_success_clears_the_counts(client):
    sign_up(fresh())
    for _ in range(4):
        log_in(client, "user@example.com", "nope nope nope")
    assert log_in(client).status_code == 303
    client.post("/api/logout")
    for _ in range(4):
        assert log_in(client, "user@example.com", "nope nope nope").status_code == 401


def test_logout_ends_an_account_session(client):
    sign_up(client)
    assert client.post("/api/logout").status_code == 204
    assert client.get("/api/notes").status_code == 401


# ------------------------------------------------------------------ recovery

def test_forgot_password_sends_one_mail_with_a_token_through_smtp(client, outbox):
    sign_up(fresh())
    r = forgot(client)
    assert r.status_code == 200
    assert len(outbox.mails) == 1
    mail = outbox.mails[0]
    assert mail["message"]["To"] == "user@example.com" and mail["host"] == "smtp.example.test" and mail["port"] == 587
    assert mail["tls"] is True  # STARTTLS before the login
    assert mail["login"] == ("mailer@example.test", SMTP_SECRET)
    assert "reset" in mail["message"]["Subject"].lower()
    assert len(token_from(outbox)) >= 40


def test_the_reply_is_the_same_whether_or_not_the_account_exists(client, outbox):
    sign_up(fresh())
    known = forgot(client, "user@example.com")
    unknown = forgot(client, "nobody@example.com")
    invalid = forgot(client, "not an email")
    empty = forgot(client, "")
    owner = forgot(client, OWNER_EMAIL)
    for r in (unknown, invalid, empty, owner):
        assert r.status_code == known.status_code == 200
        assert r.text == known.text
        assert {k: v for k, v in r.headers.items() if k != "content-length"} == \
               {k: v for k, v in known.headers.items() if k != "content-length"}
    assert "set-cookie" not in known.headers
    # a mail went to the two real accounts only
    assert sorted(m["message"]["To"] for m in outbox.mails) == ["owner@example.com", "user@example.com"]
    assert "user@example.com" not in known.text and "nobody@example.com" not in unknown.text


def test_a_failing_mail_server_gives_the_same_reply(client, outbox, monkeypatch):
    sign_up(fresh())
    good = forgot(client, "nobody@example.com")

    def broken(*a, **k):
        raise smtplib.SMTPConnectError(421, "down")

    monkeypatch.setattr(smtplib, "SMTP", broken)
    bad = forgot(client, "user@example.com")
    assert bad.status_code == good.status_code and bad.text == good.text


def test_the_token_works_once(client, outbox):
    sign_up(fresh())
    forgot(client)
    token = token_from(outbox)
    r = client.post("/reset", data={"token": token, "password": NEW_PASSWORD})
    assert r.status_code == 200 and "changed" in r.text
    assert log_in(fresh(), "user@example.com", NEW_PASSWORD).status_code == 303
    assert log_in(fresh(), "user@example.com", USER_PASSWORD).status_code == 401
    again = client.post("/reset", data={"token": token, "password": "yet another password"})
    assert again.status_code == 400 and "not valid or has expired" in again.text
    assert log_in(fresh(), "user@example.com", "yet another password").status_code == 401
    assert log_in(fresh(), "user@example.com", NEW_PASSWORD).status_code == 303


def test_an_expired_token_is_refused(client, outbox):
    sign_up(fresh())
    forgot(client)
    token = token_from(outbox)
    path = settings.data_dir / "accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["resets"]) == 1
    for entry in data["resets"].values():
        assert 29 * 60 < entry["expires"] - __import__("time").time() <= 30 * 60 + 5  # valid for 30 minutes
        entry["expires"] -= 31 * 60
    path.write_text(json.dumps(data), encoding="utf-8")
    r = client.post("/reset", data={"token": token, "password": NEW_PASSWORD})
    assert r.status_code == 400 and "not valid or has expired" in r.text
    assert log_in(fresh(), "user@example.com", USER_PASSWORD).status_code == 303  # the old password stands


@pytest.mark.parametrize("token", ["", "made-up", "x" * 5000])
def test_a_made_up_token_is_refused(client, token):
    sign_up(fresh())
    r = client.post("/reset", data={"token": token, "password": NEW_PASSWORD})
    assert r.status_code == 400
    assert log_in(fresh(), "user@example.com", NEW_PASSWORD).status_code == 401


def test_the_token_is_stored_as_a_hash_only(client, outbox):
    sign_up(fresh())
    forgot(client)
    token = token_from(outbox)
    assert token not in accounts_file_text()
    import hashlib
    assert hashlib.sha256(token.encode()).hexdigest() in json.loads(accounts_file_text())["resets"]


def test_a_new_request_cancels_the_older_token(client, outbox):
    sign_up(fresh())
    forgot(client)
    forgot(client)
    first, second = token_from(outbox, 0), token_from(outbox, 1)
    assert first != second
    assert client.post("/reset", data={"token": first, "password": NEW_PASSWORD}).status_code == 400
    assert client.post("/reset", data={"token": second, "password": NEW_PASSWORD}).status_code == 200


def test_a_short_new_password_does_not_use_up_the_token(client, outbox):
    sign_up(fresh())
    forgot(client)
    token = token_from(outbox)
    assert client.post("/reset", data={"token": token, "password": "short"}).status_code == 400
    assert client.post("/reset", data={"token": token, "password": NEW_PASSWORD}).status_code == 200


def test_a_reset_ends_the_open_sessions_of_the_account(client, outbox):
    sign_up(client)
    assert client.get("/api/notes").status_code == 200
    forgot(fresh())
    assert client.post("/reset", data={"token": token_from(outbox), "password": NEW_PASSWORD}).status_code == 200
    assert client.get("/api/notes").status_code == 401


def test_a_reset_works_for_the_owner_and_keeps_the_notes(client, outbox):
    nid = make_done()
    forgot(client, OWNER_EMAIL)
    assert client.post("/reset", data={"token": token_from(outbox), "password": NEW_PASSWORD}).status_code == 200
    assert log_in(fresh(), OWNER_EMAIL, OWNER_PASSWORD).status_code == 401
    owner = fresh()
    assert log_in(owner, OWNER_EMAIL, NEW_PASSWORD).status_code == 303
    assert [n["id"] for n in owner.get("/api/notes").json()] == [nid]


def test_recovery_requests_are_limited_and_the_reply_does_not_change(client, outbox):
    sign_up(fresh())
    replies = [forgot(client) for _ in range(6)]
    assert len({(r.status_code, r.text) for r in replies}) == 1
    assert len(outbox.mails) == 3  # three per email in the window


def test_without_smtp_settings_nothing_is_sent_and_the_reply_is_the_same(client, outbox, monkeypatch):
    sign_up(fresh())
    monkeypatch.delenv("SMTP_HOST")
    a, b = forgot(client), forgot(client, "nobody@example.com")
    assert a.text == b.text and a.status_code == b.status_code == 200 and outbox.mails == []


def test_the_recovery_pages_are_public_and_the_reset_page_is_a_form(client):
    assert client.get("/forgot").status_code == 200
    page = client.get("/reset")
    assert page.status_code == 200 and 'name="token"' in page.text and 'name="password"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "<script" not in page.text


def test_a_token_is_not_accepted_in_the_url(client, outbox):
    sign_up(fresh())
    forgot(client)
    token = token_from(outbox)
    assert client.get(f"/reset?token={token}").status_code == 200  # only shows the form
    assert accounts.token_valid(token)  # and did not use it


# ------------------------------------------------------------------ secrets stay out of logs and responses

def test_no_password_or_token_appears_in_logs_or_responses(client, outbox, caplog):
    caplog.set_level(logging.DEBUG)
    responses = []
    responses.append(sign_up(client, "user@example.com", USER_PASSWORD))
    responses.append(log_in(fresh(), "user@example.com", "wrong secret password"))
    responses.append(forgot(client))
    token = token_from(outbox)
    responses.append(client.post("/reset", data={"token": token, "password": NEW_PASSWORD}))
    responses.append(client.post("/reset", data={"token": token, "password": NEW_PASSWORD}))
    responses.append(log_in(fresh(), "user@example.com", NEW_PASSWORD))
    for r in responses:
        text = r.text + str(r.headers)
        for secret in (USER_PASSWORD, NEW_PASSWORD, "wrong secret password", token, SMTP_SECRET, OWNER_PASSWORD):
            assert secret not in text
    for secret in (USER_PASSWORD, NEW_PASSWORD, "wrong secret password", token, SMTP_SECRET, OWNER_PASSWORD):
        assert secret not in caplog.text
    # the token is only in the mail
    assert token in outbox.bodies()[0]


def test_a_failed_mail_logs_no_address_and_no_secret(client, outbox, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    sign_up(fresh())

    def broken(*a, **k):
        raise smtplib.SMTPAuthenticationError(535, f"bad login {SMTP_SECRET}".encode())

    monkeypatch.setattr(smtplib, "SMTP", broken)
    forgot(client)
    assert SMTP_SECRET not in caplog.text and "user@example.com" not in caplog.text


def test_the_settings_reader_never_returns_the_smtp_password_to_a_visitor(client):
    for url in ("/api/health", "/login", "/signup", "/forgot", "/reset"):
        assert SMTP_SECRET not in client.get(url).text


def test_the_source_has_no_mail_secret():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in (root / "app").rglob("*.py"):
        assert SMTP_SECRET not in path.read_text(encoding="utf-8", errors="ignore")


# ------------------------------------------------------------------ the tests never reach the network

def test_the_mail_goes_to_the_fake_smtp_not_the_network(client, outbox, monkeypatch):
    import socket

    def no_network(*a, **k):
        raise AssertionError("a real network connection was attempted")

    monkeypatch.setattr(socket, "create_connection", no_network)
    sign_up(fresh())
    forgot(client)
    assert outbox.connections == [("smtp.example.test", 587)]
