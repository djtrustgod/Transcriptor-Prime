<img src="src/transcriptor_prime/assets/logo.png" alt="" width="88" align="right">

# Transcriptor Prime 0.8.0

A desktop app that turns audio or video recordings into plain-text transcripts with periodic
timecodes. Everything runs **locally** — no cloud service, no API key, and nothing about your
recordings leaves the machine.

Built for long recordings: a three-hour interview transcribes with a live progress bar and ETA, can
be cancelled at any point, and writes text to disk as it goes so a crash never costs you the whole
run. Queue a folder of them and leave it running.

## Quick start

Double-click **`run.bat`**.

The first launch sets up a private Python environment and downloads the dependencies (about
150 MB); it takes a few minutes. Every launch after that opens the app immediately.

Then:

1. **Add files…** and pick one or more recordings — or **Add folder…** to queue everything in a
   folder at once. Each file's length and format appear in the list.
2. Check the **Save to** path — it defaults to a `.txt` beside the source file. With more than one
   file queued it is disabled: every transcript is written beside its own source.
3. Pick a **Model** (see the table below) and press **Start**.

The first transcription with a given model downloads that model's weights (~250 MB for `small`).
That happens once; afterwards the app works fully offline.

### Add it to the taskbar

Double-click **`install-shortcuts.bat`** once. It creates a Start Menu entry and a Desktop
shortcut, both carrying the app icon.

To pin it: open Start, find **Transcriptor Prime**, right-click, **Pin to taskbar**.

The shortcuts launch the app directly — no console window — and are stamped with the app's
Windows AppUserModelID, so the pinned button and the running window share one taskbar entry
instead of appearing twice. To remove them again:

```powershell
pwsh -File tools\install_shortcuts.ps1 -Uninstall
```

(Unpin from the taskbar separately; Windows does not allow an app to unpin itself.)

### Requirements

- Windows with Python 3.10 or newer ([python.org](https://python.org))
- **No ffmpeg install needed** — media decoding is built in

## Supported files

Audio: `.mp3` `.m4a` `.wav` `.flac` `.ogg` `.opus` `.aac` `.wma`
Video: `.mp4` `.mkv` `.mov` `.avi` `.webm` `.m4v` — the audio track is extracted automatically

Anything else FFmpeg can read will also work; pick "All files" in the dialog.

## Choosing a model

Transcription runs on the CPU. Bigger models are more accurate and considerably slower. Times are
rough figures for a **three-hour** recording on a modern laptop CPU:

| Model | 3-hour recording | Notes |
|---|---|---|
| `tiny` | ~8 min | Fast, makes real mistakes. Fine for skimming. |
| `base` | ~13 min | Noticeably better than tiny, still quick. |
| **`small`** | **~30 min** | **Default.** Good on clear speech. The best balance. |
| `medium` | ~75 min | Best practical accuracy here. Worth it for difficult audio. |
| `large-v3` | 3 hr+ | Listed for completeness; not practical without a CUDA GPU. |

Speed scales roughly linearly, so a 30-minute recording is about a tenth of the figures above.

Two settings are worth knowing about:

- **Language** — leave on *Auto-detect*, or force a language if you know it. Detection samples only
  the opening audio and can pick wrong on a noisy intro.
- **Use preceding text for context** — on by default and generally better. Turn it off if the
  transcript ever gets stuck repeating a phrase.

## What the transcript looks like

`Timecode every` (default 30 seconds) controls how often a marker appears. Text is grouped into
paragraphs and each paragraph is stamped with the time its first words were spoken.

```
Transcript: meeting.mp3
Duration:   00:00:31
Model:      small (int8, CPU)
Language:   en (detected, 0.99)
Generated:  2026-08-02 15:28
Created by: Transcriptor Prime 0.8.0

------------------------------------------------------------

[00:00:00]
Welcome everyone, and thanks for joining today. We are going to cover the quarterly numbers
first. Revenue came in at $4.2 million, which is about 8% above where we projected.

[00:00:13]
The engineering team shipped 14 releases this quarter, and customer support tickets dropped by
a third. Before I move on to the road map, does anyone have questions about the financial
summary?

[00:00:25]
All right, let us talk about what is planned for the next three months.
```

Set **Wrap at** to `0` if you would rather each paragraph stayed on one long line.

## Long recordings

- The progress bar reflects real position in the audio, and the ETA sharpens after the first
  minute or so.
- **Cancel** stops the run and keeps everything transcribed so far as
  `<name>.partial.txt`. With a queue, it also skips the files that have not started.
- While a job runs, completed paragraphs are already being written to `<name>.txt.part`. If the app
  or the machine dies, that file holds the work up to that moment — rename it to `.txt` to keep it.
- Closing the window mid-job asks first.

## Transcribing several files

The list at the top of the window is a queue. Build it however suits you:

| Button | What it does |
|---|---|
| **Add files…** | Pick one file or several — the dialog allows a multi-selection. |
| **Add folder…** | Queue every supported recording in a folder. Tick **Include subfolders** first to walk it recursively. |
| **Remove** | Drop the selected rows. Also on the <kbd>Delete</kbd> key. |
| **Clear** | Empty the queue. |

Press **Start** and the app works through the list in order, top to bottom. Things worth knowing:

- **Each transcript is written beside its own source** as `<name>.txt`. Nothing is overwritten: an
  existing transcript pushes the new one to `<name> (2).txt`. That also covers the case where
  `talk.mp3` and `talk.mp4` sit in the same folder and both want `talk.txt`.
- **The model loads once for the whole queue**, not once per file. On a batch of short clips that is
  most of the total time saved.
- **A file that cannot be read does not stop the run.** It is logged, its row turns *Failed*, and
  the queue moves on. There is no dialog to dismiss — check the log when it finishes.
- **Two progress bars.** The top one is the file being transcribed; the second, which appears only
  when there is more than one file, is the queue as a whole.
- **Cancel stops everything.** The file in flight is kept as `<name>.partial.txt`; the rest are
  marked *Skipped*.
- When it finishes you get a one-line summary — `3 succeeded · 1 failed · 12m 04s`. **Show
  transcript** opens the output folder, and double-clicking any finished row reveals that file.

A single queued file behaves exactly as it always has, **Save to** box and all.

## Where things are stored

| What | Where |
|---|---|
| Model weights | `%LOCALAPPDATA%\TranscriptorPrime\models` |
| Preferences | `%LOCALAPPDATA%\TranscriptorPrime\settings.json` |
| Python environment | `.venv\` in this folder |
| Start Menu shortcut | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Transcriptor Prime\` |
| App icon | `src\transcriptor_prime\assets\transcriptor-prime.ico` |

Deleting any of these is safe — the app rebuilds or re-downloads what it needs. To reclaim disk
space, delete the `models` folder; to reset every setting, delete `settings.json`.

## Running from a terminal

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m transcriptor_prime
```

Tests:

```powershell
.venv\Scripts\python.exe -m pytest              # everything
.venv\Scripts\python.exe -m pytest -m "not slow" # skip the real-model run
```

The `slow` test performs a genuine transcription with the `tiny` model and downloads it on first
use.

## Troubleshooting

**"Could not download the model — check your internet connection."**
The first use of each model size needs internet. Once downloaded it is cached permanently.

**The app does not start after `run.bat`.**
Delete the `.venv` folder and run it again to rebuild the environment from scratch.

**The transcript repeats the same sentence over and over.**
Uncheck *Use preceding text for context* and re-run.

**Poor accuracy.**
Move up a model size. `medium` handles accents, crosstalk and background noise far better than
`small`, at roughly 2.5× the time.

**The taskbar shows a generic Python icon.**
The app was launched some other way than the shortcut. Use the Start Menu entry created by
`install-shortcuts.bat`.

**The pinned icon and the running window are two separate taskbar buttons.**
The shortcut predates the AppUserModelID stamping. Unpin it, re-run `install-shortcuts.bat`, and
pin again from the Start Menu.

## Version

The running version appears in the window title and the first line of the log. Every transcript
also records it in its `Created by:` header line.

This is **0.8.0** — working and tested, but pre-1.0 while the output format and the option set
settle. See [CHANGELOG.md](CHANGELOG.md).

## Not included

Speaker labels ("who said what"), drag-and-drop, and subtitle (`.srt`) export.

## Design notes

See [ARCHITECTURE.md](ARCHITECTURE.md).
