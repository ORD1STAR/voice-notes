"""FastAPI application: upload, status, export."""
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import audio, export, jobs, structure
from .config import settings

STATIC_DIR = Path(__file__).resolve().parent / "static"
READ_SIZE = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    jobs.load_all()
    yield


app = FastAPI(title="Voice Notes", docs_url=None, redoc_url=None, lifespan=lifespan)


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items()}


@app.get("/api/health")
def health():
    return {
        "ffmpeg": audio.ffmpeg_available(),
        "groq_key": bool(settings.groq_api_key),
        "ollama": structure.ollama_status(),
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
def list_notes():
    return jobs.list_jobs()


@app.get("/api/notes/{job_id}")
def get_note(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "This note does not exist.")
    return _public(job)


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
    return Response(
        content,
        media_type=mime,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
