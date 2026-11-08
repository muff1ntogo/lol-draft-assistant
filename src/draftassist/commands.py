"""The batch jobs as subcommands of the app, so the installed DraftAssistant.exe can run them without Python or uv.

    DraftAssistant.exe crawl --hours 8        same as: uv run python -m draftassist.data.crawl --hours 8
    DraftAssistant.exe import-data C:\\path\\to\\lol-draft-assistant [--with-cache] [--force]

`draftassist <command> ...` does the same from a source checkout.
"""
from __future__ import annotations

import importlib
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

# subcommand -> (module, what it does). Each module's main() reads sys.argv, as `python -m` would give it.
COMMANDS: dict[str, tuple[str, str]] = {
    "crawl": ("draftassist.data.crawl", "crawl ranked matches (overnight; resumable)"),
    "fit": ("draftassist.data.execution_model", "fit the expectations and state model"),
    "learn": ("draftassist.learning.jobs", "the learning job: link players, history, comfort"),
    "split": ("draftassist.learning.execution", "work out every logged game's draft / early / conversion again"),
    "postgame": ("draftassist.data.postgame", "confirm logged games via match-v5 and store their timelines"),
    "import-games": ("draftassist.data.import_games", "import games we played together that History missed"),
    "ddragon": ("draftassist.data.ddragon", "champion names and icons (each patch)"),
    "opgg": ("draftassist.data.opgg", "OP.GG bootstrap stats (each patch)"),
    "coverage": ("draftassist.data.coverage", "crawl coverage and the state model's calibration"),
    "power": ("draftassist.data.power", "lane power curves by level and item milestone (after a crawl)"),
    "backfill": ("draftassist.data.backfill", "damage, game length and traits from the Riot cache"),
    "pools": ("draftassist.data.pools", "import pools from a file: pools import pools.txt"),
    "scout-check": ("draftassist.data.scout", "which enemy identity fields recorded champ selects carry, per queue"),
    "cache-pack": ("draftassist.data.cache_pack", "pack the Riot cache's .json files into cache.sqlite (resumable)"),
    "cache-archive": ("draftassist.data.cache_archive", "move old patches' cached matches into archive files"),
    "db-optimize": ("draftassist.db.optimize", "smaller databases: --dry-run measures first (stop jobs to apply)"),
    "publish": ("draftassist.data.shared.publish", "publish the crawl's counts and fits to the shared database (admin)"),
    "pull": ("draftassist.data.shared.pull", "pull the shared counts and fits into this machine's database"),
    "shared-inspect": ("draftassist.data.shared.published", "show what is published in the shared database"),
    "account": ("draftassist.data.shared.account_command", "sign in: status | join <invite> | admin | sign-out"),
    "members": ("draftassist.data.shared.members_command", "the team's members (admin): list | add | renew | remove"),
}
IMPORT = "import-data"
DATABASES = ("data/draftassist.sqlite", "data/master.sqlite")
FILES = (".env", "data/champion_tags.yaml", "data/style_rules.yaml", "data/team_rules.yaml", "data/roster.json")


def is_command(argv: Sequence[str]) -> bool:
    return bool(argv) and (argv[0] in COMMANDS or argv[0] == IMPORT)


def account_store():
    """The signed-in account (data/shared/account.py); tests replace this."""
    from draftassist.data.shared.account import default_store

    return default_store()


def run(argv: Sequence[str], prog: str = "draftassist") -> int:
    name, rest = argv[0], list(argv[1:])
    if name == IMPORT:
        return import_data_main(rest)
    from draftassist.data.shared.account import ADMIN_COMMANDS

    if name in ADMIN_COMMANDS and account_store().load().role == "user":
        print(f"`{name}` is for the team's admin: this machine is signed in as a user, and gets the crawl's data by "
              "pulling it (Crawl page, Sync now).")
        return 2
    from draftassist.config import ensure_user_files

    ensure_user_files()
    module = importlib.import_module(COMMANDS[name][0])
    saved = sys.argv
    sys.argv = [f"{prog} {name}", *rest]
    try:
        code = module.main()
    finally:
        sys.argv = saved
    return int(code or 0)


def usage() -> str:
    lines = ["Jobs (run from a console):"]
    lines += [f"  {name:<10} {about}" for name, (_, about) in COMMANDS.items()]
    lines.append(f"  {IMPORT:<10} copy the database, key and tags from a source checkout into this app's data folder")
    return "\n".join(lines)


# ---------- moving to the installed app ----------
def _copy_sqlite(src: Path, dst: Path) -> None:
    """SQLite's own backup: consistent even with WAL files beside it, which a plain file copy isn't."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


USER_SHARED_TABLES = {
    "crawled_matches", "match_participants", "match_team_state", "match_frames",
    "match_builds", "match_build_events", "match_draft_logodds", "matchups",
    "champ_role_stats", "bracket_totals", "synergy_pairs", "synergy_role_stats",
    "cnt_bracket", "cnt_role", "cnt_ban", "cnt_matchup", "cnt_pair", "cnt_build",
    "cnt_order", "cnt_lane", "champion_traits", "matchup_fits", "synergy_fits",
    "execution_fits", "damage_thresholds", "power_curves", "datasets",
}


def _remove_shared_data(db: Path) -> None:
    """Leave a user's local tables in place while removing imported crawl data."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in sorted(USER_SHARED_TABLES):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
                conn.execute(f'DELETE FROM "{table}"')
        conn.commit()
    finally:
        conn.close()


def import_data(src: Path, dst: Path, with_cache: bool = False, force: bool = False, user: bool = False) -> list[str]:
    """Copy local data into the app folder.

    A signed-in user imports only local data; shared crawl tables are supplied by
    Turso and must not overwrite the user's pulled data. Solo/admin imports keep
    the historical behavior and copy both local databases.
    """
    src, dst = Path(src), Path(dst)
    if not (src / DATABASES[0]).is_file():
        raise FileNotFoundError(f"no database at {src / DATABASES[0]}: is that the lol-draft-assistant folder?")
    if (dst / DATABASES[0]).exists() and not force:
        raise FileExistsError(f"{dst / DATABASES[0]} already exists: add --force to replace it")
    copied = []
    # Signed-in users receive shared data from Turso; solo/admin installs keep
    # the historical two-database import behavior.
    database_paths = (DATABASES[0],)
    master = src / DATABASES[1]
    if not user and master.is_file():
        probe = sqlite3.connect(master)
        try:
            has_master_data = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name IN ('match_participants', 'matchups', 'champ_role_stats')"
            ).fetchone() is not None
        finally:
            probe.close()
        if has_master_data:
            database_paths += (DATABASES[1],)
    for rel in database_paths:
        if (src / rel).is_file():
            target = dst / rel
            for suffix in ("", "-wal", "-shm"):  # stale WAL files would be replayed into the new copy
                Path(f"{target}{suffix}").unlink(missing_ok=True)
            _copy_sqlite(src / rel, target)
        if user and rel == DATABASES[0]:
            _remove_shared_data(target)
        copied.append(rel)
    for rel in FILES:
        if (src / rel).is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst / rel)
            copied.append(rel)
    if with_cache and (src / "cache").is_dir():
        shutil.copytree(src / "cache", dst / "cache", dirs_exist_ok=True)
        copied.append("cache/")
    return copied


def import_data_main(argv: Sequence[str]) -> int:
    import argparse

    from draftassist.config import DATA_ROOT

    parser = argparse.ArgumentParser(prog=f"draftassist {IMPORT}",
                                     description="Copy a source checkout's data into this app's data folder")
    parser.add_argument("checkout", type=Path, help="the lol-draft-assistant folder (the one with data/ and .env)")
    parser.add_argument("--with-cache", action="store_true", help="also copy cache/ (Riot responses; can be large)")
    parser.add_argument("--force", action="store_true", help="replace a database already in the data folder")
    parser.add_argument("--user", action="store_true", help="import local data only; shared data comes from Turso")
    args = parser.parse_args(argv)
    if args.checkout.resolve() == DATA_ROOT.resolve():
        print(f"{DATA_ROOT} is already this app's data folder: nothing to copy")
        return 0
    try:
        copied = import_data(args.checkout, DATA_ROOT, args.with_cache, args.force, args.user)
    except (FileNotFoundError, FileExistsError) as e:
        print(e)
        return 1
    print(f"Copied into {DATA_ROOT}:\n  " + "\n  ".join(copied))
    print("Stop any running crawl first next time: a database copied mid-write is consistent but misses what came after.")
    return 0
