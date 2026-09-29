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
    {"phase": "voices"}                                <- only when merging to a count
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
            # Always by threshold, even when the user gave a count: the engine's
            # own forced count lets a stray fragment claim one of the slots, and
            # the real voices fuse to make room (see merge_to_count).
            num_clusters=-1,
            threshold=args.threshold,
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("the speaker models could not be loaded")
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def cluster_centroids(samples, turns, embedding_model: str, threads: int) -> dict:
    """One voice print per cluster: unit embeddings summed, weighted by seconds.

    Turns under a second are skipped (too short to embed reliably); long turns
    are cut into slices of at most ten seconds, so a long answer counts for
    what it is worth rather than as a single vote.
    """
    import numpy as np
    import sherpa_onnx

    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=embedding_model, num_threads=threads
        )
    )
    sums: dict[int, np.ndarray] = {}
    for start, end, speaker in turns:
        cursor = start
        while end - cursor >= 1.0:
            stop = min(end, cursor + 10.0)
            stream = extractor.create_stream()
            stream.accept_waveform(
                SAMPLE_RATE,
                samples[int(cursor * SAMPLE_RATE) : int(stop * SAMPLE_RATE)],
            )
            stream.input_finished()
            vector = np.asarray(extractor.compute(stream), dtype=np.float64)
            norm = np.linalg.norm(vector)
            if norm > 0:
                weighted = vector / norm * (stop - cursor)
                sums[speaker] = sums[speaker] + weighted if speaker in sums else weighted
            cursor = stop
    return sums


def merge_to_count(turns, centroids: dict, count: int, scrap_share: float = 0.03):
    """Relabel ``turns`` so that at most ``count`` voices remain.

    The engine's clusters (found by threshold) are merged by voice similarity:
    first each scrap — under ``scrap_share`` of all speech — goes to the most
    alike substantial voice, so a cough or a burst of overlap can never take
    one of the ``count`` places; then the most alike pair is merged until
    ``count`` remain. Fewer clusters than ``count`` are returned as they are:
    two voices the engine fused cannot be pulled apart here.

    ``turns`` are ``(start, end, cluster)``; ``centroids`` maps cluster to a
    summed embedding (see :func:`cluster_centroids`). A cluster with no
    centroid (all its turns under a second) is treated as a scrap.
    """
    import numpy as np

    seconds: dict[int, float] = {}
    for start, end, speaker in turns:
        seconds[speaker] = seconds.get(speaker, 0.0) + (end - start)
    if len(seconds) <= count:
        return [tuple(t) for t in turns]

    owner = {speaker: speaker for speaker in seconds}  # cluster -> surviving group
    voice = {s: np.asarray(v, dtype=np.float64) for s, v in centroids.items()}
    size = dict(seconds)

    def similarity(a: int, b: int) -> float:
        if a not in voice or b not in voice:
            return -1.0
        x, y = voice[a], voice[b]
        return float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y)))

    def merge(into: int, gone: int) -> None:
        for cluster, group in owner.items():
            if group == gone:
                owner[cluster] = into
        size[into] += size.pop(gone)
        if gone in voice:
            voice[into] = voice[into] + voice.pop(gone) if into in voice else voice.pop(gone)

    floor = scrap_share * sum(seconds.values())
    while True:
        scraps = [g for g in size if size[g] < floor or g not in voice]
        substantial = [g for g in size if g not in scraps]
        if not scraps or len(substantial) < count:
            break
        scrap = min(scraps, key=size.get)
        nearest = max(substantial, key=lambda g: (similarity(g, scrap), size[g]))
        merge(nearest, scrap)

    while len(size) > count:
        groups = sorted(size)
        _, a, b = max(
            (similarity(a, b), a, b)
            for i, a in enumerate(groups)
            for b in groups[i + 1 :]
        )
        # The bigger voice keeps its id; it hardly matters which, but it is stable.
        if size[b] > size[a]:
            a, b = b, a
        merge(a, b)

    return [(start, end, owner[speaker]) for start, end, speaker in turns]


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
        turns = [(t.start, t.end, int(t.speaker)) for t in result]
        if args.num_speakers > 0 and len({t[2] for t in turns}) > args.num_speakers:
            _say(phase="voices")
            centroids = cluster_centroids(samples, turns, args.embedding, args.threads)
            turns = merge_to_count(turns, centroids, args.num_speakers)
        _say(turns=[[round(s, 3), round(e, 3), int(who)] for s, e, who in turns])
        return 0
    except Exception as exc:
        _say(error=f"{type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
