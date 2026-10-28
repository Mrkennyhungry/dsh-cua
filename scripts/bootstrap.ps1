[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'dsh-desktop'),
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $pluginRoot 'kernel'
$requirements = Join-Path $pluginRoot 'requirements.txt'
$venv = Join-Path $InstallDir 'venv'
$python = Join-Path $venv 'Scripts\python.exe'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { throw 'Install uv and retry; the bundled kernel requires Python 3.11.' }
if ($DryRun) {
  Write-Host "Would create $venv, install requirements, and copy local model weights."
  return
}
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
if (-not (Test-Path $python)) {
  & $uv.Source venv $venv --python 3.11
  if ($LASTEXITCODE -ne 0) { throw 'uv could not create the Python 3.11 environment.' }
}
& $uv.Source pip install --python $python -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'Kernel dependencies could not be installed.' }
$weightsSource = Join-Path $pluginRoot 'vendor\weights'
$weightsDir = Join-Path $InstallDir 'weights'
if (Test-Path (Join-Path $weightsSource 'icon_detect\model.onnx')) {
  New-Item -ItemType Directory -Path $weightsDir -Force | Out-Null
  Copy-Item (Join-Path $weightsSource '*') $weightsDir -Recurse -Force
}
$env:PYTHONPATH = $sourceRoot
$env:DSH_DESKTOP_HOME = $InstallDir
& $python -c 'import dsh_desktop.dsh_bridge_cli; print("kernel source import: ok")'
if ($LASTEXITCODE -ne 0) { throw 'Kernel source import failed.' }
Write-Host "Desktop kernel ready. Python: $python"
Write-Host 'Run scripts/install-elevated-task.ps1 once from an elevated PowerShell to enable High integrity.'
