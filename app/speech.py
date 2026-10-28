"""Remove what only makes sense when it is spoken.

A voice note is full of talk aimed at a listener: "Explique moi comment ça marche", "Tell me about the plan",
"Écoute, voilà ce que je pense", "In this note I am going to talk about the garden", "Okay so, the thing is, we
have no budget". Written notes keep the content of such sentences and lose the framing. `unspeak` rewrites one
sentence (it returns an empty string when nothing is left) and `scrub` does the same for a text of several
sentences. Both work for English, French and Spanish and leave every other sentence exactly as it is.

`has_speech` says whether a line still reads like talk (it speaks to "you", asks a question, says "let's" or
"this note"). The model pass that rewrites such lines uses it to decide which lines to send.
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
# The same verbs once the polite or "I want you to" part in front of them is gone ("can you tell me ...").
_ASK_BARE = (
    r"(?:explain|tell|show|walk me through|expliques?|expliquer|dises|dire|dites|montres?|montrer|racontes?"
    r"|raconter|digas|cuentes|muestres|expliques)"
)
_POLITE = (
    r"(?:(?:can|could|would|will) you(?: please)?|please|peux[- ]tu|pourrais[- ]tu|peux[- ]tu me|pouvez[- ]vous"
    r"|pourriez[- ]vous|tu peux|tu pourrais|vous pouvez|vous pourriez|s'il (?:te|vous) pla[iî]t|puedes|podr[ií]as"
    r"|puede usted|por favor)"
)
_WANT = (
    r"(?:i (?:want|need|would like|would love|'d like|'d love) you to|i'd like you to|i'd love you to"
    r"|je (?:veux|voudrais|souhaite|aimerais) que (?:tu|vous)|j'aimerais que (?:tu|vous)|il faut que (?:tu|vous)"
    r"|quiero que(?: me)?)"
)
_PREFIX_ASK = re.compile(r"^(?:(?:please|s'il (?:te|vous) pla[iî]t|por favor)[ ,]+)?" + _ASK + r"\b[\s,:]*", re.IGNORECASE)
_PREFIX_POLITE = re.compile(r"^" + _POLITE + r"\b[\s,:]*(?:(?:me|te|nous|vous|moi)\s+)?", re.IGNORECASE)
_PREFIX_WANT = re.compile(r"^" + _WANT + r"\s+(?:me\s+|m')?(?=" + _ASK_BARE + r"\b)", re.IGNORECASE)
_BARE_ASK = re.compile(r"^" + _ASK_BARE + r"\b[\s,:]*(?:(?:me|moi|to me|nous)\b[\s,:]*)?", re.IGNORECASE)
_QUESTION_WORD = re.compile(
    r"^(?:comment|pourquoi|quand|combien|quel(?:le)?s?|qu'est-ce|ce que|ce qui|si|how|why|when|what|which|who|whether"
    r"|if|c[oó]mo|por qu[eé]|cu[aá]ndo|cu[aá]nto|qu[eé]|cu[aá]l)\b",
    re.IGNORECASE)
_TOPIC_LEAD = re.compile(
    r"^(?:about|regarding|concerning|à propos de|a propos de|au sujet de|sur|de|du|des|d'|sobre|acerca de)\s+",
    re.IGNORECASE)

# --- announcements of what the speaker is about to do ---------------------------------------------------------------
_SUBJECT = (
    r"(?:i(?: am|'m)?(?: now)? (?:going to|gonna|will|want to|wanted to|would like to|'d like to|'ll)(?: now)?"
    r"|je (?:vais|voulais|veux|voudrais|souhaite)(?: maintenant)?|quiero|quer[ií]a|voy a)"
)
_ANNOUNCE = re.compile(
    r"^(?:(?:in|dans|en) (?:this|cette|ce|esta|este) (?:note|video|v[ií]deo|vocal|message|recording|enregistrement"
    r"|audio|nota)[ ,]+|(?:so|alors|donc|next|now|then|first|finally|lastly|ensuite|maintenant|puis|enfin|okay|ok)[ ,]+)?"
    + _SUBJECT + r" +(?:quickly +|rapidement +|just +|juste +|now +|maintenant +)?"
    r"(?:(?:talk(?: to you)?|speak|tell you|parler|vous parler|te parler|hablar)"
    r"(?: +(?:about|of|de|du|des|sur|d'|[aà] propos de|sobre))"
    r"|(?:explain|expliquer|vous expliquer|t'expliquer|explicar)(?: +(?:how|what|why|comment|ce que|pourquoi|c[oó]mo)))\b",
    re.IGNORECASE)

# --- greetings and leading discourse markers ----------------------------------------------------------------------
_GREETING = re.compile(
    r"^(?:hello|hi|hey|good (?:morning|afternoon|evening)|bonjour|bonsoir|salut|coucou|hola|buenos d[ií]as|buenas)"
    r"(?:\s+(?:everyone|everybody|all|there|tout le monde|[àa] tous|a todos))?\s*(?:[,.!:;]+\s*|$)",
    re.IGNORECASE)
_LEAD = re.compile(
    r"^(?:[ée]coute(?:z)?(?: bien| moi)?|tiens|dis|dites|tu vois|vous voyez|du coup|en fait|enfin|bref|enfin bref|voil[aà]"
    r"|listen|look|see|well|you see|so yeah|so basically|escucha|mira|bueno|oye)\s*[,:]\s*",
    re.IGNORECASE)
# Markers that are dropped only when a comma or a colon follows them.
_FRAME_COMMA = (
    "okay", "ok", "alright", "all right", "right", "well", "now", "anyway", "anyways", "actually", "basically",
    "honestly", "i mean", "you know", "like", "so", "hmm", "hm", "um", "uh", "uhm", "let me see", "let me think",
    "to sum up", "in short", "in summary", "to summarize", "to summarise", "long story short", "in conclusion",
    "i was thinking", "i was thinking that", "regardez", "regarde", "voyons", "voyons voir", "attends", "attendez",
    "bon", "ben", "bah", "euh", "heu", "hum", "voilà", "voila", "bref", "enfin", "enfin bref", "pour résumer",
    "pour resumer", "en résumé", "en resume", "en bref", "en conclusion", "tu vois", "vous voyez",
    "je ne sais plus ce que je voulais dire", "je ne sais plus ce que je disais", "ce que je voulais dire",
    "bueno", "pues", "a ver", "para resumir", "en resumen", "en fin", "eh", "mmm", "oye", "mira", "escucha",
    "como te decía", "como te decia", "como te dije", "je vais te dire", "je vais vous dire", "je te dis",
    "let me tell you", "i'll tell you what", "voy a decirte", "tu vois ce que je veux dire",
    "vous voyez ce que je veux dire", "you know what i mean", "if you know what i mean", "you see what i mean",
    "tu comprends", "vous comprenez", "you see", "do you see", "as i said", "as i said before", "as i said earlier",
    "as i mentioned", "as i mentioned before", "as i mentioned earlier", "like i said", "like i said before",
    "as we discussed", "as discussed", "as you know", "as we said", "as we said before",
)
# Markers that are dropped with or without a comma.
_FRAME_FREE = (
    "okay so", "ok so", "so basically", "so yeah", "so um", "so uh", "and so", "the thing is", "the point is",
    "the idea is", "what i want to say is", "what i want to say is that", "what i mean is", "what i mean is that",
    "what i'm saying is", "what i am saying is", "what i'm saying is that", "the thing is that", "the point is that",
    "alors", "donc", "du coup", "en fait", "en gros", "et puis", "ce que je veux dire,? c'est",
    "ce que je veux dire,? c'est que", "ce que je voulais dire,? c'est", "ce que je voulais dire,? c'est que",
    "le truc,? c'est", "le truc,? c'est que", "la chose,? c'est que", "le point,? c'est que",
    "comme je te disais(?: tout à l'heure| tout a l'heure)?", "comme je vous disais(?: tout à l'heure)?",
    "comme je l'ai dit", "comme je le disais", "je te rappelle que", "je vous rappelle que", "je te dis que",
    "entonces", "o sea", "la cosa es que", "lo que quiero decir es que", "lo que quiero decir es",
)


def _alternation(items) -> str:
    return "|".join(sorted(items, key=len, reverse=True)).replace(" ", r"\s+")


_FRAME = re.compile(
    r"^(?:(?:" + _alternation(_FRAME_FREE) + r")(?!\s+que\b)(?=[\s,:;]|$)"
    r"|(?:" + _alternation(_FRAME_COMMA) + r")(?=\s*[,:;]|\s*[.!?…]*$))\s*[,:;]?\s*",
    re.IGNORECASE)

_TRAIL = re.compile(
    r"\s*[,;]?\s*(?:tu vois ce que je veux dire|vous voyez ce que je veux dire|if you see what i mean|you see what i mean"
    r"|tu vois|vous voyez|tu comprends|vous comprenez|n'est-ce pas|d'accord|hein|si tu veux"
    r"|you see|you know what i mean|if you know what i mean|do you see|does that make sense|got it|capito|verdad"
    r"|vale)\s*(?=[.?!…]*$)",
    re.IGNORECASE)
_TRAIL_COMMA = re.compile(
    r",\s*(?:right|okay|ok|you know|tu sais|non|eh|quoi|capisce|¿verdad|¿sabes)\s*(?=[.?!…]*$)", re.IGNORECASE)

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
# Sentences about the recording or the act of speaking, with nothing about the subject.
_META = re.compile(
    r"^(?:"
    r"(?:thanks|thank you)(?: (?:so much|very much|a lot|everyone|all))? for (?:listening|watching|reading|tuning in|your attention)"
    r"|(?:thanks|thank you|merci|gracias)(?: (?:everyone|all|beaucoup|a todos|[àa] tous))?"
    r"|merci (?:beaucoup )?(?:d'avoir|de m'avoir|de votre|de ton) (?:[ée]cout[ée]e?|regard[ée]|suivi|attention)"
    r"|merci [àa] tous|gracias por (?:escuchar|ver|su atenci[oó]n|tu atenci[oó]n)"
    r"|that(?:'s| is) (?:all|it)(?: for| from)? ?(?:now|today|this (?:note|video|message|recording)|the moment)?"
    r"|c'est (?:tout|fini|termin[ée])(?: pour| de ma part)?(?: aujourd'hui| maintenant| cette note| ce vocal)?"
    r"|eso es todo(?: por hoy)?|that will be all"
    r"|see you (?:soon|next time|later|tomorrow)|[àa] (?:bient[oô]t|la prochaine|demain)|hasta (?:luego|pronto|ma[nñ]ana)"
    r"|nos vemos|bye(?: bye)?|au revoir|ciao|adios|adi[oó]s"
    r"|(?:i'm|i am) (?:recording|making|dictating|leaving) (?:this|a|an|my)\b.*"
    r"|(?:j'enregistre|je (?:suis en train d'enregistrer|t'enregistre|fais)) (?:ce|cette|un|une)\b.*"
    r"|estoy grabando\b.*"
    r"|(?:it is|it's|this is) [\w' -]{1,25} (?:speaking|calling)"
    r"|c'est [\w' -]{1,25} qui parle|soy [\w' -]{1,25} hablando"
    r"|(?:this|here) is (?:a|my|an|another) (?:(?:quick|short|small|little|new|voice|audio|personal|first|second|last|long) ){0,3}"
    r"(?:note|message|memo|recording)\b.*"
    r"|c'est (?:un|une|ma|mon) (?:(?:petite|petit|courte|court|nouvelle|nouveau|premier|dernier|vocal|audio) ){0,3}"
    r"(?:note|message|vocal|m[ée]mo|enregistrement)\b.*"
    r"|(?:esto|esta|este) es (?:una?|mi) (?:(?:breve|corta|corto|nueva|nuevo|personal|peque[ñn]a|peque[ñn]o|de voz) ){0,3}"
    r"(?:nota|mensaje|audio|grabaci[oó]n)\b.*"
    r"|as you can (?:hear|see)\b.*|comme (?:tu peux|vous pouvez) l'entendre\b.*|como (?:puedes|pueden) (?:o[ií]r|escuchar)\b.*"
    r"|where was i|what was i (?:saying|going to say)|o[uù] j'en [ée]tais|qu'est-ce que je disais|d[oó]nde estaba"
    r"|qu[eé] estaba diciendo|je ne sais plus ce que je (?:voulais|disais)(?: dire)?"
    r"|(?:let me see|voyons(?: voir)?|attends|attendez|hold on|un moment|un instant)"
    r"|can you hear me|tu m'entends|vous m'entendez|me escuchas"
    r"|(?:testing|test)(?: (?:one two(?: three)?|1 2 3|testing))?"
    r")$",
    re.IGNORECASE)
# Changes of subject. What follows a colon is kept ("Moving on to the next topic: pricing" -> "Pricing").
_TRANSITION = re.compile(
    r"^(?:moving on|let's move on|let us move on|on to|onto|turning to|let's turn to|let's talk about|let's look at"
    r"|(?:anyway,? )?back to the|next up|passons|revenons|venons-en|parlons|abordons|pasemos|volvamos|hablemos)\b",
    re.IGNORECASE)

# --- French and Spanish fillers (the English ones are handled by the rules in structure.py) ------------------------
_FR_ES_LEAD = re.compile(
    r"^(?:(?:et )?(?:donc|alors|entonces|o sea|en fait|du coup)(?:\s+(?:en fait|du coup|donc|alors))*\b\s*,?\s*"
    r"|(?:bon|ben|bah|bref|enfin bref|enfin|sinon|bueno|pues|eh bien)(?:\s+(?:en fait|du coup|donc|alors|bon))*\s*,\s*)",
    re.IGNORECASE)
_FR_ES_INLINE = re.compile(
    r"(?:,\s*)?\b(?:euh+|heu+|hum+|hmm+|eh bien|du coup|en fait|tu vois|vous voyez|disons|o sea|pues nada)\b\s*,?\s*"
    r"|,\s*(?:quoi|hein|bah|genre|vale|¿sabes\?)(?=[.?!]?$)",
    re.IGNORECASE)
_FR_ES_HEDGE = re.compile(
    r"^(?:je (?:pense|crois|trouve|suppose|dirais|me dis)|j'ai l'impression|[aà] mon avis|moi je pense|yo creo|creo|pienso|me parece)"
    r"\s+(?:que|qu'|qu’)\s*",
    re.IGNORECASE)
_REPEAT = re.compile(r"\b((?:[\w']+\s+){0,2}[\w']+)(?:\s*,)?\s+\1\b", re.IGNORECASE)

# --- lines that still read like talk ---------------------------------------------------------------------------------
_SECOND_PERSON = re.compile(
    r"\b(?:you|your|yours|yourself|toi|vous|votre|vos|usted|ustedes|vosotros|tú|tu)\b|\bt'",
    re.IGNORECASE)
_FIRST_EN = re.compile(r"\bI(?:'m|'ll|'ve|'d)?(?![\w.])")
_FIRST_OTHER = re.compile(r"\b(?:je|yo)\b|\bj'", re.IGNORECASE)
_TALK = re.compile(
    r"\b(?:let's|let us|lets)\b|\b(?:this|the) (?:note|recording|voice note|video|message)\b"
    r"|\b(?:cette note|cet enregistrement|ce vocal|esta nota|este audio)\b"
    r"|\bas (?:i|we) (?:said|mentioned|saw)\b|\bcomme je (?:disais|l'ai dit)\b|\bcomo dije\b",
    re.IGNORECASE)


def has_speech(text: str, first_person: bool = True) -> bool:
    """True when a line still reads like talk: it speaks to "you", asks a question, says "let's" or "this note".

    `first_person` also counts "I" and "je". Tasks are written in the first person on purpose, so callers ignore it
    for them.
    """
    s = (text or "").translate(_APOS)
    if not s.strip():
        return False
    if "?" in s or _SECOND_PERSON.search(s) or _TALK.search(s):
        return True
    if first_person and (_FIRST_EN.search(s) or _FIRST_OTHER.search(s)):
        return True
    return False


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
    if re.search(r"[.!?…]$", text):
        return text
    return text + (mark if mark and mark != "?" else ".")


def _is_framing(sentence: str) -> bool:
    plain = _plain(sentence)
    return bool(plain) and bool(_PURE.match(plain) or _META.match(plain))


def _strip_frames(s: str) -> str:
    """Greetings and discourse markers at the start ("Hello, okay so, the thing is, ...")."""
    for _ in range(6):
        before = s
        s = _GREETING.sub("", s, count=1).lstrip()
        s = _LEAD.sub("", s, count=1)
        s = _FRAME.sub("", s, count=1)
        if s == before:
            break
    return s.strip()


def _asked(rest: str, mark: str) -> str:
    """What is left of a request once the asking verb is gone: the question itself, or the topic."""
    rest = rest.strip()
    if not _QUESTION_WORD.match(rest):
        rest = _TOPIC_LEAD.sub("", rest, count=1)
    rest = re.sub(r"\s*\?$", "", rest)
    return _finish(rest, "." if mark == "?" else mark) if len(rest.split()) >= 3 else ""


def unspeak(sentence: str) -> str:
    """One sentence without its spoken framing. Empty when nothing of value is left."""
    original = (sentence or "").strip()
    if not original:
        return ""
    s = original.translate(_APOS)
    mark = _end_mark(s)
    if _is_framing(s):
        return ""
    s = _strip_frames(s)
    if not s.strip(" ,;:-"):
        return ""
    if _is_framing(s) or _is_framing(re.sub(r"^(?:so|and|but|well|okay|ok|et|mais)\s+", "", s, flags=re.IGNORECASE)) \
            or _ANNOUNCE.match(s):
        # "In this note I will talk about X": the announcement says nothing, the topic is named elsewhere
        return ""
    if _TRANSITION.match(s) and len(s.split()) <= 14:
        after = s.split(":", 1)[1].strip() if ":" in s else ""
        return _finish(after, mark) if after.split() else ""
    ask = _PREFIX_ASK.match(s)
    if ask:
        return _asked(s[ask.end():], mark)
    want = _PREFIX_WANT.match(s)  # "I want you to explain how X works"
    if want:
        bare = _BARE_ASK.match(s[want.end():])
        return _asked(s[want.end():][bare.end():], mark) if bare else s
    polite = _PREFIX_POLITE.match(s)
    if polite and polite.end() < len(s):
        rest = s[polite.end():].strip()
        bare = _BARE_ASK.match(rest)
        if bare:  # "Can you tell me what the deadline is?"
            return _asked(rest[bare.end():], mark)
        rest = re.sub(r"\s*\?$", "", rest)  # a polite request becomes a plain instruction
        return _finish(rest, "." if mark == "?" else mark) if len(rest.split()) >= 2 else ""
    trimmed = _TRAIL.sub("", s) if len(s.split()) > 4 else s
    if trimmed != s:
        s = trimmed.rstrip(" ,;")
        if mark and not s.endswith(tuple(".!?…")):
            s += mark
    elif len(s.split()) > 4 and _TRAIL_COMMA.search(s):  # "The server is slow, right?" is a statement
        s = _TRAIL_COMMA.sub("", s).rstrip(" ,;?!.\u2026")
        s += "." if mark in ("", "?") else mark
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
