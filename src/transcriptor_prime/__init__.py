"""Transcriptor Prime - a fully local audio/video transcription GUI."""

from pathlib import Path

__version__ = "1.0.0"

APP_NAME = "Transcriptor Prime"

#: Name and version together, e.g. "Transcriptor Prime 1.0.0". Used in the
#: window title and stamped into every transcript header.
APP_LABEL = f"{APP_NAME} {__version__}"

#: Windows taskbar identity. Deliberately excludes the version: Windows keys
#: pinned taskbar buttons off this string, and a pinned icon must survive an
#: upgrade. Shortcuts created by tools/install_shortcuts.ps1 carry the same
#: value so a pinned shortcut and the running window share one button.
APP_USER_MODEL_ID = "TranscriptorPrime.Desktop"

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

#: Multi-resolution Windows icon (16-256 px), used for the window and taskbar.
ICON_PATH = ASSETS_DIR / "transcriptor-prime.ico"

#: 256 px PNG of the same artwork, for documentation and non-Windows platforms.
LOGO_PATH = ASSETS_DIR / "logo.png"
