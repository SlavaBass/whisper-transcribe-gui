# Contributing

Thanks for considering it. This is a small, focused tool and the bar for new
dependencies is high, but bug reports, fixes and documentation are very welcome.

## Reporting bugs

The single most useful thing you can attach is **the newest file from `logs\`**.
It records package versions, core count, the exact command line with the token
redacted, and the full traceback.

Please redact anything confidential first — log files contain source filenames
and, at `--verbose True`, transcript text.

Include:

- Windows version, Python version, and the versions of `torch`,
  `pyannote.audio`, `whisper-ctranslate2` and `ctranslate2`
- What you did, what happened, what you expected
- Whether it reproduces with `tiny` on a short clip

Check [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) first — most reports so
far have been one of the documented Windows or PyTorch interactions.

## Development setup

```powershell
git clone https://github.com/<you>/whisper-transcribe-gui
cd whisper-transcribe-gui
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
python Transcribe.pyw     # python, not pythonw, to see stdout
```

Running with `python.exe` gives you a console mirroring the log.

## Code style

- Standard library only for the GUI. **Do not add a GUI framework.** Zero-install
  is the point of the project.
- One documented exception: `tkinterdnd2`, an **optional** dependency for drag
  and drop. The app must keep working when it is absent. Optional extras are
  acceptable on those terms; required ones are not. The first attempt at drag
  and drop avoided the dependency by subclassing the Win32 window procedure with
  `ctypes` — it hard-crashed the process on every drop. Hand-written pointer
  handling is not a reasonable price for a convenience feature.
- Target Python 3.9 syntax; users get whatever winget installed.
- PEP 8, 4 spaces, ~88 column soft limit.
- Type hints where they clarify; not mandatory.
- Comment **why**, not what. Every workaround here exists for a non-obvious
  reason — record it or someone will "clean it up" and reintroduce the bug.

### PowerShell

- Must work on **PowerShell 5.1** (shipped with Windows). No ternary `? :`, no
  `??`, no `-Parallel`.
- Keep `$ErrorActionPreference = "Continue"` and check `$LASTEXITCODE`. With
  `Stop`, a native tool writing to stderr kills the script and hides the cause.
- Never interpolate a token into a logged string.

## Testing

There is no automated suite yet; contributions adding one are welcome. Manually,
before opening a PR:

1. `tiny` model, 90-second clip, fixed speaker count — completes and writes five files
2. Same clip with the speaker box blank — auto-detect path works
3. Queue three files, add a fourth mid-run — the fourth is picked up
4. Stop mid-job — status becomes `Cancelled`, the intermediate WAV is deleted
5. Re-add a finished file — the old row is replaced, not duplicated
6. A path containing spaces and non-ASCII characters
7. `.\Setup.ps1 -TokenOnly` — dialog opens, Test token reports correctly

## Pull requests

- One logical change per PR
- Update `CHANGELOG.md` under `## [Unreleased]`
- Update the README if behaviour changes
- Say which Windows and Python versions you tested on

## Scope

**In scope:** reliability, clearer errors, output formats, batch ergonomics,
performance on CPU, better diarization defaults, localisation.

**Probably out of scope:** a web UI, cloud transcription backends, real-time
streaming, packaging as an installer, any dependency that needs a compiler.

**Ask first:** GPU support (CUDA/ROCm) — welcome in principle, but it must not
complicate the CPU-only path that most users rely on.

## Licence

Contributions are accepted under the [MIT Licence](LICENSE).
