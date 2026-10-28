"""FastAPI application: upload, status, export."""
import io
import logging
import hmac
import re
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, auth, export, jobs, languages, structure, visuals
from .config import settings

# The app's own log lines (which engine wrote each note) go to the console next to the server's lines.
_log = logging.getLogger("voice_notes")
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent / "static"
READ_SIZE = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if auth.auth_disabled():
        _log.warning("VOICE_NOTES_AUTH=off: the login is switched off. Use this for local development only.")
    elif not auth.configured():
        _log.warning("VOICE_NOTES_PASSWORD_HASH is not set or not valid: nobody can sign in. See README.md.")
    jobs.load_all()
    yield


app = FastAPI(title="Voice Notes", docs_url=None, redoc_url=None, lifespan=lifespan)


# ---------------------------------------------------------------- owner login
# Everything needs the owner's session except the login page, the files the login page loads, the public share view
# (the page /n/<id> and the two reads it makes) and the logout. Without a configured password hash the server
# refuses every protected request, so a new install is never open by accident.
_PUBLIC_ASSETS = {"/app.js", "/style.css", "/favicon.svg"}
_SHARE_PAGE = re.compile(r"/n/[^/]*/?")
_SHARE_READS = re.compile(r"/api/notes/[^/]+(/export)?")


def _is_public(method: str, path: str) -> bool:
    if path == "/login" and method in ("GET", "HEAD", "POST"):
        return True
    if path == "/api/logout" and method == "POST":
        return True
    if method not in ("GET", "HEAD"):
        return False
    return (path in _PUBLIC_ASSETS or path.startswith("/fonts/") or bool(_SHARE_PAGE.fullmatch(path))
            or bool(_SHARE_READS.fullmatch(path)))


def _caller(request: Request) -> str | None:
    """The account that sent the request, or None for a visitor without a session. There is one account for now;
    with more accounts this is the one place that maps a session to its owner."""
    if auth.auth_disabled():
        return jobs.OWNER
    if auth.configured() and auth.valid_session(request.cookies.get(auth.COOKIE_NAME)):
        return jobs.OWNER
    return None


PRIVATE_MESSAGE = "This note is private. Only its owner can open it. Sign in as the owner, or ask for a public link."


class PrivateNote(Exception):
    """A visitor asked for a private note without the right to read it."""


@app.exception_handler(PrivateNote)
async def private_note(_: Request, __: PrivateNote):
    return JSONResponse({"error": PRIVATE_MESSAGE, "private": True}, status_code=403,
                        headers={"Cache-Control": "no-store"})


def _own(request: Request, job_id: str, message: str = "This note does not exist.") -> dict:
    """The note, if it belongs to the signed in account. Another account sees it as not existing."""
    job = jobs.get(job_id)
    if not job or jobs.owner_of(job) != request.state.owner:
        raise HTTPException(404, message)
    return job


def _readable(request: Request, job_id: str, token: str = "") -> tuple[dict, bool]:
    """The note for a read through the share link, and whether the reader is its owner.

    The owner reads any of their notes. Everyone else reads a public note, or a private note with its link secret."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "This note does not exist.")
    if request.state.owner is not None and jobs.owner_of(job) == request.state.owner:
        return job, True
    if jobs.share_of(job) == "public":
        return job, False
    secret = job.get("share_token")
    if secret and token and hmac.compare_digest(str(secret).encode(), token.encode()):
        return job, False
    raise PrivateNote()


@app.middleware("http")
async def require_login(request: Request, call_next):
    request.state.owner = _caller(request)
    # The share reads pass here; the handler decides by the share mode of the note.
    if request.state.owner is not None or _is_public(request.method, request.url.path):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Sign in required."}, status_code=401, headers={"Cache-Control": "no-store"})
    return RedirectResponse("/login", status_code=303)


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return auth.trust_proxy() and request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def _client_id(request: Request) -> str:
    if auth.trust_proxy():
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()  # the address the trusted proxy saw
    return request.client.host if request.client else "unknown"


def _login_response(html: str, status: int = 200, **headers) -> HTMLResponse:
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store", **headers})


@app.get("/login")
def login_page(request: Request):
    if auth.auth_disabled() or (auth.configured() and auth.valid_session(request.cookies.get(auth.COOKIE_NAME))):
        return RedirectResponse("/", status_code=303)
    return _login_response(auth.login_page(setup_missing=not auth.configured()), 200 if auth.configured() else 503)


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if auth.auth_disabled():
        return RedirectResponse("/", status_code=303)
    if not auth.configured():
        return _login_response(auth.login_page(setup_missing=True), 503)
    client = _client_id(request)
    wait = auth.retry_after(client)
    if wait:
        return _login_response(auth.login_page("Too many failed attempts. Try again later."), 429,
                               **{"Retry-After": str(wait)})
    if len(password) <= 1024 and auth.verify_password(password, auth.password_hash()):
        auth.clear_failures(client)
        token = auth.new_session()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.session_seconds(), httponly=True,
                            samesite="lax", secure=_is_https(request), path="/")
        response.headers["Cache-Control"] = "no-store"
        return response
    auth.record_failure(client)
    return _login_response(auth.login_page("Wrong password."), 401)


@app.post("/api/logout", status_code=204)
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE_NAME))
    response = Response(status_code=204)
    response.delete_cookie(auth.COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=_is_https(request))
    return response


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self'; "
        "script-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    path = request.url.path
    if path.startswith("/fonts/"):
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    elif not path.startswith("/api/"):
        # Pages, scripts and styles are revalidated on every load, so an update never meets a stale file.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def _clean(job: dict) -> dict:
    """The job with spoken framing taken out of notes written by an engine. Notes the reader edited are theirs."""
    if job.get("status") != "done" or not job.get("notes") or job.get("edited"):
        return job
    return {**job, "notes": structure.unspoken_notes(job["notes"])}


def _public(job: dict, owner_view: bool = True) -> dict:
    """The job as the browser sees it. The transcript and diagrams come with finished notes only.
    The owner field and the link secret go to the owner only."""
    job = _clean(job)
    out = dict(job)
    out["share"] = jobs.share_of(job)
    if owner_view:
        out["owner"] = jobs.owner_of(job)
    else:
        out.pop("owner", None)
        out.pop("share_token", None)
    out["pdf_problem"] = export.pdf_problem(job) if job.get("status") == "done" and job.get("notes") else None
    out["language"] = languages.lenient(job.get("language"))  # notes saved before the language field are automatic
    out["language_name"] = languages.name(out["language"])
    out["pinned"] = bool(job.get("pinned"))
    out["ahead"] = jobs.queue_ahead(job["id"]) if job.get("status") in ("queued", "processing") else 0
    out["stage_elapsed"] = max(0, round(time.time() - job.get("stage_started", time.time())))
    if job.get("status") == "done" and job.get("notes"):
        out["notes"] = structure.ensure_key_points(job["notes"])  # notes saved before key points existed get them here
        out["visuals"] = visuals.build_visuals(job["notes"], job.get("language"))
        out["transcript_paragraphs"] = structure.paragraphs(job.get("transcript") or "")
    else:
        out["transcript"] = None
        out["visuals"] = []
        out["transcript_paragraphs"] = []
    return out


@app.get("/api/health")
def health():
    return {
        "ffmpeg": audio.ffmpeg_available(),
        "groq_key": bool(settings.groq_api_key),
        "ollama": structure.ollama_status(),
        "groq_chat": structure.groq_status(),
        "structuring": structure.engine_plan(),
        "languages": [{"code": c, "name": n} for c, n in languages.LANGUAGES.items()],
        "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
    }


@app.post("/api/notes", status_code=202)
async def create_note(request: Request, file: UploadFile = File(...), language: str | None = Form(None)):
    try:
        lang = languages.normalize(settings.language if language is None else language)
    except ValueError:
        raise HTTPException(400, f"The language must be one of: {languages.allowed_text()}.")
    if not settings.groq_api_key:
        raise HTTPException(503, "The server has no GROQ_API_KEY configured, so it cannot transcribe yet.")
    if not audio.ffmpeg_available():
        raise HTTPException(503, "ffmpeg is not installed on the server.")
    limit = settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 1024 * 1024:
        raise HTTPException(413, f"The file is larger than {limit // (1024 * 1024)} MB.")

    name = re.sub(r"[\x00-\x1f/\\]", "", file.filename or "recording").strip() or "recording"
    job = jobs.new_job(name, lang, request.state.owner)
    dest = jobs.upload_path(job["id"])
    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                block = await file.read(READ_SIZE)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise HTTPException(413, f"The file is larger than {limit // (1024 * 1024)} MB.")
                out.write(block)
        if size == 0:
            raise HTTPException(400, "The uploaded file is empty.")
    except HTTPException:
        dest.unlink(missing_ok=True)
        jobs.delete(job["id"])
        raise
    jobs.start(job["id"])
    return {"id": job["id"]}


@app.get("/api/notes")
def list_notes(request: Request, q: str = ""):
    return jobs.list_jobs(q[:200], request.state.owner)


@app.get("/api/notes/{job_id}")
def get_note(request: Request, job_id: str, t: str = ""):
    job, is_owner = _readable(request, job_id, t)
    return _public(job, is_owner)


class RenameBody(BaseModel):
    title: str


@app.patch("/api/notes/{job_id}")
def rename_note(request: Request, job_id: str, body: RenameBody):
    _own(request, job_id, "This note does not exist or is not finished yet.")
    try:
        job = jobs.rename(job_id, body.title)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if job is None:
        raise HTTPException(404, "This note does not exist or is not finished yet.")
    return _public(job)


@app.get("/api/tasks")
def list_tasks(request: Request):
    """The action items of every finished note, newest note first. The tick boxes are kept in the browser."""
    out = []
    for job in jobs.done_jobs(request.state.owner):
        notes = _clean(job).get("notes") or {}
        actions = [str(a).strip() for a in notes.get("actions") or [] if str(a).strip()]
        if actions:
            out.append({"id": job["id"], "title": notes.get("title") or job.get("filename") or "", "created": job["created"],
                        "pinned": bool(job.get("pinned")), "actions": actions})
    return out


class PinBody(BaseModel):
    pinned: bool


@app.put("/api/notes/{job_id}/pin")
def pin_note(request: Request, job_id: str, body: PinBody):
    _own(request, job_id, "This note does not exist or is not finished yet.")
    """Keep a finished note at the top of the list (or let it go back to its place by date)."""
    job = jobs.set_pinned(job_id, body.pinned)
    if job is None:
        raise HTTPException(404, "This note does not exist or is not finished yet.")
    return _public(job)


class EditBody(BaseModel):
    title: str
    summary: str = ""
    key_points: list = []
    actions: list = []
    sections: list = []


@app.put("/api/notes/{job_id}/notes")
def edit_note(request: Request, job_id: str, body: EditBody):
    """Replace the text of a finished note with the version the reader corrected."""
    _own(request, job_id, "This note does not exist or is not finished yet.")
    try:
        job = jobs.edit_notes(job_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if job is None:
        raise HTTPException(404, "This note does not exist or is not finished yet.")
    return _public(job)


@app.post("/api/notes/{job_id}/retry", status_code=202)
def retry_note(request: Request, job_id: str):
    _own(request, job_id)
    if not jobs.retry(job_id):
        raise HTTPException(409, "This note cannot be tried again. Upload the recording once more.")
    return {"id": job_id}


@app.post("/api/notes/{job_id}/restructure", status_code=202)
def restructure_note(request: Request, job_id: str):
    _own(request, job_id)
    if not jobs.restructure(job_id):
        raise HTTPException(409, "These notes cannot be rebuilt: the note is not finished or has no transcript.")
    return {"id": job_id}


@app.post("/api/notes/{job_id}/seen", status_code=204)
def seen_note(request: Request, job_id: str):
    """The reader opened this note: the sidebar stops flagging it as new."""
    _own(request, job_id)
    if not jobs.mark_seen(job_id):
        raise HTTPException(404, "This note does not exist.")
    return Response(status_code=204)


@app.delete("/api/notes/{job_id}", status_code=204)
def delete_note(request: Request, job_id: str):
    _own(request, job_id, "This note does not exist or is still being processed.")
    if not jobs.delete(job_id):
        raise HTTPException(404, "This note does not exist or is still being processed.")
    return Response(status_code=204)


class ShareBody(BaseModel):
    mode: str


@app.put("/api/notes/{job_id}/share")
def set_share_mode(request: Request, job_id: str, body: ShareBody):
    """Switch the note between private (the link opens for the owner only) and public (anyone with the link)."""
    _own(request, job_id)
    try:
        job = jobs.set_share(job_id, body.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _public(job)


@app.post("/api/notes/{job_id}/share/token")
def make_share_token(request: Request, job_id: str):
    """Create (or replace) the secret that lets one person open a private note by link, without a login."""
    _own(request, job_id)
    return _public(jobs.set_share_token(job_id, True))


@app.delete("/api/notes/{job_id}/share/token")
def remove_share_token(request: Request, job_id: str):
    _own(request, job_id)
    return _public(jobs.set_share_token(job_id, False))


@app.get("/api/export/all")
def export_all(request: Request, format: str = "md", transcript: bool = False):
    """A ZIP with one Markdown (or text) file per finished note, as a backup or to move the notes elsewhere."""
    if format not in ("md", "txt"):
        raise HTTPException(400, "Format must be txt or md.")
    rows = [_clean(j) for j in jobs.done_jobs(request.state.owner)]
    if not rows:
        raise HTTPException(404, "There are no finished notes yet.")
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for job in rows:
            content, _ = export.render(job, format, include_transcript=transcript)
            day = time.strftime("%Y-%m-%d", time.localtime(job["created"]))
            base = f"{day}-{export.safe_filename(job['notes']['title'], format)[: -len(format) - 1]}"
            name, n = f"{base}.{format}", 2
            while name in used:
                name, n = f"{base}-{n}.{format}", n + 1
            used.add(name)
            archive.writestr(name, content)
    stamp = time.strftime("%Y-%m-%d")
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="voice-notes-{stamp}.zip"'},
    )


@app.get("/api/notes/{job_id}/export")
def export_note(request: Request, job_id: str, format: str = "md", transcript: bool = False, t: str = ""):
    job, _ = _readable(request, job_id, t)
    job = _clean(job)
    if format not in export.FORMATS:
        raise HTTPException(400, "Format must be pdf, txt or md.")
    if job["status"] != "done" or not job.get("notes"):
        raise HTTPException(409, "This note is not ready yet.")
    if format == "pdf":
        problem = export.pdf_problem(job, transcript)
        if problem:
            raise HTTPException(422, problem)
    content, mime = export.render(job, format, include_transcript=transcript)
    filename = export.safe_filename(job["notes"]["title"], format)
    ascii_name = filename.encode("ascii", "ignore").decode()
    if not ascii_name or ascii_name.startswith("."):
        ascii_name = f"note.{format}"
    return Response(
        content,
        media_type=mime,
        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"},
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/n/{note_id}")
def shared_note(note_id: str):
    """The share link: the same page, which shows the note full screen. Unknown ids get the page too,
    and the page says the note does not exist."""
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex, nofollow"})


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
