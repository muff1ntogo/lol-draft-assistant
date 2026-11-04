"""Coverage report: does the crawl have enough games for the current patch?

Phase 3 acceptance: every real lane (a champion-role pair OP.GG lists with role share >= 10%) has
200+ crawled games in every bracket. Sparse matchups are listed, most frequent first.

CLI: `uv run python -m draftassist.data.coverage`
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.crawler import brackets_in_use, current_patch
from draftassist.db.models import BootstrapRoleStats, BracketTotal, Champion, ChampRoleStats, Matchup

MIN_GAMES = 200
REAL_LANE_ROLE_RATE = 0.10


@dataclass
class BracketCoverage:
    bracket: str
    matches: int
    real_lanes: int
    covered: int
    missing: list[tuple[int, str, int]] = field(default_factory=list)  # champ, role, games
    sparse_matchups: list[tuple[str, int, int, int]] = field(default_factory=list)  # role, a, b, games
    matchup_pairs: int = 0

    @property
    def ok(self) -> bool:
        return self.real_lanes > 0 and self.covered == self.real_lanes


def real_lanes(s: Session, patch: str) -> list[tuple[int, str, float]]:
    """(champ, role, pick_rate) for lanes champions are really played in, from the bootstrap
    (current patch or latest); falls back to every lane the crawl has seen."""
    patches = s.exec(select(BootstrapRoleStats.patch).distinct()).all()
    if patches:
        use = patch if patch in patches else max(patches, key=lambda v: tuple(int(x) for x in v.split(".")))
        rows = s.exec(select(BootstrapRoleStats).where(BootstrapRoleStats.patch == use,
                                                       BootstrapRoleStats.role_rate >= REAL_LANE_ROLE_RATE)).all()
        return [(r.champ_id, r.role, r.pick_rate) for r in rows]
    rows = s.exec(select(ChampRoleStats).where(ChampRoleStats.patch == patch)).all()
    return sorted({(r.champ_id, r.role, 0.0) for r in rows})


def coverage(db: Engine, patch: str, brackets=None) -> dict[str, BracketCoverage]:
    brackets = brackets or brackets_in_use(db)
    out = {}
    with Session(db) as s:
        lanes = real_lanes(s, patch)
        for b in brackets:
            total = s.get(BracketTotal, (patch, b))
            games = {(r.champ_id, r.role): r.games for r in s.exec(
                select(ChampRoleStats).where(ChampRoleStats.patch == patch, ChampRoleStats.bracket == b))}
            missing = sorted(((c, r, games.get((c, r), 0)) for c, r, _ in lanes if games.get((c, r), 0) < MIN_GAMES),
                             key=lambda t: (t[2], t[1], t[0]))
            mu_games = {(m.role, m.champ_a, m.champ_b): m.games for m in s.exec(
                select(Matchup).where(Matchup.patch == patch, Matchup.bracket == b))}
            sparse, pairs = [], 0
            for role in sorted({r for _, r, _ in lanes}):
                in_role = sorted((c, pr) for c, r, pr in lanes if r == role)
                for (a, pa), (bb, pb) in combinations(in_role, 2):
                    pairs += 1
                    n = mu_games.get((role, min(a, bb), max(a, bb)), 0)
                    if n < MIN_GAMES:
                        sparse.append((pa * pb, role, min(a, bb), max(a, bb), n))
            sparse.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
            out[b] = BracketCoverage(b, total.matches if total else 0, len(lanes), len(lanes) - len(missing),
                                     missing, [t[1:] for t in sparse], pairs)
    return out


def format_report(db: Engine, patch: str, report: dict[str, BracketCoverage], top: int = 10) -> str:
    with Session(db) as s:
        names = {c.champ_id: c.name for c in s.exec(select(Champion)).all()}
    n = lambda c: names.get(c, f"#{c}")
    lines = [f"Coverage for patch {patch} (real lane = role share >= {REAL_LANE_ROLE_RATE:.0%}; "
             f"covered = {MIN_GAMES}+ games)"]
    for cov in report.values():
        lines.append(f"\n{cov.bracket.upper():<8} {cov.matches} matches | lanes covered {cov.covered}/{cov.real_lanes}"
                     f" | sparse matchups {len(cov.sparse_matchups)}/{cov.matchup_pairs}"
                     f" | {'OK' if cov.ok else 'NOT YET'}")
        if cov.missing:
            lines.append("  thinnest lanes: " + ", ".join(f"{n(c)} {r} ({g})" for c, r, g in cov.missing[:top]))
        if cov.sparse_matchups:
            lines.append("  most-played sparse matchups: " + ", ".join(
                f"{n(a)} v {n(b)} {r} ({g})" for r, a, b, g in cov.sparse_matchups[:top]))
    lines.append(f"\nAcceptance (every real lane has {MIN_GAMES}+ games in every bracket): "
                 f"{'PASS' if report and all(c.ok for c in report.values()) else 'NOT YET'}")
    return "\n".join(lines)


def main() -> None:
    from draftassist.config import load_settings
    from draftassist.data.matchup_model import format_matchup_report
    from draftassist.db.session import init_db

    db = init_db(load_settings().db_path)
    patch = current_patch(db)
    print(format_report(db, patch, coverage(db, patch)))
    print()
    print(format_matchup_report(db))


if __name__ == "__main__":
    main()
