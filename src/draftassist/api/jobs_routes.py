"""The Crawl page: run the batch jobs one at a time (jobs.py) and see how far the crawl has got."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request
from sqlalchemy import text

from draftassist.api.deps import db_of

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


def crawl_summary(db, patch: str | None = None) -> dict:
    """Matches per tier: `patch` (default the current one), and every patch; with how many have their timeline, and the
    patches stored, newest first."""
    from draftassist.data.crawler import current_patch, display_patch

    current = None
    try:
        current = current_patch(db)
    except RuntimeError:
        pass
    patch = patch or current
    with db.connect() as conn:
        stored = [r[0] for r in conn.execute(text("SELECT DISTINCT patch FROM crawled_matches"))]
        rows = conn.execute(text(
            "SELECT bracket, COUNT(*), SUM(CASE WHEN patch = :p THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN timeline_version >= 2 THEN 1 ELSE 0 END) FROM crawled_matches GROUP BY bracket"),
            {"p": patch or ""}).all()
    order = ["iron", "bronze", "silver", "gold", "platinum", "emerald", "diamond", "master"]
    brackets = sorted(({"bracket": b, "all": n, "patch": p or 0, "timelines": t or 0} for b, n, p, t in rows),
                      key=lambda r: order.index(r["bracket"]) if r["bracket"] in order else 99)
    key = lambda p: tuple(int(x) for x in p.split(".")) if p and all(x.isdigit() for x in p.split(".")) else (0,)
    patches = sorted({*stored, *([current] if current else [])}, key=key, reverse=True)
    return {"patch": patch, "patch_label": display_patch(patch) if patch else None, "current": current,
            "patches": patches, "brackets": brackets,
            "total": sum(r["all"] for r in brackets), "total_patch": sum(r["patch"] for r in brackets)}


def _summary_db(svc, database: str):
    """The main database, or the Master+ one (its own file beside it) when asked and it exists."""
    db = db_of(svc)
    if database == "master":
        from pathlib import Path

        from draftassist.data.master import master_path
        from draftassist.db.session import get_engine

        path = master_path(Path(db.url.database))
        if path.is_file():
            return get_engine(path)
    return db


@router.get("/api/jobs")
def get_jobs(request: Request, patch: str | None = None, database: str = "main") -> dict:
    """Jobs, the running one's live progress, and matches per tier for `patch` in the main or Master+ database
    (the page follows the running crawl's patch and database unless you pick others)."""
    svc: Services = request.app.state.services
    status = svc.jobs().status()
    return {**status, "crawl": crawl_summary(_summary_db(svc, database), patch or None), "database": database,
            "master_exists": _summary_db(svc, "master") is not db_of(svc),
            "riot_key_set": bool(svc.settings and svc.settings.has_riot_api_key)}


@router.post("/api/jobs/start")
def start_job(request: Request, job: str = Body(..., embed=True), hours: float | None = Body(None, embed=True),
              options: dict | None = Body(None, embed=True)) -> dict:
    svc: Services = request.app.state.services
    try:
        svc.jobs().start(job, hours, options)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    return get_jobs(request)


@router.post("/api/jobs/stop")
def stop_job(request: Request) -> dict:
    request.app.state.services.jobs().stop()
    return get_jobs(request)
