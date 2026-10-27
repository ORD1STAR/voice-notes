"""FastAPI application: upload, status, export."""
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, export, jobs, structure, visuals
from .config import settings

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


def _public(job: dict) -> dict:
    """The job as the browser sees it. The transcript and diagrams come with finished notes only."""
    out = dict(job)
    out["ahead"] = jobs.queue_ahead(job["id"]) if job.get("status") in ("queued", "processing") else 0
    out["stage_elapsed"] = max(0, round(time.time() - job.get("stage_started", time.time())))
    if job.get("status") == "done" and job.get("notes"):
        out["notes"] = structure.ensure_key_points(job["notes"])  # notes saved before key points existed get them here
        out["visuals"] = visuals.build_visuals(job["notes"])
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
        "structuring": structure.engine_plan(),
        "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
    }


@app.post("/api/notes", status_code=202)
async def create_note(request: Request, file: UploadFile = File(...)):
    if not settings.groq_api_key:
        raise HTTPException(503, "The server has no GROQ_API_KEY configured, so it cannot transcribe yet.")
    if not audio.ffmpeg_available():
        raise HTTPException(503, "ffmpeg is not installed on the server.")
    limit = settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 1024 * 1024:
        raise HTTPException(413, f"The file is larger than {limit // (1024 * 1024)} MB.")

    name = re.sub(r"[\x00-\x1f/\\]", "", file.filename or "recording").strip() or "recording"
    job = jobs.new_job(name)
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


@app.delete("/api/notes/{job_id}", status_code=204)
def delete_note(job_id: str):
    if not jobs.delete(job_id):
        raise HTTPException(404, "This note does not exist or is still being processed.")
    return Response(status_code=204)


@app.get("/api/notes/{job_id}/export")
def export_note(job_id: str, format: str = "md", transcript: bool = False):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "This note does not exist.")
    if format not in export.FORMATS:
        raise HTTPException(400, "Format must be pdf, txt or md.")
    if job["status"] != "done" or not job.get("notes"):
        raise HTTPException(409, "This note is not ready yet.")
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


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
