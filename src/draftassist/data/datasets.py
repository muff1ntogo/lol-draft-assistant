"""Datasets and the user's selection (docs/shared-data.md, Stage 4).

Datasets (the admin's, published in the shared `datasets` table): a patch scope with its weighting. Each gets its own
non-additive fits (matchup and synergy fits, traits, damage thresholds) under the window `ds:<id>`, computed on the admin's
machine before a publish (`fit_datasets`). The fits pool every tier and queue, as they always have; `master_plus` is the
Master+ database's own fits (the Matchup tree's).

A selection (each user's, in app_settings): patch scope, tiers to pool for each player bracket, queues, Master+ for the
tree. `apply_selection` sums the pulled counts for exactly that selection into the runtime tables under the patch key
`sel` (the per-patch rows stay as pulled, so the Matchup tree's own patch weighting is untouched) and derives the synergy
window from the counts under the closest dataset's window, beside that dataset's pulled fits; Live then reads both. The
default selection changes nothing: the admin's current window, as before.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from collections.abc import Sequence

from sqlalchemy import Engine, text

SEL = "sel"  # the patch key of the selection's runtime rows
WINDOW_PREFIX = "ds:"
SELECTION_SETTING = "data_selection"
ACTIVE_WINDOW_SETTING = "active_window"
FITTED_SETTING = "datasets_fitted"  # what the datasets were last fitted on
SCOPES = ("default", "current", "last_n", "patches", "all")
QUEUES = (420, 440)


@dataclass(frozen=True)
class Dataset:
    dataset_id: str
    name: str
    scope: str  # current, last_n, patches, all
    n: int | None = None
    patches: tuple[str, ...] = ()
    half_life: float = 6.0
    tiers: tuple[str, ...] | None = None  # None: every tier
    queues: tuple[int, ...] = QUEUES
    source: str = "main"  # or master_plus: the Master+ database's fits
    status: str = "published"
    created_at: float | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["description"] = describe(self)
        return d


DEFAULT_DATASETS = (
    Dataset("current", "Current patch", "current", half_life=0),
    Dataset("recent", "Recent", "last_n", n=6, half_life=6.0),
    Dataset("all", "Every patch", "all", half_life=0),
    Dataset("master_plus", "Master+ (Matchup tree)", "all", half_life=0, source="master_plus"),
)


@dataclass(frozen=True)
class Selection:
    scope: str = "default"  # default = the admin's current window, as before Stage 4
    n: int = 6
    patches: list[str] = field(default_factory=list)
    half_life: float = 6.0
    queues: list[int] = field(default_factory=lambda: list(QUEUES))
    tiers: dict[str, list[str]] = field(default_factory=dict)  # player bracket -> tiers pooled for it; missing = itself
    master_tree: bool = True

    @classmethod
    def from_dict(cls, d: dict | None) -> Selection:
        d = d or {}
        sel = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if sel.scope not in SCOPES:
            raise ValueError(f"unknown patch scope {sel.scope!r}")
        if not set(sel.queues) <= set(QUEUES) or not sel.queues:
            raise ValueError("queues: 420 (solo), 440 (flex) or both")
        return sel

    def as_dict(self) -> dict:
        return asdict(self)


def window_of(dataset_id: str) -> str:
    return WINDOW_PREFIX + dataset_id


def is_dataset_window(window: str | None) -> bool:
    return bool(window) and window.startswith(WINDOW_PREFIX)


def describe(ds: Dataset) -> str:
    if ds.scope == "current":
        return "current patch"
    if ds.scope == "last_n":
        return f"{ds.n} patches"
    if ds.scope == "patches":
        return "patches " + ", ".join(ds.patches)
    return "every patch"


def fits_label(ds: Dataset | None, window: str | None = None) -> str:
    if ds is None:
        return f"Fits from: the admin's current window ({window})" if window else "Fits from: none yet"
    return f"Fits from: {ds.dataset_id} ({describe(ds)})"


# ---------- patches and weights ----------
def _vkey(p: str) -> tuple[int, ...]:
    from draftassist.data.counts import version_key

    return version_key(p)


def scope_patches(scope: str, available: Sequence[str], current: str, n: int | None = None,
                  patches: Sequence[str] = ()) -> list[str]:
    """Newest first."""
    ordered = sorted({p for p in available if _vkey(p) <= _vkey(current)}, key=_vkey, reverse=True)
    if scope == "current":
        return [current]
    if scope == "last_n":
        return ordered[:max(int(n or 1), 1)]
    if scope == "patches":
        return sorted((p for p in patches if p in ordered or p == current), key=_vkey, reverse=True)
    return ordered


def scope_weights(scope: str, available: Sequence[str], current: str, n: int | None = None,
                  patches: Sequence[str] = (), half_life: float = 0.0) -> dict[str, float]:
    chosen = scope_patches(scope, available, current, n, patches)
    return {p: 0.5 ** (i / half_life) if half_life else 1.0 for i, p in enumerate(chosen)}


def _current(conn) -> str | None:
    from draftassist.data.counts import crawled_patches

    patches = crawled_patches(conn)
    return patches[-1] if patches else None


def dataset_weights(conn, window: str) -> dict[str, float]:
    """crawl_weights for a dataset window (data/synergy.crawl_weights calls this)."""
    from draftassist.data.counts import crawled_patches

    ds = next((d for d in load_datasets_conn(conn) if window_of(d.dataset_id) == window), None)
    current = _current(conn)
    if ds is None or current is None:
        return {}
    return scope_weights(ds.scope, crawled_patches(conn), current, ds.n, ds.patches, ds.half_life)


# ---------- the datasets table ----------
def _row_to_dataset(r) -> Dataset:
    return Dataset(r.dataset_id, r.name, r.scope, r.n, tuple(json.loads(r.patches or "[]")), r.half_life,
                   tuple(json.loads(r.tiers)) if r.tiers else None, tuple(json.loads(r.queues or "[420, 440]")),
                   r.source, r.status, r.created_at)


def load_datasets_conn(conn) -> list[Dataset]:
    try:
        rows = conn.execute(text("SELECT dataset_id, name, scope, n, patches, half_life, tiers, queues, source, status, "
                                 "created_at, position FROM datasets ORDER BY position, dataset_id")).all()
    except Exception:  # a database from before the table
        return []
    return [_row_to_dataset(r) for r in rows]


def load_datasets(db: Engine) -> list[Dataset]:
    with db.connect() as conn:
        return load_datasets_conn(conn)


def save_dataset(conn, ds: Dataset, position: int) -> None:
    conn.execute(text("INSERT OR REPLACE INTO datasets (dataset_id, name, scope, n, patches, half_life, tiers, queues, "
                      "source, status, created_at, position) VALUES (:id, :name, :scope, :n, :patches, :hl, :tiers, "
                      ":queues, :source, :status, :created, :pos)"),
                 {"id": ds.dataset_id, "name": ds.name, "scope": ds.scope, "n": ds.n,
                  "patches": json.dumps(list(ds.patches)), "hl": ds.half_life,
                  "tiers": json.dumps(list(ds.tiers)) if ds.tiers else None, "queues": json.dumps(list(ds.queues)),
                  "source": ds.source, "status": ds.status, "created": ds.created_at or 0.0, "pos": position})


def ensure_datasets(db: Engine) -> list[Dataset]:
    """The four defaults, on the admin's machine (kept if they exist, so edits stay)."""
    with db.begin() as conn:
        have = {d.dataset_id for d in load_datasets_conn(conn)}
        for i, ds in enumerate(DEFAULT_DATASETS):
            if ds.dataset_id not in have:
                save_dataset(conn, ds, i)
    return load_datasets(db)


def add_dataset(db: Engine, ds: Dataset) -> list[Dataset]:
    if ds.scope not in ("current", "last_n", "patches", "all"):
        raise ValueError(f"unknown patch scope {ds.scope!r}")
    if not ds.dataset_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("a dataset id is letters, digits, - and _")
    with db.begin() as conn:
        existing = load_datasets_conn(conn)
        if any(d.dataset_id == ds.dataset_id for d in existing):
            raise ValueError(f"there's already a dataset {ds.dataset_id}")
        save_dataset(conn, ds, len(existing))
    return load_datasets(db)


# ---------- the admin: fits per dataset ----------
def _fingerprint(db: Engine, datasets: list[Dataset]) -> str:
    with db.connect() as conn:
        totals = conn.execute(text("SELECT patch, bracket, queue, matches FROM cnt_bracket ORDER BY 1, 2, 3")).all()
    blob = json.dumps([[list(r) for r in totals], [d.as_dict() for d in datasets]], default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def fit_datasets(db: Engine, force: bool = False) -> bool:
    """Each main-source dataset's fits under its window: the synergy window, traits and damage thresholds (aggregation's
    steps), then the synergy and matchup fits. The synergy window is only for fitting and is dropped after (each user
    derives their own from the counts). Skipped when neither the counts nor the datasets changed. True if it fitted."""
    from draftassist.data.aggregate import _fit_matchups, _fit_synergy, _store_damage_thresholds
    from draftassist.data.synergy import aggregate_synergy, crawl_weights
    from draftassist.data.traits import aggregate_traits

    datasets = [d for d in load_datasets(db) if d.source == "main" and d.status == "published"]
    stamp = _fingerprint(db, datasets)
    with db.connect() as conn:
        row = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": FITTED_SETTING}).first()
    if not force and row is not None and json.loads(row[0]) == stamp:
        return False
    for ds in datasets:
        window = window_of(ds.dataset_id)
        with db.begin() as conn:
            weights = crawl_weights(conn, window)
            aggregate_synergy(conn, window, weights)
            aggregate_traits(conn, window, weights)
        _store_damage_thresholds(db, window)
        _fit_synergy(db, window)
        _fit_matchups(db, window)
        with db.begin() as conn:
            for table in ("synergy_pairs", "synergy_role_stats"):
                conn.execute(text(f'DELETE FROM {table} WHERE "window" = :w'), {"w": window})
    with db.begin() as conn:
        conn.execute(text("INSERT OR REPLACE INTO app_settings (key, value) VALUES (:k, :v)"),
                     {"k": FITTED_SETTING, "v": json.dumps(stamp)})
    return True


# ---------- the user's selection ----------
def load_selection(db: Engine) -> Selection:
    with db.connect() as conn:
        row = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": SELECTION_SETTING}).first()
    try:
        return Selection.from_dict(json.loads(row[0])) if row else Selection()
    except (ValueError, TypeError):
        return Selection()


def save_selection(db: Engine, sel: Selection) -> None:
    with db.begin() as conn:
        conn.execute(text("INSERT OR REPLACE INTO app_settings (key, value) VALUES (:k, :v)"),
                     {"k": SELECTION_SETTING, "v": json.dumps(sel.as_dict())})


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def closest_dataset(sel: Selection, datasets: Sequence[Dataset], available: Sequence[str],
                    current: str) -> Dataset | None:
    """Same patch scope first (for "last N", the nearest N), then the most patches in common, then the most tiers in
    common, then the dataset id. Only datasets of the main database: Master+ is the tree's own."""
    candidates = [d for d in datasets if d.source == "main" and d.status == "published"]
    if not candidates:
        return None
    mine = set(scope_patches(sel.scope, available, current, sel.n, sel.patches))
    my_tiers = {t for ts in sel.tiers.values() for t in ts} or None

    def rank(d: Dataset):
        theirs = set(scope_patches(d.scope, available, current, d.n, d.patches))
        n_gap = abs((d.n or 0) - sel.n) if d.scope == sel.scope == "last_n" else 0
        tiers = 1.0 if my_tiers is None or d.tiers is None else _jaccard(my_tiers, set(d.tiers))
        return (0 if d.scope == sel.scope else 1, n_gap, -_jaccard(mine, theirs), -tiers, d.dataset_id)

    return min(candidates, key=rank)


def _set_setting(conn, key: str, value) -> None:
    if value is None:
        conn.execute(text("DELETE FROM app_settings WHERE key = :k"), {"k": key})
    else:
        conn.execute(text("INSERT OR REPLACE INTO app_settings (key, value) VALUES (:k, :v)"),
                     {"k": key, "v": json.dumps(value)})


def active_window(db_or_conn) -> str | None:
    def read(conn):
        row = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": ACTIVE_WINDOW_SETTING}).first()
        return json.loads(row[0]) if row else None

    if isinstance(db_or_conn, Engine):
        with db_or_conn.connect() as conn:
            return read(conn)
    return read(db_or_conn)


def apply_selection(db: Engine) -> dict:
    """Derive the runtime tables for the saved selection (see the module docstring). Returns its "Fits from" label."""
    from draftassist.data.counts import _champion_roles, crawled_patches
    from draftassist.data.synergy import aggregate_synergy, latest_window

    sel = load_selection(db)
    with db.begin() as conn:
        previous = active_window(conn)
        for t in ("champ_role_stats", "matchups", "bracket_totals"):
            conn.execute(text(f"DELETE FROM {t} WHERE patch = :p"), {"p": SEL})
        if previous is not None:
            for t in ("synergy_pairs", "synergy_role_stats"):
                conn.execute(text(f'DELETE FROM {t} WHERE "window" = :w'), {"w": previous})
            _set_setting(conn, ACTIVE_WINDOW_SETTING, None)
        if sel.scope == "default":
            window = latest_window(db, conn=conn)
            if window is not None:
                _champion_roles(conn, window)
            return {"selection": sel.as_dict(), "fits_from": fits_label(None, window), "dataset": None}
        available, current = crawled_patches(conn), _current(conn)
        if current is None:
            return {"selection": sel.as_dict(), "fits_from": fits_label(None), "dataset": None}
        weights = scope_weights(sel.scope, available, current, sel.n, sel.patches, sel.half_life)
        _derive_selected(conn, sel, weights)
        ds = closest_dataset(sel, load_datasets_conn(conn), available, current)
        if ds is not None:
            window = window_of(ds.dataset_id)
            aggregate_synergy(conn, window, weights, queues=sel.queues)
            _set_setting(conn, ACTIVE_WINDOW_SETTING, window)
        _champion_roles(conn, SEL)
    return {"selection": sel.as_dict(), "fits_from": fits_label(ds), "dataset": ds.dataset_id if ds else None}


def _derive_selected(conn, sel: Selection, weights: dict[str, float]) -> None:
    """champ_role_stats, matchups and bracket_totals under patch `sel`: for each tier with data, and for each player
    bracket the tiers pooled for it; the chosen patches (weighted) and queues."""
    if not weights:
        return
    brackets = [r[0] for r in conn.execute(text("SELECT DISTINCT bracket FROM cnt_bracket"))]
    groups = {b: [b] for b in brackets}
    for player_bracket, tiers in sel.tiers.items():
        groups[player_bracket] = list(tiers)
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))
    base = {f"p{i}": p for i, p in enumerate(weights)} | {f"w{i}": w for i, w in enumerate(weights.values())}
    qmarks = ", ".join(f":q{i}" for i in range(len(sel.queues)))
    base |= {f"q{i}": q for i, q in enumerate(sel.queues)}
    for bracket, tiers in groups.items():
        tmarks = ", ".join(f":t{i}" for i in range(len(tiers)))
        p = base | {f"t{i}": t for i, t in enumerate(tiers)} | {"b": bracket, "sel": SEL}
        where = f"c.bracket IN ({tmarks}) AND c.queue IN ({qmarks})"
        w = f"WITH w(patch, weight) AS (VALUES {values}) "
        conn.execute(text(f"""{w}
            INSERT INTO bracket_totals (patch, bracket, matches)
            SELECT :sel, :b, SUM(c.matches * w.weight) FROM cnt_bracket c JOIN w ON w.patch = c.patch WHERE {where}
            HAVING COUNT(*) > 0"""), p)
        conn.execute(text(f"""{w}
            INSERT INTO champ_role_stats (patch, bracket, champ_id, role, games, wins, picks, bans, dmg_phys, dmg_magic,
                                          dmg_true, games_short, wins_short, games_long, wins_long)
            SELECT :sel, :b, c.champ_id, c.role, SUM(c.games * w.weight), SUM(c.wins * w.weight),
                   SUM(c.games * w.weight),
                   COALESCE((SELECT SUM(x.bans * w2.weight) FROM cnt_ban x JOIN w w2 ON w2.patch = x.patch
                             WHERE x.champ_id = c.champ_id AND x.bracket IN ({tmarks})
                             AND x.queue IN ({qmarks})), 0),
                   SUM(c.dmg_phys_sum * w.weight) / NULLIF(SUM(c.dmg_n * w.weight), 0),
                   SUM(c.dmg_magic_sum * w.weight) / NULLIF(SUM(c.dmg_n * w.weight), 0),
                   SUM(c.dmg_true_sum * w.weight) / NULLIF(SUM(c.dmg_n * w.weight), 0),
                   SUM(c.games_short * w.weight), SUM(c.wins_short * w.weight), SUM(c.games_long * w.weight),
                   SUM(c.wins_long * w.weight)
            FROM cnt_role c JOIN w ON w.patch = c.patch WHERE {where} GROUP BY c.champ_id, c.role"""), p)
        conn.execute(text(f"""{w}
            INSERT INTO matchups (patch, bracket, role, champ_a, champ_b, games, wins_a, lane_gold_sum, lane_xp_sum, lane_n)
            SELECT :sel, :b, c.role, c.champ_a, c.champ_b, SUM(c.games * w.weight), SUM(c.wins_a * w.weight),
                   SUM(c.lane_gold_sum * w.weight), SUM(c.lane_xp_sum * w.weight), SUM(c.lane_n * w.weight)
            FROM cnt_matchup c JOIN w ON w.patch = c.patch WHERE {where} GROUP BY c.role, c.champ_a, c.champ_b"""), p)


def active_selection(db: Engine) -> bool:
    """True while a selection other than the default is applied (its rows are under SEL)."""
    with db.connect() as conn:
        try:
            return conn.execute(text("SELECT EXISTS (SELECT 1 FROM bracket_totals WHERE patch = :p)"),
                                {"p": SEL}).scalar() == 1
        except Exception:
            return False
