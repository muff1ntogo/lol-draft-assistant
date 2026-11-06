"""App settings, loaded from the environment and an optional `.env` file.

The Riot API key is a secret: it is kept out of repr/str so it never ends up in
logs or tracebacks.
"""
from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # the source checkout
APP_FOLDER = "DraftAssistant"
# Bundled files the app also writes (Tags and Rules pages): the user's copy lives in the data folder, seeded from the bundle
USER_FILES = ("data/champion_tags.yaml", "data/style_rules.yaml", "data/team_rules.yaml")


def is_frozen() -> bool:
    """True when running from the installed app (PyInstaller), not a source checkout."""
    return bool(getattr(sys, "frozen", False))


def resource_root(frozen: bool | None = None) -> Path:
    """Read-only files shipped with the app: web/dist, the bundled YAML, sample fixtures."""
    frozen = is_frozen() if frozen is None else frozen
    return Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) if frozen else PROJECT_ROOT


def data_root(frozen: bool | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Where the app writes: the database, cache/, .env, recordings and the user's YAML. DRAFTASSIST_HOME wins; the installed
    app uses %LOCALAPPDATA%\\DraftAssistant (Program Files isn't writable); a source checkout keeps everything in itself."""
    env = os.environ if env is None else env
    frozen = is_frozen() if frozen is None else frozen
    if env.get("DRAFTASSIST_HOME"):
        return Path(env["DRAFTASSIST_HOME"])
    if frozen:
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_FOLDER
    return PROJECT_ROOT


RESOURCE_ROOT = resource_root()
DATA_ROOT = data_root()


def ensure_user_files(data: Path | None = None, resources: Path | None = None) -> list[Path]:
    """Copy the bundled YAML into the data folder where it's missing (first run of the installed app). Never overwrites:
    the user's edits win over a newer bundled copy. Returns what was copied."""
    data, resources = data or DATA_ROOT, resources or RESOURCE_ROOT
    copied = []
    for rel in USER_FILES:
        src, dst = resources / rel, data / rel
        if src.is_file() and not dst.exists() and src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(dst)
    return copied


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Ignores blanks, comments and an optional `export ` prefix;
    strips one pair of matching surrounding quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    riot_api_key: str = field(default="", repr=False)
    db_path: Path = DATA_ROOT / "data" / "draftassist.sqlite"
    cache_dir: Path = DATA_ROOT / "cache"
    ddragon_locale: str = "en_US"
    draft_format: str = "auto"  # auto | tournament | ranked
    lcu_mode: str = "client"  # client (real League client) | demo (simulated draft) | replay | off
    replay_dir: str = ""  # replay mode: a folder of recorded NNNN-session.json files
    record_sessions: bool = True  # dump every champ select event to fixtures/lcu/recorded/
    stats_refresh_s: float = 120.0  # how often the app checks for fresh crawl stats (0 = never)
    demo_log: bool = False  # demo mode writes its simulated games to History (use a scratch DRAFTASSIST_DB)

    @property
    def has_riot_api_key(self) -> bool:
        return bool(self.riot_api_key)


def load_settings(env_file: Path | None = None) -> Settings:
    """Real environment variables win over values from the `.env` file."""
    file_values = parse_env_file(env_file if env_file is not None else DATA_ROOT / ".env")

    def get(name: str) -> str | None:
        return os.environ.get(name) or file_values.get(name) or None

    defaults = Settings()
    return Settings(
        riot_api_key=get("RIOT_API_KEY") or "",
        db_path=Path(get("DRAFTASSIST_DB") or defaults.db_path),
        cache_dir=Path(get("DRAFTASSIST_CACHE") or defaults.cache_dir),
        ddragon_locale=get("DDRAGON_LOCALE") or defaults.ddragon_locale,
        draft_format=get("DRAFT_FORMAT") or defaults.draft_format,
        lcu_mode=get("DRAFTASSIST_LCU") or defaults.lcu_mode,
        replay_dir=get("DRAFTASSIST_REPLAY") or "",
        record_sessions=(get("DRAFTASSIST_RECORD") or "1") == "1",
        demo_log=get("DRAFTASSIST_DEMO_LOG") == "1",
        stats_refresh_s=float(get("DRAFTASSIST_STATS_REFRESH_S") or defaults.stats_refresh_s),
    )
