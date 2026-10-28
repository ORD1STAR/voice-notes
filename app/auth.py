"""Login: salted scrypt password hashes, server side sessions, a limit on failed logins, and the account pages.

Passwords are never stored. The owner's first password comes from the hash in VOICE_NOTES_PASSWORD_HASH. Create the
hash with `python -m app.auth` (see README.md). Accounts and password recovery live in app/accounts.py.
"""
import base64
import getpass
import hashlib
import hmac
import html
import os
import secrets
import sys
import threading
import time

COOKIE_NAME = "vn_session"
OWNER_ID = "owner"  # the account id of the owner; the same string as jobs.OWNER
MIN_PASSWORD = 10
MAX_PASSWORD = 1024

# scrypt cost: 2**15 uses about 32 MB and roughly 0.1 s per check on a small server.
_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 128 * 1024 * 1024
_DKLEN = 32

_lock = threading.Lock()
_sessions: dict[str, float] = {}  # sha256 of the session token -> expiry time
_session_owner: dict[str, str] = {}  # sha256 of the session token -> account id
# Times of recent failed logins, per client address ("ip:...") and per email ("email:...").
_failures: dict[str, list[float]] = {}
_global_failures: list[float] = []
_events: dict[str, list[float]] = {}  # sign ups and recovery requests, per kind, address and email


# ------------------------------------------------------------------ settings (read when used, so tests can change them)

def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def password_hash() -> str:
    return os.environ.get("VOICE_NOTES_PASSWORD_HASH", "").strip()


def auth_disabled() -> bool:
    """Only for local development and the test suite: VOICE_NOTES_AUTH=off turns the login off."""
    return os.environ.get("VOICE_NOTES_AUTH", "").strip().lower() == "off"


def signup_open() -> bool:
    """Sign up is on unless VOICE_NOTES_SIGNUP is off (also 0, false, no)."""
    return os.environ.get("VOICE_NOTES_SIGNUP", "on").strip().lower() not in ("off", "0", "false", "no")


def trust_proxy() -> bool:
    return _flag("VOICE_NOTES_TRUST_PROXY")


def session_seconds() -> int:
    return _int("VOICE_NOTES_SESSION_DAYS", 14) * 86400


def max_failures() -> int:
    return _int("VOICE_NOTES_LOGIN_MAX_FAILS", 5)


def failure_window() -> int:
    return _int("VOICE_NOTES_LOGIN_WINDOW", 900)


# ------------------------------------------------------------------ password hash

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    """A string `scrypt:N:r:p:salt:hash` (no dollar signs, so it is safe in shell and unit files)."""
    salt = salt if salt is not None else os.urandom(16)
    raw = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=_DKLEN)
    return f"scrypt:{_N}:{_R}:{_P}:{_b64(salt)}:{_b64(raw)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split(":")
        if scheme != "scrypt":
            return False
        want = _unb64(expected)
        got = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=_MAXMEM, dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def valid_hash_format(stored: str) -> bool:
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        int(parts[1]), int(parts[2]), int(parts[3])
        _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, TypeError):
        return False
    return True


def configured() -> bool:
    return valid_hash_format(password_hash())


# ------------------------------------------------------------------ sessions

def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(owner: str = OWNER_ID) -> str:
    """A new session for the account `owner` (the id the notes carry: "owner" or the email of another account)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for k in [k for k, exp in _sessions.items() if exp <= now]:
            del _sessions[k]
            _session_owner.pop(k, None)
        _sessions[_key(token)] = now + session_seconds()
        _session_owner[_key(token)] = owner
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        exp = _sessions.get(_key(token))
        if exp is None:
            return False
        if exp <= time.time():
            del _sessions[_key(token)]
            _session_owner.pop(_key(token), None)
            return False
        return True


def session_account(token: str | None) -> str | None:
    """The account id of a valid session, or None."""
    if not valid_session(token):
        return None
    with _lock:
        return _session_owner.get(_key(token), OWNER_ID)


def end_session(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(_key(token), None)
            _session_owner.pop(_key(token), None)


def end_sessions_of(account: str) -> None:
    """End every session of one account (after its password was reset)."""
    with _lock:
        for k in [k for k, who in _session_owner.items() if who == account]:
            _session_owner.pop(k, None)
            _sessions.pop(k, None)


# ------------------------------------------------------------------ rate limit on failed logins

def _prune(times: list[float], now: float) -> None:
    cutoff = now - failure_window()
    while times and times[0] <= cutoff:
        times.pop(0)


def _keys(client: str, email: str) -> list[str]:
    keys = ["ip:" + client]
    if email:
        keys.append("email:" + email[:254])
    return keys


def retry_after(client: str, email: str = "") -> int:
    """Seconds to wait before the next try, or 0 when a try is allowed.

    One client address may fail max_failures times per window, and so may one email address; all addresses together
    may fail ten times as many, which stops a guesser who changes address."""
    now = time.time()
    with _lock:
        wait = 0
        for key in _keys(client, email):
            times = _failures.get(key, [])
            _prune(times, now)
            if len(times) >= max_failures():
                wait = max(wait, int(times[0] + failure_window() - now) + 1)
        _prune(_global_failures, now)
        if len(_global_failures) >= max_failures() * 10:
            wait = max(wait, int(_global_failures[0] + failure_window() - now) + 1)
        return wait


def record_failure(client: str, email: str = "") -> None:
    now = time.time()
    with _lock:
        for key in _keys(client, email):
            _failures.setdefault(key, []).append(now)
        _global_failures.append(now)
        if len(_failures) > 5000:  # keep memory bounded
            for k in [k for k, v in _failures.items() if not v or v[-1] <= now - failure_window()]:
                del _failures[k]


def clear_failures(client: str, email: str = "") -> None:
    with _lock:
        for key in _keys(client, email):
            _failures.pop(key, None)


def allow_event(kind: str, client: str, email: str = "", per_address: int = 10, per_email: int = 3) -> bool:
    """Count an event (a sign up, a recovery request) and say whether it is within the limits for this window.

    The count goes up whether or not the event is allowed, so a flood stays blocked."""
    now = time.time()
    ok = True
    with _lock:
        for key, limit in ((f"{kind}:ip:{client}", per_address), (f"{kind}:email:{email[:254]}", per_email)):
            if key.endswith(":") and not email:
                continue
            times = _events.setdefault(key, [])
            _prune(times, now)
            if len(times) >= limit:
                ok = False
            times.append(now)
        if len(_events) > 5000:
            for k in [k for k, v in _events.items() if not v or v[-1] <= now - failure_window()]:
                del _events[k]
    return ok


def reset_state() -> None:
    """Forget all sessions and failures (used by the tests)."""
    with _lock:
        _sessions.clear()
        _session_owner.clear()
        _failures.clear()
        _global_failures.clear()
        _events.clear()


# ------------------------------------------------------------------ the pages

def _shell(title: str, inner: str, message: str = "") -> str:
    note = f'<p class="field-error" role="alert">{html.escape(message)}</p>' if message else ""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f'<title>{html.escape(title)} - Voice Notes</title>\n<link rel="icon" href="/favicon.svg" type="image/svg+xml">\n'
        '<link rel="stylesheet" href="/style.css">\n</head>\n<body class="login-page">\n'
        '<main class="login-box">\n<h1>Voice Notes</h1>\n'
        f"{inner}\n{note}\n</main>\n</body>\n</html>\n"
    )


def _setup_text() -> str:
    return (
        "<p>The owner password is not set on this server yet. Create a hash with "
        "<code>python -m app.auth</code>, put it in <code>VOICE_NOTES_PASSWORD_HASH</code> and restart the server. "
        "README.md explains the steps.</p>"
    )


def login_page(message: str = "", setup_missing: bool = False, email_required: bool = False,
               signup: bool = False, info: str = "") -> str:
    if setup_missing:
        return _shell("Sign in", _setup_text(), message)
    required = " required" if email_required else ""
    links = '<p><a href="/forgot">Forgot password?</a></p>'
    if signup:
        links += '<p><a href="/signup">Create an account</a></p>'
    notice = f"<p>{html.escape(info)}</p>" if info else ""
    body = (
        f"{notice}"
        '<form method="post" action="/login" class="login-form">'
        '<label for="email">Email</label>'
        f'<input id="email" name="email" type="email" autocomplete="username" maxlength="254"{required}>'
        '<label for="password">Password</label>'
        '<input id="password" name="password" type="password" autocomplete="current-password" required>'
        '<button type="submit" class="btn primary">Sign in</button>'
        "</form>"
        f"{links}"
    )
    return _shell("Sign in", body, message)


def signup_page(message: str = "", setup_missing: bool = False, closed: bool = False) -> str:
    if setup_missing:
        return _shell("Create an account", _setup_text(), message)
    if closed:
        return _shell("Create an account", '<p>Sign up is closed on this server.</p><p><a href="/login">Sign in</a></p>',
                      message)
    body = (
        '<form method="post" action="/signup" class="login-form">'
        '<label for="email">Email</label>'
        '<input id="email" name="email" type="email" autocomplete="username" maxlength="254" autofocus required>'
        f'<label for="password">Password (at least {MIN_PASSWORD} characters)</label>'
        f'<input id="password" name="password" type="password" autocomplete="new-password" minlength="{MIN_PASSWORD}" required>'
        '<button type="submit" class="btn primary">Create account</button>'
        '</form><p><a href="/login">Back to sign in</a></p>'
    )
    return _shell("Create an account", body, message)


def forgot_page(message: str = "", sent: bool = False) -> str:
    if sent:
        body = (
            "<p>If an account exists for that address, an email with a reset code is on its way. "
            "The code works once and for 30 minutes.</p>"
            '<p><a href="/reset">I have a code</a></p><p><a href="/login">Back to sign in</a></p>'
        )
        return _shell("Forgot password", body)
    body = (
        '<form method="post" action="/forgot" class="login-form">'
        '<label for="email">Email</label>'
        '<input id="email" name="email" type="email" autocomplete="username" maxlength="254" autofocus required>'
        '<button type="submit" class="btn primary">Send a reset code</button>'
        '</form><p><a href="/login">Back to sign in</a></p>'
    )
    return _shell("Forgot password", body, message)


def reset_page(message: str = "", done: bool = False) -> str:
    if done:
        return _shell("Reset password", '<p>Your password was changed.</p><p><a href="/login">Sign in</a></p>')
    body = (
        '<form method="post" action="/reset" class="login-form">'
        '<label for="token">Reset code from the email</label>'
        '<input id="token" name="token" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" '
        'maxlength="200" autofocus required>'
        f'<label for="password">New password (at least {MIN_PASSWORD} characters)</label>'
        f'<input id="password" name="password" type="password" autocomplete="new-password" minlength="{MIN_PASSWORD}" required>'
        '<button type="submit" class="btn primary">Set the new password</button>'
        '</form><p><a href="/forgot">Send a new code</a></p><p><a href="/login">Back to sign in</a></p>'
    )
    return _shell("Reset password", body, message)


# ------------------------------------------------------------------ command line: print a hash

def main() -> int:
    """`python -m app.auth` asks for a password twice (nothing is shown) and prints its hash."""
    if sys.stdin.isatty():
        first = getpass.getpass("New owner password: ")
        if first != getpass.getpass("Repeat the password: "):
            print("The two passwords differ.", file=sys.stderr)
            return 1
    else:
        first = sys.stdin.readline().rstrip("\r\n")
    if len(first) < MIN_PASSWORD:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    print(hash_password(first))
    return 0


if __name__ == "__main__":
    sys.exit(main())
