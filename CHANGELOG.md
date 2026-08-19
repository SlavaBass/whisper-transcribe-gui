# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Engine selector** — a session-wide dropdown listing only the compute devices
  actually present on the machine. Detection runs in a child process at startup
  and is cached in `.transcribe_gui.json` behind an environment fingerprint
  (machine, CPU count, Python and `ctranslate2`/`torch`/`faster-whisper`
  versions), so subsequent launches are instant. A **Re-detect** button forces a
  refresh, and the cache invalidates automatically when the fingerprint changes.
  The best compute type for the chosen device is selected automatically
  (`int8` on CPU, `float16` on CUDA, falling back through what CTranslate2
  reports as supported). The selector is locked while a batch runs, since
  switching device between files would make results and timings inconsistent.

  Note that CTranslate2 supports **CPU and CUDA only** — there is no Metal,
  DirectML, Vulkan or NPU backend — so on machines without an NVIDIA GPU the
  list correctly contains a single CPU entry. AMD ROCm builds of CTranslate2
  report themselves as `cuda`.
- **Optional CUDA setup in `Setup.ps1`.** Detects an NVIDIA GPU via `nvidia-smi`
  with a `Win32_VideoController` fallback (so a card with no driver yet is still
  found), then asks whether to install GPU support. Installs `nvidia-cublas-*`
  and a `nvidia-cudnn-*` version matched to the installed `ctranslate2`, replaces
  CPU-only PyTorch with the CUDA build, adds the `site-packages\nvidia\*\bin`
  directories to PATH, and verifies with `ctranslate2.get_cuda_device_count()`.
  New `-Gpu` and `-NoGpu` switches skip the prompt. The NVIDIA driver itself
  cannot be installed this way; Setup says so and links to the download.

  **Untested.** Developed without access to NVIDIA hardware. Reports from anyone
  with a CUDA GPU are welcome.

## [1.0.0] - 2026-08-19

First public release.

### Added

- `Transcribe.pyw` — tkinter desktop app, no third-party GUI toolkit
  - Language dropdown (Russian and English first), model dropdown, thread count
  - Speaker labelling with a fixed count or automatic detection when left blank
  - Batch queue with a grid showing per-file language, model, speakers, status
    and detail; files can be added while a batch runs
  - Per-job settings snapshot taken at add time, so changing the controls
    mid-batch cannot alter pending work
  - Duplicate detection across the whole queue using normalised absolute paths
  - Existing-transcript policy: ask, skip, or reprocess; asked once per batch
    with a *Decide one by one* option, and re-checked immediately before each job
  - Progress bar with ETA derived from segment timestamps
  - Console pane plus per-session and per-job log files
  - Automatic 16 kHz mono conversion with cover-art stripping, cleaned up after
- `Setup.ps1` — one-time provisioning
  - Installs Python and ffmpeg via winget when missing
  - Installs CPU-only PyTorch to avoid a ~2.5 GB CUDA download
  - HuggingFace token dialog with masked input, *How do I get one?* guidance and
    a live *Test token* button that validates both the token and gated-repo access
  - Enables Developer Mode, probes symlink capability, pre-downloads models
  - Verifies every import and prints a summary
- `Transcribe.bat` — double-click launcher
- Documentation: README, troubleshooting guide, architecture notes, contributing
  guide, security policy, code of conduct

### Fixed

- PyTorch ≥ 2.6 `weights_only` default breaking pyannote checkpoint loading,
  worked around by a generated shim
- Windows `WinError 1314` symlink failures during model download, now detected
  before a download starts rather than partway through
- Speaker count silently defaulting to 2 when not specified; true auto-detect is
  now available by patching `num_speakers=None`
- PowerShell treating native stderr as a terminating error under
  `$ErrorActionPreference = "Stop"`, which hid the real cause of failures
- Filenames containing spaces being split into multiple arguments
- Thread count defaulting above the physical core count, costing throughput
- Embedded MJPEG cover-art streams breaking audio decoding

### Known issues

- Batched inference (`--batched`) collapses speaker attribution into ~30-second
  blocks and can produce repetition loops, so it is intentionally not exposed
- Output is written only when a file finishes; cancelling produces nothing
- Windows only

[Unreleased]: https://github.com/OWNER/whisper-transcribe-gui/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/SlavaBass/whisper-transcribe-gui/releases/tag/v1.0.0
