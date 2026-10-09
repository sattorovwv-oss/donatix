param(
    [string]$Repo = "$env:USERPROFILE\Desktop\Donatix_Android_iOS_v4",
    [switch]$Push
)
$ErrorActionPreference = "Stop"
$utf8 = [Text.UTF8Encoding]::new($false)

function Read-Normalized([string]$Path) {
    return [IO.File]::ReadAllText($Path).Replace("`r`n", "`n")
}
function Get-TextHash([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes($Text))).Replace("-", "").ToLowerInvariant())
    } finally { $sha.Dispose() }
}

if (-not (Test-Path -LiteralPath $Repo -PathType Container)) { throw "Existing project folder was not found." }
Push-Location $Repo
try {
    $inside = git rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or $inside -ne "true") { throw "Use the existing Git repository with -Repo." }
    if ($Push) {
        $remote = git remote get-url origin
        if ($LASTEXITCODE -ne 0 -or $remote -notmatch 'github\.com[:/]sattorovwv-oss/donatix(?:\.git)?/?$') {
            throw "Unexpected GitHub origin. Nothing was written or pushed."
        }
        $branch = git branch --show-current
        if ($LASTEXITCODE -ne 0 -or $branch -ne "main") { throw "Use the existing main branch." }
    }
    $manifestPath = Join-Path $PSScriptRoot "server_fix_manifest_v9_1.json"
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.format -ne 1) { throw "Unsupported update manifest." }
    $writes = @()
    $paths = @()
    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        if ($relative.Contains("..") -or [IO.Path]::IsPathRooted($relative)) { throw "Unsafe update path." }
        $source = Join-Path $PSScriptRoot $relative
        $target = Join-Path $Repo $relative
        $desired = Read-Normalized $source
        if ((Get-TextHash $desired) -ne $entry.after) { throw "Damaged update file: $relative" }
        if (Test-Path -LiteralPath $target) {
            $current = Read-Normalized $target
            $hash = Get-TextHash $current
            if ($relative -eq "Android_Server_Addon/donatix_android_extension/factory.py") {
                if ($hash -notin $entry.allowed_after) { throw "Apply Android v9 first; existing factory differs." }
                # Keep the existing deployed Google link/reset variant intact.
                $desired = $current
            } elseif ($hash -ne $entry.after -and $hash -ne $entry.before) {
                throw "File has unrelated local changes: $relative. Nothing was written."
            }
        } elseif ($entry.before) {
            throw "Apply Android v9 first; required file is missing: $relative"
        }
        $writes += [PSCustomObject]@{ Path = $target; Text = $desired }
        $paths += $relative
    }
    $paths += "server_fix_manifest_v9_1.json"
    if ($Push) {
        $staged = @(git diff --cached --name-only)
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect staged files." }
        foreach ($name in $staged) {
            if ($name -and $name -notin $paths) { throw "Unrelated files are staged. Commit them separately." }
        }
    }
    foreach ($item in $writes) {
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($item.Path)) | Out-Null
        [IO.File]::WriteAllText($item.Path, $item.Text, $utf8)
    }
    [IO.File]::WriteAllText((Join-Path $Repo "server_fix_manifest_v9_1.json"), (Read-Normalized $manifestPath), $utf8)
    Write-Host "Android server compatibility update 9.1 applied to the project. Flutter and signing are unchanged."
    if ($Push) {
        git add -- @paths
        if ($LASTEXITCODE -ne 0) { throw "Git add failed." }
        git diff --cached --quiet
        if ($LASTEXITCODE -eq 1) {
            git commit -m "Adapt Android history and account erasure to current Donatix server"
            if ($LASTEXITCODE -ne 0) { throw "Git commit failed." }
        } elseif ($LASTEXITCODE -ne 0) { throw "Git diff failed." }
        git push origin main
        if ($LASTEXITCODE -ne 0) { throw "Git push failed." }
        Write-Host "GitHub updated. Upload this ZIP to the server and run its installer, then build android-release."
    }
} finally { Pop-Location }
