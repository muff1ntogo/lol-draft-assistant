# Packaging: the Windows installer

The installed app is the same code as the source checkout, bundled with PyInstaller and wrapped in an Inno Setup
installer. Nobody needs Python, uv or Node to run it.

| File | What it is |
| --- | --- |
| `DraftAssistant.spec` | PyInstaller recipe: one folder with `DraftAssistant.exe` (app window, no console) and `draftassist-cli.exe` (console, for the jobs) |
| `entry.py` | The bundle's entry point: `draftassist.launcher.main` |
| `DraftAssistant.iss` | Inno Setup script: per-user install (no admin prompt), Start menu and optional Desktop shortcuts, uninstaller |
| `build-installer.ps1` | Builds everything: dependencies, web UI, tests, bundle, smoke test, installer |
| `app.ico` | Optional icon. If present it's used for the programs and the installer |

## Build it on your PC

Once: install [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`). uv and Node.js
you already have. Then, from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-installer.ps1
```

The installer lands in `packaging\Output\DraftAssistant-Setup-<version>.exe` (the version is `pyproject.toml`'s).

## Or let GitHub build it

`.github/workflows/installer.yml` builds the same installer on a Windows runner: Actions tab > Windows installer >
Run workflow, then download the `DraftAssistant-Setup` artifact. It also runs on `v*` tags and when this folder changes.

## What the installed app does

- **Program files** go to `%LOCALAPPDATA%\Programs\Draft Assistant`; nothing needs admin rights.
- **Your data** lives in `%LOCALAPPDATA%\DraftAssistant` (`config.DATA_ROOT`): the databases, `cache\`, `.env`
  (the Riot key), recordings, and the tags and rules YAML. Updating or uninstalling never touches it.
- **First run:** copy your checkout's data across once, from the Start menu's "Draft Assistant jobs" prompt:
  `draftassist-cli import-data C:\path\to\lol-draft-assistant` (add `--with-cache` to bring `cache\` too).
  Stop the crawl first. Without it the app starts empty and needs `ddragon`, `opgg` and a crawl, as a new checkout does.
- **Jobs** run from that prompt: `draftassist-cli crawl --hours 8`, `draftassist-cli fit`, and so on
  (`draftassist-cli --help` lists them).
- **The window** is Edge WebView2, which ships with Windows 10 and 11. Without it the app opens your browser instead.
- **Output** from the window-only program goes to `%LOCALAPPDATA%\DraftAssistant\cache\launcher.log`.

The bundle never contains a database, `.env`, `cache\` or recordings: the spec lists exactly what goes in, and the
workflow checks the result.
