param(
    [string]$Repo = "$env:USERPROFILE\Desktop\Donatix_Android_iOS_v4",
    [switch]$Push
)
$ErrorActionPreference = "Stop"
$utf8 = [Text.UTF8Encoding]::new($false)

function Get-TextHash([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes($Text))).Replace("-", "").ToLowerInvariant())
    } finally { $sha.Dispose() }
}

function Read-Normalized([string]$Path) {
    return [IO.File]::ReadAllText($Path).Replace("`r`n", "`n")
}

if (-not (Test-Path -LiteralPath $Repo -PathType Container)) {
    throw "Project folder does not exist: $Repo"
}
Push-Location $Repo
try {
    $inside = git rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or $inside -ne "true") {
        throw "This folder is not the existing Git repository. Use -Repo with the correct project folder."
    }
    if ($Push) {
        $remote = git remote get-url origin
        if ($LASTEXITCODE -ne 0 -or $remote -notmatch 'github\.com[:/]sattorovwv-oss/donatix(?:\.git)?/?$') {
            throw "Unexpected origin repository. Nothing was pushed."
        }
        $branch = git branch --show-current
        if ($LASTEXITCODE -ne 0 -or $branch -ne "main") { throw "Use your existing main branch." }
    }
    $manifest = Get-Content -LiteralPath "$PSScriptRoot\update_files_manifest.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $backend = Get-Content -LiteralPath "$PSScriptRoot\Android_Server_Addon\orders_deletion_manifest.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $writes = @()
    $paths = @()
    # Validate every target before writing any file, including LF/CRLF copies.
    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        if ($relative.Contains("..") -or [IO.Path]::IsPathRooted($relative)) { throw "Unsafe update path." }
        $source = Join-Path $PSScriptRoot $relative
        $target = Join-Path $Repo $relative
        $desired = Read-Normalized $source
        if ((Get-TextHash $desired) -ne $entry.after) { throw "Damaged upload: $relative" }
        if (Test-Path -LiteralPath $target) {
            $current = Read-Normalized $target
            $currentHash = Get-TextHash $current
            if ($relative -eq "Android_Server_Addon/donatix_android_extension/factory.py") {
                $variant = $backend.factories | Where-Object { $_.before -eq $currentHash -or $_.after -eq $currentHash } | Select-Object -First 1
                if (-not $variant) { throw "Existing addon factory differs. No files were written." }
                if ($currentHash -eq $variant.before) {
                    foreach ($edit in $backend.factory_edits) {
                        if ([Regex]::Matches($current, [Regex]::Escape([string]$edit.before)).Count -ne 1) {
                            throw "Factory patch mismatch. No files were written."
                        }
                        $current = $current.Replace([string]$edit.before, [string]$edit.after)
                    }
                }
                if ((Get-TextHash $current) -ne $variant.after) { throw "Factory integrity check failed." }
                $desired = $current
            } elseif ($currentHash -ne $entry.after -and $currentHash -ne $entry.before) {
                throw "File has other local changes: $relative. No files were written."
            }
        } elseif ($entry.before) {
            throw "Required original file is missing: $relative. No files were written."
        }
        $writes += [PSCustomObject]@{ Path = $target; Text = $desired }
        $paths += $relative
    }
    # Keep the control file next to the copied script for later reruns.
    $writes += [PSCustomObject]@{
        Path = (Join-Path $Repo "update_files_manifest.json")
        Text = (Read-Normalized "$PSScriptRoot\update_files_manifest.json")
    }
    $paths += "update_files_manifest.json"
    foreach ($item in $writes) {
        $parent = [IO.Path]::GetDirectoryName($item.Path)
        [IO.Directory]::CreateDirectory($parent) | Out-Null
        [IO.File]::WriteAllText($item.Path, $item.Text, $utf8)
    }
    Write-Host "Order history and account deletion source update applied."
    if ($Push) {
        $staged = @(git diff --cached --name-only)
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect staged files." }
        foreach ($name in $staged) {
            if ($name -and $name -notin $paths) { throw "Other files are staged. Commit them separately before running -Push." }
        }
        git add -- @paths
        if ($LASTEXITCODE -ne 0) { throw "Git add failed." }
        git diff --cached --quiet
        if ($LASTEXITCODE -eq 1) {
            git commit -m "Fix Android order history and implement account deletion"
            if ($LASTEXITCODE -ne 0) { throw "Git commit failed." }
        } elseif ($LASTEXITCODE -ne 0) { throw "Git diff failed." }
        git push origin main
        if ($LASTEXITCODE -ne 0) { throw "Git push failed." }
        Write-Host "GitHub updated. Build the existing android-release workflow in Codemagic."
    }
} finally { Pop-Location }
