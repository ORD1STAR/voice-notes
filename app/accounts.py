"""Accounts keyed by email address, and the one-time tokens for password recovery.

Everything is kept in one small JSON file, DATA_DIR/accounts.json (mode 600): for each account a salted scrypt hash
(never the password), and for each recovery request only the sha256 of the token, the account and the expiry.

Account ids: the owner account (the email in VOICE_NOTES_OWNER_EMAIL) uses the id "owner", which every existing note
already carries. Any other account uses its email address as the id, so it only sees its own notes.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time

from . import auth
from .config import settings

TOKEN_SECONDS = 30 * 60
_EMAIL = re.compile(r"^[^@\s<>\"',;:\\]{1,64}@[^@\s<>\"',;:\\]{1,190}\.[^@\s<>\"',;:\\.]{2,63}$")

_lock = threading.RLock()
_dummy_hash: str | None = None


def normalize_email(text: str) -> str | None:
    """The email in lower case, or None when it is not a plain address."""
    email = (text or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        return None
    return email


def owner_email() -> str | None:
    """The owner's email from VOICE_NOTES_OWNER_EMAIL, or None (then the hash-only setup is used)."""
    return normalize_email(os.environ.get("VOICE_NOTES_OWNER_EMAIL", ""))


def account_id(email: str) -> str:
    return auth.OWNER_ID if email == owner_email() else email


def _path():
    return settings.data_dir / "accounts.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("accounts"), dict):
        data["accounts"] = {}
    if not isinstance(data.get("resets"), dict):
        data["resets"] = {}
    return data


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("accounts.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _migrate(data: dict) -> bool:
    """Make sure the owner has an account entry. The first time, it takes the hash from VOICE_NOTES_PASSWORD_HASH."""
    email = owner_email()
    if email and email not in data["accounts"] and auth.configured():
        data["accounts"][email] = {"hash": auth.password_hash(), "created": time.time(), "owner": True}
        return True
    return False


def migrate_owner() -> None:
    """Called when the server starts (and again whenever accounts are read)."""
    with _lock:
        data = _load()
        if _migrate(data):
            _save(data)


def exists(email: str) -> bool:
    with _lock:
        data = _load()
        _migrate(data)
        return email in data["accounts"]


def create(email: str, password: str) -> bool:
    """Add an account. False when the email is already taken (the owner's email counts as taken)."""
    stored = auth.hash_password(password)
    with _lock:
        data = _load()
        migrated = _migrate(data)
        if email in data["accounts"] or email == owner_email():
            if migrated:
                _save(data)
            return False
        data["accounts"][email] = {"hash": stored, "created": time.time()}
        _save(data)
        return True


def check(email: str, password: str) -> str | None:
    """The account id when the email and password are right, else None. The work is the same for unknown emails."""
    global _dummy_hash
    with _lock:
        data = _load()
        if _migrate(data):
            _save(data)
        entry = data["accounts"].get(email)
    if entry is None:
        if _dummy_hash is None:
            _dummy_hash = auth.hash_password(secrets.token_urlsafe(8))
        auth.verify_password(password, _dummy_hash)
        return None
    if auth.verify_password(password, str(entry.get("hash", ""))):
        return account_id(email)
    return None


# ------------------------------------------------------------------ recovery

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(email: str) -> str | None:
    """A new recovery token for an existing account (older tokens of it stop working), else None.

    Only the hash of the token is stored."""
    with _lock:
        data = _load()
        _migrate(data)
        if email not in data["accounts"]:
            return None
        now = time.time()
        data["resets"] = {h: r for h, r in data["resets"].items()
                          if r.get("expires", 0) > now and r.get("email") != email}
        token = secrets.token_urlsafe(32)
        data["resets"][_digest(token)] = {"email": email, "expires": now + TOKEN_SECONDS}
        _save(data)
        return token


def token_valid(token: str) -> bool:
    with _lock:
        r = _load()["resets"].get(_digest(token.strip()))
        return bool(r) and r.get("expires", 0) > time.time()


def use_token(token: str, new_password: str) -> str | None:
    """Set a new password with a recovery token. Returns the account id, or None for an unknown, used or expired token.

    The token is removed whatever happens next, so it works once."""
    stored = auth.hash_password(new_password)
    with _lock:
        data = _load()
        r = data["resets"].pop(_digest((token or "").strip()), None)
        if r is None or r.get("expires", 0) <= time.time() or r.get("email") not in data["accounts"]:
            if r is not None:
                _save(data)
            return None
        email = r["email"]
        data["accounts"][email]["hash"] = stored
        data["resets"] = {h: x for h, x in data["resets"].items() if x.get("email") != email}
        _save(data)
        return account_id(email)
