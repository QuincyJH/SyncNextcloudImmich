<#
.SYNOPSIS
    Cut a release: tag the current commit and publish a GitHub Release so CI
    builds the versioned ghcr.io image.

.DESCRIPTION
    .github/workflows/publish_image.yml only produces a *versioned* image on a
    published GitHub Release -- macbre/push-to-ghcr reads the version from
    github.event.release.tag_name and falls back to "latest" for every other
    event. A bare `git push origin <tag>` therefore publishes nothing new, which
    is why this script creates the Release rather than just pushing the tag.

    Requires GITHUB_TOKEN (or GH_TOKEN) in the environment: a classic PAT with
    the `repo` scope, or a fine-grained token with Contents: read/write.

.EXAMPLE
    ./scripts/release.ps1
    Releases today's next version, e.g. 2026.09.17.1.

.EXAMPLE
    ./scripts/release.ps1 -Version 2026.09.17.3 -DryRun
    Shows exactly what would happen, changing nothing.
#>
param(
    [string]$Version,
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [switch]$DryRun,
    [switch]$AllowDirty,
    [switch]$MoveLatestTag
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    $output = & git @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed:`n$output"
    }
    return ($output | Out-String).Trim()
}

function Get-RepoSlug {
    param([string]$Url)

    # Accepts https://github.com/owner/repo(.git) and git@github.com:owner/repo(.git)
    if ($Url -match "github\.com[:/]([^/]+)/([^/]+?)(\.git)?/?$") {
        return "$($Matches[1])/$($Matches[2])"
    }
    throw "Could not parse a GitHub owner/repo out of remote URL: $Url"
}

function Resolve-NextVersion {
    param([string[]]$ExistingTags)

    $today = (Get-Date).ToString("yyyy.MM.dd")
    $highest = 0
    foreach ($tag in $ExistingTags) {
        if ($tag -match "^$([Regex]::Escape($today))\.(\d+)$") {
            $n = [int]$Matches[1]
            if ($n -gt $highest) { $highest = $n }
        }
    }
    return "$today.$($highest + 1)"
}

# --- Preflight ------------------------------------------------------------
# Everything that can fail is checked before the tag is created, so a failed
# run never leaves a dangling tag behind.

# Two ways to authenticate, preferring gh: it keeps the credential in the OS
# credential manager instead of an environment variable.
$useGh = $false
if (Get-Command gh -ErrorAction SilentlyContinue) {
    & gh auth status 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $useGh = $true
    } else {
        Write-Warning "gh is installed but not authenticated -- run 'gh auth login' (falling back to GITHUB_TOKEN)."
    }
}

$token = $env:GITHUB_TOKEN
if (-not $token) { $token = $env:GH_TOKEN }
if (-not $useGh -and -not $token -and -not $DryRun) {
    throw @"
No GitHub credentials. Either:
  1. winget install --id GitHub.cli   (then: gh auth login)      <- recommended
  2. `$env:GITHUB_TOKEN = '<PAT with repo scope>'
"@
}

$repoRoot = Invoke-Git rev-parse --show-toplevel
Set-Location -LiteralPath $repoRoot

$remoteUrl = Invoke-Git remote get-url $Remote
$slug = Get-RepoSlug -Url $remoteUrl
Write-Host "Repository: $slug"

if (-not $AllowDirty) {
    $dirty = Invoke-Git status --porcelain
    if ($dirty) {
        throw "Working tree has uncommitted changes -- they would NOT be in the release image.`nCommit or stash them first, or pass -AllowDirty to tag HEAD anyway.`n$dirty"
    }
}

$currentBranch = Invoke-Git rev-parse --abbrev-ref HEAD
if ($currentBranch -ne $Branch) {
    Write-Warning "On branch '$currentBranch', not '$Branch'. The release will be cut from this commit."
}

Write-Host "Fetching tags from $Remote ..."
Invoke-Git fetch --tags --quiet $Remote | Out-Null

# The Release is built from the tagged commit, so that commit must already be on
# the remote branch -- otherwise CI checks out a commit that exists on no branch.
$head = Invoke-Git rev-parse HEAD
$remoteHead = Invoke-Git rev-parse "$Remote/$Branch"
if ($head -ne $remoteHead) {
    & git merge-base --is-ancestor $head "$Remote/$Branch" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "HEAD ($($head.Substring(0,7))) is not pushed to $Remote/$Branch. Push your commits first:`n  git push $Remote $Branch"
    }
    Write-Warning "HEAD is behind $Remote/$Branch -- releasing the older commit $($head.Substring(0,7))."
}

$existingTags = (Invoke-Git tag --list) -split "`r?`n" | Where-Object { $_ }

if ($Version) {
    if ($Version -notmatch "^\d{4}\.\d{2}\.\d{2}\.\d+$") {
        throw "Version '$Version' does not match the YYYY.MM.DD.N format (e.g. 2026.09.17.1)."
    }
    if ($existingTags -contains $Version) {
        throw "Tag '$Version' already exists. Pick another version."
    }
} else {
    $Version = Resolve-NextVersion -ExistingTags $existingTags
}

Write-Host ""
$image = "ghcr.io/$($slug.ToLowerInvariant())"
Write-Host "Releasing $Version from $($head.Substring(0,7)) on $currentBranch" -ForegroundColor Cyan
Write-Host "  -> ${image}:$Version"
Write-Host "  -> ${image}:latest  (moved to this release)"
Write-Host ""

if ($DryRun) {
    Write-Host "[dry run] git tag -a $Version -m ""Release $Version"""
    Write-Host "[dry run] git push $Remote $Version"
    if ($MoveLatestTag) {
        Write-Host "[dry run] git tag -f latest && git push -f $Remote latest"
    }
    if ($useGh) {
        Write-Host "[dry run] gh release create $Version --repo $slug --generate-notes --latest"
    } elseif ($token) {
        Write-Host "[dry run] POST https://api.github.com/repos/$slug/releases (tag_name=$Version)"
    } else {
        Write-Warning "[dry run] No gh auth and no GITHUB_TOKEN -- a real run would stop before tagging."
    }
    Write-Host ""
    Write-Host "[dry run] Nothing was changed."
    return
}

# --- Tag and push ---------------------------------------------------------

Write-Host "Creating tag $Version ..."
Invoke-Git tag -a $Version -m "Release $Version" | Out-Null

Write-Host "Pushing tag to $Remote ..."
try {
    Invoke-Git push $Remote $Version | Out-Null
} catch {
    Write-Warning "Push failed -- removing the local tag so you can retry cleanly."
    & git tag -d $Version 2>&1 | Out-Null
    throw
}

if ($MoveLatestTag) {
    # Purely cosmetic for the package: the image's "latest" tag comes from the
    # push-to-main trigger, not from this git tag.
    Write-Host "Moving the 'latest' git tag ..."
    Invoke-Git tag -f latest | Out-Null
    Invoke-Git push -f $Remote latest | Out-Null
}

# --- Publish the GitHub Release (this is what triggers the versioned build) --

Write-Host "Publishing GitHub Release $Version ..."
$releaseUrl = $null
try {
    if ($useGh) {
        $releaseUrl = (& gh release create $Version --repo $slug --title $Version --generate-notes --latest 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw $releaseUrl }
        $releaseUrl = ($releaseUrl -split "`r?`n" | Where-Object { $_ -match "^https://" } | Select-Object -Last 1)
    } else {
        $headers = @{
            "User-Agent"           = "SyncNextcloudImmich"
            "Accept"               = "application/vnd.github+json"
            "X-GitHub-Api-Version" = "2022-11-28"
            "Authorization"        = "Bearer $token"
        }
        $body = @{
            tag_name               = $Version
            name                   = $Version
            generate_release_notes = $true
            draft                  = $false
            prerelease             = $false
            make_latest            = "true"
        } | ConvertTo-Json

        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$slug/releases" `
            -Method Post -Headers $headers -Body $body -ContentType "application/json"
        $releaseUrl = $release.html_url
    }
} catch {
    throw "Tag $Version was pushed, but creating the Release failed: $($_.Exception.Message)`nCreate it by hand at https://github.com/$slug/releases/new?tag=$Version, or delete the tag and retry:`n  git push $Remote :refs/tags/$Version && git tag -d $Version"
}

if (-not $releaseUrl) { $releaseUrl = "https://github.com/$slug/releases/tag/$Version" }

Write-Host ""
Write-Host "Released $Version" -ForegroundColor Green
Write-Host "  Release:  $releaseUrl"
Write-Host "  Actions:  https://github.com/$slug/actions"
Write-Host "  Image:    ${image}:$Version  and  ${image}:latest  (once the workflow finishes)"
