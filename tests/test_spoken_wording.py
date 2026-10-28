"""Notes that read like notes: the scrubber for spoken framing and the model pass for lines that still sound spoken.

The network is never used: Groq is replaced by the fake of test_groq_structuring.
"""
import pytest

from app import speech, structure
from test_groq_structuring import FakeNet, status

MODEL = "groq:llama-3.3-70b-versatile"


# ------------------------------------------------------------------ the scrubber

REWRITTEN = [
    # requests to a listener become the subject
    ("Explique moi comment fonctionne le chargement des batteries.", "Comment fonctionne le chargement des batteries."),
    ("Tell me about the new onboarding flow.", "The new onboarding flow."),
    ("Can you explain how the pump works?", "How the pump works."),
    ("Could you tell me what the deadline is?", "What the deadline is."),
    ("I want you to explain how the pump works.", "How the pump works."),
    ("Il faut que tu me dises quand arrive le colis.", "Quand arrive le colis."),
    ("Tu peux me dire combien coûte le loyer ?", "Combien coûte le loyer."),
    ("Alors, explique moi ce que tu penses du budget.", "Ce que tu penses du budget."),
    # a plain request for an action stays an instruction
    ("Can you call Paul tomorrow?", "Call Paul tomorrow."),
    ("Tu peux appeler Paul demain ?", "Appeler Paul demain."),
    # discourse markers in front of the content
    ("So basically what I want to say is that the launch slips to May.", "The launch slips to May."),
    ("Okay so, the thing is, we have no budget for ads.", "We have no budget for ads."),
    ("I was thinking, maybe we could move the meeting to Friday.", "Maybe we could move the meeting to Friday."),
    ("Well, the budget is 5000 euros.", "The budget is 5000 euros."),
    ("To sum up, there are three priorities.", "There are three priorities."),
    ("Donc voilà, en gros le client veut une livraison avant mars.", "Le client veut une livraison avant mars."),
    ("Bon, ben, je vais te dire, le prix est trop élevé.", "Le prix est trop élevé."),
    ("Tu vois ce que je veux dire, le serveur est trop lent.", "Le serveur est trop lent."),
    ("Comme je te disais tout à l'heure, il faut relancer Paul.", "Il faut relancer Paul."),
    ("Je te rappelle que la réunion est lundi.", "La réunion est lundi."),
    ("Pour résumer, nous avons trois priorités.", "Nous avons trois priorités."),
    ("Bueno, pues, como te decía, hay que llamar al banco.", "Hay que llamar al banco."),
    ("Je ne sais plus ce que je voulais dire, bref, la date limite est le 5 juin.", "La date limite est le 5 juin."),
    ("What was I saying? Right, the deadline is June 5.", "The deadline is June 5."),
    ("The server is slow, right?", "The server is slow."),
    ("Moving on to the next topic: pricing.", "Pricing."),
]

GONE = [
    "Thanks for listening.", "Merci d'avoir écouté.", "That's all for this note.", "Voilà, c'est tout pour aujourd'hui.",
    "Hello, it is Amine speaking.", "Hey, so this is a note for myself about the garden.",
    "Salut, c'est un message pour moi-même.", "Hola, esto es una nota de voz sobre el viaje.",
    "I'm recording this on my phone while walking.", "As you can hear, the background is noisy.",
    "Hmm, let me see, where was I.", "I will now talk about the second point.", "Next, I will talk about pricing.",
    "Passons maintenant au deuxième point.", "Anyway, back to the budget.", "Revenons à nos moutons, le budget.",
]

UNTOUCHED = [
    "It is cold here in the warehouse.", "This is a good recording device for interviews.",
    "Back to back meetings are planned on Monday.", "Alors que le prix baisse, les ventes augmentent.",
    "Right to appeal is limited to 30 days.", "Like a rolling stone, the project moves slowly.",
    "Hi-fi speakers cost 300 euros.", "Il faut que tu appelles Paul demain.", "Now the invoice must be paid by Friday.",
    "Tell the team about the delay.", "Thank you cards go out on Friday.", "The design team will finish the page by Friday.",
]


@pytest.mark.parametrize("spoken,written", REWRITTEN)
def test_spoken_framing_is_turned_into_the_content(spoken, written):
    assert speech.scrub(spoken) == written


@pytest.mark.parametrize("sentence", GONE)
def test_sentences_about_the_recording_or_the_act_of_speaking_are_dropped(sentence):
    assert speech.scrub(sentence) == ""


@pytest.mark.parametrize("sentence", UNTOUCHED)
def test_real_content_is_not_touched(sentence):
    assert speech.scrub(sentence) == sentence


def test_a_paragraph_keeps_only_its_content():
    text = ("Hello, it is Amine speaking. Okay so, the launch slips to May. Can you explain how the pump works? "
            "Thanks for listening.")
    assert speech.scrub(text) == "The launch slips to May. How the pump works."


@pytest.mark.parametrize("line,expected", [
    ("Do you know the answer?", True), ("Let's start with the plan.", True), ("You can export the notes.", True),
    ("Tu vois le problème", True), ("This note covers the budget.", True), ("I think the price is too high.", True),
    ("The pump works with a valve.", False), ("The launch moves to May.", False), ("See i.e. the list", False),
])
def test_has_speech_flags_lines_that_still_sound_spoken(line, expected):
    assert speech.has_speech(line) is expected


def test_a_task_may_say_i():
    assert speech.has_speech("I need to call Paul.") is True
    assert speech.has_speech("I need to call Paul.", first_person=False) is False


def test_old_saved_notes_lose_the_new_framing_when_shown():
    notes = {"title": "Garden", "summary": "Okay so, the garden needs water twice a week.",
             "sections": [{"heading": "Watering", "bullets": ["Hello, it is Amine speaking.",
                                                              "Well, the roses need water twice a week.",
                                                              "Thanks for listening."]}],
             "actions": []}
    out = structure.unspoken_notes(notes)
    assert out["summary"] == "The garden needs water twice a week."
    assert out["sections"][0]["bullets"] == ["The roses need water twice a week."]


# ------------------------------------------------------------------ the model pass for lines that still sound spoken

TRANSCRIPT = ("The design team will finish the landing page by Friday. Marketing still needs the final copy before "
              "the newsletter goes out. The ad budget stays at 5000 euros for the quarter.")


def draft(bullets, summary="The landing page is due on Friday and the newsletter copy is missing."):
    return {"title": "Website launch", "summary": summary, "key_points": [],
            "sections": [{"heading": "Launch status", "bullets": bullets}], "action_items": []}


TALKY = draft(["The design team will finish the landing page by Friday.",
               "You should ask marketing about the final copy before the newsletter goes out.",
               "Does the ad budget stay at 5000 euros for the quarter?"])


class Router:
    """The writer returns TALKY; the wording pass returns what the test gives it."""

    def __init__(self, tidy):
        self.tidy = tidy
        self.requests = []

    def __call__(self, req):
        system = req["messages"][0]["content"]
        self.requests.append(req)
        if "edit single lines" in system:
            return self.tidy(req) if callable(self.tidy) else self.tidy
        return TALKY


FIXED = {"lines": [
    {"id": 0, "text": "Marketing still needs the final copy before the newsletter goes out."},
    {"id": 1, "text": "The ad budget stays at 5000 euros for the quarter."}]}


def run(monkeypatch, tidy, enabled=True, text=TRANSCRIPT):
    monkeypatch.setenv("GROQ_TIDY", "1" if enabled else "0")
    router = Router(tidy)
    net = FakeNet(monkeypatch, chat=router)
    notes, engine = structure.structure_transcript(text)
    assert engine == MODEL
    tidy_calls = [r for r in net.chat_calls if "edit single lines" in r["messages"][0]["content"]]
    return notes, tidy_calls, router


def bullets(notes):
    return [b for s in notes["sections"] for b in s["bullets"]]


def test_lines_that_still_sound_spoken_are_rewritten_by_one_extra_call(monkeypatch):
    notes, calls, _ = run(monkeypatch, FIXED)
    assert len(calls) == 1
    assert bullets(notes) == ["The design team will finish the landing page by Friday.",
                              "Marketing still needs the final copy before the newsletter goes out.",
                              "The ad budget stays at 5000 euros for the quarter."]
    sent = calls[0]["messages"][1]["content"]
    assert "You should ask marketing" in sent and "Does the ad budget" in sent
    assert "The design team will finish the landing page by Friday." not in sent.split("Lines (JSON):")[1]
    assert "Transcript:" in sent


def test_the_wording_pass_makes_no_call_when_nothing_sounds_spoken(monkeypatch):
    clean = draft(["The design team will finish the landing page by Friday.",
                   "Marketing still needs the final copy before the newsletter goes out."])
    monkeypatch.setenv("GROQ_TIDY", "1")
    net = FakeNet(monkeypatch, chat=clean)
    structure.structure_transcript(TRANSCRIPT)
    assert len(net.chat_calls) == 1


def test_the_wording_pass_can_be_switched_off(monkeypatch):
    notes, calls, _ = run(monkeypatch, FIXED, enabled=False)
    assert calls == []
    assert any(b.startswith("You should ask") for b in bullets(notes))


def test_a_rewrite_that_brings_a_new_figure_or_name_is_refused(monkeypatch):
    bad = {"lines": [{"id": 0, "text": "Marketing needs the final copy by Thursday 14 for the newsletter."},
                     {"id": 1, "text": "Luigi confirmed that the ad budget stays at 7000 euros."}]}
    notes, calls, _ = run(monkeypatch, bad)
    assert len(calls) == 1
    assert any(b.startswith("You should ask") for b in bullets(notes))
    assert not any("Luigi" in b or "7000" in b for b in bullets(notes))


def test_a_rewrite_that_still_sounds_spoken_is_refused(monkeypatch):
    still = {"lines": [{"id": 0, "text": "Do you know that marketing still needs the final copy?"},
                       {"id": 1, "text": "The ad budget stays at 5000 euros for the quarter."}]}
    notes, _, _ = run(monkeypatch, still)
    assert any(b.startswith("You should ask") for b in bullets(notes))
    assert "The ad budget stays at 5000 euros for the quarter." in bullets(notes)


def test_a_line_that_is_only_talk_can_be_dropped(monkeypatch):
    talk = draft(["The design team will finish the landing page by Friday.",
                  "Do you see the newsletter copy and the landing page?"])
    drop = {"lines": [{"id": 0, "text": ""}]}
    monkeypatch.setenv("GROQ_TIDY", "1")
    net = FakeNet(monkeypatch, chat=lambda req: drop if "edit single lines" in req["messages"][0]["content"] else talk)
    notes, _ = structure.structure_transcript(TRANSCRIPT)
    assert bullets(notes) == ["The design team will finish the landing page by Friday."]


@pytest.mark.parametrize("answer", [status(500), {"lines": "nothing"}, {"lines": [{"id": "x", "text": 4}]}, {}, "not json"])
def test_a_wording_pass_that_fails_keeps_the_notes(monkeypatch, answer):
    notes, _, _ = run(monkeypatch, answer)
    assert len(bullets(notes)) == 3
    assert any(b.startswith("You should ask") for b in bullets(notes))


def test_a_task_that_says_i_is_not_sent_for_rewriting():
    notes = {"summary": "", "key_points": [], "actions": ["I need to call Paul before Friday."],
             "sections": [{"heading": "A", "bullets": ["The launch moves to May."]}]}
    assert structure._speech_lines(notes) == []


def test_at_most_forty_lines_are_sent():
    notes = {"summary": "", "key_points": [], "actions": [],
             "sections": [{"heading": "A", "bullets": [f"Do you remember item {i}?" for i in range(60)]}]}
    assert len(structure._speech_lines(notes)) == structure.MAX_TIDY_LINES


def test_the_prompt_follows_the_language():
    assert "French" in structure.groq_tidy_prompt("fr")
    assert "edit single lines" in structure.groq_tidy_prompt()


# ------------------------------------------------------------------ the rules (no model)

RULES_EN = ("Okay so, the thing is, the roses need water twice a week. Can you tell me when to prune the apple tree? "
            "Anyway, I think we should prune it in February. I need to buy fertilizer before Saturday. "
            "The shed roof leaks, so we should fix it before March. As I said before, the budget is 300 euros.")


def test_a_task_about_it_names_the_right_thing():
    actions = structure.structure_rules(RULES_EN)["actions"]
    assert "Prune the apple tree in February" in actions
    assert "Fix the shed roof before March" in actions
    assert not any("fertilizer before March" in a or "prune the prune" in a.lower() for a in actions)


def test_it_stays_when_nothing_in_the_text_says_what_it_is():
    actions = structure._task_text("We should fix it before March.", "I need to buy fertilizer before Saturday.")
    assert actions == ("Fix it before March", "self")


def test_the_rules_leave_no_spoken_framing_in_the_bullets():
    notes = structure.structure_rules(RULES_EN)
    bullets = [b for s in notes["sections"] for b in s["bullets"]]
    assert "The budget is 300 euros." in bullets
    assert not any("As I said" in b or "Anyway" in b or "Can you" in b for b in bullets)
    assert "When to prune the apple tree." in bullets


def test_french_rule_tasks_start_with_a_capital_and_the_listener_tag_goes():
    notes = structure.structure_rules(
        "Alors, explique moi comment fonctionne le remboursement. Quand un client envoie sa demande, il faut d'abord "
        "vérifier le dossier. Ensuite on l'envoie à Karim pour validation, tu vois ce que je veux dire. "
        "Karim a deux jours pour répondre.")
    assert all(a[:1].isupper() for a in notes["actions"])
    assert not any("ce que je veux dire" in b for s in notes["sections"] for b in s["bullets"])
    assert "ce que je veux dire" not in notes["summary"]
