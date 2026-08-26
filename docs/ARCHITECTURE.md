# Architecture

## Files

| File | Role |
|---|---|
| `Transcribe.pyw` | The entire application: GUI, queue, worker, subprocess management |
| `Transcribe.bat` | Double-click launcher; runs the `.pyw` under `pythonw.exe` |
| `Setup.ps1` | One-time environment provisioning |

Three files, no package, no build step. Deliberate: the target user copies a
folder and double-clicks.

## Pipeline

```
 source file (mp3/mp4/m4a/...)
      │
      │  ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le
      ▼
 <stem>.wav  (16 kHz mono, in the output folder, deleted afterwards)
      │
      ├──────────────► pyannote speaker-diarization-community-1
      │                     └─> speaker turns
      │
      └──────────────► Silero VAD ─> CTranslate2 int8 Whisper
                            └─> segments with timestamps
                                      │
                    turns ∩ segments (largest overlap wins)
                                      ▼
                     txt · srt · vtt · tsv · json
```

Diarization runs **before** transcription, so the progress bar stays at zero for
the first phase. The status line says "identifying speakers" during it.

## Why the audio is normalised

- Removes MP3/AAC decoding as a variable across container formats.
- `-vn` strips embedded cover art. Some recorders attach an MJPEG stream, which
  trips certain decoders.
- Whisper resamples to 16 kHz mono internally anyway; doing it once up front
  avoids repeating it.
- Naming the WAV after the source stem makes the outputs `<stem>.srt` rather
  than `<stem>_16k.srt`.

The WAV is deleted in a `finally` block, so it is removed on success, failure
and cancellation alike.

## The torch shim

Generated fresh into a temp directory on every run, so it can never drift out of
sync with the app:

1. `import torch`
2. `add_safe_globals([Specifications, Problem, Resolution])`
3. Replace `torch.load` with a wrapper forcing `weights_only=False`
4. Optionally patch `Diarization.__init__` for speaker auto-detect
5. `from whisper_ctranslate2.whisper_ctranslate2 import main; sys.exit(main())`

Step 3 is the load-bearing one; step 2 alone only surfaces the next blocked
class. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#_picklerunpicklingerror-weights-only-load-failed).

Step 4 exists because the CLI has no "auto" option — it always passes an int and
defaults to 2 — while pyannote auto-detects when `num_speakers=None`. The shim
intercepts the constructor rather than the CLI, controlled by the
`WCT2_AUTO_SPEAKERS` environment variable.

## Threading model

- **Main thread** — tkinter only. All widget mutation happens here.
- **One worker thread** — pulls the next `Queued` job and processes it to
  completion, then loops. Never touches widgets.
- **`queue.Queue`** — the worker posts `("log"|"row"|"prog"|"ask"|…)` messages;
  `drain()` runs on the main thread every 120 ms and applies them.
- **Child process** — `python.exe shim.py …` with stdout and stderr merged into a
  pipe, read line by line. `python.exe` is chosen over `pythonw.exe` because
  piped stdio is better behaved.

Strictly one job at a time. CTranslate2 already saturates every physical core;
concurrency would slow the batch down.

When the worker needs a decision from the user (a transcript appeared after the
job was queued) it posts an `ask` message with a `threading.Event`, then blocks
on it with a 10-minute timeout. The dialog runs on the main thread and sets the
result. This keeps the "dialogs only on the GUI thread" rule intact without
deadlocking the worker.

## Progress estimation

Whisper emits `[00:04:12.340 --> 00:04:18.900]` per segment with
`--verbose True`. The worker regexes the start timestamp, divides by the
duration from `ffprobe`, and posts a fraction. ETA is linear extrapolation from
elapsed time. Crude, but far better than an indeterminate spinner on an
hour-long job.

## Job settings snapshot

Each job captures language, model, speaker count, diarization flag and thread
count **at the moment it is added**. Changing the dropdowns mid-batch affects
only subsequent additions. Without this, editing settings while a queue runs
would silently rewrite pending work.

## Duplicate and existing-output handling

Two independent checks:

- **Queue duplicates** — keyed on `os.path.normcase(os.path.abspath(path))`, so
  case and relative-path variants collapse. Active duplicates are refused;
  finished rows are replaced on re-add.
- **Existing transcripts** — a `<stem>_transcript` folder counts as processed
  only if it contains a **non-empty** `.txt/.srt/.vtt/.tsv/.json`. An empty
  folder left by a cancelled run must not cause a false skip.

Checked twice: at enqueue time, and again immediately before the job runs, since
output can appear in between.

## Engine detection and caching

Detection runs `DETECT_SCRIPT` in a **child process**, so a broken `torch` or
`ctranslate2` install produces a readable error instead of taking the GUI down.
It queries `ctranslate2.get_cuda_device_count()` and
`ctranslate2.get_supported_compute_types(device, index)`, then asks torch for
friendly GPU names.

Because that import costs seconds, the result is cached in
`.transcribe_gui.json` alongside an `env_fingerprint()`: machine name, machine
architecture, Python version, CPU count, and the versions of `ctranslate2`,
`torch`, `faster-whisper` and `whisper-ctranslate2`. The fingerprint is built
with `importlib.metadata.version()`, which reads package metadata **without
importing** the packages, so it costs milliseconds. A mismatch triggers silent
re-detection; a match skips it entirely.

Compute type is derived, not stored per device: `COMPUTE_PREF` gives a
preference order per backend and the first supported entry wins.

Engine is session-wide by design. It is read from `self.engine_spec` when a job
launches rather than snapshotted into the `Job` like language and model, and the
combobox is disabled while the worker runs. Mixing devices inside one batch would
produce inconsistent results and meaningless timings.

**The device list is short because CTranslate2 is:** it implements CPU and CUDA
backends only. There is no Metal, DirectML, Vulkan or XDNA/NPU support, and
`whisper-ctranslate2` exposes exactly `{auto, cpu, cuda}`. ROCm builds identify
as `cuda`. A single CPU entry on a machine without an NVIDIA GPU is the correct
result, not a detection bug.

## Drag and drop

tkinter has no native Explorer drop support, so this uses **`tkinterdnd2`**, a
thin wrapper over the tkdnd Tcl extension. It is an *optional* dependency:

- `make_root()` returns `TkinterDnD.Tk()` when the import succeeds, otherwise a
  plain `tk.Tk()`, and reports which one via `root._dnd_ok`
- `setup_drop()` calls `drop_target_register(DND_FILES)` and binds `<<Drop>>`
- `_on_drop_event()` parses `event.data` with `tk.splitlist()`. tkdnd hands over
  a **Tcl list**, so paths containing spaces arrive brace-quoted; splitting on
  whitespace mangles them

Absent the package the app logs that drag and drop is off and works normally.

### Why not do it without a dependency

The first implementation registered the toplevel with `DragAcceptFiles` and
subclassed its window procedure through `ctypes` to intercept `WM_DROPFILES`.
No dependency, and it **crashed the process on every drop**.

The probable cause: `restype` was set on `SetWindowLongPtrW` and
`CallWindowProcW` but `argtypes` was not, so ctypes defaulted those parameters
to 32-bit ints and truncated the 64-bit procedure pointer. Windows then called a
bad address.

It was removed rather than fixed. The failure mode is a hard process crash with
no traceback, it cannot be covered by tests in CI, and the benefit is one
convenience feature. An optional, well-tested dependency is the better trade.

## Editing queued items

`editing_iid` holds the row under edit. `begin_edit()` copies the job's fields
into the controls, locks the File entry and Browse button, relabels the action
button, and tints the row. `apply_edit()` writes the values back and refreshes
the grid; `cancel_edit()` restores the controls.

The edit is abandoned automatically when the queue starts or the row is removed,
because a job in flight must not have its settings mutated underneath it. Only
`Queued` rows are editable; double-clicking a running row says so, and a
finished row opens its output folder.

## Logging

- `logs/session_<timestamp>.log` — one per app launch
- `logs/<source-stem>_<timestamp>.log` — one per job

Every line goes to the Console pane, both open log files, and stdout (a no-op
under `pythonw`). Line-buffered, so a log survives a hard kill.
