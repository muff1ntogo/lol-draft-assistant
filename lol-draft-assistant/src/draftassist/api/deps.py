"""Small helpers shared by the API modules (main.py, synergy_routes.py)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from sqlalchemy import Engine

if TYPE_CHECKING:
    from draftassist.api.main import Services


def services(request: Request) -> Services:
    return request.app.state.services


def db_of(svc: Services) -> Engine:
    if svc.db is None:
        raise HTTPException(503, "no database configured")
    return svc.db
