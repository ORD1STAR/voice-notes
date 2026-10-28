"""FastAPI application: upload, status, export."""
import io
import logging
import re
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, export, jobs, languages, structure, visuals
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
    jobs.load_all()
    yield


app = FastAPI(title="Voice Notes", docs_url=None, redoc_url=None, lifespan=lifespan)


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


def _public(job: dict) -> dict:
    """The job as the browser sees it. The transcript and diagrams come with finished notes only."""
    job = _clean(job)
    out = dict(job)
    out["pdf_problem"] = export.pdf_problem(job) if job.get("status") == "done" and job.get("notes") else None
    out["language"] = languages.lenient(job.get("language"))  # notes saved before the language field are automatic
    out["language_name"] = languages.name(out["language"])
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
    job = jobs.new_job(name, lang)
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
def list_notes(q: str = ""):
    return jobs.list_jobs(q[:200])


@app.get("/api/notes/{job_id}")
def get_note(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "This note does not exist.")
    return _public(job)


class RenameBody(BaseModel):
    title: str


@app.patch("/api/notes/{job_id}")
def rename_note(job_id: str, body: RenameBody):
    try:
        job = jobs.rename(job_id, body.title)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
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
def edit_note(job_id: str, body: EditBody):
    """Replace the text of a finished note with the version the reader corrected."""
    try:
        job = jobs.edit_notes(job_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if job is None:
        raise HTTPException(404, "This note does not exist or is not finished yet.")
    return _public(job)


@app.post("/api/notes/{job_id}/retry", status_code=202)
def retry_note(job_id: str):
    if not jobs.get(job_id):
        raise HTTPException(404, "This note does not exist.")
    if not jobs.retry(job_id):
        raise HTTPException(409, "This note cannot be tried again. Upload the recording once more.")
    return {"id": job_id}


@app.post("/api/notes/{job_id}/restructure", status_code=202)
def restructure_note(job_id: str):
    if not jobs.get(job_id):
        raise HTTPException(404, "This note does not exist.")
    if not jobs.restructure(job_id):
        raise HTTPException(409, "These notes cannot be rebuilt: the note is not finished or has no transcript.")
    return {"id": job_id}


@app.post("/api/notes/{job_id}/seen", status_code=204)
def seen_note(job_id: str):
    """The reader opened this note: the sidebar stops flagging it as new."""
    if not jobs.mark_seen(job_id):
        raise HTTPException(404, "This note does not exist.")
    return Response(status_code=204)


@app.delete("/api/notes/{job_id}", status_code=204)
def delete_note(job_id: str):
    if not jobs.delete(job_id):
        raise HTTPException(404, "This note does not exist or is still being processed.")
    return Response(status_code=204)


@app.get("/api/export/all")
def export_all(format: str = "md", transcript: bool = False):
    """A ZIP with one Markdown (or text) file per finished note, as a backup or to move the notes elsewhere."""
    if format not in ("md", "txt"):
        raise HTTPException(400, "Format must be txt or md.")
    rows = [_clean(j) for j in jobs.done_jobs()]
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
def export_note(job_id: str, format: str = "md", transcript: bool = False):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "This note does not exist.")
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
