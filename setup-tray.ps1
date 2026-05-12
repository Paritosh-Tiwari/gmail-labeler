# QuickLabel -- enable the system-tray icon
#
# Right-click this file -> "Run with PowerShell". Idempotent: re-run
# any time to update.
#
# Registers a Task Scheduler entry "QuickLabel Tray" that launches the
# tray app at every login. The tray polls /healthz, shows green/red
# status in your notification area, and gives you a menu to open the
# queue / settings / restart the server.
#
# To remove: right-click uninstall-tray.ps1 -> Run with PowerShell.

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }
$TaskName = "QuickLabel Tray"

if (-not (Test-Path (Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"))) {
    Write-Host "QuickLabel doesn't seem installed. Run setup.ps1 first." -ForegroundColor Yellow
    exit 1
}

# Verify pystray is in the venv (otherwise tray will crash on launch)
$venvPy = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$check = & $venvPy -c "import pystray, PIL; print('ok')" 2>&1
if ($check -notmatch "^ok") {
    Write-Host "Tray dependencies not in venv. Installing..." -ForegroundColor Yellow
    & $venvPy -m pip install pystray pillow --quiet
}

# Remove any existing task so re-run gives a fresh one
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "-m quicklabel tray" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
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
    -Description "QuickLabel system-tray status icon" | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Tray enabled." -ForegroundColor Green
Write-Host "  Task scheduler entry: '$TaskName' (runs as $env:USERNAME at logon)"
Write-Host "  The tray icon should appear in your notification area shortly."
Write-Host ""
Write-Host "  To remove the tray:  .\uninstall-tray.ps1"
