"""The Crawl page: run the batch jobs one at a time (jobs.py) and see how far the crawl has got."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request
from sqlalchemy import text

from draftassist.api.deps import db_of

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


def crawl_summary(db) -> dict:
    """Matches per tier: the current patch, and every patch; with how many have their timeline."""
    from draftassist.data.crawler import current_patch, display_patch

    try:
        patch = current_patch(db)
    except RuntimeError:
        patch = None
    with db.connect() as conn:
        rows = conn.execute(text(
            "SELECT bracket, COUNT(*), SUM(CASE WHEN patch = :p THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN timeline_version >= 2 THEN 1 ELSE 0 END) FROM crawled_matches GROUP BY bracket"),
            {"p": patch or ""}).all()
    order = ["iron", "bronze", "silver", "gold", "platinum", "emerald", "diamond"]
    brackets = sorted(({"bracket": b, "all": n, "patch": p or 0, "timelines": t or 0} for b, n, p, t in rows),
                      key=lambda r: order.index(r["bracket"]) if r["bracket"] in order else 99)
    return {"patch": patch, "patch_label": display_patch(patch) if patch else None, "brackets": brackets,
            "total": sum(r["all"] for r in brackets), "total_patch": sum(r["patch"] for r in brackets)}


@router.get("/api/jobs")
def get_jobs(request: Request) -> dict:
    svc: Services = request.app.state.services
    return {**svc.jobs().status(), "crawl": crawl_summary(db_of(svc)),
            "riot_key_set": bool(svc.settings and svc.settings.has_riot_api_key)}


@router.post("/api/jobs/start")
def start_job(request: Request, job: str = Body(..., embed=True), hours: float | None = Body(None, embed=True)) -> dict:
    svc: Services = request.app.state.services
    try:
        svc.jobs().start(job, hours)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    return get_jobs(request)


@router.post("/api/jobs/stop")
def stop_job(request: Request) -> dict:
    request.app.state.services.jobs().stop()
    return get_jobs(request)
