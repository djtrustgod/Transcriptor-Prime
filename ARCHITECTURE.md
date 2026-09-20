# Architecture

## Layout

```
src/transcriptor_prime/
  __init__.py    version, app name, taskbar identity, asset paths
  __main__.py    entry point; turns a startup crash into a dialog
  app.py         the window, file queue, event pump
  speaker_dialog.py  the "Name speakers" window
  widgets.py     the queue's ttk theming and a spinbox   (these three = all Tk)
  transcriber.py worker thread wrapping faster-whisper   (no Tk imports)
  diarizer.py    launches and supervises the speaker engine's child process
  diarize_worker.py  that child: the only importer of sherpa_onnx
  attribution.py which speaker said each word            (pure functions)
  speakers.py    reads and renames speakers in a saved .txt (pure functions)
  formatting.py  timecodes and paragraph grouping        (pure functions)
  media.py       PyAV probe, folder scan, output naming
  settings.py    JSON preferences + app data locations
  assets/        transcriptor-prime.ico, logo.png
tools/
  make_icon.ps1        redraws the icon; run only when the artwork changes
  install_shortcuts.ps1 Start Menu + Desktop shortcuts for taskbar pinning
  diarization_spike.py  times the speaker pass on this machine; not part of the app
```

The dependency direction is one-way: `app` → `transcriber` → `diarizer`/`attribution`/`formatting`/
`settings`. Nothing below `app`/`speaker_dialog`/`widgets` imports Tk, and `formatting` imports nothing from the project at all, which is what
makes the transcript rules testable in milliseconds without a model or a display.

## Why this stack

**faster-whisper (CTranslate2), not openai-whisper.** Roughly 4–5× faster on CPU at the same model
size, runs int8-quantized, and pulls in no PyTorch. Critically, `transcribe()` returns a *lazy
generator* of segments rather than a finished result — so the app can show genuine progress and
write output incrementally instead of blocking for half an hour on an opaque call.

**PyAV for decoding.** Its wheels bundle FFmpeg's libraries, so MP3, MP4, MKV and the rest decode
with no system ffmpeg install — a meaningful simplification for a double-click desktop app. The
same library probes duration and stream layout up front.

**CustomTkinter, over stock tkinter.** The app is one window, so a heavyweight toolkit buys
nothing — but stock `ttk` has no notion of a colour scheme, and a permanently light-grey window
beside a dark desktop is the first thing anyone notices. CustomTkinter is a widget set drawn on Tk
canvases, so it keeps Tk's event loop, `after()`, variables and geometry managers exactly as they
were; the migration touched the widgets and nothing below them. It is pure Python and pulls in only
`darkdetect` and `packaging`, so `run.bat` still has no compiled dependency to install for the UI.

What it does not have is a table widget, a spinbox, a label-frame, or replacements for `filedialog`
and `messagebox`. The first two are dealt with below; a label-frame is a heading label above a
plain frame; and the two dialog modules stay stock, which on Windows means they are native.

**Silero VAD (`vad_filter=True`).** Voice-activity detection skips silence, which both speeds up
long recordings and suppresses Whisper's habit of inventing text over quiet passages — the failure
mode that most often ruins a multi-hour transcript.

## Threading contract

Tk is not thread-safe, so the rule is absolute: **only the main thread touches a widget.**

```
main thread                          worker thread
-----------                          -------------
_on_start()
  plans one Job per queued file
  Thread(transcribe_batch) ──────>   transcribe_batch(jobs, emit, cancel)
                                       _build_model()      — once per queue
                                       per file:
                                         emit(FileStarted)     ─┐
  root.after(100, _poll)                  emit(Status/Progress) │
  _poll() drains the queue  <────────     emit(FileFinished)    ├─> queue.Queue
    _handle(event) updates widgets     emit(BatchFinished)     ─┘
```

`emit` is just `queue.Queue.put`. Every event type — `Status`, `Progress`, `Done`, `Failed`,
`FileStarted`, `FileFinished`, `BatchFinished` — is a frozen dataclass, and the GUI hands the worker
a `tuple` of frozen `Job`s, so nothing mutable crosses the boundary in either direction. `_poll`
reschedules itself every 100 ms for the life of the window.

Progress events are throttled to one every 0.5 s. Whisper emits a segment every few seconds of
*audio*, which on a fast model is many per second of wall clock; unthrottled it would flood the
queue and thrash the disk.

**There is exactly one background thread.** Reading a queued file's duration would be the obvious
second one, but see "Probing on the event loop" below — it is not, deliberately. Speaker
identification is a second *process*, supervised from this same thread without adding another
(see "Why a subprocess"). The one exception is the naming window's Play button, which decodes a
few seconds of audio on a short-lived thread of its own.

### Cancellation

A `threading.Event` checked once per segment. Because segments arrive every few hundred
milliseconds, a cancel during transcription lands almost immediately.

The exception is model loading: an in-flight HTTP download of the weights cannot be interrupted, so
cancellation is honoured at a checkpoint right after the load instead. The button relabels to
"Cancelling…" to make the wait legible. This phase lasts seconds against a run that may last hours,
so a hard kill is not worth the complexity of a subprocess.

Cancel abandons the **whole queue**, not just the current file: one button, one meaning. The file in
flight still lands as `<name>.partial.txt`; the rest are counted in `BatchFinished.skipped` and never
get a `FileStarted`. A "skip just this one" affordance would need a second button and has no obvious
use for a queue you walked away from.

The worker is a daemon thread, so it cannot keep a closed app alive.

## Batches

`transcribe_batch()` is the GUI's only entry point — a single file is a queue of one, so there is no
second code path to keep in step. Three decisions shape it.

**The model is loaded once.** `_build_model()` was split out of the old `_load_model` precisely so a
queue could hoist it: constructing a `WhisperModel` costs seconds and hundreds of megabytes, and
paying that per file would dominate a batch of short clips. `_run_one()` takes an already-loaded
model and is what both entry points share.

**One bad file does not end the run.** `_run_one` never raises; it returns a `_Result` carrying
`COMPLETED` / `CANCELLED` / `FAILED` and, on failure, the text from `_friendly_error()`. The loop
reports it as `FileFinished` and moves on.

**`transcribe_batch` never emits `Done` or `Failed`.** Both of those unlock the GUI's form, so
either one mid-queue would re-enable Start while the worker was still running. The terminal event is
always `BatchFinished`, on every path including an empty queue and a fatal model load — the latter
rides on `BatchFinished.message`, which is the only thing in a batch that raises a dialog.

### Progress across a queue

`Progress` carries the queue position and totals alongside the per-file numbers rather than there
being a separate `BatchProgress` event. The two progress bars are two views of one instant; one
event means one throttle and no way for them to disagree. Every batch field is defaulted, so a
single-file run constructs a `Progress` exactly as it always did.

Two details the naive version gets wrong:

- **The denominator is summed *probe* durations, but `audio_done` counts what Whisper decoded**, and
  the two disagree by a second or two on VBR MP3. The in-flight file's contribution is clamped with
  `min(audio_done, job.duration)` or the overall bar creeps past 100% on a long queue.
- **The batch ETA is timed from after the model load.** A first-time weight download is minutes;
  folding it into the throughput estimate would poison the ETA for the next two hours.

If any queued file has an unknown duration the summed denominator would be a lie, so `batch_total`
is set to `0.0` and `batch_fraction` falls back to counting files. A coarse bar beats a wrong one.

### Naming outputs before they exist

`media.unique_path()` grew a `taken=` set. A batch plans every output path up front, when no
transcript exists yet to collide with — so `talk.mp3` and `talk.mp4` in one folder would both
resolve to `talk.txt` and the second run would silently clobber the first. `_build_jobs()`
accumulates the reservation as it goes. Same stem in *different* folders was never a problem: the
`.part` file lives beside its own output.

### Probing on the event loop

`media.probe()` opens the container, which is 5-20 ms for a local file and can be a second on a
network share. Doing 200 of them inline when someone adds a folder is a visibly frozen window.

The obvious fix is a background thread, and that is what this originally was — until the test suite
started reporting `RuntimeError: main thread is not in main loop` from `Variable.__del__`. Any thread
that allocates can trigger the GC pass that finalises an orphaned Tk object, and the finaliser calls
into Tcl. The thread never touched a widget and still broke the one rule this app has.

So `_schedule_probe()` walks the queue `PROBE_CHUNK` files per `after(1, …)` tick instead. Rows
appear immediately with a `reading…` placeholder and fill in over the next few frames, the event loop
repaints between chunks, and every Tcl call stays on the main thread. `_probe_pending_now()` resolves
whatever is left synchronously — `_on_start` calls it so no `Job` ever goes out with an unknown
duration, and the GUI tests use it instead of pumping the event loop.

## Durability: `.part` files and atomic rename

Losing two and a half hours of transcription to a crash at hour three would be the app's worst
failure, so output is never held in memory:

1. Write the header to `<output>.txt.part` and flush.
2. As each paragraph closes, append it and flush periodically.
3. On success, `os.replace()` the `.part` onto `<output>.txt` — atomic on the same volume, so the
   real path either does not exist or is complete. There is no window in which a reader sees a
   half-written transcript. In a batch each file gets its own `.part` beside its own output, so a
   crash at file seven leaves the first six complete and the seventh recoverable.
4. On cancel, rename to `<output>.partial.txt` instead — the distinct name makes it obvious the
   text is incomplete.
5. On an exception, delete the `.part` and report a `Failed` event. (A crash of the whole process
   leaves the `.part` behind on purpose; it is the recovery copy.)

## Paragraph grouping

`ParagraphBuilder` accumulates segments and closes a block once its span reaches the configured
interval. Two decisions worth recording:

**The stamp is the first segment's actual start, not a rounded boundary.** Writing `[00:01:00]` when
speech resumed at 01:07 would point the reader at silence. Real start times mean the marker always
lands on a word.

**A segment longer than the interval closes its own block.** No block is ever split mid-segment,
because Whisper's segments are the smallest unit with a reliable timestamp.

## Speaker identification

Optional, off by default, and additive: with it off, no code path, Whisper argument or output byte
differs from before it existed (`test_the_off_path_output_is_what_it_always_was`).

```
per file, on the worker thread:
  diarizer.run()  ── spawns ──>  python -m transcriptor_prime.diarize_worker   (child process)
     polls a spool file  <─────    decode → segment → embed → cluster → {"turns": [...]}
  model.transcribe(word_timestamps=True)
     per segment: attribution.split_segment() → ParagraphBuilder.add_run() → .part file
```

**sherpa-onnx, not pyannote.audio.** pyannote is the reference implementation, but it needs
PyTorch (a 2 GB install against this app's 170 MB), a Hugging Face account, and a token pasted
into the app. sherpa-onnx runs the *same* pyannote segmentation model plus a speaker-embedding
model through ONNX: a 19 MB wheel, 33 MB of ungated models, CPU only. That matches everything
under "Why this stack".

**The voice model was chosen by measurement, after shipping the wrong one.** The embedding model
decides whether two voices can be told apart at all; everything downstream only arranges its
answer. The first choice, 3D-Speaker's CAM++, was the fastest candidate and passed a clean
16-second studio sample — and on the first real recording, a radio interview between two men, it
labelled one man's answer as a conversation. Seven candidates were then run on that interview,
forced to two speakers, scored as the share of speech attributed to the right person:

| Model | Correct | Size |
|---|---|---|
| **WeSpeaker ResNet34-LM** (in use) | 92% | 27 MB |
| NeMo TitaNet large | 92% | 101 MB |
| 3D-Speaker ERes2NetV2 (zh) | 92% | 71 MB, 3x slower |
| 3D-Speaker ERes2Net | 80% | 26 MB |
| NeMo TitaNet small | 80% | 40 MB |
| WeSpeaker CAM++ | 76% | 29 MB |
| 3D-Speaker CAM++ (first choice) | 69% | 30 MB |

92% is the ceiling of a hand-made reference, not of the model: the resulting transcript has every
turn in the right place and errs only on a few one-word replies at a change of speaker.
ResNet34-LM is also what pyannote 3.1 itself pairs with this segmentation model. The lesson is
recorded in `diarizer.py` beside the constant: test a voice model on a hard, real recording —
similar voices, broadcast audio — before trusting it.

**Counting speakers is the weak part, so the defaults lean on recoverable errors.** With the
count on auto the engine clusters by a similarity threshold, and no threshold suits every
recording: across five test files the value that gave the right count ranged from under 0.45 to
0.7. At 0.5 the interview above comes out as 181 s, 40 s and **1.3 s** — two people and a scrap.
So `attribution.absorb_minor_speakers` folds any voice with under 3 s of speech, or under 1.5% of
it, into the nearest real speaker (auto only — a count the user typed is honoured). Beyond that
the threshold errs toward one speaker too many, because an over-split is fixed in the naming
window by giving two voices the same name, and two people fused into one cannot be fixed at all.

Models come from `huggingface_hub.hf_hub_download` — already a dependency via faster-whisper —
into the same `models` folder, at a **pinned revision**, so a given release of the app always
fetches the same bytes. It is resumable, atomic and works offline once cached, exactly like the
Whisper weights.

### Why a subprocess

The "Cancellation" section above judges a hard kill not worth a subprocess. For this engine the
judgement reverses, for five independent reasons found by reading sherpa-onnx 1.13.8's source:

1. **It holds the GIL.** `OfflineSpeakerDiarization.process` is bound without
   `gil_scoped_release`. On a worker thread the Tk main thread cannot run for minutes and Windows
   marks the window "Not Responding".
2. **It cannot be cancelled.** The progress callback's documented "return non-zero to abort" is
   ignored by the implementation.
3. **It can end the process.** An internal error path calls `_Exit()`. In-process, that closes
   the app with no message and no traceback.
4. **It ships its own `onnxruntime.dll`**, the same base name as the one faster-whisper loads for
   Silero VAD. Windows resolves loaded modules by base name, so whichever loads first serves both;
   that works only while the two versions happen to be compatible.
5. **It copies the audio**, doubling peak memory — memory the transcription then needs back.

A child process answers all five: Cancel is `proc.kill()` and lands within a poll; a native exit
or an out-of-memory becomes `DiarizationError` for that one file; no DLL is shared; and the
child's memory is returned to the OS before Whisper decodes. `diarize_worker.py` is the only
module that imports `sherpa_onnx`, and `test_the_app_never_loads_the_engine_in_process` keeps it
that way.

Details that matter:

- **The child writes to a spool *file*, which the worker thread polls** every 0.2 s. A pipe would
  need a reader thread (or risk a full-pipe deadlock); a file needs neither, so "exactly one
  background thread" still holds. The spool doubles as the error report when the child dies.
- The child is launched with the `python.exe` beside `pythonw.exe` (a `pythonw` child has no
  usable stdout), with `CREATE_NO_WINDOW` so no console flashes, and with `PYTHONPATH` set
  explicitly, since neither `run.bat`'s nor pytest's path setup is inherited.
- A venv's `python.exe` is a launcher stub; the real interpreter is *its* child. Killing the stub
  does take the interpreter with it; that was checked rather than assumed (no worker process
  survives a cancel).
- `_on_close` calls `diarizer.kill_active()`. The worker is a daemon thread and dies with the
  window; a child process would not, and would burn CPU for minutes unobserved.
- `window_shift_for()` coarsens the analysis step on recordings over an hour. The clustering
  cost grows with the square of the length; the values are measured, and recorded in its
  docstring. `tools/diarization_spike.py` reproduces them.

### Failure policy

If the engine cannot be *prepared* — no network for the first download, a broken install — the
queue stops before the first file, with a message naming the checkbox to untick. The alternative
is an overnight queue that quietly produces unlabelled transcripts. A `--selftest` child run is
what proves the install, including that `sherpa-onnx-core`'s own `onnxruntime.dll` is present
(without it Windows binds the engine to an old copy in System32).

If the pass fails for *one file*, that file is transcribed without labels, the log says why, and
the queue continues. Losing labels is never allowed to cost a transcript.

### Who said each word

The engine reports *turns* ("cluster 4, 9.3–14.6 s"); Whisper reports segments. `attribution.py`
lines them up one segment at a time, which keeps the transcript streaming to disk:

- Whisper is asked for `word_timestamps` — only when there are turns to use them, since it costs
  time. Each word goes to the speaker with the most overlap; a tie goes to whoever was already
  talking; a word in a gap takes the nearest turn, or inherits the previous speaker if that turn
  is over a second away. A segment is split wherever the speaker changes between words.
- Two small repairs, both *within* a segment: a lone short first/last word that disagrees with
  the rest is snapped to it (timestamp jitter at a turn boundary), and a tiny `A b A` flip is
  folded back into `A`. Nothing looks across segments, because Whisper gives a genuine
  interjection ("Right.") a segment of its own, and those must survive.
- Overlapped speech produces overlapping turns, so the lookup cannot be "the last turn that
  started before t". `Timeline` keeps a running maximum of turn ends to know when a backwards scan
  may stop.
- Raw cluster ids are arbitrary and non-contiguous. `SpeakerNamer` numbers speakers by first
  appearance, so "Speaker 1" is whoever opens the recording.

`ParagraphBuilder.add_run` closes a block on a change of speaker as well as on the interval, so
no paragraph mixes two voices. The header is written before anyone has spoken, so it lists one
label per cluster and `speakers.sync_header` trues it up at the end if a cluster (a door slam)
never had a word attributed to it.

### Progress in two phases

The speaker pass emits `Progress(phase="speakers")`. The file bar runs 0–100% for it, then again
for transcription; folding both into one bar would need a guess at their relative cost, which
varies several-fold with the model chosen. The queue bar counts only transcribed audio and holds
still during the pass. The per-file ETA is timed from when transcription began (`rate_started`),
so a five-minute speaker pass does not inflate it; the displayed *elapsed* still counts from the
start of the file. The engine's first phase reports nothing, so the poll loop ticks the callback
regardless and the elapsed clock keeps moving.

### Names live in the transcript

There is no sidecar file. `speakers.py` reads the current names, sample quotes and playable
timestamps back out of the `.txt`'s own label lines, so a transcript can be named, renamed,
moved between machines and renamed again.

- A label line is `[HH:MM:SS] Name:` **preceded by a blank line**. Wrapped body text never follows
  a blank line, so it cannot be mistaken for one, whatever it says. The label line itself is
  never wrapped.
- Renaming is an exact-string lookup of the captured label against a `{current: new}` mapping —
  no regex is ever built from a name, and because lookups are against the *original* labels, two
  names can be swapped in one pass. The same name twice merges two speakers.
- The header's `Speakers:` line is regenerated, never parsed, so a comma in a name is harmless.
- Line endings and a BOM are preserved per line (the file may have been through Notepad), and the
  rewrite is atomic: temp file, `fsync`, `os.replace`.

### The naming window

`speaker_dialog.py` is the app's first `CTkToplevel`. Notes for whoever writes the second:

- `_apply_icon` must be called **in the constructor**. `CTkToplevel` swaps in its own icon 200 ms
  after creation unless `iconbitmap` has been called by then.
- `grab_set` is deferred and retried: `CTkToplevel` withdraws and re-shows itself while colouring
  its title bar, and a grab on a window that is not viewable raises.
- `geometry()` scales the size but not the position, so size goes in unscaled units and the
  offset in device pixels. It is clamped to `widgets.work_area`, like the main window.
- **Play sample** is the one other thread in the app: short-lived, started per click, it runs
  only `media.decode_clip` (a PyAV seek, not a decode from the top) and hands a WAV path back
  through a queue the dialog polls. It touches no widget. `winsound` cannot play from memory
  asynchronously, hence the temp file; every Play and Stop bumps a request counter so a decode
  that finishes late is recognisably stale.
- The options row that turns the feature on made the main window one row taller. The queue list
  is one row shorter to pay for it, so that at the 130% Text size on a 1504-px-tall display at
  150% scaling the log pane keeps the height it had (measured: 93 px, was 98).

## Failure handling

`media.probe()` runs on file selection, so a corrupt file or a video with no audio track fails in
the dialog rather than 30 seconds into a job. In a queue it marks that row *Unreadable* and the file
is dropped from the run — one dialog per bad file would be unusable on a folder add.

`transcriber._friendly_error()` translates the failures users actually hit — no network on first
download, a transcript open in another program, a full disk — into plain sentences. Everything else
falls back to `TypeName: message`. The GUI shows the text in the log pane and a dialog; a raw
traceback never reaches the user.

`settings.load()` treats a missing, corrupt, or hand-edited file as "use defaults", and clamps
out-of-range values. `settings.save()` swallows OS errors: failing to persist a preference must
never take the app down.

## Appearance: following the system theme

`_apply_appearance_mode()` runs before the first window and sets CustomTkinter's mode from the
saved `appearance` preference. On the default `"system"`, CustomTkinter polls `darkdetect` — which
reads the `AppsUseLightTheme` registry value — on the Tk event loop and pushes a Light/Dark switch
through every widget, so a mid-session change to the Windows setting is picked up without a
restart. `ctk.CTk` also calls `DwmSetWindowAttribute` with `DWMWA_USE_IMMERSIVE_DARK_MODE`, which
is what carries the **title bar** across; a light title bar over a dark window is the tell that an
app has been dark-mode-retrofitted badly.

The Options panel's *System / Light / Dark* control writes `settings.appearance` and is saved on
the same two paths as every other preference.

### The queue is the exception

CustomTkinter has no table widget, and the file queue needs four columns, multi-selection, stable
per-row identity and a status colour per row. Rebuilding that on a `CTkScrollableFrame` means
hand-writing selection and ordering, and `MAX_QUEUE` is 500 files — 2000 canvas-backed widgets. So
the queue stays a `ttk.Treeview`, and `widgets.style_queue_tree()` paints it from
`ctk.ThemeManager.theme` instead: colours are read from the loaded CustomTkinter theme rather than
hardcoded a second time, so the two cannot drift.

Three details are load-bearing:

- **The `clam` ttk theme is required, not preferred.** It is the only stock theme whose Treeview
  honours `background`/`fieldbackground`. Under the Windows native theme the rows stay white
  whatever the style says. `clam` also draws its border from `bordercolor`/`lightcolor`/`darkcolor`
  rather than `borderwidth`, so all three are set to the row background or the tree keeps a light
  3D frame in dark mode.
- **Row tags carry their own foreground**, so `tag_configure` has to be re-run on every switch or
  finished rows keep their light-mode green on a dark background. The five status colours gained
  dark variants for the same reason: `#1a7f37` on `#343638` is a near-black smudge.
- **The repaint hook is CustomTkinter-internal.** Every CustomTkinter widget registers itself with
  `AppearanceModeTracker`; a ttk widget has to be registered by hand.
  `TranscriptorApp._watch_appearance_mode` does that inside a `try`, and the unconditional
  `style_queue_tree()` call happens first — so if that tracker ever moves, the tree still renders
  correctly and merely stops following a mid-session change. The callback is removed in `_on_close`,
  because the tracker holds it in a module-level list and would otherwise fire against a destroyed
  widget.

### DPI, scaling, and the Text size control

`_enable_dpi_awareness()` asks for **level 2, per-monitor**, and the level matters. Both level 1
and level 2 tell Windows "do not magnify this window, the app scales itself" — but only level 2
matches what CustomTkinter then does, which is read the monitor's DPI and scale every widget by it.
Under level 1 CustomTkinter reads back 96 DPI, scales by 1.0, and the whole window renders a third
smaller than intended on a 150% display: physically small and hard to read. This was shipped wrong
once; the symptom is a window occupying 41% of the screen width where it should occupy 61%.

Because CustomTkinter is the scaling authority, **`widgets.py` has to scale by hand** — row height,
column widths and padding are all multiplied by `ScalingTracker.get_widget_scaling()`, or the ttk
queue renders at two thirds the size of the window around it. The font is the subtle one:
`theme_font()` reproduces CustomTkinter's own `_apply_font_scaling` arithmetic,
`-abs(round(size * scale))`. The negation is load-bearing — Tk reads a *positive* size as points
and multiplies it by `tk scaling` (about 2.0 on a 150% display) on top of everything else, so a
positive 13 renders the queue at nearly twice the height of the label beside it.

**Text size** (`settings.ui_scale`, one of `UI_SCALES`) multiplies on top of that via
`set_widget_scaling`/`set_window_scaling`, so 100% is already the correct size for the monitor and
the larger values are deliberate extra magnification for legibility. CustomTkinter's widgets redraw
themselves on the change; the ttk queue is re-measured and repainted by hand in `_on_scale_change`.

`_fit_to_screen()` exists because of that control. The window's natural height grows with the
setting, and at the largest one on a small or heavily scaled display it would open taller than the
desktop with the buttons behind the taskbar. It compares the requested size against the Windows
work area (`SPI_GETWORKAREA`, which excludes the taskbar) and shrinks the window if needed; the log
pane carries the layout's vertical weight, so it is what gives. One wrinkle: `CTk.geometry()`
multiplies its argument by the window scaling on the way through, so the string is built in
unscaled units rather than device pixels.

The largest setting on a small panel leaves the log only a few lines tall. That is the trade the
setting exists to offer, and the clamp is what keeps it merely cramped rather than unusable.

## Windows integration: icon and taskbar

Three separate pieces have to line up before the app gets a proper taskbar button.

**The icon file.** `tools/make_icon.ps1` draws the artwork with GDI+ and packs nine sizes
(16–256 px) into `assets/transcriptor-prime.ico`. Each size is drawn at its own resolution rather
than downscaled from one master, and below 32 px the design simplifies — five fat bars instead of
seven, no text lines — because the detailed version turns to mush at 16 px.

Entries up to 64 px are uncompressed BMP/DIB; 128 and 256 are PNG-compressed. That is the
conventional Windows split, and it is what keeps the file at ~57 KB instead of ~370 KB. Tk on
Windows reads PNG entries perfectly well (verified, not assumed), so the split is purely about
size and maximal shell compatibility, not a Tk limitation.

**The window icon.** `_apply_icon()` calls `iconbitmap(default=…)`, which sets the icon for the
window and for any dialog opened later. Off Windows, Tk cannot read `.ico`, so it falls back to
`iconphoto` with the PNG — keeping a reference on the widget, because Tk does not, and a
garbage-collected `PhotoImage` silently reverts the icon.

**The taskbar identity.** Without an explicit AppUserModelID, Windows identifies the process by
its executable — `pythonw.exe` — so the window inherits the generic Python icon and groups with
every other Python GUI. `_set_app_user_model_id()` fixes that, and must run *before* the first
window exists, since Windows reads the identity when it creates the taskbar button.

That identity also has to appear on the shortcut, or a pinned icon and the running window become
two separate buttons. `WScript.Shell` cannot write shell property-store values, so
`tools/install_shortcuts.ps1` drops to COM (`IShellLink` / `IPersistFile` / `IPropertyStore`) to
stamp `System.AppUserModel.ID` onto the `.lnk`. It reads the value from the Python package rather
than hardcoding it, so the shortcut can never drift from what the process sets at runtime.

`APP_USER_MODEL_ID` deliberately carries no version number: Windows keys pinned buttons off that
string, and embedding a version would orphan the user's pinned icon on every upgrade. A test
enforces this.

## Testing

`pytest` runs the whole suite in a couple of seconds.

- `test_formatting.py` — timecodes and grouping, pure functions, no I/O.
- `test_media.py` — probes real MP3 and MP4 files that `conftest.py` synthesizes with PyAV, plus the
  folder scan and the output-name reservation.
- `test_settings.py` — round-trip, corruption, clamping, unknown keys from a future version.
- `test_attribution.py` — word-to-speaker rules on hand-written numbers: overlap, ties, gaps,
  overlapping turns, edge snapping, flip smoothing, and the interjection that must survive.
- `test_speakers.py` — parsing and renaming on plain text files: swap, merge, re-rename, names
  full of punctuation, body text shaped like a label, CRLF/BOM preservation, and that a failed
  `os.replace` leaves the original intact and no temp file behind.
- `test_diarizer.py` — the process boundary, with `python -c` one-liners standing in for the
  engine: protocol parsing, progress, a cancel that kills within a poll, and every way a child
  can die (reported error, silent `os._exit`, garbage output). One `slow` test runs the real
  engine.
- `test_speaker_dialog.py` — the option, the button's three-way choice of target, auto-open for
  a single file only, and the naming window itself with `winsound` faked. The dialog is always
  built non-modal there: a real grab would seize the keyboard of whoever runs the tests.
- `test_transcriber.py` — a `FakeModel` stands in for `WhisperModel`, so incremental writing,
  atomic rename, cancellation, progress monotonicity and error mapping are all verified without
  downloading weights. `TestBatch` pins the batch contract: the model is constructed exactly once
  for a three-job queue, a failing file leaves the others untouched, a cancel skips the remainder,
  and neither a bad file nor a fatal load emits `Done`/`Failed`. One test at the bottom, marked
  `slow`, does a genuine run with `tiny`.
- `test_app.py` — builds the real window and drives the handlers directly; skips where no display
  exists. The Tk interpreter is session-scoped and is a `ctk.CTk`, because CustomTkinter's scaling
  and appearance trackers walk up `.master` looking for the root and register their polling loop
  against it; each test then gets its own `CTkToplevel`. A fresh root per test failed
  intermittently, and the fixture's display check would have reported any such failure as a skip
  rather than a failure. `TestQueue` covers add/remove/clear, deduplication and the running-guard;
  `TestQueueOutputPaths` covers the single-file back-compat surface that is most likely to rot;
  `TestSpinbox` and `TestAppearance` cover the two pieces in `widgets.py`.
- `test_branding.py` — parses the committed `.ico` and asserts the size set, the BMP/PNG split,
  the file size ceiling, and that the taskbar identity round-trips through the Windows shell API.

Four toolkit behaviours were measured rather than assumed, and all four shaped the code:

- **`ttk.Treeview` has no `-state` option.** `tree.configure(state="disabled")` raises `TclError`,
  so it cannot go in `_set_running`'s widget loop; `tree.state(["disabled"])` is used instead.
- **A "disabled" Treeview still accepts clicks.** `state(["disabled"])` only greys the rows. The
  `self.running` flag, checked at the top of every queue handler, is what actually protects the
  queue during a run — there is a test that pins exactly this.
- **`.config()` does not reach a CustomTkinter widget's `configure()`.** `tkinter` binds
  `Misc.config = Misc.configure` at class-definition time, so the alias resolves to the base
  implementation and silently skips every override. `_set_running` calls `.configure()` throughout.
- **`CTkEntry` traces its own textvariable and calls `get()` on it**, which throws the moment the
  box is emptied — an unhandled traceback on every keystroke if the variable is an `IntVar`. So
  `CTkSpinbox` drives its entry with a private `StringVar` and mirrors that into the caller's
  `IntVar`. The `IntVar` still ends up holding the raw text, which is what keeps
  `_capture_settings`' existing `TclError` fallback meaningful.

The stub-plus-one-real-run split is deliberate: the fast tests can run on every change, and the
`slow` marker keeps the genuine end-to-end check one command away.
