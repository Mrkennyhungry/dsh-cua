[CmdletBinding()]
param(
  [switch]$Uninstall,
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'dsh-desktop'),
  [string]$TaskName = 'DshDesktopKernelAdmin'
)
$ErrorActionPreference = 'Stop'
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "Removed scheduled task $TaskName."
  return
}
$pluginRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $pluginRoot 'kernel'
$venv = Join-Path $InstallDir 'venv'
$requirements = Join-Path $pluginRoot 'requirements.txt'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { throw 'uv is required to locate the Python 3.11 runtime.' }
if (-not (Test-Path (Join-Path $sourceRoot 'dsh_desktop\dsh_bridge_cli.py'))) { throw "Kernel source missing: $sourceRoot" }
if (-not (Test-Path (Join-Path $venv 'Lib\site-packages\pywin32_system32'))) { throw 'Run scripts/bootstrap.ps1 before installing the elevated task.' }
$uvPython = (& $uv.Source python find 3.11 2>$null | Select-Object -Last 1).Trim()
if (-not (Test-Path $uvPython)) { throw 'uv Python 3.11 was not found.' }
$homeDir = $InstallDir
$wrapper = Join-Path $InstallDir 'run-kernel-high.py'
$launch = Join-Path $InstallDir 'start-kernel-high.ps1'
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'run-kernel-high.py') -Destination $wrapper -Force
$lines = @(
  '$ErrorActionPreference = ''Stop''',
  ('$homeDir = ''' + $homeDir + ''''),
  ('$sourceRoot = ''' + $sourceRoot + ''''),
  ('$installRoot = ''' + $InstallDir + ''''),
  ('$uvPython = ''' + $uvPython + ''''),
  ('$boot = Join-Path $homeDir ''kernel-boot.json'''),
  'if (-not (Test-Path $boot)) { throw "Missing boot config: $boot" }',
  '$cfg = Get-Content $boot -Raw -Encoding UTF8 | ConvertFrom-Json',
  'if ([string]::IsNullOrWhiteSpace([string]$cfg.token)) { throw ''Bridge token missing'' }',
  '$env:DSH_DESKTOP_BRIDGE_TOKEN = [string]$cfg.token',
  'if ($cfg.apiKey) { $env:DEEPSEEK_API_KEY = [string]$cfg.apiKey }',
  '$env:DSH_DESKTOP_INSTALL = $installRoot',
  '$env:DSH_DESKTOP_SOURCE = $sourceRoot',
  '$env:DSH_DESKTOP_HOME = if ($cfg.home) { [string]$cfg.home } else { $homeDir }',
  '$env:DSH_DESKTOP_STATE_DIR = if ($cfg.stateDir) { [string]$cfg.stateDir } else { Join-Path $homeDir ''state'' }',
  '$env:DSH_DESKTOP_PORT = if ($cfg.port) { [string]$cfg.port } else { ''8766'' }',
  '$env:PYTHONPATH = $sourceRoot',
  '$env:PYTHONIOENCODING = ''utf-8''',
  '$env:PYTHONUNBUFFERED = ''1''',
  '& $uvPython (Join-Path $installRoot ''run-kernel-high.py'')'
)
Set-Content -LiteralPath $launch -Value $lines -Encoding utf8
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launch`""
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered $TaskName. Kernel source: $sourceRoot"
Write-Host 'The task is triggered by the plugin; DSH does not need a second console window.'
