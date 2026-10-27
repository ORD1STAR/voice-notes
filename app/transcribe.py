"""Transcription through the Groq Whisper API."""
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import httpx

from .config import settings


class TranscriptionError(Exception):
    """Raised with a message that is safe to show to the user."""


def _norm(word: str) -> str:
    return re.sub(r"[\W_]+", "", word.lower())


def merge_overlap(previous: str, following: str, window: int = 14) -> str:
    """Remove from `following` the words it repeats from the end of `previous`.

    Used when a chunk was started a little early, so that the words around the cut appear in
    both transcripts. The match is fuzzy because the two passes over the same speech do not
    always write it identically. Without a convincing match the text is returned unchanged.
    """
    a, b = previous.split(), following.split()
    if not a or not b:
        return following
    tail, head = [_norm(w) for w in a[-window:]], [_norm(w) for w in b[:window]]
    m = SequenceMatcher(None, tail, head, autojunk=False).find_longest_match(0, len(tail), 0, len(head))
    chars = sum(len(w) for w in head[m.b:m.b + m.size])
    near_start = m.b <= 3 and m.a + m.size >= len(tail) - 3
    if m.size >= 3 and near_start or m.size >= 2 and chars >= 8 and m.b <= 2 and m.a + m.size >= len(tail) - 2:
        return " ".join(b[m.b + m.size:])
    return following


def join_transcripts(parts: list[str], overlaps: list[float] | None = None) -> str:
    """Join chunk transcripts into one text, dropping empty parts.

    `overlaps[i]` is how many seconds chunk i repeats from the chunk before it. Where it is
    above zero, words repeated at the seam are removed.
    """
    out = ""
    for i, p in enumerate(parts):
        text = re.sub(r"\s+", " ", p).strip()
        if not text:
            continue
        if out and overlaps and i < len(overlaps) and overlaps[i] > 0:
            text = merge_overlap(out, text)
        out = f"{out} {text}".strip() if text else out
    return out


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
    overlaps: list[float] | None = None,
) -> str:
    parts = []
    for i, chunk in enumerate(chunks):
        if on_progress:
            on_progress(i, len(chunks))
        parts.append(transcribe_file(chunk, client=client))
    if on_progress:
        on_progress(len(chunks), len(chunks))
    return join_transcripts(parts, overlaps)
