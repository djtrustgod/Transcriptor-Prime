"""The version is declared in two places; these keep them from drifting apart."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from transcriptor_prime import APP_LABEL, APP_NAME, __version__

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+( BETA)?", __version__), __version__


def test_app_label_combines_name_and_version():
    assert APP_LABEL == f"{APP_NAME} {__version__}"


def test_pyproject_version_matches_the_package():
    tomllib = pytest.importorskip("tomllib")  # stdlib from Python 3.11
    data = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    # pyproject.toml must hold a PEP 440 version, which spells a beta "b0".
    assert data["project"]["version"] == __version__.replace(" BETA", "b0")


def test_changelog_documents_the_current_version():
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in changelog
