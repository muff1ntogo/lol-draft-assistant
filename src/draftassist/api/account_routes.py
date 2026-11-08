"""Settings, Account and the admin's Members (docs/shared-data.md, Stage 3). No response ever holds a token: invites are
returned once, to the admin who made them."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter()


def _svc(request: Request):
    return request.app.state.services


def require_admin(svc) -> None:
    if svc.account().role != "admin":
        raise HTTPException(403, "only the team's admin can do that")


@router.get("/api/account")
def get_account(request: Request) -> dict:
    return _svc(request).account().as_dict()


@router.post("/api/account/join")
def join(request: Request, invite: str = Body(..., embed=True)) -> dict:
    from draftassist.data.shared.account import InviteError

    svc = _svc(request)
    try:
        acc = svc.account_store.join(invite)
    except InviteError as e:
        raise HTTPException(422, str(e)) from None
    svc.sync_shared()  # pull straight away (never in a draft)
    return acc.as_dict()


@router.post("/api/account/admin")
def set_admin(request: Request, url: str = Body(...), db_token: str = Body(...), platform_token: str = Body(...),
              org: str = Body(...), database: str = Body(...), name: str = Body(...)) -> dict:
    try:
        return _svc(request).account_store.set_admin(url, db_token, platform_token, org, database, name).as_dict()
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@router.post("/api/account/sign-out")
def sign_out(request: Request) -> dict:
    return _svc(request).account_store.sign_out().as_dict()


# ---------- members (admin) ----------
def _members(request: Request):
    svc = _svc(request)
    require_admin(svc)
    try:
        return svc.members()
    except (PermissionError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from None


def _call(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from None


@router.get("/api/members")
def list_members(request: Request) -> dict:
    return {"members": _call(_members(request).list)}


@router.post("/api/members")
def add_member(request: Request, name: str = Body(..., embed=True), days: int = Body(90, embed=True)) -> dict:
    m = _members(request)
    return {"invite": _call(m.add, name, days), "members": m.list()}


@router.post("/api/members/{name}/renew")
def renew_member(request: Request, name: str) -> dict:
    m = _members(request)
    return {"invite": _call(m.renew, name), "members": m.list()}


@router.delete("/api/members/{name}")
def remove_member(request: Request, name: str) -> dict:
    """Rotates the database's keys: every remaining member gets a new invite code, shown here once."""
    m = _members(request)
    return {"reissued": _call(m.remove, name), "members": m.list()}
