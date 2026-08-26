<#
.SYNOPSIS
    One-time setup for the Transcribe tool: Python, ffmpeg, Python packages,
    HuggingFace token (asked in a dialog), symlink privilege, and model
    pre-download.

.DESCRIPTION
    Run this ONCE, from an ELEVATED PowerShell:

        Right-click PowerShell -> Run as administrator
        cd <this folder>
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
        .\Setup.ps1

    Elevation is REQUIRED for the model download. HuggingFace creates symlinks
    in its cache, which standard user accounts cannot do; without elevation the
    download fails partway through with WinError 1314.

    Developer Mode is supposed to grant that privilege to standard accounts, and
    this script enables it, but it has proven unreliable: it applies only to a
    new logon token, and on managed machines may not apply at all. Elevation is
    the approach that works.

    Running the app itself never needs elevation -- reading cached symlinks
    requires no privilege.

    If not elevated, this script offers to relaunch itself elevated.

.PARAMETER TokenOnly
    Skip everything except the HuggingFace token dialog.

.PARAMETER SkipModels
    Install software but do not pre-download the ~1.6 GB of models.

.PARAMETER Force
    Re-ask for the token even if one is already stored.

.PARAMETER Gpu
    Install the CUDA libraries without prompting. Only useful on a machine with
    an NVIDIA GPU and a working driver.

.PARAMETER NoGpu
    Skip the CUDA question entirely and stay on CPU.
#>

[CmdletBinding()]
param(
    [switch]$TokenOnly,
    [switch]$SkipModels,
    [switch]$Force,
    # Skip the "do you have an NVIDIA GPU?" prompt and force the answer.
    [switch]$Gpu,
    [switch]$NoGpu
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

# ------------------------------------------------------- elevation gate ---
# Model downloads create symlinks in the HuggingFace cache, which standard
# accounts cannot do. Developer Mode is meant to grant that privilege but has
# proven unreliable in practice (it only applies to a new logon token, and on
# managed machines may not apply at all). Elevation is the approach that works,
# so make it hard to start without it by accident.
if (-not $IsAdmin -and -not $TokenOnly) {
    Say ""
    Bad "This window is NOT running as administrator."
    Warn "Model downloads will fail with WinError 1314 without elevation."
    Warn "Developer Mode alone is not a reliable substitute."
    Say ""
    $answer = Read-Host "Relaunch elevated now? [Y] yes  [n] continue anyway  [q] quit"
    if ($answer -match '^(q|quit)$') {
        Say "aborted." "DarkGray"
        try { Stop-Transcript | Out-Null } catch {}
        exit 1
    }
    if ($answer -notmatch '^(n|no)$') {
        $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", "`"$PSCommandPath`"")
        foreach ($kv in $PSBoundParameters.GetEnumerator()) {
            if ($kv.Value -is [switch] -and $kv.Value.IsPresent) {
                $argList += "-$($kv.Key)"
            }
        }
        try {
            Start-Process -FilePath "powershell.exe" -Verb RunAs `
                          -ArgumentList $argList -WorkingDirectory $Root
            Ok "elevated window launched -- continue there; closing this one."
            try { Stop-Transcript | Out-Null } catch {}
            exit 0
        } catch {
            Bad "could not elevate: $($_.Exception.Message)"
            Warn "Right-click PowerShell -> Run as administrator, then re-run."
            try { Stop-Transcript | Out-Null } catch {}
            exit 1
        }
    }
    Warn "continuing WITHOUT elevation -- expect model downloads to fail"
}

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

    # Optional: drag and drop. The app runs fine without it, so a failure here
    # must not fail setup.
    Say "installing tkinterdnd2 (optional -- drag and drop) ..." "DarkGray"
    $r = Invoke-Native "python" @("-m","pip","install","--upgrade","tkinterdnd2") -Stream
    if ($r.Code -eq 0) { Ok "tkinterdnd2 (drag and drop enabled)" }
    else { Warn "tkinterdnd2 failed (exit $($r.Code)) -- the app works, without drag and drop" }

    Step "Installed versions"
    $lst = (Invoke-Native "python" @("-m","pip","list","--format=freeze")).Text
    foreach ($p in @("torch","whisper-ctranslate2","faster-whisper","ctranslate2",
                     "pyannote.audio","huggingface-hub","tkinterdnd2")) {
        $rx = [regex]::Escape($p).Replace('\.','[._-]')
        $m = [regex]::Match($lst, "(?im)^$rx==(\S+)")
        if ($m.Success) { Ok "$p $($m.Groups[1].Value)" } else { Warn "$p missing" }
    }
}

# ===================================================================== gpu ===
function Get-NvidiaGpu {
    <# Returns the GPU name if an NVIDIA card is present, else $null. #>
    if (Have "nvidia-smi") {
        $r = Invoke-Native "nvidia-smi" @("--query-gpu=name","--format=csv,noheader")
        if ($r.Code -eq 0 -and $r.Text.Trim()) {
            return ($r.Text.Trim() -split "`r?`n")[0]
        }
    }
    # nvidia-smi missing does not prove there is no card -- the driver may not
    # be installed yet. Fall back to the device list.
    try {
        $vc = Get-CimInstance Win32_VideoController -ErrorAction Stop |
              Where-Object { $_.Name -match 'NVIDIA|GeForce|RTX|GTX|Quadro|Tesla' }
        if ($vc) { return ($vc | Select-Object -First 1).Name }
    } catch { }
    return $null
}

function Get-Ct2Version {
    if (-not (Have "python")) { return $null }
    $r = Invoke-Native "python" @("-c","import ctranslate2,sys;sys.stdout.write(ctranslate2.__version__)")
    if ($r.Code -eq 0 -and $r.Text.Trim() -match '^\d+\.\d+') { return $r.Text.Trim() }
    return $null
}

function Setup-Gpu {
    Step "GPU (CUDA) support"

    $gpuName = Get-NvidiaGpu
    if ($gpuName) { Ok "NVIDIA device detected: $gpuName" }
    else { Warn "no NVIDIA device detected on this machine" }

    if ($NoGpu) { Warn "-NoGpu set -- skipping"; return $false }

    $want = $false
    if ($Gpu) {
        $want = $true
        Ok "-Gpu set -- installing CUDA libraries"
    } else {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        $msg = if ($gpuName) {
@"
Detected: $gpuName

Install CUDA acceleration for transcription?

This downloads roughly 1-2 GB:
  - nvidia-cublas-cu12, nvidia-cudnn-cu12  (CTranslate2 needs these)
  - the CUDA build of PyTorch, replacing the CPU-only one

It does NOT install the NVIDIA driver. If transcription still shows
CPU only afterwards, install the latest driver from nvidia.com and
run Setup.ps1 again.

Choose No to stay on CPU. Everything works on CPU, just slower.
"@
        } else {
@"
No NVIDIA GPU was detected on this machine.

Do you have an NVIDIA card (RTX / GTX / Quadro / Tesla) that this
check may have missed - for example because the driver is not
installed yet?

Choose Yes only if you are sure. It downloads roughly 1-2 GB and is
useless without NVIDIA hardware.

AMD and Intel GPUs, and Ryzen AI NPUs, cannot be used: CTranslate2
implements CPU and CUDA backends only.
"@
        }
        $icon = if ($gpuName) { [Windows.Forms.MessageBoxIcon]::Question }
                else { [Windows.Forms.MessageBoxIcon]::Warning }
        $default = if ($gpuName) { [Windows.Forms.MessageBoxDefaultButton]::Button1 }
                   else { [Windows.Forms.MessageBoxDefaultButton]::Button2 }
        $ans = [Windows.Forms.MessageBox]::Show(
            $msg, "CUDA GPU support",
            [Windows.Forms.MessageBoxButtons]::YesNo, $icon, $default)
        $want = ($ans -eq [Windows.Forms.DialogResult]::Yes)
    }

    if (-not $want) { Say "  staying on CPU" "DarkGray"; return $false }

    # CTranslate2 >= 4.5 needs cuDNN 9 + CUDA >= 12.3; 4.0-4.4 needs cuDNN 8.
    $ct2 = Get-Ct2Version
    $cudnn = "nvidia-cudnn-cu12==9.*"
    if ($ct2) {
        Ok "ctranslate2 $ct2"
        $parts = $ct2.Split('.')
        $maj = [int]$parts[0]; $min = [int]$parts[1]
        if ($maj -lt 4) {
            Warn "ctranslate2 $ct2 expects CUDA 11 + cuDNN 8; installing the cu11 stack"
            $cudnn = "nvidia-cudnn-cu11==8.*"
        } elseif ($maj -eq 4 -and $min -lt 5) {
            Warn "ctranslate2 $ct2 expects cuDNN 8"
            $cudnn = "nvidia-cudnn-cu12==8.*"
        }
    } else {
        Warn "could not read the ctranslate2 version; assuming cuDNN 9"
    }

    $cublas = if ($cudnn -like "*cu11*") { "nvidia-cublas-cu11" } else { "nvidia-cublas-cu12" }

    Say "installing $cublas and $cudnn ..." "DarkGray"
    $r = Invoke-Native "python" @("-m","pip","install","--upgrade",$cublas,$cudnn) -Stream
    if ($r.Code -ne 0) { Bad "CUDA library install failed (exit $($r.Code))"; return $false }
    Ok "CUDA libraries installed"

    # The CPU-only torch installed earlier cannot use the GPU. The default
    # PyPI wheel on Windows is the CUDA build, so drop the CPU index.
    Say "replacing CPU-only torch with the CUDA build (large download)..." "DarkGray"
    $r = Invoke-Native "python" @("-m","pip","install","--upgrade",
                                  "--force-reinstall","torch") -Stream
    if ($r.Code -ne 0) { Warn "torch reinstall returned $($r.Code); CTranslate2 may still work" }

    # pip drops the DLLs under site-packages\nvidia\*\bin, which is not on the
    # loader path. Without this, CTranslate2 reports zero CUDA devices.
    $r = Invoke-Native "python" @("-c","import site,sys;sys.stdout.write(site.getsitepackages()[-1])")
    $sp = $r.Text.Trim()
    if ($sp -and (Test-Path $sp)) {
        $dirs = @(Get-ChildItem -Path (Join-Path $sp "nvidia") -Recurse -Directory `
                    -Filter "bin" -ErrorAction SilentlyContinue |
                  Select-Object -ExpandProperty FullName)
        if ($dirs) {
            $userPath = [Environment]::GetEnvironmentVariable('Path','User')
            $addedAny = $false
            foreach ($d in $dirs) {
                if ($userPath -notlike "*$d*") {
                    $userPath = "$userPath;$d"
                    $addedAny = $true
                    Ok "PATH += $d"
                }
            }
            if ($addedAny) {
                [Environment]::SetEnvironmentVariable('Path', $userPath, 'User')
                Warn "PATH updated -- reopen your terminal and the app"
            } else { Ok "CUDA DLL directories already on PATH" }
            $env:Path = "$env:Path;" + ($dirs -join ';')
        } else { Warn "no nvidia\*\bin directories found under $sp" }
    }

    Step "GPU verification"
    $chk = @"
import sys
try:
    import ctranslate2 as c
    n = c.get_cuda_device_count()
    print('ctranslate2 sees', n, 'CUDA device(s)')
except Exception as e:
    print('ctranslate2 CUDA check failed:', repr(e)); n = 0
try:
    import torch
    print('torch CUDA available:', torch.cuda.is_available())
    if torch.cuda.is_available():
        print('torch device:', torch.cuda.get_device_name(0))
except Exception as e:
    print('torch check failed:', repr(e))
sys.exit(0 if n > 0 else 3)
"@
    $tmp = Join-Path $env:TEMP "verify_cuda.py"
    Set-Content -LiteralPath $tmp -Value $chk -Encoding ASCII
    $r = Invoke-Native "python" @($tmp) -Stream
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    if ($r.Code -eq 0) {
        Ok "CUDA is usable -- pick it in the Engine dropdown (press Re-detect if already open)"
        return $true
    }
    Warn "CUDA libraries are installed but no device is visible yet."
    Warn "Most likely the NVIDIA driver is missing or out of date:"
    Warn "  https://www.nvidia.com/Download/index.aspx"
    Warn "Reopen the terminal after installing it and re-run: .\Setup.ps1 -Gpu"
    return $false
}

$gpuOk = Setup-Gpu

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

if ($devOn) {
    Ok "Developer Mode is enabled in the registry"
    Warn "note: this alone has proven unreliable for symlinks -- the real"
    Warn "guarantee is running this script elevated, which you are doing."
}
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
Say "  CUDA GPU        : $(if ($gpuOk) {'ready'} else {'not enabled (CPU only)'})"
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
