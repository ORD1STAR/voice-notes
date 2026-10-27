"""Audio preparation: probing, converting and splitting with ffmpeg."""
import re
import shutil
import subprocess
from pathlib import Path

from .config import AUDIO_BITRATE, AUDIO_RATE, settings

# When a chunk has to start in the middle of speech, it starts this many seconds early.
# The repeated words are removed again when the transcripts are joined.
OVERLAP_SECONDS = 2.0


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


_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _decoded_duration(path: Path) -> float:
    """Measure the length by decoding the audio.

    Recordings made in a browser (WebM from MediaRecorder) carry no duration in
    their header, so `ffmpeg -i` alone reports none.
    """
    try:
        out = subprocess.run(
            [settings.ffmpeg, "-hide_banner", "-i", str(path), "-vn", "-f", "null", "-"],
            capture_output=True, text=True, timeout=900, errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    times = _TIME.findall(out.stderr)
    if not times:
        return 0.0
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


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
        duration = _decoded_duration(path)
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


# ------------------------------------------------------- cutting in quiet places

_SILENCE = re.compile(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)")


def parse_silences(stderr: str) -> list[tuple[float, float]]:
    """Read (start, end) pairs from the output of ffmpeg's silencedetect filter."""
    out: list[tuple[float, float]] = []
    start = None
    for kind, value in _SILENCE.findall(stderr):
        t = max(float(value), 0.0)
        if kind == "start":
            start = t
        elif start is not None:
            out.append((start, t))
            start = None
    return out


def detect_silences(src: Path, min_length: float = 0.3) -> list[tuple[float, float]]:
    """Find the quiet gaps in a recording. Returns [] when they cannot be found."""
    cmd = [settings.ffmpeg, "-hide_banner", "-nostats", "-i", str(src), "-vn",
           "-af", f"silencedetect=noise=-32dB:d={min_length}", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return parse_silences(r.stderr)


def plan_cuts(
    duration: float,
    seg: int,
    silences: list[tuple[float, float]] | None = None,
    window: float | None = None,
    overlap: float = OVERLAP_SECONDS,
) -> list[tuple[float, float, float]]:
    """Return (start, length, overlap) triples that cover the whole duration.

    A chunk normally ends at the quiet gap closest to (and before) the target length, so no word
    is cut in two. When there is no gap within the search window the chunk is cut at the target
    and the next chunk starts `overlap` seconds early; the second value of the triple then
    includes those seconds and the third tells the transcript join to remove repeated words.
    """
    if duration <= 0 or seg <= 0:
        return []
    silences = silences or []
    window = min(45.0, seg * 0.3) if window is None else window
    cuts: list[tuple[float, bool]] = []  # (time of the cut, was it a hard cut)
    pos = 0.0
    while duration - pos > seg + 0.01:
        target = pos + seg
        low = max(pos + seg * 0.5, target - window)
        mids = [(s + e) / 2 for s, e in silences if low <= (s + e) / 2 <= target]
        if mids:
            cut, hard = max(mids), False
        else:
            cut, hard = target, True
        cuts.append((cut, hard))
        pos = cut
    plan = []
    start, lead = 0.0, 0.0
    for cut, hard in cuts + [(duration, False)]:
        plan.append((max(start - lead, 0.0), cut - start + lead, lead))
        start = cut
        lead = overlap if hard else 0.0
    return plan


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


def split_audio_ex(src: Path, workdir: Path, duration: float | None = None) -> tuple[list[Path], list[float]]:
    """Convert to small mono MP3 chunks, each well under the Groq size limit.

    Returns the chunk files and, for each, how many seconds it repeats from the chunk before.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    duration = duration or probe_duration(src)
    seg = segment_seconds()
    silences = detect_silences(src) if duration > seg + 0.01 else []
    paths: list[Path] = []
    overlaps: list[float] = []
    for i, (start, length, lead) in enumerate(plan_cuts(duration, seg, silences)):
        dst = workdir / f"chunk_{i:04d}.mp3"
        _encode(src, dst, start, length)
        if dst.stat().st_size > settings.max_chunk_bytes:
            raise AudioError("An audio chunk is larger than the transcription limit.")
        if dst.stat().st_size > 0:
            paths.append(dst)
            overlaps.append(lead)
    if not paths:
        raise AudioError("No audio could be extracted from this file.")
    return paths, overlaps


def split_audio(src: Path, workdir: Path, duration: float | None = None) -> list[Path]:
    """Same as split_audio_ex, for callers that only need the files."""
    return split_audio_ex(src, workdir, duration)[0]
