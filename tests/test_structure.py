import json

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
