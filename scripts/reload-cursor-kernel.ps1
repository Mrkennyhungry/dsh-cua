# Run with no active Doraemon task. The next DSH tool starts the updated kernel.
[CmdletBinding()]
param([string]$StateDir = (Join-Path $PSScriptRoot '../.runtime/state'))
$ErrorActionPreference = 'Stop'
$statePath = Join-Path $StateDir 'kernel-state.json'
if (-not (Test-Path -LiteralPath $statePath)) {
    Write-Host 'No recorded kernel. Restart DSH to reload the plugin.'
    exit 0
}
$cursorState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$kernelPid = [int]$cursorState.pid
if ($kernelPid -le 0) { throw 'Invalid recorded kernel PID.' }
$authHeaders = @{ Authorization = "Bearer $($cursorState.token)"; 'X-DSH-Owner' = ('0' * 64) }
$health = Invoke-RestMethod -Uri "http://127.0.0.1:$($cursorState.port)/api/dsh/health" -Headers $authHeaders -TimeoutSec 5
if ([int]$health.bridge.pid -ne $kernelPid) {
    $expectedSource = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../kernel/dsh_desktop')).TrimEnd('\')
    if (-not $health.bridge.source_root -or ([IO.Path]::GetFullPath($health.bridge.source_root)).TrimEnd('\') -ne $expectedSource) {
        throw 'Live kernel identity differs and source could not be verified; no process was stopped.'
    }
    $kernelPid = [int]$health.bridge.pid
}
Stop-Process -Id $kernelPid -ErrorAction Stop
Write-Host 'Recorded Doraemon kernel stopped. Restart DSH; the next Doraemon action loads physical input and the task commander.'
