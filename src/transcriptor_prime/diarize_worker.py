"""Speaker identification, run as a separate process.

    python -m transcriptor_prime.diarize_worker --source talk.mp3 …

This is the **only** module that imports ``sherpa_onnx``, and it is never
imported by the app — only launched by :mod:`transcriptor_prime.diarizer`. See
"Why a subprocess" in ARCHITECTURE.md; in short, the engine holds the GIL for
minutes, cannot be interrupted, can end the process outright, and ships its own
``onnxruntime.dll``. A child process turns every one of those into a non-event.

It talks to its parent in JSON lines on stdout::

    {"phase": "decode"}
    {"phase": "segment"}
    {"progress": [12, 480]}
    {"turns": [[0.31, 8.92, 0], [9.4, 31.0, 1]]}      <- last line on success
    {"error": "…"}                                     <- last line on failure, exit 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SAMPLE_RATE = 16000
_PROGRESS_INTERVAL = 0.25


def _say(**payload) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def decode(path: str):
    """The whole recording as 16 kHz mono float32, written into one buffer.

    Collecting frames in a list and concatenating would briefly hold the audio
    twice; at 230 MB per hour that matters for a three-hour recording.
    """
    import av
    import numpy as np
    from av.audio.resampler import AudioResampler

    container = av.open(path)
    try:
        stream = container.streams.audio[0]
        if container.duration:
            seconds = float(container.duration) / av.time_base
        elif stream.duration is not None and stream.time_base is not None:
            seconds = float(stream.duration * stream.time_base)
        else:
            seconds = 600.0
        samples = np.empty(int((seconds + 5.0) * SAMPLE_RATE), dtype=np.float32)
        filled = 0

        resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

        def take(frames) -> None:
            nonlocal samples, filled
            for frame in frames:
                chunk = frame.to_ndarray().reshape(-1)
                if filled + chunk.size > samples.size:
                    # The container under-reported its length; grow by half.
                    samples = np.concatenate(
                        [samples, np.empty(max(chunk.size, samples.size // 2), np.float32)]
                    )
                samples[filled : filled + chunk.size] = chunk
                filled += chunk.size

        for frame in container.decode(stream):
            take(resampler.resample(frame))
        take(resampler.resample(None))  # flush the resampler's tail
    finally:
        container.close()

    samples = samples[:filled]
    samples /= 32768.0
    return samples


def build(args):
    import sherpa_onnx

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=args.segmentation,
                window_shift_ratio=args.shift,
            ),
            num_threads=args.threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=args.embedding,
            num_threads=args.threads,
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            # -1 means "work the number out from the threshold".
            num_clusters=args.num_speakers if args.num_speakers > 0 else -1,
            threshold=args.threshold,
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("the speaker models could not be loaded")
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def selftest(args) -> None:
    """Prove the engine is usable before a queue commits to it."""
    import sherpa_onnx

    # Without the core wheel's own copy, Windows quietly binds the engine to an
    # old onnxruntime.dll in System32 and the failure surfaces much later.
    bundled = Path(sherpa_onnx.__file__).parent / "lib" / "onnxruntime.dll"
    if sys.platform == "win32" and not bundled.is_file():
        raise RuntimeError(
            "sherpa-onnx-core is missing or incomplete "
            f"({bundled} not found) — reinstall the requirements"
        )
    build(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transcriptor_prime.diarize_worker")
    parser.add_argument("--source")
    parser.add_argument("--segmentation", required=True)
    parser.add_argument("--embedding", required=True)
    parser.add_argument("--num-speakers", type=int, default=0)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--shift", type=float, default=0.1)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.selftest:
            selftest(args)
            _say(ok=True)
            return 0

        _say(phase="decode")
        samples = decode(args.source)
        if samples.size == 0:
            _say(turns=[])
            return 0

        _say(phase="segment")
        engine = build(args)

        last = 0.0

        def on_progress(done: int, total: int) -> int:
            nonlocal last
            now = time.monotonic()
            if now - last >= _PROGRESS_INTERVAL or done >= total:
                last = now
                _say(progress=[done, total])
            return 0

        result = engine.process(samples, callback=on_progress).sort_by_start_time()
        _say(turns=[[round(t.start, 3), round(t.end, 3), int(t.speaker)] for t in result])
        return 0
    except Exception as exc:
        _say(error=f"{type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
