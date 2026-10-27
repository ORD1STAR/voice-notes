"""Note jobs: state, persistence on disk and the processing pipeline."""
import json
import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import audio, languages, structure, transcribe
from .config import settings

STAGES = ["queued", "converting", "transcribing", "structuring", "done"]
KEEP_FAILED_UPLOADS_DAYS = 7
MAX_TITLE = 100

log = logging.getLogger("voice_notes.jobs")

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_worker_slot = threading.Semaphore(1)  # one job at a time keeps the CPU free for the local model


def _job_dir(job_id: str) -> Path:
    return settings.data_dir / "jobs" / job_id


def _save(job: dict) -> None:
    d = _job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / "job.json")


def upload_path(job_id: str) -> Path:
    return _job_dir(job_id) / "upload.bin"


def load_all() -> None:
    """Load saved jobs at startup.

    Jobs that were running are marked as interrupted; they can be tried again because the upload is
    kept. Uploads of failed notes older than a week are removed.
    """
    with _lock:
        _jobs.clear()
        root = settings.data_dir / "jobs"
        if not root.exists():
            return
        now = time.time()
        for f in root.glob("*/job.json"):
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("status") in ("queued", "processing") and job.get("restructure") and job.get("notes"):
                # a rebuild was interrupted: the notes from before are still complete
                job.update(status="done", stage="done", progress=1.0, detail="Done", restructure=False, error=None,
                           rebuild_error="The server restarted before the notes were rebuilt. The earlier notes are kept.")
                _save(job)
            elif job.get("status") in ("queued", "processing"):
                job.update(status="error", error="The server restarted before this note was finished.",
                           retryable=True)
                _save(job)
            if job.get("status") == "error":
                up = upload_path(job["id"])
                if up.exists() and now - float(job.get("created", now)) > KEEP_FAILED_UPLOADS_DAYS * 86400:
                    up.unlink(missing_ok=True)
                    job["retryable"] = bool(job.get("transcript"))
                    _save(job)
                elif job.get("retryable") and not up.exists() and not job.get("transcript"):
                    job["retryable"] = False
            _jobs[job["id"]] = job


def new_job(filename: str, language: str = "auto") -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "filename": filename[:200],
        "language": languages.lenient(language),
        "created": time.time(),
        "status": "queued",
        "stage": "queued",
        "stage_started": time.time(),
        "progress": 0.0,
        "detail": "Waiting to start",
        "error": None,
        "retryable": False,
        "duration": None,
        "transcript": None,
        "notes": None,
        "engine": None,
        "engine_detail": None,
        "plan": None,
    }
    with _lock:
        _jobs[job["id"]] = job
        _save(job)
    return job


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def queue_ahead(job_id: str) -> int:
    """How many notes that were added earlier are still waiting or being processed."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return 0
        return sum(1 for j in _jobs.values()
                   if j["id"] != job_id and j["status"] in ("queued", "processing") and j["created"] < job["created"])


# ------------------------------------------------------------ list and search

def _summary_row(j: dict) -> dict:
    return {k: j[k] for k in ("id", "filename", "created", "status", "duration")} | {
        "title": (j.get("notes") or {}).get("title")}


def _snippet(text: str, term: str, before: int = 40, after: int = 90) -> str:
    i = text.lower().find(term)
    if i < 0:
        return ""
    a, b = max(0, i - before), min(len(text), i + len(term) + after)
    if a:
        a = text.rfind(" ", 0, a + 1) + 1 if text.rfind(" ", 0, a + 1) >= 0 else a
    out = text[a:b].strip()
    return ("..." if a > 0 else "") + out + ("..." if b < len(text) else "")


def _searchable(j: dict) -> list[tuple[str, str]]:
    """Pieces of a note that a search looks through, as (kind, text)."""
    notes = j.get("notes") or {}
    pieces = [("title", notes.get("title") or ""), ("filename", j.get("filename") or ""),
              ("summary", notes.get("summary") or "")]
    for sec in notes.get("sections") or []:
        pieces.append(("heading", sec["heading"]))
        pieces += [("bullet", b) for b in sec["bullets"]]
    pieces += [("action", a) for a in notes.get("actions") or []]
    if j.get("transcript"):
        pieces.append(("transcript", j["transcript"]))
    return pieces


def list_jobs(query: str = "") -> list[dict]:
    """Notes, newest first. With a query, only notes that contain every word of it, with a snippet."""
    terms = [t for t in re.split(r"\s+", (query or "").lower().strip()) if t][:8]
    with _lock:
        items = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
        rows = []
        for j in items:
            row = _summary_row(j)
            if terms:
                pieces = _searchable(j)
                hay = "\n".join(t for _, t in pieces).lower()
                if not all(t in hay for t in terms):
                    continue
                title_hit = all(t in (row["title"] or j["filename"]).lower() for t in terms)
                snippet = ""
                for kind, text in pieces:
                    if kind in ("title", "filename"):
                        continue
                    snippet = _snippet(text, terms[0])
                    if snippet:
                        break
                row["snippet"] = snippet
                row["_rank"] = 0 if title_hit else 1
            rows.append(row)
    if terms:
        rows.sort(key=lambda r: r["_rank"])  # stable: newest first inside each group
        for r in rows:
            r.pop("_rank")
    return rows


def delete(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] == "processing":
            return False
        _jobs.pop(job_id)
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return True


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", title or "")).strip()[:MAX_TITLE].strip()


def rename(job_id: str, title: str) -> dict | None:
    """Give a finished note a new title. Raises ValueError for an empty title, returns None if unknown."""
    title = clean_title(title)
    if not title:
        raise ValueError("The title cannot be empty.")
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "done" or not job.get("notes"):
            return None
        job["notes"] = {**job["notes"], "title": title}
        job["custom_title"] = title  # a rebuild keeps a title the user chose
        _save(job)
        return dict(job)


def _line(value, limit: int) -> str:
    """One line of text: control characters and runs of spaces removed, cut at `limit`."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(value if value is not None else ""))
    return re.sub(r"\s+", " ", text).strip()[:limit].strip()


def _items(value, limit: int, cap: int, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list.")
    out: list[str] = []
    for item in value:
        text = re.sub(r"^(?:(?:[\-\*\u2022]|\[[ xX]?\])\s+)+", "", _line(item, limit + 4))[:limit].strip()
        if text and text.lower() not in {o.lower() for o in out}:
            out.append(text)
    return out[:cap]


def edit_notes(job_id: str, raw: dict) -> dict | None:
    """Replace the notes of a finished note with a corrected version (the reader edited the text).

    Raises ValueError when the new text is unusable, returns None if the note is unknown or not finished.
    Empty points are dropped, an empty section disappears, and at least one section must remain.
    """
    title = clean_title(_line(raw.get("title"), MAX_TITLE))
    if not title:
        raise ValueError("The title cannot be empty.")
    if not isinstance(raw.get("sections") or [], list):
        raise ValueError("The sections must be a list.")
    sections = []
    for sec in (raw.get("sections") or [])[:30]:
        if not isinstance(sec, dict):
            continue
        bullets = _items(sec.get("bullets"), 600, 30, "The points")
        if bullets:
            sections.append({"heading": _line(sec.get("heading"), 100) or "Notes", "bullets": bullets})
    if not sections:
        raise ValueError("Keep at least one section with a point in it.")
    notes = {
        "title": title,
        "summary": _line(raw.get("summary"), 2000),
        "key_points": _items(raw.get("key_points"), 400, 8, "The key points"),
        "sections": sections,
        "actions": _items(raw.get("actions"), 300, 20, "The action items"),
    }
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "done" or not job.get("notes"):
            return None
        if title != job["notes"].get("title"):
            job["custom_title"] = title  # a rebuild keeps a title the user chose
        job["notes"] = notes
        job["edited"] = True
        _save(job)
        return dict(job)


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
        _save(job)


def _stage(job_id: str, stage: str, **fields) -> None:
    _update(job_id, stage=stage, stage_started=time.time(), **fields)


def start(job_id: str) -> threading.Thread:
    t = threading.Thread(target=run, args=(job_id,), daemon=True)
    t.start()
    return t


def retry(job_id: str) -> bool:
    """Run a failed note again from its saved upload (or from its transcript when it has one)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "error" or not job.get("retryable"):
            return False
        if not upload_path(job_id).exists() and not job.get("transcript"):
            return False
        job.update(status="queued", stage="queued", stage_started=time.time(), progress=0.0,
                   detail="Waiting to start", error=None, retryable=False)
        _save(job)
    start(job_id)
    return True


def restructure(job_id: str) -> bool:
    """Build the notes of a finished note again from its saved transcript, with the engine available now.

    The earlier notes stay in place until the new ones are ready, and are kept if the rebuild fails.
    """
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] != "done" or not job.get("notes") or not (job.get("transcript") or "").strip():
            return False
        job.update(status="queued", stage="queued", stage_started=time.time(), progress=0.0, detail="Waiting to start",
                   error=None, retryable=False, restructure=True, rebuild_error=None)
        _save(job)
    start(job_id)
    return True


def _fail(job_id: str, message: str, retryable: bool) -> None:
    """Mark a job as failed, except for a rebuild, which falls back to the notes it already had."""
    with _lock:
        job = _jobs.get(job_id)
        if job is not None and job.get("restructure") and job.get("notes"):
            job.update(status="done", stage="done", progress=1.0, detail="Done", error=None, retryable=False,
                       restructure=False, rebuild_error=f"The notes could not be rebuilt: {message} The earlier notes are kept.")
            _save(job)
            return
    _update(job_id, status="error", error=message, detail="Failed", retryable=retryable)


def run(job_id: str) -> None:
    """Pipeline: convert and split, transcribe, structure. Never raises."""
    _update(job_id, plan=structure.engine_plan())  # so a waiting note already says how it will be structured
    with _worker_slot:
        job = get(job_id)
        if job is None:  # deleted while it was waiting in the queue
            return
        src = upload_path(job_id)
        work = _job_dir(job_id) / "chunks"
        keep_upload = False
        try:
            _update(job_id, status="processing", plan=structure.engine_plan())
            language = languages.lenient(job.get("language"))
            text = job.get("transcript") or ""
            if not text.strip():
                if not src.exists():
                    raise structure.StructureError("The recording is no longer on the server.")
                _stage(job_id, "converting", progress=0.02, detail="Preparing the audio")
                duration = audio.probe_duration(src)
                chunks, overlaps = audio.split_audio_ex(src, work, duration)
                _stage(job_id, "transcribing", duration=duration, progress=0.1,
                       detail=f"Transcribing part 1 of {len(chunks)}")

                def on_transcribe(done: int, total: int) -> None:
                    label = f"Transcribing part {min(done + 1, total)} of {total}" if done < total else "Transcription finished"
                    _update(job_id, progress=0.1 + 0.6 * done / total, detail=label)

                try:
                    text = transcribe.transcribe_chunks(chunks, on_transcribe, overlaps=overlaps, language=language)
                except transcribe.TranscriptionError:
                    keep_upload = True
                    raise
                if not text.strip():
                    raise structure.StructureError("No speech was recognised in this recording.")
                _update(job_id, transcript=text)

            plan = structure.engine_plan()
            kind = plan["engine"].split(":", 1)[0]
            using_model = kind in ("groq", "ollama")
            who = ("Groq model " if kind == "groq" else "the local model ") + plan["model"]
            _stage(job_id, "structuring", plan=plan, progress=0.72,
                   detail=(f"Waiting for {who}") if using_model
                   else "Structuring with the built-in rules")

            def on_structure(done: int, total: int) -> None:
                if not using_model:
                    return
                label = f"{who[0].upper()}{who[1:]}: part {min(done + 1, total)} of {total}" if done < total else "Structuring the notes"
                _update(job_id, progress=0.72 + 0.26 * done / total, detail=label)

            report: dict = {}
            notes, engine = structure.structure_transcript(text, on_structure, language=language, report=report)
            log.info("note %s: structured with %s (language %s)", job_id, engine, language)
            custom = (get(job_id) or {}).get("custom_title")
            if custom:
                notes = {**notes, "title": custom}
            _update(job_id, notes=notes, engine=engine, engine_detail=report.get("fallback_reason") or None,
                    status="done", stage="done", progress=1.0, detail="Done", retryable=False,
                    restructure=False, rebuild_error=None, edited=False)
        except (audio.AudioError, structure.StructureError) as exc:
            _fail(job_id, str(exc), False)
        except transcribe.TranscriptionError as exc:
            _fail(job_id, f"{exc} You can try again.", True)
        except Exception:  # unexpected: keep details out of the response
            keep_upload = True
            _fail(job_id, "Something went wrong while processing this file. You can try again.", True)
        finally:
            if not keep_upload:
                src.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)
