"""Judgement tags (docs/synergy-v2.md, Stage 1b): data/champion_tags.yaml <-> champion_tags table.

The YAML file is the source of truth and lives in version control. It is keyed by Data Dragon champion
key, then role; each entry rates engage / disengage / poke / dive / pick / splitpush / enchanter /
hypercarry 0-3 (missing = 0) and carries `reviewed`. The first pass was drafted by Claude
(reviewed: false); edits on the Tags page write the file back and re-sync the table.

    Leona:
      support: {engage: 3, pick: 2, reviewed: true}
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml
from sqlalchemy import Engine
from sqlmodel import Session, delete, select

from draftassist.config import PROJECT_ROOT
from draftassist.db.models import Champion, ChampionTag
from draftassist.engine.model import ROLES
from draftassist.engine.traits import TAG_MAX, TAGS, TagEntry

TAGS_FILE = PROJECT_ROOT / "data" / "champion_tags.yaml"
HEADER = """\
# Judgement traits per champion and role, 0-3 (docs/synergy-v2.md, Stage 1b). Missing tags are 0.
# reviewed: false = drafted by Claude, not checked yet: counts at half weight in every model.
# Edit here or on the Tags page (it writes this file). Keys are Data Dragon champion keys.
# Tags: engage, disengage, poke, dive, pick, splitpush, enchanter, hypercarry
"""

Raw = dict[str, dict[str, TagEntry]]  # champion key -> role -> entry


def _entry(values: Mapping, where: str) -> TagEntry:
    if not isinstance(values, Mapping):
        raise ValueError(f"{where}: expected a mapping of tags")
    reviewed = bool(values.get("reviewed", False))
    tags = {k: v for k, v in values.items() if k != "reviewed"}
    unknown = set(tags) - set(TAGS)
    if unknown:
        raise ValueError(f"{where}: unknown tags {sorted(unknown)}")
    out = dict.fromkeys(TAGS, 0)
    for k, v in tags.items():
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= TAG_MAX:
            raise ValueError(f"{where}: {k} must be an integer 0-{TAG_MAX}, got {v!r}")
        out[k] = v
    return TagEntry(out, reviewed)


def load_tags_yaml(path: Path = TAGS_FILE) -> Raw:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: Raw = {}
    for key, by_role in data.items():
        out[str(key)] = {}
        for role, values in (by_role or {}).items():
            if role not in ROLES:
                raise ValueError(f"{key}: unknown role {role!r}")
            out[str(key)][role] = _entry(values or {}, f"{key}.{role}")
    return out


def _line(e: TagEntry) -> str:
    parts = [f"{t}: {e.values[t]}" for t in TAGS if e.values.get(t)]
    parts.append(f"reviewed: {'true' if e.reviewed else 'false'}")
    return "{" + ", ".join(parts) + "}"


def dump_tags_yaml(raw: Raw, path: Path = TAGS_FILE) -> None:
    """Deterministic: champions alphabetically, roles in lane order, one line per role (clean diffs)."""
    lines = [HEADER]
    for key in sorted(raw, key=str.lower):
        roles = raw[key]
        if not roles:
            continue
        lines.append(f"{key}:")
        for role in (r for r in ROLES if r in roles):
            lines.append(f"  {role}: {_line(roles[role])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sync_tags(db: Engine, path: Path = TAGS_FILE) -> dict:
    """Replace the champion_tags table with the file. Unknown champion keys are skipped and reported."""
    raw = load_tags_yaml(path)
    with Session(db) as s:
        ids = {c.key: c.champ_id for c in s.exec(select(Champion)).all()}
        s.exec(delete(ChampionTag))
        loaded, unknown = 0, []
        for key, by_role in raw.items():
            if key not in ids:
                unknown.append(key)
                continue
            for role, e in by_role.items():
                s.add(ChampionTag(champ_id=ids[key], role=role, reviewed=e.reviewed, **e.values))
                loaded += 1
        s.commit()
    return {"loaded": loaded, "unknown": sorted(unknown)}


def load_tags(db: Engine) -> dict[tuple[int, str], TagEntry]:
    with Session(db) as s:
        rows = s.exec(select(ChampionTag)).all()
    return {(r.champ_id, r.role): TagEntry({t: getattr(r, t) for t in TAGS}, r.reviewed) for r in rows}


def update_tag(db: Engine, path: Path, champ_id: int, role: str, values: Mapping[str, int],
               reviewed: bool) -> TagEntry:
    """Save one champion-role's tags (missing tags = 0) to the file and the table."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    with Session(db) as s:
        champ = s.get(Champion, champ_id)
    if champ is None:
        raise ValueError(f"unknown champion {champ_id}")
    entry = _entry({**values, "reviewed": reviewed}, f"{champ.key}.{role}")
    raw = load_tags_yaml(path)
    raw.setdefault(champ.key, {})[role] = entry
    dump_tags_yaml(raw, path)
    sync_tags(db, path)
    return entry


def remove_tag(db: Engine, path: Path, champ_id: int, role: str) -> None:
    with Session(db) as s:
        champ = s.get(Champion, champ_id)
    raw = load_tags_yaml(path)
    if champ is not None and role in raw.get(champ.key, {}):
        del raw[champ.key][role]
        if not raw[champ.key]:
            del raw[champ.key]
        dump_tags_yaml(raw, path)
        sync_tags(db, path)
