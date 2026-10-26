import pytest

from app import audio
from app.config import AUDIO_BITRATE

from conftest import needs_ffmpeg


def test_segment_seconds_uses_configured_length():
    assert audio.segment_seconds(wanted=600, max_bytes=20 * 1024 * 1024) == 600


def test_segment_seconds_shrinks_to_respect_size_limit():
    seg = audio.segment_seconds(bitrate=AUDIO_BITRATE, max_bytes=1_000_000, wanted=3600)
    assert seg * AUDIO_BITRATE / 8 <= 1_000_000


def test_plan_chunks_covers_the_whole_duration():
    plan = audio.plan_chunks(1450.0, 600)
    assert [round(s) for s, _ in plan] == [0, 600, 1200]
    assert sum(length for _, length in plan) == pytest.approx(1450.0)


def test_plan_chunks_short_and_empty():
    assert audio.plan_chunks(30, 600) == [(0.0, 30)]
    assert audio.plan_chunks(0, 600) == []


def test_200_mb_worth_of_audio_gives_chunks_under_25_mb():
    # Worst case: 4 hours of audio at the working bitrate.
    seg = audio.segment_seconds()
    for _, length in audio.plan_chunks(4 * 3600, seg):
        assert length * AUDIO_BITRATE / 8 < 25 * 1024 * 1024


def test_parse_ffmpeg_info():
    text = "Input #0, wav\n  Duration: 00:01:02.50, bitrate: 705 kb/s\n  Stream #0:0: Audio: pcm_s16le"
    assert audio.parse_ffmpeg_info(text) == (62.5, True)
    assert audio.parse_ffmpeg_info("Stream #0:0: Video: h264\nDuration: 00:00:05.00") == (5.0, False)
    assert audio.parse_ffmpeg_info("garbage") == (0.0, False)


@needs_ffmpeg
def test_probe_duration(make_audio):
    assert audio.probe_duration(make_audio(3)) == pytest.approx(3, abs=0.2)


@needs_ffmpeg
def test_probe_rejects_non_audio(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("this is not audio")
    with pytest.raises(audio.AudioError):
        audio.probe_duration(bad)


@needs_ffmpeg
def test_split_audio_makes_small_ordered_chunks(make_audio, tmp_path, monkeypatch):
    monkeypatch.setenv("CHUNK_SECONDS", "10")
    src = make_audio(25)
    chunks = audio.split_audio(src, tmp_path / "out")
    assert len(chunks) == 3
    assert [c.name for c in chunks] == sorted(c.name for c in chunks)
    for c in chunks:
        assert c.stat().st_size < 25 * 1024 * 1024
        assert c.suffix == ".mp3"
    total = sum(audio.probe_duration(c) for c in chunks)
    assert total == pytest.approx(25, abs=1.5)


@needs_ffmpeg
def test_split_audio_single_chunk_for_short_file(make_audio, tmp_path):
    chunks = audio.split_audio(make_audio(4), tmp_path / "out")
    assert len(chunks) == 1
