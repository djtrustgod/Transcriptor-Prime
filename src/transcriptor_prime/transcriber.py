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
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from transcriptor_prime import settings as settings_mod
from transcriptor_prime.formatting import ParagraphBuilder, build_header


# --------------------------------------------------------------------------
# Job description and the events sent back to the GUI
# --------------------------------------------------------------------------


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

    @property
    def fraction(self) -> float:
        if self.audio_total <= 0:
            return 0.0
        return min(1.0, self.audio_done / self.audio_total)


@dataclass(frozen=True)
class Done:
    output: Path
    elapsed: float
    partial: bool = False


@dataclass(frozen=True)
class Failed:
    message: str


Event = Status | Progress | Done | Failed
Emit = Callable[[Event], None]

# How often to push a Progress event / fsync the partial file. Whisper produces
# a segment every few seconds of audio, which on a fast model is many per
# second of wall clock — throttling keeps the UI queue and the disk quiet.
_UPDATE_INTERVAL = 0.5


def transcribe(job: Job, emit: Emit, cancel: threading.Event) -> None:
    """Run one transcription job to completion, cancellation, or failure."""
    started = time.monotonic()
    part_path = job.output.with_suffix(job.output.suffix + ".part")

    try:
        model = _load_model(job, emit, cancel)
        if model is None:  # cancelled during load
            return

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
        )

        elapsed = time.monotonic() - started
        if completed:
            os.replace(part_path, job.output)  # atomic on the same volume
            emit(Done(output=job.output, elapsed=elapsed))
        else:
            partial = job.output.with_name(job.output.stem + ".partial.txt")
            os.replace(part_path, partial)
            emit(Done(output=partial, elapsed=elapsed, partial=True))

    except Exception as exc:  # surfaced in the log pane, never as a traceback
        _discard(part_path)
        emit(Failed(_friendly_error(exc)))


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _load_model(job: Job, emit: Emit, cancel: threading.Event):
    """Instantiate the CTranslate2 model, downloading weights on first use."""
    from faster_whisper import WhisperModel

    cache = settings_mod.models_dir()
    cache.mkdir(parents=True, exist_ok=True)

    if _model_is_cached(cache, job.model):
        emit(Status(f"Loading model '{job.model}' from cache…"))
    else:
        emit(
            Status(
                f"Downloading model '{job.model}' (one time, saved to "
                f"{cache}). This needs an internet connection."
            )
        )

    model = WhisperModel(
        job.model,
        device="cpu",
        compute_type="int8",
        cpu_threads=job.cpu_threads,
        download_root=str(cache),
    )

    # A download in flight cannot be interrupted, so cancellation is honoured at
    # this checkpoint instead. The load phase is short next to the transcription.
    if cancel.is_set():
        emit(Status("Cancelled before transcription started."))
        emit(Done(output=job.output, elapsed=0.0, partial=True))
        return None

    emit(Status(f"Model ready ({job.cpu_threads} CPU threads)."))
    return model


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
                emit(_progress(audio_done, total, started))

        block = builder.flush()
        if block:
            fh.write(block.render(job.wrap_width) + "\n")
        fh.flush()

    emit(_progress(total, total, started))
    return True


def _progress(audio_done: float, total: float, started: float) -> Progress:
    elapsed = time.monotonic() - started
    eta: float | None = None
    # `elapsed` can still be 0.0 on the first event — the clock's resolution is
    # coarser than a fast model's first segment.
    if audio_done > 1.0 and total > 0 and elapsed > 0:
        rate = audio_done / elapsed  # seconds of audio per second of wall clock
        if rate > 0:
            eta = max(0.0, (total - audio_done) / rate)
    return Progress(audio_done=audio_done, audio_total=total, elapsed=elapsed, eta=eta)


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
