import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not available")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Every test gets its own data folder and no real network services."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    monkeypatch.setenv("USE_OLLAMA", "0")
    # Structuring through Groq is switched off by default and never reaches the real network in tests.
    # Tests of that path turn it on and replace the HTTP calls.
    monkeypatch.setenv("USE_GROQ_CHAT", "0")
    # The final editing pass is one more chat call; the tests that count calls leave it off, the tests of the
    # pass itself turn it on.
    monkeypatch.setenv("GROQ_POLISH", "0")
    monkeypatch.setenv("GROQ_CHAT_URL", "http://127.0.0.1:9/openai/v1/chat/completions")
    monkeypatch.setenv("GROQ_MODELS_URL", "http://127.0.0.1:9/openai/v1/models")
    monkeypatch.delenv("GROQ_CHAT_MODEL", raising=False)
    monkeypatch.delenv("TRANSCRIPTION_LANGUAGE", raising=False)
    monkeypatch.delenv("MAX_UPLOAD_MB", raising=False)
    monkeypatch.delenv("CHUNK_SECONDS", raising=False)
    if FFMPEG:
        monkeypatch.setenv("FFMPEG_BIN", FFMPEG)
    from app import structure

    structure.reset_groq_cache()
    return tmp_path


@pytest.fixture
def make_audio(tmp_path):
    """Create a sine wave file of the given length in seconds."""

    def make(seconds: float, name: str = "tone.wav") -> Path:
        path = tmp_path / name
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", str(path)],
            check=True,
        )
        return path

    return make


SAMPLE_TEXT = (
    "Today we planned the launch of the new website. The design team will finish the landing page by Friday. "
    "Marketing needs the final copy before the newsletter goes out. We also talked about the budget. "
    "The budget for ads stays the same as last month. Next week we will review the analytics together. "
    "Sarah will prepare the report for the analytics review. Finally we agreed to meet every Monday morning. "
    "The Monday meeting should stay short and focused on blockers."
)
