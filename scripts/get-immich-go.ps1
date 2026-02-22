param(
    [string]$Version = "latest",
    [string]$OutputDir = "tools/immich-go",
    [string]$Repo = "simulot/immich-go",
    [string]$Arch
)

$ErrorActionPreference = "Stop"

function Resolve-Arch {
    param([string]$RequestedArch)

    if ($RequestedArch) { return $RequestedArch.ToLowerInvariant() }

    try {
        $dockerArch = (docker version --format '{{.Server.Arch}}' 2>$null).Trim()
        if ($dockerArch) {
            if ($dockerArch -eq "x86_64") { return "amd64" }
            return $dockerArch.ToLowerInvariant()
        }
    } catch {
    }

    $osArch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
    switch ($osArch) {
        "x64" { return "amd64" }
        "arm64" { return "arm64" }
        default { return "amd64" }
    }
}

$resolvedArch = Resolve-Arch -RequestedArch $Arch
$archAliases = switch ($resolvedArch) {
    "amd64" { @("amd64", "x86_64") }
    "arm64" { @("arm64", "aarch64") }
    default { @($resolvedArch) }
}
$apiUrl = if ($Version -eq "latest") {
    "https://api.github.com/repos/$Repo/releases/latest"
} else {
    "https://api.github.com/repos/$Repo/releases/tags/$Version"
}

$headers = @{
    "User-Agent" = "SyncNextcloudImmich"
    "Accept" = "application/vnd.github+json"
}

if ($env:GITHUB_TOKEN) {
    $headers["Authorization"] = "Bearer $($env:GITHUB_TOKEN)"
}

Write-Host "Fetching immich-go release metadata from $apiUrl ..."
$release = Invoke-RestMethod -Uri $apiUrl -Headers $headers -Method Get

$asset = $release.assets |
    Where-Object {
        $name = $_.name.ToLowerInvariant()
        $matchesArch = $false
        foreach ($alias in $archAliases) {
            if ($name -match [Regex]::Escape($alias.ToLowerInvariant())) {
                $matchesArch = $true
                break
            }
        }
        $name -match "linux" -and $matchesArch -and $name -match "\.tar\.gz$"
    } |
    Select-Object -First 1

if (-not $asset) {
    throw "No linux/$resolvedArch tar.gz asset found in release $($release.tag_name)."
}

$targetDir = Resolve-Path -LiteralPath "." | ForEach-Object { Join-Path $_.Path $OutputDir }
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

$archivePath = Join-Path $targetDir $asset.name
$binaryPath = Join-Path $targetDir "immich-go"

Write-Host "Downloading $($asset.name) ..."
Invoke-WebRequest -Uri $asset.browser_download_url -Headers $headers -OutFile $archivePath

Write-Host "Extracting archive ..."
$windowsTar = Join-Path $env:SystemRoot "System32\tar.exe"
if (Test-Path -LiteralPath $windowsTar) {
    & $windowsTar -xzf $archivePath -C $targetDir
} else {
    $tar = Get-Command tar -ErrorAction SilentlyContinue
    if (-not $tar) {
        throw "tar is required to extract immich-go archive but was not found."
    }
    & $tar.Source -xzf $archivePath -C $targetDir
}

if (-not (Test-Path -LiteralPath $binaryPath)) {
    $candidate = Get-ChildItem -Path $targetDir -File -Recurse | Where-Object { $_.Name -eq "immich-go" } | Select-Object -First 1
    if ($candidate) {
        Copy-Item -LiteralPath $candidate.FullName -Destination $binaryPath -Force
    }
}

if (-not (Test-Path -LiteralPath $binaryPath)) {
    throw "Download succeeded but immich-go binary was not found after extraction."
}

Write-Host "immich-go ready at: $binaryPath"
