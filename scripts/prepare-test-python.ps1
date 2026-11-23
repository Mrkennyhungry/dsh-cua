param(
  [string]$RuntimeDir = (Join-Path $env:LOCALAPPDATA 'dsh-desktop\venv')
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repo '.venv\Scripts\python.exe'
$runtimePython = Join-Path $RuntimeDir 'Scripts\python.exe'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $runtimePython)) {
  throw 'Create the project .venv and run bootstrap.ps1 before preparing runtime test dependencies.'
}
$version = & $python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
$runtimeVersion = & $runtimePython -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
if ($LASTEXITCODE -ne 0 -or $version -ne $runtimeVersion) {
  throw 'The test and installed runtime interpreters must have the same Python major/minor version.'
}
# Reuse the installed kernel dependencies while keeping pytest and new test
# packages in the project venv. addsite also activates pywin32's .pth setup.
$site = Join-Path $RuntimeDir 'Lib\site-packages'
$encoded = ConvertTo-Json -InputObject $site -Compress
$line = "import site; site.addsitedir($encoded)"
Set-Content -LiteralPath (Join-Path $repo '.venv\Lib\site-packages\dsh-test-runtime.pth') -Value $line -Encoding utf8NoBOM
Write-Output "Test dependencies use $site with Python $version"
