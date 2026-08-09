from __future__ import annotations

import sys
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

    def test_reserved_names_are_skipped_even_when_nothing_is_on_disk(
        self, tmp_path: Path
    ):
        """A batch plans every output before any transcript exists."""
        target = tmp_path / "talk.txt"
        assert media.unique_path(target, taken={target}) == tmp_path / "talk (2).txt"

    def test_reservations_and_disk_collisions_combine(self, tmp_path: Path):
        target = tmp_path / "talk.txt"
        target.write_text("on disk", encoding="utf-8")
        assert media.unique_path(
            target, taken={tmp_path / "talk (2).txt"}
        ) == tmp_path / "talk (3).txt"

    def test_two_same_stem_sources_get_distinct_outputs(self, tmp_path: Path):
        """The exact collision batch mode introduces: talk.mp3 and talk.mp4."""
        reserved: set[Path] = set()
        outputs = []
        for name in ("talk.mp3", "talk.mp4"):
            out = media.unique_path(
                media.default_output_path(tmp_path / name), taken=reserved
            )
            reserved.add(out)
            outputs.append(out)
        assert outputs == [tmp_path / "talk.txt", tmp_path / "talk (2).txt"]


class TestMediaFilesIn:
    def test_finds_supported_files_and_ignores_the_rest(self, tmp_path: Path):
        for name in ("b.mp3", "a.MP4", "notes.txt", "cover.jpg"):
            (tmp_path / name).write_bytes(b"x")

        found = media.media_files_in(tmp_path)

        assert [p.name for p in found] == ["a.MP4", "b.mp3"]

    def test_is_not_recursive_by_default(self, tmp_path: Path):
        (tmp_path / "top.mp3").write_bytes(b"x")
        nested = tmp_path / "sub"
        nested.mkdir()
        (nested / "deep.mp3").write_bytes(b"x")

        assert [p.name for p in media.media_files_in(tmp_path)] == ["top.mp3"]
        assert [p.name for p in media.media_files_in(tmp_path, recursive=True)] == [
            "deep.mp3",
            "top.mp3",
        ]

    def test_a_missing_directory_yields_nothing(self, tmp_path: Path):
        assert media.media_files_in(tmp_path / "nope") == []


class TestSameFileKey:
    def test_case_differences_collapse_on_windows(self, tmp_path: Path):
        target = tmp_path / "Recording.MP3"
        target.write_bytes(b"x")
        same = tmp_path / "recording.mp3"
        if sys.platform == "win32":
            assert media.same_file_key(target) == media.same_file_key(same)
        else:
            assert media.same_file_key(target) != media.same_file_key(same)

    def test_a_relative_path_matches_its_absolute_form(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mp3"
        target.write_bytes(b"x")
        monkeypatch.chdir(tmp_path)
        assert media.same_file_key(Path("clip.mp3")) == media.same_file_key(target)
