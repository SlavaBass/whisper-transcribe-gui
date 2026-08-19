# Troubleshooting

Every failure met while building this tool, with the actual cause and the fix.
Most are undocumented interactions between Windows, PyTorch and HuggingFace
rather than bugs in Whisper.

---

## `OSError: [WinError 1314] A required privilege is not held by the client`

**When:** partway through a model download, after gigabytes have transferred.

**Cause:** `huggingface_hub` stores each file once as a content-hashed blob and
symlinks it into a snapshot folder. Creating a symlink on Windows requires
`SeCreateSymbolicLinkPrivilege`, which standard user accounts do not hold.

**Fix:** Settings → System → For developers → **Developer Mode: On**.

**The part that catches everyone:** privileges are written into your access token
**at logon**. Opening a new PowerShell window inherits the same token and changes
nothing — you must **sign out and sign back in** (or reboot). Until then, run
from an elevated shell.

**There is no environment-variable workaround.** `HF_HUB_DISABLE_SYMLINKS_WARNING`
only silences the warning; it does not change behaviour. There is no
`HF_HUB_DISABLE_SYMLINKS` setting, despite how plausible the name sounds.

Already-downloaded models keep working with Developer Mode off, because *reading*
a symlink needs no privilege. Only new downloads are affected.

---

## `_pickle.UnpicklingError: Weights only load failed`

Full text mentions `Unsupported global: GLOBAL pyannote.audio.core.task.Specifications`.

**Cause:** PyTorch 2.6 changed `torch.load`'s `weights_only` default from `False`
to `True`. pyannote checkpoints pickle objects that are not on the allowlist. It
affects pyannote.audio 4.0.0 with torch 2.8 — this is version skew, not a
corrupt download, and upgrading pyannote does not fix it.

**Fix:** the app generates a shim that runs before importing whisper-ctranslate2:

```python
import torch
torch.serialization.add_safe_globals([Specifications, Problem, Resolution])
_real = torch.load
def _load(*a, **kw):
    kw["weights_only"] = False
    return _real(*a, **kw)
torch.load = _load
```

`add_safe_globals` alone is not enough — it just surfaces the next blocked class,
so the default is restored outright. This permits arbitrary code execution during
unpickling and is only acceptable because the checkpoints come from the official
pyannote repositories. Do not apply it to checkpoints from unknown sources.

---

## `running scripts is disabled on this system`

**Fix, for the current window only:**

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Or without changing anything:

```powershell
powershell -ExecutionPolicy Bypass -File .\Setup.ps1
```

Avoid `-Scope CurrentUser`: it permanently lowers protection for one task.

---

## HTTP 401 or 403 from HuggingFace

Three distinct causes:

1. **Conditions not accepted.** Open
   `huggingface.co/pyannote/speaker-diarization-community-1` while signed in as
   the token's owner and accept the terms. A valid token belonging to an account
   that never accepted them yields 403 with no useful message.
2. **Fine-grained token missing permission.** Tick *"Read access to contents of
   all public gated repos you can access"*. Classic Read tokens include it.
3. **Token not visible to the process.** `setx` only affects windows opened
   *after* it ran.

`Setup.ps1`'s **Test token** button checks 1 and 3 explicitly.

---

## `whisper-ctranslate2 is not recognized`

Either the package is not installed, or its `Scripts` directory is not on PATH:

```powershell
pip show whisper-ctranslate2
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```

Add that directory to PATH. If `python --version` opens the Microsoft Store, you
have the App Execution Alias stub rather than real Python — install from
python.org with *Add python.exe to PATH* ticked.

Note that installing a *model* is not the same as installing the *program*;
the two are unrelated.

---

## `torchcodec is not installed correctly ... Could not load libtorchcodec`

**Usually harmless here.** whisper-ctranslate2 hands pyannote a preloaded
waveform, so torchcodec's decoder is never used. Diarization still runs.

If you do need it: torchcodec supports **FFmpeg 4–7 only**. FFmpeg 8 and 9 will
not load, and it needs a *shared* build with `avcodec-*.dll` on PATH.

---

## Cancelled a run and got nothing

By design, not a bug — whisper-ctranslate2 writes output only after the entire
file completes. There is no incremental flush, so an empty output folder during a
run is normal and a cancellation discards everything.

For a 59-minute call on 8 cores, budget around 70 minutes with `large-v3-turbo`.
Verify with `tiny` on a short clip first if you are unsure.

---

## `ffmpeg is not recognized` right after installing it

winget prints *"Path environment variable modified; restart your shell"* for a
reason. Either reopen PowerShell or refresh in place:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path','User')
```

The winget shim also lives at `%LOCALAPPDATA%\Microsoft\WinGet\Links`.

---

## Only one speaker in the output, or fewer than expected

- **Batched inference was enabled.** It merges output into ~30-second blocks and
  collapses attribution. On a 3-speaker test it reported 2 and produced
  repetition loops. Keep it off when labels matter.
- **Auto-detect under-counted.** Type the exact number instead.
- **`--speaker_num` defaults to 2.** Not passing it does *not* mean auto.
- **A participant barely spoke.** Short total speech time gets absorbed into
  another cluster.

## More speakers than expected

Almost always one person being split: variable connection quality, switching
audio devices, or long silence in the middle of their speech. Pin the count.

---

## Cyrillic shows as `?????` or `Ð¿Ñ€Ð¸Ð²`

The output files are UTF-8 and correct — the *viewer* is wrong. Notepad handles
it; some editors need an explicit encoding. In PowerShell run `chcp 65001`.

Mojibake inside a `Start-Transcript` log is an artefact of PowerShell's
transcript encoding, not of the transcript files.

---

## PowerShell dies mid-script with `TerminatingError`

If a script sets `$ErrorActionPreference = "Stop"` and calls a native executable
with `2>&1`, PowerShell treats **any** stderr line as a terminating error. Python
writing a warning is enough to kill the script, and the real message is lost.
Keep `Continue` and check `$LASTEXITCODE` explicitly.

---

## Filenames with spaces

Always quote paths. Unquoted, PowerShell splits
`2026-08-18 19-32-29_Call.mp3` into three separate arguments.

---

## GPU is installed but the Engine dropdown shows only CPU

Work through these in order:

1. **Is there a driver?** Run `nvidia-smi`. "not recognized" means no driver, and
   no amount of pip installing will help. Get it from
   [nvidia.com](https://www.nvidia.com/Download/index.aspx).
2. **Are the libraries present?** `pip show nvidia-cudnn-cu12 nvidia-cublas-cu12`.
   If missing, run `.\Setup.ps1 -Gpu`.
3. **Are the DLLs on PATH?** pip installs them to
   `site-packages\nvidia\<lib>\bin`, which is not on the loader path. Setup adds
   these to your user PATH, but only new processes see the change — reopen the
   terminal and the app.
4. **Version mismatch.** `ctranslate2` ≥ 4.5 requires CUDA ≥ 12.3 with cuDNN 9.
   Pairing it with cuDNN 8 produces DLL load failures rather than a clear error.
   Downgrade `ctranslate2` to 4.4.0 for cuDNN 8 on CUDA 12, or 3.24.0 for
   CUDA 11.
5. **Press Re-detect.** The device list is cached; the app only re-probes
   automatically when the environment fingerprint changes.

Diagnose directly with:

```powershell
python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
python -c "import torch; print(torch.cuda.is_available())"
```

If CTranslate2 reports 0 while torch reports `True`, the cuDNN/cuBLAS DLLs are
the problem, not the driver.

---

## My GPU is AMD or Intel, or my CPU has an NPU

It cannot be used. CTranslate2 implements **CPU and CUDA backends only** — no
ROCm on Windows, no DirectML, no Vulkan, no Metal, no XDNA/NPU. An AMD Ryzen AI
NPU is reachable only through a completely different runtime (ONNX Runtime with
the Vitis AI provider), which faster-whisper does not use.

CPU is the correct and only option on such machines. `large-v3-turbo` keeps that
practical: roughly 70 minutes for an hour of audio on 8 physical cores.

---

## Still stuck

Open an issue and attach the newest file from `logs\`. It records versions,
core count, the exact command line (token redacted) and the full error.
