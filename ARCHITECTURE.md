# Architecture

## Layout

```
src/transcriptor_prime/
  __init__.py    version, app name, taskbar identity, asset paths
  __main__.py    entry point; turns a startup crash into a dialog
  app.py         Tk window, file queue, event pump       (the only Tk code)
  transcriber.py worker thread wrapping faster-whisper   (no Tk imports)
  formatting.py  timecodes and paragraph grouping        (pure functions)
  media.py       PyAV probe, folder scan, output naming
  settings.py    JSON preferences + app data locations
  assets/        transcriptor-prime.ico, logo.png
tools/
  make_icon.ps1        redraws the icon; run only when the artwork changes
  install_shortcuts.ps1 Start Menu + Desktop shortcuts for taskbar pinning
```

The dependency direction is one-way: `app` → `transcriber` → `formatting`/`settings`. Nothing below
`app` imports Tk, and `formatting` imports nothing from the project at all, which is what makes the
transcript rules testable in milliseconds without a model or a display.

## Why this stack

**faster-whisper (CTranslate2), not openai-whisper.** Roughly 4–5× faster on CPU at the same model
size, runs int8-quantized, and pulls in no PyTorch. Critically, `transcribe()` returns a *lazy
generator* of segments rather than a finished result — so the app can show genuine progress and
write output incrementally instead of blocking for half an hour on an opaque call.

**PyAV for decoding.** Its wheels bundle FFmpeg's libraries, so MP3, MP4, MKV and the rest decode
with no system ffmpeg install — a meaningful simplification for a double-click desktop app. The
same library probes duration and stream layout up front.

**tkinter.** Ships with Python, so `run.bat` installs nothing for the UI and there is no packaging
story to maintain. The app is one window; a heavier toolkit would buy nothing.

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
second one, but see "Probing on the event loop" below — it is not, deliberately.

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
- `test_transcriber.py` — a `FakeModel` stands in for `WhisperModel`, so incremental writing,
  atomic rename, cancellation, progress monotonicity and error mapping are all verified without
  downloading weights. `TestBatch` pins the batch contract: the model is constructed exactly once
  for a three-job queue, a failing file leaves the others untouched, a cancel skips the remainder,
  and neither a bad file nor a fatal load emits `Done`/`Failed`. One test at the bottom, marked
  `slow`, does a genuine run with `tiny`.
- `test_app.py` — builds the real Tk window and drives the handlers directly; skips where no
  display exists. The Tk interpreter is session-scoped, with each test on its own `Toplevel`: a
  fresh `tk.Tk()` per test failed intermittently, and the fixture's display check would have
  reported any such failure as a skip rather than a failure. `TestQueue` covers add/remove/clear,
  deduplication and the running-guard; `TestQueueOutputPaths` covers the single-file back-compat
  surface that is most likely to rot.

Two Tk behaviours were measured rather than assumed, and both shaped the code:

- **`ttk.Treeview` has no `-state` option.** `tree.config(state="disabled")` raises `TclError`, so
  it cannot go in `_set_running`'s widget loop; `tree.state(["disabled"])` is used instead.
- **A "disabled" Treeview still accepts clicks.** `state(["disabled"])` only greys the rows. The
  `self.running` flag, checked at the top of every queue handler, is what actually protects the
  queue during a run — there is a test that pins exactly this.
- `test_branding.py` — parses the committed `.ico` and asserts the size set, the BMP/PNG split,
  the file size ceiling, and that the taskbar identity round-trips through the Windows shell API.

The stub-plus-one-real-run split is deliberate: the fast tests can run on every change, and the
`slow` marker keeps the genuine end-to-end check one command away.
