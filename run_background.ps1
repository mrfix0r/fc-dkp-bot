# Keep the working python.exe/VPN combination, with no visible console.
# Wait for Python and propagate its exit code so Task Scheduler can retry failures.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
try {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $runner = Join-Path $PSScriptRoot 'background.py'
    $process = Start-Process -FilePath $python -ArgumentList ('-X utf8 "' + $runner + '"') `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -Wait -PassThru
    exit $process.ExitCode
} catch {
    $logDirectory = Join-Path $PSScriptRoot 'logs'
    New-Item -Path $logDirectory -ItemType Directory -Force | Out-Null
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss,fff'
    $kind = $_.Exception.GetType().Name
    Add-Content -LiteralPath (Join-Path $logDirectory 'background.log') -Encoding UTF8 `
        -Value "$stamp ERROR: Hidden Python launcher failed ($kind). Check project permissions and Python installation."
    exit 1
}
