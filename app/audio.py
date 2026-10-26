"""Audio preparation: probing, converting and splitting with ffmpeg."""
import re
import shutil
import subprocess
from pathlib import Path

from .config import AUDIO_BITRATE, AUDIO_RATE, settings


class AudioError(Exception):
    """Raised when a file cannot be read or converted. The message is safe to show."""


def ffmpeg_available() -> bool:
    return shutil.which(settings.ffmpeg) is not None


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def parse_ffmpeg_info(stderr: str) -> tuple[float, bool]:
    """Read (duration in seconds, has audio) from the text `ffmpeg -i` prints."""
    m = _DURATION.search(stderr)
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    has_audio = re.search(r"Stream #\d+:\d+.*?: Audio:", stderr) is not None
    return duration, has_audio


def probe_duration(path: Path) -> float:
    """Return the duration in seconds, or raise AudioError if there is no audio."""
    try:
        out = subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=60, errors="replace",
        )
    except FileNotFoundError:
        raise AudioError("ffmpeg is not installed on the server.")
    except subprocess.TimeoutExpired:
        raise AudioError("The file could not be read in time.")
    duration, has_audio = parse_ffmpeg_info(out.stderr)
    if not has_audio and not duration:
        raise AudioError("This file is not a readable audio or video file.")
    if not has_audio:
        raise AudioError("This file does not contain an audio track.")
    if duration <= 0:
        raise AudioError("The audio in this file is empty.")
    return duration


def segment_seconds(
    bitrate: int = AUDIO_BITRATE,
    max_bytes: int | None = None,
    wanted: int | None = None,
) -> int:
    """Length of one chunk in seconds.

    It is the configured length, reduced when needed so that a chunk at the
    given bitrate stays under the size limit (with a 10 percent margin).
    """
    max_bytes = max_bytes or settings.max_chunk_bytes
    wanted = wanted or settings.chunk_seconds
    limit = int(max_bytes * 8 * 0.9 / bitrate)
    return max(5, min(wanted, limit))


def plan_chunks(duration: float, seg: int) -> list[tuple[float, float]]:
    """Return (start, length) pairs that cover the whole duration."""
    if duration <= 0 or seg <= 0:
        return []
    out, start = [], 0.0
    while start < duration - 0.01:
        out.append((start, min(seg, duration - start)))
        start += seg
    return out


def _encode(src: Path, dst: Path, start: float, length: float | None) -> None:
    cmd = [settings.ffmpeg, "-y", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-b:a", str(AUDIO_BITRATE // 1000) + "k", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        raise AudioError("ffmpeg is not installed on the server.")
    except subprocess.TimeoutExpired:
        raise AudioError("Converting the audio took too long.")
    if r.returncode != 0 or not dst.exists():
        raise AudioError("The audio could not be converted.")


def split_audio(src: Path, workdir: Path, duration: float | None = None) -> list[Path]:
    """Convert to small mono MP3 chunks, each well under the Groq size limit."""
    workdir.mkdir(parents=True, exist_ok=True)
    duration = duration or probe_duration(src)
    seg = segment_seconds()
    paths: list[Path] = []
    for i, (start, length) in enumerate(plan_chunks(duration, seg)):
        dst = workdir / f"chunk_{i:04d}.mp3"
        _encode(src, dst, start, length)
        if dst.stat().st_size > settings.max_chunk_bytes:
            raise AudioError("An audio chunk is larger than the transcription limit.")
        if dst.stat().st_size > 0:
            paths.append(dst)
    if not paths:
        raise AudioError("No audio could be extracted from this file.")
    return paths
