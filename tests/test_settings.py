from __future__ import annotations

import json
from pathlib import Path

import pytest

from transcriptor_prime import settings as settings_mod
from transcriptor_prime.settings import Settings


@pytest.fixture
def app_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path / "TranscriptorPrime"


def test_defaults_when_no_file_exists(app_dir: Path):
    loaded = settings_mod.load()
    assert loaded.model == "small"
    assert loaded.language == "auto"
    assert loaded.interval_seconds == 30


def test_round_trip(app_dir: Path):
    settings_mod.save(Settings(model="medium", language="es", interval_seconds=45))
    loaded = settings_mod.load()
    assert (loaded.model, loaded.language, loaded.interval_seconds) == ("medium", "es", 45)


def test_corrupt_file_falls_back_to_defaults(app_dir: Path):
    path = settings_mod.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert settings_mod.load().model == "small"


def test_unknown_keys_are_ignored(app_dir: Path):
    """A settings file from a future version must not crash an older build."""
    path = settings_mod.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"model": "base", "some_future_option": True}), encoding="utf-8"
    )
    assert settings_mod.load().model == "base"


def test_out_of_range_values_are_clamped(app_dir: Path):
    path = settings_mod.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "model": "enormous",
                "language": "klingon",
                "interval_seconds": 99999,
                "wrap_width": -20,
                "cpu_threads": 5000,
            }
        ),
        encoding="utf-8",
    )
    loaded = settings_mod.load()
    assert loaded.model == "small"
    assert loaded.language == "auto"
    assert loaded.interval_seconds == 600
    assert loaded.wrap_width == 0
    assert loaded.cpu_threads == 64


def test_zero_cpu_threads_resolves_to_a_sensible_default():
    assert Settings(cpu_threads=0).resolved_cpu_threads() >= 4


def test_explicit_cpu_threads_is_respected():
    assert Settings(cpu_threads=3).resolved_cpu_threads() == 3


def test_models_dir_lives_under_the_app_data_dir(app_dir: Path):
    assert settings_mod.models_dir() == app_dir / "models"


def test_save_failure_does_not_raise(monkeypatch, tmp_path: Path):
    """Persisting preferences is best-effort; it must never take the app down."""
    monkeypatch.setattr(
        settings_mod, "settings_path", lambda: tmp_path / "nope" / "s.json"
    )
    monkeypatch.setattr(
        Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("denied"))
    )
    settings_mod.save(Settings())  # must not raise
