#!/usr/bin/env pythonw
"""
Transcribe -- tkinter GUI around whisper-ctranslate2 with speaker diarization
and a batch queue. Double-click Transcribe.bat to run.

No third-party GUI toolkit: tkinter ships with Python on Windows.

Design notes
  * Everything goes through one queue. "Transcribe" enqueues the chosen file
    and starts the worker; "Add to queue" enqueues without disturbing a run.
    Files can be added while processing.
  * One worker thread, strictly sequential. Whisper already saturates every
    core, so running two jobs at once would be slower overall, not faster.
  * Each job snapshots the language/model/speaker settings at the moment it
    was added, so changing the dropdowns mid-run cannot corrupt queued work.
  * Already-processed files are skipped when <stem>_transcript exists and
    holds real output. Every skip is logged.

Elevation is never required once models are cached. Only downloading a model
absent from %USERPROFILE%\\.cache\\huggingface needs the symlink privilege.
"""

import json
import os
import re
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP = "Transcribe"
HERE = Path(__file__).resolve().parent
SETTINGS = HERE / ".transcribe_gui.json"
LOGDIR = HERE / "logs"

LANGUAGES = [
    ("Russian", "ru"),
    ("English", "en"),
    ("Auto-detect", ""),
    ("Czech", "cs"), ("Dutch", "nl"), ("French", "fr"), ("German", "de"),
    ("Hebrew", "he"), ("Italian", "it"), ("Polish", "pl"),
    ("Portuguese", "pt"), ("Romanian", "ro"), ("Spanish", "es"),
    ("Turkish", "tr"), ("Ukrainian", "uk"),
]

MODELS = [
    ("large-v3-turbo  (recommended)", "large-v3-turbo"),
    ("large-v3  (slowest, best)", "large-v3"),
    ("medium", "medium"),
    ("small", "small"),
    ("tiny  (test only)", "tiny"),
]

MEDIA_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
             ".mp4", ".mkv", ".mov", ".avi", ".webm"}

MEDIA_FILTER = [
    ("Audio / video", " ".join("*" + e for e in sorted(MEDIA_EXT))),
    ("All files", "*.*"),
]

OUTPUT_EXT = {".txt", ".srt", ".vtt", ".tsv", ".json"}

SHIM = r'''
import os, sys, warnings, traceback
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
warnings.filterwarnings("ignore")
import torch
try:
    from pyannote.audio.core.task import Specifications, Problem, Resolution
    torch.serialization.add_safe_globals([Specifications, Problem, Resolution])
except Exception:
    pass
_real = torch.load
def _load(*a, **kw):
    kw["weights_only"] = False
    return _real(*a, **kw)
torch.load = _load

# whisper-ctranslate2 always passes an int to pyannote (CLI default 2) and has
# no "auto" option, but pyannote auto-detects when num_speakers is None.
if os.environ.get("WCT2_AUTO_SPEAKERS") == "1":
    try:
        from whisper_ctranslate2 import diarization as _dia
        _orig_init = _dia.Diarization.__init__
        def _auto_init(self, token=None, device="cpu", num_speakers=2):
            _orig_init(self, token=token, device=device, num_speakers=None)
        _dia.Diarization.__init__ = _auto_init
        print("[shim] speaker auto-detect enabled (num_speakers=None)", flush=True)
    except Exception as e:
        print("[shim] could not enable auto-detect: %r" % (e,), flush=True)

try:
    from whisper_ctranslate2.whisper_ctranslate2 import main
except Exception:
    traceback.print_exc(file=sys.stdout)
    sys.exit(91)
sys.exit(main())
'''

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

ST_QUEUED, ST_RUN, ST_DONE = "Queued", "Running", "Done"
ST_SKIP, ST_FAIL, ST_CANCEL = "Skipped", "Failed", "Cancelled"

DUP_ASK = "Ask me"
DUP_SKIP = "Skip it"
DUP_REDO = "Reprocess it"


# --------------------------------------------------------------------------- #
# Engine (device) detection
#
# CTranslate2 -- the runtime under faster-whisper -- supports CPU and CUDA only.
# There is no Metal, DirectML, XDNA/NPU or Vulkan backend, and whisper-ctranslate2
# exposes exactly {auto, cpu, cuda}. AMD ROCm builds of CTranslate2 also present
# themselves as "cuda". So the list below is the complete set of possibilities,
# not an arbitrary subset.
#
# Detection imports ctranslate2 and torch, which takes seconds, so the result is
# cached in the settings file behind a cheap fingerprint.
# --------------------------------------------------------------------------- #

DETECT_SCRIPT = r'''
import json, sys
out = {"devices": [], "errors": []}

try:
    import ctranslate2 as ct2
    out["ctranslate2"] = getattr(ct2, "__version__", "?")
    try:
        cpu_types = sorted(ct2.get_supported_compute_types("cpu"))
    except Exception as e:
        cpu_types = ["int8", "float32"]
        out["errors"].append("cpu compute types: %r" % (e,))
    out["devices"].append({"device": "cpu", "index": 0, "name": "CPU",
                           "compute_types": cpu_types})
    try:
        n = ct2.get_cuda_device_count()
    except Exception as e:
        n = 0
        out["errors"].append("cuda count: %r" % (e,))
    for i in range(n):
        try:
            types = sorted(ct2.get_supported_compute_types("cuda", i))
        except Exception:
            try:
                types = sorted(ct2.get_supported_compute_types("cuda"))
            except Exception:
                types = ["float16", "float32"]
        out["devices"].append({"device": "cuda", "index": i,
                               "name": "CUDA device %d" % i,
                               "compute_types": types})
except Exception as e:
    out["errors"].append("ctranslate2 unavailable: %r" % (e,))

# Friendly GPU names, and a second opinion on CUDA availability.
try:
    import torch
    out["torch"] = torch.__version__
    if torch.cuda.is_available():
        for d in out["devices"]:
            if d["device"] == "cuda":
                try:
                    d["name"] = torch.cuda.get_device_name(d["index"])
                except Exception:
                    pass
    else:
        out["errors"].append("torch reports no CUDA")
except Exception as e:
    out["errors"].append("torch unavailable: %r" % (e,))

sys.stdout.write(json.dumps(out))
'''

# Preference order per device, best first.
COMPUTE_PREF = {
    "cpu":  ["int8", "int8_float32", "int16", "float32", "bfloat16", "float16"],
    "cuda": ["float16", "int8_float16", "bfloat16", "int8_bfloat16",
             "float32", "int8", "int8_float32"],
}


def pick_compute_type(device: str, available) -> str:
    for c in COMPUTE_PREF.get(device, []):
        if c in available:
            return c
    return sorted(available)[0] if available else "default"


def env_fingerprint() -> str:
    """Cheap signature of the environment. Uses importlib.metadata rather than
    importing torch/ctranslate2, so it costs milliseconds."""
    import platform
    try:
        from importlib.metadata import version as _v
    except Exception:
        def _v(_):
            return "?"
    parts = [platform.node(), platform.machine(), sys.version.split()[0],
             str(os.cpu_count())]
    for pkg in ("ctranslate2", "torch", "faster-whisper", "whisper-ctranslate2"):
        try:
            parts.append(f"{pkg}={_v(pkg)}")
        except Exception:
            parts.append(f"{pkg}=none")
    return "|".join(parts)


def detect_engines(timeout=180):
    """Run detection in a child process so a broken torch/ct2 install cannot
    take the GUI down with it. Returns the parsed dict."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            exe = cand
    tmp = Path(tempfile.mkdtemp(prefix="engdetect_"))
    try:
        script = tmp / "detect.py"
        script.write_text(DETECT_SCRIPT, encoding="ascii")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        cp = subprocess.run([str(exe), str(script)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, env=env, creationflags=NO_WINDOW,
                            stdin=subprocess.DEVNULL)
        if cp.returncode != 0 or not cp.stdout.strip():
            return {"devices": [], "errors": [f"detector exit {cp.returncode}",
                                              f"stderr: {(cp.stderr or '')[:400]}",
                                              f"stdout: {(cp.stdout or '')[:200]}"]}
        # The child may emit warnings before the JSON; take the last line.
        last = [l for l in cp.stdout.strip().splitlines() if l.strip()][-1]
        return json.loads(last)
    except subprocess.TimeoutExpired:
        return {"devices": [], "errors": [f"detector timed out after {timeout}s "
                                          "(torch import hung?)"]}
    except Exception as e:
        return {"devices": [], "errors": [repr(e)]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def engine_options(info):
    """[(label, {device, index, compute_type}), ...] -- only what actually
    exists on this machine."""
    opts = []
    for d in info.get("devices", []):
        dev, idx = d["device"], int(d.get("index", 0))
        types = d.get("compute_types") or []
        ct = pick_compute_type(dev, types)
        # Plain device names. The compute type is an implementation detail --
        # shown beside the dropdown and in the log, not baked into the label.
        if dev == "cpu":
            label = "CPU"
        else:
            label = f"CUDA:{idx}  {d.get('name', 'GPU')}"
        opts.append((label, {"device": dev, "index": idx, "compute_type": ct}))
    if not opts:
        opts.append(("CPU", {"device": "cpu", "index": 0,
                             "compute_type": "int8"}))
    return opts


# --------------------------------------------------------------------------- #
# Drag and drop from Explorer
#
# tkinter has no native drop support. This used to be done by subclassing the
# window procedure with ctypes to intercept WM_DROPFILES -- no dependency, but
# it crashed the process on every drop, and hand-written pointer handling that
# can hard-crash is not worth a convenience feature.
#
# tkinterdnd2 wraps the mature tkdnd Tcl extension and does the job properly.
# It is an OPTIONAL dependency: without it the app runs exactly as before,
# minus drag and drop, and says so at startup.
# --------------------------------------------------------------------------- #

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except Exception:                                   # not installed, or broken
    DND_FILES = None
    TkinterDnD = None
    DND_AVAILABLE = False


def make_root():
    """A DnD-capable Tk root when possible, otherwise a plain one."""
    if DND_AVAILABLE:
        try:
            return TkinterDnD.Tk(), True
        except Exception:
            pass                                    # tkdnd libs missing/broken
    return tk.Tk(), False


def physical_cores() -> int:
    """Physical cores; CTranslate2 loses throughput on hyperthreads."""
    try:
        out = subprocess.run(["wmic", "cpu", "get", "NumberOfCores", "/value"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=NO_WINDOW).stdout
        vals = [int(m) for m in re.findall(r"NumberOfCores=(\d+)", out)]
        if vals:
            return max(1, sum(vals))
    except Exception:
        pass
    return max(1, (os.cpu_count() or 4) // 2)


def which(n):
    return shutil.which(n)


def hhmmss(sec) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return (s or "recording")[:120]


def output_dir_for(src: Path) -> Path:
    return src.parent / f"{src.stem}_transcript"


def already_processed(src: Path):
    """(bool, detail). True when the transcript folder holds real output."""
    d = output_dir_for(src)
    if not d.is_dir():
        return False, ""
    made = [p.name for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in OUTPUT_EXT and p.stat().st_size > 0]
    if made:
        return True, f"{len(made)} file(s) in {d.name}"
    return False, ""


class Job:
    __slots__ = ("iid", "src", "lang", "lang_label", "model", "model_label",
                 "spk", "diarize", "threads", "status", "detail", "force")

    def __init__(self, iid, src, lang, lang_label, model, model_label,
                 spk, diarize, threads):
        self.iid = iid
        self.src = src
        self.lang = lang
        self.lang_label = lang_label
        self.model = model
        self.model_label = model_label
        self.spk = spk
        self.diarize = diarize
        self.threads = threads
        self.status = ST_QUEUED
        self.detail = ""
        # True once the user has explicitly approved overwriting existing
        # output, so the pre-run re-check does not ask a second time.
        self.force = False


# --------------------------------------------------------------------------- #

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()

        self.jobs = {}            # iid -> Job
        self.order = []           # iid order
        self.lock = threading.Lock()
        self.worker = None
        self.proc = None
        self.stop_queue = False
        self.cancel_current = False
        self.current = None
        self.job_started = 0.0
        self.tmpdir = None
        self.tempwav = None

        self.session_log = None
        self.job_log = None

        # Engine is a session-wide choice: a batch runs entirely on one device.
        self.engine_opts = []      # [(label, spec)]
        self.engine_info = {}      # raw detector output, cached
        self.engine_fp = ""        # fingerprint the cache belongs to
        self.detect_done = False   # has the background probe finished?
        self.editing_iid = None    # queue row whose settings are being edited
        # Safe default: CPU always exists, so a job can start before detection
        # completes, and a failed probe changes nothing.
        self.engine_spec = {"device": "cpu", "index": 0, "compute_type": "int8"}

        root.title(APP)
        root.geometry("1040x740")
        root.minsize(900, 640)

        cfg = self.load_settings()
        self.build_ui(root, cfg)
        self.open_session_log()
        self.preflight()
        self.init_engines(cfg)

        self.setup_drop(root)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(120, self.drain)

    # ------------------------------------------------------------------- ui
    def build_ui(self, root, cfg):
        pad = dict(padx=6, pady=4)
        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        # ---- settings block
        top = ttk.LabelFrame(outer, text="Settings (applied to files as they are added)",
                             padding=8)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="File").grid(row=0, column=0, sticky="w", **pad)
        self.file_var = tk.StringVar()
        self.file_entry = ttk.Entry(top, textvariable=self.file_var)
        self.file_entry.grid(row=0, column=1, sticky="ew", **pad)
        self.browse_btn = ttk.Button(top, text="Browse...", command=self.pick_file)
        self.browse_btn.grid(row=0, column=2, **pad)

        row = ttk.Frame(top)
        row.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=(6, 2))

        ttk.Label(row, text="Language").pack(side="left")
        self.lang_var = tk.StringVar(value=cfg.get("lang_label", LANGUAGES[0][0]))
        ttk.Combobox(row, textvariable=self.lang_var, state="readonly", width=20,
                     values=[a for a, _ in LANGUAGES]).pack(side="left", padx=(4, 16))

        ttk.Label(row, text="Model").pack(side="left")
        self.model_var = tk.StringVar(value=cfg.get("model_label", MODELS[0][0]))
        ttk.Combobox(row, textvariable=self.model_var, state="readonly", width=26,
                     values=[a for a, _ in MODELS]).pack(side="left", padx=(4, 16))

        self.diar_var = tk.BooleanVar(value=cfg.get("diarize", True))
        ttk.Checkbutton(row, text="Label speakers", variable=self.diar_var,
                        command=self.sync_enabled).pack(side="left")
        vcmd = (root.register(self._validate_spk), "%P")
        self.spk_var = tk.StringVar(value=str(cfg.get("speakers", "3")))
        self.spk_entry = ttk.Entry(row, width=3, textvariable=self.spk_var,
                                   justify="center", validate="key", validatecommand=vcmd)
        self.spk_entry.pack(side="left", padx=(6, 3))
        ttk.Label(row, text="(blank = auto)", foreground="#777").pack(side="left", padx=(0, 16))

        self.thr_label = ttk.Label(row, text="threads")
        self.thr_label.pack(side="left")
        self.thr_var = tk.IntVar(value=cfg.get("threads", physical_cores()))
        self.thr_spin = ttk.Spinbox(row, from_=1, to=64, width=4,
                                    textvariable=self.thr_var)
        self.thr_spin.pack(side="left", padx=4)
        self.thr_note = ttk.Label(row, text="", foreground="#777")
        self.thr_note.pack(side="left", padx=(4, 0))

        # ---- engine: session-wide, not per job
        rowe = ttk.Frame(top)
        rowe.grid(row=3, column=0, columnspan=3, sticky="ew", padx=6, pady=(6, 2))
        ttk.Label(rowe, text="Engine").pack(side="left")
        self.engine_var = tk.StringVar(value="detecting...")
        self.engine_combo = ttk.Combobox(rowe, textvariable=self.engine_var,
                                         state="disabled", width=44, values=[])
        self.engine_combo.pack(side="left", padx=(4, 6))
        self.redetect_btn = ttk.Button(rowe, text="Re-detect",
                                       command=self.redetect_engines, state="disabled")
        self.redetect_btn.pack(side="left")
        self.engine_note = ttk.Label(rowe, text="", foreground="#777")
        self.engine_note.pack(side="left", padx=8)

        row2 = ttk.Frame(top)
        row2.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(2, 2))
        ttk.Label(row2, text="If a transcript already exists:").pack(side="left")
        self.dup_var = tk.StringVar(value=cfg.get("dup_policy", DUP_ASK))
        ttk.Combobox(row2, textvariable=self.dup_var, state="readonly", width=26,
                     values=[DUP_ASK, DUP_SKIP, DUP_REDO]).pack(side="left", padx=6)

        # ---- action buttons
        btns = ttk.Frame(outer)
        btns.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.run_btn = ttk.Button(btns, text="Transcribe", command=self.transcribe_now)
        self.run_btn.pack(side="left", padx=(0, 6))
        # No dialog here -- the file comes from the Browse field above, with
        # whatever settings are currently selected. Doubles as "Update queue
        # item" when a queued row is being edited.
        self.add_btn = ttk.Button(btns, text="Add to queue",
                                  command=self.add_current)
        self.add_btn.pack(side="left", padx=3)
        ttk.Button(btns, text="Add folder...",
                   command=self.add_folder).pack(side="left", padx=3)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_now, state="disabled")
        self.stop_btn.pack(side="left", padx=(16, 3))
        ttk.Button(btns, text="Remove selected",
                   command=self.remove_selected).pack(side="left", padx=(16, 3))
        ttk.Button(btns, text="Clear finished",
                   command=self.clear_finished).pack(side="left", padx=3)
        self.edit_note = ttk.Label(btns, text="", foreground="#0b57d0")
        self.edit_note.pack(side="left", padx=10)

        # ---- queue grid
        qf = ttk.LabelFrame(outer, text="Queue", padding=6)
        qf.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        outer.rowconfigure(2, weight=3)
        qf.rowconfigure(0, weight=1)
        qf.columnconfigure(0, weight=1)

        cols = ("file", "lang", "model", "spk", "status", "detail")
        self.tree = ttk.Treeview(qf, columns=cols, show="headings", selectmode="extended")
        for c, txt, w, anchor in (
                ("file", "File", 330, "w"),
                ("lang", "Language", 90, "w"),
                ("model", "Model", 130, "w"),
                ("spk", "Speakers", 75, "center"),
                ("status", "Status", 90, "w"),
                ("detail", "Detail", 260, "w")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "detail"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(qf, command=self.tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.tag_configure(ST_DONE, foreground="#1a7f37")
        self.tree.tag_configure(ST_FAIL, foreground="#b3261e")
        self.tree.tag_configure(ST_SKIP, foreground="#8a6d00")
        self.tree.tag_configure(ST_CANCEL, foreground="#777777")
        self.tree.tag_configure(ST_RUN, foreground="#0b57d0")
        self.tree.tag_configure("editing", background="#e8f0fe")
        self.tree.bind("<Double-1>", self.on_row_double_click)

        # ---- progress + status
        self.bar = ttk.Progressbar(outer, mode="determinate", maximum=1000)
        self.bar.grid(row=3, column=0, sticky="ew", pady=(10, 2))
        self.status = tk.StringVar(value="Idle")
        ttk.Label(outer, textvariable=self.status, foreground="#555"
                  ).grid(row=4, column=0, sticky="w")

        # ---- log pane
        lf = ttk.LabelFrame(outer, text="Console", padding=6)
        lf.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        outer.rowconfigure(5, weight=2)
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        self.log = tk.Text(lf, wrap="word", height=10, font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(lf, command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set, state="disabled")

        bottom = ttk.Frame(outer)
        bottom.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(bottom, text="Open logs folder",
                   command=lambda: os.startfile(str(LOGDIR))).pack(side="left")
        ttk.Label(bottom, foreground="#777", text=(
            "   drop files anywhere on this window  ·  double-click a queued row "
            "to edit it, a finished row to open its output")).pack(side="left")

        self.sync_enabled()
        root.bind("<Escape>", lambda _e: self.cancel_edit())

    def _validate_spk(self, proposed: str) -> bool:
        return proposed == "" or (proposed.isdigit() and len(proposed) <= 2)

    def sync_enabled(self):
        self.spk_entry.configure(state="normal" if self.diar_var.get() else "disabled")

    # ---------------------------------------------------------------- config
    def load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_settings(self):
        try:
            SETTINGS.write_text(json.dumps({
                "lang_label": self.lang_var.get(),
                "model_label": self.model_var.get(),
                "diarize": self.diar_var.get(),
                "speakers": self.spk_var.get(),
                "threads": self.thr_var.get(),
                "dup_policy": self.dup_var.get(),
                "engine_label": self.engine_var.get(),
                "engine_fingerprint": self.engine_fp,
                "engine_info": self.engine_info,
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------ logs
    def open_session_log(self):
        try:
            LOGDIR.mkdir(exist_ok=True)
            p = LOGDIR / f"session_{time.strftime('%Y%m%d-%H%M%S')}.log"
            self.session_log = open(p, "w", encoding="utf-8", buffering=1)
            self.say(f"[log] session log: {p.name}")
        except Exception as e:
            self.session_log = None
            self.say(f"[warn] no session log: {e!r}")

    def open_job_log(self, src: Path):
        self.close_job_log()
        try:
            LOGDIR.mkdir(exist_ok=True)
            p = LOGDIR / f"{safe_name(src.stem)}_{time.strftime('%Y%m%d-%H%M%S')}.log"
            self.job_log = open(p, "w", encoding="utf-8", buffering=1)
            self.say(f"[log] {p.name}")
        except Exception as e:
            self.job_log = None
            self.say(f"[warn] no job log: {e!r}")

    def close_job_log(self):
        if self.job_log:
            try:
                self.job_log.close()
            except Exception:
                pass
            self.job_log = None

    def say(self, text):
        line = text.rstrip()
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        stamped = f"[{time.strftime('%H:%M:%S')}] {line}\n"
        for fh in (self.session_log, self.job_log):
            if fh:
                try:
                    fh.write(stamped)
                except Exception:
                    pass
        try:
            sys.stdout.write(stamped)
            sys.stdout.flush()
        except Exception:
            pass

    # --------------------------------------------------------------- engines
    def init_engines(self, cfg):
        """Use the cached device list when the environment is unchanged;
        otherwise detect in the background so startup is not blocked."""
        fp = env_fingerprint()
        cached_fp = cfg.get("engine_fingerprint")
        cached = cfg.get("engine_info")

        if cached and cached_fp == fp and cached.get("devices"):
            self.engine_fp = fp
            self.engine_info = cached
            self.detect_done = True
            self.apply_engine_options(cfg.get("engine_label"))
            self.say("[engine] using cached detection "
                     "(delete .transcribe_gui.json or press Re-detect to refresh)")
            return

        if cached and cached_fp != fp:
            self.say("[engine] environment changed since last run -- re-detecting")

        # Never block on detection. CPU always exists, so offer it immediately
        # and add any GPU to the list when the probe finishes.
        self.engine_info = {"devices": [{"device": "cpu", "index": 0,
                                         "name": "CPU",
                                         "compute_types": ["int8", "float32"]}]}
        self.apply_engine_options(cfg.get("engine_label"), final=False)
        self.say("[engine] CPU available now; probing for GPUs in the "
                 "background (imports torch, can take 10-40 s on first run)")
        self.start_detect(cfg.get("engine_label"))

    def start_detect(self, prefer_label=None):
        # Text change makes it obvious the button is busy rather than broken.
        self.redetect_btn.configure(state="disabled", text="Detecting...")
        self.detect_started = time.time()

        def work():
            info = None
            try:
                info = detect_engines(timeout=180)
            except Exception as e:
                info = {"devices": [], "errors": [f"detector thread: {e!r}"]}
            finally:
                # Must always post, or the button stays disabled forever.
                self.q.put(("engines", (info or {"devices": [], "errors":
                                                 ["detector returned nothing"]},
                                        prefer_label)))

        threading.Thread(target=work, daemon=True).start()

    def redetect_engines(self):
        if self.worker and self.worker.is_alive():
            self.say("[engine] cannot re-detect while a job is running")
            return
        self.say("[engine] re-detecting...")
        self.engine_var.set("detecting available engines...")
        self.start_detect(self.engine_var.get())

    def apply_engine_options(self, prefer_label=None, final=True):
        self.engine_opts = engine_options(self.engine_info)
        labels = [lbl for lbl, _ in self.engine_opts]
        self.engine_combo.configure(values=labels, state="readonly")
        self.redetect_btn.configure(state="normal", text="Re-detect")

        chosen = prefer_label if prefer_label in labels else labels[0]
        self.engine_var.set(chosen)
        self.on_engine_change()
        self.engine_combo.bind("<<ComboboxSelected>>",
                               lambda _e: self.on_engine_change())

        if not final:
            return
        gpus = [l for l in labels if l.startswith("CUDA")]
        self.say(f"[engine] {len(labels)} option(s): {', '.join(labels)}")
        if not gpus:
            self.say("[engine] no CUDA device visible -- CPU only. CTranslate2 "
                     "has no Metal/DirectML/Vulkan/NPU backend, so CPU is the "
                     "only option without an NVIDIA GPU.")
        for err in (self.engine_info.get("errors") or [])[:4]:
            self.say(f"[engine] note: {err}")

    def on_engine_change(self):
        label = self.engine_var.get()
        for lbl, spec in self.engine_opts:
            if lbl == label:
                self.engine_spec = spec
                break
        s = self.engine_spec
        try:
            self.engine_note.configure(
                text=f"{s['compute_type']} · session-wide, applies to batches")
        except Exception:
            pass
        self.say(f"[engine] selected {label}  -> --device {s['device']} "
                 f"--device_index {s['index']} --compute_type {s['compute_type']}")

        # The thread count controls CTranslate2's CPU inference and is
        # meaningless for GPU decoding, so grey it out. It is not *entirely*
        # inert -- whisper-ctranslate2 also feeds it to torch.set_num_threads(),
        # which pyannote's CPU-side work uses -- so when disabled we quietly
        # pass the physical core count instead of whatever was last typed.
        on_cpu = (s["device"] == "cpu")
        try:
            self.thr_spin.configure(state="normal" if on_cpu else "disabled")
            self.thr_label.configure(foreground="" if on_cpu else "#999")
            self.thr_note.configure(
                text="" if on_cpu else f"(CPU only; using {physical_cores()} "
                                       f"for diarization)")
        except Exception:
            pass
        if not on_cpu:
            self.say("[engine] --threads does not affect GPU decoding; "
                     f"passing {physical_cores()} for the CPU-side "
                     "diarization work")

    def preflight(self):
        self.say(f"{APP} ready -- {physical_cores()} physical cores.")
        if not which("ffmpeg"):
            self.say("[warn] ffmpeg not on PATH; files are fed to whisper as-is. "
                     "Run Setup.ps1 or: winget install Gyan.FFmpeg.Shared")
        if not os.environ.get("HF_TOKEN"):
            self.say("[warn] HF_TOKEN not set -- speaker labelling unavailable. "
                     "Run: .\\Setup.ps1 -TokenOnly")
        self.say("[info] no admin rights needed once models are cached.")

    # ----------------------------------------------------------- enqueueing
    def pick_file(self):
        p = filedialog.askopenfilename(title="Choose a recording",
                                       filetypes=MEDIA_FILTER, initialdir=str(HERE))
        if p:
            self.file_var.set(p)

    def snapshot(self, src: Path, iid: str) -> Job:
        lang_label = self.lang_var.get()
        model_label = self.model_var.get()
        spk = self.spk_var.get().strip()
        return Job(iid, src,
                   dict(LANGUAGES)[lang_label], lang_label,
                   dict(MODELS)[model_label], model_label,
                   spk, bool(self.diar_var.get()), int(self.thr_var.get()))

    def ask_reprocess(self, items):
        """items: list of (Path, detail). Returns a set of paths to reprocess.

        One dialog for the whole batch rather than N message boxes, with an
        option to decide file by file.
        """
        if not items:
            return set()

        if len(items) == 1:
            src, detail = items[0]
            yes = messagebox.askyesno(
                APP,
                f"{src.name}\n\nalready has a transcript ({detail}).\n\n"
                "Reprocess it and overwrite the output?")
            return {src} if yes else set()

        dlg = tk.Toplevel(self.root)
        dlg.title(f"{len(items)} file(s) already transcribed")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("620x420")

        ttk.Label(dlg, padding=10, justify="left", text=(
            f"{len(items)} of the files you added already have a transcript "
            "folder.\nReprocessing overwrites the existing output."
        )).pack(anchor="w")

        box = ttk.Frame(dlg, padding=(10, 0))
        box.pack(fill="both", expand=True)
        lst = tk.Listbox(box, font=("Consolas", 9))
        lst.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, command=lst.yview)
        sb.pack(side="right", fill="y")
        lst.configure(yscrollcommand=sb.set)
        for src, detail in items:
            lst.insert("end", f"{src.name}   ({detail})")

        choice = {"value": "skip"}

        def close(val):
            choice["value"] = val
            dlg.destroy()

        bar = ttk.Frame(dlg, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="Reprocess all",
                   command=lambda: close("all")).pack(side="left")
        ttk.Button(bar, text="Skip all",
                   command=lambda: close("skip")).pack(side="left", padx=6)
        ttk.Button(bar, text="Decide one by one",
                   command=lambda: close("each")).pack(side="left", padx=6)

        dlg.protocol("WM_DELETE_WINDOW", lambda: close("skip"))
        self.root.wait_window(dlg)

        if choice["value"] == "all":
            return {src for src, _ in items}
        if choice["value"] == "skip":
            return set()

        redo = set()
        for src, detail in items:
            if messagebox.askyesno(APP, f"{src.name}\n\nalready transcribed "
                                        f"({detail}).\n\nReprocess it?"):
                redo.add(src)
        return redo

    def enqueue(self, paths, announce=True) -> int:
        """Returns the number actually queued (skips excluded)."""
        # Duplicate check across the ENTIRE grid, not just pending rows.
        # Active duplicates are refused; finished rows are replaced so a
        # re-add does not leave two rows for one file.
        with self.lock:
            active = {}     # normalised path -> Job (Queued / Running)
            finished = {}   # normalised path -> iid (Done / Skipped / ...)
            for j in self.jobs.values():
                key = os.path.normcase(os.path.abspath(str(j.src)))
                if j.status in (ST_QUEUED, ST_RUN):
                    active[key] = j
                else:
                    finished[key] = j.iid

        seen_now = set()
        stale_rows = []
        fresh, dupes = [], []
        for p in paths:
            src = Path(p)
            if not src.is_file():
                self.say(f"[skip] not a file: {src}")
                continue
            key = os.path.normcase(os.path.abspath(str(src)))

            if key in seen_now:
                self.say(f"[dup] {src.name} listed twice in this selection -- "
                         f"ignoring the repeat")
                continue
            if key in active:
                st = active[key].status.lower()
                self.say(f"[dup] {src.name} is already in the queue ({st}) -- "
                         f"not added again")
                continue
            if key in finished:
                stale_rows.append(finished[key])
                self.say(f"[dup] {src.name} was processed earlier in this "
                         f"session -- replacing its row")

            seen_now.add(key)
            done, detail = already_processed(src)
            if done:
                dupes.append((src, detail))
            else:
                fresh.append(src)

        # Decide what to do with the files that already have output.
        redo = set()
        if dupes:
            policy = self.dup_var.get()
            for src, detail in dupes:
                self.say(f"[exists] {src.name} -- transcript present ({detail})")
            if policy == DUP_REDO:
                redo = {src for src, _ in dupes}
                self.say(f"[exists] policy '{policy}' -> reprocessing all "
                         f"{len(dupes)} of them")
            elif policy == DUP_SKIP:
                self.say(f"[exists] policy '{policy}' -> skipping all "
                         f"{len(dupes)} of them")
            else:
                redo = self.ask_reprocess(dupes)
                self.say(f"[exists] you chose to reprocess {len(redo)} of "
                         f"{len(dupes)}")

        # Drop superseded rows for files being re-added.
        for iid in stale_rows:
            with self.lock:
                self.jobs.pop(iid, None)
                if iid in self.order:
                    self.order.remove(iid)
            if self.tree.exists(iid):
                self.tree.delete(iid)

        added = 0
        for src in fresh + [s for s, _ in dupes]:
            forced = src in redo
            was_dupe = any(src == d for d, _ in dupes)
            if was_dupe and not forced:
                detail = next(dt for d, dt in dupes if d == src)
                iid = self.tree.insert("", "end", values=(
                    src.name, "-", "-", "-", ST_SKIP, f"already processed: {detail}"),
                    tags=(ST_SKIP,))
                j = self.snapshot(src, iid)
                j.status, j.detail = ST_SKIP, detail
                with self.lock:
                    self.jobs[iid] = j
                    self.order.append(iid)
                continue

            iid = self.tree.insert("", "end", values=("", "", "", "", "", ""))
            j = self.snapshot(src, iid)
            j.force = forced
            spk_txt = j.spk if (j.diarize and j.spk) else ("auto" if j.diarize else "-")
            note = "will overwrite existing output" if forced else ""
            self.tree.item(iid, values=(src.name, j.lang_label, j.model,
                                        spk_txt, ST_QUEUED, note), tags=(ST_QUEUED,))
            with self.lock:
                self.jobs[iid] = j
                self.order.append(iid)
            added += 1
            if announce:
                self.say(f"[queue] + {src.name}  ({j.lang_label}, {j.model}, "
                         f"speakers={spk_txt})"
                         + ("  [reprocess]" if forced else ""))
        self.update_counts()
        return added

    def current_file(self):
        raw = self.file_var.get().strip().strip('"')
        if not raw:
            messagebox.showinfo(APP, "Use Browse... to choose a file first.")
            return None
        src = Path(raw)
        if not src.is_file():
            messagebox.showerror(APP, f"Not found:\n{src}")
            return None
        return src

    def add_current(self):
        """Queue the file in the Browse field with the current settings, or
        apply the settings to the row being edited."""
        if self.editing_iid:
            self.apply_edit()
            return
        src = self.current_file()
        if not src:
            return
        if self.enqueue([src]):
            self.say(f"[queue] added {src.name}; adjust settings and add more, "
                     f"or press Transcribe")

    # ------------------------------------------------------------ drag/drop
    def setup_drop(self, root):
        if not getattr(root, "_dnd_ok", False):
            self.say("[info] drag and drop is off (tkinterdnd2 not installed). "
                     "Enable it with:  pip install tkinterdnd2   "
                     "or re-run Setup.ps1. Browse... works regardless.")
            return
        try:
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", self._on_drop_event)
            self.say("[info] drag and drop enabled -- drop files or folders "
                     "onto this window")
        except Exception as e:
            self.say(f"[warn] could not register drop target: {e!r}")

    def _on_drop_event(self, event):
        """tkdnd hands over a Tcl list; paths with spaces arrive brace-quoted,
        so let Tcl split it rather than parsing by hand."""
        try:
            raw = self.root.tk.splitlist(event.data)
        except Exception:
            raw = [p for p in str(event.data).split() if p]
        self.on_drop([str(p) for p in raw])

    def on_drop(self, paths):
        """Explorer drop. One file -> the Browse field. Several -> straight
        into the queue with the current settings."""
        media = []
        for p in paths:
            q = Path(p)
            if q.is_dir():
                found = sorted(x for x in q.iterdir()
                               if x.is_file() and x.suffix.lower() in MEDIA_EXT)
                self.say(f"[drop] folder {q.name}: {len(found)} media file(s)")
                media.extend(found)
            elif q.suffix.lower() in MEDIA_EXT:
                media.append(q)
            else:
                self.say(f"[drop] ignoring non-media file: {q.name}")

        if not media:
            self.say("[drop] nothing usable dropped")
            return

        if len(media) == 1:
            src = media[0]
            if self.editing_iid:
                # File is locked while editing; do not silently repoint the row.
                self.say(f"[drop] editing a queued item -- file stays "
                         f"{Path(self.file_var.get()).name}. "
                         f"Press Escape to cancel the edit first.")
                return
            self.file_var.set(str(src))
            self.say(f"[drop] {src.name} -> File field. Adjust settings, then "
                     f"'Add to queue' or 'Transcribe'.")
            return

        if self.editing_iid:
            self.cancel_edit(quiet=True)
        self.say(f"[drop] {len(media)} files -- adding to the queue with the "
                 f"current settings")
        n = self.enqueue(media)
        self.say(f"[drop] queued {n} file(s)")

    # ----------------------------------------------------------- edit mode
    def begin_edit(self, iid):
        """Load a queued row's settings back into the controls."""
        j = self.jobs.get(iid)
        if not j or j.status != ST_QUEUED:
            return
        self.editing_iid = iid
        self.file_var.set(str(j.src))
        self.lang_var.set(j.lang_label)
        self.model_var.set(j.model_label)
        self.diar_var.set(j.diarize)
        self.spk_var.set(j.spk)
        self.thr_var.set(j.threads)
        self.sync_enabled()

        # Source file is fixed while editing -- changing it would create a
        # duplicate or silently repoint the job.
        self.file_entry.configure(state="readonly")
        self.browse_btn.configure(state="disabled")
        self.add_btn.configure(text="Update queue item")
        self.edit_note.configure(
            text=f"editing: {j.src.name}   (Esc to cancel)")
        self.tree.item(iid, tags=("editing",))
        self.say(f"[edit] {j.src.name} -- change the settings above and press "
                 f"'Update queue item'")

    def cancel_edit(self, quiet=False):
        if not self.editing_iid:
            return
        iid = self.editing_iid
        self.editing_iid = None
        self.file_entry.configure(state="normal")
        self.browse_btn.configure(state="normal")
        self.add_btn.configure(text="Add to queue")
        self.edit_note.configure(text="")
        if self.tree.exists(iid):
            j = self.jobs.get(iid)
            self.tree.item(iid, tags=((j.status,) if j else (ST_QUEUED,)))
        if not quiet:
            self.say("[edit] cancelled")

    def apply_edit(self):
        iid = self.editing_iid
        j = self.jobs.get(iid) if iid else None
        if not j:
            self.cancel_edit(quiet=True)
            return
        if j.status != ST_QUEUED:
            self.say(f"[edit] {j.src.name} is no longer queued ({j.status}) -- "
                     f"changes discarded")
            self.cancel_edit(quiet=True)
            return

        before = (j.lang_label, j.model_label, j.spk, j.diarize, j.threads)
        j.lang_label = self.lang_var.get()
        j.lang = dict(LANGUAGES)[j.lang_label]
        j.model_label = self.model_var.get()
        j.model = dict(MODELS)[j.model_label]
        j.diarize = bool(self.diar_var.get())
        j.spk = self.spk_var.get().strip()
        j.threads = int(self.thr_var.get())
        after = (j.lang_label, j.model_label, j.spk, j.diarize, j.threads)

        spk_txt = j.spk if (j.diarize and j.spk) else ("auto" if j.diarize else "-")
        vals = list(self.tree.item(iid, "values"))
        vals[1], vals[2], vals[3] = j.lang_label, j.model, spk_txt
        self.tree.item(iid, values=vals)

        if before == after:
            self.say(f"[edit] {j.src.name} -- nothing changed")
        else:
            self.say(f"[edit] {j.src.name} updated: {j.lang_label}, {j.model}, "
                     f"speakers={spk_txt}, threads={j.threads}")
        self.cancel_edit(quiet=True)

    def add_folder(self):
        d = filedialog.askdirectory(title="Add every recording in a folder",
                                    initialdir=str(HERE))
        if not d:
            return
        found = sorted(p for p in Path(d).iterdir()
                       if p.is_file() and p.suffix.lower() in MEDIA_EXT)
        if not found:
            self.say(f"[queue] no media files in {d}")
            return
        self.say(f"[queue] scanning {d} -- {len(found)} candidate(s)")
        n = self.enqueue(found)
        self.say(f"[queue] added {n} file(s)")

    def transcribe_now(self):
        """Single action for both modes.

        Queue has pending work  -> process it in bulk (the Browse field is
                                   ignored; add it explicitly first if wanted).
        Queue empty             -> queue the Browse field's file and run it.
        """
        pending = self.next_queued() is not None
        if pending:
            with self.lock:
                n = sum(1 for j in self.jobs.values() if j.status == ST_QUEUED)
            self.say(f"[queue] starting bulk run -- {n} file(s) pending")
            self.start_queue()
            return

        src = self.current_file()
        if not src:
            return
        # enqueue() applies the duplicate policy (ask / skip / reprocess).
        if self.enqueue([src]) == 0:
            self.say(f"[queue] nothing to do for {src.name}")
            return
        self.start_queue()

    def remove_selected(self):
        if self.editing_iid in self.tree.selection():
            self.cancel_edit(quiet=True)
        for iid in self.tree.selection():
            with self.lock:
                j = self.jobs.get(iid)
                if j and j.status == ST_RUN:
                    self.say(f"[queue] {j.src.name} is running; use Stop first.")
                    continue
                self.jobs.pop(iid, None)
                if iid in self.order:
                    self.order.remove(iid)
            self.tree.delete(iid)
        self.update_counts()

    def clear_finished(self):
        for iid in list(self.order):
            with self.lock:
                j = self.jobs.get(iid)
                if j and j.status in (ST_DONE, ST_SKIP, ST_FAIL, ST_CANCEL):
                    self.jobs.pop(iid, None)
                    self.order.remove(iid)
                    dead = True
                else:
                    dead = False
            if dead:
                self.tree.delete(iid)
        self.update_counts()

    def on_row_double_click(self, event=None):
        """Queued row  -> load its settings for editing.
        Finished row -> open its output folder."""
        iid = self.tree.identify_row(event.y) if event else None
        if not iid:
            sel = self.tree.selection()
            iid = sel[0] if sel else None
        if not iid:
            return
        j = self.jobs.get(iid)
        if not j:
            return

        if j.status == ST_QUEUED:
            self.begin_edit(iid)
            return
        if j.status == ST_RUN:
            self.say(f"[edit] {j.src.name} is already running -- "
                     f"press Stop first to change it")
            return

        d = output_dir_for(j.src)
        if d.is_dir():
            os.startfile(str(d))
        else:
            self.say(f"[info] no output folder for {j.src.name}")

    def update_counts(self):
        with self.lock:
            tally = {}
            for j in self.jobs.values():
                tally[j.status] = tally.get(j.status, 0) + 1
        pending = tally.get(ST_QUEUED, 0)
        # One button, two meanings -- make the label say which one applies.
        try:
            if pending:
                self.run_btn.configure(text=f"Transcribe queue ({pending})")
            else:
                self.run_btn.configure(text="Transcribe")
        except Exception:
            pass
        parts = [f"{k}: {v}" for k, v in sorted(tally.items())]
        if self.current is None:
            self.status.set("Idle   " + "   ".join(parts) if parts else "Idle")

    # ------------------------------------------------------------- worker
    def next_queued(self):
        with self.lock:
            for iid in self.order:
                j = self.jobs.get(iid)
                if j and j.status == ST_QUEUED:
                    return j
        return None

    def start_queue(self):
        if self.worker and self.worker.is_alive():
            self.say("[queue] worker already running; new items will be picked up.")
            return
        if self.next_queued() is None:
            self.say("[queue] nothing to do.")
            return
        # A row could start running mid-edit; drop the edit rather than let it
        # apply to a job already in flight.
        if self.editing_iid:
            self.say("[edit] queue starting -- edit cancelled")
            self.cancel_edit(quiet=True)
        self.save_settings()
        self.stop_queue = False
        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        # Locked mid-run: switching device between files in one batch would make
        # the results inconsistent and the timings meaningless.
        self.engine_combo.configure(state="disabled")
        self.redetect_btn.configure(state="disabled")

    def stop_now(self):
        self.stop_queue = True
        self.cancel_current = True
        self.say("[stop] aborting the current file and halting the queue...")
        p = self.proc
        if p and p.poll() is None:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                               capture_output=True, creationflags=NO_WINDOW)
            except Exception as e:
                self.say(f"[stop] {e!r}")

    def worker_loop(self):
        try:
            while not self.stop_queue:
                job = self.next_queued()
                if job is None:
                    break
                self.cancel_current = False
                self.run_job(job)
        except Exception:
            import traceback as tb
            self.q.put(("log", tb.format_exc()))
        finally:
            self.q.put(("idle", None))

    # ------------------------------------------------------------- one job
    def probe_duration(self, path: Path) -> float:
        if not which("ffprobe"):
            return 0.0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=90,
                creationflags=NO_WINDOW).stdout.strip()
            return float(out)
        except Exception:
            return 0.0

    def normalise(self, src: Path, outdir: Path):
        """16 kHz mono WAV in the output folder, named after the source stem so
        transcripts come out as <stem>.txt rather than <stem>_16k.txt.
        -vn drops embedded cover art that can break decoders."""
        if not which("ffmpeg"):
            return src
        wav = outdir / f"{src.stem}.wav"
        self.tempwav = wav
        self.q.put(("log", f"[1/3] converting -> {wav.name} (16 kHz mono)"))
        cp = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            capture_output=True, text=True, creationflags=NO_WINDOW)
        if cp.returncode != 0 or not wav.exists():
            self.q.put(("log", "[warn] conversion failed; using the original file"))
            if cp.stderr:
                self.q.put(("log", cp.stderr.strip()[:800]))
            self.tempwav = None
            return src
        mb = wav.stat().st_size / (1024 * 1024)
        self.q.put(("log", f"[info] {wav.name} ready ({mb:.1f} MB)"))
        return wav

    def cleanup(self):
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None
        if self.tempwav:
            try:
                if self.tempwav.exists():
                    self.tempwav.unlink()
                    self.q.put(("log", f"[cleanup] removed {self.tempwav.name}"))
            except Exception as e:
                self.q.put(("log", f"[cleanup] could not remove "
                                   f"{self.tempwav.name}: {e!r}"))
            self.tempwav = None

    def run_job(self, job: Job):
        self.current = job
        self.job_started = time.time()
        job.status = ST_RUN
        self.q.put(("row", (job.iid, ST_RUN, "starting...")))
        self.q.put(("joblog", job.src))
        self.q.put(("prog", 0.0))

        src = job.src
        try:
            # Re-check: output may have appeared after this was queued (another
            # job, another tool, or the same file added twice).
            done, detail = already_processed(src)
            if done and not job.force:
                policy = self.dup_var.get()
                self.q.put(("log", f"[exists] {src.name} already has a "
                                   f"transcript ({detail}); policy '{policy}'"))
                decision = False
                if policy == DUP_REDO:
                    decision = True
                elif policy == DUP_SKIP:
                    decision = False
                else:
                    # Ask on the GUI thread and block here for the answer.
                    ev = threading.Event()
                    holder = {"v": False}
                    self.q.put(("ask", (src, detail, ev, holder)))
                    if not ev.wait(timeout=600):
                        self.q.put(("log", "[exists] no answer in 10 min -- skipping"))
                    decision = bool(holder["v"])
                if not decision:
                    job.status, job.detail = ST_SKIP, detail
                    self.q.put(("log", f"[skip] {src.name}"))
                    self.q.put(("row", (job.iid, ST_SKIP,
                                        f"already processed: {detail}")))
                    return
                job.force = True
                self.q.put(("log", f"[exists] reprocessing {src.name} "
                                   "(output will be overwritten)"))
            elif done and job.force:
                self.q.put(("log", f"[exists] {src.name} -- reprocessing as "
                                   f"approved ({detail})"))

            self.q.put(("log", "=" * 62))
            self.q.put(("log", f"[job] {src.name}"))
            self.q.put(("log", f"[job] {job.lang_label} / {job.model} / "
                               f"speakers={job.spk or 'auto'} / threads={job.threads}"))
            self.q.put(("log", f"[job] engine {self.engine_spec['device']}:"
                               f"{self.engine_spec['index']} "
                               f"{self.engine_spec['compute_type']}"))

            outdir = output_dir_for(src)
            outdir.mkdir(parents=True, exist_ok=True)

            dur = self.probe_duration(src)
            if dur:
                self.q.put(("log", f"[info] length {hhmmss(dur)}"))

            audio = self.normalise(src, outdir)
            if self.cancel_current:
                job.status = ST_CANCEL
                self.q.put(("row", (job.iid, ST_CANCEL, "stopped")))
                return

            self.tmpdir = tempfile.mkdtemp(prefix="transcribe_")
            shim = Path(self.tmpdir) / "_shim.py"
            shim.write_text(SHIM, encoding="ascii")

            # python.exe rather than pythonw.exe: better behaved piped stdio.
            exe = Path(sys.executable)
            if exe.name.lower() == "pythonw.exe":
                cand = exe.with_name("python.exe")
                if cand.exists():
                    exe = cand

            # Engine is session-wide, read at launch time rather than snapshotted
            # per job -- a batch always runs on one device.
            eng = dict(self.engine_spec or {})
            if eng.get("device") not in ("cpu", "cuda"):
                self.q.put(("log", f"[engine] unusable selection {eng!r} -- "
                                   "falling back to CPU"))
                eng = {"device": "cpu", "index": 0, "compute_type": "int8"}
            if not self.detect_done and eng["device"] == "cpu":
                self.q.put(("log", "[engine] device probe still running; this "
                                   "job uses CPU. Any GPU found later applies "
                                   "to jobs started after it appears."))

            # --threads is a CPU-inference control. On GPU, pass the physical
            # core count so pyannote's CPU-side work is still sensible.
            threads = job.threads if eng["device"] == "cpu" else physical_cores()
            cmd = [str(exe), str(shim), str(audio),
                   "--model", job.model,
                   "--task", "transcribe",
                   "--device", eng["device"],
                   "--device_index", str(eng["index"]),
                   "--compute_type", eng["compute_type"],
                   "--threads", str(threads),
                   "--vad_filter", "True",
                   "--verbose", "True",
                   "--output_dir", str(outdir),
                   "--output_format", "all"]
            if job.lang:
                cmd += ["--language", job.lang]

            auto_spk = False
            if job.diarize:
                cmd += ["--hf_token", os.environ.get("HF_TOKEN", "")]
                if job.spk.isdigit() and int(job.spk) >= 1:
                    cmd += ["--speaker_num", job.spk]
                else:
                    auto_spk = True
                    self.q.put(("log", "[info] speaker count: auto-detect "
                                       "(less reliable than an exact number)"))

            shown = " ".join("hf_****" if c.startswith("hf_") else c for c in cmd[1:])
            self.q.put(("log", f"[2/3] {exe.name} {shown}"))
            if job.diarize:
                self.q.put(("row", (job.iid, ST_RUN, "identifying speakers...")))

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
            env["WCT2_AUTO_SPEAKERS"] = "1" if auto_spk else "0"

            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=env, creationflags=NO_WINDOW)

            ts = re.compile(r"\[(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\s*-+>")
            for line in self.proc.stdout:
                if not line.strip():
                    continue
                m = ts.search(line)
                if m and dur:
                    h = int(m.group(1) or 0)
                    sec = h * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                    self.q.put(("prog", min(1.0, sec / dur)))
                self.q.put(("log", line.rstrip()[:400]))

            rc = self.proc.wait()
            self.proc = None
            el = hhmmss(time.time() - self.job_started)

            if self.cancel_current:
                job.status, job.detail = ST_CANCEL, "stopped by user"
                self.q.put(("row", (job.iid, ST_CANCEL, f"stopped after {el}")))
                return
            if rc != 0:
                job.status, job.detail = ST_FAIL, f"exit {rc}"
                self.q.put(("log", f"[fail] {src.name} exited {rc} after {el}"))
                self.q.put(("row", (job.iid, ST_FAIL, f"exit {rc} after {el}")))
                return

            files = sorted(p.name for p in outdir.iterdir()
                           if p.is_file() and p != self.tempwav)
            self.q.put(("log", f"[3/3] wrote {len(files)} file(s)"))

            spk_note = ""
            try:
                txt = next((p for p in outdir.iterdir() if p.suffix == ".txt"), None)
                if txt:
                    body = txt.read_text(encoding="utf-8", errors="replace")
                    found = sorted(set(re.findall(r"SPEAKER_\d+", body)))
                    if found:
                        counts = {s: body.count(f"[{s}]") for s in found}
                        self.q.put(("log", "[info] " + str(len(found)) +
                                    " speaker(s): " +
                                    ", ".join(f"{s}={n}" for s, n in counts.items())))
                        spk_note = f", {len(found)} speakers"
                    else:
                        self.q.put(("log", "[warn] no speaker labels in output"))
            except Exception:
                pass

            job.status = ST_DONE
            job.detail = f"{el}{spk_note}"
            self.q.put(("prog", 1.0))
            self.q.put(("row", (job.iid, ST_DONE, f"{el}{spk_note}")))
            self.q.put(("log", f"[done] {src.name} in {el}"))

        except Exception as e:
            import traceback as tb
            self.q.put(("log", tb.format_exc()))
            job.status, job.detail = ST_FAIL, repr(e)
            self.q.put(("row", (job.iid, ST_FAIL, repr(e)[:120])))
        finally:
            self.cleanup()
            self.current = None
            self.q.put(("closejoblog", None))

    # ---------------------------------------------------------------- pump
    def drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.say(payload)
                elif kind == "row":
                    iid, status, detail = payload
                    if self.tree.exists(iid):
                        vals = list(self.tree.item(iid, "values"))
                        vals[4] = status
                        vals[5] = detail
                        self.tree.item(iid, values=vals, tags=(status,))
                        if status == ST_RUN:
                            self.tree.see(iid)
                elif kind == "prog":
                    self.bar.configure(value=int(payload * 1000))
                    if self.current:
                        el = time.time() - self.job_started
                        eta = (el / payload - el) if payload > 0.02 else 0
                        with self.lock:
                            left = sum(1 for j in self.jobs.values()
                                       if j.status == ST_QUEUED)
                        self.status.set(
                            f"{self.current.src.name}  {payload*100:5.1f}%   "
                            f"elapsed {hhmmss(el)}"
                            + (f"   remaining ~{hhmmss(eta)}" if eta else "")
                            + (f"   ({left} more queued)" if left else ""))
                elif kind == "engines":
                    info, prefer = payload
                    took = time.time() - getattr(self, "detect_started", time.time())
                    self.detect_done = True
                    self.say(f"[engine] probe finished in {took:.1f}s")
                    if info.get("devices"):
                        self.engine_info = info
                        self.engine_fp = env_fingerprint()
                        self.apply_engine_options(prefer or self.engine_var.get())
                        self.save_settings()   # cache for the next startup
                    else:
                        # Keep the provisional CPU entry; just report why.
                        self.say("[engine] probe found nothing usable -- "
                                 "staying on CPU")
                        for err in (info.get("errors") or [])[:6]:
                            self.say(f"[engine] {err}")
                        self.redetect_btn.configure(state="normal",
                                                    text="Re-detect")
                elif kind == "ask":
                    # Asked by the worker thread; dialogs must run here.
                    src, detail, ev, holder = payload
                    try:
                        holder["v"] = messagebox.askyesno(
                            APP,
                            f"{src.name}\n\nalready has a transcript ({detail}).\n\n"
                            "Reprocess it now and overwrite the output?")
                    except Exception:
                        holder["v"] = False
                    ev.set()
                elif kind == "joblog":
                    self.open_job_log(payload)
                elif kind == "closejoblog":
                    self.close_job_log()
                elif kind == "idle":
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    if self.engine_opts:
                        self.engine_combo.configure(state="readonly")
                        self.redetect_btn.configure(state="normal",
                                                    text="Re-detect")
                    self.bar.configure(value=0)
                    self.update_counts()
                    with self.lock:
                        tally = {}
                        for j in self.jobs.values():
                            tally[j.status] = tally.get(j.status, 0) + 1
                    summary = "   ".join(f"{k}: {v}" for k, v in sorted(tally.items()))
                    self.say(f"[queue] idle. {summary}" if summary else "[queue] idle.")
                    self.status.set("Idle   " + summary)
        except queue.Empty:
            pass
        self.root.after(120, self.drain)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP, "A transcription is running.\n\n"
                                            "Quit and abandon it?"):
                return
            self.stop_now()
        self.save_settings()
        self.close_job_log()
        if self.session_log:
            try:
                self.session_log.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    root, dnd_ok = make_root()
    root._dnd_ok = dnd_ok
    try:
        root.call("tk", "scaling", 1.3)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
