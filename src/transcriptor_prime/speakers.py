"""Reading and renaming the speakers in a saved transcript.

There is deliberately no sidecar file: the ``.txt`` is the only record of who
said what. Everything the "Name speakers" window needs — the current names,
sample quotes, where in the recording each voice can be heard — is parsed back
out of the transcript's own label lines::

    [00:00:07] Speaker 2:

So a transcript can be named, renamed, moved to another machine and renamed
again, and one produced months ago works as well as one produced a minute ago.

No Tk, PyAV or engine import, so all of it is testable on plain text files.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from transcriptor_prime.formatting import RULE, SPEAKERS_PREFIX

#: A paragraph heading that carries a name. Only counts when the line before it
#: is blank (see :func:`_is_label`), so body text can never be mistaken for one.
LABEL_RE = re.compile(r"^\[(\d{2,}):(\d{2}):(\d{2})\] (.+):$")
_TIMECODE_RE = re.compile(r"^\[(\d{2,}):(\d{2}):(\d{2})\]( .+:)?$")

_SOURCE_PREFIX = "Transcript: "
_EOL_RE = re.compile(r"(\r\n|\n|\r)")

MAX_NAME_LENGTH = 60


@dataclass(frozen=True)
class Paragraph:
    start: float  # the block's timecode, in seconds (floored to a whole second)
    end_hint: float | None  # the next block's timecode, if there is one
    speaker: str
    text: str
    continuation: bool  # the previous block was the same speaker, mid-answer


@dataclass(frozen=True)
class SpeakerInfo:
    label: str
    paragraphs: tuple[Paragraph, ...]


@dataclass(frozen=True)
class Parsed:
    path: Path
    source_name: str | None  # the header's "Transcript:" value
    speakers: tuple[SpeakerInfo, ...]  # in order of first appearance


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def parse(path: str | Path) -> Parsed:
    """Read the speakers out of a transcript. Raises ``OSError`` if unreadable.

    A transcript made without speaker identification parses to zero speakers.
    """
    path = Path(path)
    lines = [content for content, _ in _read_lines(path)[0]]
    body_from = _body_start(lines)

    source_name = None
    for line in lines[:body_from]:
        if line.startswith(_SOURCE_PREFIX):
            source_name = line[len(_SOURCE_PREFIX):].strip() or None
            break

    # One pass to find every block heading, labelled or not, so each paragraph
    # knows where the next one starts.
    headings: list[tuple[int, float, str | None]] = []
    for index in range(body_from, len(lines)):
        if index > 0 and lines[index - 1].strip():
            continue
        match = _TIMECODE_RE.match(lines[index])
        if match:
            label = LABEL_RE.match(lines[index])
            headings.append(
                (index, _seconds(match), label.group(4) if label else None)
            )

    by_label: dict[str, list[Paragraph]] = {}
    previous_speaker: str | None = None
    for position, (index, start, label) in enumerate(headings):
        following = headings[position + 1] if position + 1 < len(headings) else None
        if label is not None:
            stop = following[0] if following else len(lines)
            text = " ".join(
                line.strip() for line in lines[index + 1 : stop] if line.strip()
            )
            by_label.setdefault(label, []).append(
                Paragraph(
                    start=start,
                    end_hint=following[1] if following else None,
                    speaker=label,
                    text=text,
                    continuation=label == previous_speaker,
                )
            )
        previous_speaker = label

    return Parsed(
        path=path,
        source_name=source_name,
        speakers=tuple(
            SpeakerInfo(label=label, paragraphs=tuple(paragraphs))
            for label, paragraphs in by_label.items()
        ),
    )


def sample_quotes(info: SpeakerInfo, count: int = 3, max_chars: int = 140) -> list[str]:
    """A few things this speaker said, to recognise them by.

    The opening words come first — that is where people introduce themselves —
    then the longest paragraphs, which carry the most recognisable content.
    """
    paragraphs = [p for p in info.paragraphs if p.text]
    if not paragraphs:
        return []
    chosen = [paragraphs[0]]
    for paragraph in sorted(paragraphs[1:], key=lambda p: len(p.text), reverse=True):
        if len(chosen) >= count:
            break
        chosen.append(paragraph)
    chosen.sort(key=lambda p: p.start)
    return [_truncate(p.text, max_chars) for p in chosen]


def best_clip(info: SpeakerInfo, length: float = 6.0) -> tuple[float, float] | None:
    """``(start, duration)`` of the stretch of audio that best samples this voice.

    Timecodes are floored to a whole second, so a paragraph that opens a turn
    can begin with the tail of the *previous* voice. A continuation paragraph
    sits mid-answer and has no such risk, so those are preferred.
    """
    if not info.paragraphs:
        return None

    def span(paragraph: Paragraph) -> float:
        if paragraph.end_hint is None:
            return length
        return max(0.0, paragraph.end_hint - paragraph.start)

    best = max(
        info.paragraphs,
        key=lambda p: (p.continuation, min(span(p), length), len(p.text)),
    )
    start = best.start
    room = span(best)
    if not best.continuation and room >= 4.0:
        # Step past the floored second rather than risk the previous speaker.
        start += 1.0
        room -= 1.0
    return start, min(length, max(2.0, room))


def find_source(parsed: Parsed, hint: Path | None = None) -> Path | None:
    """The recording a transcript was made from, for playing voice samples.

    ``hint`` is the queue's own record when the file was transcribed this
    session; otherwise look beside the transcript for the name in its header.
    """
    if hint is not None and Path(hint).is_file():
        return Path(hint)
    if parsed.source_name:
        candidate = parsed.path.parent / parsed.source_name
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def clean_name(text: str) -> str:
    """Tidy a typed name. An empty result means "keep the current label"."""
    name = " ".join(str(text).split())  # also removes CR, LF and tabs
    name = name.rstrip(":").rstrip()
    return name[:MAX_NAME_LENGTH].rstrip()


def rename(path: str | Path, mapping: Mapping[str, str]) -> None:
    """Rewrite speaker labels in place: ``{current label: new name}``.

    Labels are matched as exact strings against what is in the file *now*, so
    swapping two names works, and a name full of regex characters is harmless.
    Giving two speakers the same name merges them — useful when the detector
    split one person in two, and not reversible.

    The replacement is atomic: a failure leaves the original untouched.
    """
    path = Path(path)
    lines, bom = _read_lines(path)
    contents = [content for content, _ in lines]
    body_from = _body_start(contents)

    names: list[str] = []
    for index in range(body_from, len(lines)):
        content, eol = lines[index]
        if not _is_label(contents, index):
            continue
        match = LABEL_RE.match(content)
        assert match is not None
        current = match.group(4)
        name = clean_name(mapping.get(current, "")) or current
        if name not in names:
            names.append(name)
        if name != current:
            lines[index] = (f"{content[: match.start(4)]}{name}:", eol)

    _sync_header_line(lines, body_from, names)
    _write_atomically(path, lines, bom)


def sync_header(path: str | Path) -> None:
    """Make the header's ``Speakers:`` line match the labels actually present."""
    rename(path, {})


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _read_lines(path: Path) -> tuple[list[tuple[str, str]], bool]:
    """``[(content, line ending), …]`` plus whether the file began with a BOM.

    Line endings are kept per line so a transcript that has been through
    Notepad (CRLF, BOM) comes back out exactly as its owner left it.
    """
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    pieces = _EOL_RE.split(text)
    lines = [
        (pieces[i], pieces[i + 1] if i + 1 < len(pieces) else "")
        for i in range(0, len(pieces), 2)
    ]
    if lines and lines[-1] == ("", ""):
        lines.pop()
    return lines, bom


def _body_start(contents: list[str]) -> int:
    """Index of the first line after the header's rule (0 if there is no rule)."""
    for index, content in enumerate(contents):
        if content == RULE:
            return index + 1
    return 0


def _is_label(contents: list[str], index: int) -> bool:
    if index > 0 and contents[index - 1].strip():
        return False
    return LABEL_RE.match(contents[index]) is not None


def _seconds(match: re.Match) -> float:
    hours, minutes, seconds = (int(match.group(i)) for i in (1, 2, 3))
    return float(hours * 3600 + minutes * 60 + seconds)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _sync_header_line(
    lines: list[tuple[str, str]], body_from: int, names: list[str]
) -> None:
    """Regenerate the ``Speakers:`` header line from ``names``.

    The line is never *read* for meaning — only replaced — so a name with a
    comma in it cannot confuse anything.
    """
    if not names or body_from == 0:
        return
    wanted = f"{SPEAKERS_PREFIX}{', '.join(names)}"
    header = range(body_from - 1)
    for index in header:
        if lines[index][0].startswith(SPEAKERS_PREFIX.rstrip()):
            lines[index] = (wanted, lines[index][1])
            return
    for index in header:
        if lines[index][0].startswith("Language:"):
            lines.insert(index + 1, (wanted, lines[index][1]))
            return


def _write_atomically(path: Path, lines: list[tuple[str, str]], bom: bool) -> None:
    data = "".join(content + eol for content, eol in lines).encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    temp = path.with_name(path.name + ".rename.tmp")
    try:
        with open(temp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
