from __future__ import annotations

from dataclasses import dataclass, field

from transcriptor_prime.attribution import (
    SpeakerNamer,
    Timeline,
    Turn,
    absorb_minor_speakers,
    split_segment,
)


@dataclass
class Word:
    start: float
    end: float
    word: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] | None = field(default=None)


def words(*specs: tuple[float, float, str]) -> list[Word]:
    return [Word(start, end, f" {text}") for start, end, text in specs]


class TestTimeline:
    def test_an_empty_timeline_knows_nobody(self):
        timeline = Timeline([])
        assert not timeline
        assert timeline.speaker_at(0, 5) is None

    def test_the_speaker_with_the_most_overlap_wins(self):
        timeline = Timeline([Turn(0, 10, 0), Turn(10, 20, 1)])
        assert timeline.speaker_at(2, 4) == 0
        assert timeline.speaker_at(8, 14) == 1  # 2s of speaker 0, 4s of speaker 1
        assert timeline.speaker_at(6, 11) == 0

    def test_a_tie_goes_to_whoever_was_already_talking(self):
        timeline = Timeline([Turn(0, 10, 0), Turn(10, 20, 1)])
        assert timeline.speaker_at(9, 11, previous=1) == 1
        assert timeline.speaker_at(9, 11, previous=0) == 0
        assert timeline.speaker_at(9, 11) == 0  # then to the earlier turn

    def test_overlapped_speech_does_not_hide_a_long_running_turn(self):
        # Speaker 0 talks throughout; speaker 1 interjects briefly. A naive
        # "latest turn that started before t" lookup would miss speaker 0.
        timeline = Timeline([Turn(0, 60, 0), Turn(10, 11, 1), Turn(20, 21, 1)])
        assert timeline.speaker_at(30, 35) == 0
        assert timeline.speaker_at(10.2, 10.8, previous=0) == 0  # a true tie
        assert timeline.speaker_at(10.2, 10.8, previous=1) == 1

    def test_a_word_in_a_gap_takes_the_nearest_turn(self):
        timeline = Timeline([Turn(0, 10, 0), Turn(12, 20, 1)])
        assert timeline.speaker_at(10.2, 10.5) == 0
        assert timeline.speaker_at(11.6, 11.9) == 1

    def test_a_word_far_from_any_turn_inherits_the_previous_speaker(self):
        timeline = Timeline([Turn(0, 10, 0), Turn(30, 40, 1)])
        assert timeline.speaker_at(27, 28, previous=0) == 0
        assert timeline.speaker_at(27, 28) == 1  # nobody to inherit from

    def test_a_zero_length_word_still_resolves(self):
        timeline = Timeline([Turn(0, 10, 0), Turn(10, 20, 1)])
        assert timeline.speaker_at(15, 15) == 1


class TestSplitSegment:
    timeline = Timeline([Turn(0, 5, 0), Turn(5, 20, 1)])

    def test_a_segment_without_words_gets_one_label(self):
        runs = split_segment(Segment(6, 9, " Hello there. "), self.timeline)
        assert [(r.text, r.speaker) for r in runs] == [("Hello there.", 1)]

    def test_a_segment_is_split_where_the_speaker_changes(self):
        segment = Segment(
            3,
            9,
            " How did it start? Well, back in 1998.",
            words(
                (3.0, 3.4, "How"),
                (3.4, 3.8, "did"),
                (3.8, 4.2, "it"),
                (4.2, 4.9, "start?"),
                (5.2, 5.9, "Well,"),
                (5.9, 6.5, "back"),
                (6.5, 7.0, "in"),
                (7.0, 8.0, "1998."),
            ),
        )
        runs = split_segment(segment, self.timeline)
        assert [(r.text, r.speaker) for r in runs] == [
            ("How did it start?", 0),
            ("Well, back in 1998.", 1),
        ]
        assert (runs[0].start, runs[0].end) == (3.0, 4.9)
        assert (runs[1].start, runs[1].end) == (5.2, 8.0)

    def test_a_lone_short_edge_word_snaps_to_the_rest(self):
        # "So" lands 0.2s inside the previous speaker's turn — timestamp jitter.
        segment = Segment(
            4.7,
            8,
            " So that was it.",
            words((4.7, 4.95, "So"), (5.1, 5.6, "that"), (5.6, 6.0, "was"), (6.0, 6.5, "it.")),
        )
        runs = split_segment(segment, self.timeline)
        assert [(r.text, r.speaker) for r in runs] == [("So that was it.", 1)]

    def test_a_tiny_flip_inside_a_segment_is_smoothed_away(self):
        timeline = Timeline([Turn(0, 3, 0), Turn(3, 3.4, 1), Turn(3.4, 10, 0)])
        segment = Segment(
            1,
            6,
            " one two three four five",
            words(
                (1.0, 2.0, "one"),
                (2.0, 3.0, "two"),
                (3.0, 3.4, "three"),
                (3.4, 4.5, "four"),
                (4.5, 6.0, "five"),
            ),
        )
        runs = split_segment(segment, timeline)
        assert [(r.text, r.speaker) for r in runs] == [("one two three four five", 0)]

    def test_a_real_change_mid_segment_is_not_smoothed(self):
        timeline = Timeline([Turn(0, 3, 0), Turn(3, 6, 1), Turn(6, 10, 0)])
        segment = Segment(
            1,
            9,
            "",
            words(
                (1.0, 2.9, "one"),
                (3.0, 4.0, "two"),
                (4.0, 5.0, "three"),
                (5.0, 5.9, "four"),
                (6.1, 9.0, "five"),
            ),
        )
        runs = split_segment(segment, timeline)
        assert [r.speaker for r in runs] == [0, 1, 0]

    def test_an_interjection_in_its_own_segment_survives(self):
        timeline = Timeline([Turn(0, 10, 0), Turn(10, 10.6, 1), Turn(10.6, 20, 0)])
        segment = Segment(10, 10.6, " Right.", words((10.05, 10.55, "Right.")))
        runs = split_segment(segment, timeline, previous=0)
        assert [(r.text, r.speaker) for r in runs] == [("Right.", 1)]

    def test_the_previous_speaker_carries_into_the_segment(self):
        timeline = Timeline([Turn(0, 10, 0), Turn(40, 50, 1)])
        segment = Segment(25, 27, " Hm.", words((25, 27, "Hm.")))
        assert split_segment(segment, timeline, previous=0)[0].speaker == 0

    def test_an_empty_timeline_labels_nothing(self):
        segment = Segment(0, 2, " Hi.", words((0, 1, "Hi.")))
        assert [r.speaker for r in split_segment(segment, Timeline([]))] == [None]


class TestSpeakerNamer:
    def test_speakers_are_numbered_in_the_order_they_first_speak(self):
        namer = SpeakerNamer()
        assert [namer.label(raw) for raw in (3, 0, 3, 7, 0)] == [
            "Speaker 1",
            "Speaker 2",
            "Speaker 1",
            "Speaker 3",
            "Speaker 2",
        ]
        assert namer.labels == ["Speaker 1", "Speaker 2", "Speaker 3"]

    def test_an_unknown_speaker_stays_unlabelled(self):
        namer = SpeakerNamer()
        assert namer.label(None) is None
        assert namer.labels == []


class TestAbsorbMinorSpeakers:
    """On auto, the engine tends to find one speaker too many."""

    def test_a_scrap_of_a_speaker_joins_whoever_surrounds_it(self):
        # The shape measured on a real interview: 181s, 40s, and 1.3s of "someone".
        turns = [Turn(0, 100, 0), Turn(100, 101.3, 9), Turn(101.3, 182, 0), Turn(182, 222, 1)]
        absorbed = absorb_minor_speakers(turns)
        assert {t.speaker for t in absorbed} == {0, 1}
        assert absorbed[1] == Turn(100, 101.3, 0)
        assert [(t.start, t.end) for t in absorbed] == [(t.start, t.end) for t in turns]

    def test_it_goes_to_the_nearest_real_speaker(self):
        turns = [Turn(0, 60, 0), Turn(70, 71, 9), Turn(71.2, 130, 1)]
        assert absorb_minor_speakers(turns)[1].speaker == 1

    def test_a_real_third_voice_is_kept(self):
        turns = [Turn(0, 100, 0), Turn(100, 200, 1), Turn(200, 212, 2)]  # 12s, ~6%
        assert absorb_minor_speakers(turns) == turns

    def test_the_share_floor_scales_with_the_recording(self):
        # 20 seconds is plenty in a short clip, and a scrap in three hours.
        long_talk = [Turn(0, 5000, 0), Turn(5000, 10000, 1), Turn(10000, 10020, 2)]
        assert {t.speaker for t in absorb_minor_speakers(long_talk)} == {0, 1}
        short_talk = [Turn(0, 50, 0), Turn(50, 100, 1), Turn(100, 120, 2)]
        assert {t.speaker for t in absorb_minor_speakers(short_talk)} == {0, 1, 2}

    def test_a_lone_speaker_is_never_absorbed_into_nothing(self):
        assert absorb_minor_speakers([Turn(0, 1.5, 4)]) == [Turn(0, 1.5, 4)]
        tiny = [Turn(0, 1, 0), Turn(1, 1.5, 1)]
        assert {t.speaker for t in absorb_minor_speakers(tiny)} == {0}

    def test_no_turns_is_fine(self):
        assert absorb_minor_speakers([]) == []
