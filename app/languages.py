"""Languages offered on the start page.

The code is an ISO 639-1 code, which is what the Groq Whisper `language` parameter expects.
"auto" means the language is not given and Whisper detects it.
"""
AUTO = "auto"

LANGUAGES: dict[str, str] = {
    "auto": "Auto",
    "en": "English",
    "fr": "French",
    "ar": "Arabic",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "tr": "Turkish",
    "zh": "Chinese",
}


def normalize(value: str | None) -> str:
    """Return a supported language code. An empty value means auto; anything unknown raises ValueError."""
    code = (value or "").strip().lower()
    if not code:
        return AUTO
    if code not in LANGUAGES:
        raise ValueError("Unknown language.")
    return code


def lenient(value: str | None) -> str:
    """Like normalize, but an unknown or missing value becomes auto (for notes saved earlier)."""
    try:
        return normalize(value)
    except ValueError:
        return AUTO


def name(code: str | None) -> str:
    return LANGUAGES.get(lenient(code), "Auto")


def allowed_text() -> str:
    return ", ".join(LANGUAGES)
