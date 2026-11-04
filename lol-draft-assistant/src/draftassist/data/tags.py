"""Judgement tags (docs/synergy-v2.md, Stage 1b): data/champion_tags.yaml <-> champion_tags table.

The YAML file is the source of truth and lives in version control. It is keyed by Data Dragon champion
key, then role; each entry rates engage / disengage / poke / dive / pick / splitpush / enchanter /
hypercarry 0-3 (missing = 0) and carries `reviewed`. The first pass was drafted by Claude
(reviewed: false); edits on the Tags page write the file back and re-sync the table.

An entry can also name the champion's archetype in that role (docs/matchup-archetypes.md, Stage 1). A line with
only an archetype isn't a tag rating: load_tags leaves it out, so it can't change the synergy models.

    Leona:
      support: {engage: 3, pick: 2, archetype: engage, source: video, reviewed: true}
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import yaml
from sqlalchemy import Engine
from sqlmodel import Session, delete, select

from draftassist.config import PROJECT_ROOT
from draftassist.data.archetypes import Archetype, validate_archetypes
from draftassist.db.models import Champion, ChampionTag
from draftassist.engine.model import ROLES
from draftassist.engine.traits import KIT_TAGS, TAG_MAX, TAGS, KitEntry, TagEntry

TAGS_FILE = PROJECT_ROOT / "data" / "champion_tags.yaml"
HEADER = """\
# Judgement traits per champion and role, 0-3 (docs/synergy-v2.md, Stage 1b). Missing tags are 0.
# reviewed: false = drafted by Claude, not checked yet: counts at half weight in every model.
# reviewed covers the synergy tags; reviewed_kit the kit tags; reviewed_arch the archetype (true is written, false isn't).
# Edit here or on the Tags page (it writes this file). Keys are Data Dragon champion keys.
# Tags: engage, disengage, poke, dive, pick, splitpush, enchanter, hypercarry
# archetype / archetype2: the champion's archetype in that role (data/archetypes.py, docs/matchup-archetypes.md).
# source: video = named in a matchup guide. A line with only an archetype or kit tags isn't a tag rating.
# Kit tags, 0-3 (docs/matchup-archetypes.md, Stage 2): auto_reliant, auto_denial, max_hp_damage, hard_gap_close,
# kite, escape, extend_fight, anti_dive, sustain, ability_range
"""

Raw = dict[str, dict[str, TagEntry]]  # champion key -> role -> entry
_NOT_TAGS = ("reviewed", "reviewed_kit", "reviewed_arch", "archetype", "archetype2", "source")


def _entry(values: Mapping, where: str, role: str) -> TagEntry:
    if not isinstance(values, Mapping):
        raise ValueError(f"{where}: expected a mapping of tags")
    reviewed = bool(values.get("reviewed", False))
    archetype, archetype2, source = values.get("archetype"), values.get("archetype2"), values.get("source")
    validate_archetypes(role, archetype, archetype2, source, where)
    tags = {k: v for k, v in values.items() if k not in _NOT_TAGS and k not in KIT_TAGS}
    unknown = set(tags) - set(TAGS)
    if unknown:
        raise ValueError(f"{where}: unknown tags {sorted(unknown)}")
    out = dict.fromkeys(TAGS, 0)
    kit = {}
    for k, v in values.items():
        if k in _NOT_TAGS:
            continue
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= TAG_MAX:
            raise ValueError(f"{where}: {k} must be an integer 0-{TAG_MAX}, got {v!r}")
        if k in TAGS:
            out[k] = v
        elif v:
            kit[k] = v
    return TagEntry(out, reviewed, archetype, archetype2, source, kit, bool(values.get("reviewed_kit", False)),
                    bool(values.get("reviewed_arch", False)))


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
            out[str(key)][role] = _entry(values or {}, f"{key}.{role}", role)
    return out


def _line(e: TagEntry) -> str:
    parts = [f"{t}: {e.values[t]}" for t in TAGS if e.values.get(t)]
    parts += [f"{k}: {v}" for k, v in (("archetype", e.archetype), ("archetype2", e.archetype2),
                                       ("source", e.source)) if v]
    parts += [f"{t}: {e.kit[t]}" for t in KIT_TAGS if e.kit.get(t)]
    parts.append(f"reviewed: {'true' if e.reviewed else 'false'}")
    parts += [f"{k}: true" for k, v in (("reviewed_kit", e.reviewed_kit), ("reviewed_arch", e.reviewed_arch)) if v]
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
                s.add(ChampionTag(champ_id=ids[key], role=role, reviewed=e.reviewed, archetype=e.archetype,
                                  archetype2=e.archetype2, archetype_source=e.source, kit=dict(e.kit) or None,
                                  reviewed_kit=e.reviewed_kit, reviewed_arch=e.reviewed_arch, **e.values))
                loaded += 1
        s.commit()
    return {"loaded": loaded, "unknown": sorted(unknown)}


def load_tags(db: Engine) -> dict[tuple[int, str], TagEntry]:
    """The 0-3 ratings every synergy model reads. Archetype-only lines aren't ratings: see TagEntry.rated."""
    with Session(db) as s:
        rows = s.exec(select(ChampionTag)).all()
    out = {}
    for r in rows:
        e = TagEntry({t: getattr(r, t) for t in TAGS}, r.reviewed, r.archetype, r.archetype2, r.archetype_source,
                     r.kit or {}, r.reviewed_kit, r.reviewed_arch)
        if e.rated:
            out[(r.champ_id, r.role)] = e
    return out


def load_archetypes(db: Engine) -> dict[tuple[int, str], Archetype]:
    """Written archetypes per (champ, role). Others use data/archetypes.resolve_archetype's fallback."""
    with Session(db) as s:
        rows = s.exec(select(ChampionTag).where(ChampionTag.archetype.is_not(None))).all()
    return {(r.champ_id, r.role): Archetype(r.archetype, r.archetype2, r.reviewed_arch, r.archetype_source)
            for r in rows}


def load_kit(db: Engine) -> dict[tuple[int, str], KitEntry]:
    """Kit tags per (champ, role), for engine/traits.KitTraits. Lines without any aren't included."""
    with Session(db) as s:
        rows = s.exec(select(ChampionTag)).all()
    return {(r.champ_id, r.role): KitEntry(dict(r.kit), r.reviewed_kit) for r in rows if r.kit}


def update_tag(db: Engine, path: Path, champ_id: int, role: str, values: Mapping[str, int],
               reviewed: bool, archetype: str | None = None, archetype2: str | None = None,
               kit: Mapping[str, int] | None = None, reviewed_kit: bool | None = None,
               reviewed_arch: bool | None = None) -> TagEntry:
    """Save one champion-role's tags (missing tags = 0) to the file and the table. `archetype` and `archetype2`:
    None keeps what's written, "" clears it. A changed archetype loses its source: it's no longer the guide's.
    `kit`: None keeps what's written, otherwise replaces it. A kit rating belongs to the champion, so `kit` and
    `reviewed_kit` apply to all of its lines. `reviewed` covers the synergy tags only; the other two flags: None keeps."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    with Session(db) as s:
        champ = s.get(Champion, champ_id)
    if champ is None:
        raise ValueError(f"unknown champion {champ_id}")
    raw = load_tags_yaml(path)
    old = raw.get(champ.key, {}).get(role)
    primary = (old.archetype if old else None) if archetype is None else (archetype or None)
    secondary = (old.archetype2 if old else None) if archetype2 is None else (archetype2 or None)
    if primary is None:
        secondary = None
    source = old.source if old and old.archetype == primary else None
    where = f"{champ.key}.{role}"
    lines = raw.get(champ.key, {})
    rated_kit = next((e for e in lines.values() if e.kit), None)
    kit_values = dict(rated_kit.kit if rated_kit else {}) if kit is None else dict(kit)
    if reviewed_kit is None:
        reviewed_kit = bool(rated_kit and rated_kit.reviewed_kit)
    if reviewed_arch is None:
        reviewed_arch = bool(old and old.reviewed_arch)
    wrong = (set(values) & set(KIT_TAGS)) | (set(kit_values) - set(KIT_TAGS))
    if wrong:
        raise ValueError(f"{where}: unknown tags {sorted(wrong)}")
    entry = _entry({**values, **kit_values, "reviewed": reviewed, "reviewed_kit": reviewed_kit,
                    "reviewed_arch": reviewed_arch, "archetype": primary, "archetype2": secondary,
                    "source": source}, where, role)
    raw.setdefault(champ.key, {})[role] = entry
    for r, e in raw[champ.key].items():
        raw[champ.key][r] = replace(e, kit=entry.kit, reviewed_kit=entry.reviewed_kit)
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
