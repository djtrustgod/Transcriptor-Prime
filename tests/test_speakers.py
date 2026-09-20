from __future__ import annotations

import os
from pathlib import Path

import pytest

from transcriptor_prime import speakers
from transcriptor_prime.formatting import RULE

HEADER = (
    "Transcript: interview.mp3\n"
    "Duration:   00:02:00\n"
    "Model:      small (int8, CPU)\n"
    "Language:   en (forced)\n"
    "Speakers:   Speaker 1, Speaker 2\n"
    "Generated:  2026-09-19 10:30\n"
    "Created by: Transcriptor Prime 1.0.0\n"
    "\n"
    f"{RULE}\n"
    "\n"
)

BODY = (
    "[00:00:00] Speaker 1:\n"
    "So tell me how you got started in the business.\n"
    "\n"
    "[00:00:07] Speaker 2:\n"
    "Well, it was back in 1998 when I first walked\n"
    "into the shop.\n"
    "\n"
    "[00:00:37] Speaker 2:\n"
    "And that is how the second location opened.\n"
    "\n"
    "[00:01:10] Speaker 1:\n"
    "Right.\n"
    "\n"
)


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "interview.txt"
    path.write_text(HEADER + BODY, encoding="utf-8", newline="\n")
    return path


class TestParse:
    def test_speakers_come_back_in_order_of_first_appearance(self, transcript):
        parsed = speakers.parse(transcript)
        assert [s.label for s in parsed.speakers] == ["Speaker 1", "Speaker 2"]
        assert parsed.source_name == "interview.mp3"

    def test_paragraphs_carry_their_text_and_timing(self, transcript):
        second = speakers.parse(transcript).speakers[1]
        first, continuation = second.paragraphs
        assert first.start == 7
        assert first.end_hint == 37
        assert first.text == "Well, it was back in 1998 when I first walked into the shop."
        assert first.continuation is False
        assert continuation.continuation is True
        assert continuation.end_hint == 70

    def test_the_last_paragraph_has_no_end_hint(self, transcript):
        last = speakers.parse(transcript).speakers[0].paragraphs[-1]
        assert last.text == "Right."
        assert last.end_hint is None

    def test_a_transcript_without_labels_has_no_speakers(self, tmp_path):
        path = tmp_path / "plain.txt"
        path.write_text(
            HEADER.replace("Speakers:   Speaker 1, Speaker 2\n", "")
            + "[00:00:00]\nHello there.\n\n[00:00:30]\nMore.\n\n",
            encoding="utf-8",
        )
        assert speakers.parse(path).speakers == ()

    def test_body_text_shaped_like_a_label_is_not_a_label(self, tmp_path):
        path = tmp_path / "tricky.txt"
        path.write_text(
            HEADER
            + "[00:00:00] Speaker 1:\nThe sign read\n[00:00:05] Closed:\nand that was that.\n\n",
            encoding="utf-8",
        )
        parsed = speakers.parse(path)
        assert [s.label for s in parsed.speakers] == ["Speaker 1"]
        assert "[00:00:05] Closed:" in parsed.speakers[0].paragraphs[0].text

    def test_a_partial_transcript_parses_too(self, tmp_path):
        path = tmp_path / "interview.partial.txt"
        path.write_text(HEADER + BODY[: BODY.index("[00:00:37]")], encoding="utf-8")
        assert [s.label for s in speakers.parse(path).speakers] == [
            "Speaker 1",
            "Speaker 2",
        ]

    def test_hours_past_99_parse(self, tmp_path):
        path = tmp_path / "long.txt"
        path.write_text(HEADER + "[100:00:01] Speaker 1:\nStill going.\n\n", encoding="utf-8")
        assert speakers.parse(path).speakers[0].paragraphs[0].start == 360001


class TestSamples:
    def test_quotes_lead_with_the_opening_words(self, transcript):
        second = speakers.parse(transcript).speakers[1]
        quotes = speakers.sample_quotes(second, count=2)
        assert quotes[0].startswith("Well, it was back in 1998")
        assert len(quotes) == 2

    def test_long_quotes_are_truncated(self, transcript):
        second = speakers.parse(transcript).speakers[1]
        (quote,) = speakers.sample_quotes(second, count=1, max_chars=20)
        assert len(quote) == 20 and quote.endswith("…")

    def test_the_clip_prefers_a_mid_answer_paragraph(self, transcript):
        second = speakers.parse(transcript).speakers[1]
        assert speakers.best_clip(second) == (37.0, 6.0)

    def test_a_turn_opening_clip_steps_past_the_floored_second(self, transcript):
        first = speakers.parse(transcript).speakers[0]
        # Speaker 1 has no continuation paragraph; the 7-second opener is used,
        # starting a second in so the previous voice cannot leak into it.
        assert speakers.best_clip(first) == (1.0, 6.0)

    def test_find_source_prefers_the_hint_then_looks_beside_the_transcript(
        self, transcript, tmp_path
    ):
        parsed = speakers.parse(transcript)
        assert speakers.find_source(parsed) is None

        beside = tmp_path / "interview.mp3"
        beside.write_bytes(b"x")
        assert speakers.find_source(parsed) == beside

        elsewhere = tmp_path / "elsewhere.mp3"
        elsewhere.write_bytes(b"x")
        assert speakers.find_source(parsed, hint=elsewhere) == elsewhere
        assert speakers.find_source(parsed, hint=tmp_path / "gone.mp3") == beside


class TestCleanName:
    @pytest.mark.parametrize(
        "typed,expected",
        [
            ("  Jane   Doe  ", "Jane Doe"),
            ("Jane\r\nDoe\t(host)", "Jane Doe (host)"),
            ("Jane Doe: ", "Jane Doe"),
            ("Dr. O'Neil, Jr.", "Dr. O'Neil, Jr."),
            ("   ", ""),
            ("x" * 80, "x" * 60),
        ],
    )
    def test_clean_name(self, typed, expected):
        assert speakers.clean_name(typed) == expected


class TestRename:
    def test_labels_and_header_are_rewritten(self, transcript):
        speakers.rename(transcript, {"Speaker 1": "Interviewer", "Speaker 2": "Jane Doe"})
        text = transcript.read_text(encoding="utf-8")
        assert "[00:00:00] Interviewer:\n" in text
        assert "[00:00:37] Jane Doe:\n" in text
        assert "Speakers:   Interviewer, Jane Doe\n" in text
        assert "Speaker 1" not in text and "Speaker 2" not in text

    def test_only_label_lines_change(self, transcript):
        before = transcript.read_text(encoding="utf-8").splitlines()
        speakers.rename(transcript, {"Speaker 2": "Jane Doe"})
        after = transcript.read_text(encoding="utf-8").splitlines()
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert len(before) == len(after)
        assert [before[i] for i in changed] == [
            "Speakers:   Speaker 1, Speaker 2",
            "[00:00:07] Speaker 2:",
            "[00:00:37] Speaker 2:",
        ]

    def test_renaming_again_reads_the_current_names(self, transcript):
        speakers.rename(transcript, {"Speaker 2": "Jane Doe"})
        assert [s.label for s in speakers.parse(transcript).speakers] == [
            "Speaker 1",
            "Jane Doe",
        ]
        speakers.rename(transcript, {"Jane Doe": "Jane Smith"})
        assert "Speakers:   Speaker 1, Jane Smith\n" in transcript.read_text("utf-8")

    def test_two_names_can_be_swapped(self, transcript):
        speakers.rename(transcript, {"Speaker 1": "Speaker 2", "Speaker 2": "Speaker 1"})
        text = transcript.read_text(encoding="utf-8")
        assert "[00:00:00] Speaker 2:\n" in text
        assert "[00:00:07] Speaker 1:\n" in text

    def test_the_same_name_twice_merges_two_speakers(self, transcript):
        speakers.rename(transcript, {"Speaker 1": "Jane", "Speaker 2": "Jane"})
        parsed = speakers.parse(transcript)
        assert [s.label for s in parsed.speakers] == ["Jane"]
        assert len(parsed.speakers[0].paragraphs) == 4
        assert "Speakers:   Jane\n" in transcript.read_text("utf-8")

    def test_names_full_of_punctuation_are_literal(self, transcript):
        name = "a.*(b)[c], Jr: PhD"
        speakers.rename(transcript, {"Speaker 1": name})
        assert [s.label for s in speakers.parse(transcript).speakers][0] == name
        speakers.rename(transcript, {name: "Plain"})
        assert "[00:00:00] Plain:\n" in transcript.read_text("utf-8")

    def test_a_blank_name_keeps_the_current_label(self, transcript):
        before = transcript.read_bytes()
        speakers.rename(transcript, {"Speaker 1": "   ", "Speaker 2": ""})
        assert transcript.read_bytes() == before

    def test_crlf_and_bom_survive(self, tmp_path):
        path = tmp_path / "notepad.txt"
        path.write_bytes(b"\xef\xbb\xbf" + (HEADER + BODY).replace("\n", "\r\n").encode())
        speakers.rename(path, {"Speaker 2": "Jane Doe"})
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"[00:00:07] Jane Doe:\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    def test_a_missing_speakers_line_is_added(self, tmp_path):
        path = tmp_path / "old.txt"
        path.write_text(
            HEADER.replace("Speakers:   Speaker 1, Speaker 2\n", "") + BODY,
            encoding="utf-8",
        )
        speakers.sync_header(path)
        assert (
            "Language:   en (forced)\nSpeakers:   Speaker 1, Speaker 2\nGenerated:"
            in path.read_text("utf-8")
        )

    def test_sync_header_drops_a_speaker_who_never_spoke(self, tmp_path):
        path = tmp_path / "phantom.txt"
        path.write_text(
            HEADER.replace("Speaker 1, Speaker 2", "Speaker 1, Speaker 2, Speaker 3")
            + BODY,
            encoding="utf-8",
        )
        speakers.sync_header(path)
        assert "Speakers:   Speaker 1, Speaker 2\n" in path.read_text("utf-8")

    def test_a_transcript_without_labels_is_left_alone(self, tmp_path):
        path = tmp_path / "plain.txt"
        path.write_text("Transcript: a.mp3\n\n" + RULE + "\n\n[00:00:00]\nHi.\n\n", "utf-8")
        before = path.read_bytes()
        speakers.rename(path, {"Speaker 1": "Jane"})
        assert path.read_bytes() == before

    def test_a_failed_replace_leaves_the_original_and_no_litter(
        self, transcript, monkeypatch
    ):
        before = transcript.read_bytes()

        def refuse(src, dst):
            raise PermissionError("file is open in Word")

        monkeypatch.setattr(os, "replace", refuse)
        with pytest.raises(PermissionError):
            speakers.rename(transcript, {"Speaker 1": "Interviewer"})
        assert transcript.read_bytes() == before
        assert [p.name for p in transcript.parent.iterdir()] == ["interview.txt"]
