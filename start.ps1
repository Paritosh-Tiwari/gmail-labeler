# Start the QuickLabel auto-start task. Server runs in the background.
$ErrorActionPreference = "Stop"
$TaskName = "QuickLabel"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "QuickLabel auto-start is not installed. Run setup.ps1 first." -ForegroundColor Yellow
    exit 1
}

Start-ScheduledTask -TaskName $TaskName
Write-Host "Started." -ForegroundColor Green
Write-Host "  http://127.0.0.1:8765"
