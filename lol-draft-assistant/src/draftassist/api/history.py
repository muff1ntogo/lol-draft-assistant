"""History page data: logged drafts with result, predicted win %, and every action next to what we
suggested (our turns) or predicted (their picks)."""
from __future__ import annotations

import json

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.data.provider import ChampionCatalog
from draftassist.db.models import Draft, DraftAction


def history(db: Engine, catalog: ChampionCatalog, limit: int = 50) -> list[dict]:
    with Session(db) as s:
        drafts = s.exec(select(Draft).order_by(col(Draft.played_at).desc(), col(Draft.draft_id).desc()).limit(limit)).all()
        ids = [d.draft_id for d in drafts]
        actions: dict[int, list[DraftAction]] = {i: [] for i in ids}
        for a in s.exec(select(DraftAction).where(col(DraftAction.draft_id).in_(ids)).order_by(DraftAction.seq)).all():
            actions[a.draft_id].append(a)

    out = []
    for d in drafts:
        rows, ours, followed_top, in_top3, predicted, predicted_hit = [], 0, 0, 0, 0, 0
        for a in actions[d.draft_id]:
            shown = json.loads(a.suggestions_json) if a.suggestions_json else None
            if shown and shown["kind"] == "suggested":
                ours += 1
                followed_top += a.our_rank == 1
                in_top3 += a.our_rank is not None
            elif shown and shown["kind"] == "predicted" and a.type == "pick":
                predicted += 1
                predicted_hit += a.our_rank is not None
            rows.append({
                "seq": a.seq, "side": a.side, "type": a.type, "ours": a.side == d.side,
                "champ": catalog.card(a.champ_id), "our_rank": a.our_rank,
                "shown_kind": shown["kind"] if shown else None,
                "shown": [catalog.card(i["champ_id"]) for i in shown["items"]] if shown else [],
            })
        out.append({
            "draft_id": d.draft_id, "game_id": d.game_id, "queue_id": d.queue_id, "format": d.draft_format,
            "played_at": d.played_at.isoformat(), "side": d.side, "result": d.result,
            "result_source": d.result_source, "match_verified": d.match_verified,
            "pred_win_prob": d.pred_win_prob, "actions": rows,
            "summary": {"our_turns": ours, "followed_top": followed_top, "in_top3": in_top3,
                        "enemy_picks_predicted": predicted, "enemy_picks_in_our_top3": predicted_hit},
        })
    return out
