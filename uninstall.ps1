# Remove the QuickLabel auto-start. Does NOT delete data/, .venv/, or
# the OS-keyring OAuth token -- those are kept so you can re-install
# without losing queue history, applies, or having to re-authorize Gmail.
#
# To fully remove QuickLabel from your machine:
#   1. Run this script (removes the auto-start)
#   2. Delete the project folder
#   3. (Optional, if you re-installed with a different OAuth client and
#      don't want the old token lying around in Credential Manager)
#      Open "Credential Manager" -> "Windows Credentials" -> find an
#      entry under "quicklabel" and remove it.
$ErrorActionPreference = "Stop"
$TaskName = "QuickLabel"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "QuickLabel auto-start is not installed. Nothing to remove."
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

# Same orphan-cleanup as stop.ps1 in case the task wasn't the only
# launcher of the server process.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*quicklabel*serve*' } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
    }

Write-Host "Auto-start removed." -ForegroundColor Green
Write-Host ""
Write-Host "Kept (delete by hand if you want a full clean slate):"
Write-Host "  data/                    -- queue, applies, audit log, settings"
Write-Host "  .venv/                   -- Python environment"
Write-Host "  Credential Manager entry 'quicklabel' -- OAuth refresh token"
