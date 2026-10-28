"""Remove what only makes sense when it is spoken.

A voice note is full of talk aimed at a listener: "Explique moi comment ça marche", "Tell me about the plan",
"Écoute, voilà ce que je pense", "In this note I am going to talk about the garden". Written notes keep the
content of such sentences and lose the framing. `unspeak` rewrites one sentence (it returns an empty string when
nothing is left) and `scrub` does the same for a text of several sentences. Both work for English, French and
Spanish and leave every other sentence exactly as it is.
"""
from __future__ import annotations

import re

_APOS = str.maketrans({"’": "'", "‘": "'"})

# --- requests addressed to a listener ("explain to me ...") ------------------------------------------------
_ASK = (
    r"(?:explique[zr]?[- ]?(?:moi|nous)|dis[- ]?moi|dites[- ]?moi|raconte[- ]?moi|racontez[- ]?moi|montre[- ]?moi"
    r"|montrez[- ]?moi|donne[- ]?moi|donnez[- ]?moi|aide[- ]?moi|aidez[- ]?moi|fais[- ]?moi|faites[- ]?moi"
    r"|parle[- ]?moi|parlez[- ]?moi|r[eé]sume[- ]?moi|r[eé]sumez[- ]?moi|expliquez|explique"
    r"|explain(?: to me| it to me)?|tell me|show me|give me|help me|walk me through|summari[sz]e for me"
    r"|let me (?:explain|tell you|say|show you)"
    r"|expl[ií]came|expl[ií]quenme|d[ií]me|cu[eé]ntame|mu[eé]strame|ay[uú]dame|resum[eé]me|h[aá]blame)"
)
_POLITE = (
    r"(?:(?:can|could|would|will) you(?: please)?|please|peux[- ]tu|pourrais[- ]tu|peux[- ]tu me|pouvez[- ]vous"
    r"|pourriez[- ]vous|s'il (?:te|vous) pla[iî]t|puedes|podr[ií]as|puede usted|por favor)"
)
_PREFIX_ASK = re.compile(r"^(?:(?:please|s'il (?:te|vous) pla[iî]t|por favor)[ ,]+)?" + _ASK + r"\b[\s,:]*", re.IGNORECASE)
_PREFIX_POLITE = re.compile(r"^" + _POLITE + r"\b[\s,:]*(?:(?:me|te|nous|vous|moi)\s+)?", re.IGNORECASE)
_QUESTION_WORD = re.compile(
    r"^(?:comment|pourquoi|quand|combien|quel(?:le)?s?|qu'est-ce|ce que|ce qui|si|how|why|when|what|which|who|whether"
    r"|if|c[oó]mo|por qu[eé]|cu[aá]ndo|cu[aá]nto|qu[eé]|cu[aá]l)\b",
    re.IGNORECASE)
_TOPIC_LEAD = re.compile(
    r"^(?:about|regarding|concerning|à propos de|a propos de|au sujet de|sur|de|du|des|d'|sobre|acerca de)\s+",
    re.IGNORECASE)

# --- announcements of what the speaker is about to do ---------------------------------------------------------------
_SUBJECT = (
    r"(?:i(?: am|'m)? (?:going to|gonna|will|want to|wanted to|would like to|'d like to|'ll)"
    r"|je (?:vais|voulais|veux|voudrais|souhaite)|quiero|quer[ií]a|voy a)"
)
_ANNOUNCE = re.compile(
    r"^(?:(?:in|dans|en) (?:this|cette|ce|esta|este) (?:note|video|v[ií]deo|vocal|message|recording|enregistrement"
    r"|audio|nota)[ ,]+|(?:so|alors|donc)[ ,]+)?" + _SUBJECT + r" +(?:quickly +|rapidement +|just +|juste +)?"
    r"(?:(?:talk(?: to you)?|speak|tell you|parler|vous parler|te parler|hablar)"
    r"(?: +(?:about|of|de|du|des|sur|d'|[aà] propos de|sobre))"
    r"|(?:explain|expliquer|vous expliquer|t'expliquer|explicar)(?: +(?:how|what|why|comment|ce que|pourquoi|c[oó]mo)))\b",
    re.IGNORECASE)

# --- leading discourse markers and trailing tags ---------------------------------------------------------------
_LEAD = re.compile(
    r"^(?:[ée]coute(?:z)?(?: bien| moi)?|tiens|dis|dites|tu vois|vous voyez|du coup|en fait|enfin|bref|enfin bref|voil[aà]"
    r"|listen|look|see|well|you see|so yeah|so basically|escucha|mira|bueno|oye)\s*[,:]\s*",
    re.IGNORECASE)
_TRAIL = re.compile(
    r"\s*[,;]?\s*(?:tu vois|vous voyez|tu comprends|vous comprenez|n'est-ce pas|d'accord|hein|si tu veux"
    r"|you see|you know what i mean|if you know what i mean|do you see|does that make sense|got it|capito|verdad"
    r"|vale)\s*(?=[.?!…]*$)",
    re.IGNORECASE)

# --- sentences that are only framing ---------------------------------------------------------------------------------
_PURE = re.compile(
    r"^(?:et )?(?:voil[aà](?: tout| c'est tout| ce que je voulais dire| ce que je voulais te dire)?|bref|enfin bref"
    r"|[ée]coute(?:z)?(?: bien| moi)?|tu vois ce que je veux dire|vous voyez ce que je veux dire|tu comprends|tu me suis"
    r"|voil[aà] (?:ce que|comment) je (?:pense|voulais dire|veux dire|ferais)|d'accord|n'est-ce pas|on verra|on va voir|let me explain|let me think|let's see|you see what i mean"
    r"|(?:so )?here(?:'s| is) the (?:thing|deal|idea)|here(?:'s| is) what i (?:think|mean)|are you following"
    r"|do you understand|as i (?:said|mentioned)(?: before| earlier| already)?|like i said(?: before| earlier)?"
    r"|comme je (?:disais|l'ai dit|te le disais)(?: tout à l'heure| avant)?|voici (?:l'id[ée]e|ce que je pense)"
    r"|como (?:te )?dije|ya ves|ya sabes|ya est[aá]|eso es todo|a ver)$",
    re.IGNORECASE)


# --- French and Spanish fillers (the English ones are handled by the rules in structure.py) ------------------------
_FR_ES_LEAD = re.compile(
    r"^(?:(?:et )?(?:donc|alors|entonces|o sea|en fait|du coup)(?:\s+(?:en fait|du coup|donc|alors))*\b\s*,?\s*"
    r"|(?:bon|ben|bah|bref|enfin bref|enfin|sinon|bueno|pues|eh bien)(?:\s+(?:en fait|du coup|donc|alors|bon))*\s*,\s*)",
    re.IGNORECASE)
_FR_ES_INLINE = re.compile(
    r"(?:,\s*)?\b(?:euh+|heu+|hum+|hmm+|eh bien|du coup|en fait|tu vois|vous voyez|disons|o sea|pues nada)\b\s*,?\s*"
    r"|,\s*(?:quoi|hein|bah|genre|vale|\u00bfsabes\?)(?=[.?!]?$)",
    re.IGNORECASE)
_FR_ES_HEDGE = re.compile(
    r"^(?:je (?:pense|crois|trouve|suppose|dirais|me dis)|j'ai l'impression|[aà] mon avis|moi je pense|yo creo|creo|pienso|me parece)"
    r"\s+(?:que|qu'|qu\u2019)\s*",
    re.IGNORECASE)
_REPEAT = re.compile(r"\b((?:[\w']+\s+){0,2}[\w']+)(?:\s*,)?\s+\1\b", re.IGNORECASE)


def drop_fillers(sentence: str) -> str:
    """French and Spanish speech fillers ("euh", "du coup", "en fait", "je pense que") and repeated starts."""
    s = sentence.translate(_APOS)
    s = _FR_ES_LEAD.sub("", s, count=1)
    s = _FR_ES_INLINE.sub(lambda m: ", " if m.group(0).lstrip().startswith(",") and m.end() < len(s) else " ", s)
    s = _FR_ES_HEDGE.sub("", s, count=1)
    prev = None
    while prev != s:  # "c'est que, c'est que souvent" -> "c'est que souvent"
        prev = s
        s = _REPEAT.sub(lambda m: m.group(1) if len(m.group(1)) > 3 else m.group(0), s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    return s[:1].upper() + s[1:] if s else ""


def _plain(sentence: str) -> str:
    s = re.sub(r"[.!?,;:…]+", " ", sentence.translate(_APOS).lower())
    return re.sub(r"\s+", " ", s).strip()


def _end_mark(original: str) -> str:
    m = re.search(r"[.!?…]+$", original.strip())
    return m.group(0) if m else ""


def _finish(text: str, mark: str) -> str:
    text = text.strip(" ,;:-")
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if re.search(r"[.!?\u2026]$", text):
        return text
    return text + (mark if mark and mark != "?" else ".")


def unspeak(sentence: str) -> str:
    """One sentence without its spoken framing. Empty when nothing of value is left."""
    original = (sentence or "").strip()
    if not original:
        return ""
    s = original.translate(_APOS)
    mark = _end_mark(s)
    if _PURE.match(_plain(s)):
        return ""
    for _ in range(2):
        s = _LEAD.sub("", s, count=1)
    announce = _ANNOUNCE.match(s)
    if announce:  # "In this note I will talk about X": the announcement says nothing, the topic is named elsewhere
        return ""
    ask = _PREFIX_ASK.match(s)
    if ask:
        rest = s[ask.end():].strip()
        if not _QUESTION_WORD.match(rest):
            rest = _TOPIC_LEAD.sub("", rest, count=1)
        return _finish(rest, mark) if len(rest.split()) >= 3 else ""
    polite = _PREFIX_POLITE.match(s)
    if polite and polite.end() < len(s):
        rest = s[polite.end():].strip()
        rest = re.sub(r"\s*\?$", "", rest)  # a polite request becomes a plain instruction
        return _finish(rest, "." if mark == "?" else mark) if len(rest.split()) >= 2 else ""
    trimmed = _TRAIL.sub("", s) if len(s.split()) > 4 else s
    if trimmed != s:
        s = trimmed.rstrip(" ,;")
        if mark and not s.endswith(tuple(".!?…")):
            s += mark
    if s == original.translate(_APOS):
        return original  # untouched: keep the speaker's own characters (curly quotes included)
    return s[:1].upper() + s[1:] if s else ""


_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÖØ-Þ¿¡\"'(])")


def scrub(text: str) -> str:
    """`unspeak` for a text of one or more sentences (a summary, a title, a bullet written as a paragraph)."""
    text = (text or "").strip()
    if not text:
        return ""
    parts = [unspeak(p) for p in _SPLIT.split(text)]
    return " ".join(p for p in parts if p)
