# Remove the QuickLabel system-tray auto-start. Server is unaffected.
$ErrorActionPreference = "Stop"
$TaskName = "QuickLabel Tray"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Tray auto-start is not installed. Nothing to remove."
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

# Catch any orphaned tray process (e.g. started manually).
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*quicklabel*tray*' } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
    }

Write-Host "Tray removed." -ForegroundColor Green
Write-Host "  Server auto-start (the QuickLabel task) is unchanged."
Write-Host "  Re-enable the tray any time with .\setup-tray.ps1"
