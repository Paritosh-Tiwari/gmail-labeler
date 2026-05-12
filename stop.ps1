# Stop the QuickLabel auto-start task. Auto-start at next login is unaffected.
$ErrorActionPreference = "Stop"
$TaskName = "QuickLabel"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "QuickLabel auto-start is not installed. Nothing to stop."
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# Stop-ScheduledTask only stops the running instance; the underlying
# python.exe process may need a moment to die. Also catch any orphaned
# python.exe running our serve module (e.g. started manually).
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*quicklabel*serve*' } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
    }

Write-Host "Stopped." -ForegroundColor Green
