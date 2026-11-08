# Packaging: the installer, releases and updates

The installed app is the same code as a source checkout, bundled with PyInstaller and wrapped in an Inno Setup
installer. Nobody needs Python, uv or Node to run it.

## How a change reaches an installed app

1. A push to `main` runs `.github/workflows/release.yml` on a Windows runner: it builds the installer with version
   `<major.minor from pyproject.toml>.<run number - release_run_base>` (when major.minor changes, set
   `[tool.draftassist] release_run_base` in pyproject.toml to the last release's run number, so the new line starts at .1)
   and publishes it as a GitHub release, attached as
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
- **How will you use Draft Assistant?** **Join my team** (paste the invite code), **Team admin** (the Turso database's
  URL, organisation, name, your display name, the full-access token and the Platform API token) or **Solo** (as before).
  The invite or the tokens are handed to `draftassist-cli account join|admin --file` in a file in Setup's temp folder,
  which the app deletes as soon as it has read it: never on a command line, never in the install folder. They end up in
  Windows Credential Manager. All of it can be changed later in Settings, Account.
- **Riot API key (optional page):** written to `%LOCALAPPDATA%\DraftAssistant\.env`. Team members need it only for their
  own match history and scouting; the admin needs it to crawl. It can also be set later in Settings.
- **Bring data across (optional page):** pick a source checkout's folder (the one with `data\` and `.env`) and it runs
  `draftassist-cli import-data` on it: the databases, key and edited tags. A team member's import (`--user`) keeps only
  their own games, roster, pools and settings; the stats come from the team.
- **First start:** a team member's app pulls the team's stats in the background with a progress banner ("Pulling the
  team's stats: N of M blocks"; Live uses OP.GG's stats until it's done), then fetches champions (Data Dragon). Solo and
  admin installs fetch champions and OP.GG's starting stats (`first_run.py`). The crawl stays a job you start.

An update (the app already set up, or a silent install from the Update button) skips every page.

## Shared data: setting up the team (admin)

1. Make a Turso account (turso.tech) and install its CLI. Create the database: `turso db create league-drafter`.
2. Collect what the app needs:
   - URL: `turso db show league-drafter --url` (`libsql://league-drafter-<org>.<region>.turso.io`)
   - organisation: `turso org list`; database name: `league-drafter`
   - full-access database token: `turso db tokens create league-drafter`
   - Platform API token: `turso auth api-tokens mint draftassist`
3. In the app: Settings, Account, **I am the team admin**, paste them (or pick Team admin in the installer). They go to
   Windows Credential Manager; don't keep them in a text file, and never in the repo (it's public).
4. Crawl as usual, then **Publish** on the Crawl page (or `draftassist publish`). It fits the datasets first, writes only
   the blocks that changed, and shows the rows written this month against Turso's free tier (10 M; a publish is a few
   hundred rows). It waits out champ select.
5. Crawl page, **Members**: add a teammate by display name (no Riot ID). The invite code is shown once: send it privately.
   It expires after 90 days; you and they are warned 14 days ahead; **Renew** makes a new one. **Remove** rotates the
   database's keys (Turso can't revoke one token), so every other member gets a new code to send, shown once.
6. Datasets (Crawl page): current patch, recent (last 6 patches), every patch and Master+ come by default; add more there.
   Each gets its own fits with the next Publish.

## Shared data: joining (teammates)

1. Install with the installer and pick **Join my team**, or in an installed app go to Settings, Account and paste the code.
2. The app pulls the team's stats (a banner shows the progress), then again at every start, every 3 hours and on
   **Sync now** (Crawl page), never during champ select. Offline, it keeps the last pull.
3. The Crawl page shows what's published and **Data selection**: which patches, tiers (per player bracket) and queues your
   stats are summed from, and whether the Matchup tree uses Master+. "Fits from" names the admin's dataset in use.
4. Your own games, History, scouting, roster and pools stay on your machine and are never shared. Add a Riot API key in
   Settings for your own match history and scouting.
5. Signing out (Settings, Account) removes the stored token and keeps your data: the app is then solo.

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
