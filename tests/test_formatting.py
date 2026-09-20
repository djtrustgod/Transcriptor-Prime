from __future__ import annotations

from datetime import datetime

import pytest

from transcriptor_prime import __version__
from transcriptor_prime.formatting import (
    Block,
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


class TestSpeakers:
    """Speaker labels are additive: absent, the output is what it always was."""

    def test_a_block_with_a_speaker_names_them_on_the_timecode_line(self):
        block = Block(start=7, end=20, text="Well, it was back in 1998.", speaker="Jane Doe")
        assert block.render(100) == "[00:00:07] Jane Doe:\nWell, it was back in 1998.\n"

    def test_a_block_without_a_speaker_renders_exactly_as_before(self):
        assert Block(start=7, end=20, text="Hello.").render(100) == "[00:00:07]\nHello.\n"

    def test_the_label_line_is_never_wrapped(self):
        name = "Professor Bartholomew Featherstonehaugh"
        rendered = Block(start=0, end=5, text="a b c", speaker=name).render(10)
        assert rendered.splitlines()[0] == f"[00:00:00] {name}:"

    def test_a_change_of_speaker_closes_the_block(self):
        builder = ParagraphBuilder(interval_seconds=30)
        assert builder.add_run(0, 4, "So how did it start?", "Speaker 1") == []
        closed = builder.add_run(5, 9, "Well.", "Speaker 2")
        assert [(b.speaker, b.text) for b in closed] == [
            ("Speaker 1", "So how did it start?")
        ]
        last = builder.flush()
        assert (last.speaker, last.start, last.text) == ("Speaker 2", 5, "Well.")

    def test_a_long_answer_repeats_the_name_every_interval(self):
        builder = ParagraphBuilder(interval_seconds=30)
        blocks = []
        for start in range(0, 90, 10):
            blocks += builder.add_run(start, start + 10, "words", "Speaker 1")
        assert [b.start for b in blocks] == [0, 30, 60]
        assert {b.speaker for b in blocks} == {"Speaker 1"}

    def test_one_run_can_close_two_blocks(self):
        builder = ParagraphBuilder(interval_seconds=30)
        builder.add_run(0, 5, "Question?", "Speaker 1")
        closed = builder.add_run(5, 40, "A very long answer.", "Speaker 2")
        assert [b.speaker for b in closed] == ["Speaker 1", "Speaker 2"]
        assert builder.flush() is None

    def test_add_is_unchanged_by_the_speaker_machinery(self):
        with_add, with_run = ParagraphBuilder(30), ParagraphBuilder(30)
        segments = [(0, 12, "one"), (12, 31, "two"), (31, 40, " "), (40, 45, "three")]
        rendered_add = [with_add.add(*s) for s in segments] + [with_add.flush()]
        rendered_run = [
            (with_run.add_run(*s) or [None])[0] for s in segments
        ] + [with_run.flush()]
        assert rendered_add == rendered_run

    def test_header_lists_speakers_only_when_given(self):
        common = dict(
            source_name="talk.mp3",
            duration=60,
            model="small",
            language="en",
            language_detected=False,
            language_probability=None,
            generated_at=datetime(2026, 9, 19, 10, 30),
        )
        plain = build_header(**common)
        assert "Speakers:" not in plain
        named = build_header(**common, speakers=["Speaker 1", "Speaker 2"])
        assert "Language:   en (forced)\nSpeakers:   Speaker 1, Speaker 2\nGenerated:" in named
        assert named.replace("Speakers:   Speaker 1, Speaker 2\n", "") == plain
