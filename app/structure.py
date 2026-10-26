"""Turn a raw transcript into structured notes.

The preferred engine is a small local model served by Ollama. When Ollama is
not running, or the model is missing, or a call fails, a rule-based fallback
is used so a transcript always ends up as readable notes.

Notes have this shape:
    {"title": str, "summary": str, "sections": [{"heading": str, "bullets": [str]}]}
"""
import json
import re
from collections import Counter
from typing import Callable

import httpx

from .config import settings

STOPWORDS = set(
    """a about above after again all also am an and any are as at be because been before being below between
    both but by can could did do does doing down during each few for from further had has have having he her
    here hers him his how i if in into is it its just me more most my no nor not now of off on once only or
    other our out over own same she should so some such than that the their them then there these they this
    those through to too under until up very was we were what when where which while who why will with would
    you your yeah okay ok like really actually maybe going got get gonna kind sort thing things one two
    le la les un une des du de et en est que qui dans pour pas sur ce cette ces au aux avec ne se son sa ses
    il elle ils elles on nous vous je tu mais ou donc car si comme plus tout tous très aussi bien fait faire
    el los las una por con para como pero más""".split()
)

MAX_PIECE_CHARS = 3500


class StructureError(Exception):
    pass


# ----------------------------------------------------------------- helpers

def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+(?=\S)", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        # Transcripts without punctuation: cut long runs every ~28 words.
        words = p.split()
        if len(words) > 45:
            for i in range(0, len(words), 28):
                out.append(" ".join(words[i:i + 28]))
        else:
            out.append(p)
    return out


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]{3,}", text.lower()) if w not in STOPWORDS]


def split_pieces(text: str, limit: int = MAX_PIECE_CHARS) -> list[str]:
    """Cut text into pieces of at most about `limit` characters on sentence boundaries."""
    pieces, current = [], ""
    for s in split_sentences(text):
        if current and len(current) + len(s) + 1 > limit:
            pieces.append(current)
            current = s
        else:
            current = f"{current} {s}".strip()
    if current:
        pieces.append(current)
    return pieces


def _clip(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "..."


def normalize_notes(raw: dict) -> dict:
    """Validate and clean a notes dict. Raises StructureError when unusable."""
    if not isinstance(raw, dict):
        raise StructureError("notes are not an object")
    title = str(raw.get("title") or "").strip().strip('"')
    summary = str(raw.get("summary") or "").strip()
    sections = []
    for sec in raw.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("heading") or "").strip().strip("#").strip()
        bullets = []
        for b in sec.get("bullets") or []:
            b = re.sub(r"^[\-\*•]\s*", "", str(b).strip())
            if b:
                bullets.append(b)
        if bullets:
            sections.append({"heading": heading or "Notes", "bullets": bullets[:12]})
    if not sections:
        raise StructureError("no sections")
    return {"title": _clip(title, 100) or "Untitled note", "summary": summary, "sections": sections[:30]}


# ------------------------------------------------------------- rule based

def structure_rules(text: str) -> dict:
    sentences = split_sentences(text)
    if not sentences:
        raise StructureError("The transcript is empty.")
    freq = Counter(w for s in sentences for w in _words(s))

    def score(s: str) -> float:
        ws = _words(s)
        return sum(freq[w] for w in ws) / (len(ws) ** 0.6) if ws else 0.0

    n_summary = 2 if len(sentences) < 12 else 3
    top = sorted(range(len(sentences)), key=lambda i: score(sentences[i]), reverse=True)[:n_summary]
    summary = " ".join(_clip(sentences[i], 240) for i in sorted(top))

    # Group sentences into sections of about five sentences.
    size = 5 if len(sentences) > 6 else max(len(sentences), 1)
    groups = [sentences[i:i + size] for i in range(0, len(sentences), size)]
    if len(groups) > 1 and len(groups[-1]) == 1:
        groups[-2].extend(groups.pop())

    used: set[str] = set()
    sections = []
    for g in groups:
        local = Counter(w for s in g for w in _words(s))
        ranked = [w for w, _ in sorted(local.items(), key=lambda kv: (-kv[1] * (1 + freq[kv[0]] / 10), kv[0]))]
        picked = [w for w in ranked if w not in used][:2] or ranked[:2]
        used.update(picked)
        heading = " and ".join(w.capitalize() for w in picked) or "Notes"
        sections.append({"heading": heading, "bullets": [_clip(s, 260) for s in g]})

    first = sentences[0]
    title = _clip(" ".join(first.split()[:8]).rstrip(".,;:!?"), 70)
    return {"title": title, "summary": summary, "sections": sections}


# ---------------------------------------------------------------- ollama

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "bullets"],
            },
        },
    },
    "required": ["title", "summary", "sections"],
}

SYSTEM_PROMPT = (
    "You turn the transcript of a spoken voice note into clean written notes. "
    "Write in the same language as the transcript. Stay faithful to what was said: "
    "do not add facts, names or numbers that are not in the text. "
    "Return JSON with: a short title (at most 8 words), a summary of two or three sentences, "
    "and sections. Each section has a short heading and 2 to 6 bullets. "
    "Bullets are concise sentences. Remove filler words and repetitions."
)

SUMMARY_PROMPT = (
    "You receive the summaries of consecutive parts of one long voice note. "
    "Write in the same language. Return JSON with a short title (at most 8 words), "
    "a summary of three or four sentences covering the whole note, and an empty sections list."
)


def ollama_status() -> dict:
    """Report whether Ollama answers and has the configured model."""
    if not settings.use_ollama:
        return {"enabled": False, "available": False, "model": settings.ollama_model, "reason": "disabled"}
    try:
        r = httpx.get(f"{settings.ollama_host}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return {"enabled": True, "available": False, "model": settings.ollama_model, "reason": "not reachable"}
    want = settings.ollama_model
    ok = any(n == want or n.split(":")[0] == want or (":" not in want and n.startswith(want + ":")) for n in names)
    return {"enabled": True, "available": ok, "model": want, "reason": "" if ok else "model not installed"}


def _ollama_chat(system: str, user: str) -> dict:
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "format": NOTES_SCHEMA,
        "options": {"temperature": 0.2, "num_ctx": 4096},
        "keep_alive": "10m",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    try:
        r = httpx.post(f"{settings.ollama_host}/api/chat", json=payload, timeout=settings.ollama_timeout)
        r.raise_for_status()
        content = r.json()["message"]["content"]
        return json.loads(content)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise StructureError(f"ollama call failed: {type(exc).__name__}")


def structure_ollama(text: str, on_progress: Callable[[int, int], None] | None = None) -> dict:
    pieces = split_pieces(text)
    if not pieces:
        raise StructureError("The transcript is empty.")
    results = []
    for i, piece in enumerate(pieces):
        if on_progress:
            on_progress(i, len(pieces))
        results.append(normalize_notes(_ollama_chat(SYSTEM_PROMPT, f"Transcript:\n{piece}")))
    if on_progress:
        on_progress(len(pieces), len(pieces))
    if len(results) == 1:
        return results[0]
    sections = [s for r in results for s in r["sections"]]
    digest = "\n".join(f"Part {i + 1}: {r['summary']}" for i, r in enumerate(results))
    try:
        head = _ollama_chat(SUMMARY_PROMPT, digest)
        title = str(head.get("title") or "").strip()
        summary = str(head.get("summary") or "").strip()
    except StructureError:
        title, summary = "", ""
    if not title or not summary:
        fallback = structure_rules(text)
        title = title or fallback["title"]
        summary = summary or fallback["summary"]
    return {"title": _clip(title, 100), "summary": summary, "sections": sections[:30]}


# ------------------------------------------------------------------ entry

def structure_transcript(
    text: str, on_progress: Callable[[int, int], None] | None = None
) -> tuple[dict, str]:
    """Return (notes, engine). The engine is 'ollama:<model>' or 'rule-based'."""
    text = (text or "").strip()
    if not text:
        raise StructureError("The recording contains no recognisable speech.")
    status = ollama_status()
    if status["available"]:
        try:
            return structure_ollama(text, on_progress), f"ollama:{settings.ollama_model}"
        except StructureError:
            pass
    return structure_rules(text), "rule-based"
