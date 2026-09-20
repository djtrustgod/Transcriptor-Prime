"""Speaker identification, from the app's side of the process boundary.

The engine (sherpa-onnx) runs in a child process — see
:mod:`transcriptor_prime.diarize_worker` and "Why a subprocess" in
ARCHITECTURE.md. This module downloads the two small models, launches that
child, relays its progress and turns a Cancel into ``proc.kill()``.

It imports neither Tk nor ``sherpa_onnx``. It is driven entirely from the
transcription worker thread: the child writes to a spool *file*, which that
thread polls, so there is no pipe-reader thread and the app keeps its
"exactly one background thread" contract.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from transcriptor_prime import settings as settings_mod
from transcriptor_prime.attribution import Turn

#: ``(repo, filename, pinned revision)`` — a revision is immutable, so the
#: bytes a given release of the app downloads never change underneath it.
SEGMENTATION_MODEL = (
    "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0",
    "model.onnx",
    "9403a6902bb58e3d5ae8c7e77c3422de279db2e0",
)
# WeSpeaker's ResNet34-LM: the voice model pyannote 3.1 itself pairs with the
# segmentation model above. Chosen by measurement, not reputation — on a real
# four-minute radio interview between two men, forced to two speakers:
#
#   wespeaker resnet34-LM   92% of speech attributed correctly   (this one)
#   NeMo TitaNet large      92%, but four times the download
#   3D-Speaker ERes2Net     80%
#   NeMo TitaNet small      80%
#   wespeaker CAM++         76%
#   3D-Speaker CAM++        69%   <- shipped first; it could not tell them apart
#
# The CAM++ models are the fastest and passed a clean studio sample, which is
# how the wrong one shipped. Re-run the comparison before changing this.
EMBEDDING_MODEL = (
    "csukuangfj/speaker-embedding-models",
    "wespeaker_en_voxceleb_resnet34_LM.onnx",
    "0743f301363dec56491a490f6d6cbc9d67f9a3bf",
)

#: How alike two stretches of speech must be to count as one person, when the
#: number of speakers is left on auto. Smaller finds more speakers. No value is
#: right for every recording — across five test files the workable range moved
#: from "under 0.45" to "0.6-0.7" — so this errs toward finding one too many:
#: a scrap of a speaker is absorbed afterwards (attribution.absorb_minor_speakers)
#: and a real over-split can be merged in the naming window, but two people
#: fused into one cannot be pulled apart again.
CLUSTER_THRESHOLD = 0.5

_POLL_INTERVAL = 0.2
_SELFTEST_TIMEOUT = 120.0

#: ``on_progress(done, total)`` — both 0 until the engine starts reporting.
ProgressFn = Callable[[int, int], None]


class DiarizationError(Exception):
    """Speaker identification failed; the transcript can still be made."""


class Cancelled(Exception):
    """The user cancelled while the child was running; it has been killed."""


def window_shift_for(duration: float) -> float:
    """How far the analysis window advances, as a fraction of its length.

    The engine compares every stretch of speech with every other, so that part
    of its cost grows with the *square* of the recording's length. A coarser
    step on long recordings keeps a three-hour file to minutes, at a small cost
    in how precisely a change of speaker is placed.

    Measured with ``tools/diarization_spike.py`` (8 threads, the ResNet34-LM
    voice model): 30 minutes at 0.1 took 3.5 minutes; three hours at 0.25 took
    8 minutes and peaked at 1.5 GB, nearly all of it the audio itself. At 0.5
    three hours is about twice as fast, but in an earlier measurement it split
    one of two voices in two, so 0.5 is kept for recordings too long to do any
    other way.
    """
    if duration > 4 * 3600:
        return 0.5
    if duration > 3600:
        return 0.25
    return 0.1


def prepare(emit_status: Callable[[str], None]) -> "Diarizer":
    """Fetch the models if needed and prove the engine starts. May raise.

    Called once per queue, before the first file, so a missing model or a
    broken install stops the run up front instead of quietly producing a
    night's worth of transcripts with no speakers in them.
    """
    from huggingface_hub import hf_hub_download

    cache = settings_mod.models_dir()
    cache.mkdir(parents=True, exist_ok=True)

    paths = []
    announced = False
    for repo, filename, revision in (SEGMENTATION_MODEL, EMBEDDING_MODEL):
        try:
            path = hf_hub_download(
                repo, filename, revision=revision, cache_dir=str(cache),
                local_files_only=True,
            )
        except Exception:
            if not announced:
                announced = True
                emit_status(
                    "Downloading speaker models (one time, about 33 MB, saved "
                    f"to {cache}). This needs an internet connection."
                )
            path = hf_hub_download(
                repo, filename, revision=revision, cache_dir=str(cache)
            )
        paths.append(Path(path))

    diarizer = Diarizer(segmentation=paths[0], embedding=paths[1])
    diarizer.selftest()
    emit_status("Speaker identification ready.")
    return diarizer


@dataclass
class Diarizer:
    segmentation: Path
    embedding: Path

    def selftest(self) -> None:
        """Load the models in a throwaway child. Raises :class:`DiarizationError`."""
        command = [*_worker_command(), *self._model_args(), "--selftest"]
        try:
            done = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_SELFTEST_TIMEOUT,
                stdin=subprocess.DEVNULL,
                env=_child_env(),
                creationflags=_CREATION_FLAGS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DiarizationError(
                f"Speaker identification could not start. ({exc})"
            ) from exc
        if done.returncode != 0:
            detail = _last_error(done.stdout.splitlines()) or _tail(
                done.stdout + done.stderr
            )
            raise DiarizationError(
                f"Speaker identification could not start. ({detail})"
            )

    def run(
        self,
        source: Path,
        *,
        num_speakers: int,
        threads: int,
        duration: float,
        cancel: threading.Event,
        on_progress: ProgressFn,
    ) -> list[Turn]:
        """Who spoke when. Blocks until the child finishes or ``cancel`` is set.

        Raises :class:`Cancelled`, or :class:`DiarizationError` for anything
        else — including the child dying without a word, which is how a native
        crash or running out of memory presents.
        """
        command = [
            *_worker_command(),
            *self._model_args(),
            "--source", str(source),
            "--num-speakers", str(max(0, int(num_speakers))),
            "--threshold", str(CLUSTER_THRESHOLD),
            "--threads", str(max(1, int(threads))),
            "--shift", str(window_shift_for(duration)),
        ]  # fmt: skip

        spool_dir = Path(tempfile.mkdtemp(prefix="transcriptor-diarize-"))
        spool = spool_dir / "output.jsonl"
        try:
            with open(spool, "wb") as sink:
                try:
                    proc = subprocess.Popen(
                        command,
                        stdout=sink,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        env=_child_env(),
                        creationflags=_CREATION_FLAGS,
                    )
                except OSError as exc:
                    raise DiarizationError(f"could not start: {exc}") from exc

            _set_active(proc)
            try:
                lines = self._follow(proc, spool, cancel, on_progress)
            finally:
                _set_active(None)
        finally:
            shutil.rmtree(spool_dir, ignore_errors=True)

        for line in reversed(lines):
            payload = _parse(line)
            if payload is None:
                continue
            if "turns" in payload and proc.returncode == 0:
                try:
                    return [
                        Turn(float(s), float(e), int(who))
                        for s, e, who in payload["turns"]
                    ]
                except (TypeError, ValueError) as exc:
                    raise DiarizationError(f"unreadable result: {exc}") from exc
            if "error" in payload:
                raise DiarizationError(str(payload["error"]))

        raise DiarizationError(
            f"the speaker engine stopped unexpectedly (exit code {proc.returncode}"
            + (f": {_tail(chr(10).join(lines))}" if lines else "")
            + "). Very long recordings can run out of memory."
        )

    def _follow(
        self,
        proc: subprocess.Popen,
        spool: Path,
        cancel: threading.Event,
        on_progress: ProgressFn,
    ) -> list[str]:
        """Tail the spool until the child exits; returns every complete line."""
        lines: list[str] = []
        pending = b""
        latest = (0, 0)
        with open(spool, "rb") as source:
            while True:
                finished = proc.poll() is not None
                pending += source.read()
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        lines.append(line)

                payload = next(
                    (p for p in map(_parse, reversed(complete)) if p and "progress" in p),
                    None,
                )
                if payload:
                    try:
                        done, total = payload["progress"]
                        latest = (int(done), int(total))
                    except (TypeError, ValueError):
                        pass
                # Called on every poll, news or not: the engine's first phase
                # is silent, and the GUI's elapsed clock ticks off these calls.
                on_progress(*latest)

                if finished:
                    tail = pending.decode("utf-8", errors="replace").strip()
                    if tail:
                        lines.append(tail)
                    return lines
                if cancel.is_set():
                    _kill(proc)
                    raise Cancelled()
                time.sleep(_POLL_INTERVAL)

    def _model_args(self) -> list[str]:
        return [
            "--segmentation", str(self.segmentation),
            "--embedding", str(self.embedding),
        ]  # fmt: skip


# --------------------------------------------------------------------------
# The child process
# --------------------------------------------------------------------------

# No console window may flash up when the app itself runs under pythonw.
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_active: subprocess.Popen | None = None
_active_lock = threading.Lock()


def _worker_command() -> list[str]:
    """``python -m transcriptor_prime.diarize_worker``, as an argv prefix.

    The app runs under ``pythonw.exe``, whose children get no usable stdout on
    some Windows setups; the ``python.exe`` beside it always does.
    """
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        sibling = executable.with_name("python.exe")
        if sibling.is_file():
            executable = sibling
    return [str(executable), "-m", "transcriptor_prime.diarize_worker"]


def _child_env() -> dict[str, str]:
    """The parent's environment, plus a guarantee the package is importable.

    The app is normally run from a source tree with ``src`` on the path by way
    of ``run.bat`` or pytest's config — neither of which a child inherits.
    """
    env = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        package_parent + os.pathsep + existing if existing else package_parent
    )
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _set_active(proc: subprocess.Popen | None) -> None:
    global _active
    with _active_lock:
        _active = proc


def kill_active() -> None:
    """Stop a running child, if any. Safe to call at any time, from any thread.

    The transcription worker is a daemon thread and dies with the window; its
    child would not, and would burn CPU for minutes with nobody listening.
    """
    with _active_lock:
        proc = _active
    if proc is not None:
        _kill(proc)


atexit.register(kill_active)


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _parse(line: str | bytes) -> dict | None:
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _last_error(lines: Sequence[str]) -> str:
    for line in reversed(lines):
        payload = _parse(line)
        if payload and "error" in payload:
            return str(payload["error"])
    return ""


def _tail(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text[-limit:] if text else "no output"
