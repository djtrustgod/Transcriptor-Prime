"""The transcription worker.

Runs on a background thread and communicates with the GUI purely by emitting
:class:`Event` objects — it never touches a Tk widget. See ARCHITECTURE.md for
the full threading contract.

Two properties matter most for the three-hour recordings this app targets:

* **Streaming progress.** ``WhisperModel.transcribe`` returns a lazy generator,
  so each segment's ``end`` timestamp against the known total duration gives a
  genuine percentage and ETA rather than a spinner.
* **Nothing is lost.** Finished paragraphs are written to a ``.part`` file as
  they are produced and only renamed onto the real output path at the end. A
  crash or a cancel at 2h50m still leaves 2h50m of readable transcript.

Two entry points share all of that machinery: :func:`transcribe` for a single
job, and :func:`transcribe_batch` for a queue. The batch loads the model once
and keeps going when one file fails.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from transcriptor_prime import settings as settings_mod
from transcriptor_prime.formatting import ParagraphBuilder, build_header, format_duration


# --------------------------------------------------------------------------
# Job description and the events sent back to the GUI
# --------------------------------------------------------------------------


#: Outcomes of one file. Plain strings rather than an enum — ``StrEnum`` needs
#: Python 3.11 and this package supports 3.10.
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"


@dataclass(frozen=True)
class Job:
    source: Path
    output: Path
    model: str
    language: str  # "auto" or a Whisper language code
    interval_seconds: float
    wrap_width: int
    cpu_threads: int
    duration: float  # from media.probe, used for the progress denominator
    condition_on_previous_text: bool = True


@dataclass(frozen=True)
class Status:
    """A line for the log pane / status label."""

    message: str


@dataclass(frozen=True)
class Progress:
    audio_done: float
    audio_total: float
    elapsed: float
    eta: float | None
    # Batch context. Every field is defaulted, so a single-file run constructs
    # a Progress exactly as it always has. Carrying the queue position on the
    # same event as the per-file numbers means the two progress bars are two
    # views of one instant and can never disagree.
    file_index: int = 0
    file_count: int = 1
    batch_done: float = 0.0  # audio seconds finished across the whole queue
    batch_total: float = 0.0  # 0.0 when some duration is unknown
    batch_elapsed: float = 0.0
    batch_eta: float | None = None

    @property
    def fraction(self) -> float:
        if self.audio_total <= 0:
            return 0.0
        return min(1.0, self.audio_done / self.audio_total)

    @property
    def batch_fraction(self) -> float:
        """Progress across the whole queue, 0.0-1.0.

        Falls back to counting files when the durations are unknown — better a
        coarse bar than a stuck one.
        """
        if self.batch_total > 0:
            return min(1.0, self.batch_done / self.batch_total)
        if self.file_count > 0:
            return min(1.0, (self.file_index + self.fraction) / self.file_count)
        return self.fraction


@dataclass(frozen=True)
class Done:
    output: Path
    elapsed: float
    partial: bool = False


@dataclass(frozen=True)
class Failed:
    message: str


@dataclass(frozen=True)
class FileStarted:
    """One file in a batch is about to begin."""

    index: int  # 0-based position in the queue
    count: int
    source: Path
    output: Path
    duration: float


@dataclass(frozen=True)
class FileFinished:
    """One file in a batch reached an end — any end."""

    index: int
    source: Path
    output: Path | None  # None when nothing was written
    elapsed: float
    status: str  # COMPLETED | CANCELLED | FAILED
    message: str = ""  # friendly text when status is FAILED


@dataclass(frozen=True)
class BatchFinished:
    """The terminal event of every batch, on every path."""

    completed: int
    failed: int
    cancelled: int
    skipped: int  # queued but never started — cancelled, or a fatal model load
    elapsed: float
    outputs: tuple[Path, ...] = ()
    message: str = ""  # non-empty only when the batch could not start at all


Event = (
    Status | Progress | Done | Failed | FileStarted | FileFinished | BatchFinished
)
Emit = Callable[[Event], None]

# How often to push a Progress event / fsync the partial file. Whisper produces
# a segment every few seconds of audio, which on a fast model is many per
# second of wall clock — throttling keeps the UI queue and the disk quiet.
_UPDATE_INTERVAL = 0.5


def transcribe(job: Job, emit: Emit, cancel: threading.Event) -> None:
    """Run one transcription job to completion, cancellation, or failure.

    Emits the single-file event shape — ``Done`` or ``Failed`` as the terminal
    event. :func:`transcribe_batch` is the multi-file entry point and speaks a
    different, deliberately non-overlapping dialect; see its docstring.
    """
    try:
        model = _build_model(job.model, job.cpu_threads, emit)
    except Exception as exc:  # surfaced in the log pane, never as a traceback
        emit(Failed(_friendly_error(exc)))
        return

    # A download in flight cannot be interrupted, so cancellation is honoured at
    # this checkpoint instead. The load phase is short next to the transcription.
    if cancel.is_set():
        emit(Status("Cancelled before transcription started."))
        emit(Done(output=job.output, elapsed=0.0, partial=True))
        return

    result = _run_one(job, model, emit, cancel)
    if result.status == FAILED:
        emit(Failed(result.message))
    else:
        emit(
            Done(
                output=result.output,
                elapsed=result.elapsed,
                partial=result.status == CANCELLED,
            )
        )


def transcribe_batch(
    jobs: Sequence[Job], emit: Emit, cancel: threading.Event
) -> None:
    """Run every job in turn against one shared model.

    All jobs must agree on ``model`` and ``cpu_threads`` — they come from a
    single Options panel — and the first job's values are the ones used.

    Three rules define the batch's behaviour:

    * **The model is loaded once.** Constructing a ``WhisperModel`` costs
      seconds and hundreds of megabytes; paying that per file would dominate a
      queue of short clips.
    * **One bad file does not end the run.** Every file is wrapped, reported as
      ``FileFinished(status=FAILED)``, and the queue moves on.
    * **The terminal event is always** :class:`BatchFinished`, and this function
      never emits ``Done`` or ``Failed``. The GUI unlocks its form on those two,
      so either one mid-queue would re-enable Start while the worker still runs.
      A fatal model-load error travels as ``BatchFinished.message`` instead.
    """
    count = len(jobs)
    batch_started = time.monotonic()
    if count == 0:
        emit(BatchFinished(0, 0, 0, 0, 0.0))
        return

    # A single unknown duration makes the summed denominator a lie, so fall back
    # to counting files instead of quietly reporting a wrong percentage.
    audio_total = (
        sum(job.duration for job in jobs)
        if all(job.duration > 0 for job in jobs)
        else 0.0
    )
    if count > 1:
        span = f", {format_duration(audio_total)} of audio" if audio_total else ""
        emit(Status(f"Queue: {count} files{span}."))

    try:
        model = _build_model(jobs[0].model, jobs[0].cpu_threads, emit)
    except Exception as exc:
        message = _friendly_error(exc)
        emit(Status(f"ERROR: {message}"))
        emit(
            BatchFinished(
                completed=0,
                failed=0,
                cancelled=0,
                skipped=count,
                elapsed=time.monotonic() - batch_started,
                message=message,
            )
        )
        return

    # A first-time weight download can take minutes. Timing throughput from
    # *after* the load keeps that one-off cost out of the queue's ETA.
    throughput_started = time.monotonic()

    completed = failed = cancelled = 0
    processed = 0
    outputs: list[Path] = []
    audio_before = 0.0

    for index, job in enumerate(jobs):
        if cancel.is_set():
            break

        emit(
            FileStarted(
                index=index,
                count=count,
                source=job.source,
                output=job.output,
                duration=job.duration,
            )
        )
        if count > 1:
            emit(
                Status(
                    f"[{index + 1}/{count}] {job.source.name} → {job.output.name}"
                )
            )

        result = _run_one(
            job,
            model,
            emit,
            cancel,
            span=_BatchSpan(
                file_index=index,
                file_count=count,
                file_duration=job.duration,
                audio_before=audio_before,
                audio_total=audio_total,
                throughput_started=throughput_started,
            ),
        )
        processed += 1
        audio_before += job.duration

        if result.status == COMPLETED:
            completed += 1
        elif result.status == CANCELLED:
            cancelled += 1
        else:
            failed += 1
        if result.output is not None and result.status == COMPLETED:
            outputs.append(result.output)

        emit(
            FileFinished(
                index=index,
                source=job.source,
                output=result.output,
                elapsed=result.elapsed,
                status=result.status,
                message=result.message,
            )
        )

    emit(
        BatchFinished(
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            skipped=count - processed,
            elapsed=time.monotonic() - batch_started,
            outputs=tuple(outputs),
        )
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Result:
    """What :func:`_run_one` reports back to its caller."""

    status: str
    output: Path | None
    elapsed: float
    message: str = ""


@dataclass(frozen=True)
class _BatchSpan:
    """Where the current file sits in the queue, for the overall progress bar."""

    file_index: int
    file_count: int
    file_duration: float  # the probe duration — the denominator's currency
    audio_before: float  # summed probe durations of the files already finished
    audio_total: float  # summed probe durations of the whole queue, 0 if unknown
    throughput_started: float  # monotonic, set after the model load


def _run_one(
    job: Job,
    model,
    emit: Emit,
    cancel: threading.Event,
    *,
    span: _BatchSpan | None = None,
) -> _Result:
    """Transcribe one file with an already-loaded model. Never raises."""
    started = time.monotonic()
    part_path = job.output.with_suffix(job.output.suffix + ".part")

    try:
        emit(Status("Analyzing audio…"))
        segments, info = model.transcribe(
            str(job.source),
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            language=None if job.language == "auto" else job.language,
            condition_on_previous_text=job.condition_on_previous_text,
        )

        detected = job.language == "auto"
        language = info.language or job.language
        if detected:
            emit(
                Status(
                    f"Detected language: {language} "
                    f"({info.language_probability:.2f} confidence)"
                )
            )
        else:
            emit(Status(f"Language forced to: {language}"))

        # info.duration is what Whisper actually decoded; prefer it over the
        # container's advertised length, which can disagree slightly on MP3.
        total = info.duration or job.duration or 0.0

        job.output.parent.mkdir(parents=True, exist_ok=True)
        completed = _write_transcript(
            job=job,
            part_path=part_path,
            segments=segments,
            total=total,
            language=language,
            language_detected=detected,
            language_probability=info.language_probability if detected else None,
            emit=emit,
            cancel=cancel,
            started=started,
            span=span,
        )

        elapsed = time.monotonic() - started
        if completed:
            os.replace(part_path, job.output)  # atomic on the same volume
            return _Result(COMPLETED, job.output, elapsed)

        partial = job.output.with_name(job.output.stem + ".partial.txt")
        os.replace(part_path, partial)
        return _Result(CANCELLED, partial, elapsed)

    except Exception as exc:  # surfaced in the log pane, never as a traceback
        _discard(part_path)
        return _Result(
            FAILED, None, time.monotonic() - started, _friendly_error(exc)
        )


def _build_model(model: str, cpu_threads: int, emit: Emit):
    """Instantiate the CTranslate2 model, downloading weights on first use.

    Deliberately knows nothing about cancellation or the event protocol beyond
    its two status lines — that is what lets a batch call it once and reuse the
    result across every file.
    """
    from faster_whisper import WhisperModel

    cache = settings_mod.models_dir()
    cache.mkdir(parents=True, exist_ok=True)

    if _model_is_cached(cache, model):
        emit(Status(f"Loading model '{model}' from cache…"))
    else:
        emit(
            Status(
                f"Downloading model '{model}' (one time, saved to "
                f"{cache}). This needs an internet connection."
            )
        )

    instance = WhisperModel(
        model,
        device="cpu",
        compute_type="int8",
        cpu_threads=cpu_threads,
        download_root=str(cache),
    )
    emit(Status(f"Model ready ({cpu_threads} CPU threads)."))
    return instance


def _model_is_cached(cache: Path, model: str) -> bool:
    """Whether huggingface_hub already has this model on disk.

    Only used to word the status message, so a wrong guess is harmless.
    """
    try:
        return any(
            entry.is_dir() and model in entry.name and any(entry.rglob("*.bin"))
            for entry in cache.iterdir()
        )
    except OSError:
        return False


def _write_transcript(
    *,
    job: Job,
    part_path: Path,
    segments,
    total: float,
    language: str,
    language_detected: bool,
    language_probability: float | None,
    emit: Emit,
    cancel: threading.Event,
    started: float,
    span: _BatchSpan | None = None,
) -> bool:
    """Stream segments to ``part_path``. Returns True if the audio ran to the end."""
    builder = ParagraphBuilder(interval_seconds=job.interval_seconds)
    last_update = 0.0
    audio_done = 0.0

    with open(part_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            build_header(
                source_name=job.source.name,
                duration=total,
                model=job.model,
                language=language,
                language_detected=language_detected,
                language_probability=language_probability,
                generated_at=datetime.now(),
            )
        )
        fh.flush()

        emit(Status("Transcribing…"))
        for segment in segments:
            if cancel.is_set():
                # Keep whatever is buffered rather than dropping the last
                # partial paragraph on the floor.
                block = builder.flush()
                if block:
                    fh.write(block.render(job.wrap_width) + "\n")
                fh.flush()
                return False

            block = builder.add(segment.start, segment.end, segment.text)
            if block:
                fh.write(block.render(job.wrap_width) + "\n")

            audio_done = max(audio_done, float(segment.end))
            now = time.monotonic()
            if now - last_update >= _UPDATE_INTERVAL:
                last_update = now
                fh.flush()
                emit(_progress(audio_done, total, started, span))

        block = builder.flush()
        if block:
            fh.write(block.render(job.wrap_width) + "\n")
        fh.flush()

    emit(_progress(total, total, started, span))
    return True


def _progress(
    audio_done: float, total: float, started: float, span: _BatchSpan | None = None
) -> Progress:
    elapsed = time.monotonic() - started
    eta: float | None = None
    # `elapsed` can still be 0.0 on the first event — the clock's resolution is
    # coarser than a fast model's first segment.
    if audio_done > 1.0 and total > 0 and elapsed > 0:
        rate = audio_done / elapsed  # seconds of audio per second of wall clock
        if rate > 0:
            eta = max(0.0, (total - audio_done) / rate)

    if span is None:
        return Progress(
            audio_done=audio_done, audio_total=total, elapsed=elapsed, eta=eta
        )

    # The queue's denominator is summed *probe* durations, but `audio_done`
    # counts what Whisper decoded, and the two disagree by a second or two on
    # VBR MP3. Clamping the in-flight file's contribution keeps the overall bar
    # from creeping past 100% over a long queue.
    contribution = (
        min(audio_done, span.file_duration)
        if span.file_duration > 0
        else audio_done
    )
    batch_done = span.audio_before + contribution
    batch_elapsed = max(0.0, time.monotonic() - span.throughput_started)
    batch_eta: float | None = None
    if batch_done > 1.0 and span.audio_total > 0 and batch_elapsed > 0:
        batch_rate = batch_done / batch_elapsed
        if batch_rate > 0:
            batch_eta = max(0.0, (span.audio_total - batch_done) / batch_rate)

    return Progress(
        audio_done=audio_done,
        audio_total=total,
        elapsed=elapsed,
        eta=eta,
        file_index=span.file_index,
        file_count=span.file_count,
        batch_done=batch_done,
        batch_total=span.audio_total,
        batch_elapsed=batch_elapsed,
        batch_eta=batch_eta,
    )


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _friendly_error(exc: Exception) -> str:
    """Translate the failures users actually hit into plain language."""
    text = str(exc)
    name = type(exc).__name__

    lowered = text.lower()
    if "connection" in lowered or "resolve" in lowered or "network" in lowered:
        return (
            "Could not download the model — check your internet connection. "
            "Once a model has been downloaded, later runs work offline. "
            f"({name}: {text})"
        )
    if "permission" in lowered or isinstance(exc, PermissionError):
        return (
            "Permission denied writing the transcript. Choose a different save "
            f"location, or close the file if it is open elsewhere. ({text})"
        )
    if "no space" in lowered:
        return f"Ran out of disk space while writing the transcript. ({text})"
    return f"{name}: {text}"
