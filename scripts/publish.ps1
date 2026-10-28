<#
.SYNOPSIS
    Publish dsh-doraemon: npm registry and the GitHub Release page.

.DESCRIPTION
    Two channels need a token you paste at a hidden prompt; nothing is written to the
    repository afterwards. The git install and the market index need no token and are
    not touched here.

    npm token:    https://www.npmjs.com/settings/<user>/tokens  -> Classic -> Automation
    GitHub token: https://github.com/settings/tokens/new?scopes=repo&description=dsh-cua-release

.PARAMETER SkipNpm
    Do not publish to npm.

.PARAMETER SkipRelease
    Do not create the GitHub Release.

.PARAMETER DryRun
    Print what would happen, ask for nothing, change nothing.

.PARAMETER SyncExisting
    When the Release for this tag already exists, replace its body with
    docs/release-v<version>.md. Without this switch an existing release is left alone.

.PARAMETER Force
    Run even if the working tree is dirty (not recommended: npm publishes the worktree).

.EXAMPLE
    pwsh -File scripts\publish.ps1

.EXAMPLE
    $env:NPM_TOKEN = 'npm_...'; pwsh -File scripts\publish.ps1 -SkipRelease
#>
[CmdletBinding()]
param(
    [switch]$SkipNpm,
    [switch]$SkipRelease,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$SyncExisting
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Manifest = Get-Content (Join-Path $RepoRoot 'package.json') -Raw | ConvertFrom-Json
$PackageName = $Manifest.name
$Version = $Manifest.version
$Tag = "v$Version"
$RepoSlug = 'Mrkennyhungry/dsh-cua'
$Registry = 'https://registry.npmjs.org'

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }

function Read-Secret([string]$Label) {
    $Secure = Read-Host -AsSecureString $Label
    if ($null -eq $Secure -or $Secure.Length -eq 0) { throw "No token entered." }
    $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) }
}

function Resolve-PnpmLauncher {
    $OnPath = Get-Command pnpm -ErrorAction SilentlyContinue
    if ($OnPath) { return $OnPath.Source }

    $RuntimePnpm = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\pnpm\bin\pnpm.mjs'
    if (Test-Path $RuntimePnpm) {
        $Node = Get-Command node -ErrorAction SilentlyContinue
        if (-not $Node) {
            $BundledNode = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe'
            if (Test-Path $BundledNode) { $Node = Get-Item $BundledNode }
        }
        if (-not $Node) { throw "pnpm found but node is not on PATH; install Node.js >= 22." }
        return @{ Exe = $Node.Source ?? $Node.FullName; Prefix = @($RuntimePnpm) }
    }
    throw "pnpm not found. Install it (corepack enable pnpm) or use the DSH desktop runtime."
}

Write-Step "Publishing $PackageName@$Version (tag $Tag) from $RepoRoot"

$Dirty = git -C $RepoRoot status --porcelain
if ($Dirty -and -not $Force -and -not $DryRun) {
    throw "Working tree is dirty; commit first so the release matches the published files. (Use -Force to override.)"
}
if ($Dirty) { Write-Warn "Working tree is dirty; the published tarball uses the worktree, not HEAD." }

# ---------------------------------------------------------------- npm ---------
if (-not $SkipNpm) {
    Write-Step "npm publish"
    if ($DryRun) {
        Write-Ok "would write a temporary .npmrc and run: pnpm publish --access public --no-git-checks"
    }
    else {
        $Token = $env:NPM_TOKEN
        if (-not $Token) {
            Write-Warn "Create one at https://www.npmjs.com/settings/~your-username~/tokens (Classic, Automation)."
            $Token = Read-Secret "npm token (npm_...)"
        }

        $Npmrc = Join-Path $RepoRoot '.npmrc'
        if (Test-Path $Npmrc) {
            Write-Warn "A .npmrc already exists in the repo; renaming it to .npmrc.publish-backup for the run."
            Move-Item $Npmrc "$Npmrc.publish-backup" -Force
        }
        try {
            Set-Content -Path $Npmrc -Value "//registry.npmjs.org/:_authToken=$Token" -NoNewline -Encoding ascii
            $Pnpm = Resolve-PnpmLauncher
            if ($Pnpm -is [hashtable]) {
                $Arguments = $Pnpm.Prefix + @('publish', '--access', 'public', '--no-git-checks')
                & $Pnpm.Exe @Arguments
            }
            else {
                & $Pnpm publish --access public --no-git-checks
            }
            if ($LASTEXITCODE -ne 0) { throw "pnpm publish exited $LASTEXITCODE." }
        }
        finally {
            Remove-Item $Npmrc -Force -ErrorAction SilentlyContinue
            if (Test-Path "$Npmrc.publish-backup") { Move-Item "$Npmrc.publish-backup" $Npmrc -Force }
        }

        $Published = $null
        try { $Published = Invoke-RestMethod "$Registry/$PackageName/$Version" -TimeoutSec 30 } catch { }
        if ($Published -and $Published.version -eq $Version) {
            Write-Ok "$Registry/$PackageName/$Version is live."
            Write-Ok "Install with: $PackageName@$Version"
        }
        else {
            Write-Warn "Publish returned success but $Registry/$PackageName/$Version is not readable yet;"
            Write-Warn "registry propagation can take a minute. Check https://www.npmjs.com/package/$PackageName"
        }
    }
}

# ------------------------------------------------------------ GitHub ---------
if (-not $SkipRelease) {
    Write-Step "GitHub Release $Tag"
    $BodyFile = Join-Path $RepoRoot "docs\release-$Tag.md"
    if (-not (Test-Path $BodyFile)) { throw "Release notes missing: $BodyFile" }

    $Headers = @{ 'User-Agent' = 'dsh-cua-publish'; Accept = 'application/vnd.github+json' }
    $Existing = $null
    try { $Existing = Invoke-RestMethod "https://api.github.com/repos/$RepoSlug/releases/tags/$Tag" -Headers $Headers -TimeoutSec 30 } catch { }

    if ($Existing) {
        Write-Ok "Release already exists: $($Existing.html_url)"
        $Want = (Get-Content $BodyFile -Raw).Trim()
        if (($Existing.body ?? '').Trim() -eq $Want) {
            Write-Ok "Body matches $BodyFile."
        }
        elseif ($DryRun) {
            Write-Ok "body differs from $BodyFile; -SyncExisting would PATCH it"
        }
        elseif (-not $SyncExisting) {
            Write-Warn "Body differs from $BodyFile. Re-run with -SyncExisting to update it,"
            Write-Warn "or edit the release in the web UI and paste that file."
        }
        else {
            $Token = $env:GITHUB_TOKEN
            if (-not $Token) {
                Write-Warn "Create one at https://github.com/settings/tokens/new?scopes=repo&description=dsh-cua-release"
                $Token = Read-Secret "GitHub token (ghp_... or github_pat_...)"
            }
            $Headers['Authorization'] = "Bearer $Token"
            $Patch = @{ body = $Want } | ConvertTo-Json -Depth 3
            try {
                $Updated = Invoke-RestMethod -Method Patch `
                    -Uri "https://api.github.com/repos/$RepoSlug/releases/$($Existing.id)" `
                    -Headers $Headers -Body $Patch -ContentType 'application/json' -TimeoutSec 60
                Write-Ok "Updated: $($Updated.html_url)"
            }
            catch { throw "GitHub Release update failed: $($_.Exception.Message) $($_.ErrorDetails.Message)" }
        }
    }
    elseif ($DryRun) {
        Write-Ok "would POST /repos/$RepoSlug/releases (tag $Tag, body $BodyFile)"
    }
    else {
        $Token = $env:GITHUB_TOKEN
        if (-not $Token) {
            Write-Warn "Create one at https://github.com/settings/tokens/new?scopes=repo&description=dsh-cua-release"
            $Token = Read-Secret "GitHub token (ghp_... or github_pat_...)"
        }
        $Headers['Authorization'] = "Bearer $Token"
        $Payload = @{
            tag_name         = $Tag
            target_commitish = 'main'
            name             = "$PackageName $Tag"
            body             = (Get-Content $BodyFile -Raw)
            draft            = $false
            prerelease       = $false
        } | ConvertTo-Json -Depth 5

        try {
            $Release = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$RepoSlug/releases" `
                -Headers $Headers -Body $Payload -ContentType 'application/json' -TimeoutSec 60
            Write-Ok "Created: $($Release.html_url)"
        }
        catch {
            $Detail = $_.ErrorDetails.Message
            if ($Detail -match 'already_exists') { Write-Ok "A release for $Tag already exists." }
            else { throw "GitHub Release failed: $($_.Exception.Message) $Detail" }
        }
    }
}

Write-Step "Done"
Write-Host "Next: add an npm version entry to market/index.json once npm is live (docs/publishing.md)." -ForegroundColor DarkGray
