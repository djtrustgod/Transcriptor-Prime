"""The Tk user interface.

Everything Tk-related lives here. The transcription itself runs on a worker
thread that pushes events onto a queue; :meth:`TranscriptorApp._poll` drains
that queue on the Tk main loop. No widget is ever touched from the worker.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
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
from transcriptor_prime.transcriber import Done, Failed, Job, Progress, Status, transcribe

PAD = 8


class TranscriptorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = settings_mod.load()

        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.media_info: media.MediaInfo | None = None
        self.output_is_manual = False
        self.last_output: Path | None = None

        self._build_vars()
        self._build_widgets()
        self._set_running(False)

        root.title(APP_LABEL)
        _apply_icon(root)
        root.minsize(720, 620)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

        self._log(f"{APP_LABEL} ready. Everything runs locally on this machine.")
        self._log(f"Model cache: {settings_mod.models_dir()}")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_vars(self) -> None:
        s = self.settings
        self.var_source = tk.StringVar()
        self.var_source_info = tk.StringVar(value="No file selected.")
        self.var_output = tk.StringVar()
        self.var_model = tk.StringVar(value=_model_label(s.model))
        self.var_language = tk.StringVar(value=_language_label(s.language))
        self.var_interval = tk.IntVar(value=s.interval_seconds)
        self.var_threads = tk.IntVar(value=s.resolved_cpu_threads())
        self.var_wrap = tk.IntVar(value=s.wrap_width)
        self.var_condition = tk.BooleanVar(value=s.condition_on_previous_text)
        self.var_status = tk.StringVar(value="Idle.")
        self.var_percent = tk.StringVar(value="")
        self.var_progress = tk.DoubleVar(value=0.0)

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self.root, padding=PAD * 2)
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        row = 0

        # --- Source -----------------------------------------------------
        ttk.Label(frame, text="Source file").grid(row=row, column=0, sticky="w")
        self.entry_source = ttk.Entry(frame, textvariable=self.var_source)
        self.entry_source.grid(row=row, column=1, sticky="ew", padx=(PAD, PAD))
        self.btn_browse = ttk.Button(frame, text="Browse…", command=self._on_browse)
        self.btn_browse.grid(row=row, column=2, sticky="ew")
        row += 1

        self.lbl_info = ttk.Label(
            frame, textvariable=self.var_source_info, foreground="#555555"
        )
        self.lbl_info.grid(row=row, column=1, sticky="w", padx=(PAD, 0), pady=(2, PAD))
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
            text="Defaults to a .txt file beside the source.",
            foreground="#555555",
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

        ttk.Label(frame, textvariable=self.var_status).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, PAD)
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
    # File selection
    # ------------------------------------------------------------------

    def _on_browse(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in media.SUPPORTED_EXTENSIONS)
        audio = " ".join(f"*{ext}" for ext in media.AUDIO_EXTENSIONS)
        video = " ".join(f"*{ext}" for ext in media.VIDEO_EXTENSIONS)
        path = filedialog.askopenfilename(
            title="Choose an audio or video file",
            initialdir=self.settings.last_input_dir or str(Path.home()),
            filetypes=[
                ("Audio and video", patterns),
                ("Audio", audio),
                ("Video", video),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._set_source(Path(path))

    def _set_source(self, path: Path) -> None:
        self.var_source.set(str(path))
        self.settings.last_input_dir = str(path.parent)

        try:
            info = media.probe(path)
        except media.MediaError as exc:
            self.media_info = None
            self.var_source_info.set("Unreadable file.")
            self._log(f"ERROR: {exc}")
            messagebox.showerror(APP_NAME, str(exc))
            return

        self.media_info = info
        self.var_source_info.set(info.summary())
        self._log(f"Selected {path.name} — {info.summary()}")
        self._estimate(info.duration)

        if not self.output_is_manual:
            default = media.default_output_path(path)
            unique = media.unique_path(default)
            if unique != default:
                self._log(
                    f"'{default.name}' already exists — saving as '{unique.name}' instead."
                )
            self.var_output.set(str(unique))

    def _estimate(self, duration: float) -> None:
        """Rough wall-clock warning so a 3-hour file is not a surprise."""
        model = _model_from_label(self.var_model.get())
        # Approximate speed multiples for int8 CPU inference, keyed by model.
        speed = {"tiny": 22.0, "base": 14.0, "small": 6.0, "medium": 2.4, "large-v3": 0.9}
        factor = speed.get(model, 6.0)
        self._log(
            f"Estimated time with '{model}': roughly {format_duration(duration / factor)} "
            "on this CPU (varies with audio quality)."
        )

    def _on_save_as(self) -> None:
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

        source_text = self.var_source.get().strip()
        if not source_text:
            messagebox.showwarning(APP_NAME, "Choose an audio or video file first.")
            return
        source = Path(source_text)

        # The path may have been typed rather than browsed, or the file may have
        # moved since selection — probe again rather than trusting stale info.
        if self.media_info is None or not source.is_file():
            try:
                self.media_info = media.probe(source)
                self.var_source_info.set(self.media_info.summary())
            except media.MediaError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return

        output_text = self.var_output.get().strip()
        if not output_text:
            messagebox.showwarning(APP_NAME, "Choose where to save the transcript.")
            return
        output = Path(output_text)
        if output.suffix.lower() != ".txt":
            output = output.with_suffix(".txt")
            self.var_output.set(str(output))

        if output.exists() and not messagebox.askyesno(
            APP_NAME, f"'{output.name}' already exists. Overwrite it?"
        ):
            return

        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot write to that folder: {exc}")
            return

        self._capture_settings()
        settings_mod.save(self.settings)

        job = Job(
            source=source,
            output=output,
            model=self.settings.model,
            language=self.settings.language,
            interval_seconds=float(self.settings.interval_seconds),
            wrap_width=self.settings.wrap_width,
            cpu_threads=self.settings.resolved_cpu_threads(),
            duration=self.media_info.duration,
            condition_on_previous_text=self.settings.condition_on_previous_text,
        )

        self.cancel.clear()
        self.last_output = None
        self.var_progress.set(0.0)
        self.var_percent.set("")
        self._set_running(True)
        self._log("")
        self._log(f"Starting: {source.name} → {output.name}")

        self.worker = threading.Thread(
            target=transcribe,
            args=(job, self.events.put, self.cancel),
            name="transcriber",
            daemon=True,
        )
        self.worker.start()

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
        s.sanitized()
        self.var_interval.set(s.interval_seconds)
        self.var_wrap.set(s.wrap_width)
        self.var_threads.set(s.resolved_cpu_threads())

    def _on_cancel(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.btn_cancel.config(state="disabled", text="Cancelling…")
            self.var_status.set("Cancelling — finishing the current step…")
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

        elif isinstance(event, Done):
            self._set_running(False)
            self.last_output = event.output
            self.btn_open.config(state="normal")
            if event.partial:
                self.var_status.set("Cancelled — partial transcript saved.")
                self._log(f"Partial transcript saved to {event.output}")
            else:
                self.var_progress.set(100.0)
                self.var_status.set(
                    f"Done in {format_duration(event.elapsed)}."
                )
                self._log(f"Finished in {format_duration(event.elapsed)}.")
                self._log(f"Transcript saved to {event.output}")

        elif isinstance(event, Failed):
            self._set_running(False)
            self.var_status.set("Failed.")
            self._log(f"ERROR: {event.message}")
            messagebox.showerror(APP_NAME, event.message)

    def _set_running(self, running: bool) -> None:
        widgets = [
            self.entry_source, self.btn_browse, self.entry_output, self.btn_saveas,
            self.btn_start, self.spn_interval, self.spn_threads, self.spn_wrap,
            self.chk_condition,
        ]
        for widget in widgets:
            widget.config(state="disabled" if running else "normal")
        for combo in (self.cmb_model, self.cmb_language):
            combo.config(state="disabled" if running else "readonly")

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

    def _on_open(self) -> None:
        if not self.last_output or not self.last_output.exists():
            messagebox.showinfo(APP_NAME, "No transcript to show yet.")
            return
        target = str(self.last_output)
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", target])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", target])
            else:
                subprocess.Popen(["xdg-open", str(self.last_output.parent)])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open the folder: {exc}")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                APP_NAME,
                "A transcription is still running. Quit anyway?\n\n"
                "The text produced so far is already on disk as a .part file.",
            ):
                return
            self.cancel.set()
        self._capture_settings()
        settings_mod.save(self.settings)
        self.root.destroy()


# ----------------------------------------------------------------------
# Combobox label <-> value helpers
# ----------------------------------------------------------------------


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
