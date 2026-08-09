# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
