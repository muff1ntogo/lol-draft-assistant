"""Shared data in the app (docs/shared-data.md, Stage 2): the pull's status ("Stats from <date>") and "Sync now".
The pull itself runs in Services.sync_shared: at start, every few hours, and from here; never in a draft."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/shared/status")
def shared_status(request: Request) -> dict:
    return request.app.state.services.shared_status()


@router.post("/api/shared/sync")
def shared_sync(request: Request) -> dict:
    svc = request.app.state.services
    svc.sync_shared()
    return svc.shared_status()
