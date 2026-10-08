param([switch]$Live, [string]$Python = 'py')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $PSScriptRoot
function Invoke-Checked([string]$Executable, [string[]]$CommandArguments) {
    & $Executable @CommandArguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" }
}
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    if ($Python -eq 'py' -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $Python = 'python'
    } else { throw 'Install Python 3.11+ or pass -Python with the interpreter path.' }
}
$pythonArguments = @()
if ($Python -eq 'py') { $pythonArguments += '-3' }
Invoke-Checked $Python ($pythonArguments + @('-c', 'import sys; assert sys.version_info >= (3,11)'))
$environment = Join-Path $projectRoot '.venv'
if (-not (Test-Path (Join-Path $environment 'Scripts\python.exe'))) {
    Invoke-Checked $Python ($pythonArguments + @('-m', 'venv', $environment))
}
$environmentPython = Join-Path $environment 'Scripts\python.exe'
$installTarget = $projectRoot
if ($Live) { $installTarget += '[xhs]' }
Invoke-Checked $environmentPython @('-m', 'pip', 'install', $installTarget)
Invoke-Checked $environmentPython @('-m', 'huozhiji_audit', 'doctor')
Write-Host 'Installed. Run tools\huozhiji-audit.ps1; no activation or global execution-policy change is needed.'
