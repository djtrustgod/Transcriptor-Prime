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

from transcriptor_prime import diarizer as diarizer_mod
from transcriptor_prime import settings as settings_mod
from transcriptor_prime import speakers as speakers_mod
from transcriptor_prime.attribution import (
    SpeakerNamer,
    Timeline,
    Turn,
    absorb_minor_speakers,
    split_segment,
)
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
    identify_speakers: bool = False
    num_speakers: int = 0  # 0 lets the engine work the number out


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
    # "speakers" while the file is being listened to for who is talking, a
    # separate pass that comes first. The file bar runs 0-100% for each phase;
    # the queue bar only ever counts transcription, and holds still meanwhile.
    phase: str = "transcribe"

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
    speakers: int = 0  # how many speakers the transcript names; 0 = unlabelled


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
        diar = _prepare_diarizer([job], emit)
    except Exception as exc:  # surfaced in the log pane, never as a traceback
        emit(Failed(_friendly_error(exc)))
        return

    # A download in flight cannot be interrupted, so cancellation is honoured at
    # this checkpoint instead. The load phase is short next to the transcription.
    if cancel.is_set():
        emit(Status("Cancelled before transcription started."))
        emit(Done(output=job.output, elapsed=0.0, partial=True))
        return

    result = _run_one(job, model, emit, cancel, diar=diar)
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
        # Inside the same guard on purpose: a queue that asked for speakers and
        # cannot have them stops here, rather than quietly producing a night's
        # worth of transcripts with nobody named in them.
        diar = _prepare_diarizer(jobs, emit)
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
            diar=diar,
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
                speakers=result.speakers,
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
    speakers: int = 0


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
    diar: diarizer_mod.Diarizer | None = None,
) -> _Result:
    """Transcribe one file with an already-loaded model. Never raises."""
    started = time.monotonic()
    part_path = job.output.with_suffix(job.output.suffix + ".part")

    try:
        turns: list[Turn] | None = None
        if diar is not None and job.identify_speakers:
            try:
                turns = _identify_speakers(job, diar, emit, cancel, started, span)
            except diarizer_mod.Cancelled:
                # Nothing has been written yet, so there is no partial file.
                return _Result(CANCELLED, None, time.monotonic() - started)

        emit(Status("Analyzing audio…"))
        options = {}
        if turns:
            # Only asked for when there are speakers to line the words up with:
            # it costs time, and leaving it out keeps a run without speaker
            # identification exactly what it was before the feature existed.
            options["word_timestamps"] = True
        transcribing_from = time.monotonic()
        segments, info = model.transcribe(
            str(job.source),
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            language=None if job.language == "auto" else job.language,
            condition_on_previous_text=job.condition_on_previous_text,
            **options,
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
        completed, named = _write_transcript(
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
            turns=turns,
            rate_started=transcribing_from,
        )

        elapsed = time.monotonic() - started
        if completed:
            os.replace(part_path, job.output)  # atomic on the same volume
            return _Result(COMPLETED, job.output, elapsed, speakers=named)

        partial = job.output.with_name(job.output.stem + ".partial.txt")
        os.replace(part_path, partial)
        return _Result(CANCELLED, partial, elapsed, speakers=named)

    except Exception as exc:  # surfaced in the log pane, never as a traceback
        _discard(part_path)
        return _Result(
            FAILED, None, time.monotonic() - started, _friendly_error(exc)
        )


def _prepare_diarizer(
    jobs: Sequence[Job], emit: Emit
) -> diarizer_mod.Diarizer | None:
    """Ready the speaker engine if any job wants it. Raises with a usable message."""
    if not any(job.identify_speakers for job in jobs):
        return None
    try:
        return diarizer_mod.prepare(lambda message: emit(Status(message)))
    except Exception as exc:
        raise RuntimeError(
            f"Speaker identification could not be set up. {_friendly_error(exc)} "
            "Untick 'Identify speakers' to transcribe without it."
        ) from exc


def _identify_speakers(
    job: Job,
    diar: diarizer_mod.Diarizer,
    emit: Emit,
    cancel: threading.Event,
    started: float,
    span: _BatchSpan | None,
) -> list[Turn] | None:
    """Who spoke when, or ``None`` if that could not be worked out.

    A failure here costs the labels, not the transcript: the file is still
    transcribed, in the plain format. Raises :class:`diarizer.Cancelled`.
    """
    emit(Status("Identifying speakers…"))
    last_update = 0.0

    def on_progress(done: int, total: int) -> None:
        nonlocal last_update
        now = time.monotonic()
        if now - last_update < _UPDATE_INTERVAL:
            return
        last_update = now
        fraction = done / total if total > 0 else 0.0
        emit(_speaker_progress(fraction, job.duration, started, span))

    try:
        turns = diar.run(
            job.source,
            num_speakers=job.num_speakers,
            threads=job.cpu_threads,
            duration=job.duration,
            cancel=cancel,
            on_progress=on_progress,
        )
    except diarizer_mod.DiarizationError as exc:
        emit(
            Status(
                f"Speaker identification failed for {job.source.name} ({exc}) — "
                "transcribing without speaker labels."
            )
        )
        return None

    if not turns:
        emit(
            Status(
                "No speech found to tell speakers apart — "
                "transcribing without speaker labels."
            )
        )
        return None

    if job.num_speakers <= 0:
        # The engine chose the count, and tends to choose one too many.
        turns = absorb_minor_speakers(turns)

    found = len({turn.speaker for turn in turns})
    emit(Status(f"Found {found} speaker{'s' if found != 1 else ''}."))
    emit(_speaker_progress(1.0, job.duration, started, span))
    return turns


def _speaker_progress(
    fraction: float, duration: float, started: float, span: _BatchSpan | None
) -> Progress:
    """A Progress event for the speaker pass. The queue bar does not move."""
    total = duration if duration > 0 else 1.0
    common = dict(
        audio_done=fraction * total,
        audio_total=total,
        elapsed=time.monotonic() - started,
        eta=None,
        phase="speakers",
    )
    if span is None:
        return Progress(**common)
    return Progress(
        **common,
        file_index=span.file_index,
        file_count=span.file_count,
        batch_done=span.audio_before,
        batch_total=span.audio_total,
        batch_elapsed=max(0.0, time.monotonic() - span.throughput_started),
        batch_eta=None,
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
    turns: Sequence[Turn] | None = None,
    rate_started: float | None = None,
) -> tuple[bool, int]:
    """Stream segments to ``part_path``.

    Returns ``(ran to the end of the audio, number of speakers named)``.
    """
    builder = ParagraphBuilder(interval_seconds=job.interval_seconds)
    last_update = 0.0
    audio_done = 0.0

    timeline = Timeline(turns) if turns else None
    namer = SpeakerNamer()
    previous: int | None = None
    # The header is written before anyone has spoken, so it can only promise
    # one name per voice the engine found. It is trued up once the file closes.
    expected = len({turn.speaker for turn in turns}) if turns else 0

    def render(segment) -> str:
        """This segment's finished paragraphs, as text ready to write."""
        nonlocal previous
        if timeline is None:
            blocks = builder.add_run(segment.start, segment.end, segment.text)
        else:
            blocks = []
            for run in split_segment(segment, timeline, previous):
                previous = run.speaker
                blocks += builder.add_run(
                    run.start, run.end, run.text, namer.label(run.speaker)
                )
        return "".join(block.render(job.wrap_width) + "\n" for block in blocks)

    completed = True

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
                speakers=[f"Speaker {n + 1}" for n in range(expected)] or None,
            )
        )
        fh.flush()

        emit(Status("Transcribing…"))
        for segment in segments:
            if cancel.is_set():
                completed = False
                break

            fh.write(render(segment))

            audio_done = max(audio_done, float(segment.end))
            now = time.monotonic()
            if now - last_update >= _UPDATE_INTERVAL:
                last_update = now
                fh.flush()
                emit(_progress(audio_done, total, started, span, rate_started))

        # On a cancel too: keep whatever is buffered rather than dropping the
        # last partial paragraph on the floor.
        block = builder.flush()
        if block:
            fh.write(block.render(job.wrap_width) + "\n")
        fh.flush()

    named = len(namer.labels)
    if timeline is not None and named != expected:
        # A voice the engine heard never had a word attributed to it — noise,
        # or a cancel before its turn came.
        try:
            speakers_mod.sync_header(part_path)
        except OSError:
            pass  # a cosmetic line; never worth failing the transcript over

    if completed:
        emit(_progress(total, total, started, span, rate_started))
    return completed, named


def _progress(
    audio_done: float,
    total: float,
    started: float,
    span: _BatchSpan | None = None,
    rate_started: float | None = None,
) -> Progress:
    elapsed = time.monotonic() - started
    # The speaker pass comes first and can take minutes. Timing the rate from
    # when transcription itself began keeps those minutes out of the ETA, while
    # `elapsed` — what the user is shown — still counts from the file's start.
    working = time.monotonic() - (started if rate_started is None else rate_started)
    eta: float | None = None
    # `working` can still be 0.0 on the first event — the clock's resolution is
    # coarser than a fast model's first segment.
    if audio_done > 1.0 and total > 0 and working > 0:
        rate = audio_done / working  # seconds of audio per second of wall clock
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
