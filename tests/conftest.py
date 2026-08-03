"""Shared fixtures: synthetic media files built with PyAV."""

from __future__ import annotations

import math
from pathlib import Path

import pytest


def _write_tone(path: Path, seconds: float, container_format: str, codec: str) -> Path:
    """Encode a plain sine tone so tests have a real, decodable media file."""
    import av
    import numpy as np

    sample_rate = 16000
    container = av.open(str(path), mode="w", format=container_format)
    stream = container.add_stream(codec, rate=sample_rate)
    stream.layout = "mono"

    total = int(sample_rate * seconds)
    chunk = 1024
    t0 = 0
    while t0 < total:
        count = min(chunk, total - t0)
        t = (np.arange(t0, t0 + count) / sample_rate).astype("float32")
        samples = (0.2 * np.sin(2 * math.pi * 440.0 * t)).astype("float32")
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="fltp", layout="mono"
        )
        frame.sample_rate = sample_rate
        frame.pts = t0
        for packet in stream.encode(frame):
            container.mux(packet)
        t0 += count

    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return path


@pytest.fixture(scope="session")
def tone_mp3(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("media") / "tone.mp3"
    return _write_tone(path, seconds=3.0, container_format="mp3", codec="mp3")


@pytest.fixture(scope="session")
def tone_mp4(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("media") / "tone.mp4"
    return _write_tone(path, seconds=3.0, container_format="mp4", codec="aac")
