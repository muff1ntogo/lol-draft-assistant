"""App window, stage 3: the PyInstaller recipe and the Inno Setup script agree with the app and with each other.

The full build runs only with DRAFTASSIST_BUILD_TEST=1 (about a minute): it bundles the app with PyInstaller, starts the
frozen program and checks that it serves the UI and writes only to its data folder.
"""
import os
import re
import subprocess
import sys
import time

import httpx
import pytest

from draftassist.config import APP_FOLDER, PROJECT_ROOT, USER_FILES

PACKAGING = PROJECT_ROOT / "packaging"
SPEC = (PACKAGING / "DraftAssistant.spec").read_text(encoding="utf-8")
ISS = (PACKAGING / "DraftAssistant.iss").read_text(encoding="utf-8")
BUILD = (PACKAGING / "build-installer.ps1").read_text(encoding="utf-8")
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "installer.yml"


def bundled() -> list[str]:
    """The (source, destination) pairs in the spec's BUNDLED list, as source paths relative to the project."""
    block = SPEC.split("BUNDLED = [", 1)[1].split("\n]", 1)[0]
    return ["/".join(re.findall(r'"([^"]+)"', line.split(",")[0])) for line in block.splitlines() if "ROOT" in line]


def test_every_file_the_app_reads_from_the_bundle_is_in_it():
    sources = bundled()
    assert "web/dist" in sources and "data/archetype_matchups.yaml" in sources
    assert all(rel in sources for rel in USER_FILES)  # seeds for the data folder
    assert "fixtures/lcu/flex-ranked-sample" in sources


def test_nothing_private_is_bundled():
    for src in bundled():
        assert not src.endswith((".sqlite", ".env")) and "cache" not in src and "recorded" not in src, src


def test_the_installer_ships_both_programs_from_the_bundle_folder():
    names = re.findall(r'name="([^"]+)"', SPEC)
    assert names == ["DraftAssistant", "draftassist-cli", "DraftAssistant"]  # the two programs, then the folder
    assert '#define AppExe "DraftAssistant.exe"' in ISS and '#define CliExe "draftassist-cli.exe"' in ISS
    assert r'Source: "..\dist\DraftAssistant\*"' in ISS


def test_only_the_cli_has_a_console():
    assert re.search(r'name="DraftAssistant", console=False', SPEC)
    assert re.search(r'name="draftassist-cli", console=True', SPEC)


def test_the_installer_and_the_app_agree_on_the_data_folder():
    assert f"{{localappdata}}\\{APP_FOLDER}" in ISS
    assert "uninsneveruninstall" in ISS  # uninstalling keeps the user's data
    assert "PrivilegesRequired=lowest" in ISS  # no admin prompt


def test_the_build_script_runs_the_recipe_and_the_installer():
    assert r"packaging\DraftAssistant.spec" in BUILD and "DraftAssistant.iss" in BUILD
    assert "npm run build" in BUILD and "/DAppVersion=$Version" in BUILD
    assert r"packaging\build_info.json" in BUILD and "build_info.json" in SPEC  # the build number the updater reads


# ---------- releases and updates (updates.py, .github/workflows/release.yml) ----------
RELEASE = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_every_push_to_main_publishes_the_installer_the_app_downloads():
    from draftassist.updates import ASSET, REPO

    assert "branches: [main]" in RELEASE and "contents: write" in RELEASE
    assert f"packaging/Output/{ASSET}" in RELEASE and "gh release create" in RELEASE
    assert "-Version ${{ steps.version.outputs.version }}" in RELEASE and "github.run_number" in RELEASE
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"github.com/{REPO}/releases/latest/download/{ASSET}" in readme  # the download link up front


def test_a_silent_update_restarts_the_app_and_skips_the_setup_pages():
    from draftassist.updates import INSTALL_ARGS

    assert "/VERYSILENT" in INSTALL_ARGS and "/CLOSEAPPLICATIONS" in INSTALL_ARGS
    assert "Check: WizardSilent" in ISS and "skipifsilent" in ISS
    assert "AlreadySetUp" in ISS and "ShouldSkipPage" in ISS


def test_the_installer_offers_the_key_and_the_import():
    assert "CreateInputQueryPage" in ISS and "RIOT_API_KEY=" in ISS
    assert "import-data" in ISS and "CreateInputDirPage" in ISS


def test_the_workflow_builds_with_the_script():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "./packaging/build-installer.ps1" in text and "runs-on: windows-latest" in text
    for path in re.findall(r'^\s+- "([^"*]+)', text, re.MULTILINE):
        assert (PROJECT_ROOT / path).exists(), path


# ---------- the real build (opt in) ----------
@pytest.mark.skipif(os.environ.get("DRAFTASSIST_BUILD_TEST") != "1", reason="set DRAFTASSIST_BUILD_TEST=1 to build")
def test_the_frozen_app_serves_the_ui_and_writes_only_to_its_data_folder(tmp_path):
    if not (PROJECT_ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip("build the web UI first")
    dist, work, home = tmp_path / "dist", tmp_path / "build", tmp_path / "home"
    subprocess.run([sys.executable, "-m", "PyInstaller", str(PACKAGING / "DraftAssistant.spec"), "--noconfirm",
                    "--distpath", str(dist), "--workpath", str(work)], check=True, capture_output=True)
    exe = dist / "DraftAssistant" / ("draftassist-cli.exe" if os.name == "nt" else "draftassist-cli")
    env = {**os.environ, "DRAFTASSIST_HOME": str(home), "DRAFTASSIST_LCU": "off", "DRAFTASSIST_STATS_REFRESH_S": "0"}
    port = 8151
    proc = subprocess.Popen([str(exe), "--no-browser", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/live", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        page = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)
        assert "<title>Draft Assistant</title>" in page.text
        script = re.search(r'assets/index-[^"]+\.js', page.text).group(0)
        assert httpx.get(f"http://127.0.0.1:{port}/{script}", timeout=5).status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    assert (home / "data" / "draftassist.sqlite").is_file()
    assert all((home / rel).is_file() for rel in USER_FILES)
    assert not list((dist / "DraftAssistant").rglob("*.sqlite"))
    help_text = subprocess.run([str(exe), "--help"], env=env, capture_output=True, text=True, check=True).stdout
    assert "import-data" in help_text
