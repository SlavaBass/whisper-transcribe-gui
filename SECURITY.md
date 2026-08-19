# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use GitHub's private reporting: the **Security** tab → *Report a vulnerability*.
If that is unavailable, open a normal issue titled "Security contact request"
with no technical detail and a maintainer will arrange a private channel.

Expect an acknowledgement within a few days. This is a hobby-scale project
maintained in spare time; there is no paid on-call rotation and no bounty.

## Scope

In scope:

- Command injection through filenames or paths
- Token leakage into logs, the GUI, process arguments or crash output
- Arbitrary code execution triggered by opening a media file
- Path traversal when writing output

Out of scope:

- Vulnerabilities in upstream dependencies (report to PyTorch, pyannote,
  faster-whisper, CTranslate2 or ffmpeg directly)
- Anything requiring an attacker who already has code execution on the machine
- The deliberate `weights_only=False` behaviour described below

## Known, deliberate security trade-offs

### `torch.load(weights_only=False)`

The app patches `torch.load` to restore pre-PyTorch-2.6 behaviour, because
pyannote checkpoints cannot otherwise be loaded. This permits **arbitrary code
execution during unpickling**.

The risk is bounded because only the official
`pyannote/speaker-diarization-community-1` weights are loaded, fetched over HTTPS
from huggingface.co with integrity-checked blobs. If you point the tool at
checkpoints from an untrusted source, that protection disappears. Do not.

Rationale and code: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#the-torch-shim).

### Developer Mode

Setup enables Windows Developer Mode to grant `SeCreateSymbolicLinkPrivilege`,
without which HuggingFace model downloads fail. This lets a standard user create
symlinks — a mild privilege that matters mainly in multi-user or server contexts
where an unprivileged user could plant links to mislead a privileged process. On
a single-user workstation the practical risk is low.

It can be switched off once models are cached; reading existing symlinks needs no
privilege. Setup tells you this.

### Execution policy bypass

Documentation recommends `Set-ExecutionPolicy -Scope Process -ExecutionPolicy
Bypass` — process-scoped and gone when the window closes. We deliberately do not
recommend `CurrentUser` or `LocalMachine` scope.

## Token handling

- `HF_TOKEN` lives in the user environment, not in any file in the repository.
- It is redacted to `hf_****` before any command line is logged or displayed.
- `.gitignore` excludes `.env`, `*.token` and `hf_token*`.
- It is transmitted only to huggingface.co, over HTTPS.
- The Setup dialog masks input by default.

If you believe a token has leaked, revoke it at
`huggingface.co/settings/tokens`.

## Privacy

Audio and transcripts never leave the machine. The only outbound traffic is the
one-time model download. After that the tool runs fully offline.

Note that **logs may contain transcript text**, since whisper runs with
`--verbose True`. Redact log files before attaching them to a public issue.
