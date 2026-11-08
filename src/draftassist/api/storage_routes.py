"""Local storage on the Crawl page (data/storage.py): what takes space, and the safe clears. The optimising runs as
jobs (Pack cache, Archive old patches, Optimise databases) through the Crawl page's job runner."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter()


def _paths(svc):
    from pathlib import Path

    from draftassist.config import DATA_ROOT

    db_path = Path(svc.db.url.database) if svc.db is not None else DATA_ROOT / "data" / "draftassist.sqlite"
    cache_dir = svc.settings.cache_dir if svc.settings else DATA_ROOT / "cache"
    return DATA_ROOT, db_path, cache_dir


@router.get("/api/storage")
def get_storage(request: Request) -> dict:
    from draftassist.data.storage import storage_report

    return storage_report(*_paths(request.app.state.services))


@router.post("/api/storage/clear")
def clear_storage(request: Request, what: str = Body(..., embed=True)) -> dict:
    from draftassist.data.storage import clear, storage_report

    svc = request.app.state.services
    root, db_path, cache_dir = _paths(svc)
    updating = getattr(getattr(svc, "_updater", None), "state", "idle") in ("downloading", "installing")
    try:
        out = clear(root, cache_dir, what, updating=updating)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    return {**out, **storage_report(root, db_path, cache_dir)}
