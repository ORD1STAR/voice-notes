"""Turn a raw transcript into structured notes.

The preferred engine is a strong chat model on Groq. When Groq cannot be used, a small local model served
by Ollama is tried, and when that is not available either (or a call fails), a rule-based fallback is used,
so a transcript always ends up as readable notes.

Notes have this shape:
    {"title": str, "summary": str, "key_points": [str],
     "sections": [{"heading": str, "bullets": [str]}], "actions": [str]}
"key_points" and "actions" are optional.
"""
import inspect
import json
import logging
import math
import re
import time
from collections import Counter
from typing import Callable

import httpx

from . import languages, speech
from .config import settings

log = logging.getLogger("voice_notes.structure")

STOPWORDS = set(
    """a about above after again all also am an and any are as at be because been before being below between
    both but by can could did do does doing down during each few for from further had has have having he her
    here hers him his how i if in into is it its just me more most my no nor not now of off on once only or
    other our out over own same she should so some such than that the their them then there these they this
    those through to too under until up very was we were what when where which while who why will with would
    you your yeah okay ok like really actually maybe going got get gonna kind sort thing things one two
    don doesn didn isn aren wasn weren won wouldn couldn shouldn hasn haven hadn ain ive youve weve
    le la les un une des du de et en est que qui dans pour pas sur ce cette ces au aux avec ne se son sa ses
    il elle ils elles on nous vous je tu mais ou donc car si comme plus tout tous très aussi bien fait faire
    el los las una por con para como pero más""".split()
)

# Words that are true but make poor keywords or headings ("said", "next", "lot").
WEAK = set(
    """said say says saying talk talked talking talks discuss discussed mention mentioned thought think thinking
    thinks want wanted wants need needs needed take takes took taken make makes made send sent give gave put use
    used using look looks looked see saw seen know knew feel felt seem seems try tried trying come came goes went
    let lets start started next first last then right good great bad nice big small long short lot lots bit way
    ways sure much many every everything something anything nothing someone anyone people today tomorrow
    yesterday still even already always never often usually probably quite rather around almost enough another
    else however though although anyway finally important least new old different able possible either whether
    whatever keep kept check topic point points part parts question questions end time times day days week weeks
    month months year years morning evening honestly basically literally obviously
    minute minutes second seconds hour hours euro euros dollar dollars percent gram grams degrees kilos
    heure heures neuf dix
    own owns owned wake woke miss missed told tell tells find finding found keeps walked went cooked
    wants propose proposes proposed happen happens happened worked helps helped ask asks asked
    called gets getting gives giving showed showing leaves left comes brings brought bring runs
    ran turns turned moved changed adds added builds built decide decides decided agree agrees agreed
    prepares prepared finishes finished reviewed planned liters litres kilos meters buy bought cook pick charge pay clean visit install
    download upload share watch listen eat drink drive travel arrive update updates updated freeze
    alors aussi vraiment dit dire voulais veux faut doit doivent peut peuvent être avoir ensuite puis enfin
    toujours jamais encore moins autre autres chose choses rapide note cela mon ton notre nos votre vos leur
    leurs""".split()
)
KEY_STOP = STOPWORDS | WEAK

MAX_PIECE_CHARS = 3500
MAX_ACTIONS = 8

# Spoken openers that add nothing once the speech is written down.
_OPENER = re.compile(
    r"^(?:"
    r"(?:okay|ok|alright|all right)\s+so\b"
    r"|so\s+(?:yeah|um+|uh+)\b"
    r"|(?:and\s+)?(?:another|one more)\s+thing(?:\s+(?:is|was))?\s*[,:]?(?:\s+that\b)?"
    r"|i\s+mean\s*,?"
    r"|you\s+know\s*,"
    r"|(?:okay|ok|alright|right|well|so|yeah|yes|um+|uh+|hmm+|er|erm|hey|hi|hello|basically|anyway|anyways|like"
    r"|actually|honestly|frankly|literally|obviously|now|oh|and|also|plus|alors|bon|ben|euh|donc|bueno|pues"
    r"|entonces|voilà)\s*,"
    r"|(?:oh\s+and|and\s+also|oh|and|also|plus|basically|honestly|frankly|literally|anyway|anyways|actually"
    r"|obviously|(?:okay|ok|alright|um+|uh+|hmm+|er|erm|yeah))\b"
    r"|so\b(?!\s+(?:far|much|many|long|on|forth|that|called|as|to speak)\b)"
    r")\s*[,.]?\s*",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"^i\s+(?:think|guess|believe|suppose|reckon|feel(?:\s+like)?|would\s+say)\s+"
    r"(?:that\s+(?=(?:we|i|you|they|he|she|it|there|our|my|the|a|an)\b))?", re.IGNORECASE)
_INLINE_FILLER = re.compile(
    r"(?:,\s*)?\b(?:you know|i mean|um+|uh+|erm|basically|literally)\b\s*,?\s*|,\s*like\s*,|\bhonestly,\s*",
    re.IGNORECASE)
_TAG = re.compile(r",\s*(?:you know|right|okay|ok|yeah|huh)\s*(?=[.?!]?$)", re.IGNORECASE)
_STUTTER = re.compile(r"\b([^\W\d_]+)(\s+\1\b)+", re.IGNORECASE)
_TAIL_WORDS = STOPWORDS | {"before", "after", "by", "with", "while", "than", "without", "within", "onto", "upon",
                          "against", "toward", "towards", "via", "per", "across", "along", "among", "whose"}

# A remark that carries no content once it is written down.
_EMPTY_REMARK = re.compile(
    r"^(?:that'?s (?:it|all)(?: for now| for today)?|that (?:is|was) (?:it|all)(?: for now| for today)?|this is (?:it|all)(?: for now| for today)?|(?:thank you|thanks)(?: so much| very much"
    r"| for listening)?|see you(?: later| soon| next time)?|talk (?:to you )?(?:soon|later)|bye|goodbye|cheers"
    r"|end of (?:the )?(?:note|message|recording)|(?:that|this|it)(?:'s| is| was) (?:everything|all there is)(?: for now| for today)?|what else|let me think|let me see|i don'?t know|i'?m not sure|not sure"
    r"|we'?ll see|we will see|something like that|and so on|or something|stuff like that|yes|no|right|exactly|sure"
    r"|true|indeed|good|great|nice|cool|fine|okay|ok|alright|hmm+|well|interesting|noted|voilà|c'?est tout"
    r"|eso es todo|that'?s about it|that'?s everything|nothing else|more to come|to be continued"
    r"|(?:that|this|it|which)(?:'s| is| was| sounds| seems| looks| feels)(?: also| really| very| pretty| quite| a| an| the"
    r"| so| too| kind of)* (?:good|great|fine|nice|cool|ok|okay|bad|interesting|important|true|right|wrong|correct|clear"
    r"|possible|better|worse|best|enough|useful|helpful|amazing|awesome|perfect|crazy|funny|weird|hard|difficult|easy"
    r"|simple|cheap|expensive)(?: call| idea| thing| point| plan| option| choice| decision| one| way| step| move| deal)?"
    r"(?: to me| for now)?)$",
    re.IGNORECASE,
)


class StructureError(Exception):
    pass


# ----------------------------------------------------------------- helpers

_TITLE_ABBR = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "jr", "sr", "mme", "mlle", "inc", "ltd", "approx",
               "fig", "e.g", "i.e"}


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"([\u3002\uff01\uff1f])(?=\S)", r"\1 ", re.sub(r"\s+", " ", text)).strip()
    if not text:
        return []
    parts: list[str] = []
    start = 0
    for m in re.finditer(r"(?<=[.!?…。！？])\s+(?=\S)", text):
        before = text[start:m.start()]
        last = before.rsplit(" ", 1)[-1].rstrip(".").lower()
        nxt = text[m.end()]
        if text[m.start() - 1] == ".":
            if last in _TITLE_ABBR:  # "Dr. Smith", "e.g. Anna"
                continue
            if nxt.islower() and ("." in last or len(last) == 1 or last == "etc"):  # "5 p.m. tomorrow", "etc. and"
                continue
        parts.append(before.strip())
        start = m.end()
    parts.append(text[start:].strip())
    out: list[str] = []
    for p in (x for x in parts if x):
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


def _kw(text: str) -> list[str]:
    """Keywords: content words without stopwords and without weak, conversational words."""
    return [w for w in re.findall(r"[^\W\d_]{3,}", text.lower()) if w not in KEY_STOP]


def clean_sentence(text: str) -> str:
    """Drop spoken fillers ("okay so", "um", "you know", "I think") and capitalise the result."""
    s = text.strip().replace("’", "'")
    s = re.sub(r"^and\s+(then|next|finally|after that)\b", r"\1", s, flags=re.IGNORECASE)
    for _ in range(4):
        before = s
        s = _OPENER.sub("", s, count=1)
        s = re.sub(r"^and\s+(then|next|finally|after that)\b", r"\1", s, flags=re.IGNORECASE)
        hedged = _HEDGE.sub("", s, count=1)
        if len(hedged.split()) >= 3:
            s = hedged
        if s == before:
            break
    s = speech.unspeak(s) if s else ""
    s = speech.drop_fillers(s) if s else ""
    s = _INLINE_FILLER.sub(lambda m: "," if m.group(0).lstrip().startswith(", like") else " ", s)
    s = _TAG.sub("", s)
    s = _STUTTER.sub(r"\1", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    s = speech.unspeak(s).strip(" ,;") if s else ""
    if not s:
        return ""
    return s[0].upper() + s[1:]


def is_empty_remark(sentence: str) -> bool:
    """True for remarks with no content once written down ("That is a good call.", "That's it.")."""
    plain = re.sub(r"[.!?,;:…]+", " ", sentence.lower()).strip()
    plain = re.sub(r"\s+", " ", plain)
    return bool(plain) and bool(_EMPTY_REMARK.match(plain)) and not re.search(r"\d", plain)


def paragraphs(text: str, target: int = 520) -> list[str]:
    """Group the sentences of a transcript into readable paragraphs."""
    out, current = [], ""
    for s in split_sentences(text):
        if current and len(current) + len(s) + 1 > target:
            out.append(current)
            current = s
        else:
            current = f"{current} {s}".strip()
    if current:
        out.append(current)
    return out


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


def short_phrase(text: str, max_words: int = 8, split: bool = True) -> str:
    """A short label taken from the start of a sentence, without a dangling word at the end.

    With split=False the text is not cut at the first comma or colon, only at max_words.
    """
    s = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"[,;:]\s|\s[-/]\s", s) if split else None
    if m and len(s[:m.start()].split()) >= 3:
        s = s[:m.start()]
    words = s.split()
    cut = len(words) > max_words
    words = words[:max_words]
    while cut and len(words) > 3 and re.sub(r"\W", "", words[-1].lower()) in _TAIL_WORDS:
        words.pop()
    s = " ".join(words).rstrip(".,;:!?-")
    return s[:1].upper() + s[1:] if s else ""


def _as_list(value) -> list:
    """A model sometimes sends one string or one object where a list is expected."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [line for line in value.splitlines() if line.strip()]
    return [value]


def _item_text(item) -> str:
    """The text of a list item; a model may wrap it in an object such as {"text": "..."}."""
    if isinstance(item, dict):
        for key in ("text", "bullet", "point", "content", "sentence", "task"):
            if item.get(key):
                return str(item[key])
        return ""
    return "" if item is None else str(item)


def normalize_notes(raw: dict) -> dict:
    """Validate and clean a notes dict. Raises StructureError when unusable."""
    if not isinstance(raw, dict):
        raise StructureError("notes are not an object")
    title = str(raw.get("title") or "").strip().strip('"')
    summary = str(raw.get("summary") or "").strip()
    sections = []
    for sec in _as_list(raw.get("sections")):
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("heading") or sec.get("title") or "").strip().strip("#").strip()
        bullets = []
        for b in _as_list(sec.get("bullets")):
            b = re.sub(r"^[\-\*•]\s*", "", _item_text(b).strip())
            if b:
                bullets.append(b)
        if bullets:
            sections.append({"heading": heading or "Notes", "bullets": bullets[:12]})
    if not sections:
        raise StructureError("no sections")
    actions = _clean_list(raw.get("actions"), 160, 10, bullet_marks=True)
    key_points = _clean_list(raw.get("key_points"), 220, 6)
    return {"title": _clip(title, 100) or "Untitled note", "summary": summary, "key_points": key_points,
            "sections": sections[:30], "actions": actions}


def _clean_list(items, clip: int, cap: int, bullet_marks: bool = False) -> list[str]:
    out: list[str] = []
    for a in _as_list(items):
        a = _item_text(a).strip()
        a = re.sub(r"^(?:(?:[\-\*•]|\[[ xX]?\])\s*)+", "", a) if bullet_marks else re.sub(r"^[\-\*•]\s*", "", a)
        if a and a.lower() not in {x.lower() for x in out}:
            out.append(_clip(a, clip))
    return out[:cap]


# --------------------------------------------------- sentence preparation

def _near_duplicate(a: str, b: str) -> bool:
    wa, wb = set(_words(a)), set(_words(b))
    if not wa or not wb:
        return a.lower() == b.lower()
    return len(wa & wb) / len(wa | wb) >= 0.8


_FRAGMENT = re.compile(r"^(?:which|because|especially|something|otherwise|for example|for instance)\b", re.IGNORECASE)


def _prepare_sentences(text: str) -> list[str]:
    """Sentences with fillers removed, near repetitions and empty remarks dropped, fragments joined."""
    cleaned: list[str] = []
    for s in split_sentences(text):
        c = clean_sentence(s)
        if not c or (len(c.split()) < 2 and cleaned):
            continue
        if is_empty_remark(c):
            continue
        if any(_near_duplicate(c, prev) for prev in cleaned[-6:]):
            continue
        if cleaned and _FRAGMENT.match(c) and len(cleaned[-1]) + len(c) < 280:
            prev = cleaned[-1].rstrip(".!?")
            cleaned[-1] = f"{prev}, {c[0].lower() + c[1:]}"
            continue
        cleaned.append(c)
    if not cleaned:  # a note that is only filler still gets its words
        cleaned = [c for c in (clean_sentence(s) for s in split_sentences(text)) if c]
    if len(cleaned) > 8:
        # "Fine." says nothing once written down.
        substantial = [c for c in cleaned if len(_words(c)) >= 2 or re.search(r"\d", c)]
        if len(substantial) >= 6:
            return substantial
    return cleaned


# ------------------------------------------------------- phrases, titles

_LOWER_FIRST = {"the", "a", "an", "my", "our", "your", "this", "that", "his", "her", "their"}


def _runs(sentence: str) -> list[list[str]]:
    """Runs of consecutive content words in a sentence: "the payment integration is" gives [payment, integration]."""
    runs: list[list[str]] = []
    cur: list[str] = []
    for m in re.finditer(r"[^\W\d_]+|\d+|[^\s\w]", sentence):
        tok = m.group(0)
        if len(tok) >= 3 and tok[0].isalpha() and tok.lower() not in KEY_STOP:
            cur.append(tok)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def _display(tokens: list[str]) -> str:
    text = " ".join(tokens)
    return text[:1].upper() + text[1:]


def doc_stats(doc_groups: list[list[str]]) -> tuple[Counter, int]:
    """In how many sections each keyword occurs, and the number of sections."""
    df: Counter = Counter()
    for g in doc_groups:
        df.update({w for s in g for w in _kw(s)})
    return df, len(doc_groups)


def best_phrase(group: list[str], doc_groups: list[list[str]] | tuple | None = None, avoid: set[str] | None = None,
                max_words: int = 3, min_count: int = 1) -> str:
    """The phrase that best names a group of sentences: frequent, specific to the group, preferably two words.

    `doc_groups` is the list of all sections of the note, or the result of doc_stats() for it.
    """
    avoid = avoid or set()
    if isinstance(doc_groups, tuple):
        df, n_groups = doc_groups
    else:
        df, n_groups = doc_stats(doc_groups or [group])
    tf = Counter(w for s in group for w in _kw(s))

    def idf(w: str) -> float:
        return 1.0 + math.log((n_groups + 1) / (1 + df[w]))

    seen: dict[tuple, list] = {}
    for si, s in enumerate(group):
        for run in _runs(s):
            for size in range(1, min(max_words, len(run)) + 1):
                for i in range(len(run) - size + 1):
                    ph = run[i:i + size]
                    ent = seen.setdefault(tuple(t.lower() for t in ph), [ph, 0, si])
                    ent[1] += 1
    best, best_score = "", -1.0
    for key, (ph, count, first) in seen.items():
        if any(w in avoid for w in key) or count < min_count:
            continue
        base = sum(tf[w] * idf(w) for w in key) / (len(key) ** 0.5)
        score = base + 1.5 * count + (0.8 if len(key) == 2 else 0.3 if len(key) == 3 else 0.0)
        score += 0.6 if first == 0 else 0.0
        if any(t.isupper() and len(t) >= 2 for t in ph):  # acronyms such as PDF or API name a topic well
            score += 0.4
        if score > best_score:
            best, best_score = _display(ph), score
    return best


_TOPIC = re.compile(
    r"\b(?P<noun>note|notes|recording|memo|message|talk|thought|thoughts|update|idea|ideas|plan|recap|summary|overview|"
    r"review|report|reminder|minutes|meeting|call|session|status|progress|briefing|agenda|"
    r"note|m[eé]mo|rappel|r[eé]sum[eé]|nota|recordatorio)\b"
    r"(?:\s+(?:rapide|r[aá]pida|quick|short|brief|small))?\s*[^.!?]{0,40}?\s*"
    r"\b(?:about|on|regarding|concerning|for|of|from|sur|[aà] propos de|concernant|pour|sobre|acerca de)\s+(?P<topic>.{4,90})",
    re.IGNORECASE,
)
_TOPIC_VERB = re.compile(
    r"\b(?:want(?:ed)?|going|trying|like|need(?:ed)?|plan(?:ned)?|voulais|veux|quiero)\s+(?:to\s+)?(?:\w+\s+){0,2}?"
    r"(?:write down|note down|record|document|explain|describe|capture|remember|outline|talk about|share|sum up|"
    r"parler de|noter|expliquer)\s+(?P<topic>.{4,90})",
    re.IGNORECASE,
)
_SUFFIX_NOUNS = {"idea", "ideas", "thought", "thoughts", "plan", "update", "recap", "summary", "overview", "review",
                 "report", "reminder", "minutes", "status", "progress", "briefing"}
_ARTICLE = re.compile(r"^(?:the|a|an|my|our|your|this|that|these|those|some|le|la|les|l'|un|une|des|du|de la|de l'|mon|"
                      r"ma|mes|notre|nos|el|los|las|una|unos|mi|nuestro)\s+", re.IGNORECASE)
_TRAILING_ADV = re.compile(r"\s+(?:first|today|yesterday|now|also|too|again|later|briefly|quickly|next|here|"
                           r"for now|real quick|this morning)$", re.IGNORECASE)


def _clean_topic(topic: str) -> str:
    topic = re.split(r"[,;:.!?]|\s(?:and then|because|so that|which|but|that I|that we|since|as I|where|when I)\s", topic)[0]
    topic = topic.strip()
    m = re.match(r"how\s+(?:i|we|you)\s+([a-z]+)\s+(.+)", topic, re.IGNORECASE)
    if m and not m.group(1).lower().endswith(("s", "ed")):
        topic = f"how to {m.group(1).lower()} {m.group(2)}"
    topic = _ARTICLE.sub("", topic)
    m = re.match(r"how to (\w+) (.+)", topic, re.IGNORECASE)
    if m:
        topic = f"how to {m.group(1)} {_ARTICLE.sub('', m.group(2))}"
    for _ in range(2):
        topic = _TRAILING_ADV.sub("", topic)
    return topic.strip()


def _topic_title(sentence: str) -> str:
    """For openings like "I wanted to record a note about the garden project", the topic is the title."""
    m = _TOPIC.search(sentence)
    noun = ""
    if m:
        noun = m.group("noun").lower()
        topic = m.group("topic")
    else:
        m = _TOPIC_VERB.search(sentence)
        if not m:
            return ""
        topic = m.group("topic")
    topic = _clean_topic(topic)
    words = topic.split()
    if not topic or len(topic) < 4 or all(w.lower() in KEY_STOP for w in words):
        return ""
    phrase = short_phrase(topic, 8)
    if noun in _SUFFIX_NOUNS and len(words) <= 2 and noun not in topic.lower():
        phrase = f"{phrase} {noun}"
    return phrase


_INTRO = re.compile(
    r"\b(?:(?:\w+\s+){0,2}?(?:talk(?:ed|ing)?|spoke|speak(?:ing)?|thought|think(?:ing)?|discuss(?:ed|ing)?|"
    r"cover(?:ed|ing)?)\s+(?:about|on)"
    r"|(?:\w+\s+){0,2}?(?:start(?:ed|s)?|began|begin(?:s)?|kicked off)\s+(?:with|by talking about)"
    r"|(?:\w+\s+){0,2}?(?:turn(?:ed)?\s+to|went\s+(?:over|through)|go(?:ing)?\s+(?:over|through)|look(?:ed|ing)?\s+at)"
    r"|(?:moving|moved|move)\s+on\s+to|let'?s\s+(?:talk|move)\s+(?:about|on to)|as for|when it comes to|speaking of"
    r"|(?:the\s+)?(?:next|last|first|second|third|final|main|other|another)\s+(?:topic|point|thing|item|subject|issue|"
    r"question)\s+(?:is|was|being|:)?|regarding|concerning|on the subject of|now (?:for|about)|"
    r"parlons de|passons [aà]|hablemos de)\s+"
    r"(?:the\s+|a\s+|an\s+|our\s+|my\s+|some\s+|le\s+|la\s+|les\s+|l')?(?P<topic>[^.!?]{3,70})[.!?]?$",
    re.IGNORECASE,
)


def intro_topic(sentence: str) -> str:
    """"Then we moved on to hiring." gives "Hiring". Empty when the sentence is not a topic introduction."""
    m = _INTRO.search(sentence)
    if not m:
        return ""
    topic = _clean_topic(m.group("topic"))
    words = topic.split()
    if not 1 <= len(words) <= 6 or all(w.lower() in KEY_STOP for w in words) or re.search(r"\d", topic):
        return ""
    return short_phrase(topic, 6)


# ----------------------------------------------------------- importance

_DECIDE = re.compile(
    r"\b(?:decided|decide|agreed|agree|decision|chose|chosen|settled|approved|confirmed|resolved|conclusion|concluded|"
    r"going with|the plan is|plan is to|the goal|goal is|the idea is|the point is|in short|in summary|overall|"
    r"bottom line|d[eé]cid[eé]|convenu|decidimos|acordamos)\b", re.IGNORECASE)
_EMPH = re.compile(
    r"\b(?:most important|important thing|key|main|critical|essential|priority|urgent|crucial|biggest|the problem|"
    r"(?:real |main |biggest )problem|the issue|problem is|issue is|risk|deadline|due|must|only way|le plus important|essentiel|priorit[eé]|"
    r"urgent|principal|importante|prioridad)\b", re.IGNORECASE)
_EVENT = re.compile(
    r"\b(?:delayed|postponed|cancel(?:l)?ed|blocked|stuck|missed|behind schedule|failed|broke|broken|launched|released|"
    r"rejected|raised|dropped|increased|decreased|doubled|slipped|leaves|leaving|renews|expires|ends|starts|moves|"
    r"retard|reporté|annulé|retrasado|cancelado)\b", re.IGNORECASE)
_NUMDATE = re.compile(
    r"\d|\b(?:two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|fifty|hundred|thousand|million|half|monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|june|july|august|"
    r"september|october|november|december|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b", re.IGNORECASE)
_PROPOSAL = re.compile(r"\b(?:we|i)\s+(?:should|could|must|need to|have to)\b|\bmaybe\b|\bpropos|\bsuggest", re.IGNORECASE)
_STEP = re.compile(r"^(?:first(?:ly)?|then|next|after that|afterwards?|finally|lastly|once that|step\s*\d+|d'abord|ensuite|puis|enfin|primero|luego)\b", re.IGNORECASE)
_DEPENDENT = re.compile(r"^(?:that|this|it|they|those|these|them|he|she|which|something|most of them|one of them|"
                        r"and|but|or|because|also|cela|ça|ils|elles)\b", re.IGNORECASE)


def is_context_dependent(sentence: str) -> bool:
    """True when a sentence leans on the one before it ("That is something we should fix"), so it reads badly alone."""
    return bool(_DEPENDENT.match(sentence.strip()))


def _importance(sentences: list[str]) -> list[float]:
    """How much each sentence deserves to be in a summary or among the key points."""
    freq = Counter(w for s in sentences for w in _kw(s))
    raw = []
    for s in sentences:
        ws = _kw(s)
        raw.append(sum(freq[w] for w in ws) / (len(ws) ** 0.6) if ws else 0.0)
    top = max(raw) if raw and max(raw) > 0 else 1.0
    out = []
    for s, r in zip(sentences, raw):
        v = r / top
        if _DECIDE.search(s):
            v += 1.0
        if _EMPH.search(s):
            v += 0.8
        if _NUMDATE.search(s):
            v += 0.5
        if _PROPOSAL.search(s):
            v += 0.4
        if _EVENT.search(s):
            v += 0.5
        if _DEPENDENT.match(s):
            v -= 0.6
        if s.rstrip().endswith("?"):
            v -= 1.5
        n = len(s.split())
        if n < 5:
            v -= 0.5
        elif n > 25:
            v -= 0.3
        out.append(v)
    return out


def _has_cue(s: str) -> bool:
    return bool(_DECIDE.search(s) or _EMPH.search(s) or _NUMDATE.search(s) or _PROPOSAL.search(s) or _EVENT.search(s))


def select_key_points(sentences: list[str], exclude: list[str] = (), min_sentences: int = 8) -> list[str]:
    """The few sentences a reader must not miss: decisions, deadlines, figures, the stated priority."""
    n = len(sentences)
    if n < min_sentences:
        return []
    limit = 3 if n < 14 else 4 if n < 24 else 5
    imp = _importance(sentences)
    chosen: list[int] = []
    for i in sorted(range(n), key=lambda k: -imp[k]):
        if imp[i] < 0.85:
            break
        s = sentences[i]
        if not _has_cue(s):
            continue
        if _task_text(s, sentences[i - 1] if i else "")[0] or _STEP.match(s):
            continue  # tasks belong to the action items, steps to the process diagram
        if any(_near_duplicate(s, e) for e in exclude) or any(_near_duplicate(s, sentences[j]) for j in chosen):
            continue
        chosen.append(i)
        if len(chosen) == limit:
            break
    return [_clip(sentences[i], 200) for i in sorted(chosen)]


def ensure_key_points(notes: dict) -> dict:
    """Notes saved before key points existed get them from their own bullets (nothing is stored)."""
    if not notes or "key_points" in notes:
        return notes
    sentences = [b for s in notes.get("sections") or [] for b in s["bullets"]]
    exclude = [notes.get("summary") or ""] + list(notes.get("actions") or [])
    return {**notes, "key_points": select_key_points(sentences, exclude=exclude)}


# ----------------------------------------------------------- action items

_ACTION_LEAD = re.compile(r"^(?:and|so|also|oh|then|now|but|okay|ok|well)\b[\s,]*", re.IGNORECASE)
_ACTION_CUT = re.compile(r";\s|,\s+(?=(?:so|and|but|because|which|then|it|its|we|i|they|he|she|you|that|this|there|if|when|my|her|his|their|"
                         r"our|since|although|otherwise|while)\b)|\s(?:because|which|since|so that|but|otherwise|although|even though|"
                         r"and then)\s",
                         re.IGNORECASE)
_REMIND = re.compile(
    r"\b(?:remember to|don'?t forget to|do not forget to|not forget to|need to remember to"
    r"|(?<!the )(?<!a )(?<!my )follow up(?: with| on)?|make sure(?: that)?(?: we| i)?(?: to)?|to-?do:?)\s*",
    re.IGNORECASE)
_SELF = re.compile(
    r"\b(?:i|we)\s+(?:(?:really|also|still|just|definitely|probably|absolutely|first|then|now|both)\s+)*"
    r"(?:(?:need|have|has|got|ought)\s+to|must|should|gotta|will|(?:am|are)\s+going\s+to)\s+"
    r"|\b(?:i|we)'(?:ll|m going to|re going to)\s+|\blet'?s\s+|\blet us\s+",
    re.IGNORECASE)
_ROMANCE = re.compile(
    r"\b(?:il faut|je dois|on doit|nous devons|n'oublie|n'oubliez|pense [aà]|faut que|je vais|il ne faut pas oublier|"
    r"hay que|tengo que|debemos|tenemos que|no olvides|necesito|voy a)\b", re.IGNORECASE)
_GROUP_NOUNS = (r"(?:team|department|group|committee|marketing|finance|sales|support|engineering|design|legal|"
                r"operations|management|accounting|hr|it)")
_NAMED = re.compile(
    r"^(?P<who>(?:(?i:the|our|my)\s+)?(?:[A-Z][\w'-]+(?:\s+(?:and|&)\s+[A-Z][\w'-]+)?|(?i:(?:\w+\s+)?" + _GROUP_NOUNS + r")))"
    r"\s+(?i:will|needs? to|has to|have to|should|must|is going to|are going to|promised to|offered to|agreed to)\s+"
    r"(?P<task>.+)$")
_IMPERATIVE = {"call", "email", "text", "send", "book", "buy", "pay", "cancel", "renew", "schedule", "ask", "tell",
               "remind", "contact", "fix", "finish", "prepare", "write", "bring", "pick", "clean", "submit", "sign",
               "upload", "organize", "organise", "reply", "answer", "order", "confirm", "invite", "print", "pack"}
_BAD_VERBS = {"be", "have", "get", "say", "admit", "agree", "mention", "see", "think", "hope", "wonder", "not",
              "never", "also", "it", "that", "this", "there", "just", "probably"}
_LEAD_ADV = re.compile(r"^(?:also|really|just|probably|definitely|still|then|first|now|maybe|both|to|remember to|"
                       r"forget to)\s+", re.IGNORECASE)
_COND = re.compile(r"^((?:if|when|once|unless|as soon as|before|after)\b[^,]{3,60},\s+)(.+)$", re.IGNORECASE)
_PLAN_LIST = re.compile(r"\b(?:next steps?|the plan|to-?do|action items?)\s*(?:is|are|:)\s*(?:to\s+)?(?P<list>.+)$",
                        re.IGNORECASE)
_RELATIVE = re.compile(r"\b(?:something|what|everything|anything|one thing|the thing|stuff)\s+(?:that\s+)?$", re.IGNORECASE)


def _prev_subject(prev: str) -> str:
    """The main thing the previous sentence is about, to stand in for "it": "The intro music is too long" gives "the intro music"."""
    runs = _runs(prev)
    if not runs:
        return ""
    run = runs[0][:2]
    text = " ".join(run)
    return text if run[0][:1].isupper() else "the " + text


def _task_text(sentence: str, prev: str = "") -> tuple[str, str]:
    """The task a sentence sets, worded as an instruction ("Call Anna about the delivery"), and its kind.

    Returns ("", "") when the sentence is not a task. Only the speaker's own duties count ("I need to",
    "we should", "remember to", "Maria will ...", a plain instruction); "you" in a how-to and plain statements do not.
    """
    s = sentence.strip()
    if not s or s.endswith("?"):
        return "", ""
    s = _ACTION_LEAD.sub("", s)
    cond = ""
    mc = _COND.match(s)
    if mc:
        cond, s = mc.group(1), mc.group(2)
    text, kind, pre = "", "", ""

    m = _NAMED.match(s)
    if m:
        who = re.sub(r"^(?:the|our|my)\s+", "", m.group("who"), flags=re.IGNORECASE)
        first = who.split()[0].lower()
        if first not in KEY_STOP and first not in {"everyone", "someone", "nobody", "everything"}:
            text, kind = m.group("task"), "named"
            who = who[:1].upper() + who[1:]
    if not text:
        found = [x for x in (_REMIND.search(s), _SELF.search(s)) if x]
        if found:
            m = min(found, key=lambda x: x.start())
            pre = s[:m.start()]
            keep = re.match(r"(?:make sure|follow up)", m.group(0), re.IGNORECASE)
            text = s[m.start():] if keep else s[m.end():]
            kind = "self"
            if not cond and re.match(r"(?:if|when|once|unless)\b", pre.strip(), re.IGNORECASE) and 1 < len(pre.split()) <= 12:
                cond, pre = pre.strip(" ,") + ", ", ""  # "If the tests fail on Wednesday we will postpone the switch"
    if not text:
        m = _ROMANCE.search(s)
        if m:
            seams = list(re.finditer(r"[,;]\s", s[:m.start()]))
            text = s[seams[-1].end():] if seams else s
            cut = _ACTION_CUT.search(text, m.end() - (seams[-1].end() if seams else 0))
            text = text[:cut.start()] if cut else text
            text = re.sub(r"\s+", " ", text).strip(" ,;")
            if len(text.split()) < 3:
                return "", ""
            text = text.rstrip(".!")
            return _clip(cond + (text if not cond else text[:1].lower() + text[1:]), 160), "romance"
    if not text and not cond:
        first = s.split(" ", 1)[0].lower().strip(",.")
        if first in _IMPERATIVE and len(s.split()) >= 2:
            text, kind = s, "imperative"
    if not text:
        return "", ""

    cut = _ACTION_CUT.search(text)
    if cut:
        text = text[:cut.start()]
    text = _LEAD_ADV.sub("", text.strip(" ,;."))
    text = _LEAD_ADV.sub("", text)
    words = text.split()
    if len(words) < 2 or words[0].lower().strip(",") in _BAD_VERBS or not _kw(text):
        return "", ""  # "Go now" says nothing
    if len(words) <= 2 and words[-1].lower().strip(",.") in {"something", "anything", "everything", "it", "that", "this", "stuff"}:
        return "", ""  # "Cook something" says nothing
    if kind == "self" and _RELATIVE.search(pre):  # "that is something we should fix before launch": the object is elsewhere
        subject = _prev_subject(prev)
        if not subject:
            return "", ""
        text = f"{words[0]} {subject} {' '.join(words[1:])}"
    else:
        m = re.match(r"(\w+)\s+(it|that|this)\b(.*)$", text, re.IGNORECASE)
        if m and m.group(1).lower() not in {"do", "make", "get", "have", "let"}:
            subject = _prev_subject(prev)
            if subject:
                text = f"{m.group(1)} {subject}{m.group(3)}"
    text = text[:1].upper() + text[1:]
    if kind == "named":
        text = f"{who}: {text[:1].lower() + text[1:]}"
    if cond:
        text = cond + (text if kind == "named" else text[:1].lower() + text[1:])
    return short_phrase(text, 18, split=False), kind


def extract_actions(sentences: list[str]) -> list[str]:
    """Tasks the speaker set for themselves or gave to someone, worded as short instructions."""
    out: list[str] = []
    for i, s in enumerate(sentences):
        prev = sentences[i - 1] if i else ""
        candidates = [_task_text(s, prev)[0]]
        plan = _PLAN_LIST.search(s)
        if plan:  # "Next steps are to finish the testing, update the config and prepare the announcement"
            for part in re.split(r",\s*(?:and\s+)?|\s+and\s+", plan.group("list").rstrip(".")):
                part = _LEAD_ADV.sub("", part.strip())
                if len(part.split()) >= 2 and part.split()[0].lower() not in _BAD_VERBS and _kw(part):
                    candidates.append(short_phrase(part, 18, split=False))
        starts = sorted({m.start() for rx in (_REMIND, _SELF) for m in rx.finditer(s)})
        for st in starts[1:]:  # a second task in the same sentence
            if re.search(r"(?:,|;|\band|\bthen|\bbut)\s+$", s[:st], re.IGNORECASE):
                candidates.append(_task_text(s[st:], prev)[0])
        for phrase in candidates:
            if phrase and not any(_near_duplicate(phrase, o) for o in out):
                out.append(phrase)
        if len(out) >= MAX_ACTIONS:
            return out[:MAX_ACTIONS]
    return out


# ---------------------------------------------------------------- sections

def _similarity(left: list[str], right: list[str]) -> float:
    a = Counter(_stem(w) for s in left for w in _kw(s))
    b = Counter(_stem(w) for s in right for w in _kw(s))
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / den if den else 0.0


def topic_groups(sentences: list[str], target: int = 5, low: int = 3, high: int = 8) -> list[list[str]]:
    """Cut a list of sentences into sections where the vocabulary changes the most.

    Short notes stay in one section. Longer ones are cut between `low` and `high` sentences from the
    start of the section, at the gap where the words before and after have the least in common. A sentence
    that announces a new topic ("Then we moved on to hiring") is a preferred place to cut.
    """
    n = len(sentences)
    if n <= 6:
        return [sentences]

    def gap(b: int) -> float:
        g = _similarity(sentences[max(0, b - 2):b], sentences[b:b + 2])
        if intro_topic(sentences[b]):
            g -= 0.5
        if _STEP.match(sentences[b]) or _STEP.match(sentences[b - 1]):
            g += 0.5  # keep a sequence of steps together
        return round(g, 3)

    groups, start = [], 0
    while n - start > high:
        best = min(range(start + low, min(start + high, n - low) + 1),
                   key=lambda b: (gap(b), abs(b - start - target)))
        groups.append(sentences[start:best])
        start = best
    rest = sentences[start:]
    if len(rest) >= 2 * low:
        best = min(range(start + low, n - low + 1), key=lambda b: (gap(b), abs(b - start - target)))
        if gap(best) < 0.05:
            groups.append(sentences[start:best])
            start = best
            rest = sentences[start:]
    groups.append(rest)
    return groups


def _heading(group: list[str], doc_groups, used: set[str], avoid: set[str] = frozenset()) -> str:
    """A short heading: an announced topic ("moved on to hiring") or the phrase that best names the section."""
    for s in group[:2]:
        topic = intro_topic(s)
        if topic and not ({w.lower() for w in topic.split()} & used):
            used.update(w.lower() for w in topic.split())
            return topic
    phrase = (best_phrase(group, doc_groups, used | set(avoid)) or best_phrase(group, doc_groups, used)
              or best_phrase(group, doc_groups))
    used.update(w.lower() for w in phrase.split())
    return phrase or "Notes"


def structure_rules(text: str) -> dict:
    sentences = _prepare_sentences(text)
    if not sentences:
        raise StructureError("The transcript is empty.")
    title = ""
    for i, s in enumerate(sentences[:3]):
        title = _topic_title(s)
        if title:
            if len(sentences) > 3:
                sentences.pop(i)  # "I want to record a note about X" only introduces the topic
            break
    if not title:
        if len(sentences) <= 3:
            title = short_phrase(sentences[0], 8)
        else:
            title = best_phrase(sentences, [sentences], min_count=2)  # a phrase the speaker keeps coming back to
            if len(title.split()) < 2 and len(sentences[0].split()) <= 8:
                title = short_phrase(sentences[0], 8)  # "Today was a strange day" beats "Work"
            title = title or short_phrase(sentences[0], 8)
    if len(sentences) > 3 and title.rstrip(".").lower() == sentences[0].rstrip(".!?").lower():
        sentences.pop(0)  # the first sentence is the title; it does not need to be a bullet as well
    title_words = {w.lower() for w in re.findall(r"[^\W\d_]{3,}", title)}

    imp = _importance(sentences)
    rank = dict(zip(sentences, imp))
    scale = max(1, len(sentences) // 240)  # very long recordings get longer sections, so the notes stay readable
    groups = topic_groups(sentences, target=5 * scale, low=3 * scale, high=8 * scale)
    stats = doc_stats(groups)
    cap = 7 if scale == 1 else 9

    # Summary: the best opening sentence, then the most important sentence of other sections.
    n = len(sentences)
    n_summary = 2 if n < 12 else 3 if n < 30 else 4
    group_of = {s: gi for gi, g in enumerate(groups) for s in g}
    lead_pool = [i for i in range(min(n, max(2, n // 3))) if not _DEPENDENT.match(sentences[i])] or [0]
    picked = [max(lead_pool, key=lambda i: imp[i])]
    used_groups = {group_of[sentences[picked[0]]]}
    for i in sorted(range(n), key=lambda k: -imp[k]):
        if len(picked) >= n_summary:
            break
        if i in picked or imp[i] < 0.3 or any(_near_duplicate(sentences[i], sentences[j]) for j in picked):
            continue
        if _task_text(sentences[i], sentences[i - 1] if i else "")[0] and len(picked) >= 1 and n >= 6:
            continue  # tasks are listed under action items
        if group_of[sentences[i]] in used_groups and len(used_groups) < len(groups):
            continue
        picked.append(i)
        used_groups.add(group_of[sentences[i]])
    summary_sentences = [sentences[i] for i in sorted(picked)]
    summary = " ".join(_clip(s, 240) for s in summary_sentences)

    used: set[str] = set()
    sections = []
    for g in groups:
        body = list(g)
        steps_heavy = sum(1 for b in body if _STEP.match(b)) * 2 >= len(body) and len(body) >= 3
        if steps_heavy and "steps" not in used:
            heading = "Steps"
            used.add("steps")
        else:
            heading = _heading(body, stats, used, title_words) if len(groups) > 1 else "Main points"
        for j, s in enumerate(body[:2]):  # an announcement of the topic is already the heading
            topic = intro_topic(s)
            if topic and len(body) >= 3 and topic.lower() in heading.lower():
                body.pop(j)
                break
        if len(body) > cap + 2:  # keep the points that matter most, in the order they were said; steps always stay
            steps = {k for k, b in enumerate(body) if _STEP.match(b)}
            room = max(cap - len(steps), 0)
            others = sorted((k for k in range(len(body)) if k not in steps), key=lambda k: -rank.get(body[k], 0))
            body = [body[k] for k in sorted(steps | set(others[:room]))]
        sections.append({"heading": heading, "bullets": [_clip(x, 260) for x in body]})

    actions = extract_actions(sentences)
    key_points = select_key_points(sentences, exclude=summary_sentences + actions)
    return {"title": _clip(title, 70) or "Untitled note", "summary": summary, "key_points": key_points,
            "sections": sections, "actions": actions}


# ---------------------------------------------------------------- ollama

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
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
        "actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "key_points", "sections", "actions"],
}

SYSTEM_PROMPT = (
    "You turn the transcript of a spoken voice note into clean written notes.\n"
    "Rules:\n"
    "- Write in the language of the transcript.\n"
    "- Use only what the speaker said. Never add facts, names, numbers or dates. Keep numbers, names and amounts exactly.\n"
    "- title: 2 to 6 words naming the topic. Not a sentence, no full stop.\n"
    "- summary: 2 or 3 sentences that say what the note is about and what was decided.\n"
    "- sections: group the content by topic, in the order spoken. Each section has a heading of 1 to 4 words that names "
    "its topic (never \"Notes\", \"Details\", \"Introduction\" or \"Section 1\") and 2 to 6 bullets. Each bullet is one short, "
    "complete statement in your own words, without filler such as \"so\", \"I think\" or \"you know\". Do not repeat a "
    "point. Skip greetings, hesitations and remarks with no content.\n"
    "- Write the subject itself, never the speaker's requests or talk to a listener: for \"Explain to me how X works\" "
    "write \"How X works\". No \"in this note\", no \"as I said\".\n"
    "- When the speaker describes steps in order, keep the order and begin each bullet with First, Then, Next or Finally.\n"
    "- key_points: the 2 to 4 most important facts, decisions or deadlines of the whole text. An empty list when the text "
    "is short.\n"
    "- actions: tasks the speaker or a named person has to do, each a short instruction that starts with a verb, with the "
    "deadline or the person when given. An empty list when there are none.\n"
    "Answer with JSON only."
)

EXAMPLE_TRANSCRIPT = (
    "Okay so I want to note down the plan for fixing my bike. The front tire has a slow leak, I think it's the valve. "
    "So first I take the wheel off, then I replace the tube. It costs about 8 euros at the shop on Main Street. "
    "I also need to buy a new bell, and I have to do it before Saturday because we are riding with Tom."
)
EXAMPLE_NOTES = {
    "title": "Bike repair plan",
    "summary": "The front tire of the bike has a slow leak, probably at the valve. The plan is to replace the tube, "
               "which costs about 8 euros.",
    "key_points": ["The slow leak is probably caused by the valve.", "A new tube costs about 8 euros."],
    "sections": [
        {"heading": "Flat tire", "bullets": ["The front tire has a slow leak, probably at the valve.",
                                             "First, take the wheel off.", "Then replace the tube."]},
        {"heading": "Costs", "bullets": ["A tube costs about 8 euros at the shop on Main Street."]},
    ],
    "actions": ["Buy a new bell before Saturday"],
}

EXAMPLE_TRANSCRIPT_FR = (
    "Alors, euh, je voulais noter le plan pour réparer mon vélo. Le pneu avant a une fuite lente, je pense que c'est "
    "la valve. Donc d'abord je démonte la roue, ensuite je remplace la chambre à air. Ça coûte environ 8 euros au "
    "magasin de la rue Victor Hugo. Il faut aussi que j'achète une nouvelle sonnette avant samedi, parce qu'on roule "
    "avec Tom."
)
EXAMPLE_NOTES_FR = {
    "title": "Réparation du vélo",
    "summary": "Le pneu avant du vélo a une fuite lente, probablement au niveau de la valve. Le plan est de remplacer "
               "la chambre à air, qui coûte environ 8 euros.",
    "key_points": ["La fuite lente vient probablement de la valve.", "Une chambre à air coûte environ 8 euros."],
    "sections": [
        {"heading": "Pneu crevé", "bullets": ["Le pneu avant a une fuite lente, probablement au niveau de la valve.",
                                              "D'abord, démonter la roue.", "Ensuite, remplacer la chambre à air."]},
        {"heading": "Coûts", "bullets": ["Une chambre à air coûte environ 8 euros au magasin de la rue Victor Hugo."]},
    ],
    "actions": ["Acheter une nouvelle sonnette avant samedi"],
}

_FR_WORDS = set("le la les des est une pour que qui dans pas donc avec sur mais ça il faut on je nous vous du au et en ce".split())
_EN_WORDS = set("the and to of is that for with we it in this have will are was be on".split())


def guess_language(text: str, language: str = "auto") -> str:
    """Returns en, fr or other: the chosen language, or a guess from common words when it is automatic."""
    lang = languages.lenient(language)
    if lang != "auto":
        return lang if lang in ("en", "fr") else "other"
    words = re.findall(r"[^\W\d_]+", text.lower())
    fr = sum(1 for w in words if w in _FR_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    if fr >= 3 and fr > en * 1.3:
        return "fr"
    if en >= 3 and en > fr * 1.3:
        return "en"
    return "other"


def ollama_example(text: str, language: str = "auto"):
    """The worked example sent with the request: one in the language of the recording, none for other languages.

    A small model copies the language of the example, so an English example would turn a French note into English.
    """
    kind = guess_language(text, language)
    if kind == "fr":
        return EXAMPLE_TRANSCRIPT_FR, EXAMPLE_NOTES_FR
    if kind == "en":
        return EXAMPLE_TRANSCRIPT, EXAMPLE_NOTES
    return None


SUMMARY_PROMPT = (
    "You receive the summaries of consecutive parts of one long voice note. "
    "Write in the same language. Return JSON with a short title of 2 to 6 words (not a sentence), "
    "a summary of three or four sentences covering the whole note, an empty key_points list, "
    "an empty sections list and an empty actions list."
)

GENERIC_TITLES = {"notes", "note", "voice note", "summary", "untitled", "untitled note", "meeting notes", "transcript",
                  "title", "recording", "voice recording", "notes from the recording",
                  "note vocale", "notes vocales", "r\u00e9sum\u00e9", "sans titre", "titre", "nota de voz", "notas",
                  "resumen", "sin t\u00edtulo", "sprachnotiz", "notizen", "zusammenfassung", "\u0645\u0644\u0627\u062d\u0638\u0627\u062a",
                  "\u0645\u0644\u062e\u0635", "\u7b14\u8bb0", "\u603b\u7ed3"}
ACTION_HEADINGS = {"action items", "action item", "actions", "next steps", "to do", "to-do", "todo", "tasks", "to dos",
                   "follow up", "follow-up", "follow ups", "actions to take", "prochaines etapes", "prochaines \u00e9tapes",
                   "actions \u00e0 faire", "t\u00e2ches", "\u00e0 faire", "pr\u00f3ximos pasos", "tareas", "acciones"}
GENERIC_HEADINGS = {"notes", "note", "details", "introduction", "summary", "overview", "main points", "other",
                    "miscellaneous", "general", "main topic", "key points", "content", "topic", "conclusion",
                    "others", "various", "misc", "the rest", "remarks", "comments", "points",
                    "divers", "autres", "autre", "g\u00e9n\u00e9ral", "g\u00e9n\u00e9ralit\u00e9s", "r\u00e9sum\u00e9", "d\u00e9tails",
                    "points principaux", "aper\u00e7u", "contenu", "sujet", "remarques",
                    "otros", "otro", "resumen", "detalles", "introducci\u00f3n", "contenido", "tema", "conclusi\u00f3n",
                    "notizen", "allgemein", "zusammenfassung", "\u00fcbersicht", "sonstiges", "einleitung", "inhalt", "fazit",
                    "dettagli", "riepilogo", "generale", "altro", "introduzione", "contenuto", "conclusione",
                    "detalhes", "resumo", "geral", "outros", "introdu\u00e7\u00e3o", "conte\u00fado", "conclus\u00e3o",
                    "genel", "di\u011fer", "\u00f6zet", "detaylar", "giri\u015f", "sonu\u00e7",
                    "\u0645\u0644\u0627\u062d\u0638\u0627\u062a", "\u062a\u0641\u0627\u0635\u064a\u0644", "\u0645\u0642\u062f\u0645\u0629", "\u0645\u0644\u062e\u0635", "\u0639\u0627\u0645",
                    "\u0623\u062e\u0631\u0649", "\u062e\u0644\u0627\u0635\u0629", "\u0645\u062d\u062a\u0648\u0649",
                    "\u7b14\u8bb0", "\u7ec6\u8282", "\u6982\u8ff0", "\u5176\u4ed6", "\u603b\u7ed3", "\u4ecb\u7ecd", "\u5185\u5bb9"}
# "Part 2", "Section 3", "Topic 1": a number is no topic
_NUMBERED_HEADING = re.compile(r"^(?:part|section|topic|point|item|partie|secci\u00f3n|seccion|teil|parte|b\u00f6l\u00fcm)\s*\d+$")


def _generic_heading(heading: str) -> bool:
    h = re.sub(r"\s+", " ", heading.lower()).strip(" .:;-")
    return not h or h in GENERIC_HEADINGS or bool(_NUMBERED_HEADING.match(h))


def _is_fragment(text: str) -> bool:
    """One short word is no statement. Texts with figures and languages written without spaces are exempt."""
    return (len(text.split()) < 2 and len(text) < 25 and not re.search(r"\d", text)
            and not _CJK_RUN.search(text) and not re.search(r"[\u0600-\u06ff]", text))


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


def engine_plan() -> dict:
    """Which engine will structure the next note and, when the first choice is not available, why.

    With Groq in use, "backup" says what happens if Groq fails.
    """
    local = _ollama_plan()
    if groq_enabled():
        model = groq_status()["model"]
        return {"engine": f"groq:{model}", "model": model, "reason": "", "backup": local,
                "review": settings.groq_review}
    return local


def _ollama_plan() -> dict:
    """The local part of the plan: the Ollama model when it is there, else the rules and why."""
    st = ollama_status()
    model = st["model"]
    if st["available"]:
        return {"engine": f"ollama:{model}", "model": model, "reason": ""}
    reasons = {
        "disabled": "The local model is switched off on this server (USE_OLLAMA=0).",
        "not reachable": "The local model server (Ollama) is not running on this server.",
        "model not installed": f"The model {model} is not installed in Ollama on this server.",
    }
    return {"engine": "rule-based", "model": model, "reason": reasons.get(st["reason"], "The local model is not available.")}


def _ollama_chat(system: str, user: str, example=False) -> dict:
    """`example` is True for the English example, a (transcript, notes) pair for another one, or False for none."""
    messages = [{"role": "system", "content": system}]
    if example:  # one worked example helps small models keep to the format and the style
        sample_text, sample_notes = (EXAMPLE_TRANSCRIPT, EXAMPLE_NOTES) if example is True else example
        messages += [{"role": "user", "content": f"Transcript:\n{sample_text}"},
                     {"role": "assistant", "content": json.dumps(sample_notes, ensure_ascii=False)}]
    messages.append({"role": "user", "content": user})
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "format": NOTES_SCHEMA,
        "options": {"temperature": 0.1, "top_p": 0.9, "repeat_penalty": 1.1, "num_ctx": 4096, "num_predict": 1400},
        "keep_alive": "10m",
        "messages": messages,
    }
    try:
        r = httpx.post(f"{settings.ollama_host}/api/chat", json=payload, timeout=settings.ollama_timeout)
        r.raise_for_status()
        content = r.json()["message"]["content"]
        return json.loads(content)
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise StructureError(f"ollama call failed: {type(exc).__name__}")


def _clean_title(title: str) -> str:
    t = re.sub(r"^(?:title|titre|título)\s*:\s*", "", str(title or "").strip(), flags=re.IGNORECASE)
    t = speech.scrub(t.strip().strip("\"'")).rstrip(".:;").strip()
    if len(t.split()) > 10:
        t = short_phrase(t, 8)
    return t


_NUMBER_SPECS = (
    "one=1 two=2 three=3 four=4 five=5 six=6 seven=7 eight=8 nine=9 ten=10 eleven=11 twelve=12 thirteen=13 "
    "fourteen=14 fifteen=15 sixteen=16 seventeen=17 eighteen=18 nineteen=19 twenty=20 thirty=30 forty=40 fifty=50 "
    "sixty=60 seventy=70 eighty=80 ninety=90 hundred=100 thousand=1000 million=1000000",
    "un=1 une=1 deux=2 trois=3 quatre=4 cinq=5 sept=7 huit=8 neuf=9 dix=10 onze=11 douze=12 treize=13 quatorze=14 "
    "quinze=15 seize=16 vingt=20 trente=30 quarante=40 cinquante=50 soixante=60 cent=100 mille=1000",
    "uno=1 una=1 dos=2 tres=3 cuatro=4 cinco=5 seis=6 siete=7 ocho=8 nueve=9 diez=10 doce=12 trece=13 catorce=14 "
    "quince=15 veinte=20 treinta=30 cuarenta=40 cincuenta=50 sesenta=60 setenta=70 ochenta=80 noventa=90 cien=100 mil=1000",
    "eins=1 zwei=2 drei=3 vier=4 fünf=5 sechs=6 sieben=7 acht=8 neun=9 zehn=10 elf=11 zwölf=12 zwanzig=20 "
    "dreißig=30 vierzig=40 fünfzig=50 sechzig=60 siebzig=70 achtzig=80 neunzig=90 hundert=100 tausend=1000",
    "due=2 tre=3 quattro=4 cinque=5 sette=7 otto=8 dieci=10 undici=11 dodici=12 venti=20 trenta=30 quaranta=40 "
    "cinquanta=50 sessanta=60 settanta=70 ottanta=80 novanta=90 cento=100",
    "dois=2 duas=2 três=3 oito=8 dez=10 doze=12 vinte=20 trinta=30 quarenta=40 cinquenta=50 sessenta=60 setenta=70 "
    "oitenta=80 noventa=90 cem=100",
    "bir=1 iki=2 üç=3 dört=4 beş=5 altı=6 yedi=7 sekiz=8 dokuz=9 yirmi=20 otuz=30 kırk=40 elli=50 altmış=60 "
    "yetmiş=70 seksen=80 doksan=90 yüz=100",
)
NUMBER_WORDS: dict[str, int] = {w: int(v) for spec in _NUMBER_SPECS for w, v in (p.split("=") for p in spec.split())}

_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_AR_MARKS = re.compile(r"[\u064b-\u065f\u0670\u0640]")
_AR_PREFIX = re.compile(r"^(?:وال|بال|كال|فال|لل|ال|و)(?=.{3,})")
_TOKEN = re.compile(r"[^\W\d_]+")
_NOT_NAME_START = ".!?:;…\"“«(-•*\n"


def _starts_sentence(text: str, pos: int) -> bool:
    before = text[max(0, pos - 4):pos].rstrip()
    return not before or before[-1] in _NOT_NAME_START


def _stem(word: str) -> str:
    """A short form of a word, so that endings and Arabic prefixes do not hide a match."""
    w = word.lower()
    if re.search(r"[\u0600-\u06ff]", w):
        w = _AR_PREFIX.sub("", _AR_MARKS.sub("", w))
    return w[:5]


def _bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for run in _CJK_RUN.findall(text):
        out |= {run[i:i + 2] for i in range(len(run) - 1)}
    return out


def _number_word_values(text: str) -> set[int]:
    """Numbers that the transcript spells out as words ("twenty", "cinq", "veinticinco" is not covered)."""
    vals = {NUMBER_WORDS[w] for w in _TOKEN.findall(text.lower()) if w in NUMBER_WORDS}
    tens = {v for v in vals if 20 <= v < 100}
    units = {v for v in vals if 1 <= v <= 9}
    extra = {t + u for t in tens for u in units}
    small = {v for v in vals | extra if v < 100}
    for big in (v for v in vals if v in (100, 1000, 1000000)):
        extra |= {big * k for k in small}
    return vals | extra


class _Source:
    """What the transcript contains, used to check that notes only say what was said."""

    def __init__(self, text: str, language: str = "auto"):
        flat = _CJK_RUN.sub(" ", text)
        self.stems = {_stem(w) for w in _kw(flat)}
        self.all_stems = {_stem(w) for w in re.findall(r"[^\W\d_]{2,}", flat.lower())}
        self.bigrams = _bigrams(text)
        self.digits = {int(d) for d in re.findall(r"\d+", text)}
        self.number_words = _number_word_values(text)
        mid = [m.group(0) for m in _TOKEN.finditer(flat) if len(m.group(0)) >= 3 and not _starts_sentence(flat, m.start())]
        caps = sum(1 for t in mid if t[0].isupper())
        # German writes every noun with a capital letter, so capitals say nothing about names there.
        self.capitals_are_nouns = language == "de" or (len(mid) >= 15 and caps / len(mid) > 0.18)

    def support(self, text: str) -> float:
        """The share of the content words of `text` that appear in the transcript (stems, or pairs of characters)."""
        words = _kw(_CJK_RUN.sub(" ", text))
        pairs = _bigrams(text)
        total = len(words) + len(pairs)
        if not total:
            return 1.0
        found = sum(1 for w in words if _stem(w) in self.stems) + sum(1 for p in pairs if p in self.bigrams)
        return found / total

    def numbers_ok(self, text: str, lenient: bool = False) -> bool:
        for d in re.findall(r"\d+", text):
            n = int(d)
            if n in self.digits or n <= 12 or (lenient and n in self.number_words):
                continue
            return False
        return True

    def names_ok(self, text: str) -> bool:
        """Capitalised words in the middle of a sentence (names, places, products) must occur in the transcript."""
        if self.capitals_are_nouns:
            return True
        for m in _TOKEN.finditer(text):
            tok = m.group(0)
            if len(tok) < 3 or not tok[0].isupper() or tok.lower() in STOPWORDS:
                continue
            if _starts_sentence(text, m.start()):
                continue
            if _stem(tok) not in self.all_stems:
                return False
        return True


def _tidy(text) -> str:
    """Light cleaning for text written by a strong model: spacing, bullet marks and the first letter."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    s = re.sub(r"^(?:(?:[\-\*•]|\[[ xX]?\])\s*)+", "", s)
    s = speech.scrub(s)
    return s[:1].upper() + s[1:] if s else ""


def _covers(task: str, other: str) -> bool:
    """True when almost every content word of the shorter text is in the longer one (the same task, worded twice)."""
    a, b = set(_kw(task)), set(_kw(other))
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.75


def _without_repeated_tasks(sections: list[dict], actions: list[str]) -> list[dict]:
    """Drop bullets that are a task already in the checklist ("I need to ask Tom ..." next to "Ask Tom ...").

    A small model often writes each task as its own one-point section as well. Nothing is dropped when it would
    leave the notes empty.
    """
    if not actions:
        return sections
    out = []
    for sec in sections:
        keep = []
        for b in sec["bullets"]:
            tasks = extract_actions([b])
            if tasks and any(_covers(tasks[0], a) for a in actions):
                continue
            keep.append(b)
        if keep:
            out.append({"heading": sec["heading"], "bullets": keep})
    return out or sections


def refine_model_notes(notes: dict, source: str, relaxed: bool = False, language: str = "auto") -> dict:
    """Check what a model returned against the transcript and repair it with the rules.

    Bullets, actions and key points that say things the speaker did not say (words or numbers that are not in
    the source) are dropped; filler and empty remarks are removed; a generic title or heading is replaced by one
    the rules find; tasks the model missed are added from the rules. Raises StructureError when nothing is left.

    With `relaxed` set (output of a strong model) the rewriting is trusted more: the model's own wording is kept
    instead of being cleaned by the filler rules, fewer of the words have to come from the transcript, numbers
    may also be written out in the transcript, and no tasks are added from the rules. Numbers and names must
    still come from the transcript.
    """
    src = _Source(source, language)
    prefixes = {w[:5] for w in _kw(source)}
    digits = set(re.findall(r"\d+", source))

    def supported(text: str, threshold: float = 0.5) -> bool:
        if relaxed:
            return src.numbers_ok(text, True) and src.names_ok(text) and src.support(text) >= min(threshold, 0.3)
        ws = _kw(text)
        if ws and sum(1 for w in ws if w[:5] in prefixes) / len(ws) < threshold:
            return False
        return all(d in digits or int(d) <= 12 for d in re.findall(r"\d+", text))

    def label_ok(text: str) -> bool:
        return not relaxed or (src.numbers_ok(text, True) and src.names_ok(text))

    rules: dict | None = None

    def fallback() -> dict:
        nonlocal rules
        if rules is None:
            rules = structure_rules(source)
        return rules

    seen: list[str] = []
    sections = []
    for sec in notes["sections"]:
        bullets = []
        for b in sec["bullets"]:
            c = _tidy(b) if relaxed else clean_sentence(b)
            if (not c or is_empty_remark(c) or (relaxed and _is_fragment(c)) or not supported(c)
                    or any(_near_duplicate(c, x) for x in seen)):
                continue
            seen.append(c)
            bullets.append(c)
        if not bullets:
            continue
        heading = sec["heading"].strip().rstrip(".:;").strip()
        if _generic_heading(heading) or len(heading.split()) > 7 or not label_ok(heading):
            heading = best_phrase(bullets) or short_phrase(heading, 5) or "Notes"
        sections.append({"heading": heading, "bullets": bullets})
    # A section that only lists tasks ("Action items", "Next steps") repeats the checklist: its tasks move there.
    moved: list[str] = []
    kept = []
    for sec in sections:
        if sec["heading"].lower().strip(" :.") in ACTION_HEADINGS:
            tasks = [b for b in sec["bullets"] if extract_actions([b])]
            rest = [b for b in sec["bullets"] if b not in tasks]
            moved += tasks
            if rest:
                kept.append({"heading": sec["heading"], "bullets": rest})
        else:
            kept.append(sec)
    sections = kept
    if not sections:
        raise StructureError("the model produced no usable bullets")

    title = _clean_title(notes.get("title"))
    if not title or title.lower() in GENERIC_TITLES or not label_ok(title):
        title = fallback()["title"]
    summary = (notes.get("summary") or "").strip()
    if not summary or not supported(summary, 0.4):
        summary = fallback()["summary"]
    elif relaxed:
        summary = _tidy(summary)
    if relaxed:
        actions = [_tidy(a) for a in notes.get("actions") or [] if supported(str(a))]
        key_points = [_tidy(k) for k in notes.get("key_points") or [] if supported(str(k)) and not is_empty_remark(str(k))]
    else:
        actions = [a for a in notes.get("actions") or [] if supported(a)]
        if not actions:  # a small model often leaves the tasks out; when it lists some, its wording is kept
            actions = extract_actions(_prepare_sentences(source))
        key_points = [k for k in notes.get("key_points") or [] if supported(k) and not is_empty_remark(k)]
    for task in extract_actions(moved):
        if not any(_near_duplicate(task, a) for a in actions):
            actions.append(task)
    if not relaxed:  # a strong model's wording is trusted; a small one repeats its tasks as one-point sections
        sections = _without_repeated_tasks(sections, actions)
    return normalize_notes({"title": title, "summary": summary, "key_points": key_points, "sections": sections,
                            "actions": actions})


def _notifier(on_progress):
    """Wrap a progress callback. A callback that takes a third argument also gets a short text for the step."""
    if on_progress is None:
        return lambda done, total, step="": None
    try:
        kinds = [p.kind for p in inspect.signature(on_progress).parameters.values()]
    except (TypeError, ValueError):
        kinds = []
    positional = sum(1 for k in kinds if k in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD))
    wide = inspect.Parameter.VAR_POSITIONAL in kinds or positional >= 3

    def notify(done: int, total: int, step: str = "") -> None:
        if wide:
            on_progress(done, total, step)
        else:
            on_progress(done, total)

    return notify


def _digest_summary(results: list[dict]) -> str:
    """A summary of the whole note made of the first sentence of each part summary (used when no model gives one)."""
    firsts = []
    for r in results:
        sentences = split_sentences(r.get("summary") or "")
        if sentences:
            firsts.append(sentences[0])
    return " ".join(firsts[:5])


def _structure_pieces(
    text: str,
    limit: int,
    chat: Callable[[str], dict],
    head: Callable[[str], dict] | None,
    refine: Callable[[dict, str], dict],
    on_progress: Callable[..., None] | None = None,
    review: Callable[[dict, str], dict] | None = None,
    final: Callable[[str, list[str], list[str]], dict] | None = None,
    polish: Callable[[dict], dict] | None = None,
) -> tuple[dict, int]:
    """Structure a transcript piece by piece with a model. Returns (notes, number of pieces done by the rules).

    A piece the model cannot handle is structured by the rules instead of throwing away the pieces that
    worked. After two failures in a row the model is assumed to be unavailable and the remaining pieces use
    the rules. `chat(piece)` returns the raw notes of one piece, `head(digest)` the title and summary of the
    whole note, `refine(notes, piece)` checks and repairs one piece.

    With `review`, each piece that the model wrote is checked once more: `review(notes, piece)` returns the
    notes to keep (the draft itself when the review is not better). A review that raises StructureError keeps
    the draft; it does not count as a failure of the model, but two reviews in a row that fail stop the reviews
    for the rest of the note. With `final(digest, headings, key_points)` the title, the summary and the key
    points of a note with several pieces come from one call that sees the merged headings and the candidates.

    The progress callback gets (done, total) and, when it takes a third argument, a text such as "writing part
    2 of 3". With a review every piece is two steps.
    """
    pieces = split_pieces(text, limit)
    if not pieces:
        raise StructureError("The transcript is empty.")
    n = len(pieces)
    notify = _notifier(on_progress)
    steps = 2 * n if review else n
    total = steps + (1 if review and n > 1 else 0) + (1 if polish else 0)
    results: list[dict] = []
    fallback_pieces = 0

    def polished(notes: dict) -> dict:
        """The last editing pass on the wording of the finished note. A pass that fails keeps the notes as they are."""
        if polish is None or fallback_pieces >= n:
            return notes
        notify(total - 1, total, "polishing the wording")
        try:
            return polish(notes) or notes
        except (StructureError, ValueError, TypeError, KeyError, AttributeError) as exc:
            log.info("final editing pass skipped, the notes are kept (%s)", exc)
            return notes
        finally:
            notify(total, total)

    streak = 0
    review_misses = 0
    last_error = ""
    for i, piece in enumerate(pieces):
        done = 2 * i if review else i
        notify(done, total, f"writing part {i + 1} of {n}" if review else f"part {i + 1} of {n}")
        notes = None
        if streak < 2:
            try:
                notes = refine(normalize_notes(chat(piece)), piece)
                streak = 0
            except StructureError as exc:
                streak += 1
                last_error = str(exc)
        if notes is not None and review and review_misses < 2:
            notify(done + 1, total, f"checking part {i + 1} of {n}")
            try:
                notes = review(notes, piece) or notes
                review_misses = 0
            except (StructureError, ValueError, TypeError, KeyError, AttributeError) as exc:
                review_misses += 1
                log.info("review of part %d skipped, the draft is kept (%s)", i + 1, exc)
        if notes is None:
            try:
                notes = structure_rules(piece)
            except StructureError:
                continue
            fallback_pieces += 1
        results.append(notes)
    notify(steps, total, "combining the parts" if total > steps else "")
    if not results or fallback_pieces == n:
        raise StructureError(last_error or "the model produced no usable notes")
    if len(results) == 1:
        return polished(results[0]), fallback_pieces
    sections: list[dict] = []
    for r in results:
        for sec in r["sections"]:
            if sections and sections[-1]["heading"].lower() == sec["heading"].lower():
                sections[-1] = {"heading": sections[-1]["heading"],
                                "bullets": (sections[-1]["bullets"] + sec["bullets"])[:20]}
            else:
                sections.append(sec)
    actions = _clean_list([a for r in results for a in r.get("actions", [])], 160, 10, bullet_marks=True)
    candidates: list[str] = []
    for r in results:
        for k in r.get("key_points", []):
            if not any(_near_duplicate(k, c) for c in candidates):
                candidates.append(k)
    key_points = candidates
    if len(key_points) > 5:
        imp = _importance(key_points)
        key_points = [key_points[i] for i in sorted(sorted(range(len(key_points)), key=lambda k: -imp[k])[:5])]
    digest = "\n".join(f"Part {i + 1}: {r['summary']}" for i, r in enumerate(results))
    title, summary = "", ""
    try:
        top = final(digest, [sec["heading"] for sec in sections], candidates) if final else head(digest)
        if not isinstance(top, dict):
            top = {}
        title = _clean_title(top.get("title"))
        summary = str(top.get("summary") or "").strip()
        chosen = [str(k).strip() for k in _as_list(top.get("key_points")) if str(k).strip()] if final else []
        if len(chosen) >= 2 or (chosen and len(candidates) < 2):  # a single survivor of many candidates: keep the rules' pick
            key_points = chosen[:5]
    except StructureError:
        title, summary = "", ""
    if not title or title.lower() in GENERIC_TITLES:
        title = structure_rules(text)["title"]
    if not summary:
        summary = _digest_summary(results) or structure_rules(text)["summary"]
    merged = {"title": _clip(title, 100), "summary": summary, "key_points": key_points,
              "sections": sections[:30], "actions": actions}
    if polish is None and total > steps:
        notify(total, total)
    return polished(merged), fallback_pieces


def ollama_system_prompt(language: str = "auto") -> str:
    lang = languages.lenient(language)
    if lang == "auto":
        return SYSTEM_PROMPT
    name = languages.LANGUAGES[lang]
    return SYSTEM_PROMPT.replace("- Write in the language of the transcript.\n",
                                 f"- The recording is in {name}. Write the notes in {name}. Do not translate.\n")


def structure_ollama(
    text: str, on_progress: Callable[[int, int], None] | None = None, language: str = "auto"
) -> tuple[dict, int]:
    """Structure with the local model. Returns (notes, number of pieces done by the fallback)."""
    system = ollama_system_prompt(language)
    example = ollama_example(text, language)
    return _structure_pieces(
        text,
        MAX_PIECE_CHARS,
        lambda piece: _ollama_chat(system, f"Transcript:\n{piece}", example=example or False),
        lambda digest: _ollama_chat(SUMMARY_PROMPT, digest),
        lambda notes, piece: refine_model_notes(notes, piece),
        on_progress,
    )


# ------------------------------------------------------------------ groq

GROQ_PREFERRED = ("llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "llama3-70b-8192")
_NOT_CHAT = ("whisper", "guard", "tts", "orpheus", "embed", "playai")
GROQ_PIECE_CHARS = 12000  # about 3000 tokens of transcript per request

_models_cache: dict = {"key": None, "at": 0.0, "ids": None, "resolved": None}


def reset_groq_cache() -> None:
    _models_cache.update(key=None, at=0.0, ids=None, resolved=None)


def groq_enabled() -> bool:
    return settings.use_groq_chat and bool(settings.groq_api_key)


def groq_models() -> list[str] | None:
    """Chat models that Groq offers to this key (GET /models), cached for ten minutes. None when it cannot be read."""
    cache_key = (settings.groq_models_url, settings.groq_api_key)
    if _models_cache["key"] == cache_key and _models_cache["ids"] is not None and time.time() - _models_cache["at"] < 600:
        return _models_cache["ids"]
    try:
        r = httpx.get(settings.groq_models_url, headers={"Authorization": f"Bearer {settings.groq_api_key}"}, timeout=8.0)
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")]
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return None
    ids = [i for i in ids if not any(x in i.lower() for x in _NOT_CHAT)]
    _models_cache.update(key=cache_key, at=time.time(), ids=ids, resolved=None)
    return ids


def resolve_groq_model() -> str:
    """The configured chat model when Groq lists it, else the best 70B-class model that it does list."""
    want = settings.groq_chat_model
    ids = groq_models()
    if ids is None or want in ids:  # when the list cannot be read, try the configured model anyway
        model = want
    else:
        model = next((m for m in GROQ_PREFERRED if m in ids), None) or next(
            (i for i in sorted(ids) if "70b" in i.lower() or "120b" in i.lower()), None)
        if not model:
            raise StructureError("Groq lists no suitable chat model for this key.")
        log.warning("groq model %s is not available, using %s", want, model)
    _models_cache["resolved"] = model
    return model


def groq_status() -> dict:
    """Whether Groq is used for structuring and with which model (no network call)."""
    model = _models_cache["resolved"] or settings.groq_chat_model
    if not settings.use_groq_chat:
        return {"enabled": False, "model": model, "reason": "disabled"}
    if not settings.groq_api_key:
        return {"enabled": False, "model": model, "reason": "no key"}
    return {"enabled": True, "model": model, "reason": "", "review": settings.groq_review,
            "polish": settings.groq_polish}


GROQ_SYSTEM_PROMPT = (
    "You write clean notes from the transcript of a spoken voice note. The notes will be shared with team members "
    "or classmates who were not there, so they must be clear, well organised and complete on their own.\n"
    "Rules:\n"
    "- Rewrite the speech into proper written sentences. Remove fillers, stutters, repetitions and false starts. "
    "Never copy the spoken wording word for word.\n"
    "- Never invent facts, names, numbers or dates. Every name, number and date in the notes must come from the "
    "transcript, written as it was said: keep amounts and units, and keep relative dates such as \"next Friday\" "
    "as they were said. Leave out anything that is unclear.\n"
    "- {language_rule}\n"
    "- Keep it concise in wording but complete in content: never drop a decision, a figure, a name, a date, a "
    "deadline or a reason to save space, and do not repeat a point. Leave out greetings, small talk and asides.\n"
    "- Use a neutral, impersonal tone, like a record written for readers who were not there. No first person "
    "filler such as \"I think\" or \"I want to say\", no \"the speaker says\", do not address the speaker and do "
    "not describe the recording itself. When the speaker is unsure, say so with a word like \"probably\" or "
    "\"possibly\". When several people are named, say who decided, said or owns what.\n"
    "- The notes are a written document about the subject, not a record of the recording. When the speaker talks to "
    "a listener or to an assistant (\"explain to me how X works\", \"tell me about X\", \"can you check X\", "
    "\"listen\", \"voila\", \"as I said\", \"explique moi ...\"), keep only the subject and write the topic or "
    "the question itself (\"How X works\"), never the request. Nothing may only make sense when heard: no \"in this "
    "note\", \"let us see\", \"as I said\", no question to the listener and no remark about the recording. Replace "
    "\"this\", \"that\", \"it\" and \"they\" by the noun they stand for, so that every bullet can be understood "
    "alone.\n"
    "Return one JSON object with exactly these keys:\n"
    "- \"title\": 2 to 8 words naming the topic. Not a sentence, no full stop.\n"
    "- \"summary\": 2 to 4 sentences saying what the note is about and what was decided or concluded.\n"
    "- \"key_points\": the 2 to 5 most important facts, decisions or deadlines, each a complete sentence that "
    "does not just repeat the summary. An empty list when the note is short.\n"
    "- \"sections\": a list of objects with \"heading\" and \"bullets\". Group the content by topic, in the order "
    "spoken. A heading has 1 to 5 words and names its topic (never \"Notes\", \"Details\", \"Overview\" or "
    "\"Introduction\"). Each section has as many bullets as its content needs, usually 2 to 7, and every bullet is "
    "one complete, clean sentence with one idea, never a fragment. When the speaker describes steps in order, keep "
    "that order.\n"
    "- \"action_items\": the tasks that someone has to do, each a short instruction that starts with a verb. Name "
    "the owner only when the speaker gave a name and the deadline only when the speaker gave one; never guess "
    "either. An empty list when there are none.\n"
    "Answer with the JSON object only."
)

GROQ_REVIEW_PROMPT = (
    "You are the editor of notes that were written from the transcript of a spoken voice note. You receive the "
    "transcript and the draft notes as JSON. The notes will be shared with team members or classmates who were not "
    "there, so they must be accurate, complete and clear on their own. Check the draft against the transcript and "
    "return an improved version.\n"
    "Rules:\n"
    "- Restore the important facts, decisions, names, figures and deadlines that the draft left out.\n"
    "- Fix wrong attributions (who said, decided or owns what) and ideas that were merged or mixed up.\n"
    "- Split a bullet that carries two ideas into two bullets. Remove duplicates, filler and remarks without content.\n"
    "- Every bullet is one complete, clean sentence in a neutral tone: no \"I think\", no \"the speaker says\", no "
    "talk about the recording itself.\n"
    "- Make every heading specific: 1 to 5 words naming its topic, never generic (\"Notes\", \"Details\", "
    "\"Overview\", \"Introduction\", \"Other\").\n"
    "- An action item starts with a verb. It names the owner and the deadline only when the speaker said them; "
    "never guess either.\n"
    "- Remove every trace of speech. A request to a listener or an assistant (\"explain to me how X works\", "
    "\"tell me about X\", \"explique moi ...\", \"can you ...\") becomes the topic or the question itself (\"How X "
    "works\"). Delete \"in this note\", \"as I said\", \"let us see\", greetings, questions to the listener and "
    "remarks about the recording. A bullet must be understandable alone: replace \"this\", \"that\" and \"it\" by "
    "the noun they stand for.\n"
    "- Keep the order of the speaker.\n"
    "- Never invent anything. Every name, number and date must come from the transcript, written as it was said.\n"
    "- {language_rule}\n"
    "- When the draft is already good, return it with only small fixes. Do not shorten it.\n"
    "Return one JSON object with the same keys as the draft: \"title\", \"summary\", \"key_points\", \"sections\" "
    "(a list of objects with \"heading\" and \"bullets\") and \"action_items\".\n"
    "Answer with the JSON object only."
)


GROQ_POLISH_PROMPT = (
    "You are the final editor of notes that were made from a spoken voice note. You receive the notes as JSON, "
    "usually with the transcript. The notes will be read, shared and printed as a document by people who never heard "
    "the recording. Your only job is the wording and the order: every fact stays.\n"
    "Rules:\n"
    "- The result must read like notes written by a careful person at a desk, not like a summary of a voice message.\n"
    "- Remove every trace of speech: requests to a listener or an assistant (\"explain to me how X works\", \"tell "
    "me about X\", \"explique moi ...\", \"can you ...\") become the topic or the question itself (\"How X works\"); "
    "delete \"in this note\", \"as I said\", \"let us see\", greetings, questions to the listener, the speaker as a "
    "person (\"the speaker thinks\", \"I\", \"you\") and every remark about the recording.\n"
    "- Every bullet is one complete sentence that can be understood alone: replace \"this\", \"that\", \"it\", "
    "\"they\" and \"the latter\" by the noun they stand for. Use the same term for the same thing everywhere.\n"
    "- The summary describes the subject and its outcome in 2 to 4 sentences. It never starts with \"This note\", "
    "\"The speaker\" or \"The recording\".\n"
    "- Headings are specific noun phrases of 1 to 5 words. Move a bullet to the section where it belongs, merge "
    "sections about the same topic and keep a logical order (context, facts, decisions, next steps). Steps stay in "
    "the order spoken.\n"
    "- Key points are the 2 to 5 facts a reader must not miss and do not repeat the summary word for word. Action "
    "items start with a verb and keep the owner and the deadline only when the transcript has them.\n"
    "- Never drop a fact, a decision, a figure, a name, a date or a deadline, and never invent one. Every name, "
    "number and date must be in the notes or the transcript exactly as given.\n"
    "- {language_rule}\n"
    "Return one JSON object with the keys \"title\", \"summary\", \"key_points\", \"sections\" (a list of objects "
    "with \"heading\" and \"bullets\") and \"action_items\". Answer with the JSON object only."
)


def groq_polish_prompt(language: str = "auto") -> str:
    return GROQ_POLISH_PROMPT.format(language_rule=_language_rule(language))


# The whole transcript is shown to the final editor when it is not longer than this.
POLISH_TRANSCRIPT_CHARS = 14000


def _language_rule(language: str = "auto") -> str:
    lang = languages.lenient(language)
    if lang == "auto":
        return ("Write the notes in the language of the recording. Do not translate. Every title, heading, bullet, "
                "summary, key point and action item must be in that language, even though these instructions are "
                "in English.")
    name = languages.LANGUAGES[lang]
    return (f"The recording is in {name}. Write all the notes in {name}. Do not translate. Every title, heading, "
            f"bullet, summary, key point and action item must be in {name}, even though these instructions are in "
            "English.")


def groq_system_prompt(language: str = "auto") -> str:
    return GROQ_SYSTEM_PROMPT.format(language_rule=_language_rule(language))


def groq_review_prompt(language: str = "auto") -> str:
    return GROQ_REVIEW_PROMPT.format(language_rule=_language_rule(language))


def groq_head_prompt(language: str = "auto") -> str:
    lang = languages.lenient(language)
    same = "the same language" if lang == "auto" else languages.LANGUAGES[lang]
    return (
        "You receive the summaries of consecutive parts of one long voice note, then the section headings and the "
        "candidate key points of the whole note. "
        f"Write in {same}, do not translate, and never add facts, names, numbers or dates that are not in them. "
        "Return a JSON object with these keys: "
        "\"title\": 2 to 8 words naming the topic of the whole note (not a sentence, not generic); "
        "\"summary\": 3 to 5 sentences covering the whole note, in the order of the parts, saying what it is about "
        "and what was decided or concluded; "
        "\"key_points\": the 2 to 5 most important facts, decisions or deadlines of the whole note, each a complete "
        "sentence, chosen among the candidates or condensed from them (merge candidates that say the same thing, "
        "and do not repeat the summary); "
        "\"sections\": an empty list; \"action_items\": an empty list."
    )


# ---- language check: a model told to write French or Arabic sometimes answers in English
_ENGLISH_FUNCTION = set("the and of is are will with that this for be they their have has should".split())
_ARABIC_CHAR = re.compile(r"[؀-ۿ]")
_CJK_CHAR = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")


def _note_strings(notes: dict) -> list[str]:
    out = [str(notes.get("title") or ""), str(notes.get("summary") or "")]
    out += [_item_text(k) for k in _as_list(notes.get("key_points")) + _as_list(notes.get("actions"))]
    for sec in _as_list(notes.get("sections")):
        if isinstance(sec, dict):
            out.append(str(sec.get("heading") or ""))
            out += [_item_text(b) for b in _as_list(sec.get("bullets"))]
    return [x for x in out if x.strip()]


def _language_drift(notes: dict, language: str, source: str) -> bool:
    """True when the notes are clearly not in the chosen language although the transcript is."""
    lang = languages.lenient(language)
    if lang in ("auto", "en"):
        return False
    text = " ".join(_note_strings(notes))
    if lang in ("ar", "zh"):
        rx = _ARABIC_CHAR if lang == "ar" else _CJK_CHAR

        def share(t: str) -> float:
            letters = [c for c in t if c.isalpha()]
            return sum(1 for c in letters if rx.match(c)) / len(letters) if letters else 0.0

        return sum(c.isalpha() for c in text) >= 20 and share(text) < 0.5 and share(source) >= 0.5

    def english(t: str) -> tuple[int, float]:
        words = re.findall(r"[^\W\d_]+", t.lower())
        return len(words), (sum(1 for w in words if w in _ENGLISH_FUNCTION) / len(words) if words else 0.0)

    count, ratio = english(text)
    return count >= 25 and ratio >= 0.12 and english(source)[1] < 0.06


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    """The wait that Groq asks for (header, else the "try again in 7.6s" of the message), not capped."""
    try:
        return max(float(resp.headers.get("retry-after", "")), 0.0)
    except ValueError:
        pass
    m = re.search(r"try again in\s+(?:(\d+)m)?(\d+(?:\.\d+)?)(ms|s)", resp.text or "")
    if m:
        secs = float(m.group(2)) / (1000.0 if m.group(3) == "ms" else 1.0) + 60.0 * int(m.group(1) or 0)
        return secs
    return 2.0 * (attempt + 1)


MAX_RETRY_WAIT = 20.0


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    return min(_retry_wait(resp, attempt), MAX_RETRY_WAIT)


def _action_text(item) -> str:
    if isinstance(item, dict):
        task = str(item.get("task") or item.get("action") or item.get("text") or item.get("instruction") or "").strip()
        owner = str(item.get("owner") or item.get("assignee") or "").strip()
        return f"{owner}: {task}" if owner and task and owner.lower() not in task.lower() else task
    return str(item or "").strip()


def _groq_chat(model: str, system: str, user: str, optional: bool = False) -> dict:
    """One chat completion that returns a JSON object. Raises StructureError with a message safe to show.

    A rate limit or a server error is retried twice, waiting as long as Groq asks but never more than 20 seconds.
    With `optional` (the review of a draft that is already good enough) the call is given up at once when Groq
    asks for a longer wait than that.
    """
    key = settings.groq_api_key
    if not key:
        raise StructureError("Groq is not configured.")
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": settings.groq_chat_max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    timeout = httpx.Timeout(float(settings.groq_chat_timeout), connect=10.0)
    resp = None
    for attempt in range(3):
        try:
            resp = httpx.post(settings.groq_chat_url, headers={"Authorization": f"Bearer {key}"}, json=payload,
                              timeout=timeout)
        except httpx.TimeoutException:
            raise StructureError("Groq did not answer in time.")
        except httpx.HTTPError:
            raise StructureError("Groq could not be reached.")
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            if optional and _retry_wait(resp, attempt) > MAX_RETRY_WAIT:
                break
            time.sleep(_retry_after(resp, attempt))
            continue
        if resp.status_code == 400 and attempt < 2 and "json_validate_failed" in resp.text:
            continue  # the model broke its own JSON; asking again almost always works
        break
    if resp.status_code in (401, 403):
        raise StructureError("Groq rejected the API key.")
    if resp.status_code != 200:
        raise StructureError(f"Groq returned an error ({resp.status_code}).")
    try:
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError, TypeError):
        raise StructureError("Groq sent an answer that could not be read.")
    if not isinstance(data, dict):
        raise StructureError("Groq sent an answer that could not be read.")
    items = data["action_items"] if "action_items" in data else data.get("actions")
    data["actions"] = [t for t in (_action_text(a) for a in items or []) if t] if isinstance(items, list) else []
    return data


def _bullet_count(notes: dict) -> int:
    return sum(len(sec["bullets"]) for sec in notes["sections"])


def _figures(notes: dict) -> set[str]:
    return set(re.findall(r"\d+", " ".join(_note_strings(notes))))


def _review_is_better(draft: dict, reviewed: dict, kept: int, language: str, source: str) -> bool:
    """False when the reviewed notes are clearly worse than the draft (see structure_groq)."""
    before, after = _bullet_count(draft), _bullet_count(reviewed)
    if before - after > before / 3:  # lost more than a third of the bullets
        return False
    if kept - after > max(1, kept // 4):  # the checks had to throw out a lot: do not trust this answer
        return False
    if draft.get("actions") and not reviewed.get("actions"):
        return False
    old = _figures(draft)
    if old and len(old - _figures(reviewed)) > len(old) / 2:  # most of the figures are gone
        return False
    return not _language_drift(reviewed, language, source)


def _validated_head(raw: dict, text: str, language: str) -> dict:
    """The title, summary and key points of a whole note, kept only when the transcript supports them."""
    src = _Source(text, languages.lenient(language))

    def ok(t: str) -> bool:
        return src.numbers_ok(t, True) and src.names_ok(t)

    title = _clean_title(raw.get("title"))
    if not ok(title):
        title = ""
    summary = _tidy(raw.get("summary"))
    if summary and not ok(summary):  # a summary may generalise, but it may not bring new numbers or names
        summary = ""
    points: list[str] = []
    for k in _as_list(raw.get("key_points")):
        k = _tidy(_item_text(k))
        if k and ok(k) and src.support(k) >= 0.3 and not is_empty_remark(k) and not any(_near_duplicate(k, x) for x in points):
            points.append(k)
    return {"title": title, "summary": summary, "key_points": points[:5]}


def structure_groq(
    text: str, model: str, language: str = "auto", on_progress: Callable[..., None] | None = None
) -> tuple[dict, int]:
    """Structure with a Groq chat model. Returns (notes, number of pieces done by the fallback).

    Every piece is written, checked against the transcript and then (unless GROQ_REVIEW is 0) reviewed in a
    second call that sees the transcript and the draft. The review is only used when it passes the same checks
    and is not clearly worse; a review that fails keeps the draft. A note with several pieces gets its title,
    summary and key points from one more call that sees the part summaries, the headings and the candidates.
    """
    system = groq_system_prompt(language)
    lang = languages.lenient(language)
    label = "Detect the language of the recording." if lang == "auto" else f"Language: {languages.LANGUAGES[lang]}."

    def write(piece: str) -> dict:
        user = f"{label}\n\nTranscript:\n{piece}"
        raw = _groq_chat(model, system, user)
        if _language_drift(raw, lang, piece):  # one more try, with a firm reminder
            name = languages.LANGUAGES[lang]
            raw = _groq_chat(model, system, user + f"\n\nReminder: write every word of the notes in {name}, not in English.")
            if _language_drift(raw, lang, piece):
                raise StructureError(f"the notes were not written in {name}")
        return raw

    def refine(notes: dict, piece: str) -> dict:
        return refine_model_notes(notes, piece, relaxed=True, language=lang)

    review_system = groq_review_prompt(language)

    def review(draft: dict, piece: str) -> dict:
        shown = {"title": draft["title"], "summary": draft["summary"], "key_points": draft.get("key_points", []),
                 "sections": draft["sections"], "action_items": draft.get("actions", [])}
        user = (f"{label}\n\nTranscript:\n{piece}\n\nDraft notes (JSON):\n"
                f"{json.dumps(shown, ensure_ascii=False)}")
        raw = normalize_notes(_groq_chat(model, review_system, user, optional=True))
        kept = _bullet_count(raw)
        reviewed = refine(raw, piece)
        if not _review_is_better(draft, reviewed, kept, lang, piece):
            return draft
        if not reviewed.get("key_points"):
            reviewed["key_points"] = draft.get("key_points", [])
        return reviewed

    def final(digest: str, headings: list[str], candidates: list[str]) -> dict:
        parts = [digest]
        if headings:
            parts.append("Section headings:\n" + "\n".join(f"- {h}" for h in headings[:40]))
        if candidates:
            parts.append("Candidate key points:\n" + "\n".join(f"- {c}" for c in candidates[:20]))
        raw = _groq_chat(model, groq_head_prompt(language), "\n\n".join(parts))
        return _validated_head(raw, text, lang)

    polish_system = groq_polish_prompt(language)

    def polish(notes: dict) -> dict:
        shown = {"title": notes["title"], "summary": notes["summary"], "key_points": notes.get("key_points", []),
                 "sections": notes["sections"], "action_items": notes.get("actions", [])}
        parts = [label]
        if len(text) <= POLISH_TRANSCRIPT_CHARS:
            parts.append(f"Transcript:\n{text}")
        else:
            parts.append("The transcript is too long to show. Only change wording and order; add no fact.")
        parts.append(f"Notes (JSON):\n{json.dumps(shown, ensure_ascii=False)}")
        raw = normalize_notes(_groq_chat(model, polish_system, "\n\n".join(parts), optional=True))
        kept = _bullet_count(raw)
        edited = refine(raw, text)
        if not _review_is_better(notes, edited, kept, lang, text):
            return notes
        if not edited.get("key_points"):
            edited["key_points"] = notes.get("key_points", [])
        return edited

    return _structure_pieces(
        text,
        GROQ_PIECE_CHARS,
        write,
        lambda digest: _groq_chat(model, groq_head_prompt(language), digest),
        refine,
        on_progress,
        review=review if settings.groq_review else None,
        final=final,
        polish=polish if settings.groq_polish else None,
    )


# ------------------------------------------------------------------ entry

def structure_transcript(
    text: str,
    on_progress: Callable[[int, int], None] | None = None,
    language: str = "auto",
    report: dict | None = None,
) -> tuple[dict, str]:
    """Return (notes, engine). The engine is 'groq:<model>', 'ollama:<model>' or 'rule-based'.

    Groq is tried first, then the local Ollama model, then the rules. When some pieces of a long note fell
    back to the rules, the engine reads '<engine> (partly rule-based)'. If `report` is given, the reason why a
    better engine was not used is stored in report["fallback_reason"].
    """
    text = (text or "").strip()
    if not text:
        raise StructureError("The recording contains no recognisable speech.")
    language = languages.lenient(language)
    report = report if report is not None else {}
    reasons: list[str] = []

    if groq_enabled():
        try:
            model = resolve_groq_model()
            notes, partial = structure_groq(text, model, language, on_progress)
            engine = f"groq:{model}" + (" (partly rule-based)" if partial else "")
            log.info("structuring engine used: %s", engine)
            return notes, engine
        except StructureError as exc:
            msg = str(exc) or "Groq could not be used."
            reasons.append(msg if msg.startswith("Groq") else f"The Groq answer could not be used ({msg}).")
            log.warning("groq structuring failed (%s), falling back", exc)

    plan = _ollama_plan()
    if plan["engine"].startswith("ollama:"):
        try:
            notes, partial = structure_ollama(text, on_progress, language)
            engine = f"ollama:{settings.ollama_model}" + (" (partly rule-based)" if partial else "")
            if reasons:
                report["fallback_reason"] = reasons[0] + " The local model was used instead."
            log.info("structuring engine used: %s", engine)
            return _with_extras(notes, text), engine
        except StructureError as exc:
            reasons.append("The local model did not give usable notes.")
            log.warning("ollama structuring failed (%s), using the rules", exc)
    else:
        reasons.append(plan["reason"])
    if reasons:
        report["fallback_reason"] = " ".join(r for r in reasons if r) + " The built-in rules were used."
    log.info("structuring engine used: rule-based")
    return structure_rules(text), "rule-based"


def unspoken_notes(notes: dict) -> dict:
    """The same notes without spoken framing ("Explique moi ...", "Listen, ...", "Voila.").

    Notes written before the scrubber existed are cleaned when they are shown or exported, so nothing has to be
    rebuilt. A text that is only framing is dropped; a section left empty goes with it. Notes that would be left
    without any section are returned as they are.
    """
    if not notes or not notes.get("sections"):
        return notes

    def one(text) -> str:
        return speech.scrub(str(text or "").strip())

    sections = []
    for sec in notes["sections"]:
        bullets = [b for b in (one(b) for b in sec.get("bullets") or []) if b]
        if bullets:
            sections.append({**sec, "bullets": bullets})
    if not sections:
        return notes
    out = {**notes, "sections": sections}
    out["title"] = _clean_title(notes.get("title")) or notes.get("title") or ""
    if "summary" in notes:
        out["summary"] = one(notes.get("summary"))
    for key in ("key_points", "actions"):
        if key in notes:
            out[key] = [t for t in (one(t) for t in notes.get(key) or []) if t]
    return out


def _with_extras(notes: dict, text: str) -> dict:
    """A small model often leaves out the tasks and key points; the rules find them in the transcript."""
    sentences = _prepare_sentences(text)
    if not notes.get("actions"):
        notes["actions"] = extract_actions(sentences)
    if not notes.get("key_points"):
        notes["key_points"] = select_key_points(sentences, exclude=[notes.get("summary") or ""] + notes["actions"])
    return notes
