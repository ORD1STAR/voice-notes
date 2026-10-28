"""Structuring through the Groq chat API. The network is never used: httpx is replaced by a fake."""
import json
import logging

import httpx
import pytest

from app import structure
from conftest import SAMPLE_TEXT

KEY = "test-key-not-real"
CHAT_URL = "http://groq.test/openai/v1/chat/completions"
MODELS_URL = "http://groq.test/openai/v1/models"

FR_TRANSCRIPT = (
    "Alors, euh, explique moi comment ça marche donc l'ancienne procédure de remboursement. Donc en fait, quand un "
    "client envoie sa demande, euh, il faut d'abord vérifier le dossier, et ensuite on l'envoie à Karim pour "
    "validation. Karim a deux jours pour répondre. Après ça, la comptabilité fait le virement, ça prend environ "
    "cinq jours. Le problème, c'est que, c'est que souvent les dossiers sont incomplets, du coup on perd du temps. "
    "Je pense qu'on devrait ajouter un formulaire en ligne. Il faut que Sophie prépare une maquette avant vendredi."
)
FR_NOTES = {
    "title": "Procédure de remboursement",
    "summary": "L'ancienne procédure de remboursement passe par la vérification du dossier, la validation par Karim "
               "puis le virement par la comptabilité. Les dossiers incomplets font perdre du temps, c'est pourquoi un "
               "formulaire en ligne est proposé.",
    "key_points": ["Karim dispose de deux jours pour valider une demande."],
    "sections": [
        {"heading": "Étapes du remboursement", "bullets": [
            "Le dossier du client est d'abord vérifié.",
            "Karim valide ensuite la demande sous deux jours.",
            "La comptabilité effectue le virement en environ cinq jours."]},
        {"heading": "Problème et solution", "bullets": [
            "Les dossiers sont souvent incomplets, ce qui fait perdre du temps.",
            "Un formulaire en ligne est proposé pour éviter cela."]},
    ],
    "action_items": ["Sophie : préparer une maquette du formulaire avant vendredi."],
}


class FakeNet:
    """Stands in for httpx.get and httpx.post: Groq models and chat, and optionally Ollama."""

    def __init__(self, monkeypatch, models=("llama-3.3-70b-versatile", "whisper-large-v3-turbo", "llama-3.1-8b-instant"),
                 chat=None, ollama=None, review=False):
        self.models = list(models) if models is not None else None
        self.chat = chat if chat is not None else FR_NOTES
        self.ollama = ollama
        self.chat_calls: list[dict] = []
        self.model_calls = 0
        monkeypatch.setenv("USE_GROQ_CHAT", "1")
        # The review pass doubles the calls. It is off here so that the older tests count one call per part;
        # tests/test_review_pass.py switches it on.
        monkeypatch.setenv("GROQ_REVIEW", "1" if review else "0")
        monkeypatch.setenv("GROQ_CHAT_URL", CHAT_URL)
        monkeypatch.setenv("GROQ_MODELS_URL", MODELS_URL)
        if ollama is not None:
            monkeypatch.setenv("USE_OLLAMA", "1")
            monkeypatch.setenv("OLLAMA_MODEL", "tiny:1b")
        monkeypatch.setattr(structure.httpx, "get", self.get)
        monkeypatch.setattr(structure.httpx, "post", self.post)
        monkeypatch.setattr(structure.time, "sleep", lambda s: None)
        structure.reset_groq_cache()

    @staticmethod
    def _resp(url, status=200, body=None):
        return httpx.Response(status, json=body if body is not None else {}, request=httpx.Request("GET", url))

    def get(self, url, headers=None, timeout=None, **kw):
        if url == MODELS_URL:
            self.model_calls += 1
            assert headers and headers["Authorization"] == f"Bearer {KEY}"
            if self.models is None:
                raise httpx.ConnectError("models are down")
            return self._resp(url, body={"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]})
        if self.ollama is not None and url.endswith("/api/tags"):
            return self._resp(url, body={"models": [{"name": "tiny:1b"}]})
        raise httpx.ConnectError("no such service in the test")

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        if url == CHAT_URL:
            assert headers["Authorization"] == f"Bearer {KEY}"
            self.chat_calls.append(json)
            answer = self.chat(json) if callable(self.chat) else self.chat
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, httpx.Response):
                return answer
            content = answer if isinstance(answer, str) else __import__("json").dumps(answer, ensure_ascii=False)
            return self._resp(url, body={"choices": [{"message": {"role": "assistant", "content": content}}]})
        if self.ollama is not None and url.endswith("/api/chat"):
            return self._resp(url, body={"message": {"content": __import__("json").dumps(self.ollama)}})
        raise httpx.ConnectError("no such service in the test")


def status(code):
    return httpx.Response(code, json={"error": {"message": "nope"}}, request=httpx.Request("POST", CHAT_URL))


# ------------------------------------------------------------ models and choice

def test_default_model_is_a_70b_model_and_comes_from_the_models_list(monkeypatch):
    net = FakeNet(monkeypatch)
    assert structure.settings.groq_chat_model == "llama-3.3-70b-versatile"
    assert structure.resolve_groq_model() == "llama-3.3-70b-versatile"
    assert net.model_calls == 1
    structure.resolve_groq_model()
    assert net.model_calls == 1  # the list is cached
    assert "whisper-large-v3-turbo" not in structure.groq_models()


def test_model_name_is_configurable(monkeypatch):
    net = FakeNet(monkeypatch, models=("llama-3.3-70b-versatile", "custom-chat-model"))
    monkeypatch.setenv("GROQ_CHAT_MODEL", "custom-chat-model")
    notes, engine = structure.structure_transcript(FR_TRANSCRIPT)
    assert engine == "groq:custom-chat-model"
    assert net.chat_calls[0]["model"] == "custom-chat-model"


def test_unlisted_model_is_replaced_by_the_best_listed_one(monkeypatch):
    FakeNet(monkeypatch, models=("llama-3.1-8b-instant", "acme/chat-70b-instruct", "whisper-large-v3"))
    monkeypatch.setenv("GROQ_CHAT_MODEL", "retired-model")
    assert structure.resolve_groq_model() == "acme/chat-70b-instruct"
    FakeNet(monkeypatch, models=("llama-3.1-8b-instant", "acme/chat-70b-instruct", "llama3-70b-8192"))
    assert structure.resolve_groq_model() == "llama3-70b-8192"  # a known good name wins over the guess


def test_no_suitable_model_means_fallback(monkeypatch):
    FakeNet(monkeypatch, models=("llama-3.1-8b-instant", "whisper-large-v3"))
    monkeypatch.setenv("GROQ_CHAT_MODEL", "retired-model")
    report = {}
    notes, engine = structure.structure_transcript(SAMPLE_TEXT, report=report)
    assert engine == "rule-based" and "no suitable chat model" in report["fallback_reason"]


def test_configured_model_is_tried_when_the_list_cannot_be_read(monkeypatch):
    net = FakeNet(monkeypatch, models=None)
    _, engine = structure.structure_transcript(FR_TRANSCRIPT)
    assert engine == "groq:llama-3.3-70b-versatile" and len(net.chat_calls) == 1


# ----------------------------------------------------------------- the request

def test_request_asks_for_json_and_carries_the_instructions(monkeypatch):
    net = FakeNet(monkeypatch)
    notes, engine = structure.structure_transcript(FR_TRANSCRIPT)
    assert engine == "groq:llama-3.3-70b-versatile"
    call = net.chat_calls[0]
    assert call["response_format"] == {"type": "json_object"}
    system, user = call["messages"][0]["content"], call["messages"][1]["content"]
    for phrase in ("team members or classmates", "Rewrite the speech into proper written sentences",
                   "fillers, stutters, repetitions and false starts", "Never invent facts, names, numbers or dates",
                   "Do not translate", "Keep it concise", "language of the recording"):
        assert phrase in system
    for key in ("title", "summary", "key_points", "sections", "heading", "bullets", "action_items"):
        assert f'"{key}"' in system
    assert "2 to 4 sentences" in system
    assert FR_TRANSCRIPT in user and "Detect the language" in user
    assert KEY not in json.dumps(call)  # the key travels in the header only


def test_chosen_language_goes_into_the_prompt(monkeypatch):
    net = FakeNet(monkeypatch)
    structure.structure_transcript(FR_TRANSCRIPT, language="fr")
    system, user = (m["content"] for m in net.chat_calls[0]["messages"])
    assert "The recording is in French. Write all the notes in French. Do not translate." in system
    assert "Language: French." in user
    assert "language of the recording" not in system


# --------------------------------------------------------------- the result

def test_groq_notes_are_used_and_keep_their_own_wording(monkeypatch):
    FakeNet(monkeypatch)
    notes, engine = structure.structure_transcript(FR_TRANSCRIPT, language="fr")
    assert engine.startswith("groq:")
    assert notes["title"] == "Procédure de remboursement"
    assert notes["sections"][0]["bullets"][0] == "Le dossier du client est d'abord vérifié."
    assert [s["heading"] for s in notes["sections"]] == ["Étapes du remboursement", "Problème et solution"]
    assert notes["summary"].startswith("L'ancienne procédure")
    assert notes["actions"] == ["Sophie : préparer une maquette du formulaire avant vendredi."]  # action_items become actions
    assert notes["key_points"] == ["Karim dispose de deux jours pour valider une demande."]
    assert not any("euh" in b for s in notes["sections"] for b in s["bullets"])


def test_no_tasks_are_added_from_the_rules_to_groq_notes(monkeypatch):
    payload = dict(FR_NOTES, action_items=[])
    FakeNet(monkeypatch, chat=payload)
    notes, _ = structure.structure_transcript(FR_TRANSCRIPT)
    assert notes["actions"] == []


def test_action_items_may_be_objects(monkeypatch):
    payload = dict(FR_NOTES, action_items=[{"owner": "Sophie", "task": "Préparer une maquette avant vendredi"}])
    FakeNet(monkeypatch, chat=payload)
    notes, _ = structure.structure_transcript(FR_TRANSCRIPT)
    assert notes["actions"] == ["Sophie: Préparer une maquette avant vendredi"]


def test_long_transcript_is_sent_in_pieces_and_gets_one_title_and_summary(monkeypatch):
    def chat(req):
        user = req["messages"][1]["content"]
        if user.startswith("Part 1"):
            return {"title": "Whole note", "summary": "A summary of everything.", "key_points": [], "sections": [],
                    "action_items": []}
        return FR_NOTES

    net = FakeNet(monkeypatch, chat=chat)
    long_text = (FR_TRANSCRIPT + " ") * 40
    assert len(long_text) > structure.GROQ_PIECE_CHARS
    notes, engine = structure.structure_transcript(long_text)
    assert engine == "groq:llama-3.3-70b-versatile"
    assert len(net.chat_calls) >= 3  # at least two pieces and the summary of the whole
    assert notes["title"] == "Whole note" and notes["summary"] == "A summary of everything."


# ------------------------------------------------------------------ fallback

@pytest.mark.parametrize("answer", [
    httpx.ReadTimeout("slow"), httpx.ConnectError("down"), status(500), status(401), status(400),
    "this is not json", "[1, 2]", {"title": "x", "summary": "y", "sections": []},
])
def test_groq_failure_falls_back_to_the_rules(monkeypatch, answer):
    FakeNet(monkeypatch, chat=answer)
    report = {}
    notes, engine = structure.structure_transcript(SAMPLE_TEXT, report=report)
    assert engine == "rule-based"
    assert notes["sections"] and notes["title"]
    assert "Groq" in report["fallback_reason"] and "built-in rules were used" in report["fallback_reason"]


def test_groq_failure_uses_the_local_model_when_it_is_there(monkeypatch):
    local = {"title": "Launch plan", "summary": "The landing page is due on Friday.",
             "sections": [{"heading": "Website", "bullets": ["Finish the landing page by Friday."]}]}
    FakeNet(monkeypatch, chat=httpx.ConnectError("down"), ollama=local)
    report = {}
    notes, engine = structure.structure_transcript(SAMPLE_TEXT, report=report)
    assert engine == "ollama:tiny:1b" and notes["title"] == "Launch plan"
    assert "Groq could not be reached" in report["fallback_reason"] and "local model was used" in report["fallback_reason"]


def test_a_rate_limit_is_retried_before_giving_up(monkeypatch):
    answers = iter([status(429), FR_NOTES])
    net = FakeNet(monkeypatch, chat=lambda req: next(answers))
    _, engine = structure.structure_transcript(FR_TRANSCRIPT)
    assert engine.startswith("groq:") and len(net.chat_calls) == 2


def test_a_rate_limit_that_does_not_end_falls_back(monkeypatch):
    net = FakeNet(monkeypatch, chat=status(429))
    _, engine = structure.structure_transcript(SAMPLE_TEXT)
    assert engine == "rule-based" and len(net.chat_calls) == 3


def test_a_note_made_only_of_invented_content_falls_back(monkeypatch):
    invented = {"title": "Quarterly sales", "summary": "Sales grew by 45 percent in Berlin.", "key_points": [],
                "sections": [{"heading": "Sales", "bullets": ["Revenue reached 9000 euros in Berlin.",
                                                              "Marco leads the Munich office."]}],
                "action_items": []}
    FakeNet(monkeypatch, chat=invented)
    _, engine = structure.structure_transcript(SAMPLE_TEXT)
    assert engine == "rule-based"


def test_groq_is_not_used_when_switched_off_or_without_a_key(monkeypatch):
    net = FakeNet(monkeypatch)
    monkeypatch.setenv("USE_GROQ_CHAT", "0")
    _, engine = structure.structure_transcript(SAMPLE_TEXT)
    assert engine == "rule-based" and net.chat_calls == [] and net.model_calls == 0
    monkeypatch.setenv("USE_GROQ_CHAT", "1")
    monkeypatch.delenv("GROQ_API_KEY")
    _, engine = structure.structure_transcript(SAMPLE_TEXT)
    assert engine == "rule-based" and net.chat_calls == []


def test_plan_and_status_name_the_engine_without_calling_the_network(monkeypatch):
    net = FakeNet(monkeypatch)
    plan = structure.engine_plan()
    assert plan["engine"] == "groq:llama-3.3-70b-versatile" and plan["backup"]["engine"] == "rule-based"
    assert structure.groq_status() == {"enabled": True, "model": "llama-3.3-70b-versatile", "reason": "", "review": False,
                                      "polish": False}
    assert net.model_calls == 0 and net.chat_calls == []
    monkeypatch.setenv("USE_GROQ_CHAT", "0")
    assert structure.engine_plan()["engine"] == "rule-based"
    assert structure.groq_status()["reason"] == "disabled"


# ------------------------------------------------------------- output checks

SRC = ("We met on Monday with Tom. The budget is 5000 euros and the launch is in twenty days. "
       "Anna will send the report.")


def model_notes(bullets, **extra):
    base = {"title": "Launch budget meeting", "summary": "The team met with Tom to discuss the budget of 5000 euros.",
            "key_points": [], "sections": [{"heading": "Budget", "bullets": bullets}], "actions": []}
    base.update(extra)
    return base


def bullets_of(notes):
    return [b for s in notes["sections"] for b in s["bullets"]]


def test_invented_numbers_are_dropped():
    out = structure.refine_model_notes(model_notes(["The budget is 5000 euros.", "The launch will cost 9000 euros."]),
                                       SRC, relaxed=True)
    assert bullets_of(out) == ["The budget is 5000 euros."]


def test_invented_names_are_dropped():
    out = structure.refine_model_notes(model_notes(["The budget is 5000 euros.", "Then Marco will lead the launch."]),
                                       SRC, relaxed=True)
    assert bullets_of(out) == ["The budget is 5000 euros."]


def test_names_from_the_transcript_are_kept():
    out = structure.refine_model_notes(model_notes(["The team met on Monday with Tom."]), SRC, relaxed=True)
    assert bullets_of(out) == ["The team met on Monday with Tom."]


def test_a_number_spelled_out_in_the_transcript_may_be_written_in_digits():
    out = structure.refine_model_notes(model_notes(["The launch happens in 20 days."]), SRC, relaxed=True)
    assert bullets_of(out) == ["The launch happens in 20 days."]
    # but not for a model that is checked strictly, and not for a number that nobody said
    with pytest.raises(structure.StructureError):
        structure.refine_model_notes(model_notes(["The launch happens in 20 days."]), SRC)
    with pytest.raises(structure.StructureError):
        structure.refine_model_notes(model_notes(["The launch happens in 30 days."]), SRC, relaxed=True)


def test_a_title_or_heading_with_an_invented_name_is_replaced():
    notes = model_notes(["The budget is 5000 euros."], title="Budget meeting with Marco")
    notes["sections"][0]["heading"] = "Budget for Berlin"
    out = structure.refine_model_notes(notes, SRC, relaxed=True)
    assert "Marco" not in out["title"] and "Berlin" not in out["sections"][0]["heading"]


def test_good_rewriting_is_kept_where_the_strict_check_would_block_it():
    bullet = "The budget covers launch preparations and training."
    notes = model_notes([bullet, "The budget is 5000 euros."])
    assert bullet in bullets_of(structure.refine_model_notes(notes, SRC, relaxed=True))
    assert bullet not in bullets_of(structure.refine_model_notes(notes, SRC))


def test_relaxed_notes_are_not_run_through_the_filler_rules():
    notes = model_notes(["So the budget is 5000 euros."])
    assert bullets_of(structure.refine_model_notes(notes, SRC, relaxed=True)) == ["So the budget is 5000 euros."]
    assert bullets_of(structure.refine_model_notes(notes, SRC)) == ["The budget is 5000 euros."]


def test_german_capitals_do_not_count_as_names():
    src = "Wir haben über das Budget gesprochen. Die Planung für den Start ist fertig und der Termin steht."
    notes = {"title": "Budget und Start", "summary": "Das Budget und die Planung wurden besprochen.", "key_points": [],
             "sections": [{"heading": "Planung", "bullets": ["Die Planung für den Start ist fertig."]}], "actions": []}
    out = structure.refine_model_notes(notes, src, relaxed=True, language="de")
    assert bullets_of(out) == ["Die Planung für den Start ist fertig."]


def test_chinese_and_arabic_notes_are_checked_by_their_own_characters():
    zh_src = "今天我们讨论了新网站的发布计划。设计团队会在星期五之前完成首页。预算保持不变。"
    zh = {"title": "网站发布计划", "summary": "团队讨论了新网站的发布计划。", "key_points": [],
          "sections": [{"heading": "发布任务", "bullets": ["设计团队将在星期五之前完成首页。", "预算保持不变。",
                                                         "广告预算增加到 9000 元。"]}], "actions": []}
    out = structure.refine_model_notes(zh, zh_src, relaxed=True, language="zh")
    assert bullets_of(out) == ["设计团队将在星期五之前完成首页。", "预算保持不变。"]

    ar_src = "ناقشنا اليوم ميزانية المشروع. سيرسل أحمد التقرير يوم الخميس."
    ar = {"title": "ميزانية المشروع", "summary": "ناقش الفريق ميزانية المشروع.", "key_points": [],
          "sections": [{"heading": "الميزانية", "bullets": ["ناقش الفريق الميزانية الخاصة بالمشروع.",
                                                            "سيرسل أحمد التقرير يوم الخميس."]}], "actions": []}
    out = structure.refine_model_notes(ar, ar_src, relaxed=True, language="ar")
    assert len(bullets_of(out)) == 2


def test_empty_remarks_and_repeats_are_removed_from_groq_notes():
    out = structure.refine_model_notes(
        model_notes(["The budget is 5000 euros.", "The budget is 5000 euros.", "That is a good call."]), SRC, relaxed=True)
    assert bullets_of(out) == ["The budget is 5000 euros."]


# ------------------------------------------------------------ the local model

def test_local_model_gets_an_example_in_the_language_of_the_recording():
    assert structure.ollama_example(FR_TRANSCRIPT)[0] == structure.EXAMPLE_TRANSCRIPT_FR
    assert structure.ollama_example(SAMPLE_TEXT)[0] == structure.EXAMPLE_TRANSCRIPT
    assert structure.ollama_example("Das ist ein Test über das Budget.", "de") is None
    assert structure.ollama_example(SAMPLE_TEXT, "fr")[0] == structure.EXAMPLE_TRANSCRIPT_FR
    assert "Write the notes in German" in structure.ollama_system_prompt("de")
    assert structure.ollama_system_prompt("auto") == structure.SYSTEM_PROMPT


def test_local_model_request_names_the_language(monkeypatch):
    seen = {}

    def post(url, json=None, timeout=None):
        seen["messages"] = json["messages"]
        answer = {"title": "Remboursement", "summary": "La procédure de remboursement passe par Karim.",
                  "sections": [{"heading": "Étapes", "bullets": ["Karim valide la demande sous deux jours."]}]}
        return httpx.Response(200, json={"message": {"content": __import__("json").dumps(answer)}},
                              request=httpx.Request("POST", url))

    monkeypatch.setenv("USE_OLLAMA", "1")
    monkeypatch.setenv("OLLAMA_MODEL", "tiny:1b")
    monkeypatch.setattr(structure.httpx, "get", lambda url, timeout=None: httpx.Response(
        200, json={"models": [{"name": "tiny:1b"}]}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(structure.httpx, "post", post)
    _, engine = structure.structure_transcript(FR_TRANSCRIPT, language="fr")
    assert engine == "ollama:tiny:1b"
    assert "Write the notes in French" in seen["messages"][0]["content"]
    assert seen["messages"][1]["content"].endswith(structure.EXAMPLE_TRANSCRIPT_FR)


# ------------------------------------------------------------------- logging

def test_the_engine_that_was_used_is_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="voice_notes")
    net = FakeNet(monkeypatch)
    structure.structure_transcript(FR_TRANSCRIPT)
    assert any("structuring engine used: groq:llama-3.3-70b-versatile" in r.getMessage() for r in caplog.records)
    caplog.clear()
    net.chat = httpx.ConnectError("down")
    structure.structure_transcript(SAMPLE_TEXT)
    messages = [r.getMessage() for r in caplog.records]
    assert any("groq structuring failed" in m for m in messages)
    assert any("structuring engine used: rule-based" in m for m in messages)
    assert KEY not in " ".join(messages)
