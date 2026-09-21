# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The window is laid out as four outlined cards — Files, Options, Progress and Log — each with
  a bold title**, so the sections read as separate. Before, the section names were the same size
  and weight as every field label, only Options had a visible panel, and the file list, progress
  bars and log ran together.
- The queue summary (*3 files · 2h 14m total*) and **Include subfolders** now sit beside the
  **Files** title instead of under the list, and the status sentence and percentage sit beside
  the **Progress** title instead of taking a row each. That is what pays for the cards' padding:
  at 100% the window is 33 px taller on a 150%-scaled display and the log pane is the same height.
- The "seconds" and "chars (0 = off)" labels in Options line up with their spin boxes; they sat a
  few pixels high.

### Removed

- The **130% Text size**. It already needed more height than a 1504-px-tall display at 150%
  scaling has, leaving a two-line log. 100% and 115% remain; a saved 130% opens at 100%.

## [2.0.0 BETA] - 2026-09-19

The transcript can now say who is talking, and a new window puts names to the voices. This
release is labelled **BETA**: the window title, the startup log and every transcript's
`Created by:` line read `Transcriptor Prime 2.0.0 BETA`. (`pyproject.toml` spells the same
version `2.0.0b0`, the PEP 440 form.)

### Added

- **Speaker identification.** Tick *Identify speakers (who said what)* in Options and the
  transcript names who is talking: `[00:00:07] Speaker 2:` on each paragraph's timecode line, a
  new paragraph on every change of speaker, and a `Speakers:` line in the header. Speakers are
  numbered in the order they first speak. A *Speakers* box takes the number of people when it is
  known, which is markedly more reliable than letting the app work it out.
- **Name speakers… window.** Lists each voice with sample quotes and a **Play sample** button
  that plays a few seconds of it from the recording; type a name beside each and the `.txt` is
  rewritten in place. It opens by itself after a single-file run, never during a queue, and can
  be pointed at any earlier transcript. Two voices given the same name are merged. Names are
  stored only in the transcript — there is no sidecar file.
- The speaker pass runs through [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) in a
  separate process, so it can be cancelled instantly and cannot freeze or crash the window. Its
  two models (about 33 MB) download on first use into the existing `models` folder. No PyTorch,
  no account, no token.
- With the speaker count on auto, a "speaker" amounting to a second or two of sound is folded
  into the surrounding speaker instead of becoming a person of its own.
- A file whose speaker pass fails is still transcribed, without labels, and the log says why. A
  speaker engine that cannot start at all stops the queue before the first file.
- `tools/diarization_spike.py` measures the speaker pass on the current machine.

### Changed

- The file queue shows five rows instead of six, which pays for the new Options row so the log
  pane keeps its height at the largest Text size.
- The first-run dependency download grows from about 150 MB to about 170 MB (`sherpa-onnx` and
  `sherpa-onnx-core`). `run.bat` installs them into an existing environment by itself.
- "Cancelled before any text was produced." is now logged when a cancel lands before a file has
  written anything.
- Transcripts made **without** speaker identification are byte-for-byte what they were in 1.0.0.

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
