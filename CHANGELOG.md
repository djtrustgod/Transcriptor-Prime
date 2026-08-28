# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-27

The interface is rebuilt on CustomTkinter and follows the Windows light/dark setting.

### Added

- The window now **follows the system light/dark setting**, and switches live: change
  *Settings → Personalisation → Colours → Choose your mode* while the app is open and the
  window, the file queue and the title bar all follow within a frame.
- An **Appearance** control in Options pins the window to *Light* or *Dark* regardless of what
  the desktop is doing. It defaults to *System* and is remembered in `settings.json` as a new
  `appearance` key.
- A **Text size** control in Options — 100%, 115% or 130% — magnifies the whole window on top of
  whatever the display already asks for, for anyone who wants it larger than the system default.
  Remembered as a new `ui_scale` key. The window clamps itself to the desktop work area, so the
  largest setting cannot open with its buttons behind the taskbar.
- `src/transcriptor_prime/widgets.py`, holding the two pieces CustomTkinter does not ship: the
  ttk-to-CustomTkinter colour bridge that paints the file queue, and a spinbox.

### Changed

- **The UI is CustomTkinter** rather than stock `ttk`. `customtkinter==6.0.0` is a new runtime
  dependency; it brings `darkdetect`, which is what reads the Windows theme setting. Both are
  pure Python and add roughly 1 MB to the first-run download.
- `run.bat` now checks that the app's imports resolve before launching, and tops the environment
  up if they do not. Previously dependencies were installed only when `.venv` was first created,
  so an existing install would have started into a missing-module error.
- The **file queue is still a `ttk.Treeview`** — CustomTkinter has no table widget — but is now
  painted from CustomTkinter's active theme and repainted on every light/dark switch. Its status
  colours gained dark-mode variants; the originals were near-black on a dark row.
- The three spinboxes in Options are entry-plus-stepper widgets. Typing something that is not a
  number still leaves the saved value untouched, exactly as before.
- Progress is tracked as a fraction rather than a percentage, matching `CTkProgressBar`. This is
  internal; the displayed percentages and ETAs are unchanged.

### Fixed

- Emptying a spinbox no longer prints an unhandled `TclError` traceback on every keystroke.
- The window is no longer rendered undersized on a high-DPI display. The app asks Windows for
  per-monitor DPI awareness rather than system DPI awareness, which is what lets CustomTkinter
  scale the interface to the monitor; the file queue, which is outside that scaling, is scaled to
  match. On a 150% display the window was a third smaller than it should have been.

## [0.8.0] - 2026-08-08

Batch transcription: queue several files and let the app work through them.

### Added

- A **file queue** replaces the single source picker. **Add files…** takes a
  multi-selection, **Add folder…** queues everything in a directory (with an
  **Include subfolders** option), and **Remove** / **Clear** edit the list.
  Each row shows the file's length, format and live status.
- Batch runs write one `<name>.txt` beside each source, auto-suffixed to
  ` (2)` rather than overwriting. Output names are reserved as the queue is
  planned, so two sources sharing a stem in one folder — `talk.mp3` and
  `talk.mp4` — no longer resolve to the same transcript.
- The Whisper model is loaded **once per queue** instead of once per file,
  which is most of the wall-clock cost of a batch of short recordings.
- A file that fails is logged, marked **Failed** in the list, and skipped — the
  rest of the queue still runs. Only a fatal model-load error raises a dialog.
- A second **overall** progress bar and a `File 2 of 7 · 34% overall` readout,
  shown only when more than one file is queued, plus an end-of-batch summary
  (`3 succeeded · 1 failed · 12m 04s`).
- Double-clicking a finished row reveals that file's transcript.
- `transcriber.transcribe_batch()` and the `FileStarted` / `FileFinished` /
  `BatchFinished` events; `media.media_files_in()` and `media.same_file_key()`.
- An `Include subfolders` preference (`scan_subfolders`).
- App icon: a waveform over two lines of text, drawn at nine sizes (16–256 px) in
  `src/transcriptor_prime/assets/transcriptor-prime.ico`. Below 32 px the design simplifies so it
  stays legible in the taskbar. Regenerate with `tools/make_icon.ps1`.
- The icon is applied to the window, taskbar and Alt-Tab, and inherited by dialogs.
- An explicit Windows AppUserModelID (`TranscriptorPrime.Desktop`), so the app gets its own
  taskbar button instead of grouping under the generic `pythonw.exe` Python icon.
- `install-shortcuts.bat` / `tools/install_shortcuts.ps1` — creates Start Menu and Desktop
  shortcuts carrying the icon and the matching AppUserModelID, so the app can be pinned to the
  taskbar and the pinned button merges with the running window. Supports `-Uninstall` and
  `-NoDesktop`.
- Tests for the icon file layout and the taskbar identity (`tests/test_branding.py`).

### Changed

- **Cancel now stops the whole queue.** The file in flight is still kept as
  `<name>.partial.txt`; files not yet started are marked *Skipped*.
- `Save to` is disabled while two or more files are queued, since each
  transcript goes beside its own source. A single queued file behaves exactly
  as before, manual Save As path included.
- `media.unique_path()` takes a `taken=` set of names to treat as reserved.
- `Progress` carries the queue position and overall totals, so the two progress
  bars are driven by one event and can never disagree.

### Fixed

- GUI tests no longer create a Tk interpreter per test. That churn failed intermittently, and the
  fixture's "no display available" guard would have masked a genuine failure as a skip.

## [0.5.0] - 2026-08-02

First working release. Pre-1.0 while the output format and options settle.

### Added

- Initial release of Transcriptor Prime, a fully local audio/video transcription desktop app.
- Tk GUI (`src/transcriptor_prime/app.py`) with source and destination pickers, model, language,
  timecode-interval, wrap-width and CPU-thread controls, a determinate progress bar with ETA, and
  a log pane.
- Transcription via faster-whisper (CTranslate2, int8, CPU) across five model sizes from `tiny` to
  `large-v3`, defaulting to `small`. Silero VAD is enabled to skip silence.
- Media decoding through PyAV, which bundles FFmpeg — MP3, MP4, MKV, MOV, WAV, FLAC and more work
  with no separate ffmpeg install. Audio is extracted automatically from video containers.
- Plain-text transcripts with a `[HH:MM:SS]` marker per paragraph at a configurable interval
  (default 30 s), preceded by a header recording the source, duration, model and language.
- Output defaults to a `.txt` beside the source file; an existing transcript is auto-suffixed
  rather than overwritten.
- Cancellable jobs. Paragraphs stream to a `.part` file as they are produced and are renamed onto
  the final path atomically on success, so a crash or cancel during a multi-hour run never loses
  the work already done.
- Preferences and downloaded model weights persist under `%LOCALAPPDATA%\TranscriptorPrime`.
- `run.bat` launcher that creates a virtual environment and installs dependencies on first use.
- Version number shown in the window title and the startup log, and stamped into every transcript
  header as a `Created by:` line so a saved transcript can be traced to the build that made it.
  `transcriptor_prime.__version__` is the single source of truth.
- Test suite covering formatting, media probing, settings, the worker (against a stubbed model),
  the GUI and version consistency, plus one `slow`-marked genuine end-to-end transcription.
- `README.md` and `ARCHITECTURE.md`.
