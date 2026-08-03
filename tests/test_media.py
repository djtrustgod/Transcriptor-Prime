from __future__ import annotations

from pathlib import Path

import pytest

from transcriptor_prime import media


def test_probe_mp3(tone_mp3: Path):
    info = media.probe(tone_mp3)
    assert info.duration == pytest.approx(3.0, abs=0.3)
    assert info.audio_codec == "mp3"
    assert info.has_video is False
    assert "3s" in info.summary()


def test_probe_mp4_extracts_the_audio_stream(tone_mp4: Path):
    """MP4 is a video container; the app must find the audio track inside it."""
    info = media.probe(tone_mp4)
    assert info.duration == pytest.approx(3.0, abs=0.3)
    assert info.audio_codec == "aac"


def test_mp4_is_not_mislabelled_as_mov(tone_mp4: Path):
    """FFmpeg demuxes .mp4 with the shared 'mov,mp4,m4a,...' demuxer."""
    assert media.probe(tone_mp4).container == "mp4"


def test_mp3_container_name(tone_mp3: Path):
    assert media.probe(tone_mp3).container == "mp3"


def test_probe_missing_file_raises_media_error(tmp_path: Path):
    with pytest.raises(media.MediaError, match="File not found"):
        media.probe(tmp_path / "nope.mp3")


def test_probe_non_media_file_raises_media_error(tmp_path: Path):
    """A text file renamed .mp3 must fail cleanly, not hang or raise raw av errors."""
    fake = tmp_path / "not-audio.mp3"
    fake.write_text("this is plainly not an audio file", encoding="utf-8")
    with pytest.raises(media.MediaError):
        media.probe(fake)


def test_default_output_path_sits_beside_the_source():
    source = Path("C:/recordings/interview.mp4")
    assert media.default_output_path(source) == Path("C:/recordings/interview.txt")


class TestUniquePath:
    def test_returns_the_path_when_free(self, tmp_path: Path):
        target = tmp_path / "transcript.txt"
        assert media.unique_path(target) == target

    def test_suffixes_when_taken(self, tmp_path: Path):
        target = tmp_path / "transcript.txt"
        target.write_text("existing", encoding="utf-8")
        assert media.unique_path(target) == tmp_path / "transcript (2).txt"

    def test_keeps_counting_past_the_first_collision(self, tmp_path: Path):
        (tmp_path / "transcript.txt").write_text("a", encoding="utf-8")
        (tmp_path / "transcript (2).txt").write_text("b", encoding="utf-8")
        assert media.unique_path(tmp_path / "transcript.txt") == (
            tmp_path / "transcript (3).txt"
        )
