"""Ban value audit: the top-10 ban candidates with every term's contribution, on the Phase 1
simulated draft (synthetic data, tournament draft, blue side), at Ban 1 (step 1) and Ban 2 (step 14).

    uv run python scripts/ban_audit.py          # synthetic data
    uv run python scripts/ban_audit.py --real   # your database (stats, pools)
    uv run python scripts/ban_audit.py --real --matchups   # the matchup model: how much it says, what it changes

"""
from __future__ import annotations

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import ROLES
from draftassist.engine.simulate import run_draft, synthetic_data
from draftassist.engine.state import DraftState


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
        rows = engine.ban_breakdown(state)
        print_table(f"NEW ban value, {label}: rel * discount * (a*Threat + b*Matchup + c*Comp)", rows,
                    ["champ", "Threat", "Matchup", "Comp", "a*Threat", "b*Matchup", "c*Comp", "rel", "discount",
                     "value"])



if __name__ == "__main__":
    main()
