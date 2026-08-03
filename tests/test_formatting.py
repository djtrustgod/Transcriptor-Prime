from __future__ import annotations

from datetime import datetime

import pytest

from transcriptor_prime import __version__
from transcriptor_prime.formatting import (
    ParagraphBuilder,
    build_header,
    format_duration,
    format_timecode,
)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00:00"),
        (0.4, "00:00:00"),
        (59.9, "00:00:59"),
        (60, "00:01:00"),
        (3600, "01:00:00"),
        (3661, "01:01:01"),
        (10752, "02:59:12"),  # a three-hour recording
        (-5, "00:00:00"),  # decoders occasionally report a negative first pts
    ],
)
def test_format_timecode(seconds, expected):
    assert format_timecode(seconds) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0s"), (45, "45s"), (90, "1m 30s"), (3600, "1h 00m 00s"), (10032, "2h 47m 12s")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


class TestParagraphBuilder:
    def test_buffers_until_interval_is_reached(self):
        builder = ParagraphBuilder(interval_seconds=30)
        assert builder.add(0.0, 10.0, "one") is None
        assert builder.add(10.0, 20.0, "two") is None

        block = builder.add(20.0, 30.0, "three")
        assert block is not None
        assert block.start == 0.0
        assert block.text == "one two three"

    def test_next_block_starts_at_its_own_first_segment(self):
        """The stamp must point at real speech, not a rounded interval boundary."""
        builder = ParagraphBuilder(interval_seconds=30)
        builder.add(0.0, 31.0, "first")
        block = builder.add(47.5, 80.0, "second")
        assert block is not None
        assert block.start == 47.5
        assert block.render(wrap_width=0).startswith("[00:00:47]")

    def test_segment_longer_than_the_interval_emits_immediately(self):
        builder = ParagraphBuilder(interval_seconds=30)
        block = builder.add(0.0, 120.0, "a very long uninterrupted stretch")
        assert block is not None
        assert block.text == "a very long uninterrupted stretch"

    def test_flush_emits_the_trailing_partial_block(self):
        builder = ParagraphBuilder(interval_seconds=30)
        builder.add(0.0, 5.0, "tail end")
        block = builder.flush()
        assert block is not None
        assert block.text == "tail end"

    def test_flush_on_empty_builder_returns_none(self):
        assert ParagraphBuilder(interval_seconds=30).flush() is None

    def test_flush_is_idempotent(self):
        builder = ParagraphBuilder(interval_seconds=30)
        builder.add(0.0, 5.0, "text")
        assert builder.flush() is not None
        assert builder.flush() is None

    def test_blank_segments_are_dropped(self):
        builder = ParagraphBuilder(interval_seconds=30)
        assert builder.add(0.0, 5.0, "   ") is None
        assert builder.flush() is None

    def test_segment_text_is_stripped_and_joined_with_single_spaces(self):
        builder = ParagraphBuilder(interval_seconds=10)
        builder.add(0.0, 4.0, "  Hello there. ")
        block = builder.add(4.0, 11.0, " General Kenobi.  ")
        assert block is not None
        assert block.text == "Hello there. General Kenobi."


class TestBlockRender:
    def test_wraps_at_the_requested_width(self):
        builder = ParagraphBuilder(interval_seconds=1)
        block = builder.add(0.0, 2.0, "word " * 40)
        assert block is not None
        lines = block.render(wrap_width=40).splitlines()
        assert lines[0] == "[00:00:00]"
        assert all(len(line) <= 40 for line in lines[1:])
        assert len(lines) > 2

    def test_wrap_width_zero_leaves_text_on_one_line(self):
        builder = ParagraphBuilder(interval_seconds=1)
        block = builder.add(0.0, 2.0, "word " * 40)
        assert block is not None
        assert len(block.render(wrap_width=0).splitlines()) == 2


class TestHeader:
    def test_detected_language_shows_confidence(self):
        header = build_header(
            source_name="interview.mp4",
            duration=10032,
            model="small",
            language="en",
            language_detected=True,
            language_probability=0.987,
            generated_at=datetime(2026, 8, 2, 15, 42),
        )
        assert "Transcript: interview.mp4" in header
        assert "Duration:   02:47:12" in header
        assert "Model:      small (int8, CPU)" in header
        assert "Language:   en (detected, 0.99)" in header
        assert "Generated:  2026-08-02 15:42" in header

    def test_header_records_the_producing_version(self):
        """A transcript found later must be traceable to the build that made it."""
        header = build_header(
            source_name="a.mp3",
            duration=60,
            model="tiny",
            language="en",
            language_detected=True,
            language_probability=1.0,
            generated_at=datetime(2026, 8, 2, 9, 0),
        )
        assert f"Created by: Transcriptor Prime {__version__}" in header

    def test_forced_language_is_labelled_as_such(self):
        header = build_header(
            source_name="a.mp3",
            duration=60,
            model="tiny",
            language="es",
            language_detected=False,
            language_probability=None,
            generated_at=datetime(2026, 1, 1, 9, 0),
        )
        assert "Language:   es (forced)" in header
