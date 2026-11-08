# PyInstaller recipe for the installed app (packaging/build-installer.ps1 runs it; see packaging/README.md).
#
# One folder, two programs sharing it:
#   DraftAssistant.exe   the app window, no console (what the shortcuts start)
#   draftassist-cli.exe  the same launcher with a console, for the jobs: draftassist-cli crawl --hours 8
#
# Bundled read-only files land under the bundle (config.RESOURCE_ROOT). Never bundle a database, .env, cache/ or
# recordings: only the files listed in BUNDLED below go in.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is set by PyInstaller

BUNDLED = [
    (ROOT / "web" / "dist", "web/dist"),
    (ROOT / "data" / "archetype_matchups.yaml", "data"),
    (ROOT / "data" / "champion_tags.yaml", "data"),
    (ROOT / "data" / "style_rules.yaml", "data"),
    (ROOT / "data" / "team_rules.yaml", "data"),
    (ROOT / "fixtures" / "lcu" / "flex-ranked-sample", "fixtures/lcu/flex-ranked-sample"),  # --replay's default
    (ROOT / "packaging" / "app.ico", "."),  # the window's icon (launcher.py); the programs get it from EXE(icon=...)
]
missing = [str(src) for src, _ in BUNDLED if not src.exists()]
if missing:
    raise SystemExit("missing (build the web UI first: cd web; npm ci; npm run build): " + ", ".join(missing))
# The build number, written by build-installer.ps1; without it the app can't tell it's out of date (updates.py)
BUILD_INFO = ROOT / "packaging" / "build_info.json"
if BUILD_INFO.is_file():
    BUNDLED.append((BUILD_INFO, "."))

# The app imports its jobs and routers lazily, and uvicorn picks its loop and protocols by name
HIDDEN = (collect_submodules("draftassist") + collect_submodules("uvicorn") + collect_submodules("webview")
          + ["websockets", "httptools", "yaml",
             "zstandard",  # the Riot cache (data/response_cache.py)
             "keyring.backends.Windows"])  # the sign-in's tokens in Windows Credential Manager (data/shared/account.py)

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    datas=[(str(src), dest) for src, dest in BUNDLED],
    hiddenimports=HIDDEN,
    excludes=["tkinter", "pytest", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

ICON = ROOT / "packaging" / "app.ico"
icon = str(ICON) if ICON.is_file() else None

app = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True, name="DraftAssistant", console=False, icon=icon,
)
cli = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True, name="draftassist-cli", console=True, icon=icon,
)
COLLECT(app, cli, a.binaries, a.datas, name="DraftAssistant")  # noqa: F821
