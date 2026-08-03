"""Worker tests.

A stub stands in for ``faster_whisper.WhisperModel`` so the incremental write,
atomic rename, cancellation and progress behaviour can be exercised in
milliseconds without downloading model weights. The one genuine end-to-end run
lives at the bottom behind the ``slow`` marker.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from transcriptor_prime import transcriber
from transcriptor_prime.transcriber import Done, Failed, Job, Progress, Status


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

    def __init__(self, segments, duration, on_segment=None, **kwargs):
        self.segments = segments
        self.duration = duration
        self.on_segment = on_segment
        self.kwargs = kwargs
        self.transcribe_kwargs: dict = {}

    def transcribe(self, path, **kwargs):
        self.transcribe_kwargs = kwargs

        def generate():
            for index, segment in enumerate(self.segments):
                if self.on_segment:
                    self.on_segment(index)
                yield segment

        return generate(), FakeInfo(duration=self.duration)


@pytest.fixture
def install_model(monkeypatch):
    """Patch ``faster_whisper.WhisperModel`` and hand back the constructed stub."""
    import faster_whisper

    holder: dict = {}

    def install(segments, duration, on_segment=None):
        def factory(*args, **kwargs):
            model = FakeModel(segments, duration, on_segment, **kwargs)
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
