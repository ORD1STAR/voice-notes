"""Owner login: a salted scrypt password hash, server side sessions and a limit on failed logins.

The password is never stored. The server reads only its hash from VOICE_NOTES_PASSWORD_HASH. Create the hash with
`python -m app.auth` (see README.md).
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

# scrypt cost: 2**15 uses about 32 MB and roughly 0.1 s per check on a small server.
_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 128 * 1024 * 1024
_DKLEN = 32

_lock = threading.Lock()
_sessions: dict[str, float] = {}  # sha256 of the session token -> expiry time
_failures: dict[str, list[float]] = {}  # client address -> times of recent failed logins
_global_failures: list[float] = []


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


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for k in [k for k, exp in _sessions.items() if exp <= now]:
            del _sessions[k]
        _sessions[_key(token)] = now + session_seconds()
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
            return False
        return True


def end_session(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(_key(token), None)


# ------------------------------------------------------------------ rate limit on failed logins

def _prune(times: list[float], now: float) -> None:
    cutoff = now - failure_window()
    while times and times[0] <= cutoff:
        times.pop(0)


def retry_after(client: str) -> int:
    """Seconds to wait before the next try, or 0 when a try is allowed.

    One client address may fail max_failures times per window; all addresses together may fail ten times as many,
    which stops a guesser who changes address."""
    now = time.time()
    with _lock:
        times = _failures.get(client, [])
        _prune(times, now)
        _prune(_global_failures, now)
        wait = 0
        if len(times) >= max_failures():
            wait = int(times[0] + failure_window() - now) + 1
        if len(_global_failures) >= max_failures() * 10:
            wait = max(wait, int(_global_failures[0] + failure_window() - now) + 1)
        return wait


def record_failure(client: str) -> None:
    now = time.time()
    with _lock:
        _failures.setdefault(client, []).append(now)
        _global_failures.append(now)
        if len(_failures) > 5000:  # keep memory bounded
            for k in [k for k, v in _failures.items() if not v or v[-1] <= now - failure_window()]:
                del _failures[k]


def clear_failures(client: str) -> None:
    with _lock:
        _failures.pop(client, None)


def reset_state() -> None:
    """Forget all sessions and failures (used by the tests)."""
    with _lock:
        _sessions.clear()
        _failures.clear()
        _global_failures.clear()


# ------------------------------------------------------------------ the login page

def login_page(message: str = "", setup_missing: bool = False) -> str:
    note = ""
    if message:
        note = f'<p class="field-error" role="alert">{html.escape(message)}</p>'
    if setup_missing:
        body = (
            "<p>The owner password is not set on this server yet. Create a hash with "
            "<code>python -m app.auth</code>, put it in <code>VOICE_NOTES_PASSWORD_HASH</code> and restart the server. "
            "README.md explains the steps.</p>"
        )
    else:
        body = (
            '<form method="post" action="/login" class="login-form">'
            '<label for="password">Password</label>'
            '<input id="password" name="password" type="password" autocomplete="current-password" autofocus required>'
            '<button type="submit" class="btn primary">Sign in</button>'
            "</form>"
        )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        '<title>Sign in - Voice Notes</title>\n<link rel="icon" href="/favicon.svg" type="image/svg+xml">\n'
        '<link rel="stylesheet" href="/style.css">\n</head>\n<body class="login-page">\n'
        '<main class="login-box">\n<h1>Voice Notes</h1>\n'
        f"{body}\n{note}\n</main>\n</body>\n</html>\n"
    )


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
    if len(first) < 10:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    print(hash_password(first))
    return 0


if __name__ == "__main__":
    sys.exit(main())
