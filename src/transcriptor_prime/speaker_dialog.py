"""The "Name speakers" window.

Speaker identification can only ever say "Speaker 1" and "Speaker 2"; a person
has to say who they are. This window lists each voice found in a transcript
with a few things it said and a button to hear it, takes a name for each, and
has :mod:`transcriptor_prime.speakers` rewrite the ``.txt``.

It works from the transcript alone — there is no sidecar file — so it opens
just as well on a transcript from last month as on the one that just finished.

Threading: decoding a voice sample takes a moment on a long recording, so it
happens on a short-lived thread that touches no widget and hands its result
back through a queue this window polls. Everything else is on the Tk thread.
"""

from __future__ import annotations

import queue
import shutil
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

from transcriptor_prime import APP_NAME, media
from transcriptor_prime import speakers as speakers_mod
from transcriptor_prime.widgets import MUTED_TEXT, work_area

try:  # Windows only; elsewhere the Play buttons are simply unavailable.
    import winsound
except ImportError:  # pragma: no cover - non-Windows
    winsound = None  # type: ignore[assignment]

PAD = 8
_WIDTH = 820  # unscaled units; CustomTkinter applies the display scaling
_QUOTE_WRAP = 600
_CLIP_RATE = 22050


class SpeakerDialog(ctk.CTkToplevel):
    def __init__(
        self,
        parent: tk.Misc,
        parsed: speakers_mod.Parsed,
        *,
        source: Path | None = None,
        on_applied: Callable[[Path, dict[str, str]], None] | None = None,
        on_closed: Callable[[], None] | None = None,
        apply_icon: Callable[[tk.Misc], None] | None = None,
        modal: bool = True,
    ) -> None:
        super().__init__(parent)
        self.parsed = parsed
        self.source = source
        self._on_applied = on_applied
        self._on_closed = on_closed
        self._closed = False

        self.entries: dict[str, ctk.CTkEntry] = {}
        self.play_buttons: dict[str, ctk.CTkButton] = {}

        # Voice samples. `_request` is bumped by every Play and every Stop, so a
        # decode that finishes after the user has moved on is recognisably stale.
        self._request = 0
        self._playing: str | None = None
        self._clips: queue.Queue = queue.Queue()
        self._clip_dir: Path | None = None
        self._reset_job: str | None = None

        self.title(f"Name speakers — {parsed.path.name}")
        if apply_icon is not None:
            # Must happen here, in the constructor: CTkToplevel swaps in its own
            # icon 200 ms after creation unless iconbitmap has been called by then.
            apply_icon(self)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Return>", lambda _event: self._on_apply())

        self._build()
        self._place(parent)

        if modal:
            self.transient(parent)
            self.after(50, self._grab)
        first = next(iter(self.entries.values()), None)
        if first is not None:
            self.after(150, lambda: self._focus(first))

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        heading = ctk.CTkFrame(self, fg_color="transparent")
        heading.grid(row=0, column=0, sticky="ew", padx=PAD * 2, pady=(PAD * 2, PAD))
        ctk.CTkLabel(
            heading,
            text="Who is speaking?",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            heading,
            text="Type a name for each voice. Leave a box alone to keep its label.",
            text_color=MUTED_TEXT,
        ).grid(row=1, column=0, sticky="w")

        count = len(self.parsed.speakers)
        # Tall enough for the usual two to four voices without scrolling, and
        # no taller: a half-empty grey panel makes two speakers look like a bug.
        self.body = ctk.CTkScrollableFrame(self, height=min(520, 30 + count * 130))
        self.body.grid(row=1, column=0, sticky="nsew", padx=PAD * 2)
        self.body.columnconfigure(0, weight=1)
        for index, info in enumerate(self.parsed.speakers):
            self._build_speaker(index, info)

        notes = [
            "Giving two voices the same name merges them into one speaker. "
            "That cannot be undone."
        ]
        if winsound is None:
            notes.append("Playing a voice sample is only available on Windows.")
        elif self.source is None:
            wanted = self.parsed.source_name or "the original recording"
            notes.append(
                f"Play is unavailable: '{wanted}' was not found beside this transcript."
            )
        self.lbl_note = ctk.CTkLabel(
            self,
            text="\n".join(notes),
            text_color=MUTED_TEXT,
            justify="left",
            wraplength=_WIDTH - PAD * 6,
        )
        self.lbl_note.grid(row=2, column=0, sticky="w", padx=PAD * 2, pady=(PAD, 0))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="e", padx=PAD * 2, pady=PAD * 2)
        self.btn_cancel = ctk.CTkButton(
            buttons,
            text="Cancel",
            command=self.close,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        )
        self.btn_cancel.grid(row=0, column=0, padx=(0, PAD))
        self.btn_apply = ctk.CTkButton(buttons, text="Apply names", command=self._on_apply)
        self.btn_apply.grid(row=0, column=1)

    def _build_speaker(self, index: int, info: speakers_mod.SpeakerInfo) -> None:
        card = ctk.CTkFrame(self.body)
        card.grid(row=index, column=0, sticky="ew", pady=(0, PAD), padx=(0, PAD))
        card.columnconfigure(1, weight=1)

        paragraphs = len(info.paragraphs)
        ctk.CTkLabel(
            card, text=info.label, font=ctk.CTkFont(weight="bold"), anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=(PAD * 2, PAD), pady=(PAD, 0))

        entry = ctk.CTkEntry(card, placeholder_text="Name")
        entry.insert(0, info.label)
        entry.grid(row=0, column=1, sticky="ew", pady=(PAD, 0))
        self.entries[info.label] = entry

        playable = (
            winsound is not None
            and self.source is not None
            and speakers_mod.best_clip(info) is not None
        )
        button = ctk.CTkButton(
            card,
            text="▶  Play sample",
            width=130,
            state="normal" if playable else "disabled",
            command=lambda label=info.label: self._on_play(label),
        )
        button.grid(row=0, column=2, padx=PAD * 2, pady=(PAD, 0))
        self.play_buttons[info.label] = button

        ctk.CTkLabel(
            card,
            text=f"{paragraphs} paragraph{'s' if paragraphs != 1 else ''}",
            text_color=MUTED_TEXT,
            anchor="w",
        ).grid(row=1, column=0, sticky="nw", padx=(PAD * 2, PAD), pady=(2, PAD))

        quotes = "\n".join(f"“{quote}”" for quote in speakers_mod.sample_quotes(info))
        ctk.CTkLabel(
            card,
            text=quotes or "(nothing transcribed for this voice)",
            text_color=MUTED_TEXT,
            justify="left",
            anchor="w",
            wraplength=_QUOTE_WRAP,
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, PAD), padx=(0, PAD))

    def _place(self, parent: tk.Misc) -> None:
        """Size to the content, never past the desktop, centred on the parent."""
        self.update_idletasks()
        try:
            scaling = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            scaling = 1.0
        avail_w, avail_h = work_area(self)
        avail_h -= 48  # title bar, and a little breathing room

        width = min(max(self.winfo_reqwidth(), round(_WIDTH * scaling)), avail_w)
        height = min(self.winfo_reqheight(), avail_h)

        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        except tk.TclError:
            x = y = 0
        x = max(0, min(x, avail_w - width))
        y = max(0, min(y, avail_h - height))

        # CTkToplevel.geometry scales the size but not the position, so the size
        # goes in as unscaled units and the offset as device pixels.
        self.geometry(f"{round(width / scaling)}x{round(height / scaling)}+{x}+{y}")
        self.minsize(560, 320)

    def _grab(self, attempts: int = 10) -> None:
        """Make the window modal once it is actually on screen.

        CTkToplevel withdraws and re-shows itself while it colours its title
        bar, and a grab on a window that is not viewable raises.
        """
        if self._closed:
            return
        try:
            self.grab_set()
        except tk.TclError:
            if attempts > 0:
                self.after(50, lambda: self._grab(attempts - 1))

    def _focus(self, entry: ctk.CTkEntry) -> None:
        if self._closed:
            return
        try:
            self.lift()
            entry.focus_set()
            entry.select_range(0, "end")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Applying
    # ------------------------------------------------------------------

    def mapping(self) -> dict[str, str]:
        """``{current label: new name}`` for every box that was really changed."""
        changed: dict[str, str] = {}
        for label, entry in self.entries.items():
            name = speakers_mod.clean_name(entry.get())
            if name and name != label:
                changed[label] = name
        return changed

    def _on_apply(self) -> None:
        mapping = self.mapping()
        if mapping:
            try:
                speakers_mod.rename(self.parsed.path, mapping)
            except OSError as exc:
                messagebox.showerror(
                    APP_NAME,
                    f"Could not update '{self.parsed.path.name}'. If it is open in "
                    f"another program, close it there and try again.\n\n({exc})",
                    parent=self,
                )
                return
            if self._on_applied is not None:
                self._on_applied(self.parsed.path, mapping)
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop()
        if self._clip_dir is not None:
            shutil.rmtree(self._clip_dir, ignore_errors=True)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if self._on_closed is not None:
            self._on_closed()

    # ------------------------------------------------------------------
    # Voice samples
    # ------------------------------------------------------------------

    def _on_play(self, label: str) -> None:
        if self._playing == label:
            self._stop()
            return
        self._stop()

        info = next(s for s in self.parsed.speakers if s.label == label)
        clip = speakers_mod.best_clip(info)
        if clip is None or self.source is None or winsound is None:
            return
        if self._clip_dir is None:
            self._clip_dir = Path(tempfile.mkdtemp(prefix="transcriptor-clips-"))

        self._request += 1
        self._playing = label
        self.play_buttons[label].configure(text="Loading…")
        # A fresh file per request: winsound may still hold the previous one.
        target = self._clip_dir / f"clip-{self._request}.wav"
        threading.Thread(
            target=_decode_clip,
            args=(self._clips, self._request, self.source, clip, target),
            name="voice-sample",
            daemon=True,
        ).start()
        self.after(50, self._poll_clips)

    def _poll_clips(self) -> None:
        if self._closed:
            return
        try:
            request, path, seconds, error = self._clips.get_nowait()
        except queue.Empty:
            if self._playing is not None:
                self.after(50, self._poll_clips)
            return
        if request != self._request or self._playing is None:
            return  # the user pressed Stop, or another Play, in the meantime

        label = self._playing
        if error:
            self._stop()
            messagebox.showwarning(
                APP_NAME, f"Could not play a sample: {error}", parent=self
            )
            return
        winsound.PlaySound(
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
        self.play_buttons[label].configure(text="■  Stop")
        self._reset_job = self.after(int(seconds * 1000) + 200, self._stop)

    def _stop(self) -> None:
        """Silence playback and put every button back. Safe to call any time."""
        self._request += 1
        if self._reset_job is not None:
            try:
                self.after_cancel(self._reset_job)
            except tk.TclError:
                pass
            self._reset_job = None
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except RuntimeError:
                pass
        if self._playing is not None:
            try:
                self.play_buttons[self._playing].configure(text="▶  Play sample")
            except tk.TclError:
                pass
            self._playing = None


def _decode_clip(
    results: queue.Queue,
    request: int,
    source: Path,
    clip: tuple[float, float],
    target: Path,
) -> None:
    """Runs on the sample thread. Touches no widget; reports through ``results``."""
    start, duration = clip
    try:
        pcm = media.decode_clip(source, start, duration, rate=_CLIP_RATE)
        if not pcm:
            raise media.MediaError("that part of the recording has no audio")
        media.write_wav(target, pcm, rate=_CLIP_RATE)
        results.put((request, target, len(pcm) / 2 / _CLIP_RATE, ""))
    except Exception as exc:  # reported in a dialog, never a traceback
        results.put((request, None, 0.0, str(exc)))
