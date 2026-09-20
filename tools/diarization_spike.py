"""Measure speaker identification on this machine. Not part of the app.

    .venv\\Scripts\\python.exe tools\\diarization_spike.py interview.mp3
    .venv\\Scripts\\python.exe tools\\diarization_spike.py two.wav --loop-to 10800 --shift 0.5

Reports wall-clock time, real-time factor, the speakers found and the child
process's peak memory. ``--loop-to`` repeats a short WAV out to a target length,
which is how the defaults in ``diarizer.window_shift_for`` were chosen without
needing a real three-hour recording to hand.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from transcriptor_prime import diarizer, media  # noqa: E402


def _peak_mb() -> float:
    """Peak working set of the real worker process, in MB.

    ``Popen`` only knows the venv's ``python.exe`` launcher stub; the interpreter
    doing the work is *its* child, so it has to be found by command line.
    """
    query = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
        "'*diarize_worker*' -and $_.Name -like 'python*' } | "
        "ForEach-Object { $_.PeakWorkingSetSize } | Measure-Object -Maximum | "
        "ForEach-Object { $_.Maximum }"
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-Command", query], capture_output=True, text=True
    )
    try:
        return float(done.stdout.strip()) / 1024  # reported in KB
    except ValueError:
        return 0.0


def _looped(source: Path, seconds: float, folder: Path) -> Path:
    with wave.open(str(source), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(wav.getnframes())
    repeats = int(seconds / (params.nframes / params.framerate)) + 1
    target = folder / f"looped-{int(seconds)}s.wav"
    with wave.open(str(target), "wb") as out:
        out.setparams(params)
        for _ in range(repeats):
            out.writeframes(frames)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--speakers", type=int, default=0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--shift", type=float, default=None)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--loop-to", type=float, default=0.0)
    args = parser.parse_args()

    if args.shift is not None:
        diarizer.window_shift_for = lambda duration: args.shift
    if args.threshold is not None:
        diarizer.CLUSTER_THRESHOLD = args.threshold

    with tempfile.TemporaryDirectory() as scratch:
        source = args.source
        if args.loop_to:
            source = _looped(source, args.loop_to, Path(scratch))
        duration = media.probe(source).duration

        engine = diarizer.prepare(print)
        peak = 0.0
        first_progress: float | None = None
        started = time.monotonic()

        sampled = 0.0

        def on_progress(done: int, total: int) -> None:
            nonlocal peak, first_progress, sampled
            if time.monotonic() - sampled > 5.0:
                sampled = time.monotonic()
                peak = max(peak, _peak_mb())
            if done and first_progress is None:
                first_progress = time.monotonic() - started

        turns = engine.run(
            source,
            num_speakers=args.speakers,
            threads=args.threads,
            duration=duration,
            cancel=threading.Event(),
            on_progress=on_progress,
        )
        elapsed = time.monotonic() - started

    print(f"audio            {duration:9.1f} s")
    print(f"window shift     {diarizer.window_shift_for(duration):9.2f}")
    print(f"elapsed          {elapsed:9.1f} s   (RTF {elapsed / duration:.3f})")
    print(f"silent phase     {first_progress or 0:9.1f} s   (before the first progress report)")
    print(f"child peak       {peak:9.0f} MB")
    print(f"turns            {len(turns):9d}")
    print(f"speakers         {sorted({t.speaker for t in turns})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
