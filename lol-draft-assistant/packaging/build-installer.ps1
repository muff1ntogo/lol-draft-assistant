# Builds packaging\Output\DraftAssistant-Setup-<version>.exe. Run from anywhere, in PowerShell:
#     powershell -ExecutionPolicy Bypass -File packaging\build-installer.ps1
# Needs uv, Node.js (npm) and Inno Setup 6 (https://jrsoftware.org/isinfo.php). See packaging\README.md.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($name, [scriptblock]$block) {
    Write-Host "`n== $name" -ForegroundColor Cyan
    & $block
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "$name failed (exit $LASTEXITCODE)" }
}

$version = (Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Draft Assistant $version"

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
Step "Tests" { uv run pytest -q tests/test_packaging.py tests/test_launcher.py tests/test_data_folder.py }
Step "Bundle (PyInstaller)" { uv run pyinstaller packaging\DraftAssistant.spec --noconfirm --clean }
Step "Smoke test" {
    $cli = Join-Path $root "dist\DraftAssistant\draftassist-cli.exe"
    & $cli --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "draftassist-cli.exe --help failed" }
}
Step "Installer (Inno Setup)" { Push-Location packaging; try { & $iscc "/DAppVersion=$version" DraftAssistant.iss } finally { Pop-Location } }

$setup = Join-Path $root "packaging\Output\DraftAssistant-Setup-$version.exe"
Write-Host "`nDone: $setup" -ForegroundColor Green
