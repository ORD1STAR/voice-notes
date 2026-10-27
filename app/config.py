"""Runtime settings, all read from environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# A 70B-class instruction model on Groq. It follows a JSON format reliably and writes well in the
# languages offered on the start page.
DEFAULT_GROQ_CHAT_MODEL = "llama-3.3-70b-versatile"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Settings:
    """Settings are resolved when accessed so tests can change the environment."""

    @property
    def groq_api_key(self) -> str:
        return os.environ.get("GROQ_API_KEY", "").strip()

    @property
    def groq_url(self) -> str:
        return os.environ.get(
            "GROQ_API_URL", "https://api.groq.com/openai/v1/audio/transcriptions"
        )

    @property
    def groq_model(self) -> str:
        return os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")

    @property
    def language(self) -> str:
        """Optional ISO language code. Empty means automatic detection."""
        return os.environ.get("TRANSCRIPTION_LANGUAGE", "").strip()

    @property
    def ollama_host(self) -> str:
        return os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434").rstrip("/")

    @property
    def ollama_model(self) -> str:
        return os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

    @property
    def use_ollama(self) -> bool:
        return os.environ.get("USE_OLLAMA", "1").lower() not in ("0", "false", "no", "off")

    @property
    def ollama_timeout(self) -> int:
        return _int("OLLAMA_TIMEOUT", 240)

    @property
    def use_groq_chat(self) -> bool:
        """Structure notes with a Groq chat model first (needs GROQ_API_KEY)."""
        return os.environ.get("USE_GROQ_CHAT", "1").lower() not in ("0", "false", "no", "off")

    @property
    def groq_chat_url(self) -> str:
        return os.environ.get("GROQ_CHAT_URL", "https://api.groq.com/openai/v1/chat/completions")

    @property
    def groq_models_url(self) -> str:
        return os.environ.get("GROQ_MODELS_URL", "https://api.groq.com/openai/v1/models")

    @property
    def groq_chat_model(self) -> str:
        return os.environ.get("GROQ_CHAT_MODEL", "").strip() or DEFAULT_GROQ_CHAT_MODEL

    @property
    def groq_chat_timeout(self) -> int:
        return _int("GROQ_CHAT_TIMEOUT", 90)

    @property
    def groq_chat_max_tokens(self) -> int:
        """Longest answer asked from the chat model, kept between 512 and 8192."""
        return min(max(_int("GROQ_CHAT_MAX_TOKENS", 4096), 512), 8192)

    @property
    def groq_review(self) -> bool:
        """After the first draft of each part, ask the chat model to check the draft against the transcript."""
        return os.environ.get("GROQ_REVIEW", "1").strip().lower() not in ("0", "false", "no", "off")

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))

    @property
    def ffmpeg(self) -> str:
        return os.environ.get("FFMPEG_BIN", "ffmpeg")

    @property
    def max_upload_bytes(self) -> int:
        return _int("MAX_UPLOAD_MB", 200) * 1024 * 1024

    @property
    def chunk_seconds(self) -> int:
        return _int("CHUNK_SECONDS", 600)

    @property
    def max_chunk_bytes(self) -> int:
        """Upper bound for one chunk sent to Groq. The free tier allows about 25 MB."""
        return _int("MAX_CHUNK_MB", 20) * 1024 * 1024


settings = Settings()

# Audio is converted to mono 16 kHz MP3 before upload. Whisper resamples to
# 16 kHz mono anyway, so nothing useful is lost and the files stay small.
AUDIO_BITRATE = 48_000  # bits per second
AUDIO_RATE = 16_000
