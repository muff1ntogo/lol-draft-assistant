"""App settings, loaded from the environment and an optional `.env` file.

The Riot API key is a secret: it is kept out of repr/str so it never ends up in
logs or tracebacks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    db_path: Path = PROJECT_ROOT / "data" / "draftassist.sqlite"
    cache_dir: Path = PROJECT_ROOT / "cache"
    ddragon_locale: str = "en_US"
    draft_format: str = "auto"  # auto | tournament | ranked
    lcu_mode: str = "client"  # client (real League client) | demo (simulated draft) | off
    record_sessions: bool = True  # dump every champ select event to fixtures/lcu/recorded/

    @property
    def has_riot_api_key(self) -> bool:
        return bool(self.riot_api_key)


def load_settings(env_file: Path | None = None) -> Settings:
    """Real environment variables win over values from the `.env` file."""
    file_values = parse_env_file(env_file if env_file is not None else PROJECT_ROOT / ".env")

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
        record_sessions=(get("DRAFTASSIST_RECORD") or "1") == "1",
    )
