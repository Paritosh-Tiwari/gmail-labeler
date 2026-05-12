# Stop + start. Use this after editing Python code in src/quicklabel/.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $here "stop.ps1")
Start-Sleep -Seconds 1
& (Join-Path $here "start.ps1")
