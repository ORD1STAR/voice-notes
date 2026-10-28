"""Send the password reset email through the SMTP server named in the environment.

SMTP_HOST, SMTP_PORT (default 587), SMTP_USER and SMTP_PASSWORD are read when a mail is sent and never logged or
printed. SMTP_FROM is optional (the sender address; it defaults to SMTP_USER when that is an address).
VOICE_NOTES_BASE_URL is optional (the public address of the site, used to put the link of the reset page in the mail).
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("voice_notes.mail")


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def _port() -> int:
    try:
        return int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        return 587


def _sender() -> str:
    explicit = os.environ.get("SMTP_FROM", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("SMTP_USER", "").strip()
    return user if "@" in user else "voice-notes@localhost"


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain text mail. Returns True when the server accepted it. Never raises, never logs secrets."""
    if not configured():
        log.warning("SMTP_HOST is not set: no email can be sent.")
        return False
    host, port = os.environ["SMTP_HOST"].strip(), _port()
    user, password = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = _sender(), to, subject
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            server.ehlo()
            if port != 465 and server.has_extn("starttls"):
                server.starttls(context=context)
                server.ehlo()
            if user and password:
                server.login(user, password)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        log.warning("The email could not be sent (%s).", type(exc).__name__)  # the class only: no address, no secret
        return False


def send_reset(to: str, token: str, minutes: int = 30) -> bool:
    base = os.environ.get("VOICE_NOTES_BASE_URL", "").strip().rstrip("/")
    where = f"Open {base}/reset" if base else "Open the Forgot password page of the site and choose 'I have a code'"
    body = (
        "Someone asked to reset the password of this Voice Notes account.\n\n"
        f"{where} and enter this code with your new password:\n\n{token}\n\n"
        f"The code works once and for {minutes} minutes. If you did not ask for it, ignore this message: "
        "your password stays as it is.\n"
    )
    return send(to, "Voice Notes: reset your password", body)
