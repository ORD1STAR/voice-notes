import json
from collections import Counter

import httpx
import pytest

from app import structure

from conftest import SAMPLE_TEXT


def assert_valid(notes):
    assert notes["title"]
    assert isinstance(notes["summary"], str)
    assert notes["sections"]
    for sec in notes["sections"]:
        assert sec["heading"]
        assert sec["bullets"] and all(isinstance(b, str) and b for b in sec["bullets"])


def test_rule_based_structure_is_valid_and_keeps_content():
    notes = structure.structure_rules(SAMPLE_TEXT)
    assert_valid(notes)
    assert len(notes["sections"]) >= 2
    bullets = " ".join(b for s in notes["sections"] for b in s["bullets"])
    assert "landing page" in bullets
    assert notes["summary"]


def test_rule_based_handles_one_sentence_and_no_punctuation():
    assert_valid(structure.structure_rules("Buy milk"))
    long_run = " ".join(["word%d" % i for i in range(120)])
    assert_valid(structure.structure_rules(long_run))


def test_rule_based_rejects_empty():
    with pytest.raises(structure.StructureError):
        structure.structure_rules("   ")


def test_split_pieces_respects_limit():
    pieces = structure.split_pieces(SAMPLE_TEXT, limit=200)
    assert len(pieces) > 2
    assert all(len(p) < 400 for p in pieces)
    assert " ".join(pieces).replace("  ", " ") == " ".join(structure.split_sentences(SAMPLE_TEXT))


def test_normalize_notes_cleans_and_validates():
    raw = {"title": '"A title"', "summary": " s ", "sections": [
        {"heading": "## Head", "bullets": ["- one", "  ", "* two"]},
        {"heading": "empty", "bullets": []},
    ]}
    notes = structure.normalize_notes(raw)
    assert notes["title"] == "A title"
    assert notes["sections"] == [{"heading": "Head", "bullets": ["one", "two"]}]
    with pytest.raises(structure.StructureError):
        structure.normalize_notes({"title": "x", "sections": []})
    with pytest.raises(structure.StructureError):
        structure.normalize_notes("nope")


def test_entry_point_falls_back_when_ollama_is_off():
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    assert engine == "rule-based"
    assert_valid(notes)


def test_entry_point_rejects_empty_transcript():
    with pytest.raises(structure.StructureError):
        structure.structure_transcript("  ")


def fake_ollama(monkeypatch, notes_payload, fail_chat=False):
    monkeypatch.setenv("USE_OLLAMA", "1")
    monkeypatch.setenv("OLLAMA_MODEL", "tiny:1b")

    def fake_get(url, timeout=None):
        return httpx.Response(200, json={"models": [{"name": "tiny:1b"}]}, request=httpx.Request("GET", url))

    def fake_post(url, json=None, timeout=None):
        if fail_chat:
            raise httpx.ConnectError("down")
        return httpx.Response(
            200, json={"message": {"content": __import__("json").dumps(notes_payload)}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(structure.httpx, "get", fake_get)
    monkeypatch.setattr(structure.httpx, "post", fake_post)


def test_ollama_engine_used_when_available(monkeypatch):
    payload = {"title": "Launch plan", "summary": "A plan.",
               "sections": [{"heading": "Website", "bullets": ["Finish the landing page"]}]}
    fake_ollama(monkeypatch, payload)
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    assert engine == "ollama:tiny:1b"
    assert notes["title"] == "Launch plan"


def test_ollama_long_transcript_is_processed_in_pieces(monkeypatch):
    payload = {"title": "Part", "summary": "Sum.", "sections": [{"heading": "H", "bullets": ["b"]}]}
    fake_ollama(monkeypatch, payload)
    long_text = (SAMPLE_TEXT + " ") * 8
    notes, engine = structure.structure_transcript(long_text)
    assert engine.startswith("ollama:")
    assert len(notes["sections"]) > 1


def test_ollama_failure_falls_back_to_rules(monkeypatch):
    fake_ollama(monkeypatch, {}, fail_chat=True)
    notes, engine = structure.structure_transcript(SAMPLE_TEXT)
    assert engine == "rule-based"
    assert_valid(notes)


def test_ollama_bad_json_falls_back(monkeypatch):
    fake_ollama(monkeypatch, {"title": "x", "summary": "y", "sections": []})
    _, engine = structure.structure_transcript(SAMPLE_TEXT)
    assert engine == "rule-based"


def test_ollama_status_unreachable(monkeypatch):
    monkeypatch.setenv("USE_OLLAMA", "1")
    monkeypatch.setenv("OLLAMA_HOST_URL", "http://127.0.0.1:1")
    st = structure.ollama_status()
    assert st["available"] is False


# ----------------------------------------------------- spoken text cleanup

def test_clean_sentence_removes_fillers_but_keeps_meaning():
    assert structure.clean_sentence("Okay so we need to ship on Friday.") == "We need to ship on Friday."
    assert structure.clean_sentence("Um, the budget, you know, stays the same.") == "The budget stays the same."
    assert structure.clean_sentence("So far the results look good.") == "So far the results look good."
    assert structure.clean_sentence("Well, that works.") == "That works."
    assert structure.clean_sentence("um") == ""


def test_short_phrase_never_ends_on_a_small_word():
    p = structure.short_phrase("Marketing needs the final copy before the newsletter is sent.", 7)
    assert p == "Marketing needs the final copy"
    assert structure.short_phrase("Cut churn, then grow", 8) == "Cut churn, then grow"
    assert structure.short_phrase("Rent is 1,200 euros, paid monthly", 8).startswith("Rent is 1,200 euros")


def test_rule_based_title_is_a_readable_phrase_and_fillers_are_gone():
    text = ("Okay so today we planned the launch of the new website and the newsletter for next month. "
            "Um, the design team will finish the landing page by Friday. "
            "The design team will finish the landing page by Friday. "
            "Marketing needs the final copy before the newsletter goes out.")
    notes = structure.structure_rules(text)
    assert not notes["title"].lower().startswith(("okay", "so ", "um"))
    assert len(notes["title"].split()) <= 8
    bullets = [b for s in notes["sections"] for b in s["bullets"]]
    assert not any(b.lower().startswith(("okay", "um")) for b in bullets)
    assert sum("landing page" in b for b in bullets) == 1  # the repeated sentence is dropped


def test_paragraphs_split_long_transcripts():
    text = " ".join(f"This is sentence number {i} of the transcript." for i in range(40))
    paras = structure.paragraphs(text, target=300)
    assert len(paras) > 3
    assert all(len(p) < 400 for p in paras)
    assert structure.paragraphs("") == []


def test_one_bad_piece_does_not_discard_the_others(monkeypatch):
    """A model failure on one piece is covered by the rules for that piece only."""
    monkeypatch.setenv("USE_OLLAMA", "1")
    monkeypatch.setenv("OLLAMA_MODEL", "tiny:1b")
    good = {"title": "Part", "summary": "Sum.", "sections": [{"heading": "H", "bullets": ["from the model"]}]}
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        return httpx.Response(200, json={"models": [{"name": "tiny:1b"}]}, request=httpx.Request("GET", url))

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"message": {"content": __import__("json").dumps(good)}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(structure.httpx, "get", fake_get)
    monkeypatch.setattr(structure.httpx, "post", fake_post)
    notes, engine = structure.structure_transcript((SAMPLE_TEXT + " ") * 12)
    assert engine == "ollama:tiny:1b (partly rule-based)"
    bullets = [b for s in notes["sections"] for b in s["bullets"]]
    assert "from the model" in bullets
    assert any("landing page" in b for b in bullets)


def test_model_that_keeps_failing_is_dropped_after_two_tries(monkeypatch):
    monkeypatch.setenv("USE_OLLAMA", "1")
    monkeypatch.setenv("OLLAMA_MODEL", "tiny:1b")
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        return httpx.Response(200, json={"models": [{"name": "tiny:1b"}]}, request=httpx.Request("GET", url))

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(structure.httpx, "get", fake_get)
    monkeypatch.setattr(structure.httpx, "post", fake_post)
    notes, engine = structure.structure_transcript((SAMPLE_TEXT + " ") * 20)
    assert engine == "rule-based"
    assert calls["n"] == 2
    assert_valid(notes)


GARDEN = (
    "Okay so, um, I wanted to record a quick note about the garden project. So first we need to clear the old beds, "
    "and then we add compost, you know, about ten centimeters on top. Next we plant the tomatoes and the peppers. "
    "Finally we set up the watering system so it runs every morning. The budget is around 400 euros for the compost, "
    "150 euros for the seeds and 220 euros for the irrigation parts. I think that is okay. "
    "My sister Anna said she can help on Saturday, so we should plan the clearing for that day. "
    "We need to check the weather, because if it rains the soil will be too wet. "
    "We could build a compost bin with old pallets, that would be cheap. "
    "The neighbours asked if they can share some of the tomatoes, which is fine with me. "
    "The plan is clear the beds on Saturday, compost on Sunday, and plant the following weekend."
)


def test_title_is_the_topic_not_the_opening_words():
    notes = structure.structure_rules(GARDEN)
    assert notes["title"] == "Garden project"
    bullets = [b for s in notes["sections"] for b in s["bullets"]]
    assert not any("record a quick note" in b for b in bullets)  # the introduction is not repeated as a point
    assert not any(b.lower().startswith(("okay", "um")) for b in bullets)
    assert "I think that is okay." not in bullets  # a remark with no content is skipped in longer notes


def test_short_notes_keep_every_sentence():
    notes = structure.structure_rules("Buy milk. Call Anna. Okay.")
    bullets = [b for s in notes["sections"] for b in s["bullets"]]
    assert "Buy milk." in bullets and "Call Anna." in bullets


# ---------------------------------------------- action items, sections, status

def test_extract_actions_finds_tasks_and_keeps_the_task_clause():
    sents = [
        "We went to the park yesterday.",
        "I need to call Anna about the compost delivery.",
        "Also, my sister said she can help on Saturday, so we should plan the clearing for that day.",
        "Remember to buy seeds because they sell out fast.",
        "Should we paint the shed?",
        "It will rain on Friday.",
        "If it rains, we should move the event indoors.",
    ]
    got = structure.extract_actions(sents)
    assert got == [
        "I need to call Anna about the compost delivery",
        "We should plan the clearing for that day",
        "Remember to buy seeds",
        "If it rains, we should move the event indoors",
    ][:len(got)]
    assert len(got) == 4  # questions and plain statements are not tasks


def test_extract_actions_french_and_limit():
    assert structure.extract_actions(["Il faut appeler le plombier demain matin."]) == ["Il faut appeler le plombier demain matin"]
    names = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
    many = [f"We need to call {n} about the {names[(i + 3) % 12]} order." for i, n in enumerate(names)]
    assert len(structure.extract_actions(many)) == structure.MAX_ACTIONS


def test_rule_based_notes_carry_action_items():
    notes = structure.structure_rules(GARDEN + " Remember to order the seeds before Friday.")
    assert any("order the seeds" in a for a in notes["actions"])
    assert notes["actions"] == [a for a in notes["actions"] if a]
    assert structure.normalize_notes({"title": "t", "sections": [{"heading": "h", "bullets": ["b"]}],
                                      "actions": ["- [ ] Call Anna", "call anna", ""]})["actions"] == ["Call Anna"]


def test_sections_follow_a_change_of_topic():
    garden = ["The tomatoes need more sun in the garden.", "The garden soil is dry and the tomatoes are small.",
              "We water the tomatoes in the garden every morning.", "The garden beds need compost for the tomatoes.",
              "Garden compost makes the soil rich for tomatoes."]
    budget = ["The quarterly budget shows a gap in revenue.", "Revenue targets for the quarterly budget are missed.",
              "The budget review covers revenue and costs.", "Costs in the quarterly budget rose with revenue falling.",
              "Finance will present the budget revenue report."]
    groups = structure.topic_groups(garden + budget)
    assert [len(g) for g in groups] == [5, 5]
    assert groups[0] == garden
    # Short notes stay in one section.
    assert len(structure.topic_groups(garden)) == 1
    # Long notes are cut into sections of 3 to 8 sentences.
    many = [f"Sentence {i} talks about topic{i // 4} things and topic{i // 4} details." for i in range(40)]
    assert all(3 <= len(g) <= 8 for g in structure.topic_groups(many))


def test_heading_prefers_a_repeated_word_pair():
    group = ["We build a compost bin from pallets.", "The compost bin needs a lid.", "Pallets are free from the shop."]
    assert structure._heading(group, Counter(), set()) == "Compost bin"


def test_engine_plan_explains_why_the_fallback_is_used(monkeypatch):
    monkeypatch.setenv("USE_OLLAMA", "0")
    plan = structure.engine_plan()
    assert plan["engine"] == "rule-based" and "switched off" in plan["reason"]
    monkeypatch.setenv("USE_OLLAMA", "1")
    monkeypatch.setenv("OLLAMA_HOST_URL", "http://127.0.0.1:1")
    plan = structure.engine_plan()
    assert plan["engine"] == "rule-based" and "not running" in plan["reason"]
    fake_ollama(monkeypatch, {})
    plan = structure.engine_plan()
    assert plan["engine"] == "ollama:tiny:1b" and plan["reason"] == ""
    monkeypatch.setenv("OLLAMA_MODEL", "other:7b")
    assert "not installed" in structure.engine_plan()["reason"]


def test_model_notes_get_actions_from_the_rules_when_the_model_gives_none(monkeypatch):
    payload = {"title": "Plan", "summary": "S.", "sections": [{"heading": "H", "bullets": ["b"]}], "actions": []}
    fake_ollama(monkeypatch, payload)
    notes, engine = structure.structure_transcript("We talked a lot. I need to call Anna about the delivery tomorrow.")
    assert engine.startswith("ollama:")
    assert notes["actions"] == ["I need to call Anna about the delivery tomorrow"]
    payload["actions"] = ["Book the van"]
    notes, _ = structure.structure_transcript("We talked a lot. I need to call Anna about the delivery tomorrow.")
    assert notes["actions"] == ["Book the van"]
