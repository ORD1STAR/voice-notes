"""Notes must read like written notes: no spoken framing, and a last editing pass on the wording (Groq).

The network is never used: Groq is replaced by the fake of test_groq_structuring.
"""
import copy

import pytest

from app import speech, structure
from test_groq_structuring import FR_NOTES, FR_TRANSCRIPT, FakeNet

# ------------------------------------------------------------------ the speech scrubber


@pytest.mark.parametrize("spoken, written", [
    ("Explique moi comment fonctionne l'export PDF.", "Comment fonctionne l'export PDF."),
    ("Explique-moi le plan de lancement du produit.", "Le plan de lancement du produit."),
    ("Tell me about the Q3 budget plan.", "The Q3 budget plan."),
    ("Tell me how the export works.", "How the export works."),
    ("Dis-moi si c'est possible de livrer avant le 12.", "Si c'est possible de livrer avant le 12."),
    ("Muéstrame cómo funciona el sistema de pagos.", "Cómo funciona el sistema de pagos."),
    ("Can you please check the invoice before Friday?", "Check the invoice before Friday."),
    ("Peux-tu envoyer le fichier à Anna demain ?", "Envoyer le fichier à Anna demain."),
    ("Écoute, le camion arrive samedi.", "Le camion arrive samedi."),
    ("Look, we need to decide this week.", "We need to decide this week."),
    ("The release is delayed by two weeks, you know what I mean.", "The release is delayed by two weeks."),
    ("Le camion est réservé pour samedi, tu vois.", "Le camion est réservé pour samedi."),
])
def test_requests_and_tags_are_rewritten_as_content(spoken, written):
    assert speech.unspeak(spoken) == written


@pytest.mark.parametrize("spoken", [
    "Voilà.", "Explique-moi.", "As I said before.", "Voilà ce que je pense.", "Écoute bien.",
    "In this note I am going to talk about the garden project and the budget.",
    "Je voulais te parler de la fête de samedi soir.",
])
def test_pure_framing_disappears(spoken):
    assert speech.unspeak(spoken) == ""


@pytest.mark.parametrize("sentence", [
    "The budget is 400 euros.",
    "Look into the export bug.",
    "I will explain the process to Tom tomorrow.",
    "We will see Anna on Monday.",
    "Karim a deux jours pour répondre.",
    "Can you imagine the cost?",
])
def test_real_content_is_left_alone(sentence):
    out = speech.unspeak(sentence)
    assert out.rstrip(".?") == sentence.rstrip(".?") or sentence.startswith("Can you imagine")


def test_a_text_is_scrubbed_sentence_by_sentence():
    assert speech.scrub("Explique moi comment ça marche. Le budget est de 400 euros. Voilà.") == \
        "Comment ça marche. Le budget est de 400 euros."


def test_french_fillers_and_repeated_starts_are_dropped():
    clean = structure.clean_sentence
    assert clean("Donc en fait, quand un client envoie sa demande, euh, il faut vérifier le dossier.") == \
        "Quand un client envoie sa demande, il faut vérifier le dossier."
    assert clean("Le problème, c'est que, c'est que souvent les dossiers sont incomplets, du coup on perd du temps.") == \
        "Le problème, c'est que souvent les dossiers sont incomplets, on perd du temps."
    assert clean("Je pense qu'on devrait ajouter un formulaire en ligne.") == "On devrait ajouter un formulaire en ligne."
    assert clean("Ben will send the file on Monday.") == "Ben will send the file on Monday."


def test_the_rules_keep_no_speech_leftovers():
    notes = structure.structure_rules(FR_TRANSCRIPT)
    text = " ".join([notes["title"], notes["summary"]] + [b for s in notes["sections"] for b in s["bullets"]]).lower()
    for leftover in ("explique", "euh", "du coup", "en fait", "je pense"):
        assert leftover not in text, leftover
    english = structure.structure_rules(
        "Hey, so tell me about the new onboarding flow. New users sign up with an email address and then we send a "
        "confirmation link. Listen, the conversion rate is 12 percent right now. Can you check the funnel with Anna "
        "before Friday? In this note I am going to talk about the pricing page. The pricing page has three plans, "
        "you know what I mean. Voila, that's it.")
    bullets = " ".join(b for s in english["sections"] for b in s["bullets"]).lower()
    for leftover in ("tell me", "listen", "can you", "in this note", "you know", "voila"):
        assert leftover not in bullets, leftover
    assert "pricing page has three plans." in bullets


# ------------------------------------------------------------ model output is scrubbed too


def test_a_model_answer_with_spoken_framing_is_scrubbed():
    answer = copy.deepcopy(FR_NOTES)
    answer["title"] = "Explique moi la procédure de remboursement"
    answer["summary"] = ("Explique moi comment ça marche. La procédure de remboursement passe par la vérification "
                         "du dossier puis la validation par Karim.")
    answer["sections"][0]["bullets"] = ["Voilà.", "Écoute, le dossier du client est d'abord vérifié.",
                                        "Karim valide ensuite la demande sous deux jours."]
    notes = structure.refine_model_notes(structure.normalize_notes(answer), FR_TRANSCRIPT, relaxed=True, language="fr")
    assert notes["title"] == "La procédure de remboursement"
    assert not notes["summary"].lower().startswith("explique")
    assert notes["sections"][0]["bullets"][0] == "Le dossier du client est d'abord vérifié."
    assert "Voilà." not in notes["sections"][0]["bullets"]


def test_a_small_local_model_that_copies_the_request_is_cleaned_too():
    answer = copy.deepcopy(FR_NOTES)
    answer["sections"][0]["bullets"] = [
        "Explique moi comment ça marche l'ancienne procédure de remboursement.",
        "Euh, le dossier du client est d'abord vérifié, tu vois.",
        "Karim valide ensuite la demande sous deux jours.",
    ]
    notes = structure.refine_model_notes(structure.normalize_notes(answer), FR_TRANSCRIPT, language="fr")
    bullets = notes["sections"][0]["bullets"]
    assert "Le dossier du client est d'abord vérifié." in bullets
    assert not any(b.lower().startswith(("explique", "euh")) for b in bullets)


def test_the_prompts_forbid_speech_leftovers():
    for prompt in (structure.groq_system_prompt("fr"), structure.groq_review_prompt("fr"),
                   structure.groq_polish_prompt("fr"), structure.SYSTEM_PROMPT):
        assert "explique moi" in prompt.lower() or "explain to me" in prompt.lower()
    assert "French" in structure.groq_polish_prompt("fr")


# ------------------------------------------------------------------ the final editing pass


def is_polish(req):
    return "final editor" in req["messages"][0]["content"]


def polished_answer():
    answer = copy.deepcopy(FR_NOTES)
    answer["title"] = "Procédure de remboursement des clients"
    answer["summary"] = ("Le remboursement suit trois étapes : vérification du dossier, validation par Karim puis "
                         "virement par la comptabilité. Les dossiers incomplets font perdre du temps.")
    answer["sections"][0]["heading"] = "Étapes du remboursement"
    answer["sections"][1]["bullets"][0] = "Les dossiers des clients sont souvent incomplets, ce qui fait perdre du temps."
    return answer


def router(polish):
    def chat(req):
        if is_polish(req):
            return polish(req) if callable(polish) else polish
        return FR_NOTES
    return chat


def run(monkeypatch, polish, on=True, review=False, progress=None):
    net = FakeNet(monkeypatch, chat=router(polish), review=review)
    monkeypatch.setenv("GROQ_POLISH", "1" if on else "0")
    notes, engine = structure.structure_transcript(FR_TRANSCRIPT, progress, language="fr")
    return notes, engine, net


def test_the_final_editing_pass_is_one_more_call_and_its_wording_is_used(monkeypatch):
    notes, engine, net = run(monkeypatch, polished_answer())
    assert engine.startswith("groq:")
    assert [is_polish(r) for r in net.chat_calls] == [False, True]
    assert notes["title"] == "Procédure de remboursement des clients"
    assert notes["sections"][1]["bullets"][0].startswith("Les dossiers des clients sont souvent incomplets")
    user = net.chat_calls[1]["messages"][1]["content"]
    assert "Transcript:" in user and "Notes (JSON):" in user and "Karim" in user


def test_the_editing_pass_can_be_switched_off(monkeypatch):
    notes, _, net = run(monkeypatch, polished_answer(), on=False)
    assert len(net.chat_calls) == 1
    assert notes["title"] == FR_NOTES["title"]


@pytest.mark.parametrize("bad", [
    "this is not json",
    "[1, 2]",
    {"title": "", "summary": "", "sections": []},
])
def test_a_failing_editing_pass_keeps_the_draft_and_the_note_completes(monkeypatch, bad):
    notes, engine, net = run(monkeypatch, bad)
    assert engine == "groq:llama-3.3-70b-versatile"
    assert notes["title"] == FR_NOTES["title"]
    assert len(notes["sections"]) == 2


def test_an_editing_pass_that_invents_a_figure_or_loses_the_content_is_ignored(monkeypatch):
    invented = polished_answer()
    invented["sections"][0]["bullets"] = ["La comptabilité effectue le virement en 45 jours.",
                                          "Karim valide la demande sous 30 jours.",
                                          "Le dossier du client est d'abord vérifié."]
    notes, _, _ = run(monkeypatch, invented)
    flat = " ".join(b for s in notes["sections"] for b in s["bullets"])
    assert "45" not in flat and "30" not in flat

    thin = polished_answer()
    thin["sections"] = [{"heading": "Remboursement", "bullets": ["Le dossier du client est d'abord vérifié."]}]
    thin["action_items"] = []
    notes, _, _ = run(monkeypatch, thin)
    assert notes["title"] == FR_NOTES["title"]  # the draft stays: more than a third of the bullets would be lost
    assert notes["actions"]


def test_the_editing_pass_reports_its_step(monkeypatch):
    seen = []
    run(monkeypatch, polished_answer(), progress=lambda done, total, step="": seen.append((done, total, step)))
    assert (1, 2, "polishing the wording") in seen
    assert seen[-1] == (2, 2, "")


def test_the_editing_pass_is_optional_for_the_status(monkeypatch):
    FakeNet(monkeypatch)
    monkeypatch.setenv("GROQ_POLISH", "0")
    assert structure.groq_status()["polish"] is False
    monkeypatch.setenv("GROQ_POLISH", "1")
    assert structure.groq_status()["polish"] is True
