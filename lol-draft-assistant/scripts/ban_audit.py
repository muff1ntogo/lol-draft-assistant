"""Ban value audit: the top-10 ban candidates with every term's contribution, on the Phase 1
simulated draft (synthetic data, tournament draft, blue side), at Ban 1 (step 1) and Ban 2 (step 14).

    uv run python scripts/ban_audit.py          # synthetic data
    uv run python scripts/ban_audit.py --real   # your database (stats, pools)
    uv run python scripts/ban_audit.py --real --matchups   # the matchup model: how much it says, what it changes

Works with the engine before and after the ban-value decomposition (docs/ban-value-matchups.md):
it prints whichever breakdown the engine provides.
"""
from __future__ import annotations

from draftassist.engine.engine import THEM, DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.lane_ban import team_lane_ban
from draftassist.engine.model import ROLES
from draftassist.engine.simulate import run_draft, synthetic_data
from draftassist.engine.state import DraftState


def legacy_rows(engine: DraftEngine, state: DraftState) -> list[dict]:
    """Ban value before the decomposition: rel * (P(enemy picks) * harm + gamma * 0.04 * TeamLaneBan)."""
    engine._assign_cache.clear()
    lane_cfg = engine.cfg.lane_ban
    ours, theirs = state.our_picks, state.their_picks
    ev = engine._evaluate(state)
    base = ev.terms.total
    role_probs = engine._enemy_role_probs(state, ev.their_roles)
    relevance = engine.ban_relevance(state)
    team_lane = team_lane_ban(engine._lane_ban_tables(state, ev).values())
    rows = []
    for c in engine._champs:
        if c in state.unavailable() or relevance.get(c, 0.0) < engine.cfg.min_ban_relevance:
            continue
        p = sum(role_probs.get(c, {}).values())
        their_roles = engine._assign_plus(THEM, tuple(theirs), c, list(ROLES))
        harm = base - engine._terms(ours, ev.our_roles, theirs + [c], their_roles).total
        lift = 1.0 * lane_cfg.pp_to_logodds * team_lane.get(c, 0.0)  # the old gamma, default 1.0
        rel = relevance[c]
        rows.append({"champ": engine.data.name(c), "P(pick)": p, "harm": harm, "P*harm": p * harm,
                     "TLB pp": team_lane.get(c, 0.0), "lift": lift, "rel": rel, "value": rel * (p * harm + lift)})
    return sorted(rows, key=lambda r: -r["value"])


def print_table(title: str, rows: list[dict], columns: list[str]) -> None:
    print(f"\n{title}")
    print("  " + "".join(f"{c:>11}" if i else f"{c:<12}" for i, c in enumerate(columns)))
    for r in rows[:10]:
        print("  " + "".join(f"{r[c]:>11.4f}" if i else f"{r[c]:<12}" for i, c in enumerate(columns)))


def matchup_share(data, team, enemy_bracket: str, edge_only: bool = False) -> dict[str, tuple[int, int]]:
    """Our pools vs the role's top 20 opponents: (matchups with a non-trivial estimate, |pp| > 0.5; all of them)."""
    out = {}
    enemy = data.brackets[enemy_bracket]
    for role in ROLES:
        stats = data.brackets[team.brackets[role]]
        rates = {c: s[role].pick_rate for c, s in enemy.meta.items() if role in s}
        top = sorted(rates, key=lambda c: (-rates[c], c))[:20]
        hit = total = 0
        for ours in team.pools.get(role, {}):
            for theirs in top:
                if ours == theirs:
                    continue
                total += 1
                pp = 25 * stats.matchup_edge(role, ours, theirs) if edge_only else 100 * (
                    stats.matchup_wr(role, ours, theirs) - 0.5)
                hit += abs(pp) > 0.5
        out[role] = (hit, total)
    return out


def matchup_audit(db) -> None:
    from draftassist.data.provider import load_engine_inputs

    new, old = load_engine_inputs(db), load_engine_inputs(db, use_matchup_model=False)
    enemy = DraftEngine(new.data, new.team).cfg.enemy_bracket
    print(f"\nPools vs the top 20 opponents per role, share with |pp| > 0.5 (enemy bracket {enemy})")
    print(f"  {'role':<9}{'before':>14}{'after':>14}{'after, beyond strength':>26}")
    before, after = matchup_share(old.data, old.team, enemy), matchup_share(new.data, new.team, enemy)
    edge = matchup_share(new.data, new.team, enemy, edge_only=True)
    for r in ROLES:
        cell = lambda v: f"{v[0]}/{v[1]} {v[0] / max(v[1], 1):.0%}"
        print(f"  {r:<9}{cell(before[r]):>14}{cell(after[r]):>14}{cell(edge[r]):>26}")
    for title, step, kind in (("bans, step 1", 0, "ban"), ("picks, first pick", 6, "pick")):
        lists = []
        for inputs in (old, new):
            engine = DraftEngine(inputs.data, inputs.team)
            final, _ = run_draft(engine, TOURNAMENT, "blue")
            state = DraftState(TOURNAMENT, "blue", final.actions[:step])
            got = engine.suggest_bans(state, k=10) if kind == "ban" else engine.suggest_picks(state, k=10)
            lists.append(got)
        print(f"\nTop 10 {title}: before | after")
        for i in range(10):
            a = lists[0][i] if i < len(lists[0]) else None
            b = lists[1][i] if i < len(lists[1]) else None
            print(f"  {i + 1:>2} {a.name if a else '-':<16}{b.name if b else '-':<16}{b.reason if b else ''}")


def main() -> None:
    import sys

    if "--matchups" in sys.argv:
        from draftassist.config import load_settings
        from draftassist.db.session import init_db

        matchup_audit(init_db(load_settings().db_path))
        return
    if "--real" in sys.argv:
        from draftassist.config import load_settings
        from draftassist.data.provider import load_engine_inputs
        from draftassist.db.session import init_db

        inputs = load_engine_inputs(init_db(load_settings().db_path))
        print(f"REAL DATA: {inputs.note or inputs.data_source}")
        data, team = inputs.data, inputs.team
    else:
        print("SYNTHETIC DATA")
        data, team = synthetic_data()
    engine = DraftEngine(data, team)
    final, _ = run_draft(engine, TOURNAMENT, "blue")
    for step in (0, 13):
        state = DraftState(TOURNAMENT, "blue", final.actions[:step])
        label = f"step {step + 1} ({TOURNAMENT.steps[step].label}, blue)"
        if hasattr(engine, "ban_breakdown"):
            rows = engine.ban_breakdown(state)
            print_table(f"NEW ban value, {label}: rel * discount * (a*Threat + b*Matchup + c*Comp)", rows,
                        ["champ", "Threat", "Matchup", "Comp", "a*Threat", "b*Matchup", "c*Comp", "rel", "discount",
                         "value"])
        rows = legacy_rows(engine, state)
        print_table(f"OLD ban value, {label}: rel * (P(pick)*harm + 0.04*TeamLaneBan)", rows,
                    ["champ", "P(pick)", "harm", "P*harm", "TLB pp", "lift", "rel", "value"])
        top = rows[:10]
        share = sum(abs(r["lift"]) for r in top) / max(sum(abs(r["lift"]) + abs(r["P*harm"]) for r in top), 1e-12)
        print(f"  -> in the old top 10, the lane (matchup) term is {share:.0%} of the absolute total")


if __name__ == "__main__":
    main()
