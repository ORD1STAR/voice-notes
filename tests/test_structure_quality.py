"""Quality of the notes: titles, headings, key points, action items, filler and the checks on model output."""
import httpx
import pytest

from app import structure
from transcripts import FRENCH, HOWTO, IDEAS, MEETING


def bullets_of(notes):
    return [b for s in notes["sections"] for b in s["bullets"]]


# ------------------------------------------------------------ spoken text

def test_hedges_and_openers_are_removed_but_sequence_words_stay():
    clean = structure.clean_sentence
    assert clean("Basically I think we should do shorter episodes.") == "We should do shorter episodes."
    assert clean("And another thing, we could add a newsletter.") == "We could add a newsletter."
    assert clean("So we decided to wait.") == "We decided to wait."
    assert clean("Oh and I have to check the contract.") == "I have to check the contract."
    assert clean("And then we add compost.") == "Then we add compost."
    assert clean("Then you shape the loaf.") == "Then you shape the loaf."
    assert clean("Finally, we ship.") == "Finally, we ship."
    assert clean("I think that we should wait for the copy.") == "We should wait for the copy."
    assert clean("I think that is a good call.") == "That is a good call."
    assert clean("The the budget is fine, you know.") == "The budget is fine."


def test_empty_remarks_are_recognised():
    empty = structure.is_empty_remark
    for text in ("That is a good call.", "That's it for now.", "Yeah, that was it.", "Thank you.", "What else?",
                 "It was really interesting.", "Okay."):
        assert empty(structure.clean_sentence(text) or text), text
    for text in ("That is a good call for the Monday release.", "The budget is 400 euros.", "We should call Anna.",
                 "It is important to back up the data."):
        assert not empty(text), text


def test_sentences_are_not_cut_after_abbreviations():
    got = structure.split_sentences("Dr. Smith will call at 5 p.m. tomorrow. We agreed, e.g. Anna comes too. Done.")
    assert got == ["Dr. Smith will call at 5 p.m. tomorrow.", "We agreed, e.g. Anna comes too.", "Done."]


def test_empty_remarks_and_fillers_do_not_reach_the_notes():
    notes = structure.structure_rules(MEETING)
    bullets = bullets_of(notes)
    assert not any(b.lower().startswith(("okay", "um", "yeah", "honestly", "so ")) for b in bullets)
    assert not any("good call" in b for b in bullets)
    assert not any(b.lower().startswith("is ") for b in bullets)
    ideas = bullets_of(structure.structure_rules(IDEAS))
    assert not any(b.lower().startswith(("hmm", "anyway", "basically", "oh ")) for b in ideas)
    assert not any("that's it" in b.lower() for b in ideas)
    assert "We should do shorter episodes." in ideas


# ----------------------------------------------------- titles and headings

def test_titles_name_the_topic():
    assert structure.structure_rules(MEETING)["title"] == "Monday sync with the product team"
    assert structure.structure_rules(IDEAS)["title"] == "Podcast idea"
    assert structure.structure_rules(HOWTO)["title"] == "How to make sourdough bread"
    assert structure.structure_rules(FRENCH)["title"] == "Déménagement"


def test_headings_name_the_topic_of_each_section():
    notes = structure.structure_rules(MEETING)
    assert [s["heading"] for s in notes["sections"]] == ["Mobile release", "Hiring", "Customer feedback"]
    # the announcement of a topic became the heading and is not repeated as a bullet
    assert not any(b.startswith(("The last topic", "Then we moved on")) for b in bullets_of(notes))
    ideas = [s["heading"] for s in structure.structure_rules(IDEAS)["sections"]]
    assert ideas == ["Episodes", "Guests", "Intro music"]
    # a heading never is a unit, a filler verb or a number word
    for text in (MEETING, IDEAS, HOWTO):
        for sec in structure.structure_rules(text)["sections"]:
            assert sec["heading"].lower() not in {"minutes", "seconds", "first", "takes", "think", "send", "every"}


def test_a_list_of_steps_stays_in_one_section_and_every_step_is_kept():
    notes = structure.structure_rules(HOWTO)
    assert len(notes["sections"]) == 1
    assert notes["sections"][0]["heading"] == "Steps"
    steps = [b for b in bullets_of(notes) if b.startswith(("First", "Then", "After that", "Finally"))]
    assert len(steps) == 5
    assert any(b.startswith("The next morning you mix 500 grams") for b in bullets_of(notes))  # the amounts stay


# ------------------------------------------------- summary and key points

def test_key_points_are_decisions_deadlines_and_figures_not_tasks():
    notes = structure.structure_rules(MEETING)
    kp = notes["key_points"]
    assert 2 <= len(kp) <= 5
    assert any("decided to keep the old integration" in k for k in kp)
    assert "delayed by two weeks" in notes["summary"]
    assert any("most urgent" in k for k in kp)
    for k in kp:  # none of them is a task and none repeats the summary
        assert k not in notes["summary"]
        assert not structure._task_text(k)[0]
    assert all(k in bullets_of(notes) for k in kp)


def test_short_notes_have_no_key_points_and_a_summary_that_is_not_a_task():
    notes = structure.structure_rules("Buy milk. Call Anna. The budget is fine.")
    assert notes["key_points"] == []
    ideas = structure.structure_rules(IDEAS)
    assert "hosting contract" not in ideas["summary"]
    assert "shorter episodes" in ideas["summary"]


def test_old_notes_get_key_points_without_being_changed():
    old = {"title": "T", "summary": "S.", "sections": [
        {"heading": "H", "bullets": structure.structure_rules(MEETING)["sections"][0]["bullets"]
         + structure.structure_rules(MEETING)["sections"][1]["bullets"]
         + structure.structure_rules(MEETING)["sections"][2]["bullets"]}]}
    upgraded = structure.ensure_key_points(old)
    assert upgraded is not old and "key_points" not in old
    assert upgraded["key_points"]
    again = {**old, "key_points": []}
    assert structure.ensure_key_points(again) is again  # a note that has the field is left alone


# ------------------------------------------------------------ action items

def test_actions_are_worded_as_instructions():
    got = structure.structure_rules(MEETING)["actions"]
    assert got == [
        "Maria: send the job description to the recruiters by Wednesday",
        "Fix PDF export before adding new features",
        "Ask Tom to profile the export code",
        "Send a short update to the customers who complained",
        "Book the room for the next sync",
    ]
    assert structure.structure_rules(IDEAS)["actions"] == [
        "Do shorter episodes", "Cut the intro music to ten seconds", "Check the hosting contract"]


def test_how_to_instructions_and_statements_about_things_are_not_tasks():
    actions = structure.structure_rules(HOWTO)["actions"]
    assert actions == ["Try with a bit of rye flour"]  # "you ..." in a recipe and "the starter must be active" are not tasks
    for text in ("The server must be restarted.", "It will rain on Friday.", "We have to say it was a surprise.",
                 "I will be there.", "Should we call Anna?", "You should always stretch first."):
        assert structure.extract_actions([text]) == [], text


def test_small_or_vague_tasks_are_not_listed():
    assert structure.extract_actions(["I have to go now.", "We have to say it was a surprise.", "I'll be there at five."]) == []
    assert structure.extract_actions(["I need to remember to call mom."]) == ["Call mom"]
    assert structure.extract_actions(["Oh and I need to call my mother, her birthday is on the 3rd, and I should order the flowers."]) \
        == ["Call my mother", "Order the flowers"]


def test_assigned_tasks_and_imperatives():
    got = structure.extract_actions([
        "The design team will finish the landing page by Friday, and marketing still needs the copy.",
        "Call the bank tomorrow morning.",
        "Sarah needs to prepare the report for the review.",
        "Once the copy is in, we need to publish the page.",
    ])
    assert got == [
        "Design team: finish the landing page by Friday",
        "Call the bank tomorrow morning",
        "Sarah: prepare the report for the review",
        "Once the copy is in, publish the page",
    ]


# ------------------------------------------------- checks on model output

SOURCE = ("We met on Monday. The release slips by two weeks because the payment integration is unstable. "
          "The budget is 400 euros. Maria will send the job description by Wednesday.")


def test_refine_drops_invented_content_and_repairs_generic_parts():
    raw = {
        "title": "Title: Notes.",
        "summary": "The release slips by two weeks because the payment integration is unstable.",
        "key_points": ["The release slips by two weeks.", "The server costs 9000 euros."],
        "sections": [
            {"heading": "Notes", "bullets": [
                "So the release slips by two weeks.",
                "The payment integration is unstable.",
                "The team bought a new espresso machine.",  # not in the transcript
                "The budget is 9000 euros.",  # a number that was never said
                "That is a good call.",
                "The release slips by two weeks.",  # repeats the first bullet
            ]},
            {"heading": "Invented", "bullets": ["Everyone will fly to Paris for the offsite."]},
        ],
        "actions": ["Send the job description by Wednesday", "Hire a pastry chef"],
    }
    out = structure.refine_model_notes(structure.normalize_notes(raw), SOURCE)
    assert len(out["sections"]) == 1  # the section with invented content is gone
    assert out["sections"][0]["heading"] == "Release slips"  # "Notes" is replaced by a heading from the content
    assert out["sections"][0]["bullets"] == ["The release slips by two weeks.", "The payment integration is unstable."]
    assert out["title"] == "We met on Monday"  # "Title: Notes." was a label, not a title
    assert out["key_points"] == ["The release slips by two weeks."]  # the invented 9000 euros is gone
    assert out["actions"] == ["Send the job description by Wednesday"]  # "Hire a pastry chef" was never said


def test_refine_rejects_a_model_answer_with_nothing_true_in_it():
    raw = structure.normalize_notes({"title": "x", "summary": "y", "sections": [
        {"heading": "H", "bullets": ["Unrelated astronomy facts about distant galaxies."]}]})
    with pytest.raises(structure.StructureError):
        structure.refine_model_notes(raw, SOURCE)


def test_model_call_carries_a_worked_example_and_asks_for_key_points(monkeypatch):
    monkeypatch.setenv("USE_OLLAMA", "1")
    monkeypatch.setenv("OLLAMA_MODEL", "tiny:1b")
    seen = {}

    def fake_get(url, timeout=None):
        return httpx.Response(200, json={"models": [{"name": "tiny:1b"}]}, request=httpx.Request("GET", url))

    def fake_post(url, json=None, timeout=None):
        seen["payload"] = json
        answer = {"title": "Release plan", "summary": "The release slips.", "key_points": [],
                  "sections": [{"heading": "Release", "bullets": ["The release slips by two weeks."]}], "actions": []}
        return httpx.Response(200, json={"message": {"content": __import__("json").dumps(answer)}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(structure.httpx, "get", fake_get)
    monkeypatch.setattr(structure.httpx, "post", fake_post)
    notes, engine = structure.structure_transcript(SOURCE)
    assert engine == "ollama:tiny:1b"
    payload = seen["payload"]
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user", "assistant", "user"]  # system prompt, one worked example, the real transcript
    assert SOURCE in payload["messages"][-1]["content"]
    assert "key_points" in payload["format"]["properties"] and "actions" in payload["format"]["required"]
    assert payload["options"]["temperature"] <= 0.2 and payload["options"]["num_predict"] > 0
    assert notes["title"] == "Release plan"
    assert "key_points" in notes and "actions" in notes


# ------------------------------------------------------- odd and long input

def test_chinese_sentence_marks_split_sentences():
    assert structure.split_sentences("\u4eca\u5929\u6211\u4eec\u5f00\u4f1a\u3002\u9884\u7b97\u4e0d\u53d8\u3002") == [
        "\u4eca\u5929\u6211\u4eec\u5f00\u4f1a\u3002", "\u9884\u7b97\u4e0d\u53d8\u3002"]


def test_a_very_long_note_is_structured_quickly_into_a_readable_number_of_sections():
    import time

    big = " ".join([MEETING, IDEAS, HOWTO] * 50)  # about 20000 words
    start = time.time()
    notes = structure.structure_rules(big)
    assert time.time() - start < 10
    assert 5 <= len(notes["sections"]) <= 80
    assert all(len(s["bullets"]) <= 9 for s in notes["sections"])
    assert len(notes["key_points"]) <= 5 and len(notes["actions"]) <= structure.MAX_ACTIONS


def test_odd_input_never_crashes():
    for text in ("a" * 5000, "1 2 3 4 5 6 7 8 9", "WE NEED TO CALL THE BANK. THE BUDGET IS 400 EUROS.",
                 "We need to call Anna. " * 100, "\u0627\u0644\u064a\u0648\u0645 \u0646\u0627\u0642\u0634\u0646\u0627 \u0627\u0644\u062e\u0637\u0629."):
        notes = structure.structure_rules(text)
        assert notes["title"] and notes["sections"]
