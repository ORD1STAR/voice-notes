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
