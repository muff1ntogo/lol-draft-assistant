"""History: logged drafts, a game's recap and draft report, and Data Dragon art cached on disk."""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException, Request

from draftassist.api.deps import db_of
from draftassist.api.recap import history_filters, history_rows, recap
from draftassist.config import DATA_ROOT
from draftassist.data import ddragon
from draftassist.learning.report import draft_report

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


# ---------- history ----------
@router.get("/api/history")
def get_history(request: Request, queue: str | None = None, side: str | None = None,
                result: str | None = None, patch: str | None = None) -> dict:
    svc: Services = request.app.state.services
    rows = history_rows(db_of(svc), svc.catalog, queue or None, side or None, result or None, patch or None)
    return {"games": rows, "filters": history_filters(history_rows(db_of(svc), svc.catalog))}

@router.get("/api/history/stats")
def get_history_stats(request: Request, queue: str | None = None, side: str | None = None,
                      patch: str | None = None, last: int | None = None) -> dict:
    """The overview band above the list (docs/history-charts.md); the result filter doesn't apply.
    `last`: only the most recent N games (the Overview's timeframe)."""
    from draftassist.api.history_stats import history_stats

    svc: Services = request.app.state.services
    return history_stats(db_of(svc), svc.engine, svc.catalog, queue or None, side or None, patch or None, last)


@router.get("/api/history/{draft_id}")
def get_recap(request: Request, draft_id: int) -> dict:
    svc: Services = request.app.state.services
    body = recap(db_of(svc), svc.engine, svc.catalog, draft_id)
    if body is None:
        raise HTTPException(404, "no such game")
    report = draft_report(db_of(svc), svc.engine, draft_id)
    if report is not None:
        card = svc.catalog.card
        for d in report["deviations"]:
            d["picked"], d["suggested"] = card(d["picked"]), card(d["suggested"])
    body["report"] = report
    return body

@router.get("/api/art/{version}/{kind}/{key}")
def get_art(request: Request, version: str, kind: str, key: str):
    """Data Dragon champion art pinned to a patch, fetched once and cached on disk."""
    import re

    from fastapi.responses import FileResponse

    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[A-Za-z0-9]+", key) \
            or kind not in ("square", "loading"):
        raise HTTPException(404)
    svc: Services = request.app.state.services
    cache_dir = svc.settings.cache_dir if svc.settings else DATA_ROOT / "cache"
    ext = "png" if kind == "square" else "jpg"
    path = cache_dir / "ddragon" / "art" / version / kind / f"{key}.{ext}"
    if not path.is_file():
        url = (f"{ddragon.BASE_URL}/cdn/{version}/img/champion/{key}.png" if kind == "square"
               else f"{ddragon.BASE_URL}/cdn/img/champion/loading/{key}_0.jpg")
        try:
            resp = httpx.get(url, timeout=15)
        except httpx.HTTPError:
            raise HTTPException(404) from None
        if resp.status_code != 200:
            raise HTTPException(404)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})
