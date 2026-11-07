# Packaging: the installer, releases and updates

The installed app is the same code as a source checkout, bundled with PyInstaller and wrapped in an Inno Setup
installer. Nobody needs Python, uv or Node to run it.

## How a change reaches an installed app

1. A push to `main` runs `.github/workflows/release.yml` on a Windows runner: it builds the installer with version
   `<major.minor from pyproject.toml>.<run number>` and publishes it as a GitHub release, attached as
   `DraftAssistant-Setup.exe` (and again with the version in the name). Pushes that only touch docs or `*.md` don't
   release.
2. The app checks the latest release at startup and every 6 hours (`src/draftassist/updates.py`). When it's newer than
   its own build number (`build_info.json`, bundled by the build script), a banner offers **Update**. It's disabled
   from champ select to the end of the game.
3. Update downloads the installer, checks its SHA-256 against GitHub's, runs it silently and exits. Inno Setup replaces
   the program files and starts the app again. The data folder isn't touched.

The README's download link, `releases/latest/download/DraftAssistant-Setup.exe`, always gets the newest installer.
**The repository must be public** for the app and that link to see releases without a GitHub login.

## What the installer sets up

- **Program files:** `%LOCALAPPDATA%\Programs\Draft Assistant`, no admin rights. Start menu and optional Desktop
  shortcuts, a "jobs" command prompt, a shortcut to the data folder.
- **Riot API key (optional page):** written to `%LOCALAPPDATA%\DraftAssistant\.env`. It can also be set later in
  Settings.
- **Bring data across (optional page):** pick a source checkout's folder (the one with `data\` and `.env`) and it runs
  `draftassist-cli import-data` on it: the databases, key and edited tags.
- **First start:** the app fetches champions (Data Dragon) and OP.GG's starting stats by itself (`first_run.py`).
  The crawl stays a job you start: `draftassist-cli crawl --hours 8`.

An update (the app already set up, or a silent install from the Update button) skips both pages.

## Files

| File | What it is |
| --- | --- |
| `DraftAssistant.spec` | PyInstaller recipe: one folder with `DraftAssistant.exe` (window, no console) and `draftassist-cli.exe` (console, for the jobs) |
| `entry.py` | The bundle's entry point: `draftassist.launcher.main` |
| `DraftAssistant.iss` | Inno Setup script: per-user install, the two setup pages, a restart after a silent update |
| `build-installer.ps1` | Builds everything: dependencies, web UI, tests, build number, bundle, smoke test, installer. `-Version` sets the build number |
| `app.ico` | Optional icon. If present it's used for the programs and the installer |

## Build it by hand

Once: install [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`). Then, from
the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-installer.ps1
```

The installer lands in `packaging\Output\`. On other branches, `.github/workflows/installer.yml` builds one as a
downloadable artifact when `packaging/` changes, without releasing it.

The bundle never contains a database, `.env`, `cache\` or recordings: the spec lists exactly what goes in, and both
workflows check the result. `DRAFTASSIST_BUILD_TEST=1 uv run pytest tests/test_packaging.py` builds the bundle and
runs it.
