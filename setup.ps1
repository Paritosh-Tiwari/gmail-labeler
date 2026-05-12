# QuickLabel -- Windows installer
#
# Right-click this file -> "Run with PowerShell".
#
# What it does:
#   1. Verifies you have Python 3.11+ (offers to install via winget)
#   2. Creates a project-local .venv and installs Python deps
#   3. Verifies you have Ollama (offers to install via winget)
#   4. Asks which local LLM you want and pulls it
#   5. Registers a Task Scheduler entry "QuickLabel" so the server
#      auto-starts on every login (no terminal window)
#   6. Starts the server now and opens the browser to it
#
# Idempotent -- re-run any time. Skips steps that are already done.

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }
$TaskName = "QuickLabel"

# ----------------------------- helpers -----------------------------

function Write-Step {
    param([string]$Number, [string]$Message)
    Write-Host ""
    Write-Host "[$Number] $Message" -ForegroundColor Cyan
}

function Write-Ok    { param($m) Write-Host "      $m" -ForegroundColor Green }
function Write-Info  { param($m) Write-Host "      $m" -ForegroundColor Gray }
function Write-Warn  { param($m) Write-Host "      $m" -ForegroundColor Yellow }

function Find-Python {
    # Try py launcher first (most reliable on Windows), then python on PATH.
    $candidates = @("py -3.12", "py -3.11", "py -3", "python")
    foreach ($cmd in $candidates) {
        $parts = $cmd.Split(" ")
        $exe = $parts[0]
        $arg = if ($parts.Count -gt 1) { $parts[1] } else { $null }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            if ($arg) {
                $verLine = & $exe $arg --version 2>&1
            } else {
                $verLine = & $exe --version 2>&1
            }
            if ($verLine -match "Python (\d+)\.(\d+)\.(\d+)") {
                $major = [int]$matches[1]; $minor = [int]$matches[2]
                if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                    if ($arg) {
                        # Resolve `py -3.x` to its actual python.exe path
                        $resolved = & $exe $arg -c "import sys; print(sys.executable)" 2>&1
                    } else {
                        $resolved = & $exe -c "import sys; print(sys.executable)" 2>&1
                    }
                    return [string]$resolved.Trim()
                }
            }
        } catch { continue }
    }
    return $null
}

function Test-Ollama {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) { return $false }
    try {
        $null = & ollama --version 2>&1
        return $true
    } catch { return $false }
}

function Confirm-Yes {
    param([string]$Prompt)
    $resp = Read-Host "$Prompt [Y/n]"
    return ($resp -eq "" -or $resp -eq "y" -or $resp -eq "Y")
}

function Select-Model {
    Write-Host ""
    Write-Host "      Pick a local LLM. (Pull = multi-GB download via Ollama.)" -ForegroundColor Gray
    Write-Host ""
    Write-Host "        [1] gpt-oss:20b           Best quality. ~13 GB. Needs ~16 GB VRAM."
    Write-Host "                                    Recommended if you have a recent GPU."
    Write-Host "        [2] qwen2.5:7b-instruct   Good quality. ~5 GB. Needs ~6 GB VRAM."
    Write-Host "        [3] qwen2.5:3b-instruct   Decent + fastest. ~2 GB. Runs on CPU OK."
    Write-Host "        [4] Skip -- I'll set this later via /settings"
    Write-Host ""
    while ($true) {
        $choice = Read-Host "      Choice [1-4]"
        switch ($choice) {
            "1" { return "gpt-oss:20b" }
            "2" { return "qwen2.5:7b-instruct" }
            "3" { return "qwen2.5:3b-instruct" }
            "4" { return $null }
            default { Write-Warn "Please enter 1, 2, 3, or 4." }
        }
    }
}

function Save-ModelChoice {
    param([string]$ProjectRoot, [string]$Model)
    $py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $code = @"
from quicklabel.settings import Settings, load_settings, save_settings
s = load_settings()
s.llm_model = '$Model'
save_settings(s)
print(f'Saved llm_model={s.llm_model} to settings.json')
"@
    & $py -c $code
}

function Register-AutoStart {
    param([string]$ProjectRoot)

    # Remove any existing task so re-run gives a fresh one
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    $pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path $pythonw)) {
        throw "pythonw.exe not found at $pythonw -- venv setup must have failed."
    }

    $action = New-ScheduledTaskAction `
        -Execute $pythonw `
        -Argument "-m quicklabel serve" `
        -WorkingDirectory $ProjectRoot

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    # Don't auto-stop the server after N hours -- it's a long-running service
    $settings.ExecutionTimeLimit = "PT0S"

    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "QuickLabel local Gmail labeling server" | Out-Null
}

# ----------------------------- main -----------------------------

Write-Host ""
Write-Host "QuickLabel installer (Windows)" -ForegroundColor Cyan
Write-Host "Project: $ProjectRoot"

Write-Step "1/8" "Checking project layout"
if (-not (Test-Path (Join-Path $ProjectRoot "src\quicklabel\server.py"))) {
    throw "src\quicklabel\server.py not found. setup.ps1 must live in the QuickLabel project root."
}
Write-Ok "Looks like a QuickLabel checkout."

Write-Step "2/8" "Looking for Python 3.11+"
$python = Find-Python
if (-not $python) {
    Write-Warn "No suitable Python found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Confirm-Yes "      Install Python 3.12 via winget?") {
            winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
            Write-Info "Re-detecting Python..."
            $python = Find-Python
        }
    } else {
        Write-Warn "winget not available on this machine."
    }
    if (-not $python) {
        throw "Please install Python 3.11 or newer from https://www.python.org/downloads/ and re-run."
    }
}
Write-Ok "Using $python"

Write-Step "3/8" "Creating .venv (project-local)"
$venvPy = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $python -m venv (Join-Path $ProjectRoot ".venv")
    Write-Ok "Created $ProjectRoot\.venv"
} else {
    Write-Ok "Already exists."
}

Write-Step "4/8" "Installing Python dependencies (pip install -e .)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -e $ProjectRoot --quiet
Write-Ok "Done."

Write-Step "5/8" "Looking for Ollama"
if (-not (Test-Ollama)) {
    Write-Warn "Ollama not found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Confirm-Yes "      Install Ollama via winget?") {
            winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
        }
    } else {
        Write-Warn "winget not available on this machine."
    }
    if (-not (Test-Ollama)) {
        throw "Please install Ollama from https://ollama.com/download and re-run."
    }
}
Write-Ok "Ollama is installed. (Its background service auto-starts on Windows.)"

Write-Step "6/8" "Local LLM model"
$model = Select-Model
if ($model) {
    Write-Info "Pulling $model via Ollama (this may take a while)..."
    & ollama pull $model
    Save-ModelChoice -ProjectRoot $ProjectRoot -Model $model
    Write-Ok "Model ready."
} else {
    Write-Info "Skipped. Set llm_model later at http://127.0.0.1:8765/settings"
}

Write-Step "7/8" "Registering auto-start at logon (Task Scheduler)"
Register-AutoStart -ProjectRoot $ProjectRoot
Write-Ok "Registered task '$TaskName' (runs as $env:USERNAME at logon)."

Write-Step "8/8" "Starting the server"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

# Quick health check before opening the browser
$healthUrl = "http://127.0.0.1:8765/healthz"
$ok = $false
for ($i = 0; $i -lt 5; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $ok = $true; break }
    } catch { Start-Sleep -Seconds 2 }
}

if ($ok) {
    Write-Ok "Server is up. Opening http://127.0.0.1:8765"
    Start-Process "http://127.0.0.1:8765"
} else {
    Write-Warn "Server didn't respond within 10s. Check $ProjectRoot\data\quicklabel.log"
    Write-Warn "Or run start.ps1 manually."
}

Write-Host ""
Write-Host "All set!" -ForegroundColor Green
Write-Host ""
Write-Host "  Landing page : http://127.0.0.1:8765"
Write-Host "  Settings     : http://127.0.0.1:8765/settings"
Write-Host "  Logs         : $ProjectRoot\data\quicklabel.log"
Write-Host ""
Write-Host "  Lifecycle    : .\start.ps1   .\stop.ps1   .\restart.ps1"
Write-Host "  Uninstall    : .\uninstall.ps1   (removes auto-start; keeps your data)"
Write-Host ""
Write-Host "Optional: enable the system-tray status icon"
Write-Host "  .\setup-tray.ps1   (green/red dot in notification area + menu)"
Write-Host ""
Write-Host "Next: drag the 'Label this' bookmark from the landing page into your bookmark bar."
