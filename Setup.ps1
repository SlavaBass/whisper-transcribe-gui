<#
.SYNOPSIS
    One-time setup for the Transcribe tool: Python, ffmpeg, Python packages,
    HuggingFace token (asked in a dialog), symlink privilege, and model
    pre-download.

.DESCRIPTION
    Run this ONCE, ideally from an elevated PowerShell:

        Right-click PowerShell -> Run as administrator
        cd D:\Calls
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
        .\Setup.ps1

    Elevation is needed for two things only: enabling Developer Mode (so
    HuggingFace can create cache symlinks) and downloading models before that
    privilege is active in your logon token. Everyday use of Transcribe needs
    no elevation at all.

.PARAMETER TokenOnly
    Skip everything except the HuggingFace token dialog.

.PARAMETER SkipModels
    Install software but do not pre-download the ~1.6 GB of models.

.PARAMETER Force
    Re-ask for the token even if one is already stored.
#>

[CmdletBinding()]
param(
    [switch]$TokenOnly,
    [switch]$SkipModels,
    [switch]$Force
)

# Native stderr must never abort the script; exit codes are checked instead.
$ErrorActionPreference = "Continue"

$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }
Set-Location $Root

$LogDir = Join-Path $Root "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogFile = Join-Path $LogDir ("setup_{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
Start-Transcript -Path $LogFile -Force | Out-Null

function Say  { param($m,$c="Gray") Write-Host $m -ForegroundColor $c }
function Step { param($m) Say "`n=== $m" "Cyan" }
function Ok   { param($m) Say "[ok]   $m" "Green" }
function Warn { param($m) Say "[warn] $m" "Yellow" }
function Bad  { param($m) Say "[FAIL] $m" "Red" }
function Have { param($n) [bool](Get-Command $n -ErrorAction SilentlyContinue) }

function Refresh-Path {
    $env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));" +
                "$([Environment]::GetEnvironmentVariable('Path','User'));" +
                "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
}

function Invoke-Native {
    param([string]$Exe,[string[]]$Arguments,[switch]$Stream)
    if ($Stream) {
        & $Exe @Arguments
        return @{ Code = $LASTEXITCODE; Text = "" }
    }
    $t = (& $Exe @Arguments 2>&1 | Out-String)
    return @{ Code = $LASTEXITCODE; Text = $t }
}

$IsAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Say "==================================================================" "DarkGray"
Say " Transcribe setup   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" "White"
Say " elevated: $IsAdmin" "DarkGray"
Say " log: $LogFile" "DarkGray"
Say "==================================================================" "DarkGray"

Refresh-Path

# =========================================================== token dialog ===
function Show-TokenDialog {
    param([string]$Current = "")

    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    Add-Type -AssemblyName System.Drawing | Out-Null

    $f = New-Object Windows.Forms.Form
    $f.Text = "HuggingFace access token"
    $f.Size = New-Object Drawing.Size(560, 320)
    $f.StartPosition = "CenterScreen"
    $f.FormBorderStyle = "FixedDialog"
    $f.MaximizeBox = $false
    $f.MinimizeBox = $false
    $f.Topmost = $true

    $lbl = New-Object Windows.Forms.Label
    $lbl.Location = New-Object Drawing.Point(16, 15)
    $lbl.Size = New-Object Drawing.Size(510, 96)
    $lbl.Text = @"
Speaker labelling downloads the pyannote diarization model from
huggingface.co, which is gated and requires a free account.

1. Sign in / register at huggingface.co
2. Open pyannote/speaker-diarization-community-1 and ACCEPT the conditions
   (use the same account the token belongs to)
3. Settings > Access Tokens > create a token with Read access
4. Paste it below. It is stored in your user environment as HF_TOKEN.

Leave blank to skip - transcription still works, without speaker labels.
"@
    $f.Controls.Add($lbl)

    $link = New-Object Windows.Forms.LinkLabel
    $link.Location = New-Object Drawing.Point(16, 118)
    $link.Size = New-Object Drawing.Size(510, 20)
    $link.Text = "Open the model page and the token page in a browser"
    $link.add_LinkClicked({
        Start-Process "https://huggingface.co/pyannote/speaker-diarization-community-1"
        Start-Process "https://huggingface.co/settings/tokens"
    })
    $f.Controls.Add($link)

    $tb = New-Object Windows.Forms.TextBox
    $tb.Location = New-Object Drawing.Point(16, 150)
    $tb.Size = New-Object Drawing.Size(510, 24)
    $tb.Text = $Current
    $tb.UseSystemPasswordChar = $true
    $f.Controls.Add($tb)

    $cb = New-Object Windows.Forms.CheckBox
    $cb.Location = New-Object Drawing.Point(16, 180)
    $cb.Size = New-Object Drawing.Size(200, 22)
    $cb.Text = "Show token"
    $cb.add_CheckedChanged({ $tb.UseSystemPasswordChar = -not $cb.Checked })
    $f.Controls.Add($cb)

    $status = New-Object Windows.Forms.Label
    $status.Location = New-Object Drawing.Point(16, 208)
    $status.Size = New-Object Drawing.Size(510, 22)
    $status.ForeColor = [Drawing.Color]::DimGray
    $f.Controls.Add($status)

    $script:TokenResult = $null

    $help = New-Object Windows.Forms.Button
    $help.Location = New-Object Drawing.Point(16, 238)
    $help.Size = New-Object Drawing.Size(150, 30)
    $help.Text = "How do I get one?"
    $help.add_Click({
        $hint = @"
STEP 1 - Create a free account
    Go to huggingface.co and register (or sign in). No payment, no card.

STEP 2 - Accept the model conditions  <-- most people miss this
    Open:
      huggingface.co/pyannote/speaker-diarization-community-1
    Read the terms and click Accept / Agree.
    You MUST be signed in with the same account the token will belong to.
    A valid token whose owner never accepted the terms fails with HTTP 403,
    and the error message does not explain why.

STEP 3 - Create the token
    huggingface.co/settings/tokens  ->  Create new token
    A classic token with the "Read" role is enough.
    If you create a FINE-GRAINED token instead, you must also tick
      "Read access to contents of all public gated repos you can access"
    or the download fails with HTTP 401.

STEP 4 - Copy and paste it here
    It looks like:  hf_AbCdEf0123456789...
    Then press "Test token". That makes two live checks:
      - is the token itself valid
      - can this account actually reach the gated pyannote model
    Both must pass for speaker labelling to work.

WHERE IT IS STORED
    In your Windows user environment as HF_TOKEN. Nothing is sent anywhere
    except huggingface.co. Your audio never leaves this machine.

DO YOU NEED IT AT ALL?
    Only for speaker labels (SPEAKER_00 / 01 / 02). Transcription itself
    works without a token - press Skip and you still get full text.
"@
        [Windows.Forms.MessageBox]::Show($hint, "Getting a HuggingFace token",
            [Windows.Forms.MessageBoxButtons]::OK,
            [Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    })
    $f.Controls.Add($help)

    $test = New-Object Windows.Forms.Button
    $test.Location = New-Object Drawing.Point(176, 238)
    $test.Size = New-Object Drawing.Size(110, 30)
    $test.Text = "Test token"
    $test.add_Click({
        $t = $tb.Text.Trim()
        if (-not $t) { $status.Text = "Empty."; return }
        $status.ForeColor = [Drawing.Color]::DimGray
        $status.Text = "Checking..."
        $f.Refresh()
        $r = Test-HfToken $t
        if ($r.Ok) {
            $status.ForeColor = [Drawing.Color]::Green
            $status.Text = "OK - user '$($r.User)', gated model accessible."
        } else {
            $status.ForeColor = [Drawing.Color]::Firebrick
            $status.Text = $r.Message
        }
    })
    $f.Controls.Add($test)

    $save = New-Object Windows.Forms.Button
    $save.Location = New-Object Drawing.Point(300, 238)
    $save.Size = New-Object Drawing.Size(100, 30)
    $save.Text = "Save"
    $save.add_Click({ $script:TokenResult = $tb.Text.Trim(); $f.Close() })
    $f.Controls.Add($save)

    $skip = New-Object Windows.Forms.Button
    $skip.Location = New-Object Drawing.Point(414, 238)
    $skip.Size = New-Object Drawing.Size(110, 30)
    $skip.Text = "Skip"
    $skip.add_Click({ $script:TokenResult = ""; $f.Close() })
    $f.Controls.Add($skip)

    $f.AcceptButton = $save
    [void]$f.ShowDialog()
    return $script:TokenResult
}

function Test-HfToken {
    param([string]$Token)
    $hdr = @{ Authorization = "Bearer $Token" }
    try {
        $me = Invoke-RestMethod -Uri "https://huggingface.co/api/whoami-v2" `
                                -Headers $hdr -TimeoutSec 25
        $user = $me.name
    } catch {
        return @{ Ok = $false; Message = "Token rejected by HuggingFace (invalid or revoked)." }
    }
    try {
        Invoke-RestMethod -Headers $hdr -TimeoutSec 25 `
            -Uri "https://huggingface.co/api/models/pyannote/speaker-diarization-community-1" | Out-Null
    } catch {
        return @{ Ok = $false; User = $user
                  Message = "Token valid ($user) but the gated model is NOT accessible - accept its conditions." }
    }
    return @{ Ok = $true; User = $user; Message = "ok" }
}

function Setup-Token {
    Step "HuggingFace token"
    $existing = [Environment]::GetEnvironmentVariable("HF_TOKEN", "User")
    if (-not $existing) { $existing = $env:HF_TOKEN }

    if ($existing -and -not $Force) {
        $r = Test-HfToken $existing
        if ($r.Ok) {
            Ok "existing HF_TOKEN works (user '$($r.User)')"
            $env:HF_TOKEN = $existing
            return $true
        }
        Warn "stored HF_TOKEN failed: $($r.Message)"
    }

    Say "Opening the token dialog..." "DarkGray"
    # No ternary operator on PowerShell 5.1.
    $prefill = ""
    if ($existing) { $prefill = $existing }
    $tok = Show-TokenDialog -Current $prefill
    if ([string]::IsNullOrWhiteSpace($tok)) {
        Warn "no token provided -- speaker labelling will be unavailable"
        return $false
    }
    $r = Test-HfToken $tok
    if (-not $r.Ok) { Warn $r.Message }
    [Environment]::SetEnvironmentVariable("HF_TOKEN", $tok, "User")
    $env:HF_TOKEN = $tok
    Ok "HF_TOKEN saved to your user environment"
    Warn "already-open apps and terminals will not see it until restarted"
    return $r.Ok
}

if ($TokenOnly) {
    Setup-Token | Out-Null
    Say "`n--- log: $LogFile" "DarkGray"
    try { Stop-Transcript | Out-Null } catch {}
    exit 0
}

# ================================================================= python ===
Step "Python"
$needPython = $true
if (Have "python") {
    $v = (Invoke-Native "python" @("--version")).Text.Trim()
    if ($v -match 'Python\s+(\d+)\.(\d+)') {
        $maj = [int]$Matches[1]; $min = [int]$Matches[2]
        if ($maj -eq 3 -and $min -ge 9) { Ok "$v"; $needPython = $false }
        else { Warn "$v is too old (need 3.9+)" }
    } else {
        Warn "could not parse: $v (Microsoft Store stub?)"
    }
}
if ($needPython) {
    if (-not (Have "winget")) {
        Bad "winget unavailable. Install Python 3.12 from https://python.org"
        Bad "and tick 'Add python.exe to PATH', then re-run this script."
        Say "`n--- log: $LogFile" "DarkGray"
        try { Stop-Transcript | Out-Null } catch {}
        exit 1
    }
    Say "installing Python 3.12 via winget..." "DarkGray"
    Invoke-Native "winget" @("install","-e","--id","Python.Python.3.12",
                             "--accept-source-agreements","--accept-package-agreements") -Stream | Out-Null
    Refresh-Path
    if (Have "python") { Ok (Invoke-Native "python" @("--version")).Text.Trim() }
    else { Bad "Python still not on PATH -- open a NEW terminal and re-run."; }
}

# ================================================================= ffmpeg ===
Step "ffmpeg"
if (Have "ffmpeg") {
    Ok ((Invoke-Native "ffmpeg" @("-version")).Text -split "`r?`n")[0]
} elseif (Have "winget") {
    Say "installing ffmpeg (shared build) via winget..." "DarkGray"
    Invoke-Native "winget" @("install","-e","--id","Gyan.FFmpeg.Shared",
                             "--accept-source-agreements","--accept-package-agreements") -Stream | Out-Null
    Refresh-Path
    if (Have "ffmpeg") { Ok "ffmpeg installed" }
    else { Warn "ffmpeg not on PATH yet -- reopen the terminal" }
} else {
    Warn "no winget; install ffmpeg manually (optional but recommended)"
}

# =============================================================== packages ===
Step "Python packages"
if (-not (Have "python")) {
    Bad "no python -- cannot install packages"
} else {
    Invoke-Native "python" @("-m","pip","install","--upgrade","pip","--quiet") | Out-Null
    Ok "pip upgraded"

    # CPU-only torch first: pulling the default wheel can drag in ~2.5 GB of
    # CUDA libraries that are useless without an NVIDIA GPU.
    Say "installing torch (CPU build)..." "DarkGray"
    $r = Invoke-Native "python" @("-m","pip","install","torch",
                                  "--index-url","https://download.pytorch.org/whl/cpu") -Stream
    if ($r.Code -ne 0) { Warn "CPU torch install returned $($r.Code); continuing" }

    foreach ($pkg in @("whisper-ctranslate2","pyannote.audio>=4.0","huggingface_hub")) {
        Say "installing $pkg ..." "DarkGray"
        $r = Invoke-Native "python" @("-m","pip","install","--upgrade",$pkg) -Stream
        if ($r.Code -eq 0) { Ok $pkg } else { Bad "$pkg failed (exit $($r.Code))" }
    }

    Step "Installed versions"
    $lst = (Invoke-Native "python" @("-m","pip","list","--format=freeze")).Text
    foreach ($p in @("torch","whisper-ctranslate2","faster-whisper","ctranslate2",
                     "pyannote.audio","huggingface-hub")) {
        $rx = [regex]::Escape($p).Replace('\.','[._-]')
        $m = [regex]::Match($lst, "(?im)^$rx==(\S+)")
        if ($m.Success) { Ok "$p $($m.Groups[1].Value)" } else { Warn "$p missing" }
    }
}

# =================================================================== token ===
$tokenOk = Setup-Token

# ======================================================== symlink privilege ===
Step "Symlink privilege (needed only to download models)"
$devKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
$devOn = $false
try {
    $devOn = ((Get-ItemProperty -Path $devKey -Name AllowDevelopmentWithoutDevLicense `
               -ErrorAction Stop).AllowDevelopmentWithoutDevLicense -eq 1)
} catch { $devOn = $false }

if ($devOn) { Ok "Developer Mode is enabled in the registry" }
elseif ($IsAdmin) {
    try {
        if (-not (Test-Path $devKey)) { New-Item -Path $devKey -Force | Out-Null }
        New-ItemProperty -Path $devKey -Name AllowDevelopmentWithoutDevLicense `
                         -PropertyType DWord -Value 1 -Force | Out-Null
        Ok "Developer Mode enabled"
        Warn "takes effect on your NEXT logon -- sign out and back in later"
        $devOn = $true
    } catch { Warn "could not enable Developer Mode: $($_.Exception.Message)" }
} else {
    Warn "Developer Mode is off and this shell is not elevated."
    Warn "Enable it at: Settings > System > For developers  (then sign out/in)"
}

$hub = Join-Path $env:USERPROFILE ".cache\huggingface\hub"
if (-not (Test-Path $hub)) { New-Item -ItemType Directory -Path $hub -Force | Out-Null }
$t = Join-Path $hub "_probe.tmp"; $l = Join-Path $hub "_probe.lnk.tmp"
Set-Content -LiteralPath $t -Value "x" -Encoding ASCII
Remove-Item -LiteralPath $l -Force -ErrorAction SilentlyContinue
$symlinkOk = $false
try { New-Item -ItemType SymbolicLink -Path $l -Target $t -ErrorAction Stop | Out-Null
      $symlinkOk = $true } catch { }
Remove-Item -LiteralPath $l -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $t -Force -ErrorAction SilentlyContinue
if ($symlinkOk) { Ok "symlinks work in the HuggingFace cache" }
else { Warn "symlinks NOT available in this session (model downloads will fail)" }

# ================================================================== models ===
if ($SkipModels) {
    Step "Models"
    Warn "-SkipModels set; nothing downloaded"
} elseif (-not (Have "python")) {
    Warn "no python -- skipping model download"
} elseif (-not $symlinkOk) {
    Step "Models"
    Bad "cannot download models without symlink privilege."
    Bad "Re-run this script from an ELEVATED PowerShell, or sign out/in first."
} else {
    Step "Models (~1.6 GB, one time)"
    $repos = @("mobiuslabsgmbh/faster-whisper-large-v3-turbo",
               "Systran/faster-whisper-tiny")
    if ($tokenOk) { $repos += "pyannote/speaker-diarization-community-1" }
    else { Warn "no working token -- skipping the pyannote diarization model" }

    foreach ($repo in $repos) {
        Say "downloading $repo ..." "DarkGray"
        $py = "import sys;from huggingface_hub import snapshot_download as d;" +
              "d('$repo');print('done')"
        $r = Invoke-Native "python" @("-c",$py) -Stream
        if ($r.Code -eq 0) { Ok $repo } else { Bad "$repo failed (exit $($r.Code))" }
    }
}

# ================================================================== verify ===
Step "Verification"
if (Have "python") {
    $check = @"
import sys
mods = ['torch','ctranslate2','faster_whisper','pyannote.audio',
        'whisper_ctranslate2','huggingface_hub']
bad = []
for m in mods:
    try:
        __import__(m)
        print('  import ok:', m)
    except Exception as e:
        bad.append(m); print('  IMPORT FAILED:', m, repr(e))
sys.exit(1 if bad else 0)
"@
    $tmp = Join-Path $env:TEMP "verify_transcribe.py"
    Set-Content -LiteralPath $tmp -Value $check -Encoding ASCII
    $r = Invoke-Native "python" @($tmp) -Stream
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    if ($r.Code -eq 0) { Ok "all imports succeeded" } else { Bad "one or more imports failed" }
}

foreach ($f in @("Transcribe.pyw","Transcribe.bat")) {
    if (Test-Path (Join-Path $Root $f)) { Ok "$f present" } else { Bad "$f MISSING" }
}

# ================================================================= summary ===
Step "Summary"
Say "  Python          : $(if (Have 'python') {'yes'} else {'NO'})"
Say "  ffmpeg          : $(if (Have 'ffmpeg') {'yes'} else {'no (optional)'})"
Say "  HF_TOKEN        : $(if ($tokenOk) {'valid'} elseif ($env:HF_TOKEN) {'set but unverified'} else {'not set'})"
Say "  Developer Mode  : $(if ($devOn) {'enabled'} else {'off'})"
Say "  Symlinks now    : $(if ($symlinkOk) {'yes'} else {'no'})"
Say ""
Say "Next: double-click Transcribe.bat" "Green"
if (-not $devOn -or -not $symlinkOk) {
    Warn "Sign out and back in once so Developer Mode applies; after that no"
    Warn "elevation is ever needed, including for new model downloads."
}
Say "`n--- log: $LogFile" "DarkGray"
try { Stop-Transcript | Out-Null } catch {}
