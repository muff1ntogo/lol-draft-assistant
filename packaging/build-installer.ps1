# Builds packaging\Output\DraftAssistant-Setup-<version>.exe. Run from anywhere, in PowerShell:
#     powershell -ExecutionPolicy Bypass -File packaging\build-installer.ps1 [-Version 0.1.42]
# Needs uv, Node.js (npm) and Inno Setup 6 (https://jrsoftware.org/isinfo.php). See packaging\README.md.
# -Version: the release workflow passes <major.minor from pyproject.toml>.<run number>; by hand it defaults to
# pyproject.toml's version. The installed app compares it with the latest GitHub release (src/draftassist/updates.py).
param([string]$Version = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($name, [scriptblock]$block) {
    Write-Host "`n== $name" -ForegroundColor Cyan
    & $block
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "$name failed (exit $LASTEXITCODE)" }
}

if (-not $Version) {
    $Version = (Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
}
if ($Version -notmatch '^\d+(\.\d+){1,3}$') { throw "version '$Version' must be numbers and dots, like 0.1.42" }
$commit = (git rev-parse --short HEAD 2>$null)
Write-Host "Draft Assistant $Version ($commit)"

$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found: install it from https://jrsoftware.org/isinfo.php (or: winget install JRSoftware.InnoSetup)" }

Step "Python dependencies" { uv sync --group dev }
Step "Web UI" {
    Push-Location web
    try {
        npm ci; if ($LASTEXITCODE) { throw "npm ci failed" }
        npm run build
    } finally { Pop-Location }
}
Step "Tests" { uv run pytest -q tests/test_packaging.py tests/test_launcher.py tests/test_data_folder.py tests/test_updates.py }
Step "Build number" {
    # Bundled next to the app (DraftAssistant.spec); the updater reads it
    @{ version = $Version; commit = "$commit" } | ConvertTo-Json | Set-Content -Encoding utf8 packaging\build_info.json
}
Step "Bundle (PyInstaller)" { uv run pyinstaller packaging\DraftAssistant.spec --noconfirm --clean }
Step "Smoke test" {
    $cli = Join-Path $root "dist\DraftAssistant\draftassist-cli.exe"
    & $cli --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "draftassist-cli.exe --help failed" }
}
Step "Installer (Inno Setup)" { Push-Location packaging; try { & $iscc "/DAppVersion=$Version" DraftAssistant.iss } finally { Pop-Location } }

$setup = Join-Path $root "packaging\Output\DraftAssistant-Setup-$Version.exe"
Write-Host "`nDone: $setup" -ForegroundColor Green
