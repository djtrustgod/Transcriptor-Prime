"""Deciding who said each word.

Speaker identification produces *turns* — "cluster 2 spoke from 12.4s to 31.0s"
— on its own clock, and Whisper produces *segments* of text on another. This
module lines the two up. It imports neither engine, so the rules can be tested
with hand-written numbers.

The work is done one Whisper segment at a time, which is what lets the
transcript keep streaming to disk while it is labelled.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Turn:
    """One stretch of a single voice, as the diarizer reported it."""

    start: float
    end: float
    speaker: int  # the diarizer's raw cluster id — arbitrary, not yet "Speaker N"


@dataclass(frozen=True)
class Run:
    """Consecutive words from one speaker inside a Whisper segment."""

    start: float
    end: float
    text: str
    speaker: int | None


# A word this far from any turn is more likely a continuation of whoever was
# just talking than the property of a turn a second away.
_NEAREST_LIMIT = 1.0

# Timestamp jitter at a change of speaker typically mislabels a single short
# word at the edge of a segment.
_EDGE_WORD_MAX = 0.6

# An "A b A" flip inside one segment is noise when b is this small. Real
# interjections ("Right.") arrive as their own Whisper segment and are never
# touched, because nothing here looks across segments.
_FLIP_MAX_WORDS = 2
_FLIP_MAX_SECONDS = 1.0


def absorb_minor_speakers(
    turns: Sequence[Turn], min_seconds: float = 3.0, min_share: float = 0.015
) -> list[Turn]:
    """Fold a "speaker" who barely exists into whoever was talking around them.

    Left to count speakers for itself, the engine often splits off a second or
    two of one person — a laugh, a cough, a word shouted over music — as a
    speaker of their own. A voice with under ``min_seconds`` of speech, or
    under ``min_share`` of all the speech, is treated as such a scrap: each of
    its turns goes to the nearest turn of a real speaker. Someone who genuinely
    says three words in an hour is lost to this; that is the trade.

    Only for when the engine chose the count. A count the user gave is honoured.
    """
    totals: dict[int, float] = {}
    for turn in turns:
        totals[turn.speaker] = totals.get(turn.speaker, 0.0) + (turn.end - turn.start)
    if len(totals) < 2:
        return list(turns)

    floor = max(min_seconds, min_share * sum(totals.values()))
    minor = {speaker for speaker, total in totals.items() if total < floor}
    # Never absorb everyone: the biggest voice always stays.
    minor.discard(max(totals, key=totals.get))
    if not minor:
        return list(turns)

    major = [turn for turn in turns if turn.speaker not in minor]
    absorbed = []
    for turn in turns:
        if turn.speaker in minor:
            nearest = min(major, key=lambda m: _gap(m, turn.start, turn.end))
            turn = Turn(turn.start, turn.end, nearest.speaker)
        absorbed.append(turn)
    return absorbed


class Timeline:
    """The diarizer's turns, indexed for "who is talking during [start, end]?"."""

    def __init__(self, turns: Sequence[Turn]) -> None:
        self._turns = sorted(turns, key=lambda t: (t.start, t.end))
        self._starts = [t.start for t in self._turns]
        # Overlapped speech yields overlapping turns, so a turn that started
        # long ago may still be running. The running maximum of `end` is what
        # tells the backwards scan when it can stop.
        self._max_end: list[float] = []
        running = float("-inf")
        for turn in self._turns:
            running = max(running, turn.end)
            self._max_end.append(running)

    def __bool__(self) -> bool:
        return bool(self._turns)

    def speaker_at(
        self, start: float, end: float, previous: int | None = None
    ) -> int | None:
        """The speaker with the most airtime in ``[start, end]``.

        A tie goes to ``previous`` (whoever was already talking), then to the
        earlier turn. With no overlap at all, the nearest turn wins, unless it
        is over a second away and there is a ``previous`` to inherit.
        """
        if not self._turns:
            return None
        if end <= start:
            end = start + 0.01

        overlap: dict[int, float] = {}
        first_seen: dict[int, float] = {}
        index = bisect_right(self._starts, end) - 1
        while index >= 0 and self._max_end[index] > start:
            turn = self._turns[index]
            shared = min(end, turn.end) - max(start, turn.start)
            if shared > 0:
                overlap[turn.speaker] = overlap.get(turn.speaker, 0.0) + shared
                first_seen[turn.speaker] = turn.start
            index -= 1

        if overlap:
            best = max(overlap.values())
            tied = [s for s, amount in overlap.items() if best - amount < 1e-9]
            if previous in tied:
                return previous
            return min(tied, key=lambda s: first_seen[s])

        nearest = min(self._turns, key=lambda t: _gap(t, start, end))
        if previous is not None and _gap(nearest, start, end) > _NEAREST_LIMIT:
            return previous
        return nearest.speaker


def _gap(turn: Turn, start: float, end: float) -> float:
    """Distance between a turn and an interval that does not overlap it."""
    if turn.end <= start:
        return start - turn.end
    if turn.start >= end:
        return turn.start - end
    return 0.0


def split_segment(
    segment, timeline: Timeline, previous: int | None = None
) -> list[Run]:
    """Break one Whisper segment wherever the speaker changes between words.

    ``segment`` needs ``start``, ``end``, ``text`` and, when Whisper was asked
    for word timestamps, ``words`` (each with ``start``, ``end``, ``word``).
    Without words the whole segment gets one label.
    """
    words = [w for w in (getattr(segment, "words", None) or []) if w.word.strip()]
    if not words:
        speaker = timeline.speaker_at(segment.start, segment.end, previous)
        return [Run(segment.start, segment.end, segment.text.strip(), speaker)]

    labels: list[int | None] = []
    for word in words:
        previous = timeline.speaker_at(word.start, word.end, previous)
        labels.append(previous)

    _snap_edges(words, labels)
    _smooth_flips(words, labels)

    runs: list[Run] = []
    begin = 0
    for index in range(1, len(words) + 1):
        if index == len(words) or labels[index] != labels[begin]:
            chunk = words[begin:index]
            runs.append(
                Run(
                    start=chunk[0].start,
                    end=chunk[-1].end,
                    # Whisper's words carry their own leading space.
                    text="".join(w.word for w in chunk).strip(),
                    speaker=labels[begin],
                )
            )
            begin = index
    return runs


def _snap_edges(words, labels: list[int | None]) -> None:
    """Relabel a lone short first/last word that disagrees with all the rest."""
    if len(words) < 3:
        return
    for edge, inner in ((0, slice(1, None)), (-1, slice(None, -1))):
        rest = set(labels[inner])
        if len(rest) != 1:
            continue
        (majority,) = rest
        word = words[edge]
        if labels[edge] != majority and word.end - word.start < _EDGE_WORD_MAX:
            labels[edge] = majority


def _smooth_flips(words, labels: list[int | None]) -> None:
    """Fold a tiny ``A b A`` excursion inside one segment back into ``A``."""
    index = 1
    while index < len(labels) - 1:
        if labels[index] == labels[index - 1]:
            index += 1
            continue
        stop = index
        while stop < len(labels) and labels[stop] == labels[index]:
            stop += 1
        small = (
            stop - index <= _FLIP_MAX_WORDS
            and words[stop - 1].end - words[index].start < _FLIP_MAX_SECONDS
        )
        if small and stop < len(labels) and labels[stop] == labels[index - 1]:
            for position in range(index, stop):
                labels[position] = labels[index - 1]
        index = stop


class SpeakerNamer:
    """Turns raw cluster ids into "Speaker 1", "Speaker 2"… by first appearance.

    The diarizer's ids are arbitrary, so numbering in the order people actually
    speak is what makes "Speaker 1" mean something to the reader: it is whoever
    opens the recording.
    """

    def __init__(self) -> None:
        self._labels: dict[int, str] = {}

    def label(self, raw: int | None) -> str | None:
        if raw is None:
            return None
        if raw not in self._labels:
            self._labels[raw] = f"Speaker {len(self._labels) + 1}"
        return self._labels[raw]

    @property
    def labels(self) -> list[str]:
        """Every label handed out so far, in order of first appearance."""
        return list(self._labels.values())
