"""Data Dragon loader: patch version and static champion data.

`versions.json` is always fetched live so a new patch is picked up; per-version
`champion.json` files are immutable and cached on disk.

CLI: `uv run python -m draftassist.data.ddragon` loads the latest patch into the DB.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlmodel import Session, select

from draftassist.db.models import Champion

BASE_URL = "https://ddragon.leagueoflegends.com"


def latest_version(client: httpx.Client) -> str:
    resp = client.get(f"{BASE_URL}/api/versions.json")
    resp.raise_for_status()
    return resp.json()[0]


def fetch_champions(client: httpx.Client, version: str, cache_dir: Path, locale: str = "en_US") -> dict:
    """Return the parsed champion.json for `version`, from disk cache when present."""
    cache_file = cache_dir / "ddragon" / version / locale / "champion.json"
    if cache_file.is_file():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    resp = client.get(f"{BASE_URL}/cdn/{version}/data/{locale}/champion.json")
    resp.raise_for_status()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(resp.text, encoding="utf-8")
    return resp.json()


STATIC_FILES = ("item.json", "runesReforged.json", "summoner.json")  # names for the Strategy view (docs/strategy.md)


def fetch_static(client: httpx.Client, version: str, cache_dir: Path, name: str, locale: str = "en_US") -> dict | list:
    """Another per-version Data Dragon file (items, runes, summoner spells), from disk cache when present."""
    cache_file = cache_dir / "ddragon" / version / locale / name
    if cache_file.is_file():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    resp = client.get(f"{BASE_URL}/cdn/{version}/data/{locale}/{name}")
    resp.raise_for_status()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(resp.text, encoding="utf-8")
    return resp.json()


def parse_champions(payload: dict, version: str) -> list[Champion]:
    return [
        Champion(champ_id=int(c["key"]), key=c["id"], name=c["name"], patch=version, tags=list(c.get("tags", [])),
                 attack_range=(c.get("stats") or {}).get("attackrange"))
        for c in payload["data"].values()
    ]


def load_champions(session: Session, champions: list[Champion]) -> int:
    """Upsert champions by champ_id. Keeps `roles` already filled by the crawler."""
    existing = {c.champ_id: c for c in session.exec(select(Champion)).all()}
    for champ in champions:
        row = existing.get(champ.champ_id)
        if row is None:
            session.add(champ)
        else:
            row.key, row.name, row.patch, row.tags = champ.key, champ.name, champ.patch, champ.tags
            row.attack_range = champ.attack_range
            session.add(row)
    session.commit()
    return len(champions)


def refresh(client: httpx.Client, session: Session, cache_dir: Path, locale: str = "en_US") -> tuple[str, int]:
    version = latest_version(client)
    payload = fetch_champions(client, version, cache_dir, locale)
    count = load_champions(session, parse_champions(payload, version))
    for name in STATIC_FILES:  # not essential: the Strategy view shows ids without them
        try:
            fetch_static(client, version, cache_dir, name, locale)
        except httpx.HTTPError:
            pass
    return version, count


def main() -> None:
    from draftassist.config import load_settings
    from draftassist.db.session import init_db

    settings = load_settings()
    engine = init_db(settings.db_path)
    with httpx.Client(timeout=30) as client, Session(engine) as session:
        version, count = refresh(client, session, settings.cache_dir, settings.ddragon_locale)
    print(f"Data Dragon patch {version}: loaded {count} champions into {settings.db_path}")


if __name__ == "__main__":
    main()
