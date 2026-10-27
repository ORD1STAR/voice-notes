"""The review pass, the final call for long notes, the call budget and the prompt and check improvements.

The network is never used: Groq is replaced by the fake of test_groq_structuring, and nothing really sleeps.
"""
import json

import httpx
import pytest

from app import structure
from conftest import SAMPLE_TEXT
from test_groq_structuring import FR_NOTES, FR_TRANSCRIPT, FakeNet, status

MODEL = "groq:llama-3.3-70b-versatile"

PIECE_A = ("The design team will finish the landing page by Friday. Marketing still needs the final copy before "
           "the newsletter goes out. The ad budget stays at 5000 euros for the quarter.")
PIECE_B = ("Sarah will prepare the analytics report before next Tuesday. We also agreed to meet every Monday "
           "morning and keep the meeting short.")
PIECE_C = "Tom will book the room for the first session."
LIMIT = len(PIECE_A)  # PIECE_A just fits; PIECE_B and PIECE_C together do not
TWO = f"{PIECE_A} {PIECE_B}"
THREE = f"{PIECE_A} {PIECE_B} {PIECE_C}"


def notes(title, summary, sections, key_points=(), actions=()):
    return {"title": title, "summary": summary, "key_points": list(key_points),
            "sections": [{"heading": h, "bullets": list(b)} for h, b in sections], "action_items": list(actions)}


DRAFTS = {
    "A": notes("Website launch", "The landing page and the newsletter copy are the open items for the launch.",
               [("Landing page", ["The design team will finish the landing page by Friday.",
                                  "Marketing still needs the final copy for the newsletter."])],
               ["The landing page is due on Friday."]),
    "B": notes("Reporting and meetings", "Sarah prepares the analytics report and the team meets weekly.",
               [("Analytics report", ["Sarah will prepare the analytics report before next Tuesday."]),
                ("Weekly meeting", ["The team meets every Monday morning."])],
               [], ["Sarah: prepare the analytics report before next Tuesday."]),
    "C": notes("Session planning", "Tom books the room for the first session.",
               [("Room booking", ["Tom books the room for the first session."])]),
}
REVIEWS = {
    "A": notes("Website launch", "The landing page is due on Friday, the newsletter copy is missing and the ad budget "
                                 "stays at 5000 euros.",
               [("Launch status", ["The design team will finish the landing page by Friday.",
                                   "Marketing still needs the final copy before the newsletter goes out."]),
                ("Ad budget", ["The ad budget stays at 5000 euros for the quarter."])],
               ["The landing page is due on Friday.", "The ad budget stays at 5000 euros for the quarter."]),
    "B": notes("Reporting and meetings", "Sarah prepares the analytics report and the team meets every Monday.",
               [("Analytics report", ["Sarah will prepare the analytics report before next Tuesday."]),
                ("Weekly meeting", ["The team meets every Monday morning.", "The meeting stays short."])],
               [], ["Sarah: prepare the analytics report before next Tuesday."]),
    "C": notes("Session planning", "Tom books the room for the first session.",
               [("Room booking", ["Tom will book the room for the first session."])], [],
               ["Tom: book the room for the first session."]),
}
HEAD = {"title": "Website launch plan",
        "summary": "The note covers the landing page, the budget, the analytics report and the weekly meeting.",
        "key_points": ["The landing page is due on Friday.", "The ad budget stays at 5000 euros for the quarter.",
                       "Sarah prepares the analytics report before next Tuesday."],
        "sections": [], "action_items": []}


def kind(req):
    system = req["messages"][0]["content"]
    if "editor of notes" in system:
        return "review"
    return "head" if "consecutive parts" in system else "draft"


def piece_of(req):
    return req["messages"][1]["content"].split("Transcript:\n", 1)[1].split("\n\nDraft notes", 1)[0]


def key_of(piece):
    return "A" if "design team" in piece else "B" if "Sarah" in piece else "C"


class Router:
    """Answers each Groq request according to what it is: a draft, a review or the final call."""

    def __init__(self, drafts=None, reviews=None, head=None):
        self.drafts, self.reviews, self.head = dict(drafts or DRAFTS), dict(reviews or REVIEWS), head or HEAD
        self.log: list[tuple[str, str]] = []

    def __call__(self, req):
        k = kind(req)
        if k == "head":
            self.log.append(("head", ""))
            return self.head
        key = key_of(piece_of(req))
        self.log.append((k, key))
        answer = (self.drafts if k == "draft" else self.reviews)[key]
        return answer(req) if callable(answer) else answer


@pytest.fixture
def small_pieces(monkeypatch):
    monkeypatch.setattr(structure, "GROQ_PIECE_CHARS", LIMIT)
    assert structure.split_pieces(THREE, LIMIT) == [PIECE_A, PIECE_B, PIECE_C]
    assert structure.split_pieces(TWO, LIMIT) == [PIECE_A, PIECE_B]


def run(monkeypatch, text, router=None, review=True, language="auto", **kw):
    router = router or Router()
    net = FakeNet(monkeypatch, chat=router, review=review)
    result, engine = structure.structure_transcript(text, language=language, **kw)
    return result, engine, net, router


def bullets_of(result):
    return [b for s in result["sections"] for b in s["bullets"]]


# ----------------------------------------------------------- the review improves the notes

def test_a_review_restores_what_the_draft_left_out(monkeypatch):
    result, engine, net, router = run(monkeypatch, PIECE_A)
    assert engine == MODEL and router.log == [("draft", "A"), ("review", "A")]
    assert "The ad budget stays at 5000 euros for the quarter." in bullets_of(result)  # missing from the draft
    assert [s["heading"] for s in result["sections"]] == ["Launch status", "Ad budget"]
    assert "The ad budget stays at 5000 euros for the quarter." in result["key_points"]


def test_the_review_request_carries_the_transcript_the_draft_and_the_rules(monkeypatch):
    _, _, net, _ = run(monkeypatch, PIECE_A)
    review = net.chat_calls[1]
    system, user = (m["content"] for m in review["messages"])
    for phrase in ("Restore the important facts, decisions, names, figures and deadlines", "wrong attributions",
                   "Split a bullet that carries two ideas", "Remove duplicates, filler", "1 to 5 words",
                   "never generic", "never guess either", "Keep the order of the speaker", "Never invent anything",
                   "language of the recording", "Do not translate", "same keys as the draft"):
        assert phrase in system
    assert review["response_format"] == {"type": "json_object"}
    assert "Transcript:\n" + PIECE_A in user and "Detect the language" in user
    draft = json.loads(user.split("Draft notes (JSON):\n", 1)[1])
    assert draft["title"] == "Website launch" and "action_items" in draft
    assert draft["sections"][0]["bullets"][0] == "The design team will finish the landing page by Friday."


def test_the_review_follows_the_chosen_language(monkeypatch):
    net = FakeNet(monkeypatch, chat=lambda req: FR_NOTES, review=True)
    structure.structure_transcript(FR_TRANSCRIPT, language="fr")
    assert len(net.chat_calls) == 2
    system, user = (m["content"] for m in net.chat_calls[1]["messages"])
    assert "The recording is in French. Write all the notes in French. Do not translate." in system
    assert "Language: French." in user


def test_the_review_is_on_by_default_and_the_status_says_so(monkeypatch):
    FakeNet(monkeypatch)
    monkeypatch.delenv("GROQ_REVIEW", raising=False)
    assert structure.settings.groq_review is True
    assert structure.groq_status()["review"] is True and structure.engine_plan()["review"] is True
    for off in ("0", "false", "No", "OFF"):
        monkeypatch.setenv("GROQ_REVIEW", off)
        assert structure.settings.groq_review is False
    monkeypatch.setenv("GROQ_REVIEW", "1")
    assert structure.settings.groq_review is True  # the setting follows the environment at once


# ------------------------------------------------------------- a bad review is not used

def test_a_review_with_an_invented_number_loses_the_invented_part(monkeypatch):
    bad = dict(REVIEWS["A"])
    bad["sections"] = REVIEWS["A"]["sections"] + [{"heading": "Costs", "bullets": ["The ad budget grows to 9000 euros."]}]
    result, engine, _, router = run(monkeypatch, PIECE_A, Router(reviews={**REVIEWS, "A": bad}))
    assert engine == MODEL and len(router.log) == 2
    assert "9000" not in json.dumps(result)
    assert "The ad budget stays at 5000 euros for the quarter." in bullets_of(result)


def test_a_review_that_mostly_invents_is_dropped_and_the_draft_is_kept(monkeypatch):
    bad = notes("Website launch", "The landing page is due on Friday.",
                [("Launch", ["The design team will finish the landing page by Friday.",
                             "Marketing still needs the final copy before the newsletter goes out.",
                             "Revenue rose by 9000 euros in Berlin.", "The budget grows to 7500 euros."])])
    result, engine, _, router = run(monkeypatch, PIECE_A, Router(reviews={**REVIEWS, "A": bad}))
    assert engine == MODEL and len(router.log) == 2
    assert bullets_of(result) == DRAFTS["A"]["sections"][0]["bullets"]
    assert "9000" not in json.dumps(result) and "7500" not in json.dumps(result)


def test_a_review_with_an_invented_name_is_not_trusted_either(monkeypatch):
    bad = notes("Website launch", "The landing page is due on Friday.",
                [("Launch", ["The design team will finish the landing page by Friday."]),
                 ("People", ["Marco leads the Munich office.", "Elena owns the newsletter in Paris."])])
    result, _, _, _ = run(monkeypatch, PIECE_A, Router(reviews={**REVIEWS, "A": bad}))
    assert "Marco" not in json.dumps(result) and "Elena" not in json.dumps(result)


THREE_BULLETS = notes("Website launch", "The landing page, the copy and the budget are open.",
                      [("Launch", ["The design team will finish the landing page by Friday.",
                                   "Marketing still needs the final copy for the newsletter.",
                                   "The ad budget stays at 5000 euros for the quarter."])])


@pytest.mark.parametrize("review", [
    # lost more than a third of the bullets
    notes("Website launch", "The landing page is due on Friday.", [("Launch", ["The landing page is due on Friday."])]),
    # the figure of the draft is gone
    notes("Website launch", "The landing page is due on Friday.",
          [("Launch", ["The design team will finish the landing page by Friday.",
                       "Marketing still needs the final copy before the newsletter goes out.",
                       "The newsletter goes out after the landing page."])]),
])
def test_a_review_that_is_clearly_worse_is_ignored(monkeypatch, review):
    result, engine, _, router = run(monkeypatch, PIECE_A, Router(drafts={**DRAFTS, "A": THREE_BULLETS},
                                                                  reviews={**REVIEWS, "A": review}))
    assert engine == MODEL and len(router.log) == 2
    assert bullets_of(result) == THREE_BULLETS["sections"][0]["bullets"]


def test_a_review_that_drops_all_the_tasks_is_ignored(monkeypatch):
    draft = dict(DRAFTS["A"], action_items=["Finish the landing page by Friday."])
    review = dict(REVIEWS["A"], action_items=[])
    result, _, _, _ = run(monkeypatch, PIECE_A, Router(drafts={**DRAFTS, "A": draft}, reviews={**REVIEWS, "A": review}))
    assert result["actions"] == ["Finish the landing page by Friday."]
    assert "The ad budget stays at 5000 euros for the quarter." not in bullets_of(result)


def test_a_review_that_is_equal_or_a_little_longer_is_used(monkeypatch):
    result, _, _, _ = run(monkeypatch, PIECE_B)
    assert "The meeting stays short." in bullets_of(result)


# ------------------------------------------------------- a failing review never fails the note

@pytest.mark.parametrize("answer, calls", [
    (httpx.ReadTimeout("slow"), 2), (httpx.ConnectError("down"), 2), (status(500), 4), (status(429), 4),
    (status(401), 2), (status(400), 2), ("this is not json", 2), ("[1, 2]", 2),
    ({"title": "x", "summary": "y", "sections": []}, 2),
])
def test_a_failing_review_keeps_the_draft_and_the_note_completes(monkeypatch, answer, calls):
    result, engine, net, router = run(monkeypatch, PIECE_A, Router(reviews={**REVIEWS, "A": answer}))
    assert engine == MODEL  # not "partly rule-based": the draft is the model's own notes
    assert len(net.chat_calls) == calls
    assert result["title"] == "Website launch"
    assert bullets_of(result) == DRAFTS["A"]["sections"][0]["bullets"]


def test_a_long_wait_is_capped_for_the_draft_and_not_waited_for_in_the_review(monkeypatch):
    slow = httpx.Response(429, headers={"retry-after": "120"}, json={"error": {"message": "rate limit"}},
                          request=httpx.Request("POST", "http://groq.test/openai/v1/chat/completions"))
    answers = iter([slow, DRAFTS["A"], slow])
    net = FakeNet(monkeypatch, chat=lambda req: next(answers), review=True)
    sleeps = []
    monkeypatch.setattr(structure.time, "sleep", sleeps.append)
    result, engine = structure.structure_transcript(PIECE_A)
    assert engine == MODEL and len(net.chat_calls) == 3
    assert sleeps == [20.0]  # the draft waited (at most 20 s); the review gave up at once and kept the draft
    assert bullets_of(result) == DRAFTS["A"]["sections"][0]["bullets"]


def test_waits_are_read_from_the_header_or_from_the_message():
    def resp(headers=None, text=""):
        return httpx.Response(429, headers=headers or {}, text=text, request=httpx.Request("POST", "http://x.test"))

    assert structure._retry_wait(resp({"retry-after": "7"}), 0) == 7.0
    assert structure._retry_wait(resp(text="Rate limit reached. Please try again in 7.5s."), 0) == 7.5
    assert structure._retry_wait(resp(text="Please try again in 1m5.5s."), 0) == 65.5
    assert structure._retry_wait(resp(text="Please try again in 340ms."), 1) == pytest.approx(0.34)
    assert structure._retry_wait(resp(), 1) == 4.0
    assert structure._retry_after(resp({"retry-after": "120"}), 0) == 20.0


def test_a_wait_in_the_message_is_used_for_the_retry(monkeypatch):
    limited = httpx.Response(429, text='{"error": {"message": "Please try again in 3.2s."}}',
                             request=httpx.Request("POST", "http://groq.test"))
    answers = iter([limited, DRAFTS["A"]])
    FakeNet(monkeypatch, chat=lambda req: next(answers), review=False)
    sleeps = []
    monkeypatch.setattr(structure.time, "sleep", sleeps.append)
    _, engine = structure.structure_transcript(PIECE_A)
    assert engine == MODEL and sleeps == [3.2]


def test_max_tokens_can_be_set_and_stays_in_a_safe_range(monkeypatch):
    net = FakeNet(monkeypatch, chat=DRAFTS["A"])
    structure.structure_transcript(PIECE_A)
    assert net.chat_calls[0]["max_tokens"] == 4096
    for value, expected in (("2000", 2000), ("10", 512), ("999999", 8192), ("abc", 4096), ("", 4096)):
        monkeypatch.setenv("GROQ_CHAT_MAX_TOKENS", value)
        assert structure.settings.groq_chat_max_tokens == expected
    monkeypatch.setenv("GROQ_CHAT_MAX_TOKENS", "2000")
    structure.structure_transcript(PIECE_A)
    assert net.chat_calls[-1]["max_tokens"] == 2000


# ------------------------------------------------------------- the number of calls

def test_without_the_review_there_is_one_call_per_part(monkeypatch, small_pieces):
    result, engine, net, router = run(monkeypatch, TWO, review=False)
    assert engine == MODEL
    assert router.log == [("draft", "A"), ("draft", "B"), ("head", "")]
    assert len(net.chat_calls) == 3


def test_with_the_review_there_are_two_calls_per_part_and_one_final_call(monkeypatch, small_pieces):
    result, engine, net, router = run(monkeypatch, TWO)
    assert router.log == [("draft", "A"), ("review", "A"), ("draft", "B"), ("review", "B"), ("head", "")]
    assert "The meeting stays short." in bullets_of(result) and "Ad budget" in [s["heading"] for s in result["sections"]]


def test_a_single_part_gets_no_extra_final_call(monkeypatch):
    _, _, net, router = run(monkeypatch, PIECE_A)
    assert [k for k, _ in router.log] == ["draft", "review"]


# ------------------------------------------------------ the final call for long notes

def test_the_final_call_sees_the_headings_and_the_key_points_and_chooses(monkeypatch, small_pieces):
    result, engine, net, router = run(monkeypatch, TWO)
    system, user = (m["content"] for m in net.chat_calls[-1]["messages"])
    assert user.startswith("Part 1: ") and "\nPart 2: " in user
    assert "Section headings:\n- Launch status\n- Ad budget\n- Analytics report\n- Weekly meeting" in user
    assert "Candidate key points:\n- The landing page is due on Friday.\n- The ad budget stays at 5000 euros" in user
    for phrase in ("3 to 5 sentences", "2 to 5 most important", "chosen among the candidates or condensed",
                   "never add facts"):
        assert phrase in system
    assert result["title"] == "Website launch plan" and result["summary"] == HEAD["summary"]
    assert result["key_points"] == HEAD["key_points"]  # a third point that condenses a piece was accepted
    assert len(result["actions"]) == 1 and result["actions"][0].startswith("Sarah")


def test_key_points_with_invented_figures_or_names_are_not_taken(monkeypatch, small_pieces):
    head = dict(HEAD, key_points=["Revenue grows 40 percent next year.", "Tom leads the Berlin office."])
    result, _, _, _ = run(monkeypatch, TWO, Router(head=head))
    assert result["key_points"] == REVIEWS["A"]["key_points"]  # the key points of the parts


def test_a_single_good_key_point_does_not_replace_the_candidates(monkeypatch, small_pieces):
    head = dict(HEAD, key_points=["The landing page is due on Friday.", "Revenue grows 40 percent next year."])
    result, _, _, _ = run(monkeypatch, TWO, Router(head=head))
    assert result["key_points"] == REVIEWS["A"]["key_points"]


def test_an_invented_summary_is_replaced_by_the_summaries_of_the_parts(monkeypatch, small_pieces):
    head = dict(HEAD, summary="Sales rose by 40 percent in Berlin. Everyone was happy.")
    result, _, _, _ = run(monkeypatch, TWO, Router(head=head))
    assert "40" not in result["summary"] and "Berlin" not in result["summary"]
    assert result["summary"].startswith("The landing page is due on Friday")
    assert "Sarah prepares the analytics report" in result["summary"]


def test_when_the_final_call_fails_the_part_summaries_make_the_summary(monkeypatch, small_pieces):
    router = Router()
    original = router.__call__

    def answer(req):
        return status(500) if kind(req) == "head" else original(req)

    result, engine, _, _ = run(monkeypatch, TWO, answer)
    assert engine == MODEL and result["title"] and result["title"].lower() not in structure.GENERIC_TITLES
    assert result["summary"].startswith("The landing page is due on Friday")
    assert result["key_points"] == REVIEWS["A"]["key_points"]


# ------------------------------------------------------------- the streak is not touched

def test_a_failed_review_does_not_count_toward_the_two_failure_streak(monkeypatch, small_pieces):
    router = Router(drafts={**DRAFTS, "B": httpx.ConnectError("down")}, reviews={**REVIEWS, "A": httpx.ConnectError("down")})
    result, engine, net, _ = run(monkeypatch, THREE, router)
    # review of A fails, draft of B fails: one failure only, so the model is still used for C (and its review)
    assert router.log == [("draft", "A"), ("review", "A"), ("draft", "B"), ("draft", "C"), ("review", "C"), ("head", "")]
    assert engine == MODEL + " (partly rule-based)"
    assert any("Tom" in b for b in bullets_of(result))


def test_two_reviews_in_a_row_that_fail_stop_the_reviews_but_not_the_model(monkeypatch, small_pieces):
    down = httpx.ConnectError("down")
    router = Router(reviews={**REVIEWS, "A": down, "B": down})
    result, engine, _, _ = run(monkeypatch, THREE, router)
    assert router.log == [("draft", "A"), ("review", "A"), ("draft", "B"), ("review", "B"), ("draft", "C"), ("head", "")]
    assert engine == MODEL  # all three parts were written by the model


def test_a_good_review_in_between_resets_the_review_failures(monkeypatch, small_pieces):
    down = httpx.ConnectError("down")
    router = Router(reviews={**REVIEWS, "A": down, "C": down})
    run(monkeypatch, THREE, router)
    assert [k for k, _ in router.log].count("review") == 3


def test_two_failed_drafts_in_a_row_still_switch_the_model_off(monkeypatch, small_pieces):
    down = httpx.ConnectError("down")
    router = Router(drafts={**DRAFTS, "A": down, "B": down})
    _, engine, net, _ = run(monkeypatch, THREE, router)
    assert router.log == [("draft", "A"), ("draft", "B")]  # no third attempt, no review, no final call
    assert engine == "rule-based"


# ------------------------------------------------------------------------- progress

def test_progress_counts_two_steps_per_part_with_a_review(monkeypatch, small_pieces):
    seen = []
    run(monkeypatch, TWO, on_progress=lambda done, total, step="": seen.append((done, total, step)))
    assert seen == [(0, 5, "writing part 1 of 2"), (1, 5, "checking part 1 of 2"), (2, 5, "writing part 2 of 2"),
                    (3, 5, "checking part 2 of 2"), (4, 5, "combining the parts"), (5, 5, "")]


def test_progress_without_the_review_keeps_one_step_per_part(monkeypatch, small_pieces):
    seen = []
    run(monkeypatch, TWO, review=False, on_progress=lambda done, total, step="": seen.append((done, total, step)))
    assert seen == [(0, 2, "part 1 of 2"), (1, 2, "part 2 of 2"), (2, 2, "")]


def test_a_progress_callback_with_two_arguments_still_works(monkeypatch, small_pieces):
    seen = []
    run(monkeypatch, TWO, on_progress=lambda done, total: seen.append((done, total)))
    assert seen[0] == (0, 5) and seen[-1] == (5, 5) and len(seen) == 6
    assert all(a <= b for (a, _), (b, _) in zip(seen, seen[1:]))


def test_a_single_part_with_a_review_reports_two_steps(monkeypatch):
    seen = []
    run(monkeypatch, PIECE_A, on_progress=lambda done, total, step="": seen.append((done, total, step)))
    assert seen == [(0, 2, "writing part 1 of 1"), (1, 2, "checking part 1 of 1"), (2, 2, "")]


def test_the_job_shows_writing_and_checking(monkeypatch):
    from fastapi.testclient import TestClient

    from app import jobs
    from app.main import app

    details = []
    plan = {"engine": MODEL, "model": "llama-3.3-70b-versatile", "reason": "", "backup": {"engine": "rule-based"}}
    with TestClient(app):
        job = jobs.new_job("x.wav")
        jobs._update(job["id"], transcript=SAMPLE_TEXT)

        def fake(text, on_progress=None, language="auto", report=None):
            for args in [(0, 5, "writing part 1 of 2"), (1, 5, "checking part 1 of 2"), (4, 5, "combining the parts"),
                         (5, 5, ""), (0, 2)]:
                on_progress(*args)
                details.append((jobs.get(job["id"])["detail"], jobs.get(job["id"])["progress"]))
            return structure.structure_rules(text), "rule-based"

        monkeypatch.setattr(structure, "engine_plan", lambda: plan)
        monkeypatch.setattr(structure, "structure_transcript", fake)
        jobs.run(job["id"])
    who = "Groq model llama-3.3-70b-versatile"
    assert [d for d, _ in details] == [f"{who}: writing part 1 of 2", f"{who}: checking part 1 of 2",
                                       f"{who}: combining the parts", "Structuring the notes", f"{who}: part 1 of 2"]
    progress = [p for _, p in details]
    assert progress[:4] == sorted(progress[:4]) and progress[0] == pytest.approx(0.72) and progress[3] == pytest.approx(0.98)


# ------------------------------------------------------- the local model is left alone

def test_the_local_model_stays_single_pass_even_when_the_review_is_on(monkeypatch):
    local = {"title": "Launch plan", "summary": "The landing page is due on Friday.",
             "sections": [{"heading": "Website", "bullets": ["Finish the landing page by Friday."]}]}
    net = FakeNet(monkeypatch, chat=httpx.ConnectError("down"), ollama=local, review=True)
    monkeypatch.setattr(structure, "MAX_PIECE_CHARS", LIMIT)
    count = {"n": 0}
    original = structure.httpx.post

    def counting(url, **kw):
        if url.endswith("/api/chat"):
            count["n"] += 1
        return original(url, **kw)

    monkeypatch.setattr(structure.httpx, "post", counting)
    _, engine = structure.structure_transcript(TWO)
    assert engine.startswith("ollama:tiny:1b")
    assert count["n"] == 3  # two parts and the title and summary: no review calls
    assert net.chat_calls and all(kind(c) != "review" for c in net.chat_calls)


# ------------------------------------------------------------------------ language

EN_NOTES = notes(
    "Refund procedure",
    "The old refund procedure checks the file first, then Karim validates the request and the accounting team "
    "makes the transfer. The problem is that the files are often incomplete.",
    [("Refund steps", ["The file of the client is checked first.", "Karim validates the request within two days.",
                       "The accounting team makes the transfer in about five days."])],
    ["Karim has two days to validate the request."], ["Sophie: prepare a mock-up of the form before Friday."])


def test_notes_in_english_for_a_french_recording_are_recognised():
    assert structure._language_drift(EN_NOTES, "fr", FR_TRANSCRIPT) is True
    assert structure._language_drift(EN_NOTES, "auto", FR_TRANSCRIPT) is False
    assert structure._language_drift(EN_NOTES, "en", FR_TRANSCRIPT) is False
    assert structure._language_drift(FR_NOTES, "fr", FR_TRANSCRIPT) is False
    # an English recording that was told to be French is not the model's fault
    assert structure._language_drift(EN_NOTES, "fr", SAMPLE_TEXT + " " + SAMPLE_TEXT) is False


def test_arabic_and_chinese_notes_must_use_their_own_script():
    ar_src = "ناقشنا اليوم ميزانية المشروع الجديد وقررنا أن يرسل أحمد التقرير يوم الخميس القادم إلى الفريق كله."
    ar_ok = notes("ميزانية المشروع", "ناقش الفريق ميزانية المشروع الجديد.",
                  [("الميزانية", ["سيرسل أحمد التقرير يوم الخميس القادم إلى الفريق."])])
    assert structure._language_drift(ar_ok, "ar", ar_src) is False
    assert structure._language_drift(EN_NOTES, "ar", ar_src) is True
    zh_src = "今天我们讨论了新网站的发布计划。设计团队会在星期五之前完成首页。预算保持不变。"
    assert structure._language_drift(EN_NOTES, "zh", zh_src) is True


def test_an_answer_in_the_wrong_language_is_asked_again_once(monkeypatch):
    answers = iter([EN_NOTES, FR_NOTES])
    net = FakeNet(monkeypatch, chat=lambda req: next(answers))
    result, engine = structure.structure_transcript(FR_TRANSCRIPT, language="fr")
    assert engine.startswith("groq:") and len(net.chat_calls) == 2
    assert result["title"] == "Procédure de remboursement"
    assert "write every word of the notes in French" in net.chat_calls[1]["messages"][1]["content"]


def test_an_answer_that_stays_in_the_wrong_language_falls_back(monkeypatch):
    net = FakeNet(monkeypatch, chat=EN_NOTES)
    report = {}
    _, engine = structure.structure_transcript(FR_TRANSCRIPT, language="fr", report=report)
    assert engine == "rule-based" and "not written in French" in report["fallback_reason"]
    assert len(net.chat_calls) == 2  # the answer and one more try


def test_the_prompts_ask_for_neutral_complete_notes_in_the_chosen_language():
    for lang, name in (("fr", "French"), ("ar", "Arabic"), ("auto", None)):
        prompt = structure.groq_system_prompt(lang)
        review = structure.groq_review_prompt(lang)
        for text in (prompt, review):
            assert "even though these instructions are in English" in text
            assert "Do not translate" in text
            if name:
                assert f"Write all the notes in {name}." in text
        assert "{" not in prompt and "}" not in prompt and "{" not in review
    prompt = structure.groq_system_prompt()
    for phrase in ("never drop a decision", "no \"the speaker says\"", "\"probably\" or \"possibly\"",
                   "say who decided", "never a fragment", "keep relative dates", "never guess either",
                   "\"Overview\""):
        assert phrase in prompt
    assert "I think" in prompt  # named as something to avoid
    head = structure.groq_head_prompt("fr")
    assert "French" in head and "3 to 5 sentences" in head and "key_points" in head


# ------------------------------------------------------------ checks on the model output

SRC = ("We met on Monday with Tom. The budget is 5000 euros and the launch is in twenty days. "
       "Anna will send the report.")


def raw(sections, **extra):
    base = {"title": "Launch budget meeting", "summary": "The team met with Tom to discuss the budget of 5000 euros.",
            "key_points": [], "sections": sections, "actions": []}
    base.update(extra)
    return base


def test_generic_headings_are_replaced_in_several_languages():
    bullets = ["The budget is 5000 euros."]
    for heading in ("Part 2", "Section 3", "Divers", "Détails", "Resumen", "Allgemein", "ملاحظات", "Other.", ""):
        out = structure.refine_model_notes(raw([{"heading": heading, "bullets": bullets}]), SRC, relaxed=True)
        assert out["sections"][0]["heading"].lower() not in structure.GENERIC_HEADINGS
        assert out["sections"][0]["heading"] and not out["sections"][0]["heading"].lower().startswith(("part ", "section "))
    keep = structure.refine_model_notes(raw([{"heading": "Budget", "bullets": bullets}]), SRC, relaxed=True)
    assert keep["sections"][0]["heading"] == "Budget"


def test_one_word_bullets_are_dropped_from_strong_model_notes():
    out = structure.refine_model_notes(
        raw([{"heading": "Budget", "bullets": ["Budget.", "The budget is 5000 euros.", "Report"]}]), SRC, relaxed=True)
    assert bullets_of(out) == ["The budget is 5000 euros."]


def test_a_model_that_sends_text_where_a_list_is_expected_does_not_break_the_notes():
    out = structure.normalize_notes({
        "title": "T", "summary": "S", "key_points": "The budget is 5000 euros.",
        "sections": {"heading": "Budget", "bullets": "- The budget is 5000 euros.\n- Anna sends the report."},
        "actions": [{"text": "Send the report"}, None, ""]})
    assert out["sections"] == [{"heading": "Budget", "bullets": ["The budget is 5000 euros.", "Anna sends the report."]}]
    assert out["key_points"] == ["The budget is 5000 euros."] and out["actions"] == ["Send the report"]
    objects = structure.normalize_notes({"sections": [{"title": "Budget", "bullets": [{"text": "A point."}, {"bullet": "Two."}]}]})
    assert objects["sections"] == [{"heading": "Budget", "bullets": ["A point.", "Two."]}]
    with pytest.raises(structure.StructureError):
        structure.normalize_notes({"sections": [{"heading": "x", "bullets": []}]})


def test_merging_parts_keeps_the_bullets_of_a_repeated_heading(monkeypatch, small_pieces):
    first = notes("Launch", "The launch of the website.",
                  [("Launch", ["The design team will finish the landing page by Friday.",
                               "Marketing still needs the final copy before the newsletter goes out."])])
    second = notes("Launch again", "Sarah prepares the report.",
                   [("Launch", ["Sarah will prepare the analytics report before next Tuesday.",
                                "The team agreed to meet every Monday morning."])])
    result, _, _, _ = run(monkeypatch, TWO, Router(drafts={**DRAFTS, "A": first, "B": second}), review=False)
    launch = [s for s in result["sections"] if s["heading"] == "Launch"]
    assert len(launch) == 1 and len(launch[0]["bullets"]) == 4
