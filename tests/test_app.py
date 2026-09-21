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
ctk = pytest.importorskip("customtkinter")

from transcriptor_prime import APP_LABEL, ICON_PATH, media  # noqa: E402
from transcriptor_prime import app as app_mod  # noqa: E402
from transcriptor_prime.app import (  # noqa: E402
    TranscriptorApp,
    _apply_icon,
    _language_from_label,
    _language_label,
    _model_from_label,
    _model_label,
)
from transcriptor_prime.settings import LANGUAGES, MODEL_SIZES  # noqa: E402
from transcriptor_prime.transcriber import (  # noqa: E402
    COMPLETED,
    FAILED,
    BatchFinished,
    Done,
    Failed,
    FileFinished,
    FileStarted,
    Progress,
    Status,
)


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
        assert app.btn_cancel.cget("state") == "disabled"
        assert app.btn_start.cget("state") == "normal"
        assert app.btn_open.cget("state") == "disabled"

    def test_running_state_locks_the_inputs(self, app):
        app._set_running(True)
        assert app.btn_start.cget("state") == "disabled"
        assert app.btn_browse.cget("state") == "disabled"
        assert app.cmb_model.cget("state") == "disabled"
        assert app.btn_cancel.cget("state") == "normal"

        app._set_running(False)
        assert app.btn_start.cget("state") == "normal"
        # Comboboxes must go back to readonly, not plain normal, so the user
        # cannot type an invalid model name into them.
        assert app.cmb_model.cget("state") == "readonly"


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
        assert app.var_progress.get() == pytest.approx(0.5)
        assert "50%" in app.var_percent.get()
        assert "00:30:00" in app.var_percent.get()
        assert "01:00:00" in app.var_percent.get()
        assert "remaining" in app.var_status.get()

    def test_done_enables_the_show_button(self, app, tmp_path):
        output = tmp_path / "t.txt"
        output.write_text("x", encoding="utf-8")
        app._set_running(True)

        app._handle(Done(output=output, elapsed=42.0))

        assert app.btn_open.cget("state") == "normal"
        assert app.btn_start.cget("state") == "normal"
        assert app.var_progress.get() == pytest.approx(1.0)
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
        assert app.btn_start.cget("state") == "normal"

    def test_status_messages_reach_the_log(self, app):
        app._handle(Status("Detected language: en"))
        assert "Detected language: en" in app.log.get("1.0", "end")


class TestQueue:
    def test_adding_files_populates_the_queue_and_the_summary(self, app, two_clips):
        app._add_paths(two_clips)
        app._probe_pending_now()

        assert len(app.items) == 2
        assert [Path(i.path).name for i in app._queue_items()] == [
            "alpha.mp3",
            "beta.mp4",
        ]
        assert "2 files" in app.var_source_info.get()
        assert "6s" in app.var_source_info.get()  # two 3-second tones

    def test_duplicate_files_are_not_added_twice(self, app, two_clips):
        app._add_paths(two_clips)
        app._add_paths(two_clips)
        assert len(app.items) == 2

    def test_a_path_differing_only_in_case_is_a_duplicate_on_windows(
        self, app, two_clips
    ):
        app._add_paths([two_clips[0]])
        app._add_paths([Path(str(two_clips[0]).upper())])
        expected = 1 if sys.platform == "win32" else 2
        assert len(app.items) == expected

    def test_removing_the_selection_shrinks_the_queue(self, app, two_clips):
        app._add_paths(two_clips)
        first = app.tree.get_children()[0]
        app.tree.selection_set(first)

        app._on_remove()

        assert len(app.items) == 1
        assert app._queue_items()[0].path.name == "beta.mp4"

    def test_clear_empties_the_queue(self, app, two_clips):
        app._add_paths(two_clips)
        app._on_clear()
        assert app.items == {}
        assert app.tree.get_children() == ()
        assert app.var_source_info.get() == "No files selected."

    def test_a_folder_add_ignores_unsupported_extensions(self, app, two_clips, tmp_path):
        (tmp_path / "readme.txt").write_text("not media", encoding="utf-8")

        app._add_paths(media.media_files_in(tmp_path))

        assert sorted(i.path.name for i in app._queue_items()) == [
            "alpha.mp3",
            "beta.mp4",
        ]

    def test_the_queue_cannot_be_mutated_while_running(self, app, two_clips):
        """A disabled ttk.Treeview still accepts clicks; self.running is the guard."""
        app._add_paths(two_clips)
        app._set_running(True)

        app._on_clear()
        app._on_remove()
        app._on_browse()
        app._on_add_folder()

        assert len(app.items) == 2

    def test_rows_appear_before_they_have_been_read(self, app, two_clips):
        """A folder add must not block on probing; the rows land immediately."""
        app._add_paths(two_clips)

        assert len(app.tree.get_children()) == 2
        assert all(i.info is None for i in app._queue_items())
        assert "still being read" in app.var_source_info.get()

    def test_chunked_probing_fills_the_rows_in_on_the_event_loop(
        self, app, two_clips, monkeypatch
    ):
        monkeypatch.setattr(app_mod, "PROBE_CHUNK", 1)
        app._add_paths(two_clips)

        app._probe_chunk()
        assert [i.info is not None for i in app._queue_items()] == [True, False]

        app._probe_chunk()
        assert all(i.info is not None for i in app._queue_items())
        assert "6s" in app.var_source_info.get()

    def test_clearing_mid_probe_leaves_nothing_to_probe(self, app, two_clips):
        app._add_paths(two_clips)
        app._on_clear()

        app._probe_chunk()  # must not raise or resurrect anything

        assert app.items == {}


class TestQueueOutputPaths:
    def test_two_queued_files_disable_the_save_to_box(self, app, two_clips):
        app._add_paths(two_clips)

        assert app.entry_output.cget("state") == "disabled"
        assert app.btn_saveas.cget("state") == "disabled"
        assert app.var_output.get() == app_mod.BATCH_OUTPUT_HINT

    def test_a_single_queued_file_keeps_the_save_to_box_editable(self, app, two_clips):
        app._set_source(two_clips[0])

        assert app.entry_output.cget("state") == "normal"
        assert Path(app.var_output.get()) == two_clips[0].with_suffix(".txt")

    def test_dropping_back_to_one_file_restores_the_manual_save_path(
        self, app, two_clips
    ):
        app._set_source(two_clips[0])
        app.output_is_manual = True
        app.var_output.set(r"D:\elsewhere\mine.txt")

        app._add_paths([two_clips[1]])
        assert app.var_output.get() == app_mod.BATCH_OUTPUT_HINT

        app.tree.selection_set(app.tree.get_children()[1])
        app._on_remove()

        assert app.var_output.get() == r"D:\elsewhere\mine.txt"
        assert app.entry_output.cget("state") == "normal"

    def test_output_paths_are_unique_within_a_batch(self, app, tmp_path, tone_mp3, tone_mp4):
        """talk.mp3 and talk.mp4 both want talk.txt, and neither exists yet."""
        for name, source in (("talk.mp3", tone_mp3), ("talk.mp4", tone_mp4)):
            (tmp_path / name).write_bytes(source.read_bytes())
        app._add_paths([tmp_path / "talk.mp3", tmp_path / "talk.mp4"])
        app._probe_pending_now()

        jobs = app._build_jobs()

        assert [j.output.name for j in jobs] == ["talk.txt", "talk (2).txt"]

    def test_unreadable_files_are_dropped_from_the_batch(self, app, two_clips, tmp_path):
        broken = tmp_path / "broken.mp3"
        broken.write_text("not audio", encoding="utf-8")
        app._add_paths(two_clips + [broken])
        app._probe_pending_now()

        jobs = app._build_jobs()

        assert [j.source.name for j in jobs] == ["alpha.mp3", "beta.mp4"]
        assert "1 unreadable" in app.var_source_info.get()


class TestBatchEventHandling:
    @pytest.fixture
    def running_app(self, app, two_clips):
        app._add_paths(two_clips)
        app._probe_pending_now()
        app._run_iids = list(app.tree.get_children())
        app._set_running(True)
        return app

    def test_file_started_marks_the_row_running(self, running_app, two_clips):
        running_app._handle(
            FileStarted(index=0, count=2, source=two_clips[0],
                        output=two_clips[0].with_suffix(".txt"), duration=3.0)
        )
        assert running_app._queue_items()[0].status == "running"
        assert "Transcribing" in running_app.tree.set(
            running_app._run_iids[0], "status"
        )

    def test_file_finished_marks_the_row_and_logs_the_output(
        self, running_app, two_clips
    ):
        output = two_clips[0].with_suffix(".txt")
        running_app._handle(
            FileFinished(index=0, source=two_clips[0], output=output,
                         elapsed=2.0, status=COMPLETED)
        )
        assert running_app._queue_items()[0].status == "done"
        assert running_app.batch_outputs == [output]
        assert str(output) in running_app.log.get("1.0", "end")

    def test_a_failed_file_is_logged_without_a_dialog(self, running_app, two_clips, monkeypatch):
        dialogs = []
        monkeypatch.setattr(
            "transcriptor_prime.app.messagebox.showerror",
            lambda *a, **k: dialogs.append(a),
        )
        running_app._handle(
            FileFinished(index=1, source=two_clips[1], output=None, elapsed=1.0,
                         status=FAILED, message="decoder blew up")
        )
        assert running_app._queue_items()[1].status == "failed"
        assert "decoder blew up" in running_app.log.get("1.0", "end")
        assert dialogs == []

    def test_batch_finished_summarises_and_re_enables_the_form(
        self, running_app, two_clips
    ):
        output = two_clips[0].with_suffix(".txt")
        output.write_text("x", encoding="utf-8")

        running_app._handle(
            BatchFinished(completed=1, failed=1, cancelled=0, skipped=0,
                          elapsed=42.0, outputs=(output,))
        )

        status = running_app.var_status.get()
        assert "1 succeeded" in status and "1 failed" in status
        assert running_app.btn_start.cget("state") == "normal"
        assert running_app.btn_open.cget("state") == "normal"
        assert running_app.last_output == output

    def test_batch_finished_marks_unstarted_rows_skipped(self, running_app):
        running_app._handle(
            BatchFinished(completed=0, failed=0, cancelled=0, skipped=2, elapsed=1.0)
        )
        assert [i.status for i in running_app._queue_items()] == ["skipped", "skipped"]

    def test_a_fatal_batch_message_shows_exactly_one_dialog(
        self, running_app, monkeypatch
    ):
        dialogs = []
        monkeypatch.setattr(
            "transcriptor_prime.app.messagebox.showerror",
            lambda *a, **k: dialogs.append(a),
        )
        running_app._handle(
            BatchFinished(completed=0, failed=0, cancelled=0, skipped=2,
                          elapsed=1.0, message="no internet connection")
        )
        assert len(dialogs) == 1

    def test_batch_progress_drives_the_overall_bar(self, app):
        app._handle(
            Progress(audio_done=50.0, audio_total=100.0, elapsed=10.0, eta=10.0,
                     file_index=1, file_count=4, batch_done=150.0, batch_total=400.0)
        )
        assert app.var_batch_progress.get() == pytest.approx(0.375)
        assert app.var_progress.get() == pytest.approx(0.5)
        assert "File 2 of 4" in app.var_batch_percent.get()

    def test_the_overall_bar_is_hidden_for_a_single_file(self, app, two_clips):
        app._set_source(two_clips[0])
        assert not app.progress_batch.winfo_ismapped()

        app._add_paths([two_clips[1]])
        app.root.update_idletasks()
        assert app.progress_batch.winfo_manager() == "grid"


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


def test_start_with_an_empty_queue_warns_instead_of_crashing(app, monkeypatch):
    warned = []
    monkeypatch.setattr(
        "transcriptor_prime.app.messagebox.showwarning",
        lambda *a, **k: warned.append(a),
    )
    app._on_clear()

    app._on_start()

    assert warned
    assert app.worker is None


def test_supported_extensions_cover_the_expected_inputs():
    for extension in (".mp3", ".mp4", ".m4a", ".wav", ".mkv", ".mov"):
        assert extension in media.SUPPORTED_EXTENSIONS


class TestSpinbox:
    """CustomTkinter has no spinbox; widgets.CTkSpinbox stands in for three."""

    def test_stepping_clamps_at_both_bounds(self, app):
        app.var_interval.set(600)
        app.spn_interval._nudge(5)
        assert app.var_interval.get() == 600  # upper bound is 600

        app.var_interval.set(5)
        app.spn_interval._nudge(-5)
        assert app.var_interval.get() == 5  # lower bound is 5

        app.var_interval.set(30)
        app.spn_interval._nudge(5)
        assert app.var_interval.get() == 35

    def test_an_emptied_box_leaves_the_variable_unreadable(self, app):
        """_capture_settings' fallback depends on the TclError still being raised."""
        app.spn_wrap.entry.delete(0, "end")
        app.root.update()

        with pytest.raises(tk.TclError):
            app.var_wrap.get()

        # ...and _capture_settings must survive that rather than propagate it.
        app._capture_settings()
        assert app.settings.wrap_width == 100  # the saved value, untouched

    def test_typed_text_reaches_the_variable(self, app):
        app.spn_threads.entry.delete(0, "end")
        app.spn_threads.entry.insert(0, "12")
        app.root.update()

        assert app.var_threads.get() == 12

    def test_stepping_an_emptied_box_recovers_instead_of_raising(self, app):
        app.spn_wrap.entry.delete(0, "end")

        app.spn_wrap._nudge(10)

        # from_ is 0, so an unreadable box steps up from there.
        assert app.var_wrap.get() == 10

    def test_state_reaches_the_children(self, app):
        app.spn_threads.configure(state="disabled")

        assert app.spn_threads.cget("state") == "disabled"
        assert app.spn_threads.entry.cget("state") == "disabled"
        assert app.spn_threads.btn_up.cget("state") == "disabled"


class TestAppearance:
    def test_the_control_starts_on_the_saved_mode(self, app):
        assert app.seg_appearance.get() == "System"
        assert app.settings.appearance == "system"

    def test_choosing_a_mode_records_it(self, app):
        app._on_appearance_change("Dark")

        assert app.settings.appearance == "dark"
        assert ctk.get_appearance_mode() == "Dark"

        # Leave the interpreter as we found it: the appearance mode is global.
        app._on_appearance_change("System")

    def test_capture_settings_reads_the_control_back(self, app):
        app.seg_appearance.set("Light")

        app._capture_settings()

        assert app.settings.appearance == "light"

    def test_the_queue_tree_is_repainted_for_the_current_mode(self, app):
        """The Treeview is ttk, so it only tracks the theme via this call."""
        from tkinter import ttk

        from transcriptor_prime.widgets import STATUS_COLORS, pick, style_queue_tree

        style_queue_tree(app.tree)

        assert ttk.Style(app.tree).theme_use() == "clam"
        # tag_configure hands back a Tcl object, not a str.
        assert str(app.tree.tag_configure("done", "foreground")) == pick(
            STATUS_COLORS["done"]
        )


def _section_of(app, widget) -> str | None:
    """Title of the section card a widget sits in, however deeply nested."""
    by_card = {card: title for title, card in app.sections.items()}
    while widget is not None:
        if widget in by_card:
            return by_card[widget]
        widget = getattr(widget, "master", None)
    return None


class TestSections:
    """The window is four titled cards, so its parts read as separate."""

    def test_the_cards_run_top_to_bottom_in_reading_order(self, app):
        assert list(app.sections) == ["Files", "Options", "Progress", "Log"]
        rows = [int(card.grid_info()["row"]) for card in app.sections.values()]
        assert rows == sorted(rows)

    def test_every_card_is_outlined_and_has_a_bold_title(self, app):
        """The border is what separates the cards in dark mode."""
        for card in app.sections.values():
            assert card.cget("border_width") >= 1
            assert card.title.cget("font").cget("weight") == "bold"

    def test_each_widget_sits_in_the_section_that_names_it(self, app):
        assert _section_of(app, app.tree) == "Files"
        assert _section_of(app, app.btn_browse) == "Files"
        assert _section_of(app, app.entry_output) == "Files"
        assert _section_of(app, app.chk_subfolders) == "Files"
        assert _section_of(app, app.cmb_model) == "Options"
        assert _section_of(app, app.chk_speakers) == "Options"
        assert _section_of(app, app.progress) == "Progress"
        assert _section_of(app, app.progress_batch) == "Progress"
        assert _section_of(app, app.log) == "Log"
        # The action buttons belong to the window, not to any one section.
        assert _section_of(app, app.btn_start) is None

    def test_the_log_is_the_card_that_absorbs_extra_height(self, app):
        outer = app.sections["Log"].master
        weights = {
            title: outer.grid_rowconfigure(int(card.grid_info()["row"]))["weight"]
            for title, card in app.sections.items()
        }
        assert weights == {"Files": 0, "Options": 0, "Progress": 0, "Log": 1}


class TestUiScale:
    """The Text size control magnifies on top of the display's own scaling."""

    def test_only_sizes_that_fit_the_screen_are_offered(self, app):
        assert list(app.seg_scale.cget("values")) == ["100%", "115%"]

    def test_the_control_starts_on_the_saved_scale(self, app):
        assert app.seg_scale.get() == "100%"
        assert app.settings.ui_scale == 100

    def test_choosing_a_size_records_and_applies_it(self, app):
        try:
            app._on_scale_change("115%")

            assert app.settings.ui_scale == 115
            assert ctk.ScalingTracker.widget_scaling == pytest.approx(1.15)
        finally:
            # Scaling is global to the interpreter; do not leak it into the
            # next test's geometry assertions.
            app._on_scale_change("100%")

    def test_capture_settings_reads_the_control_back(self, app):
        app.seg_scale.set("115%")

        app._capture_settings()

        assert app.settings.ui_scale == 115

    def test_the_window_is_clamped_to_the_desktop(self, app):
        """The largest size must not open taller than the screen can show."""
        from transcriptor_prime.app import _work_area

        avail_w, avail_h = _work_area(app.root)
        app.root.update_idletasks()

        assert app.root.winfo_reqwidth() <= avail_w
        assert app.root.winfo_reqheight() <= avail_h
