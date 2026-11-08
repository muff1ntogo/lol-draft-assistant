"""Local storage on the Crawl page: what takes space in the data folder, the safe clears, and which job optimises what.

Clearable at once: downloaded installers (cache/updates; the app downloads one per update and never needs it again) and
database backups (data/backup-*). Optimised by jobs: the Riot cache's .json files (cache-pack, about 10x smaller), old
patches' cached matches (cache-archive), the databases (db-optimize). Never deleted: the databases and the crawl's raw
matches, from which counts, fits and backfills are computed.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

CLEARABLE = ("updates", "backups")


def _size(path: Path) -> tuple[int, int]:
    """(bytes, files) under a file or folder."""
    if path.is_file():
        return path.stat().st_size, 1
    total = files = 0
    for dirpath, _, names in os.walk(path):
        for n in names:
            try:
                total += (Path(dirpath) / n).stat().st_size
                files += 1
            except OSError:
                pass
    return total, files


def _db(path: Path) -> tuple[int, int]:
    total = sum(Path(f"{path}{s}").stat().st_size for s in ("", "-wal") if Path(f"{path}{s}").exists())
    return total, 1 if path.exists() else 0


def storage_report(data_root: Path, db_path: Path, cache_dir: Path) -> dict:
    from draftassist.data.master import master_path
    from draftassist.data.response_cache import ARCHIVE_PREFIX, CACHE_FILE

    data_root, cache_dir = Path(data_root), Path(cache_dir)
    backups = [p for p in (data_root / "data").glob("backup*")] if (data_root / "data").is_dir() else []
    archives = list(cache_dir.glob(f"{ARCHIVE_PREFIX}*.sqlite"))
    rows = [
        ("database", "Main database", _db(Path(db_path)), "optimize",
         "Smaller with WITHOUT ROWID tables and unused indexes dropped (Optimise databases)"),
        ("master", "Master+ database", _db(master_path(Path(db_path))), "optimize", ""),
        ("cache_files", "Riot cache, unpacked .json files", _size(cache_dir / "riot") if (cache_dir / "riot").is_dir()
         else (0, 0), "pack", "About 10x smaller packed into cache.sqlite (Pack cache)"),
        ("cache_db", "Riot cache, packed", _db(cache_dir / CACHE_FILE), "archive",
         "Old patches' matches can move to archive files (Archive old patches)"),
        ("archives", "Riot cache, archived patches", (sum(_size(a)[0] for a in archives), len(archives)), None,
         "Still read by backfills; can be moved or zipped"),
        ("ddragon", "Champion data and icons", _size(cache_dir / "ddragon") if (cache_dir / "ddragon").is_dir()
         else (0, 0), None, ""),
        ("updates", "Downloaded installers", _size(cache_dir / "updates") if (cache_dir / "updates").is_dir()
         else (0, 0), "clear", "Not needed once installed"),
        ("backups", "Database backups", (sum(_size(b)[0] for b in backups), len(backups)), "clear",
         "Copies made before big changes"),
        ("jobs", "Job logs", _size(cache_dir / "jobs") if (cache_dir / "jobs").is_dir() else (0, 0), None, ""),
    ]
    items = [{"id": i, "label": label, "bytes": b, "files": n, "action": action, "note": note,
              "clearable": i in CLEARABLE and b > 0} for i, label, (b, n), action, note in rows]
    return {"items": items, "total": sum(i["bytes"] for i in items), "data_folder": str(data_root)}


def clear(data_root: Path, cache_dir: Path, what: str, updating: bool = False) -> dict:
    """Delete downloaded installers or database backups. Returns the bytes freed."""
    if what not in CLEARABLE:
        raise ValueError(f"only {', '.join(CLEARABLE)} can be cleared: the rest is optimised by its job")
    if what == "updates":
        if updating:
            raise RuntimeError("an update is installing: clear the installers after it")
        targets = list((Path(cache_dir) / "updates").glob("*")) if (Path(cache_dir) / "updates").is_dir() else []
    else:
        folder = Path(data_root) / "data"
        targets = list(folder.glob("backup*")) if folder.is_dir() else []
    freed = 0
    for t in targets:
        freed += _size(t)[0]
        if t.is_dir():
            shutil.rmtree(t)
        else:
            t.unlink(missing_ok=True)
    return {"cleared": what, "freed": freed}
