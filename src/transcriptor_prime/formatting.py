"""Transcript text formatting.

Deliberately free of any faster-whisper, PyAV or Tk import so the timecode and
paragraph-grouping rules can be unit tested without downloading a model.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from datetime import datetime

from transcriptor_prime import APP_LABEL

RULE = "-" * 60


def format_timecode(seconds: float) -> str:
    """Render a position in the recording as ``HH:MM:SS``.

    Hours are not capped at 24 (irrelevant here) and negative inputs clamp to
    zero, which can happen if a decoder reports a slightly negative timestamp
    for the first frame.
    """
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_duration(seconds: float) -> str:
    """Human-friendly duration, e.g. ``2h 47m 12s`` or ``47m 12s``."""
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


@dataclass
class Block:
    """One timestamped paragraph of the finished transcript."""

    start: float
    end: float
    text: str

    def render(self, wrap_width: int = 100) -> str:
        body = self.text
        if wrap_width > 0:
            body = "\n".join(
                textwrap.wrap(
                    body,
                    width=wrap_width,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
        return f"[{format_timecode(self.start)}]\n{body}\n"


@dataclass
class ParagraphBuilder:
    """Groups Whisper segments into timestamped paragraphs.

    A block is closed once its span reaches ``interval_seconds``, so the
    transcript carries one ``[HH:MM:SS]`` marker roughly every interval. The
    marker uses the *actual* start of the block's first segment rather than a
    rounded boundary, so it always points at real spoken content instead of
    landing in a stretch of silence.
    """

    interval_seconds: float = 30.0
    _start: float | None = field(default=None, init=False)
    _end: float = field(default=0.0, init=False)
    _parts: list[str] = field(default_factory=list, init=False)

    def add(self, start: float, end: float, text: str) -> Block | None:
        """Buffer one segment; return a finished :class:`Block` if it closed one."""
        text = text.strip()
        if not text:
            # Whisper occasionally emits an empty/whitespace segment. Skipping
            # it here keeps blank lines out of the paragraph body.
            return None

        if self._start is None:
            self._start = start
        self._end = max(self._end, end)
        self._parts.append(text)

        if self._end - self._start >= self.interval_seconds:
            return self._close()
        return None

    def flush(self) -> Block | None:
        """Emit whatever is buffered; call once at the end of the stream."""
        if self._start is None:
            return None
        return self._close()

    def _close(self) -> Block:
        assert self._start is not None
        block = Block(start=self._start, end=self._end, text=" ".join(self._parts))
        self._start = None
        self._end = 0.0
        self._parts = []
        return block


def build_header(
    *,
    source_name: str,
    duration: float,
    model: str,
    language: str,
    language_detected: bool,
    language_probability: float | None,
    generated_at: datetime,
    app_label: str = APP_LABEL,
) -> str:
    """The preamble written at the top of every transcript.

    ``app_label`` records which build produced the file, so a transcript found
    months later can be traced back to a version.
    """
    if language_detected and language_probability is not None:
        lang_line = f"{language} (detected, {language_probability:.2f})"
    elif language_detected:
        lang_line = f"{language} (detected)"
    else:
        lang_line = f"{language} (forced)"

    lines = [
        f"Transcript: {source_name}",
        f"Duration:   {format_timecode(duration)}",
        f"Model:      {model} (int8, CPU)",
        f"Language:   {lang_line}",
        f"Generated:  {generated_at.strftime('%Y-%m-%d %H:%M')}",
        f"Created by: {app_label}",
        "",
        RULE,
        "",
    ]
    return "\n".join(lines) + "\n"
