"""The process boundary around speaker identification.

The real engine is never started here: ``_worker_command`` is pointed at tiny
``python -c`` stand-ins that speak the same JSON-lines protocol, which is
enough to pin parsing, progress, cancellation and every way a child can die.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from transcriptor_prime import diarizer
from transcriptor_prime.attribution import Turn


@pytest.fixture
def stub(monkeypatch):
    """Replace the worker with a Python one-liner."""

    def install(script: str) -> diarizer.Diarizer:
        monkeypatch.setattr(
            diarizer,
            "_worker_command",
            lambda: [sys.executable, "-c", textwrap.dedent(script)],
        )
        monkeypatch.setattr(diarizer, "_POLL_INTERVAL", 0.02)
        return diarizer.Diarizer(segmentation=Path("seg.onnx"), embedding=Path("emb.onnx"))

    return install


def run(instance: diarizer.Diarizer, cancel=None, on_progress=None):
    return instance.run(
        Path("talk.mp3"),
        num_speakers=0,
        threads=2,
        duration=60.0,
        cancel=cancel or threading.Event(),
        on_progress=on_progress or (lambda done, total: None),
    )


def test_turns_are_parsed_from_the_last_line(stub):
    instance = stub(
        """
        import json
        print(json.dumps({"phase": "decode"}), flush=True)
        print(json.dumps({"progress": [3, 4]}), flush=True)
        print(json.dumps({"turns": [[0.5, 4.0, 0], [4.2, 9.0, 3]]}), flush=True)
        """
    )
    assert run(instance) == [Turn(0.5, 4.0, 0), Turn(4.2, 9.0, 3)]


def test_progress_is_forwarded(stub):
    instance = stub(
        """
        import json, time
        for k in (1, 2, 3):
            print(json.dumps({"progress": [k, 3]}), flush=True)
            time.sleep(0.1)
        print(json.dumps({"turns": []}), flush=True)
        """
    )
    seen: list[tuple[int, int]] = []
    assert run(instance, on_progress=lambda d, t: seen.append((d, t))) == []
    assert (3, 3) in seen
    assert seen == sorted(seen)


def test_progress_ticks_even_while_the_child_is_silent(stub):
    """The engine's first phase reports nothing; the GUI clock must not stall."""
    instance = stub(
        """
        import json, time
        time.sleep(0.3)
        print(json.dumps({"turns": []}), flush=True)
        """
    )
    seen: list[tuple[int, int]] = []
    run(instance, on_progress=lambda d, t: seen.append((d, t)))
    assert seen.count((0, 0)) >= 3


def test_cancel_kills_the_child_promptly(stub):
    instance = stub("import time; time.sleep(60)")
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(diarizer.Cancelled):
        run(instance, cancel=cancel)
    assert time.monotonic() - started < 3.0
    assert diarizer._active is None


def test_a_reported_error_is_raised(stub):
    instance = stub(
        """
        import json, sys
        print(json.dumps({"error": "RuntimeError: bad model"}), flush=True)
        sys.exit(2)
        """
    )
    with pytest.raises(diarizer.DiarizationError, match="bad model"):
        run(instance)


def test_a_child_that_dies_silently_is_an_error(stub):
    """How a native crash or the engine's own _Exit() presents."""
    instance = stub("import os; os._exit(-1)")
    with pytest.raises(diarizer.DiarizationError, match="stopped unexpectedly"):
        run(instance)


def test_garbage_output_is_an_error_not_a_crash(stub):
    instance = stub("print('Traceback: something native went wrong')")
    with pytest.raises(diarizer.DiarizationError, match="something native"):
        run(instance)


def test_turns_from_a_failed_exit_are_not_trusted(stub):
    instance = stub(
        """
        import json, sys
        print(json.dumps({"turns": [[0, 1, 0]]}), flush=True)
        sys.exit(3)
        """
    )
    with pytest.raises(diarizer.DiarizationError):
        run(instance)


def test_kill_active_stops_a_running_child(stub):
    instance = stub("import time; time.sleep(60)")
    outcome: list[Exception] = []

    def target():
        try:
            run(instance)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    deadline = time.monotonic() + 5
    while diarizer._active is None and time.monotonic() < deadline:
        time.sleep(0.02)
    diarizer.kill_active()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert isinstance(outcome[0], diarizer.DiarizationError)


def test_selftest_failure_carries_the_childs_reason(stub):
    instance = stub(
        """
        import json, sys
        print(json.dumps({"error": "RuntimeError: sherpa-onnx-core is missing"}))
        sys.exit(2)
        """
    )
    with pytest.raises(diarizer.DiarizationError, match="sherpa-onnx-core is missing"):
        instance.selftest()


def test_selftest_passes_quietly(stub):
    stub("print('{\"ok\": true}')").selftest()


def test_the_worker_runs_under_python_not_pythonw(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert diarizer._worker_command()[0] == str(tmp_path / "python.exe")


def test_the_child_can_import_the_package(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = diarizer._child_env()
    assert (Path(env["PYTHONPATH"].split(";")[0]) / "transcriptor_prime").is_dir()


@pytest.mark.parametrize(
    "duration,expected", [(0, 0.1), (3600, 0.1), (3 * 3600, 0.25), (5 * 3600, 0.5)]
)
def test_long_recordings_get_a_coarser_window(duration, expected):
    assert diarizer.window_shift_for(duration) == expected


def test_the_app_never_loads_the_engine_in_process():
    """sherpa_onnx ships its own onnxruntime.dll and must stay in the child."""
    code = (
        "import sys; import transcriptor_prime.app, transcriptor_prime.diarizer, "
        "transcriptor_prime.transcriber; "
        "sys.exit(1 if 'sherpa_onnx' in sys.modules else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], env=diarizer._child_env(), capture_output=True
    )
    assert done.returncode == 0, done.stderr.decode(errors="replace")


@pytest.mark.slow
def test_the_real_engine_runs_end_to_end(tone_mp3: Path):
    """Genuine run: downloads the speaker models (~33 MB) on first use.

    The audio is a sine tone, so there is nobody to find — what is being
    verified is that the models download, the child process starts under this
    interpreter, decodes real media, and its answer makes it back.
    """
    statuses: list[str] = []
    engine = diarizer.prepare(statuses.append)
    assert statuses[-1] == "Speaker identification ready."

    turns = engine.run(
        tone_mp3,
        num_speakers=0,
        threads=2,
        duration=3.0,
        cancel=threading.Event(),
        on_progress=lambda done, total: None,
    )
    assert isinstance(turns, list)
    assert all(turn.end >= turn.start for turn in turns)
