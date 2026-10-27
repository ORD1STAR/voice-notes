"""Transcription through the Groq Whisper API."""
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import httpx

from . import languages
from .config import settings


class TranscriptionError(Exception):
    """Raised with a message that is safe to show to the user."""


def _norm(word: str) -> str:
    return re.sub(r"[\W_]+", "", word.lower())


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff]")
_CJK_EDGE = re.compile(r"[\u3000-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _is_cjk(text: str) -> bool:
    """Chinese and Japanese are written without spaces, so their words cannot be told apart by splitting."""
    letters = sum(1 for c in text if c.isalpha())
    return bool(letters) and len(_CJK.findall(text)) / letters > 0.5


def _tokens(text: str, cjk: bool) -> list[tuple[str, int]]:
    """(normalised word, offset of its end in the text); one token per character for Chinese and Japanese."""
    if cjk:
        return [(n, i + 1) for i, ch in enumerate(text) if (n := _norm(ch))]
    return [(_norm(m.group()), m.end()) for m in re.finditer(r"\S+", text)]


def merge_overlap(previous: str, following: str, window: int = 14) -> str:
    """Remove from `following` the words it repeats from the end of `previous`.

    Used when a chunk was started a little early, so that the words around the cut appear in
    both transcripts. The match is fuzzy because the two passes over the same speech do not
    always write it identically. Without a convincing match the text is returned unchanged.
    """
    cjk = _is_cjk(previous) and _is_cjk(following)
    a, b = _tokens(previous, cjk), _tokens(following, cjk)
    if not a or not b:
        return following
    window = 24 if cjk else window
    tail, head = [w for w, _ in a[-window:]], [w for w, _ in b[:window]]
    m = SequenceMatcher(None, tail, head, autojunk=False).find_longest_match(0, len(tail), 0, len(head))
    chars = sum(len(w) for w in head[m.b:m.b + m.size])
    if cjk:  # single characters are weak evidence, so ask for more of them
        found = m.size >= 4 and m.b <= 4 and m.a + m.size >= len(tail) - 4
    else:
        near_start = m.b <= 3 and m.a + m.size >= len(tail) - 3
        found = m.size >= 3 and near_start or m.size >= 2 and chars >= 8 and m.b <= 2 and m.a + m.size >= len(tail) - 2
    if found:
        return " ".join(following[b[m.b + m.size - 1][1]:].split())
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
        if not text:
            continue
        glue = "" if out and _CJK_EDGE.match(out[-1]) and _CJK_EDGE.match(text[0]) else " "  # no space inside Chinese
        out = f"{out}{glue}{text}".strip()
    return out


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("retry-after")
    try:
        if header:
            return min(float(header), 60.0)
    except ValueError:
        pass
    return min(2.0 ** attempt * 2, 30.0)


def transcribe_file(path: Path, client: httpx.Client | None = None, retries: int = 3,
                    language: str | None = None) -> str:
    """Send one audio file to Groq and return its text.

    `language` is an ISO 639-1 code. "auto" leaves the parameter out so Whisper detects the language.
    None uses the server default (TRANSCRIPTION_LANGUAGE), which is automatic detection when unset.
    """
    key = settings.groq_api_key
    if not key:
        raise TranscriptionError("The server has no GROQ_API_KEY configured.")
    data = {"model": settings.groq_model, "response_format": "json", "temperature": "0"}
    code = languages.lenient(settings.language if language is None else language)  # "zh", "ar", ...; unknown means auto
    if code != languages.AUTO:
        data["language"] = code
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
                except (ValueError, AttributeError):
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
    language: str | None = None,
) -> str:
    parts = []
    for i, chunk in enumerate(chunks):
        if on_progress:
            on_progress(i, len(chunks))
        parts.append(transcribe_file(chunk, client=client, language=language))
    if on_progress:
        on_progress(len(chunks), len(chunks))
    return join_transcripts(parts, overlaps)
