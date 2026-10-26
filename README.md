<p align="center">
  <img src="docs/banner.svg" alt="Voice Notes: upload a recording, get clean structured notes" width="100%">
</p>

# Voice Notes

A small web app that turns a voice note into written notes. You upload a recording, it is transcribed with Groq's Whisper API, a small local model organises the text into a title, a short summary, headings and bullets, and you export the result as PDF, plain text or Markdown.

I built it for my own use: I record ideas while walking, and I want to find clean notes on my laptop afterwards instead of a 12 minute audio file.

<p align="center">
  <img src="docs/notes.png" alt="A finished note with summary, sections and export buttons" width="760">
</p>

## Features

- Upload audio or video files up to 200 MB (MP3, M4A, WAV, OGG, FLAC, WebM, MP4 and more), by drag and drop or file picker.
- Long recordings are converted and split with ffmpeg into chunks well under Groq's 25 MB free tier limit, transcribed one by one and joined.
- Structuring with a small local model served by Ollama. If Ollama is not running, a rule-based fallback still produces a title, summary and sections, and the interface says which one was used.
- Export to PDF, TXT or Markdown, with or without the full transcript.
- Live progress through each step, clear error messages, and a list of recent notes that survives a restart.
- Keyboard friendly, works on phones, follows the system light or dark setting.
- No build step: the frontend is plain HTML, CSS and JavaScript.

## Screenshots

| Start | Working | Phone |
| --- | --- | --- |
| <img src="docs/home.png" alt="Start screen" width="280"> | <img src="docs/progress.png" alt="Progress screen" width="280"> | <img src="docs/mobile-notes.png" alt="Notes on a phone" width="160"> |

## Quick start

You need Python 3.10 or newer and ffmpeg.

```bash
git clone <your repository url> voice-notes
cd voice-notes
./scripts/setup.sh          # creates .venv and installs requirements.txt
export GROQ_API_KEY="your key from console.groq.com"
./scripts/run.sh
```

Open <http://localhost:2525>.

On Ubuntu, `sudo apt install ffmpeg` installs ffmpeg. A free Groq API key is available at <https://console.groq.com/keys>.

### Local model for structuring (recommended)

```bash
./scripts/setup-ollama.sh   # installs Ollama and pulls the default model
```

The default model is `qwen2.5:1.5b`: about 1 GB on disk and comfortable on a 2 CPU, 4 GB machine. On two CPU cores it generates only a few words per second, so structuring a long recording takes minutes, not seconds; long transcripts are processed in pieces and the page shows progress. Pick another model with `OLLAMA_MODEL`. Without Ollama the app still works and uses the rule-based fallback, which keeps your sentences as bullets and groups them by topic words. The result is readable but much less polished.

## Configuration

Everything is set through environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GROQ_API_KEY` | none, required | Groq API key. Read from the environment only, never stored or logged. |
| `PORT` | `2525` | Port the server listens on. |
| `HOST` | `0.0.0.0` | Interface to bind. Use `127.0.0.1` to keep it local. |
| `MAX_UPLOAD_MB` | `200` | Largest accepted upload. |
| `CHUNK_SECONDS` | `600` | Length of each audio chunk. Reduced automatically if a chunk would exceed `MAX_CHUNK_MB`. |
| `MAX_CHUNK_MB` | `20` | Size ceiling for one chunk sent to Groq. |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | Whisper model name. |
| `TRANSCRIPTION_LANGUAGE` | empty | ISO code such as `en` or `fr`. Empty means automatic detection. |
| `USE_OLLAMA` | `1` | Set to `0` to always use the rule-based fallback. |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | Model used for structuring. |
| `OLLAMA_HOST_URL` | `http://127.0.0.1:11434` | Where Ollama listens. |
| `OLLAMA_TIMEOUT` | `240` | Seconds to wait for one model call before falling back. |
| `DATA_DIR` | `./data` | Where notes are stored. Uploaded audio is deleted once processed. |
| `FFMPEG_BIN` | `ffmpeg` | Path to the ffmpeg executable. |

## Running it

Directly:

```bash
GROQ_API_KEY=... python -m app
```

As a service, see `deploy/voice-notes.service` for a systemd unit. The server listens on all interfaces on port 2525; if you use a firewall, allow the port (`sudo ufw allow 2525/tcp` with ufw).

The app has no login. Anyone who can reach the port can upload files and spend your Groq quota, so keep it on a private network, behind a VPN, or behind a reverse proxy that asks for a password.

## How it works

1. The upload is saved to disk in 1 MB blocks and refused as soon as it passes the size limit.
2. ffmpeg converts it to mono 16 kHz MP3 and cuts it into chunks. At 48 kbit/s ten minutes are about 3.6 MB, so even a 4 hour recording stays far below the Groq limit per file.
3. Each chunk is sent to Groq Whisper. Rate limits and temporary errors are retried with backoff.
4. The joined transcript is split on sentence boundaries into pieces of about 3500 characters. Each piece goes to the local model, which answers in JSON (title, summary, sections with bullets). A final short call writes the overall title and summary.
5. If Ollama is missing or a call fails, the rule-based structuring takes over for the whole note.

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/health` | Server status: ffmpeg, key present, Ollama state. |
| `POST` | `/api/notes` | Multipart upload with a `file` field. Returns `{"id": ...}` with status 202. |
| `GET` | `/api/notes` | Recent notes. |
| `GET` | `/api/notes/{id}` | Status, progress, transcript and notes. |
| `GET` | `/api/notes/{id}/export?format=pdf\|txt\|md&transcript=true` | Download an export. |
| `DELETE` | `/api/notes/{id}` | Delete a note. |

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests --ignore=tests/e2e     # backend: chunking, transcription client, structuring, exports, API
```

Browser tests drive the real server with Chromium. Groq and Ollama are replaced by small fake services, so they need no key and no network:

```bash
playwright install chromium
pytest tests/e2e
```

They cover upload validation, the full upload to export flow (PDF, TXT and Markdown downloads), error screens, keyboard use, text contrast in light and dark mode, and a phone sized layout. Set `SCREENSHOT_DIR` to save screenshots of each screen.

## Project layout

```
app/
  main.py         FastAPI routes
  jobs.py         job state, storage and the processing pipeline
  audio.py        ffmpeg conversion and chunking
  transcribe.py   Groq client
  structure.py    Ollama structuring and the rule-based fallback
  export.py       PDF, TXT and Markdown output
  static/         the web interface
tests/            backend tests and tests/e2e browser tests
scripts/          setup and run helpers
deploy/           example systemd unit
```

## Known limits

- One recording is processed at a time; others wait in a queue.
- Chunks are cut at fixed times, so a word at a boundary can occasionally be split.
- PDF export covers Latin, Greek and Cyrillic text. Arabic, Hebrew and Chinese are not rendered correctly yet.
- Notes cannot be edited in the interface yet.

## License

MIT. The bundled DejaVu fonts used for the PDF export have their own permissive license, included in `app/fonts`.
