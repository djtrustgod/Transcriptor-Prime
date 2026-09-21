"""Persisted user preferences and application data locations."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")

# Rough wall-clock cost of a 3-hour recording on a modern laptop CPU with int8
# quantization. Shown in the GUI so the model choice is an informed one.
MODEL_NOTES = {
    "tiny": "fastest, roughest  (~8 min for 3h)",
    "base": "fast, some errors  (~13 min for 3h)",
    "small": "balanced  (~30 min for 3h)",
    "medium": "accurate, slow  (~75 min for 3h)",
    "large-v3": "most accurate, impractical on CPU  (3h+ for 3h)",
}

# Window appearance. "system" follows the Windows light/dark setting live; the
# other two pin it regardless of what the desktop is doing.
APPEARANCE_MODES = ("system", "light", "dark")

# Extra magnification on top of the display's own scaling, as a percentage.
# 100 means "exactly what Windows asks for"; 115 is for anyone who wants the
# window bigger than that. There was a 130 as well, until the window grew
# section cards: at 130 it no longer fitted a 1504-px-tall display at 150%
# scaling with a usable log pane. A saved 130 falls back to 100 in sanitized().
UI_SCALES = (100, 115)

# The most speakers the "Speakers" box will promise the engine. An interview is
# two to four voices; past ten, telling the count is no longer the hard part.
MAX_SPEAKERS = 10

# Whisper language codes worth surfacing; "auto" lets Whisper detect. Forcing a
# language is worth doing when you know it, as detection samples only the
# opening audio and can be wrong on a noisy intro.
LANGUAGES = (
    ("Auto-detect", "auto"),
    ("English", "en"),
    ("Spanish", "es"),
    ("French", "fr"),
    ("German", "de"),
    ("Italian", "it"),
    ("Portuguese", "pt"),
    ("Dutch", "nl"),
    ("Polish", "pl"),
    ("Russian", "ru"),
    ("Chinese", "zh"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Arabic", "ar"),
    ("Hindi", "hi"),
)


def app_data_dir() -> Path:
    """``%LOCALAPPDATA%\\TranscriptorPrime`` on Windows, ``~/.transcriptor-prime`` elsewhere."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "TranscriptorPrime"
    return Path.home() / ".transcriptor-prime"


def models_dir() -> Path:
    """Where Whisper weights are cached after their first download."""
    return app_data_dir() / "models"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def default_cpu_threads() -> int:
    """Half the logical cores, floored at 4.

    On hybrid CPUs (P-cores + E-cores) saturating every logical core tends to
    make CTranslate2 slower, not faster, and leaves nothing for the UI thread.
    """
    return max(4, (os.cpu_count() or 8) // 2)


@dataclass
class Settings:
    model: str = "small"
    language: str = "auto"
    interval_seconds: int = 30
    wrap_width: int = 100
    cpu_threads: int = 0  # 0 means "use default_cpu_threads()"
    condition_on_previous_text: bool = True
    scan_subfolders: bool = False  # whether "Add folder…" recurses
    identify_speakers: bool = False  # label who said what (a slower, extra pass)
    num_speakers: int = 0  # 0 means "let the engine work it out"
    appearance: str = "system"  # one of APPEARANCE_MODES
    ui_scale: int = 100  # one of UI_SCALES; percent, on top of display scaling
    last_input_dir: str = ""
    last_output_dir: str = ""

    def resolved_cpu_threads(self) -> int:
        return self.cpu_threads if self.cpu_threads > 0 else default_cpu_threads()

    def sanitized(self) -> "Settings":
        """Clamp values that a hand-edited settings file could put out of range."""
        if self.model not in MODEL_SIZES:
            self.model = "small"
        if self.language not in {code for _, code in LANGUAGES}:
            self.language = "auto"
        if self.appearance not in APPEARANCE_MODES:
            self.appearance = "system"
        if self.ui_scale not in UI_SCALES:
            self.ui_scale = 100
        self.interval_seconds = max(5, min(600, int(self.interval_seconds)))
        self.wrap_width = max(0, min(300, int(self.wrap_width)))
        self.cpu_threads = max(0, min(64, int(self.cpu_threads)))
        self.condition_on_previous_text = bool(self.condition_on_previous_text)
        self.scan_subfolders = bool(self.scan_subfolders)
        self.identify_speakers = bool(self.identify_speakers)
        self.num_speakers = max(0, min(MAX_SPEAKERS, int(self.num_speakers)))
        return self


def load() -> Settings:
    """Read saved settings; fall back to defaults if absent or corrupt."""
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()

    if not isinstance(raw, dict):
        return Settings()

    known = {f.name for f in fields(Settings)}
    try:
        return Settings(**{k: v for k, v in raw.items() if k in known}).sanitized()
    except (TypeError, ValueError):
        return Settings()


def save(settings: Settings) -> None:
    """Best-effort persist. A failure here must never take the app down."""
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    except OSError:
        pass
