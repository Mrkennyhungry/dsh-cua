# Build the TKCode.Arc companion picker into the local runtime directory.
[CmdletBinding()]
param(
    [string]$Dotnet = "$env:LOCALAPPDATA\dsh-desktop\dotnet\dotnet.exe"
)
$ErrorActionPreference = 'Stop'
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$root = [IO.Path]::GetFullPath((Join-Path $scriptDir '..'))
$project = Join-Path $root 'companion\DshDoraemon.Companion.csproj'
$output = Join-Path $root '.runtime\companion-ui'
if (-not (Test-Path -LiteralPath $Dotnet)) {
    $command = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($command) { $Dotnet = $command.Source }
}
if (-not (Test-Path -LiteralPath $Dotnet)) {
    throw '.NET 11 SDK was not found. Install it or pass -Dotnet <path-to-dotnet.exe>.'
}
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
# A normal self-contained folder starts without the single-file extractor. The
# extractor needs a writable temp directory and failed under the elevated kernel.
& $Dotnet publish $project -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=false -p:PublishTrimmed=false -o $output --nologo
if ($LASTEXITCODE -ne 0) { throw "Companion UI publish failed with exit code $LASTEXITCODE" }
$exe = Join-Path $output 'DshDoraemon.Companion.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Publish completed but $exe is missing" }
Write-Host "Companion UI built: $exe"
