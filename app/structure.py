"""Turn a raw transcript into structured notes.

The preferred engine is a small local model served by Ollama. When Ollama is
not running, or the model is missing, or a call fails, a rule-based fallback
is used so a transcript always ends up as readable notes.

Notes have this shape:
    {"title": str, "summary": str, "key_points": [str],
     "sections": [{"heading": str, "bullets": [str]}], "actions": [str]}
"key_points" and "actions" are optional.
"""
import json
import math
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
    s = _INLINE_FILLER.sub(lambda m: "," if m.group(0).lstrip().startswith(", like") else " ", s)
    s = _TAG.sub("", s)
    s = _STUTTER.sub(r"\1", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;")
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
    actions = _clean_list(raw.get("actions"), 160, 10, bullet_marks=True)
    key_points = _clean_list(raw.get("key_points"), 220, 6)
    return {"title": _clip(title, 100) or "Untitled note", "summary": summary, "key_points": key_points,
            "sections": sections[:30], "actions": actions}


def _clean_list(items, clip: int, cap: int, bullet_marks: bool = False) -> list[str]:
    out: list[str] = []
    for a in items or []:
        a = str(a).strip()
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

def _stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


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

SUMMARY_PROMPT = (
    "You receive the summaries of consecutive parts of one long voice note. "
    "Write in the same language. Return JSON with a short title of 2 to 6 words (not a sentence), "
    "a summary of three or four sentences covering the whole note, an empty key_points list, "
    "an empty sections list and an empty actions list."
)

GENERIC_TITLES = {"notes", "note", "voice note", "summary", "untitled", "untitled note", "meeting notes", "transcript",
                  "title", "recording", "voice recording", "notes from the recording"}
GENERIC_HEADINGS = {"notes", "note", "details", "introduction", "summary", "overview", "main points", "other",
                    "miscellaneous", "general", "main topic", "key points", "content", "topic", "conclusion"}


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
    """Which engine will structure the next note, and when it is the fallback, why."""
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


def _ollama_chat(system: str, user: str, example: bool = False) -> dict:
    messages = [{"role": "system", "content": system}]
    if example:  # one worked example helps small models keep to the format and the style
        messages += [{"role": "user", "content": f"Transcript:\n{EXAMPLE_TRANSCRIPT}"},
                     {"role": "assistant", "content": json.dumps(EXAMPLE_NOTES)}]
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
    t = t.strip().strip("\"'").rstrip(".:;").strip()
    if len(t.split()) > 10:
        t = short_phrase(t, 8)
    return t


def refine_model_notes(notes: dict, source: str) -> dict:
    """Check what a small model returned against the transcript and repair it with the rules.

    Bullets, actions and key points that say things the speaker did not say (words or numbers that are not in
    the source) are dropped; filler and empty remarks are removed; a generic title or heading is replaced by one
    the rules find; tasks the model missed are added from the rules. Raises StructureError when nothing is left.
    """
    prefixes = {w[:5] for w in _kw(source)}
    digits = set(re.findall(r"\d+", source))

    def supported(text: str, threshold: float = 0.5) -> bool:
        ws = _kw(text)
        if ws and sum(1 for w in ws if w[:5] in prefixes) / len(ws) < threshold:
            return False
        return all(d in digits or int(d) <= 12 for d in re.findall(r"\d+", text))

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
            c = clean_sentence(b)
            if not c or is_empty_remark(c) or not supported(c) or any(_near_duplicate(c, x) for x in seen):
                continue
            seen.append(c)
            bullets.append(c)
        if not bullets:
            continue
        heading = sec["heading"].strip().rstrip(".:;").strip()
        if not heading or heading.lower() in GENERIC_HEADINGS or len(heading.split()) > 7:
            heading = best_phrase(bullets) or short_phrase(heading, 5) or "Notes"
        sections.append({"heading": heading, "bullets": bullets})
    if not sections:
        raise StructureError("the model produced no usable bullets")

    title = _clean_title(notes.get("title"))
    if not title or title.lower() in GENERIC_TITLES:
        title = fallback()["title"]
    summary = (notes.get("summary") or "").strip()
    if not summary or not supported(summary, 0.4):
        summary = fallback()["summary"]
    actions = [a for a in notes.get("actions") or [] if supported(a)]
    for a in extract_actions(_prepare_sentences(source)):  # a small model often leaves tasks out
        if not any(_near_duplicate(a, x) for x in actions):
            actions.append(a)
    key_points = [k for k in notes.get("key_points") or [] if supported(k) and not is_empty_remark(k)]
    return normalize_notes({"title": title, "summary": summary, "key_points": key_points, "sections": sections,
                            "actions": actions})


def structure_ollama(
    text: str, on_progress: Callable[[int, int], None] | None = None
) -> tuple[dict, int]:
    """Structure with the local model. Returns (notes, number of pieces done by the fallback).

    A piece the model cannot handle is structured by the rules instead of
    throwing away the pieces that worked. After two failures in a row the model
    is assumed to be overloaded and the remaining pieces use the rules.
    """
    pieces = split_pieces(text)
    if not pieces:
        raise StructureError("The transcript is empty.")
    results: list[dict] = []
    fallback_pieces = 0
    streak = 0
    for i, piece in enumerate(pieces):
        if on_progress:
            on_progress(i, len(pieces))
        notes = None
        if streak < 2:
            try:
                notes = refine_model_notes(
                    normalize_notes(_ollama_chat(SYSTEM_PROMPT, f"Transcript:\n{piece}", example=True)), piece)
                streak = 0
            except StructureError:
                streak += 1
        if notes is None:
            try:
                notes = structure_rules(piece)
            except StructureError:
                continue
            fallback_pieces += 1
        results.append(notes)
    if on_progress:
        on_progress(len(pieces), len(pieces))
    if not results or fallback_pieces == len(pieces):
        raise StructureError("the model produced no usable notes")
    if len(results) == 1:
        return results[0], fallback_pieces
    sections: list[dict] = []
    for r in results:
        for sec in r["sections"]:
            if sections and sections[-1]["heading"].lower() == sec["heading"].lower():
                sections[-1] = {"heading": sections[-1]["heading"],
                                "bullets": (sections[-1]["bullets"] + sec["bullets"])[:12]}
            else:
                sections.append(sec)
    actions = _clean_list([a for r in results for a in r.get("actions", [])], 160, 10, bullet_marks=True)
    key_points = [k for r in results for k in r.get("key_points", [])]
    if len(key_points) > 5:
        imp = _importance(key_points)
        key_points = [key_points[i] for i in sorted(sorted(range(len(key_points)), key=lambda k: -imp[k])[:5])]
    digest = "\n".join(f"Part {i + 1}: {r['summary']}" for i, r in enumerate(results))
    try:
        head = _ollama_chat(SUMMARY_PROMPT, digest)
        title = _clean_title(head.get("title"))
        summary = str(head.get("summary") or "").strip()
    except StructureError:
        title, summary = "", ""
    if not title or title.lower() in GENERIC_TITLES or not summary:
        fallback = structure_rules(text)
        title = title if title and title.lower() not in GENERIC_TITLES else fallback["title"]
        summary = summary or fallback["summary"]
    return {"title": _clip(title, 100), "summary": summary, "key_points": key_points,
            "sections": sections[:30], "actions": actions}, fallback_pieces


# ------------------------------------------------------------------ entry

def structure_transcript(
    text: str, on_progress: Callable[[int, int], None] | None = None
) -> tuple[dict, str]:
    """Return (notes, engine). The engine is 'ollama:<model>' or 'rule-based'.

    When some pieces of a long note fell back to the rules, the engine reads
    'ollama:<model> (partly rule-based)'.
    """
    text = (text or "").strip()
    if not text:
        raise StructureError("The recording contains no recognisable speech.")
    status = ollama_status()
    if status["available"]:
        try:
            notes, partial = structure_ollama(text, on_progress)
            engine = f"ollama:{settings.ollama_model}" + (" (partly rule-based)" if partial else "")
            return _with_extras(notes, text), engine
        except StructureError:
            pass
    return structure_rules(text), "rule-based"


def _with_extras(notes: dict, text: str) -> dict:
    """A small model often leaves out the tasks and key points; the rules find them in the transcript."""
    sentences = _prepare_sentences(text)
    if not notes.get("actions"):
        notes["actions"] = extract_actions(sentences)
    if not notes.get("key_points"):
        notes["key_points"] = select_key_points(sentences, exclude=[notes.get("summary") or ""] + notes["actions"])
    return notes
