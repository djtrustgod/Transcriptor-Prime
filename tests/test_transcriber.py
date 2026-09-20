"""Worker tests.

A stub stands in for ``faster_whisper.WhisperModel`` so the incremental write,
atomic rename, cancellation and progress behaviour can be exercised in
milliseconds without downloading model weights. The one genuine end-to-end run
lives at the bottom behind the ``slow`` marker.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from inspect import signature
from pathlib import Path

import pytest

from transcriptor_prime import diarizer, transcriber
from transcriptor_prime.attribution import Turn
from transcriptor_prime.formatting import RULE
from transcriptor_prime.transcriber import (
    CANCELLED,
    COMPLETED,
    FAILED,
    BatchFinished,
    Done,
    Failed,
    FileFinished,
    FileStarted,
    Job,
    Progress,
    Status,
)


@dataclass
class FakeWord:
    start: float
    end: float
    word: str  # carries its own leading space, as Whisper's do


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str
    words: list[FakeWord] | None = None  # only with word_timestamps=True


@dataclass
class FakeInfo:
    duration: float
    language: str = "en"
    language_probability: float = 0.99


class FakeModel:
    """Yields canned segments, optionally pausing so a cancel can land."""

    def __init__(self, segments, duration, on_segment=None, fail_for=(), **kwargs):
        self.segments = segments
        self.duration = duration
        self.on_segment = on_segment
        self.fail_for = set(fail_for)
        self.kwargs = kwargs
        self.transcribe_kwargs: dict = {}
        self.sources: list[str] = []

    def transcribe(self, path, **kwargs):
        self.transcribe_kwargs = kwargs
        self.sources.append(Path(path).name)
        name = Path(path).name
        duration = self.duration
        if callable(duration):
            duration = duration(name)

        def generate():
            for index, segment in enumerate(self.segments):
                if name in self.fail_for:
                    raise RuntimeError(f"decoder blew up on {name}")
                if self.on_segment:
                    self.on_segment(index, name)
                yield segment

        return generate(), FakeInfo(duration=duration)


@pytest.fixture
def install_model(monkeypatch):
    """Patch ``faster_whisper.WhisperModel`` and hand back the constructed stub.

    ``holder["calls"]`` counts constructions, which is how the batch tests pin
    "the model is loaded once for the whole queue".
    """
    import faster_whisper

    holder: dict = {"calls": 0}

    def install(segments, duration, on_segment=None, fail_for=()):
        # FakeModel calls the hook as (index, name). The single-file tests'
        # hooks take only an index, so adapt on arity rather than churn every
        # existing caller.
        hook = on_segment
        if on_segment is not None and len(signature(on_segment).parameters) == 1:
            hook = lambda index, name: on_segment(index)  # noqa: E731

        def factory(*args, **kwargs):
            model = FakeModel(segments, duration, hook, fail_for, **kwargs)
            holder["calls"] += 1
            holder["model"] = model
            holder["init_args"] = (args, kwargs)
            return model

        monkeypatch.setattr(faster_whisper, "WhisperModel", factory)
        return holder

    return install


def make_job(tmp_path: Path, **overrides) -> Job:
    source = tmp_path / "recording.mp3"
    source.write_bytes(b"not really audio, the model is stubbed")
    defaults = dict(
        source=source,
        output=tmp_path / "recording.txt",
        model="tiny",
        language="auto",
        interval_seconds=30.0,
        wrap_width=0,
        cpu_threads=4,
        duration=120.0,
    )
    defaults.update(overrides)
    return Job(**defaults)


def run(job: Job, cancel: threading.Event | None = None) -> list:
    events: list = []
    transcriber.transcribe(job, events.append, cancel or threading.Event())
    return events


SEGMENTS = [
    FakeSegment(0.0, 15.0, "First half of the opening paragraph."),
    FakeSegment(15.0, 31.0, "Second half of the opening paragraph."),
    FakeSegment(31.0, 50.0, "The middle section begins here."),
    FakeSegment(50.0, 70.0, "And it continues for a while."),
    FakeSegment(70.0, 95.0, "A final thought to close on."),
]


class TestHappyPath:
    def test_writes_the_transcript_and_reports_done(self, tmp_path, install_model):
        install_model(SEGMENTS, duration=95.0)
        job = make_job(tmp_path)

        events = run(job)

        done = [e for e in events if isinstance(e, Done)]
        assert len(done) == 1
        assert done[0].partial is False
        assert done[0].output == job.output
        assert job.output.exists()
        assert not any(isinstance(e, Failed) for e in events)

    def test_no_part_file_survives_a_successful_run(self, tmp_path, install_model):
        install_model(SEGMENTS, duration=95.0)
        job = make_job(tmp_path)
        run(job)
        assert list(tmp_path.glob("*.part")) == []

    def test_output_contains_header_and_timecoded_paragraphs(
        self, tmp_path, install_model
    ):
        install_model(SEGMENTS, duration=95.0)
        job = make_job(tmp_path)
        run(job)

        text = job.output.read_text(encoding="utf-8")
        assert "Transcript: recording.mp3" in text
        assert "Model:      tiny (int8, CPU)" in text
        assert "Language:   en (detected, 0.99)" in text
        # 30s interval over 95s of audio: blocks open at 0s, 31s and 70s.
        assert "[00:00:00]" in text
        assert "[00:00:31]" in text
        assert "[00:01:10]" in text
        assert "First half of the opening paragraph. Second half" in text
        assert "A final thought to close on." in text

    @pytest.mark.parametrize(
        "interval,expected_blocks",
        [
            (15.0, 5),  # every segment closes its own block
            (30.0, 3),  # blocks open at 0s, 31s, 70s
            (90.0, 1),  # the whole 95s of audio in one paragraph
        ],
    )
    def test_interval_controls_the_paragraph_count(
        self, tmp_path, install_model, interval, expected_blocks
    ):
        install_model(SEGMENTS, duration=95.0)
        job = make_job(tmp_path, interval_seconds=interval)
        run(job)
        text = job.output.read_text(encoding="utf-8")
        assert text.count("\n[00:") == expected_blocks

    def test_progress_events_advance_to_completion(self, tmp_path, install_model):
        install_model(SEGMENTS, duration=95.0)
        job = make_job(tmp_path)

        progress = [e for e in run(job) if isinstance(e, Progress)]

        assert progress, "expected at least one progress event"
        assert progress[-1].fraction == pytest.approx(1.0)
        fractions = [p.fraction for p in progress]
        assert fractions == sorted(fractions)

    def test_language_is_forced_when_not_auto(self, tmp_path, install_model):
        holder = install_model(SEGMENTS, duration=95.0)
        run(make_job(tmp_path, language="fr"))
        assert holder["model"].transcribe_kwargs["language"] == "fr"

    def test_auto_language_passes_none_to_whisper(self, tmp_path, install_model):
        holder = install_model(SEGMENTS, duration=95.0)
        run(make_job(tmp_path, language="auto"))
        assert holder["model"].transcribe_kwargs["language"] is None

    def test_vad_is_enabled(self, tmp_path, install_model):
        """VAD is what keeps long recordings fast and silence-hallucination free."""
        holder = install_model(SEGMENTS, duration=95.0)
        run(make_job(tmp_path))
        assert holder["model"].transcribe_kwargs["vad_filter"] is True

    def test_model_loads_on_cpu_with_int8_and_the_shared_cache(
        self, tmp_path, install_model
    ):
        holder = install_model(SEGMENTS, duration=95.0)
        run(make_job(tmp_path, cpu_threads=6))
        _, kwargs = holder["init_args"]
        assert kwargs["device"] == "cpu"
        assert kwargs["compute_type"] == "int8"
        assert kwargs["cpu_threads"] == 6
        assert "models" in kwargs["download_root"]


class TestCancellation:
    def test_cancel_mid_stream_keeps_a_partial_transcript(self, tmp_path, install_model):
        cancel = threading.Event()
        install_model(SEGMENTS, duration=95.0, on_segment=lambda i: cancel.set() if i == 2 else None)
        job = make_job(tmp_path)

        events = run(job, cancel)

        done = [e for e in events if isinstance(e, Done)][0]
        assert done.partial is True
        assert done.output == tmp_path / "recording.partial.txt"
        assert done.output.exists()
        assert not job.output.exists()

        text = done.output.read_text(encoding="utf-8")
        assert "First half of the opening paragraph." in text
        assert "A final thought to close on." not in text

    def test_cancelled_run_still_flushes_the_open_paragraph(
        self, tmp_path, install_model
    ):
        """Text buffered in the current block must not be dropped on cancel."""
        cancel = threading.Event()
        install_model(
            SEGMENTS, duration=95.0, on_segment=lambda i: cancel.set() if i == 3 else None
        )
        job = make_job(tmp_path)
        events = run(job, cancel)

        text = [e for e in events if isinstance(e, Done)][0].output.read_text("utf-8")
        # Segment 2 opened a block that never reached the 30s interval.
        assert "The middle section begins here." in text

    def test_cancel_before_the_model_loads_stops_immediately(
        self, tmp_path, install_model
    ):
        cancel = threading.Event()
        cancel.set()
        install_model(SEGMENTS, duration=95.0)
        job = make_job(tmp_path)

        events = run(job, cancel)

        assert [e for e in events if isinstance(e, Done)][0].partial is True
        assert not job.output.exists()


class TestFailures:
    def test_model_error_becomes_a_failed_event(self, tmp_path, monkeypatch):
        import faster_whisper

        def explode(*args, **kwargs):
            raise RuntimeError("could not load weights")

        monkeypatch.setattr(faster_whisper, "WhisperModel", explode)
        events = run(make_job(tmp_path))

        failed = [e for e in events if isinstance(e, Failed)]
        assert len(failed) == 1
        assert "could not load weights" in failed[0].message

    def test_failure_leaves_no_part_file_behind(self, tmp_path, install_model):
        def boom(index):
            raise RuntimeError("decoder blew up")

        install_model(SEGMENTS, duration=95.0, on_segment=boom)
        job = make_job(tmp_path)

        assert any(isinstance(e, Failed) for e in run(job))
        assert list(tmp_path.glob("*.part")) == []
        assert not job.output.exists()

    def test_network_errors_get_a_readable_explanation(self, tmp_path, monkeypatch):
        import faster_whisper

        def explode(*args, **kwargs):
            raise OSError("Failed to resolve 'huggingface.co'")

        monkeypatch.setattr(faster_whisper, "WhisperModel", explode)
        message = [e for e in run(make_job(tmp_path)) if isinstance(e, Failed)][0].message
        assert "internet connection" in message

    def test_status_events_narrate_the_run(self, tmp_path, install_model):
        install_model(SEGMENTS, duration=95.0)
        messages = " | ".join(
            e.message for e in run(make_job(tmp_path)) if isinstance(e, Status)
        )
        assert "Detected language: en" in messages
        assert "Transcribing" in messages


def make_jobs(tmp_path: Path, names, **overrides) -> list[Job]:
    """One job per name, each with its own source and output beside it."""
    jobs = []
    for name in names:
        source = tmp_path / name
        source.write_bytes(b"not really audio, the model is stubbed")
        jobs.append(
            make_job(
                tmp_path,
                source=source,
                output=source.with_suffix(".txt"),
                **overrides,
            )
        )
    return jobs


def run_batch(jobs, cancel: threading.Event | None = None) -> list:
    events: list = []
    transcriber.transcribe_batch(jobs, events.append, cancel or threading.Event())
    return events


def only(events, kind):
    return [e for e in events if isinstance(e, kind)]


class TestBatch:
    def test_the_model_is_loaded_once_for_the_whole_batch(
        self, tmp_path, install_model
    ):
        """The point of the batch path: a WhisperModel costs seconds to build."""
        holder = install_model(SEGMENTS, duration=95.0)
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3", "c.mp3"])

        run_batch(jobs)

        assert holder["calls"] == 1
        assert all(job.output.exists() for job in jobs)

    def test_every_file_gets_a_started_and_a_finished_event(
        self, tmp_path, install_model
    ):
        install_model(SEGMENTS, duration=95.0)
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3", "c.mp3"])

        events = run_batch(jobs)

        assert [e.index for e in only(events, FileStarted)] == [0, 1, 2]
        finished = only(events, FileFinished)
        assert [e.index for e in finished] == [0, 1, 2]
        assert all(e.status == COMPLETED for e in finished)
        assert [e.source.name for e in finished] == ["a.mp3", "b.mp3", "c.mp3"]

    def test_the_batch_summary_counts_every_file(self, tmp_path, install_model):
        install_model(SEGMENTS, duration=95.0)
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3"])

        summary = only(run_batch(jobs), BatchFinished)

        assert len(summary) == 1
        assert (summary[0].completed, summary[0].failed) == (2, 0)
        assert summary[0].skipped == 0
        assert summary[0].outputs == tuple(job.output for job in jobs)

    def test_a_failing_file_does_not_stop_the_batch(self, tmp_path, install_model):
        install_model(SEGMENTS, duration=95.0, fail_for={"b.mp3"})
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3", "c.mp3"])

        events = run_batch(jobs)

        finished = only(events, FileFinished)
        assert [e.status for e in finished] == [COMPLETED, FAILED, COMPLETED]
        assert "decoder blew up" in finished[1].message
        assert finished[1].output is None

        summary = only(events, BatchFinished)[0]
        assert (summary.completed, summary.failed) == (2, 1)
        assert jobs[0].output.exists() and jobs[2].output.exists()
        assert not jobs[1].output.exists()
        assert list(tmp_path.glob("*.part")) == []

    def test_a_failing_file_raises_no_failed_event(self, tmp_path, install_model):
        """`Failed` would unlock the GUI form while the worker is still running."""
        install_model(SEGMENTS, duration=95.0, fail_for={"a.mp3"})

        events = run_batch(make_jobs(tmp_path, ["a.mp3", "b.mp3"]))

        assert only(events, Failed) == []
        assert only(events, Done) == []

    def test_cancel_mid_batch_skips_the_rest(self, tmp_path, install_model):
        cancel = threading.Event()
        install_model(
            SEGMENTS,
            duration=95.0,
            on_segment=lambda i, name: (
                cancel.set() if name == "b.mp3" and i == 2 else None
            ),
        )
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3", "c.mp3"])

        events = run_batch(jobs, cancel)

        finished = only(events, FileFinished)
        assert [e.status for e in finished] == [COMPLETED, CANCELLED]
        # The third file must never even start.
        assert [e.index for e in only(events, FileStarted)] == [0, 1]

        summary = only(events, BatchFinished)[0]
        assert (summary.completed, summary.cancelled, summary.skipped) == (1, 1, 1)
        assert jobs[0].output.exists()
        assert (tmp_path / "b.partial.txt").exists()
        assert not jobs[2].output.exists()

    def test_cancel_before_the_batch_starts_runs_nothing(
        self, tmp_path, install_model
    ):
        cancel = threading.Event()
        cancel.set()
        install_model(SEGMENTS, duration=95.0)
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3"])

        events = run_batch(jobs, cancel)

        assert only(events, FileStarted) == []
        assert only(events, BatchFinished)[0].skipped == 2
        assert not any(job.output.exists() for job in jobs)

    def test_a_model_load_failure_reports_one_fatal_summary(
        self, tmp_path, monkeypatch
    ):
        import faster_whisper

        def explode(*args, **kwargs):
            raise OSError("Failed to resolve 'huggingface.co'")

        monkeypatch.setattr(faster_whisper, "WhisperModel", explode)
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3"])

        events = run_batch(jobs)

        assert only(events, FileStarted) == []
        assert only(events, Failed) == []  # rides on BatchFinished.message instead
        summary = only(events, BatchFinished)[0]
        assert summary.skipped == 2
        assert "internet connection" in summary.message

    def test_an_empty_batch_finishes_immediately(self, tmp_path, install_model):
        install_model(SEGMENTS, duration=95.0)
        events = run_batch([])
        assert len(only(events, BatchFinished)) == 1
        assert only(events, BatchFinished)[0].skipped == 0

    def test_a_batch_of_one_writes_what_transcribe_writes(
        self, tmp_path, install_model
    ):
        install_model(SEGMENTS, duration=95.0)
        job = make_jobs(tmp_path, ["solo.mp3"])[0]

        run_batch([job])

        assert "Transcript: solo.mp3" in job.output.read_text(encoding="utf-8")


class TestBatchProgress:
    def test_batch_fraction_spans_every_file(self, tmp_path, install_model, monkeypatch):
        """Two files of 100s and 300s: finishing the first is 25% overall."""
        monkeypatch.setattr(transcriber, "_UPDATE_INTERVAL", 0.0)
        install_model(SEGMENTS, duration=lambda name: 100.0 if name == "a.mp3" else 300.0)
        jobs = [
            make_jobs(tmp_path, ["a.mp3"], duration=100.0)[0],
            make_jobs(tmp_path, ["b.mp3"], duration=300.0)[0],
        ]

        progress = only(run_batch(jobs), Progress)

        assert progress[0].batch_total == pytest.approx(400.0)
        # The last event of file 0 is emitted with audio_done == its total.
        first_file_end = [p for p in progress if p.file_index == 0][-1]
        assert first_file_end.batch_fraction == pytest.approx(0.25)
        assert progress[-1].batch_fraction == pytest.approx(1.0)

        fractions = [p.batch_fraction for p in progress]
        assert fractions == sorted(fractions)

    def test_overshoot_is_clamped_when_whisper_disagrees_with_the_probe(
        self, tmp_path, install_model, monkeypatch
    ):
        """PyAV and Whisper disagree on VBR MP3; the overall bar must not creep."""
        monkeypatch.setattr(transcriber, "_UPDATE_INTERVAL", 0.0)
        # Whisper reports 200s of audio for a file the probe measured at 100s.
        install_model(SEGMENTS, duration=200.0)
        jobs = [
            make_jobs(tmp_path, ["a.mp3"], duration=100.0)[0],
            make_jobs(tmp_path, ["b.mp3"], duration=100.0)[0],
        ]

        progress = only(run_batch(jobs), Progress)

        assert all(p.batch_fraction <= 1.0 for p in progress)
        assert [p for p in progress if p.file_index == 0][-1].batch_done == (
            pytest.approx(100.0)
        )

    def test_unknown_durations_fall_back_to_counting_files(
        self, tmp_path, install_model
    ):
        install_model(SEGMENTS, duration=95.0)
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3"], duration=0.0)

        progress = only(run_batch(jobs), Progress)

        assert progress[0].batch_total == 0.0
        assert progress[-1].batch_fraction == pytest.approx(1.0)
        assert [p.batch_fraction for p in progress] == sorted(
            p.batch_fraction for p in progress
        )

    def test_a_single_file_run_carries_no_batch_context(self, tmp_path, install_model):
        """`transcribe()` must keep emitting exactly what it always has."""
        install_model(SEGMENTS, duration=95.0)

        progress = [e for e in run(make_job(tmp_path)) if isinstance(e, Progress)]

        assert all(p.file_count == 1 and p.batch_total == 0.0 for p in progress)
        assert progress[-1].batch_fraction == pytest.approx(1.0)


class FakeDiarizer:
    """Stands in for the speaker engine's process boundary."""

    def __init__(self, turns=(), error=None, on_run=None):
        self.turns = list(turns)
        self.error = error
        self.on_run = on_run
        self.calls: list[dict] = []

    def run(self, source, *, num_speakers, threads, duration, cancel, on_progress):
        self.calls.append(
            dict(source=Path(source).name, num_speakers=num_speakers, threads=threads)
        )
        on_progress(0, 0)
        on_progress(5, 10)
        if self.on_run:
            self.on_run(cancel)
        if cancel.is_set():
            raise diarizer.Cancelled()
        if self.error:
            raise self.error
        return list(self.turns)


@pytest.fixture
def install_diarizer(monkeypatch):
    holder: dict = {"prepared": 0}

    def install(turns=(), error=None, on_run=None, prepare_error=None):
        instance = FakeDiarizer(turns, error, on_run)
        holder["diarizer"] = instance

        def prepare(emit_status):
            holder["prepared"] += 1
            if prepare_error:
                raise prepare_error
            emit_status("Speaker identification ready.")
            return instance

        monkeypatch.setattr(diarizer, "prepare", prepare)
        # Progress is throttled by wall clock; the fake reports instantly.
        monkeypatch.setattr(transcriber, "_UPDATE_INTERVAL", 0.0)
        return holder

    return install


def spoken(start: float, *texts: str, step: float = 1.0) -> FakeSegment:
    """A segment with one evenly spaced word per entry in ``texts``."""
    words = [
        FakeWord(start + i * step, start + (i + 1) * step, f" {text}")
        for i, text in enumerate(texts)
    ]
    return FakeSegment(start, start + len(texts) * step, " " + " ".join(texts), words)


# Raw ids are deliberately out of order and non-contiguous, as the engine's are.
INTERVIEW_TURNS = [Turn(0.0, 4.0, 7), Turn(4.0, 40.0, 2), Turn(40.0, 44.0, 7)]
INTERVIEW = [
    spoken(0.0, "How", "did", "it", "start?"),
    spoken(4.0, "Well,", "back", "in", "1998."),
    spoken(8.0, *["word"] * 31),  # a long answer, past the 30s interval
    spoken(40.0, "I", "see."),
]


class TestSpeakerIdentification:
    def test_the_transcript_names_who_said_what(self, tmp_path, install_model, install_diarizer):
        install_model(INTERVIEW, duration=44.0)
        install_diarizer(INTERVIEW_TURNS)
        job = make_job(tmp_path, identify_speakers=True, duration=44.0)

        run(job)

        text = job.output.read_text(encoding="utf-8")
        body = text.split(RULE, 1)[1]
        assert "[00:00:00] Speaker 1:\nHow did it start?\n" in body
        assert "[00:00:04] Speaker 2:\nWell, back in 1998. word" in body
        assert "[00:00:40] Speaker 1:\nI see.\n" in body
        assert "Speakers:   Speaker 1, Speaker 2\n" in text

    def test_speakers_are_numbered_by_who_talks_first(self, tmp_path, install_model, install_diarizer):
        install_model(INTERVIEW, duration=44.0)
        install_diarizer(INTERVIEW_TURNS)  # raw id 7 opens, so 7 is "Speaker 1"
        job = make_job(tmp_path, identify_speakers=True)
        run(job)
        first_label = job.output.read_text("utf-8").split(RULE, 1)[1].split("\n")[2]
        assert first_label == "[00:00:00] Speaker 1:"

    def test_a_segment_is_split_where_the_voice_changes(self, tmp_path, install_model, install_diarizer):
        install_model([spoken(0.0, "Ready?", "Yes,", "I", "am.")], duration=4.0)
        install_diarizer([Turn(0.0, 1.0, 0), Turn(1.0, 4.0, 1)])
        # A stated count: on auto, one second of a voice would be absorbed as a scrap.
        job = make_job(tmp_path, identify_speakers=True, num_speakers=2)
        run(job)
        body = job.output.read_text("utf-8").split(RULE, 1)[1]
        assert "[00:00:00] Speaker 1:\nReady?\n\n[00:00:01] Speaker 2:\nYes, I am.\n" in body

    def test_a_long_answer_repeats_the_name_each_interval(self, tmp_path, install_model, install_diarizer):
        answer = [spoken(4.0 + 10 * n, *["word"] * 10) for n in range(7)]
        install_model([spoken(0.0, "How", "did", "it", "start?"), *answer], duration=74.0)
        install_diarizer([Turn(0.0, 4.0, 0), Turn(4.0, 74.0, 1)])
        job = make_job(tmp_path, identify_speakers=True)
        run(job)
        labels = [
            line for line in job.output.read_text("utf-8").splitlines()
            if line.startswith("[") and line.endswith(":")
        ]
        assert labels == [
            "[00:00:00] Speaker 1:",
            "[00:00:04] Speaker 2:",
            "[00:00:34] Speaker 2:",
            "[00:01:04] Speaker 2:",
        ]

    def test_word_timestamps_are_requested_only_with_speakers(self, tmp_path, install_model, install_diarizer):
        holder = install_model(INTERVIEW, duration=44.0)
        install_diarizer(INTERVIEW_TURNS)
        run(make_job(tmp_path, identify_speakers=True))
        assert holder["model"].transcribe_kwargs["word_timestamps"] is True

    def test_the_off_path_never_touches_the_engine(self, tmp_path, install_model, install_diarizer):
        holder = install_model(SEGMENTS, duration=95.0)
        diar = install_diarizer(INTERVIEW_TURNS)
        job = make_job(tmp_path)
        run(job)
        assert diar["prepared"] == 0
        assert "word_timestamps" not in holder["model"].transcribe_kwargs
        text = job.output.read_text("utf-8")
        assert "Speaker" not in text

    def test_the_off_path_output_is_what_it_always_was(self, tmp_path, install_model):
        install_model(SEGMENTS, duration=95.0)
        job = make_job(tmp_path)
        run(job)
        body = job.output.read_text("utf-8").split(RULE + "\n", 1)[1]
        assert body == (
            "\n"
            "[00:00:00]\n"
            "First half of the opening paragraph. Second half of the opening paragraph.\n"
            "\n"
            "[00:00:31]\n"
            "The middle section begins here. And it continues for a while.\n"
            "\n"
            "[00:01:10]\n"
            "A final thought to close on.\n"
            "\n"
        )

    def test_the_speaker_count_and_threads_reach_the_engine(self, tmp_path, install_model, install_diarizer):
        install_model(INTERVIEW, duration=44.0)
        diar = install_diarizer(INTERVIEW_TURNS)
        run(make_job(tmp_path, identify_speakers=True, num_speakers=2, cpu_threads=6))
        assert diar["diarizer"].calls == [
            dict(source="recording.mp3", num_speakers=2, threads=6)
        ]

    def test_a_failed_pass_still_produces_a_plain_transcript(self, tmp_path, install_model, install_diarizer):
        holder = install_model(SEGMENTS, duration=95.0)
        install_diarizer(error=diarizer.DiarizationError("ran out of memory"))
        job = make_job(tmp_path, identify_speakers=True)

        events = run(job)

        assert any(isinstance(e, Done) and not e.partial for e in events)
        assert "word_timestamps" not in holder["model"].transcribe_kwargs
        assert "Speaker" not in job.output.read_text("utf-8")
        messages = [e.message for e in events if isinstance(e, Status)]
        assert any(
            "ran out of memory" in m and "without speaker labels" in m for m in messages
        )

    def test_no_speech_means_no_labels(self, tmp_path, install_model, install_diarizer):
        install_model(SEGMENTS, duration=95.0)
        install_diarizer(turns=[])
        job = make_job(tmp_path, identify_speakers=True)
        run(job)
        assert "Speakers:" not in job.output.read_text("utf-8")

    def test_the_header_drops_a_voice_that_never_got_a_word(self, tmp_path, install_model, install_diarizer):
        install_model([spoken(0.0, "Just", "me", "talking.")], duration=3.0)
        install_diarizer([Turn(0.0, 3.0, 0), Turn(50.0, 50.4, 1)])  # 1 is a door slam
        job = make_job(tmp_path, identify_speakers=True)
        run(job)
        text = job.output.read_text("utf-8")
        assert "Speakers:   Speaker 1\n" in text
        assert not list(tmp_path.glob("*.tmp"))

    def test_the_speaker_pass_reports_its_own_progress_phase(self, tmp_path, install_model, install_diarizer):
        install_model(INTERVIEW, duration=44.0)
        install_diarizer(INTERVIEW_TURNS)
        events = run(make_job(tmp_path, identify_speakers=True, duration=44.0))

        phases = [e.phase for e in events if isinstance(e, Progress)]
        assert phases[0] == "speakers" and phases[-1] == "transcribe"
        assert phases == sorted(phases)  # every "speakers" before any "transcribe"
        halfway = next(
            e for e in events if isinstance(e, Progress) and e.audio_done > 0
        )
        assert halfway.fraction == pytest.approx(0.5)
        assert halfway.eta is None

    def test_cancel_during_the_speaker_pass_writes_nothing(self, tmp_path, install_model, install_diarizer):
        install_model(INTERVIEW, duration=44.0)
        install_diarizer(INTERVIEW_TURNS, on_run=lambda cancel: cancel.set())
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3"], identify_speakers=True)

        events = run_batch(jobs)

        (finished,) = only(events, FileFinished)
        assert (finished.status, finished.output) == (CANCELLED, None)
        (end,) = only(events, BatchFinished)
        assert (end.cancelled, end.skipped) == (1, 1)
        assert list(tmp_path.glob("*.txt*")) == []

    def test_a_cancelled_transcript_keeps_its_labels(self, tmp_path, install_model, install_diarizer):
        cancel = threading.Event()
        install_model(
            INTERVIEW, duration=44.0,
            on_segment=lambda index: cancel.set() if index == 2 else None,
        )
        install_diarizer(INTERVIEW_TURNS)
        job = make_job(tmp_path, identify_speakers=True)

        run(job, cancel)

        partial = tmp_path / "recording.partial.txt"
        text = partial.read_text("utf-8")
        assert "[00:00:04] Speaker 2:\nWell, back in 1998.\n" in text
        assert "Speakers:   Speaker 1, Speaker 2\n" in text

    def test_the_engine_is_prepared_once_for_a_whole_queue(self, tmp_path, install_model, install_diarizer):
        install_model(INTERVIEW, duration=44.0)
        diar = install_diarizer(INTERVIEW_TURNS)
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3", "c.mp3"], identify_speakers=True)

        events = run_batch(jobs)

        assert diar["prepared"] == 1
        assert len(diar["diarizer"].calls) == 3
        assert [e.speakers for e in only(events, FileFinished)] == [2, 2, 2]

    def test_file_finished_reports_zero_speakers_when_unlabelled(self, tmp_path, install_model, install_diarizer):
        install_model(SEGMENTS, duration=95.0)
        install_diarizer(error=diarizer.DiarizationError("nope"))
        events = run_batch(make_jobs(tmp_path, ["a.mp3"], identify_speakers=True))
        assert [e.speakers for e in only(events, FileFinished)] == [0]

    def test_an_engine_that_cannot_start_stops_the_queue_up_front(self, tmp_path, install_model, install_diarizer):
        holder = install_model(SEGMENTS, duration=95.0)
        install_diarizer(prepare_error=OSError("Failed to resolve 'huggingface.co'"))
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3"], identify_speakers=True)

        events = run_batch(jobs)

        (end,) = only(events, BatchFinished)
        assert end.skipped == 2 and end.completed == 0
        assert "Speaker identification could not be set up" in end.message
        assert "Untick 'Identify speakers'" in end.message
        assert only(events, FileStarted) == []
        assert list(tmp_path.glob("*.txt")) == []

    def test_on_auto_a_scrap_of_a_third_speaker_is_absorbed(self, tmp_path, install_model, install_diarizer):
        install_model(INTERVIEW, duration=44.0)
        install_diarizer([*INTERVIEW_TURNS, Turn(20.0, 20.8, 5)])  # 0.8s of "someone"
        job = make_job(tmp_path, identify_speakers=True, num_speakers=0)

        events = run(job)

        assert "Speakers:   Speaker 1, Speaker 2\n" in job.output.read_text("utf-8")
        assert any(isinstance(e, Status) and e.message == "Found 2 speakers." for e in events)

    def test_a_speaker_count_the_user_gave_is_honoured(self, tmp_path, install_model, install_diarizer):
        install_model([*INTERVIEW[:2], spoken(20.0, "Sorry?"), INTERVIEW[3]], duration=44.0)
        install_diarizer([Turn(0.0, 4.0, 7), Turn(4.0, 20.0, 2), Turn(20.0, 20.8, 5), Turn(40.0, 44.0, 7)])
        job = make_job(tmp_path, identify_speakers=True, num_speakers=3)

        run(job)

        text = job.output.read_text("utf-8")
        assert "[00:00:20] Speaker 3:\nSorry?\n" in text

    def test_the_queue_bar_holds_still_during_the_speaker_pass(self, tmp_path, install_model, install_diarizer):
        install_model(INTERVIEW, duration=44.0)
        install_diarizer(INTERVIEW_TURNS)
        jobs = make_jobs(tmp_path, ["a.mp3", "b.mp3"], identify_speakers=True, duration=44.0)

        events = run_batch(jobs)

        second_file = [
            e for e in only(events, Progress)
            if e.file_index == 1 and e.phase == "speakers"
        ]
        assert second_file and {e.batch_done for e in second_file} == {44.0}


@pytest.mark.slow
def test_end_to_end_with_the_real_tiny_model(tone_mp3: Path, tmp_path: Path):
    """Genuine run through faster-whisper. Downloads the tiny model on first use.

    The audio is a sine tone, so the text is meaningless — what is being
    verified is that decoding, the model, the writer and the rename all work
    together and produce a well-formed file.
    """
    from transcriptor_prime import media

    output = tmp_path / "tone.txt"
    job = Job(
        source=tone_mp3,
        output=output,
        model="tiny",
        language="en",
        interval_seconds=30.0,
        wrap_width=100,
        cpu_threads=4,
        duration=media.probe(tone_mp3).duration,
    )
    events = run(job)

    assert not any(isinstance(e, Failed) for e in events), [
        e.message for e in events if isinstance(e, Failed)
    ]
    assert output.exists()
    assert "Transcript: tone.mp3" in output.read_text(encoding="utf-8")
