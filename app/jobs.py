"""Note jobs: state, persistence on disk and the processing pipeline."""
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import audio, structure, transcribe
from .config import settings

STAGES = ["queued", "converting", "transcribing", "structuring", "done"]

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


def load_all() -> None:
    """Load saved jobs at startup. Jobs that were running are marked as interrupted."""
    with _lock:
        _jobs.clear()
        root = settings.data_dir / "jobs"
        if not root.exists():
            return
        for f in root.glob("*/job.json"):
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("status") in ("queued", "processing"):
                job.update(status="error", error="The server restarted before this note was finished.")
                _save(job)
            _jobs[job["id"]] = job


def new_job(filename: str) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "filename": filename[:200],
        "created": time.time(),
        "status": "queued",
        "stage": "queued",
        "progress": 0.0,
        "detail": "Waiting to start",
        "error": None,
        "duration": None,
        "transcript": None,
        "notes": None,
        "engine": None,
    }
    with _lock:
        _jobs[job["id"]] = job
        _save(job)
    return job


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def list_jobs() -> list[dict]:
    with _lock:
        items = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
        return [
            {k: j[k] for k in ("id", "filename", "created", "status", "duration")}
            | {"title": (j.get("notes") or {}).get("title")}
            for j in items
        ]


def delete(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["status"] == "processing":
            return False
        _jobs.pop(job_id)
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return True


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
        _save(job)


def upload_path(job_id: str) -> Path:
    return _job_dir(job_id) / "upload.bin"


def start(job_id: str) -> threading.Thread:
    t = threading.Thread(target=run, args=(job_id,), daemon=True)
    t.start()
    return t


def run(job_id: str) -> None:
    """Pipeline: convert and split, transcribe, structure. Never raises."""
    with _worker_slot:
        if get(job_id) is None:  # deleted while it was waiting in the queue
            return
        src = upload_path(job_id)
        work = _job_dir(job_id) / "chunks"
        try:
            _update(job_id, status="processing", stage="converting", progress=0.02,
                    detail="Preparing the audio")
            duration = audio.probe_duration(src)
            chunks = audio.split_audio(src, work, duration)
            _update(job_id, duration=duration, stage="transcribing", progress=0.1,
                    detail=f"Transcribing part 1 of {len(chunks)}")

            def on_transcribe(done: int, total: int) -> None:
                label = f"Transcribing part {min(done + 1, total)} of {total}" if done < total else "Transcription finished"
                _update(job_id, progress=0.1 + 0.6 * done / total, detail=label)

            text = transcribe.transcribe_chunks(chunks, on_transcribe)
            if not text.strip():
                raise structure.StructureError("No speech was recognised in this recording.")
            _update(job_id, transcript=text, stage="structuring", progress=0.72,
                    detail="Structuring the notes")

            def on_structure(done: int, total: int) -> None:
                label = f"Structuring part {min(done + 1, total)} of {total}" if total > 1 and done < total else "Structuring the notes"
                _update(job_id, progress=0.72 + 0.26 * done / total, detail=label)

            notes, engine = structure.structure_transcript(text, on_structure)
            _update(job_id, notes=notes, engine=engine, status="done", stage="done",
                    progress=1.0, detail="Done")
        except (audio.AudioError, transcribe.TranscriptionError, structure.StructureError) as exc:
            _update(job_id, status="error", error=str(exc), detail="Failed")
        except Exception:  # unexpected: keep details out of the response
            _update(job_id, status="error", error="Something went wrong while processing this file.",
                    detail="Failed")
        finally:
            src.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)
