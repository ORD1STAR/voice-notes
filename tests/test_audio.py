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
    # Without a quiet gap near the cut, each later chunk starts 2 seconds early to repeat the words around it.
    total = sum(audio.probe_duration(c) for c in chunks)
    assert total == pytest.approx(25 + 2 * audio.OVERLAP_SECONDS, abs=1.5)


@needs_ffmpeg
def test_split_audio_single_chunk_for_short_file(make_audio, tmp_path):
    chunks = audio.split_audio(make_audio(4), tmp_path / "out")
    assert len(chunks) == 1


def test_browser_style_webm_without_duration_is_measured(tmp_path, make_audio):
    """MediaRecorder output has no duration in its header; the length must still be found."""
    import subprocess

    from conftest import FFMPEG
    from app import audio

    src = make_audio(4)
    out = tmp_path / "browser.webm"
    with open(out, "wb") as fh:  # writing to a pipe leaves the duration out of the header
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-c:a", "libopus", "-f", "webm", "pipe:1"],
                       stdout=fh, check=True)
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "Duration: N/A" in info
    assert 3.0 < audio.probe_duration(out) < 5.5


# ------------------------------------------------- chunk boundaries and words

def test_parse_silences_pairs_starts_and_ends():
    text = ("[silencedetect @ 0x1] silence_start: 8.5\n[silencedetect @ 0x1] silence_end: 9.5 | silence_duration: 1\n"
            "[silencedetect @ 0x1] silence_start: -0.01\n[silencedetect @ 0x1] silence_end: 0.4 | silence_duration: 0.4\n"
            "[silencedetect @ 0x1] silence_start: 30.0\n")
    assert audio.parse_silences(text) == [(8.5, 9.5), (0.0, 0.4)]  # an unfinished gap at the end is ignored


def test_plan_cuts_prefers_a_quiet_gap_near_the_target():
    plan = audio.plan_cuts(25.0, 10, [(1.0, 1.5), (8.5, 9.5), (15.0, 15.4)])
    assert plan[0] == pytest.approx((0.0, 9.0, 0.0))
    assert plan[1][2] == 0.0  # cut in silence: no repeated seconds
    assert sum(1 for _ in plan) == 3
    ends = [s + length - lead * 0 for s, length, lead in plan]
    assert ends[-1] == pytest.approx(25.0)


def test_plan_cuts_without_a_gap_cuts_hard_and_overlaps():
    plan = audio.plan_cuts(25.0, 10, [])
    assert [round(s, 1) for s, _, _ in plan] == [0.0, 8.0, 18.0]
    assert [lead for _, _, lead in plan] == [0.0, 2.0, 2.0]
    # every second of the recording is still covered, and chunks stay close to the target length
    assert plan[0][0] == 0 and plan[-1][0] + plan[-1][1] == pytest.approx(25.0)
    assert all(length <= 10 + 2 + 0.01 for _, length, _ in plan)


def test_plan_cuts_ignores_gaps_that_are_too_early_in_the_chunk():
    # A gap at 2 s would make a 2 second chunk; the search only looks in the last part of the chunk.
    plan = audio.plan_cuts(25.0, 10, [(1.9, 2.1)])
    assert plan[0][1] == pytest.approx(10.0)


def test_plan_cuts_short_and_empty():
    assert audio.plan_cuts(4.0, 10, []) == [(0.0, 4.0, 0.0)]
    assert audio.plan_cuts(0, 10) == []


@needs_ffmpeg
def test_split_cuts_in_the_quiet_gap(tmp_path, monkeypatch):
    import subprocess

    from conftest import FFMPEG

    monkeypatch.setenv("CHUNK_SECONDS", "10")
    src = tmp_path / "speech-like.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                    "-af", "volume=enable='between(t,8.5,9.5)':volume=0", str(src)], check=True)
    assert audio.detect_silences(src)[0][0] == pytest.approx(8.5, abs=0.15)
    chunks, overlaps = audio.split_audio_ex(src, tmp_path / "out")
    assert overlaps[1] == 0.0
    assert audio.probe_duration(chunks[0]) == pytest.approx(9.0, abs=0.3)


def test_plan_cuts_does_not_send_a_chunk_with_almost_nothing_new():
    # 600.5 s with a 600 s target: the last half second is folded into the chunk before it
    plan = audio.plan_cuts(600.5, 600, [])
    assert len(plan) == 1 and plan[0] == pytest.approx((0.0, 600.5, 0.0))
    plan = audio.plan_cuts(1200.4, 600, [])
    assert plan[-1][0] + plan[-1][1] == pytest.approx(1200.4)
    assert all(length - lead >= audio.MIN_TAIL_SECONDS for _, length, lead in plan)
    # a tail with real speech in it still gets its own chunk
    assert len(audio.plan_cuts(25.0, 10, [])) == 3
