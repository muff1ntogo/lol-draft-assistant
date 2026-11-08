"""Datasets and the user's data selection (docs/shared-data.md, Stage 4)."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter()


def _fits_from(db) -> str:
    from draftassist.data.datasets import WINDOW_PREFIX, active_window, fits_label, load_datasets
    from draftassist.data.synergy import latest_window

    window = active_window(db)
    if window is not None:
        ds = next((d for d in load_datasets(db) if WINDOW_PREFIX + d.dataset_id == window), None)
        if ds is not None:
            return fits_label(ds)
    return fits_label(None, latest_window(db))


@router.get("/api/datasets")
def get_datasets(request: Request) -> dict:
    from draftassist.data.datasets import load_datasets, load_selection
    from draftassist.data.counts import crawled_patches

    svc = request.app.state.services
    db = svc.db
    from sqlalchemy import text

    with db.connect() as conn:
        patches = crawled_patches(conn)
        players = sorted({r[0] for r in conn.execute(text("SELECT bracket FROM players WHERE active = 1"))})
        tiers = [r[0] for r in conn.execute(text("SELECT DISTINCT bracket FROM cnt_bracket ORDER BY 1"))]
    return {"datasets": [d.as_dict() for d in load_datasets(db)], "selection": load_selection(db).as_dict(),
            "fits_from": _fits_from(db), "patches": patches, "player_brackets": players, "tiers": tiers,
            "role": svc.account().role,
            "pending": bool(getattr(svc, "selection_pending", False))}


@router.put("/api/selection")
def put_selection(request: Request, body: dict = Body(...)) -> dict:
    """A user's choice of patches, tiers and queues. Applied at once between drafts, else after the draft."""
    from draftassist.data.datasets import Selection, apply_selection, save_selection

    svc = request.app.state.services
    if svc.account().role != "user":
        raise HTTPException(403, "the data selection is for teammates who pull the shared data")
    try:
        sel = Selection.from_dict(body)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, str(e)) from None
    save_selection(svc.db, sel)
    if svc.in_draft():
        svc.selection_pending = True
        return {"selection": sel.as_dict(), "fits_from": _fits_from(svc.db), "reload": "deferred"}
    out = apply_selection(svc.db)
    svc.selection_pending = False
    return {**out, "reload": svc.refresh_stats()}


@router.post("/api/datasets")
def add_dataset(request: Request, dataset_id: str = Body(...), name: str = Body(...), scope: str = Body(...),
                n: int | None = Body(None), patches: list[str] = Body(default_factory=list),
                half_life: float = Body(6.0)) -> dict:
    """The admin adds a dataset; its fits are computed and published with the next publish."""
    import time

    from draftassist.data.datasets import Dataset, add_dataset as add

    svc = request.app.state.services
    if svc.account().role != "admin":
        raise HTTPException(403, "only the team's admin publishes datasets")
    try:
        add(svc.db, Dataset(dataset_id.strip(), name.strip(), scope, n, tuple(patches), half_life,
                            created_at=time.time()))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return get_datasets(request)
