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

from transcriptor_prime import transcriber
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
class FakeSegment:
    start: float
    end: float
    text: str


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
