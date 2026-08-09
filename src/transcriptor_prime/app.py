"""The Tk user interface.

Everything Tk-related lives here. The transcription itself runs on a worker
thread that pushes events onto a queue; :meth:`TranscriptorApp._poll` drains
that queue on the Tk main loop. No widget is ever touched from the worker.

The window is built around a *queue* of files. One queued file behaves exactly
as a single-file run always has, including an editable "Save to" path; two or
more switch to writing one ``.txt`` beside each source.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from transcriptor_prime import (
    APP_LABEL,
    APP_NAME,
    APP_USER_MODEL_ID,
    ICON_PATH,
    LOGO_PATH,
    media,
)
from transcriptor_prime import settings as settings_mod
from transcriptor_prime.formatting import format_duration, format_timecode
from transcriptor_prime.settings import LANGUAGES, MODEL_NOTES, MODEL_SIZES
from transcriptor_prime.transcriber import (
    CANCELLED,
    COMPLETED,
    BatchFinished,
    Done,
    Failed,
    FileFinished,
    FileStarted,
    Job,
    Progress,
    Status,
    transcribe_batch,
)

PAD = 8

#: Shown in the disabled "Save to" box once the queue holds more than one file.
BATCH_OUTPUT_HINT = "One .txt beside each source file"

#: A folder add is meant for a directory of recordings, not a whole drive.
MAX_QUEUE = 500

#: Files read per event-loop tick while filling in the queue's durations.
PROBE_CHUNK = 4

_STATUS_LABELS = {
    "queued": "Queued",
    "running": "Transcribing…",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "skipped": "Skipped",
    "unreadable": "Unreadable",
}


@dataclass
class QueueItem:
    """One row of the queue. GUI-local and mutable; never crosses a thread."""

    path: Path
    info: media.MediaInfo | None = None
    error: str = ""
    status: str = "queued"
    output: Path | None = None


class TranscriptorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = settings_mod.load()

        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.output_is_manual = False
        self.last_output: Path | None = None
        self.batch_outputs: list[Path] = []

        # The queue. `items` is keyed by Treeview iid; the tree itself owns the
        # ordering. iids come from a counter and are never reused, which is
        # what makes a probe result that lands after a Remove safe to drop.
        self.items: dict[str, QueueItem] = {}
        self._next_iid = 0
        self._run_iids: list[str] = []

        # A "disabled" ttk.Treeview still accepts clicks, so this flag — not the
        # widget state — is what actually protects the queue during a run.
        self.running = False
        self._probe_job: str | None = None
        self._stashed_output = ""
        self._output_stashed = False

        self._build_vars()
        self._build_widgets()
        self._set_running(False)

        root.title(APP_LABEL)
        _apply_icon(root)
        root.minsize(760, 720)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

        self._log(f"{APP_LABEL} ready. Everything runs locally on this machine.")
        self._log(f"Model cache: {settings_mod.models_dir()}")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_vars(self) -> None:
        s = self.settings
        self.var_source_info = tk.StringVar(value="No files selected.")
        self.var_output = tk.StringVar()
        self.var_model = tk.StringVar(value=_model_label(s.model))
        self.var_language = tk.StringVar(value=_language_label(s.language))
        self.var_interval = tk.IntVar(value=s.interval_seconds)
        self.var_threads = tk.IntVar(value=s.resolved_cpu_threads())
        self.var_wrap = tk.IntVar(value=s.wrap_width)
        self.var_condition = tk.BooleanVar(value=s.condition_on_previous_text)
        self.var_subfolders = tk.BooleanVar(value=s.scan_subfolders)
        self.var_status = tk.StringVar(value="Idle.")
        self.var_percent = tk.StringVar(value="")
        self.var_progress = tk.DoubleVar(value=0.0)
        self.var_batch_percent = tk.StringVar(value="")
        self.var_batch_progress = tk.DoubleVar(value=0.0)

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self.root, padding=PAD * 2)
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        row = 0

        # --- Queue --------------------------------------------------------
        ttk.Label(frame, text="Files").grid(row=row, column=0, sticky="nw")

        tree_frame = ttk.Frame(frame)
        tree_frame.grid(row=row, column=1, sticky="ew", padx=(PAD, PAD))
        tree_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("length", "kind", "status"),
            show="tree headings",
            selectmode="extended",
            height=6,
        )
        self.tree.heading("#0", text="File")
        self.tree.heading("length", text="Length")
        self.tree.heading("kind", text="Format")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=280, minwidth=140, stretch=True)
        self.tree.column("length", width=90, minwidth=70, stretch=False, anchor="e")
        self.tree.column("kind", width=130, minwidth=90, stretch=False)
        self.tree.column("status", width=110, minwidth=90, stretch=False)
        self.tree.grid(row=0, column=0, sticky="ew")

        self.tree_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview
        )
        self.tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=self.tree_scroll.set)

        self.tree.tag_configure("done", foreground="#1a7f37")
        self.tree.tag_configure("failed", foreground="#b42318")
        self.tree.tag_configure("unreadable", foreground="#b42318")
        self.tree.tag_configure("cancelled", foreground="#9a6700")
        self.tree.tag_configure("skipped", foreground="#9a6700")

        self.tree.bind("<Double-1>", self._on_row_activate)
        self.tree.bind("<Delete>", lambda _event: self._on_remove())

        queue_buttons = ttk.Frame(frame)
        queue_buttons.grid(row=row, column=2, sticky="new")
        queue_buttons.columnconfigure(0, weight=1)
        self.btn_browse = ttk.Button(
            queue_buttons, text="Add files…", command=self._on_browse
        )
        self.btn_browse.grid(row=0, column=0, sticky="ew")
        self.btn_add_folder = ttk.Button(
            queue_buttons, text="Add folder…", command=self._on_add_folder
        )
        self.btn_add_folder.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.btn_remove = ttk.Button(
            queue_buttons, text="Remove", command=self._on_remove
        )
        self.btn_remove.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.btn_clear = ttk.Button(
            queue_buttons, text="Clear", command=self._on_clear
        )
        self.btn_clear.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        row += 1

        info_row = ttk.Frame(frame)
        info_row.grid(row=row, column=1, sticky="ew", padx=(PAD, 0), pady=(2, PAD))
        info_row.columnconfigure(0, weight=1)
        self.lbl_info = ttk.Label(
            info_row, textvariable=self.var_source_info, foreground="#555555"
        )
        self.lbl_info.grid(row=0, column=0, sticky="w")
        self.chk_subfolders = ttk.Checkbutton(
            info_row,
            text="Include subfolders",
            variable=self.var_subfolders,
        )
        self.chk_subfolders.grid(row=0, column=1, sticky="e")
        row += 1

        # --- Destination ------------------------------------------------
        ttk.Label(frame, text="Save to").grid(row=row, column=0, sticky="w")
        self.entry_output = ttk.Entry(frame, textvariable=self.var_output)
        self.entry_output.grid(row=row, column=1, sticky="ew", padx=(PAD, PAD))
        self.btn_saveas = ttk.Button(frame, text="Save As…", command=self._on_save_as)
        self.btn_saveas.grid(row=row, column=2, sticky="ew")
        row += 1

        ttk.Label(
            frame,
            text="Defaults to a .txt file beside the source. "
            "With more than one file queued, every transcript goes beside its own source.",
            foreground="#555555",
            wraplength=560,
            justify="left",
        ).grid(row=row, column=1, sticky="w", padx=(PAD, 0), pady=(2, PAD))
        row += 1

        # --- Options ----------------------------------------------------
        opts = ttk.LabelFrame(frame, text="Options", padding=PAD)
        opts.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(PAD, PAD))
        opts.columnconfigure(1, weight=1)
        row += 1

        ttk.Label(opts, text="Model").grid(row=0, column=0, sticky="w")
        self.cmb_model = ttk.Combobox(
            opts,
            textvariable=self.var_model,
            values=[_model_label(m) for m in MODEL_SIZES],
            state="readonly",
        )
        self.cmb_model.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(PAD, 0))

        ttk.Label(opts, text="Language").grid(row=1, column=0, sticky="w", pady=(PAD, 0))
        self.cmb_language = ttk.Combobox(
            opts,
            textvariable=self.var_language,
            values=[name for name, _ in LANGUAGES],
            state="readonly",
            width=18,
        )
        self.cmb_language.grid(row=1, column=1, sticky="w", padx=(PAD, 0), pady=(PAD, 0))

        ttk.Label(opts, text="Timecode every").grid(
            row=1, column=2, sticky="e", pady=(PAD, 0)
        )
        self.spn_interval = ttk.Spinbox(
            opts, from_=5, to=600, increment=5, textvariable=self.var_interval, width=6
        )
        self.spn_interval.grid(row=1, column=3, sticky="w", padx=(PAD, 0), pady=(PAD, 0))
        ttk.Label(opts, text="seconds").grid(row=1, column=4, sticky="w", padx=(4, 0))

        ttk.Label(opts, text="CPU threads").grid(row=2, column=0, sticky="w", pady=(PAD, 0))
        self.spn_threads = ttk.Spinbox(
            opts, from_=1, to=64, textvariable=self.var_threads, width=6
        )
        self.spn_threads.grid(row=2, column=1, sticky="w", padx=(PAD, 0), pady=(PAD, 0))

        ttk.Label(opts, text="Wrap at").grid(row=2, column=2, sticky="e", pady=(PAD, 0))
        self.spn_wrap = ttk.Spinbox(
            opts, from_=0, to=300, increment=10, textvariable=self.var_wrap, width=6
        )
        self.spn_wrap.grid(row=2, column=3, sticky="w", padx=(PAD, 0), pady=(PAD, 0))
        ttk.Label(opts, text="chars (0 = off)").grid(row=2, column=4, sticky="w", padx=(4, 0))

        self.chk_condition = ttk.Checkbutton(
            opts,
            text="Use preceding text for context (uncheck if the transcript starts repeating itself)",
            variable=self.var_condition,
        )
        self.chk_condition.grid(row=3, column=0, columnspan=5, sticky="w", pady=(PAD, 0))

        # --- Progress ---------------------------------------------------
        self.progress = ttk.Progressbar(
            frame, orient="horizontal", mode="determinate",
            maximum=100.0, variable=self.var_progress,
        )
        self.progress.grid(row=row, column=0, columnspan=3, sticky="ew")
        row += 1

        ttk.Label(frame, textvariable=self.var_percent).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        row += 1

        # The overall bar only earns its space once there is a queue; with one
        # file the two bars would be pixel-identical.
        self.progress_batch = ttk.Progressbar(
            frame, orient="horizontal", mode="determinate",
            maximum=100.0, variable=self.var_batch_progress,
        )
        self.progress_batch.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(PAD, 0))
        row += 1

        self.lbl_batch = ttk.Label(frame, textvariable=self.var_batch_percent)
        self.lbl_batch.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        row += 1

        self.progress_batch.grid_remove()
        self.lbl_batch.grid_remove()

        ttk.Label(frame, textvariable=self.var_status).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(PAD, PAD)
        )
        row += 1

        # --- Log --------------------------------------------------------
        log_frame = ttk.LabelFrame(frame, text="Log", padding=4)
        log_frame.grid(row=row, column=0, columnspan=3, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        frame.rowconfigure(row, weight=1)
        row += 1

        self.log = ScrolledText(
            log_frame,
            height=10,
            wrap="word",
            state="disabled",
            font=_monospace_font(),
        )
        self.log.grid(row=0, column=0, sticky="nsew")

        # --- Buttons ----------------------------------------------------
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e", pady=(PAD, 0))

        self.btn_open = ttk.Button(
            buttons, text="Show transcript", command=self._on_open, state="disabled"
        )
        self.btn_open.grid(row=0, column=0, padx=(0, PAD))
        self.btn_cancel = ttk.Button(buttons, text="Cancel", command=self._on_cancel)
        self.btn_cancel.grid(row=0, column=1, padx=(0, PAD))
        self.btn_start = ttk.Button(buttons, text="Start", command=self._on_start)
        self.btn_start.grid(row=0, column=2)

    # ------------------------------------------------------------------
    # The queue
    # ------------------------------------------------------------------

    @property
    def media_info(self) -> media.MediaInfo | None:
        """The probe result when — and only when — exactly one file is queued."""
        items = self._queue_items()
        return items[0].info if len(items) == 1 else None

    def _queue_items(self) -> list[QueueItem]:
        """Queue contents in display order. The Treeview owns the ordering."""
        return [self.items[iid] for iid in self.tree.get_children()]

    def _on_browse(self) -> None:
        if self.running:
            return
        patterns = " ".join(f"*{ext}" for ext in media.SUPPORTED_EXTENSIONS)
        audio = " ".join(f"*{ext}" for ext in media.AUDIO_EXTENSIONS)
        video = " ".join(f"*{ext}" for ext in media.VIDEO_EXTENSIONS)
        chosen = filedialog.askopenfilenames(
            title="Choose one or more audio or video files",
            initialdir=self.settings.last_input_dir or str(Path.home()),
            filetypes=[
                ("Audio and video", patterns),
                ("Audio", audio),
                ("Video", video),
                ("All files", "*.*"),
            ],
        )
        # Tk sometimes hands back a single space-containing string rather than a
        # tuple; iterating that would walk it character by character.
        paths = [Path(p) for p in self.root.tk.splitlist(chosen)]
        if paths:
            self._add_paths(paths)

    def _on_add_folder(self) -> None:
        if self.running:
            return
        chosen = filedialog.askdirectory(
            title="Choose a folder of recordings",
            initialdir=self.settings.last_input_dir or str(Path.home()),
            mustexist=True,
        )
        if not chosen:
            return
        recursive = bool(self.var_subfolders.get())
        self.settings.scan_subfolders = recursive
        found = media.media_files_in(chosen, recursive=recursive)
        if not found:
            self._log(f"No supported media files found in {chosen}.")
            messagebox.showinfo(
                APP_NAME, f"No audio or video files were found in:\n\n{chosen}"
            )
            return
        self._log(f"Found {len(found)} file(s) in {chosen}.")
        self._add_paths(found)

    def _add_paths(self, paths: list[Path]) -> None:
        """Append files to the queue, then read their duration and format.

        Rows appear immediately. A single file is probed here and now, so one
        selection keeps the instant feedback it has always had; a folder add
        hands off to :meth:`_schedule_probe`.
        """
        known = {media.same_file_key(item.path) for item in self._queue_items()}
        fresh: list[Path] = []
        for path in paths:
            key = media.same_file_key(path)
            if key in known:
                continue
            known.add(key)
            fresh.append(path)

        if not fresh:
            self._log("Those files are already in the queue.")
            return

        room = MAX_QUEUE - len(self.items)
        if len(fresh) > room:
            dropped = len(fresh) - max(0, room)
            fresh = fresh[: max(0, room)]
            self._log(f"Queue limit is {MAX_QUEUE} files; {dropped} were not added.")
            messagebox.showwarning(
                APP_NAME,
                f"The queue holds at most {MAX_QUEUE} files. "
                f"{dropped} file(s) were not added.",
            )
        if not fresh:
            return

        pending: list[tuple[str, Path]] = []
        for path in fresh:
            iid = f"i{self._next_iid}"
            self._next_iid += 1
            self.items[iid] = QueueItem(path=path)
            self.tree.insert("", "end", iid=iid)
            self._refresh_row(iid)
            pending.append((iid, path))

        self.settings.last_input_dir = str(fresh[0].parent)

        if len(pending) == 1:
            self._probe_pending_now(report_single=True)
        else:
            self._schedule_probe()

        self._after_queue_change()

    def _schedule_probe(self) -> None:
        """Read the queue's media details a few files at a time.

        ``media.probe`` opens the container; 200 of them back to back is a
        visibly frozen window. A background thread looks like the obvious fix,
        but *any* thread that allocates can end up running a Tk object's
        ``__del__`` — a Tcl call — off the main thread, which breaks the one
        rule this app has. Chunking onto the event loop keeps every Tcl call
        where it belongs and still repaints between chunks.
        """
        if self._probe_job is None:
            self._probe_job = self.root.after(1, self._probe_chunk)

    def _probe_chunk(self) -> None:
        self._probe_job = None
        done = 0
        for iid, item in list(self.items.items()):
            if item.info is not None or item.error:
                continue
            self._probe_one(iid, item)
            done += 1
            if done >= PROBE_CHUNK:
                break

        self._refresh_summary()
        if any(i.info is None and not i.error for i in self.items.values()):
            self._schedule_probe()
        else:
            self._refresh_default_output()

    def _probe_one(self, iid: str, item: QueueItem, report_single: bool = False) -> None:
        try:
            item.info = media.probe(item.path)
        except Exception as exc:
            item.error = _probe_message(exc)
            item.status = "unreadable"
            self._log(f"ERROR: {item.error}")
            if report_single:
                messagebox.showerror(APP_NAME, item.error)
        else:
            self._log(f"Added {item.path.name} — {item.info.summary()}")
        self._refresh_row(iid)

    def _probe_pending_now(self, report_single: bool = False) -> None:
        """Probe every still-unread row right now, in one go.

        Used for a single add, and again from ``_on_start`` so no job ever goes
        out with an unknown duration — whatever the chunked pass has not reached
        yet is resolved there. It is also the seam the GUI tests use instead of
        pumping the event loop.
        """
        for iid, item in list(self.items.items()):
            if item.info is None and not item.error:
                self._probe_one(iid, item, report_single)
        self._refresh_summary()

    def _on_remove(self) -> None:
        if self.running:
            return
        selected = self.tree.selection()
        if not selected:
            return
        for iid in selected:
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self._after_queue_change()

    def _on_clear(self) -> None:
        if self.running:
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        self._after_queue_change()

    def _after_queue_change(self) -> None:
        self._refresh_summary()
        self._refresh_output_state()
        self._refresh_default_output()
        self._refresh_batch_widgets()

    def _refresh_row(self, iid: str) -> None:
        item = self.items.get(iid)
        if item is None:
            return
        if item.error:
            length, kind, status = "—", "—", "unreadable"
        elif item.info is None:
            length, kind, status = "…", "reading…", item.status
        else:
            length = format_duration(item.info.duration)
            kind = f"{item.info.container.upper()} / {item.info.audio_codec.upper()}"
            status = item.status
        self.tree.item(
            iid,
            text=item.path.name,
            values=(length, kind, _STATUS_LABELS.get(status, status)),
            tags=(status,),
        )

    def _refresh_summary(self) -> None:
        self.var_source_info.set(self._summary_text())

    def _summary_text(self) -> str:
        items = self._queue_items()
        if not items:
            return "No files selected."
        # A lone unreadable file gets the plain sentence rather than a count.
        if len(items) == 1 and items[0].error:
            return "Unreadable file."
        total = sum(i.info.duration for i in items if i.info)
        text = (
            f"{len(items)} file{'' if len(items) == 1 else 's'} · "
            f"{format_duration(total)} total"
        )
        pending = sum(1 for i in items if i.info is None and not i.error)
        if pending:
            text += f" · {pending} still being read"
        unreadable = sum(1 for i in items if i.error)
        if unreadable:
            text += f" · {unreadable} unreadable"
        return text

    def _refresh_output_state(self) -> None:
        """Own the "Save to" box. Editable for one file, disabled for a queue."""
        multi = len(self.items) >= 2
        if multi and not self._output_stashed:
            self._stashed_output = self.var_output.get()
            self._output_stashed = True
            self.var_output.set(BATCH_OUTPUT_HINT)
        elif not multi and self._output_stashed:
            self.var_output.set(self._stashed_output)
            self._output_stashed = False

        state = "disabled" if (multi or self.running) else "normal"
        self.entry_output.config(state=state)
        self.btn_saveas.config(state=state)

    def _refresh_default_output(self) -> None:
        """Fill in the default save path for a lone file, as selection always has."""
        if self.output_is_manual or self._output_stashed:
            return
        items = self._queue_items()
        if len(items) != 1 or items[0].error:
            if not items:
                self.var_output.set("")
            return
        default = media.default_output_path(items[0].path)
        unique = media.unique_path(default)
        if unique != default:
            self._log(
                f"'{default.name}' already exists — saving as '{unique.name}' instead."
            )
        self.var_output.set(str(unique))

    def _refresh_batch_widgets(self) -> None:
        if len(self.items) >= 2:
            self.progress_batch.grid()
            self.lbl_batch.grid()
        else:
            self.progress_batch.grid_remove()
            self.lbl_batch.grid_remove()

    # ------------------------------------------------------------------
    # Single-file entry point, kept for the "one file" flow
    # ------------------------------------------------------------------

    def _set_source(self, path: Path) -> None:
        """Replace the whole queue with this one file."""
        self._on_clear()
        self._add_paths([path])

    def _estimate(self, duration: float, count: int = 1) -> None:
        """Rough wall-clock warning so a 3-hour queue is not a surprise."""
        model = _model_from_label(self.var_model.get())
        # Approximate speed multiples for int8 CPU inference, keyed by model.
        speed = {"tiny": 22.0, "base": 14.0, "small": 6.0, "medium": 2.4, "large-v3": 0.9}
        factor = speed.get(model, 6.0)
        subject = "1 file" if count == 1 else f"{count} files"
        self._log(
            f"Estimated time for {subject} with '{model}': roughly "
            f"{format_duration(duration / factor)} on this CPU "
            "(varies with audio quality)."
        )

    def _on_save_as(self) -> None:
        if self.running or len(self.items) >= 2:
            return
        current = Path(self.var_output.get()) if self.var_output.get() else None
        initialdir = str(current.parent) if current else (
            self.settings.last_output_dir or str(Path.home())
        )
        path = filedialog.asksaveasfilename(
            title="Save transcript as",
            defaultextension=".txt",
            initialdir=initialdir,
            initialfile=current.name if current else "transcript.txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.var_output.set(path)
            # Remember the explicit choice so picking a new source afterwards
            # does not silently overwrite it.
            self.output_is_manual = True
            self.settings.last_output_dir = str(Path(path).parent)

    # ------------------------------------------------------------------
    # Running a job
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        if not self.items:
            messagebox.showwarning(
                APP_NAME, "Add at least one audio or video file first."
            )
            return

        # Anything the background prober has not reached yet is resolved here,
        # so every Job carries a real duration for the progress denominator.
        self._probe_pending_now()

        self._capture_settings()
        settings_mod.save(self.settings)

        jobs = self._build_jobs()
        if jobs is None:
            return
        if not jobs:
            messagebox.showwarning(
                APP_NAME, "None of the queued files could be read."
            )
            return

        self._estimate(sum(job.duration for job in jobs), len(jobs))

        self.cancel.clear()
        self.last_output = None
        self.batch_outputs = []
        self._run_iids = list(self.tree.get_children())
        for iid in self._run_iids:
            item = self.items[iid]
            if not item.error:
                item.status = "queued"
                item.output = None
            self._refresh_row(iid)

        self.var_progress.set(0.0)
        self.var_batch_progress.set(0.0)
        self.var_percent.set("")
        self.var_batch_percent.set("")
        self._set_running(True)
        self._log("")
        if len(jobs) == 1:
            self._log(f"Starting: {jobs[0].source.name} → {jobs[0].output.name}")
        else:
            self._log(f"Starting a batch of {len(jobs)} files.")

        self.worker = threading.Thread(
            target=transcribe_batch,
            args=(tuple(jobs), self.events.put, self.cancel),
            name="transcriber",
            daemon=True,
        )
        self.worker.start()

    def _build_jobs(self) -> list[Job] | None:
        """Plan one Job per readable queued file. ``None`` means "abort the start"."""
        items = [item for item in self._queue_items() if not item.error]
        for skipped in (i for i in self._queue_items() if i.error):
            self._log(f"Skipping {skipped.path.name}: {skipped.error}")

        single_manual = (
            len(self._queue_items()) == 1 and self.output_is_manual and items
        )
        outputs: list[Path] = []

        if single_manual:
            text = self.var_output.get().strip()
            if not text:
                messagebox.showwarning(APP_NAME, "Choose where to save the transcript.")
                return None
            output = Path(text)
            if output.suffix.lower() != ".txt":
                output = output.with_suffix(".txt")
                self.var_output.set(str(output))
            if output.exists() and not messagebox.askyesno(
                APP_NAME, f"'{output.name}' already exists. Overwrite it?"
            ):
                return None
            outputs.append(output)
        else:
            # Reserve as we go: two queued sources sharing a stem in one folder
            # both plan to <stem>.txt, and nothing is on disk yet to collide with.
            reserved: set[Path] = set()
            for item in items:
                default = media.default_output_path(item.path)
                output = media.unique_path(default, taken=reserved)
                reserved.add(output)
                if output != default:
                    self._log(
                        f"'{default.name}' is taken — saving '{item.path.name}' "
                        f"as '{output.name}'."
                    )
                outputs.append(output)
            if len(items) == 1:
                self.var_output.set(str(outputs[0]))

        for output in outputs:
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"Cannot write to that folder: {exc}")
                return None

        s = self.settings
        jobs = []
        for item, output in zip(items, outputs):
            item.output = output
            jobs.append(
                Job(
                    source=item.path,
                    output=output,
                    model=s.model,
                    language=s.language,
                    interval_seconds=float(s.interval_seconds),
                    wrap_width=s.wrap_width,
                    cpu_threads=s.resolved_cpu_threads(),
                    duration=item.info.duration if item.info else 0.0,
                    condition_on_previous_text=s.condition_on_previous_text,
                )
            )
        return jobs

    def _capture_settings(self) -> None:
        s = self.settings
        s.model = _model_from_label(self.var_model.get())
        s.language = _language_from_label(self.var_language.get())
        try:
            s.interval_seconds = int(self.var_interval.get())
            s.wrap_width = int(self.var_wrap.get())
            s.cpu_threads = int(self.var_threads.get())
        except tk.TclError:
            # A Spinbox left empty or containing junk; fall back to defaults.
            pass
        s.condition_on_previous_text = bool(self.var_condition.get())
        s.scan_subfolders = bool(self.var_subfolders.get())
        s.sanitized()
        self.var_interval.set(s.interval_seconds)
        self.var_wrap.set(s.wrap_width)
        self.var_threads.set(s.resolved_cpu_threads())

    def _on_cancel(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.btn_cancel.config(state="disabled", text="Cancelling…")
            self.var_status.set("Cancelling — finishing the current step…")
            remaining = sum(
                1 for i in self._queue_items() if i.status in ("queued", "running")
            )
            if remaining > 1:
                self._log(
                    "Cancel requested. The current file will be kept as a partial "
                    "transcript; the rest of the queue will be skipped."
                )
            else:
                self._log("Cancel requested. Text produced so far will be kept.")

    # ------------------------------------------------------------------
    # Event pump
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, event) -> None:
        if isinstance(event, Status):
            self.var_status.set(event.message)
            self._log(event.message)

        elif isinstance(event, Progress):
            self.var_progress.set(event.fraction * 100.0)
            eta = (
                f" · about {format_duration(event.eta)} remaining"
                if event.eta is not None
                else ""
            )
            self.var_percent.set(
                f"{event.fraction * 100:.0f}%   "
                f"{format_timecode(event.audio_done)} of "
                f"{format_timecode(event.audio_total)}"
            )
            self.var_status.set(
                f"Transcribing… elapsed {format_duration(event.elapsed)}{eta}"
            )
            self.var_batch_progress.set(event.batch_fraction * 100.0)
            if event.file_count > 1:
                batch_eta = (
                    f" · about {format_duration(event.batch_eta)} left overall"
                    if event.batch_eta is not None
                    else ""
                )
                self.var_batch_percent.set(
                    f"File {event.file_index + 1} of {event.file_count}   "
                    f"{event.batch_fraction * 100:.0f}% overall{batch_eta}"
                )

        elif isinstance(event, FileStarted):
            self._mark(event.index, "running")
            self.var_progress.set(0.0)
            self.var_percent.set("")

        elif isinstance(event, FileFinished):
            status = {COMPLETED: "done", CANCELLED: "cancelled"}.get(
                event.status, "failed"
            )
            item = self._mark(event.index, status)
            if item is not None:
                item.output = event.output
            if event.status == COMPLETED and event.output is not None:
                self.batch_outputs.append(event.output)
                self._log(f"Saved to {event.output}")
            elif event.status == CANCELLED and event.output is not None:
                self._log(f"Partial transcript saved to {event.output}")
            elif event.status not in (COMPLETED, CANCELLED):
                self._log(f"FAILED: {event.source.name} — {event.message}")

        elif isinstance(event, BatchFinished):
            self._finish_batch(event)

        elif isinstance(event, Done):
            # Only a direct transcriber.transcribe() caller produces these; the
            # GUI always goes through transcribe_batch. Kept so the two entry
            # points stay interchangeable.
            self._set_running(False)
            self.last_output = event.output
            self.btn_open.config(state="normal")
            if event.partial:
                self.var_status.set("Cancelled — partial transcript saved.")
                self._log(f"Partial transcript saved to {event.output}")
            else:
                self.var_progress.set(100.0)
                self.var_status.set(f"Done in {format_duration(event.elapsed)}.")
                self._log(f"Finished in {format_duration(event.elapsed)}.")
                self._log(f"Transcript saved to {event.output}")

        elif isinstance(event, Failed):
            self._set_running(False)
            self.var_status.set("Failed.")
            self._log(f"ERROR: {event.message}")
            messagebox.showerror(APP_NAME, event.message)

    def _mark(self, index: int, status: str) -> QueueItem | None:
        """Set the status of the queue row at batch position ``index``."""
        if not 0 <= index < len(self._run_iids):
            return None
        iid = self._run_iids[index]
        item = self.items.get(iid)
        if item is None:  # removed between Start and now — cannot happen today
            return None
        item.status = status
        self._refresh_row(iid)
        self.tree.see(iid)
        return item

    def _finish_batch(self, event: BatchFinished) -> None:
        self._set_running(False)

        # Anything still 'queued' or 'running' never got a FileFinished.
        for iid in self._run_iids:
            item = self.items.get(iid)
            if item is not None and item.status in ("queued", "running"):
                item.status = "skipped"
                self._refresh_row(iid)

        if event.outputs:
            self.last_output = event.outputs[-1]
            self.btn_open.config(state="normal")

        parts = [f"{event.completed} succeeded"]
        if event.failed:
            parts.append(f"{event.failed} failed")
        if event.cancelled:
            parts.append(f"{event.cancelled} cancelled")
        if event.skipped:
            parts.append(f"{event.skipped} not started")
        summary = " · ".join(parts) + f" · {format_duration(event.elapsed)}"

        self.var_status.set(summary)
        self._log(summary)
        if not (event.failed or event.cancelled or event.skipped):
            self.var_progress.set(100.0)
            self.var_batch_progress.set(100.0)

        # A fatal model-load error is the only thing worth a dialog: a single
        # bad file is logged and skipped by design.
        if event.message:
            messagebox.showerror(APP_NAME, event.message)

    def _set_running(self, running: bool) -> None:
        self.running = running
        widgets = [
            self.btn_browse, self.btn_add_folder, self.btn_remove, self.btn_clear,
            self.btn_start, self.spn_interval, self.spn_threads, self.spn_wrap,
            self.chk_condition, self.chk_subfolders,
        ]
        for widget in widgets:
            widget.config(state="disabled" if running else "normal")
        for combo in (self.cmb_model, self.cmb_language):
            combo.config(state="disabled" if running else "readonly")

        # ttk.Treeview has no -state option; config(state=…) raises TclError.
        # This greys the rows, but it does NOT stop clicks — self.running is
        # what actually guards the queue handlers.
        self.tree.state(["disabled"] if running else ["!disabled"])

        self._refresh_output_state()

        self.btn_cancel.config(
            state="normal" if running else "disabled", text="Cancel"
        )
        if running:
            self.btn_open.config(state="disabled")

    def _log(self, message: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _on_row_activate(self, _event=None) -> None:
        """Double-clicking a finished row reveals that file's transcript."""
        selected = self.tree.selection()
        if not selected:
            return
        item = self.items.get(selected[0])
        if item and item.output and item.output.exists():
            _reveal(item.output, select=True)

    def _on_open(self) -> None:
        if not self.last_output or not self.last_output.exists():
            messagebox.showinfo(APP_NAME, "No transcript to show yet.")
            return
        # With a whole batch on disk, singling one out is arbitrary — open the
        # folder instead, and let a double-click on a row pick a specific file.
        try:
            _reveal(self.last_output, select=len(self.batch_outputs) <= 1)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open the folder: {exc}")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            remaining = sum(
                1 for i in self._queue_items() if i.status in ("queued", "running")
            )
            extra = (
                f"\n\n{remaining - 1} more file(s) in the queue will not be started."
                if remaining > 1
                else ""
            )
            if not messagebox.askyesno(
                APP_NAME,
                "A transcription job is still running. Quit anyway?\n\n"
                "The text produced so far is already on disk as a .part file."
                + extra,
            ):
                return
            self.cancel.set()
        self._capture_settings()
        settings_mod.save(self.settings)
        self.root.destroy()


# ----------------------------------------------------------------------
# Combobox label <-> value helpers
# ----------------------------------------------------------------------


def _probe_message(exc: Exception) -> str:
    """Plain text for a probe failure; MediaError already reads well."""
    if isinstance(exc, media.MediaError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _reveal(target: Path, select: bool = True) -> None:
    """Show a file in the platform's file manager."""
    path = str(target)
    if sys.platform == "win32":
        if select:
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(str(target.parent))  # noqa: S606 - a directory we built
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if select else ["open", str(target.parent)])
    else:
        subprocess.Popen(["xdg-open", str(target.parent)])


def _monospace_font() -> tuple[str, int]:
    """A modern fixed-width face, falling back to Tk's default Courier."""
    from tkinter import font as tkfont

    available = set(tkfont.families())
    for family in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono"):
        if family in available:
            return (family, 9)
    return ("Courier", 9)


def _model_label(model: str) -> str:
    return f"{model}  —  {MODEL_NOTES.get(model, '')}"


def _model_from_label(label: str) -> str:
    value = label.split("—")[0].strip()
    return value if value in MODEL_SIZES else "small"


def _language_label(code: str) -> str:
    for name, value in LANGUAGES:
        if value == code:
            return name
    return LANGUAGES[0][0]


def _language_from_label(label: str) -> str:
    for name, value in LANGUAGES:
        if name == label:
            return value
    return "auto"


def run() -> int:
    _enable_dpi_awareness()
    # Must precede the first window: Windows reads the identity when it builds
    # the taskbar button.
    _set_app_user_model_id()
    root = tk.Tk()
    TranscriptorApp(root)
    root.mainloop()
    return 0


def _enable_dpi_awareness() -> None:
    """Stop Tk from rendering blurry on scaled Windows displays."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _set_app_user_model_id() -> None:
    """Give the app its own taskbar button instead of Python's.

    Without an explicit AppUserModelID, Windows identifies the process by its
    executable — ``pythonw.exe`` — so the window inherits the generic Python
    icon and groups with every other Python GUI. Setting it also lets a pinned
    shortcut carrying the same ID merge with the running window, rather than
    sitting beside it as a second button.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except Exception:
        # Cosmetic only; never worth failing startup over.
        pass


def _apply_icon(window: tk.Misc) -> None:
    """Set the window, taskbar and Alt-Tab icon.

    ``iconbitmap(default=…)`` is the Windows path, and also covers dialogs
    opened later. Elsewhere Tk cannot read .ico, so fall back to the PNG.
    """
    if ICON_PATH.exists():
        try:
            window.iconbitmap(default=str(ICON_PATH))
            return
        except tk.TclError:
            pass

    if LOGO_PATH.exists():
        try:
            image = tk.PhotoImage(file=str(LOGO_PATH))
            window.iconphoto(True, image)
            # Tk keeps no reference of its own; without this the image is
            # garbage collected and the icon silently reverts.
            window._transcriptor_icon = image  # type: ignore[attr-defined]
        except tk.TclError:
            pass


__all__ = ["TranscriptorApp", "run"]
