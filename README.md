<img src="packaging/app-512.png" alt="" width="96" align="right">

# Draft Assistant

A pick/ban assistant for League of Legends 5-stacks (EUW): live suggestions in champ select, enemy scouting, and a
look back at every game.

## Install

**[Download the installer](https://github.com/muff1ntogo/lol-draft-assistant/releases/latest/download/DraftAssistant-Setup.exe)**
(Windows 10 or 11), run it, and that's it:

- **No admin rights, nothing else to install.** Python, the web UI and everything they need are inside.
- **Setup happens in the installer:** paste your Riot API key, and, if you used the app from a source checkout before,
  point it at that folder to bring your database, pools and tags across.
- **Updates:** when a new version is pushed here, the app shows an **Update** button (never during a draft). One click
  installs it and restarts. Running the latest installer again does the same.
- **Your data** (database, key, cache) lives in `%LOCALAPPDATA%\DraftAssistant` and survives updates and uninstalling.

Start it from the Desktop or Start menu shortcut. It opens in its own window and connects to the League client by
itself; nothing is ever sent to the client (it only reads).

## What it does

| Page | |
| --- | --- |
| **Live** | Champ select: ban and pick suggestions with reasons, likely enemy picks, both comps, the plan bar. In Ranked 5s it scouts the enemy players and splits bans into "from their pools" and "the rest". |
| **Prep** | Ban plans, the matchup tree, synergy, a plan to pin for the next game. |
| **Pools** | Your five players, their champion pools and tiers (re-scored from recent games). |
| **History** | Every logged game: the draft, and draft vs execution (what the draft gave, the early game, conversion). |
| **Team** | Recent form, style, and trends over the last 10 / 20 / all games. |
| **Settings** | Weights, scouting, the lane hint. |

## Running jobs

The data jobs (crawl, fit, learning, ...) run from the Start menu's **Draft Assistant jobs** prompt:

```
draftassist-cli --help
draftassist-cli crawl --hours 8
```

## For developers

The project is Python 3.12 with [uv](https://docs.astral.sh/uv/) and a Svelte 5 web UI.

```
uv sync
cd web && npm ci && npm run build && cd ..
uv run pytest
uv run draftassist            # the app, in its own window
```

| Folder | |
| --- | --- |
| `src/draftassist/` | The app: `engine/` (scoring, pure), `api/` (FastAPI, Live), `data/` (Riot API, crawl, fits), `learning/`, `lcu/` (read-only League client) |
| `web/` | The UI (Vite + Svelte 5); FastAPI serves `web/dist` |
| `tests/` | pytest (about 1,300 tests) |
| `docs/` | The spec (`SPEC.md`) and one document per feature, with its decisions |
| `packaging/` | The installer: PyInstaller recipe, Inno Setup script, build script ([packaging/README.md](packaging/README.md)) |
| `data/` | Bundled tags, rules and archetype priors (the app's database is never committed) |
| `fixtures/`, `reference/`, `scripts/` | Sanitised LCU recordings, the v1 engine prototype, audit scripts |

`CLAUDE.md` is the working guide (commands, hard rules, status). Pushing to `main` builds a new release
(`.github/workflows/release.yml`).
