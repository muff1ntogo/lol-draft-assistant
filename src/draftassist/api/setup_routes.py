"""First-run setup and one-click updates (first_run.py, updates.py)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request

from draftassist.config import DATA_ROOT

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()
IN_GAME = ("ChampSelect", "GameStart", "InProgress", "Reconnect", "WaitingForStats", "PreEndOfGame", "EndOfGame")


@router.get("/api/setup")
def get_setup(request: Request) -> dict:
    svc: Services = request.app.state.services
    from draftassist.window_chrome import window_status

    return {"riot_key_set": bool(svc.settings and svc.settings.has_riot_api_key), "bootstrap": svc.bootstrap,
            "data_folder": str(DATA_ROOT), "window": window_status()}


@router.post("/api/setup/riot-key")
def post_riot_key(request: Request, key: str = Body(..., embed=True)) -> dict:
    svc: Services = request.app.state.services
    try:
        svc.set_riot_key(key)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return get_setup(request)


@router.get("/api/update")
def get_update(request: Request) -> dict:
    return request.app.state.services.updater().status()


@router.post("/api/update/check")
def post_update_check(request: Request) -> dict:
    return request.app.state.services.updater().check(force=True)


@router.post("/api/update/install")
def post_update_install(request: Request) -> dict:
    """Never mid-draft or mid-game: the app closes while the installer runs, and the game must still be logged."""
    svc: Services = request.app.state.services
    if svc.in_draft() or svc.live.gameflow_phase in IN_GAME:
        raise HTTPException(409, "in a draft or a game: update after it")
    if (job := svc.jobs().running()) is not None:  # the installer would close it
        raise HTTPException(409, f"{job['label']} is running: stop it on the Crawl page first, or update after it")
    try:
        return svc.updater().install()
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
