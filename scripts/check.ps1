$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
Push-Location $repo
$previousPythonPath = $env:PYTHONPATH
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
  node --check index.js
  if ($LASTEXITCODE -ne 0) { throw 'Host syntax check failed' }
  node --check client.js
  if ($LASTEXITCODE -ne 0) { throw 'Client syntax check failed' }
  npm test
  if ($LASTEXITCODE -ne 0) { throw 'Node tests failed' }
  $python = Join-Path $repo '.venv\Scripts\python.exe'
  if (!(Test-Path -LiteralPath $python)) { throw 'Create the project .venv and install pytest first' }
  $env:PYTHONPATH = Join-Path $repo 'kernel'
  # Elevated kernel runs can own the shared COM cache. Keep test-generated COM
  # wrappers and pytest temporary files in this checkout's ignored state folder.
  $env:TEMP = Join-Path $repo '.runtime\test-temp'
  $env:TMP = $env:TEMP
  New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
  & $python -m pytest test -q
  if ($LASTEXITCODE -ne 0) { throw 'Python tests failed' }
} finally {
  $env:PYTHONPATH = $previousPythonPath
  $env:TEMP = $previousTemp
  $env:TMP = $previousTmp
  Pop-Location
}
