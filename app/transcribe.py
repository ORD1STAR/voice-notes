"""Transcription through the Groq Whisper API."""
import re
import time
from pathlib import Path
from typing import Callable

import httpx

from .config import settings


class TranscriptionError(Exception):
    """Raised with a message that is safe to show to the user."""


def join_transcripts(parts: list[str]) -> str:
    """Join chunk transcripts into one text, dropping empty parts."""
    cleaned = [re.sub(r"\s+", " ", p).strip() for p in parts]
    return " ".join(p for p in cleaned if p)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("retry-after")
    try:
        if header:
            return min(float(header), 60.0)
    except ValueError:
        pass
    return min(2.0 ** attempt * 2, 30.0)


def transcribe_file(path: Path, client: httpx.Client | None = None, retries: int = 3) -> str:
    """Send one audio file to Groq and return its text."""
    key = settings.groq_api_key
    if not key:
        raise TranscriptionError("The server has no GROQ_API_KEY configured.")
    data = {"model": settings.groq_model, "response_format": "json", "temperature": "0"}
    if settings.language:
        data["language"] = settings.language
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0))
    try:
        for attempt in range(retries + 1):
            try:
                with open(path, "rb") as fh:
                    resp = client.post(
                        settings.groq_url,
                        headers={"Authorization": f"Bearer {key}"},
                        data=data,
                        files={"file": (path.name, fh, "audio/mpeg")},
                    )
            except httpx.TimeoutException:
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise TranscriptionError("The transcription service did not answer in time.")
            except httpx.HTTPError:
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise TranscriptionError("The transcription service could not be reached.")

            if resp.status_code == 200:
                try:
                    return str(resp.json().get("text", "")).strip()
                except ValueError:
                    raise TranscriptionError("The transcription service sent an unreadable answer.")
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(_retry_delay(resp, attempt))
                continue
            if resp.status_code in (401, 403):
                raise TranscriptionError("Groq rejected the API key. Check GROQ_API_KEY.")
            if resp.status_code == 413:
                raise TranscriptionError("An audio chunk was too large for Groq.")
            if resp.status_code == 429:
                raise TranscriptionError("Groq rate limit reached. Wait a few minutes and try again.")
            raise TranscriptionError(f"The transcription service returned an error ({resp.status_code}).")
        raise TranscriptionError("The transcription failed.")
    finally:
        if own:
            client.close()


def transcribe_chunks(
    chunks: list[Path],
    on_progress: Callable[[int, int], None] | None = None,
    client: httpx.Client | None = None,
) -> str:
    parts = []
    for i, chunk in enumerate(chunks):
        if on_progress:
            on_progress(i, len(chunks))
        parts.append(transcribe_file(chunk, client=client))
    if on_progress:
        on_progress(len(chunks), len(chunks))
    return join_transcripts(parts)
