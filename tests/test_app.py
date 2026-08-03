"""GUI smoke tests.

These build the real Tk window. They are skipped automatically where no
display is available (headless CI), but on a desktop they catch the whole
class of layout and wiring mistakes that only surface at widget-construction
time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from transcriptor_prime import APP_LABEL, ICON_PATH, media  # noqa: E402
from transcriptor_prime.app import (  # noqa: E402
    TranscriptorApp,
    _apply_icon,
    _language_from_label,
    _language_label,
    _model_from_label,
    _model_label,
)
from transcriptor_prime.settings import LANGUAGES, MODEL_SIZES  # noqa: E402
from transcriptor_prime.transcriber import Done, Failed, Progress, Status  # noqa: E402


@pytest.fixture(scope="session")
def tk_root():
    """One Tk interpreter for the whole session.

    Creating a fresh ``tk.Tk()`` per test churned through 35 interpreters and
    turned out to fail intermittently. Just as importantly, deciding
    "is there a display?" once means an unexpected TclError inside a test is
    reported as a failure instead of being swallowed as a skip.
    """
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless environment
        pytest.skip(f"no display available: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root, tmp_path, monkeypatch):
    """A fresh app on its own Toplevel, so tests cannot leak state into each other."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = tk.Toplevel(tk_root)
    window.withdraw()
    instance = TranscriptorApp(window)
    window.update()
    yield instance
    window.destroy()


class TestLabelRoundTrips:
    @pytest.mark.parametrize("model", MODEL_SIZES)
    def test_model_label_round_trip(self, model):
        assert _model_from_label(_model_label(model)) == model

    @pytest.mark.parametrize("code", [code for _, code in LANGUAGES])
    def test_language_label_round_trip(self, code):
        assert _language_from_label(_language_label(code)) == code

    def test_unknown_labels_fall_back_to_defaults(self):
        assert _model_from_label("nonsense") == "small"
        assert _language_from_label("Klingon") == "auto"


class TestWindowIcon:
    def test_title_shows_the_name_and_version(self, app):
        assert app.root.title() == APP_LABEL

    @pytest.mark.skipif(sys.platform != "win32", reason=".ico path is Windows-only")
    def test_tk_can_load_the_committed_ico(self, tk_root):
        """A malformed .ico would leave the app silently unbranded.

        ``_apply_icon`` swallows TclError by design, so assert against the raw
        Tk call rather than through it.
        """
        window = tk.Toplevel(tk_root)
        window.withdraw()
        try:
            window.iconbitmap(default=str(ICON_PATH))  # raises TclError if unreadable
        finally:
            window.destroy()

    @pytest.mark.skipif(sys.platform != "win32", reason=".ico path is Windows-only")
    def test_apply_icon_uses_the_ico_with_default_scope(self, tk_root, monkeypatch):
        """``default=`` is what makes later dialogs inherit the icon too."""
        window = tk.Toplevel(tk_root)
        window.withdraw()
        calls = []
        monkeypatch.setattr(
            type(window), "iconbitmap", lambda self, **kwargs: calls.append(kwargs)
        )
        try:
            _apply_icon(window)
        finally:
            window.destroy()
        assert calls == [{"default": str(ICON_PATH)}]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows class-icon lookup")
    def test_windows_gives_the_window_an_icon_handle(self, app):
        """The class icon is exactly what the taskbar and Alt-Tab render."""
        import ctypes

        hwnd = int(app.root.wm_frame(), 16)
        GCLP_HICON, GCLP_HICONSM = -14, -34
        assert ctypes.windll.user32.GetClassLongPtrW(hwnd, GCLP_HICON) != 0
        assert ctypes.windll.user32.GetClassLongPtrW(hwnd, GCLP_HICONSM) != 0

    def test_missing_icon_file_does_not_break_startup(self, tk_root, monkeypatch):
        """A stripped checkout should still run, just without branding."""
        monkeypatch.setattr(
            "transcriptor_prime.app.ICON_PATH", Path("does-not-exist.ico")
        )
        monkeypatch.setattr(
            "transcriptor_prime.app.LOGO_PATH", Path("does-not-exist.png")
        )
        window = tk.Toplevel(tk_root)
        window.withdraw()
        try:
            _apply_icon(window)  # must not raise
        finally:
            window.destroy()


class TestWindow:
    def test_builds_and_starts_idle(self, app):
        assert app.var_status.get() == "Idle."
        assert str(app.btn_cancel["state"]) == "disabled"
        assert str(app.btn_start["state"]) == "normal"
        assert str(app.btn_open["state"]) == "disabled"

    def test_running_state_locks_the_inputs(self, app):
        app._set_running(True)
        assert str(app.btn_start["state"]) == "disabled"
        assert str(app.btn_browse["state"]) == "disabled"
        assert str(app.cmb_model["state"]) == "disabled"
        assert str(app.btn_cancel["state"]) == "normal"

        app._set_running(False)
        assert str(app.btn_start["state"]) == "normal"
        # Comboboxes must go back to readonly, not plain normal, so the user
        # cannot type an invalid model name into them.
        assert str(app.cmb_model["state"]) == "readonly"


class TestSourceSelection:
    def test_selecting_a_file_fills_in_the_default_output_path(self, app, tone_mp3):
        app._set_source(tone_mp3)
        assert app.media_info is not None
        assert Path(app.var_output.get()) == tone_mp3.with_suffix(".txt")
        assert "3s" in app.var_source_info.get()

    def test_existing_transcript_is_not_overwritten_by_default(self, app, tone_mp3):
        taken = tone_mp3.with_suffix(".txt")
        taken.write_text("previous run", encoding="utf-8")
        try:
            app._set_source(tone_mp3)
            assert Path(app.var_output.get()).name == "tone (2).txt"
        finally:
            taken.unlink()

    def test_a_manually_chosen_output_path_survives_a_new_source(self, app, tone_mp3):
        app.output_is_manual = True
        app.var_output.set(r"D:\elsewhere\my transcript.txt")
        app._set_source(tone_mp3)
        assert app.var_output.get() == r"D:\elsewhere\my transcript.txt"

    def test_unreadable_file_reports_an_error_without_raising(
        self, app, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "transcriptor_prime.app.messagebox.showerror", lambda *a, **k: None
        )
        fake = tmp_path / "broken.mp3"
        fake.write_text("not audio", encoding="utf-8")

        app._set_source(fake)

        assert app.media_info is None
        assert app.var_source_info.get() == "Unreadable file."


class TestEventHandling:
    def test_progress_updates_the_bar_and_labels(self, app):
        app._handle(Progress(audio_done=1800.0, audio_total=3600.0, elapsed=300.0, eta=300.0))
        assert app.var_progress.get() == pytest.approx(50.0)
        assert "50%" in app.var_percent.get()
        assert "00:30:00" in app.var_percent.get()
        assert "01:00:00" in app.var_percent.get()
        assert "remaining" in app.var_status.get()

    def test_done_enables_the_show_button(self, app, tmp_path):
        output = tmp_path / "t.txt"
        output.write_text("x", encoding="utf-8")
        app._set_running(True)

        app._handle(Done(output=output, elapsed=42.0))

        assert str(app.btn_open["state"]) == "normal"
        assert str(app.btn_start["state"]) == "normal"
        assert app.var_progress.get() == pytest.approx(100.0)
        assert "Done" in app.var_status.get()

    def test_cancelled_job_is_reported_as_partial(self, app, tmp_path):
        output = tmp_path / "t.partial.txt"
        output.write_text("x", encoding="utf-8")
        app._set_running(True)

        app._handle(Done(output=output, elapsed=5.0, partial=True))

        assert "partial" in app.var_status.get().lower()

    def test_failure_re_enables_the_form(self, app, monkeypatch):
        monkeypatch.setattr(
            "transcriptor_prime.app.messagebox.showerror", lambda *a, **k: None
        )
        app._set_running(True)

        app._handle(Failed(message="something went wrong"))

        assert app.var_status.get() == "Failed."
        assert str(app.btn_start["state"]) == "normal"

    def test_status_messages_reach_the_log(self, app):
        app._handle(Status("Detected language: en"))
        assert "Detected language: en" in app.log.get("1.0", "end")


class TestSettingsCapture:
    def test_widget_values_are_captured_and_clamped(self, app):
        app.var_model.set(_model_label("medium"))
        app.var_language.set(_language_label("fr"))
        app.var_interval.set(45)
        app.var_wrap.set(9999)  # out of range

        app._capture_settings()

        assert app.settings.model == "medium"
        assert app.settings.language == "fr"
        assert app.settings.interval_seconds == 45
        assert app.settings.wrap_width == 300  # clamped


def test_start_without_a_source_warns_instead_of_crashing(app, monkeypatch):
    warned = []
    monkeypatch.setattr(
        "transcriptor_prime.app.messagebox.showwarning",
        lambda *a, **k: warned.append(a),
    )
    app.var_source.set("")

    app._on_start()

    assert warned
    assert app.worker is None


def test_supported_extensions_cover_the_expected_inputs():
    for extension in (".mp3", ".mp4", ".m4a", ".wav", ".mkv", ".mov"):
        assert extension in media.SUPPORTED_EXTENSIONS
