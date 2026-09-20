<img src="src/transcriptor_prime/assets/logo.png" alt="" width="88" align="right">

# Transcriptor Prime 2.0.0

A desktop app that turns audio or video recordings into plain-text transcripts with periodic
timecodes. Everything runs **locally** — no cloud service, no API key, and nothing about your
recordings leaves the machine.

Built for long recordings: a three-hour interview transcribes with a live progress bar and ETA, can
be cancelled at any point, and writes text to disk as it goes so a crash never costs you the whole
run. Queue a folder of them and leave it running.

## Quick start (Windows)

Double-click **`run.bat`**.

The first launch sets up a private Python environment and downloads the dependencies (about
170 MB); it takes a few minutes. Every launch after that opens the app immediately.

Then:

1. **Add files…** and pick one or more recordings — or **Add folder…** to queue everything in a
   folder at once. Each file's length and format appear in the list.
2. Check the **Save to** path — it defaults to a `.txt` beside the source file. With more than one
   file queued it is disabled: every transcript is written beside its own source.
3. Pick a **Model** (see the table below) and press **Start**.

The first transcription with a given model downloads that model's weights (~250 MB for `small`).
That happens once; afterwards the app works fully offline.

To have the transcript say **who is talking**, tick *Identify speakers* first — see
[Identifying speakers](#identifying-speakers).

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

Upgrading from an earlier version needs nothing special: `run.bat` checks the app's dependencies
on every launch and installs anything the existing environment is missing.

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

## Appearance

The window follows the Windows light/dark setting, title bar included, and switches while it is
open — change *Settings → Personalisation → Colours → Choose your mode* and the app follows
without a restart.

To pin it regardless of the desktop, set **Appearance** in Options to *Light* or *Dark*. The
choice is remembered.

If the interface is hard to read, **Text size** in Options enlarges the whole window — 100%, 115%
or 130% on top of whatever Windows display scaling is already set to. 100% is the correct size for
your monitor; the larger settings are deliberate extra magnification. The window never opens taller
than the desktop, so at the largest setting on a small screen the log pane ends up short.

## What the transcript looks like

`Timecode every` (default 30 seconds) controls how often a marker appears. Text is grouped into
paragraphs and each paragraph is stamped with the time its first words were spoken.

```
Transcript: meeting.mp3
Duration:   00:00:31
Model:      small (int8, CPU)
Language:   en (detected, 0.99)
Generated:  2026-08-02 15:28
Created by: Transcriptor Prime 2.0.0

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

## Identifying speakers

Tick **Identify speakers (who said what)** in Options and the transcript says who is talking. A
new paragraph starts whenever the speaker changes, and a long answer still gets a timecode every
interval, with the name repeated:

```
Transcript: interview.mp3
Duration:   00:42:10
Model:      small (int8, CPU)
Language:   en (detected, 0.99)
Speakers:   Interviewer, Jane Doe
Generated:  2026-09-19 10:30
Created by: Transcriptor Prime 2.0.0

------------------------------------------------------------

[00:00:00] Interviewer:
So tell me how you got started in the business.

[00:00:07] Jane Doe:
Well, it was back in 1998 when I first walked into the shop and asked if they needed anyone on
Saturdays.

[00:00:37] Jane Doe:
And that is how the second location opened.
```

The app can tell voices apart but cannot know whose they are, so a fresh transcript says
`Speaker 1`, `Speaker 2`… numbered in the order people first speak. Putting names to them is the
next step:

### Naming the speakers

Press **Name speakers…**. The window lists each voice with a few things it said and a
**Play sample** button that plays a few seconds of that voice from the recording. Type a name
beside each and press **Apply names** — the `.txt` is rewritten in place.

- After a **single file** finishes, the window opens by itself. A **queue** never interrupts:
  when it is done, select a finished row and press **Name speakers…** for each file.
- With nothing selected and no recent run, the button asks for a transcript — so one from last
  week, or from another machine, can be named too. Names live only in the `.txt`; there is no
  second file to keep with it.
- It can be reopened any time to correct a name. Leave a box as it is to keep that label.
- Giving two voices the **same name merges them** into one speaker. That is the fix when one
  person has been split in two — and it cannot be undone, short of transcribing again.
- **Play sample** needs the original recording. It is found automatically while the file is
  still in the queue, or when it sits beside the transcript under its original name.

### Getting the best result

- **Set "Speakers" when you know the number** — 2 for a one-on-one interview. Left at `0` the app
  works the number out for itself, which is the part it is most likely to get wrong. Telling it
  removes that guess. On auto, a "speaker" amounting to only a second or two of sound is folded
  into whoever was talking around it, so a cough does not become a third person.
- It works best on clear recordings with one person talking at a time. Heavy crosstalk, a very
  short interjection, or two similar voices on one poor microphone will produce some mislabelled
  lines. Fix those by hand in the `.txt`; the naming window only changes names.
- The voice models are trained on English speech. Other languages work, less reliably.

### What it costs

Identifying speakers is a separate pass over each recording, *before* transcription starts. The
file's progress bar runs through once for it ("Identifying speakers") and then again for the
transcription; the overall bar for a queue waits during the speaker pass. The first stretch of
the pass reports no percentage — that is normal, not a hang — and **Cancel** works throughout.

Measured on the development machine (8 threads): about **3½ minutes for a 30-minute recording**,
and about **8 minutes for a 3-hour one** (long recordings are analysed at a coarser step). It
needs memory in proportion to length — roughly **2 GB free for a 3-hour recording**. If a file
is too much for the machine, that file is transcribed without speaker labels and the log says
so; nothing is lost.

The first use downloads two small models (about 33 MB) into the same `models` folder as the
Whisper weights. After that it works offline, like everything else.

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
| Model weights (Whisper, and the two speaker models) | `%LOCALAPPDATA%\TranscriptorPrime\models` |
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
`run.bat` checks the app's dependencies on every launch and installs anything missing, so an
upgrade that adds one heals itself. If it still will not start, delete the `.venv` folder and run
it again to rebuild the environment from scratch.

**"Speaker identification could not be set up."**
The first use needs internet to fetch the two speaker models (about 33 MB) from Hugging Face.
For a machine that cannot reach it, run one speaker-labelled transcription on a machine that
can, and copy its `%LOCALAPPDATA%\TranscriptorPrime\models` folder across. If the message
mentions `sherpa-onnx-core`, delete the `.venv` folder and run `run.bat` again. Or untick
*Identify speakers* and transcribe without it.

**Too many speakers, or too few.**
Set **Speakers** in Options to the real number and transcribe again. If one person still comes
out as two, give both the same name in **Name speakers…** to merge them.

**"Speaker identification failed … transcribing without speaker labels."**
That one file was too much for the speaker pass — usually memory, on a very long recording. The
transcript is complete, just unlabelled. Close other programs and try that file again by itself.

**Play sample is greyed out.**
The naming window could not find the recording. Put it back beside the transcript under the
name shown in the transcript's `Transcript:` line, then reopen the window.

**The transcript repeats the same sentence over and over.**
Uncheck *Use preceding text for context* and re-run.

**Everything is too small to read.**
Raise **Text size** in Options. If the whole app looks smaller than other programs at the same
Windows scaling, that is a bug worth reporting — the app should match the display exactly at 100%.

**The window is light while Windows is dark, or the other way round.**
Set *Appearance* in Options back to *System*. If the desktop setting itself changed while the app
was open it should follow within a frame; if it did not, restarting the app will pick it up.

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

This is **2.0.0**, which adds speaker identification and the *Name speakers…* window. A
transcript made without speaker identification is byte-for-byte what 1.0.0 produced. Changes to
the transcript format or the option set from here will be additive, and anything that is not
will get a major version. See
[CHANGELOG.md](CHANGELOG.md).

## Not included

Drag-and-drop, and subtitle (`.srt`) export.

## Credits

Speaker identification runs on [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
(Apache-2.0) with two models the app downloads on first use: the
[pyannote segmentation 3.0](https://huggingface.co/pyannote/segmentation-3.0) model (MIT) and
the [WeSpeaker](https://github.com/wenet-e2e/wespeaker) ResNet34-LM speaker-embedding model,
trained on VoxCeleb (CC BY 4.0). Transcription is [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Design notes

See [ARCHITECTURE.md](ARCHITECTURE.md).
