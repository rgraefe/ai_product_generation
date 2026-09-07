param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PRODUCT_GENERATOR_PORT = [string]$Port
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) {
    & $bundledPython server.py
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 server.py
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python server.py
} else {
    throw 'Python 3.11 or newer is required. Install Python and run python server.py.'
}
