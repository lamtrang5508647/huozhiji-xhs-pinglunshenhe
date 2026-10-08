# Pass remaining arguments verbatim; do not run them through Invoke-Expression.
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $environmentPython)) { throw 'Run tools\install-windows.ps1 first.' }
& $environmentPython -m huozhiji_audit @args
exit $LASTEXITCODE
