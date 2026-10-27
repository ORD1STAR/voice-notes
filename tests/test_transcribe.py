import httpx
import pytest

from app import transcribe


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def chunk(tmp_path):
    p = tmp_path / "c.mp3"
    p.write_bytes(b"fake audio bytes")
    return p


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(transcribe.time, "sleep", lambda s: None)


def test_join_transcripts_cleans_and_skips_empty():
    assert transcribe.join_transcripts([" Hello  there. ", "", "General\nKenobi."]) == "Hello there. General Kenobi."


def test_transcribe_file_sends_key_and_model(chunk):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": " hello world "})

    assert transcribe.transcribe_file(chunk, client=make_client(handler)) == "hello world"
    assert seen["auth"] == "Bearer test-key-not-real"
    assert b"whisper-large-v3-turbo" in seen["body"]


def test_transcribe_file_retries_on_rate_limit(chunk):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "1"}, json={})
        return httpx.Response(200, json={"text": "ok"})

    assert transcribe.transcribe_file(chunk, client=make_client(handler)) == "ok"
    assert len(calls) == 3


def test_transcribe_file_rejected_key(chunk):
    client = make_client(lambda r: httpx.Response(401, json={}))
    with pytest.raises(transcribe.TranscriptionError, match="GROQ_API_KEY"):
        transcribe.transcribe_file(chunk, client=client)


def test_transcribe_file_gives_up_after_retries(chunk):
    client = make_client(lambda r: httpx.Response(429, json={}))
    with pytest.raises(transcribe.TranscriptionError):
        transcribe.transcribe_file(chunk, client=client, retries=1)


def test_transcribe_file_without_key(chunk, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    with pytest.raises(transcribe.TranscriptionError, match="GROQ_API_KEY"):
        transcribe.transcribe_file(chunk)


def test_transcribe_file_network_error(chunk):
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(transcribe.TranscriptionError):
        transcribe.transcribe_file(chunk, client=make_client(handler), retries=1)


def test_transcribe_chunks_joins_in_order_and_reports_progress(tmp_path):
    files = []
    for i in range(3):
        p = tmp_path / f"c{i}.mp3"
        p.write_bytes(b"x")
        files.append(p)
    texts = iter(["one", "two", "three"])
    client = make_client(lambda r: httpx.Response(200, json={"text": next(texts)}))
    progress = []
    out = transcribe.transcribe_chunks(files, lambda d, t: progress.append((d, t)), client=client)
    assert out == "one two three"
    assert progress[0] == (0, 3) and progress[-1] == (3, 3)


# ------------------------------------------------ joining at a chunk boundary

def test_merge_overlap_removes_the_repeated_words():
    a = "We will finish the landing page by"
    b = "landing page by Friday and then send the copy."
    assert transcribe.merge_overlap(a, b) == "Friday and then send the copy."


def test_merge_overlap_is_fuzzy_about_punctuation_and_case():
    a = "the newsletter goes out. Marketing needs the final copy, before"
    b = "Final copy before the end of the week."
    assert transcribe.merge_overlap(a, b) == "the end of the week."


def test_merge_overlap_handles_a_word_that_was_cut():
    # The first pass heard "landi", the second heard the whole word.
    a = "Design will finish the landing page, then"
    b = "page, then the copy arrives."
    assert transcribe.merge_overlap(a, b) == "the copy arrives."


def test_merge_overlap_leaves_unrelated_text_alone():
    a = "That was the first part of the story."
    b = "Now the second part begins with something new."
    assert transcribe.merge_overlap(a, b) == b


def test_merge_overlap_ignores_a_match_far_from_the_seam():
    a = "the budget stays the same and we move on to other things entirely now"
    b = "something else then the budget stays the same later"
    assert transcribe.merge_overlap(a, b) == b


def test_join_removes_repeats_only_where_the_chunk_overlapped():
    parts = ["We will finish the landing page by", "landing page by Friday.", "Friday. Then we rest."]
    # The third chunk did not repeat anything, so "Friday." there is real speech and stays.
    assert transcribe.join_transcripts(parts, [0, 2.0, 0.0]) == "We will finish the landing page by Friday. Friday. Then we rest."
    # No overlap information: nothing is touched.
    assert transcribe.join_transcripts(parts) == "We will finish the landing page by landing page by Friday. Friday. Then we rest."


def test_transcribe_chunks_passes_overlaps(tmp_path):
    files = []
    for i in range(2):
        p = tmp_path / f"c{i}.mp3"
        p.write_bytes(b"x")
        files.append(p)
    texts = iter(["we need to call Anna about the", "call Anna about the garden"])
    client = make_client(lambda r: httpx.Response(200, json={"text": next(texts)}))
    out = transcribe.transcribe_chunks(files, client=client, overlaps=[0, 2.0])
    assert out == "we need to call Anna about the garden"
