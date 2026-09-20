"""The speaker option, the "Name speakers…" button and the naming window.

Shares the session-wide Tk root in conftest.py (one interpreter per session).
The dialog is always built with ``modal=False`` here: a real grab would seize
the keyboard of whoever is running the tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")
ctk = pytest.importorskip("customtkinter")

from test_speakers import BODY, HEADER  # noqa: E402

from transcriptor_prime import speaker_dialog, speakers  # noqa: E402
from transcriptor_prime import app as app_mod  # noqa: E402
from transcriptor_prime.speaker_dialog import SpeakerDialog  # noqa: E402
from transcriptor_prime.transcriber import (  # noqa: E402
    CANCELLED,
    COMPLETED,
    BatchFinished,
    FileFinished,
    Progress,
)


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "interview.txt"
    path.write_text(HEADER + BODY, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def quiet(monkeypatch):
    """Record message boxes instead of showing them."""
    shown: dict[str, list] = {"info": [], "error": [], "warning": []}
    for kind in shown:
        for module in ("app", "speaker_dialog"):
            monkeypatch.setattr(
                f"transcriptor_prime.{module}.messagebox.show{kind}",
                lambda *a, _kind=kind, **k: shown[_kind].append(a),
            )
    return shown


class FakeSound:
    SND_FILENAME, SND_ASYNC, SND_NODEFAULT, SND_PURGE = 1, 2, 4, 8

    def __init__(self):
        self.calls: list[tuple] = []

    def PlaySound(self, sound, flags):  # noqa: N802 - winsound's own name
        self.calls.append((sound, flags))


@pytest.fixture
def sound(monkeypatch):
    fake = FakeSound()
    monkeypatch.setattr(speaker_dialog, "winsound", fake)
    return fake


@pytest.fixture
def dialog(tk_root, transcript, tone_mp3, sound, quiet):
    applied: list = []
    closed: list = []
    window = SpeakerDialog(
        tk_root,
        speakers.parse(transcript),
        source=tone_mp3,
        on_applied=lambda path, mapping: applied.append((path, mapping)),
        on_closed=lambda: closed.append(True),
        modal=False,
    )
    window.withdraw()
    window.update()
    window.applied, window.was_closed = applied, closed  # type: ignore[attr-defined]
    yield window
    window.close()


def type_into(entry, text: str) -> None:
    entry.delete(0, "end")
    entry.insert(0, text)


class TestOptions:
    def test_the_speaker_count_follows_the_checkbox(self, app):
        app.var_speakers.set(False)
        app._refresh_speaker_state()
        assert app.spn_speakers.cget("state") == "disabled"
        app.var_speakers.set(True)
        app._refresh_speaker_state()
        assert app.spn_speakers.cget("state") == "normal"

    def test_a_run_locks_the_speaker_controls(self, app):
        app.var_speakers.set(True)
        app._set_running(True)
        assert app.chk_speakers.cget("state") == "disabled"
        assert app.spn_speakers.cget("state") == "disabled"
        assert app.btn_name_speakers.cget("state") == "disabled"
        app._set_running(False)
        assert app.chk_speakers.cget("state") == "normal"
        assert app.spn_speakers.cget("state") == "normal"
        assert app.btn_name_speakers.cget("state") == "normal"

    def test_unlocking_keeps_the_count_greyed_when_the_box_is_clear(self, app):
        app.var_speakers.set(False)
        app._set_running(True)
        app._set_running(False)
        assert app.spn_speakers.cget("state") == "disabled"

    def test_the_choice_is_captured_clamped_and_saved(self, app):
        app.var_speakers.set(True)
        app.var_num_speakers.set(99)
        app._capture_settings()
        assert app.settings.identify_speakers is True
        assert app.settings.num_speakers == 10
        assert app.var_num_speakers.get() == 10

    def test_jobs_carry_the_speaker_options(self, app, two_clips):
        app._add_paths(two_clips)
        app._probe_pending_now()
        app.var_speakers.set(True)
        app.var_num_speakers.set(3)
        app._capture_settings()
        jobs = app._build_jobs()
        assert {(j.identify_speakers, j.num_speakers) for j in jobs} == {(True, 3)}

    def test_speakers_are_off_by_default(self, app, two_clips):
        app._add_paths(two_clips[:1])
        app._probe_pending_now()
        app._capture_settings()
        (job,) = app._build_jobs()
        assert (job.identify_speakers, job.num_speakers) == (False, 0)

    def test_the_speaker_pass_is_shown_as_its_own_phase(self, app):
        app._handle(
            Progress(audio_done=30, audio_total=60, elapsed=12, eta=None, phase="speakers")
        )
        assert app.var_progress.get() == pytest.approx(0.5)
        assert app.var_percent.get() == "Identifying speakers   50%"
        assert app.var_status.get().startswith("Identifying speakers…")

    def test_the_silent_start_of_the_pass_shows_no_percentage(self, app):
        app._handle(
            Progress(audio_done=0, audio_total=60, elapsed=3, eta=None, phase="speakers")
        )
        assert app.var_percent.get() == "Identifying speakers…"


class TestNameSpeakersButton:
    @pytest.fixture
    def opened(self, app, monkeypatch):
        """Capture what the app would have opened, without building a window."""
        calls: list[dict] = []

        class Recorder:
            def __init__(self, parent, parsed, **kwargs):
                calls.append(dict(parsed=parsed, **kwargs))

            def close(self):
                pass

        monkeypatch.setattr(app_mod, "SpeakerDialog", Recorder)
        return calls

    def test_a_selected_finished_row_is_the_target(self, app, two_clips, transcript, opened):
        app._add_paths(two_clips)
        app._probe_pending_now()
        iid = app.tree.get_children()[1]
        app.items[iid].output = transcript
        app.tree.selection_set(iid)

        app._on_name_speakers()

        (call,) = opened
        assert call["parsed"].path == transcript
        # The queue's own record of the recording beats the header's file name.
        assert call["source"] == two_clips[1]

    def test_otherwise_the_last_transcript_is(self, app, transcript, opened):
        app.last_output = transcript
        app._on_name_speakers()
        assert [c["parsed"].path for c in opened] == [transcript]

    def test_with_nothing_to_hand_the_user_is_asked_for_a_file(
        self, app, transcript, opened, monkeypatch
    ):
        monkeypatch.setattr(
            "transcriptor_prime.app.filedialog.askopenfilename",
            lambda **k: str(transcript),
        )
        app._on_name_speakers()
        assert [c["parsed"].path for c in opened] == [transcript]

    def test_dismissing_the_file_dialog_opens_nothing(self, app, opened, monkeypatch):
        monkeypatch.setattr(
            "transcriptor_prime.app.filedialog.askopenfilename", lambda **k: ""
        )
        app._on_name_speakers()
        assert opened == []

    def test_a_transcript_without_labels_gets_an_explanation(
        self, app, tmp_path, opened, quiet
    ):
        plain = tmp_path / "plain.txt"
        plain.write_text("Transcript: a.mp3\n\n[00:00:00]\nHello.\n\n", encoding="utf-8")
        app.last_output = plain
        app._on_name_speakers()
        assert opened == []
        assert "no speaker labels" in quiet["info"][0][1]

    def test_nothing_opens_during_a_run(self, app, transcript, opened):
        app.last_output = transcript
        app._set_running(True)
        app._on_name_speakers()
        assert opened == []
        app._set_running(False)

    def test_only_one_naming_window_at_a_time(self, app, transcript, opened):
        app.last_output = transcript
        app._on_name_speakers()
        app._on_name_speakers()
        assert len(opened) == 1


class TestAutoOpen:
    @pytest.fixture
    def opened(self, app, monkeypatch):
        targets: list[Path] = []
        monkeypatch.setattr(app, "_open_speaker_dialog", targets.append)
        # Run the scheduled call now instead of 150 ms from now.
        monkeypatch.setattr(app.root, "after", lambda _ms, fn=None, *a: fn and fn(*a))
        return targets

    def finish(self, app, paths, transcript, *, speakers_found, status=COMPLETED):
        app._add_paths(paths)
        app._probe_pending_now()
        app._run_iids = list(app.tree.get_children())
        app._run_speakers = []
        app._set_running(True)
        completed = 0
        for index, path in enumerate(paths):
            output = transcript if status == COMPLETED else None
            app._handle(
                FileFinished(index, path, output, 1.0, status, speakers=speakers_found)
            )
            completed += status == COMPLETED
        app._handle(
            BatchFinished(
                completed=completed,
                failed=0,
                cancelled=len(paths) - completed,
                skipped=0,
                elapsed=1.0,
                outputs=(transcript,) * completed,
            )
        )

    def test_a_single_labelled_file_opens_the_naming_window(
        self, app, two_clips, transcript, opened
    ):
        self.finish(app, two_clips[:1], transcript, speakers_found=2)
        assert opened == [transcript]

    def test_a_batch_never_interrupts(self, app, two_clips, transcript, opened):
        self.finish(app, two_clips, transcript, speakers_found=2)
        assert opened == []

    def test_an_unlabelled_transcript_does_not(self, app, two_clips, transcript, opened):
        self.finish(app, two_clips[:1], transcript, speakers_found=0)
        assert opened == []

    def test_a_cancelled_run_does_not(self, app, two_clips, transcript, opened):
        self.finish(app, two_clips[:1], transcript, speakers_found=2, status=CANCELLED)
        assert opened == []


class TestDialog:
    def test_every_speaker_gets_a_prefilled_box(self, dialog):
        assert {label: e.get() for label, e in dialog.entries.items()} == {
            "Speaker 1": "Speaker 1",
            "Speaker 2": "Speaker 2",
        }

    def test_untouched_boxes_change_nothing(self, dialog, transcript):
        before = transcript.read_bytes()
        dialog._on_apply()
        assert transcript.read_bytes() == before
        assert dialog.applied == [] and dialog.was_closed == [True]

    def test_apply_renames_the_transcript(self, dialog, transcript):
        type_into(dialog.entries["Speaker 1"], "  Interviewer ")
        type_into(dialog.entries["Speaker 2"], "Jane Doe:")

        dialog._on_apply()

        text = transcript.read_text(encoding="utf-8")
        assert "[00:00:00] Interviewer:\n" in text
        assert "[00:00:07] Jane Doe:\n" in text
        assert dialog.applied == [
            (transcript, {"Speaker 1": "Interviewer", "Speaker 2": "Jane Doe"})
        ]
        assert dialog.was_closed == [True]

    def test_a_cleared_box_keeps_the_label(self, dialog):
        type_into(dialog.entries["Speaker 1"], "")
        type_into(dialog.entries["Speaker 2"], "Jane Doe")
        assert dialog.mapping() == {"Speaker 2": "Jane Doe"}

    def test_a_locked_file_keeps_the_window_open(self, dialog, quiet, monkeypatch):
        def refuse(path, mapping):
            raise PermissionError("in use")

        monkeypatch.setattr(speakers, "rename", refuse)
        type_into(dialog.entries["Speaker 1"], "Interviewer")

        dialog._on_apply()

        assert "close it there and try again" in quiet["error"][0][1]
        assert dialog.was_closed == [] and dialog.winfo_exists()

    def test_cancel_changes_nothing(self, dialog, transcript):
        before = transcript.read_bytes()
        type_into(dialog.entries["Speaker 1"], "Interviewer")
        dialog.close()
        assert transcript.read_bytes() == before
        assert dialog.applied == []

    def test_closing_twice_is_harmless(self, dialog):
        dialog.close()
        dialog.close()
        assert dialog.was_closed == [True]

    def test_the_window_fits_the_desktop(self, dialog):
        from transcriptor_prime.widgets import work_area

        avail_w, avail_h = work_area(dialog)
        width, height = (int(n) for n in dialog.geometry().split("+")[0].split("x"))
        assert width <= avail_w and height <= avail_h

    def test_without_the_recording_play_is_disabled_and_explained(
        self, tk_root, transcript, sound
    ):
        window = SpeakerDialog(
            tk_root, speakers.parse(transcript), source=None, modal=False
        )
        window.withdraw()
        try:
            assert {b.cget("state") for b in window.play_buttons.values()} == {"disabled"}
            assert "interview.mp3" in window.lbl_note.cget("text")
        finally:
            window.close()


class TestPlayback:
    def pump(self, dialog, until, timeout=5.0):
        import time

        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            dialog.update()
            time.sleep(0.02)
        assert until()

    def test_play_decodes_a_sample_and_plays_it(self, dialog, sound):
        dialog._on_play("Speaker 1")
        assert dialog.play_buttons["Speaker 1"].cget("text") == "Loading…"

        self.pump(dialog, lambda: any(call[0] for call in sound.calls))

        played = next(call[0] for call in sound.calls if call[0])
        assert Path(played).is_file() and Path(played).suffix == ".wav"
        assert dialog.play_buttons["Speaker 1"].cget("text") == "■  Stop"

    def test_pressing_the_button_again_stops_it(self, dialog, sound):
        dialog._on_play("Speaker 1")
        self.pump(dialog, lambda: any(call[0] for call in sound.calls))
        dialog._on_play("Speaker 1")
        assert sound.calls[-1] == (None, sound.SND_PURGE)
        assert dialog.play_buttons["Speaker 1"].cget("text") == "▶  Play sample"

    def test_closing_silences_playback_and_removes_the_clips(self, dialog, sound):
        dialog._on_play("Speaker 1")
        self.pump(dialog, lambda: any(call[0] for call in sound.calls))
        clip_dir = dialog._clip_dir
        dialog.close()
        assert sound.calls[-1] == (None, sound.SND_PURGE)
        assert not clip_dir.exists()

    def test_a_sample_that_cannot_be_read_is_reported(
        self, tk_root, transcript, tmp_path, sound, quiet
    ):
        bogus = tmp_path / "interview.mp3"
        bogus.write_text("not audio")
        window = SpeakerDialog(
            tk_root, speakers.parse(transcript), source=bogus, modal=False
        )
        window.withdraw()
        try:
            window._on_play("Speaker 2")
            self.pump(window, lambda: bool(quiet["warning"]))
            assert "Could not play a sample" in quiet["warning"][0][1]
            assert window.play_buttons["Speaker 2"].cget("text") == "▶  Play sample"
        finally:
            window.close()
