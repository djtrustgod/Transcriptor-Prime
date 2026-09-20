"""Shared fixtures: synthetic media files built with PyAV, and the Tk root.

The GUI fixtures live here rather than in one test module because two modules
build windows, and they must share a single Tk interpreter (see ``tk_root``).
"""

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


@pytest.fixture(scope="session")
def tk_root():
    """One Tk interpreter for the whole session.

    Creating a fresh root per test churned through 35 interpreters and turned
    out to fail intermittently. Just as importantly, deciding "is there a
    display?" once means an unexpected TclError inside a test is reported as a
    failure instead of being swallowed as a skip.

    It is a ``ctk.CTk`` rather than a ``tk.Tk`` because CustomTkinter's scaling
    and appearance trackers walk up ``.master`` looking for the root window and
    register their polling loop against it.
    """
    import tkinter as tk

    import customtkinter as ctk

    try:
        root = ctk.CTk()
    except tk.TclError as exc:  # pragma: no cover - headless environment
        pytest.skip(f"no display available: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root, tmp_path, monkeypatch):
    """A fresh app on its own Toplevel, so tests cannot leak state into each other."""
    import customtkinter as ctk

    from transcriptor_prime.app import TranscriptorApp

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = ctk.CTkToplevel(tk_root)
    window.withdraw()
    instance = TranscriptorApp(window)
    window.update()
    yield instance
    if instance._appearance_callback is not None:
        ctk.AppearanceModeTracker.remove(instance._appearance_callback)
    window.destroy()


@pytest.fixture
def two_clips(tmp_path, tone_mp3, tone_mp4):
    """The synthesized tones copied into one folder, so a queue has real media."""
    first = tmp_path / "alpha.mp3"
    second = tmp_path / "beta.mp4"
    first.write_bytes(tone_mp3.read_bytes())
    second.write_bytes(tone_mp4.read_bytes())
    return [first, second]
