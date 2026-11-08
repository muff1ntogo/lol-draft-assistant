"""Per-(patch, tier, queue) count tables (docs/shared-data.md, Stage 2): what the admin builds from per-match rows and
publishes, and from which the runtime tables are derived the same way on every machine. Strategy reads them, so a machine
without per-match rows (a teammate's) gets the same builds; the "helpful in lane" hints (decision 6)."""
import json
import random

import pytest
from sqlalchemy import text
from sqlmodel import Session

from draftassist.data.aggregate import aggregate
from draftassist.data.builds import (
    LaneStats,
    lane_hint,
    lane_hints,
    load_damage_thresholds,
    parse_build,
    store_build,
    summarize,
)
from draftassist.data.counts import COUNT_TABLES, crawled_patches, derive, refresh_counts
from draftassist.db.models import Champion, CrawledMatch, MatchBan, MatchParticipant
from draftassist.db.session import init_db
from test_strategy import ELECTROCUTE, FLEET, LUDENS, SORCS, ZHONYAS, participant

POSITIONS = ("top", "jungle", "mid", "adc", "support")


def crawl(db, n=120, seed=1, patches=("16.20", "16.21"), brackets=("gold", "iron"), queues=(420, 440)):
    """Random but complete matches: two teams, builds, damage, lane leads, bans, game lengths."""
    rnd = random.Random(seed)
    with Session(db) as s:
        for c in range(1, 41):
            s.merge(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1"))
        s.commit()
    with Session(db) as s:
        for i in range(n):
            mid = f"EUW1_{seed * 100_000 + i}"  # a second crawl (another seed) adds new matches
            s.add(CrawledMatch(match_id=mid, patch=rnd.choice(patches), bracket=rnd.choice(brackets),
                               queue_id=rnd.choice(queues), game_start_ms=i, game_duration_s=rnd.choice([1100, 1700, 2300])))
            s.flush()
            champs = rnd.sample(range(1, 41), 10)
            blue_win = rnd.random() < 0.5
            for k, c in enumerate(champs):
                team = 100 if k < 5 else 200
                lead = rnd.gauss(0, 900) if rnd.random() < 0.8 else None
                s.add(MatchParticipant(match_id=mid, champ_id=c, team_id=team, role=POSITIONS[k % 5],
                                       win=blue_win == (team == 100), dmg_phys=rnd.randint(0, 20_000),
                                       dmg_magic=rnd.randint(0, 20_000), dmg_true=rnd.randint(0, 3_000),
                                       lane_gold_15=lead, lane_xp_15=lead and lead / 2))
                ks = rnd.choice([ELECTROCUTE, FLEET])
                items = rnd.sample([LUDENS, ZHONYAS, SORCS, 3111, 3165], 3)
                store_build(s.connection(), mid, c, parse_build(participant(c, ks, (*items, 0, 0, 0))))
            for t, b in enumerate(rnd.sample(range(1, 41), 4)):
                s.add(MatchBan(match_id=mid, team_id=100 if t % 2 else 200, pick_turn=t + 1, champ_id=b))
        s.commit()


@pytest.fixture
def db(tmp_path):
    db = init_db(tmp_path / "main.sqlite")
    crawl(db)
    return db


def reference(db, patch):
    """Today's aggregation straight from per-match rows (data/aggregate.py before Stage 2)."""
    from draftassist.engine.comp import LONG_S, SHORT_S

    with db.connect() as conn:
        role = conn.execute(text("""
            SELECT cm.bracket, mp.champ_id, mp.role, COUNT(*), SUM(mp.win),
                   COALESCE((SELECT COUNT(DISTINCT mb.match_id) FROM match_bans mb JOIN crawled_matches c2
                             ON c2.match_id = mb.match_id AND c2.patch = cm.patch AND c2.bracket = cm.bracket
                             WHERE mb.champ_id = mp.champ_id), 0),
                   AVG(CAST(mp.dmg_phys AS REAL) / NULLIF(mp.dmg_phys + mp.dmg_magic + mp.dmg_true, 0)),
                   SUM(cm.game_duration_s < :s), SUM(mp.win * (cm.game_duration_s < :s)),
                   SUM(cm.game_duration_s > :l), SUM(mp.win * (cm.game_duration_s > :l))
            FROM match_participants mp JOIN crawled_matches cm ON cm.match_id = mp.match_id AND cm.patch = :p
            GROUP BY cm.bracket, mp.champ_id, mp.role ORDER BY 1, 2, 3"""), {"p": patch, "s": SHORT_S, "l": LONG_S}).all()
        lanes = conn.execute(text("""
            SELECT cm.bracket, a.role, a.champ_id, b.champ_id, COUNT(*), SUM(a.win), COALESCE(SUM(a.lane_gold_15), 0),
                   COUNT(a.lane_gold_15)
            FROM match_participants a JOIN crawled_matches cm ON cm.match_id = a.match_id AND cm.patch = :p
            JOIN match_participants b ON b.match_id = a.match_id AND b.role = a.role AND b.team_id <> a.team_id
                 AND a.champ_id < b.champ_id
            GROUP BY cm.bracket, a.role, a.champ_id, b.champ_id ORDER BY 1, 2, 3, 4"""), {"p": patch}).all()
    return role, lanes


def derived(db, patch):
    with db.connect() as conn:
        role = conn.execute(text(
            "SELECT bracket, champ_id, role, games, wins, bans, dmg_phys, games_short, wins_short, games_long, wins_long "
            "FROM champ_role_stats WHERE patch = :p ORDER BY 1, 2, 3"), {"p": patch}).all()
        lanes = conn.execute(text(
            "SELECT bracket, role, champ_a, champ_b, games, wins_a, lane_gold_sum, lane_n FROM matchups "
            "WHERE patch = :p ORDER BY 1, 2, 3, 4"), {"p": patch}).all()
    return role, lanes


def test_runtime_tables_derived_from_counts_equal_todays_aggregation(db):
    for patch in ("16.20", "16.21"):
        aggregate(db, patch, window="16.21")
        ref_role, ref_lanes = reference(db, patch)
        got_role, got_lanes = derived(db, patch)
        assert [r[:6] + r[7:] for r in got_role] == [r[:6] + r[7:] for r in ref_role]
        assert [r[6] for r in got_role] == pytest.approx([r[6] for r in ref_role])  # a mean: sum / n
        assert [r[:6] + (r[7],) for r in got_lanes] == [r[:6] + (r[7],) for r in ref_lanes]
        assert [r[6] for r in got_lanes] == pytest.approx([r[6] for r in ref_lanes])


def test_counts_are_kept_per_queue_and_sum_to_the_runtime_tables(db):
    aggregate(db, "16.21")
    with db.connect() as conn:
        queues = {r[0] for r in conn.execute(text("SELECT DISTINCT queue FROM cnt_role WHERE patch = '16.21'"))}
        assert queues == {420, 440}
        by_q = conn.execute(text("SELECT SUM(matches) FROM cnt_bracket WHERE patch = '16.21'")).scalar()
        total = conn.execute(text("SELECT SUM(matches) FROM bracket_totals WHERE patch = '16.21'")).scalar()
        crawled = conn.execute(text("SELECT COUNT(*) FROM crawled_matches WHERE patch = '16.21'")).scalar()
    assert by_q == total == crawled


def test_synergy_window_from_counts_equals_the_per_match_window(db):
    from draftassist.data.synergy import crawl_weights

    aggregate(db, "16.20", window="16.21")
    aggregate(db, "16.21")
    with db.connect() as conn:
        w = crawl_weights(conn, "16.21")
        ref = conn.execute(text(f"""
            WITH w(patch, weight) AS (VALUES {', '.join(f"('{p}', {x})" for p, x in w.items())})
            SELECT a.role, a.champ_id, b.role, b.champ_id, COUNT(*), SUM(cm_w.weight), SUM(a.win * cm_w.weight)
            FROM match_participants a
            JOIN (SELECT cm.match_id, w.weight FROM crawled_matches cm JOIN w ON w.patch = cm.patch) cm_w
                 ON cm_w.match_id = a.match_id
            JOIN match_participants b ON b.match_id = a.match_id AND b.team_id = a.team_id
                 AND (a.role < b.role OR (a.role = b.role AND a.champ_id < b.champ_id))
            GROUP BY a.role, a.champ_id, b.role, b.champ_id ORDER BY 1, 2, 3, 4""")).all()
        got = conn.execute(text("SELECT role_a, champ_a, role_b, champ_b, games, games_w, wins_w FROM synergy_pairs "
                                "WHERE \"window\" = '16.21' AND bracket = 'all' ORDER BY 1, 2, 3, 4")).all()
    assert [r[:5] for r in got] == [r[:5] for r in ref]
    assert [x for r in got for x in r[5:]] == pytest.approx([x for r in ref for x in r[5:]])


def copy_counts(src, dst):
    """What a pull gives a teammate: the count tables only, no per-match rows."""
    with src.connect() as a, dst.begin() as b:
        for t in COUNT_TABLES:
            rows = a.execute(text(f"SELECT * FROM {t}")).mappings().all()
            if rows:
                cols = list(rows[0].keys())
                b.execute(text(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                          [dict(r) for r in rows])


def test_a_machine_with_counts_alone_derives_the_same_tables_and_builds(db, tmp_path):
    aggregate(db, "16.20", window="16.21")
    aggregate(db, "16.21")
    user = init_db(tmp_path / "user.sqlite")
    copy_counts(db, user)
    with Session(user) as s:
        for c in range(1, 41):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1"))
        s.commit()
    with user.begin() as conn:
        derive(conn, ["16.20", "16.21"], "16.21")
    for t in ("champ_role_stats", "matchups", "bracket_totals", "synergy_pairs", "synergy_role_stats"):
        with db.connect() as a, user.connect() as b:
            assert sorted(a.execute(text(f"SELECT * FROM {t}")).all()) == sorted(b.execute(text(f"SELECT * FROM {t}")).all()), t
    with user.connect() as conn:
        assert crawled_patches(conn) == ["16.20", "16.21"]  # no crawled_matches here
    for champ in (1, 7, 22):
        for role in POSITIONS:
            a = summarize(db, None, "16.21", champ, role, "gold")
            b = summarize(user, None, "16.21", champ, role, "gold")
            assert (a is None) == (b is None)
            if a is not None:
                assert a == b


def test_refresh_counts_replaces_a_patch_slice_and_is_idempotent(db):
    refresh_counts(db, "16.21")
    with db.connect() as conn:
        first = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in COUNT_TABLES}
    refresh_counts(db, "16.21")
    with db.connect() as conn:
        assert {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in COUNT_TABLES} == first
    assert first["cnt_role"] > 0 and first["cnt_build"] > 0 and first["cnt_lane"] > 0


def test_damage_thresholds_are_stored_by_aggregation(tmp_path):
    from draftassist.data.builds import MIN_TEAMS

    db = init_db(tmp_path / "x.sqlite")
    crawl(db, n=MIN_TEAMS // 2 + 10, patches=("16.21",))
    aggregate(db, "16.21")
    t = load_damage_thresholds(db, "16.21")
    assert t is not None and t.teams >= MIN_TEAMS and 0 < t.ad_heavy < 1


# ---------- decision 6: "helpful in lane" hints ----------
def stats(games, wins, gold):
    """LaneStats from a list of gold leads (one per game with a lead) and the game counts."""
    return LaneStats(games, wins, len(gold), sum(gold), sum(g * g for g in gold))


def test_hint_needs_games_on_both_sides():
    rnd = random.Random(1)
    a = stats(29, 20, [rnd.gauss(800, 900) for _ in range(29)])
    total = a + stats(200, 100, [rnd.gauss(0, 900) for _ in range(200)])
    assert lane_hint(a, total) is None  # 29 games with it
    a = stats(200, 140, [rnd.gauss(800, 900) for _ in range(200)])
    total = a + stats(29, 10, [rnd.gauss(0, 900) for _ in range(29)])
    assert lane_hint(a, total) is None  # 29 without it


def test_hint_on_a_clear_lane_gold_gain():
    rnd = random.Random(2)
    a = stats(150, 75, [rnd.gauss(600, 900) for _ in range(150)])
    total = a + stats(400, 200, [rnd.gauss(0, 900) for _ in range(400)])
    h = lane_hint(a, total)
    assert h is not None and h.measure == "gold" and h.gain >= 300 and h.z >= 3


def test_no_hint_on_a_small_or_uncertain_gain():
    rnd = random.Random(3)
    small = stats(3000, 1500, [rnd.gauss(200, 900) for _ in range(3000)])  # significant, but under 300 gold
    assert lane_hint(small, small + stats(3000, 1500, [rnd.gauss(0, 900) for _ in range(3000)])) is None
    noisy = stats(40, 20, [rnd.gauss(400, 900) for _ in range(40)])  # big enough, but z < 3
    assert lane_hint(noisy, noisy + stats(40, 20, [rnd.gauss(0, 900) for _ in range(40)])) is None


def test_hint_on_a_clear_win_rate_gain_without_gold():
    a = LaneStats(2000, 1200, 0, 0.0, 0.0)  # 60% with it
    b = LaneStats(2000, 1000, 0, 0.0, 0.0)  # 50% without
    h = lane_hint(a, a + b)
    assert h is not None and h.measure == "win" and h.gain == pytest.approx(0.10) and h.z >= 3
    a = LaneStats(2000, 1040, 0, 0.0, 0.0)  # +2 points: real, but under 3
    assert lane_hint(a, a + b) is None


def seed_lane(db, n, champ, opp, keystone, gold, win_rate, start, rnd, patch="16.21"):
    with Session(db) as s:
        for i in range(start, start + n):
            mid = f"L{i}"
            s.add(CrawledMatch(match_id=mid, patch=patch, bracket="gold", queue_id=420, game_start_ms=i))
            s.flush()
            win = rnd.random() < win_rate
            lead = rnd.gauss(gold, 900)
            s.add(MatchParticipant(match_id=mid, champ_id=champ, team_id=100, role="mid", win=win, lane_gold_15=lead))
            s.add(MatchParticipant(match_id=mid, champ_id=opp, team_id=200, role="mid", win=not win, lane_gold_15=-lead))
            store_build(s.connection(), mid, champ, parse_build(participant(champ, keystone)))
            store_build(s.connection(), mid, opp, parse_build(participant(opp)))
        s.commit()


def test_lane_hints_from_counts_and_the_overall_build_is_excluded(tmp_path):
    rnd = random.Random(4)
    db = init_db(tmp_path / "h.sqlite")
    seed_lane(db, 300, 103, 61, ELECTROCUTE, 0, 0.5, 0, rnd)  # the usual page, even lane
    seed_lane(db, 120, 103, 61, FLEET, 700, 0.5, 1000, rnd)  # Fleet vs this opponent: +700 gold
    seed_lane(db, 200, 103, 268, FLEET, 0, 0.5, 2000, rnd)  # Fleet vs someone else: nothing special
    refresh_counts(db, "16.21")
    hints = lane_hints(db, "16.21", 103, "mid", 61, exclude=set())
    assert [(h.kind, h.value) for h in hints][:1] == [("rune", FLEET)]
    assert hints[0].gain >= 300 and hints[0].games == pytest.approx(120)
    assert lane_hints(db, "16.21", 103, "mid", 268, exclude=set()) == []
    assert lane_hints(db, "16.21", 103, "mid", 61, exclude={("rune", FLEET)}) == []  # already in the overall page
    assert len(lane_hints(db, "16.21", 103, "mid", 61, exclude=set())) <= 2


def test_summary_no_longer_switches_to_a_vs_opponent_build(tmp_path):
    rnd = random.Random(5)
    db = init_db(tmp_path / "s.sqlite")
    seed_lane(db, 300, 103, 61, ELECTROCUTE, 0, 0.5, 0, rnd)
    seed_lane(db, 120, 103, 61, FLEET, 700, 0.5, 1000, rnd)
    refresh_counts(db, "16.21")
    overall = summarize(db, None, "16.21", 103, "mid", "gold")
    vs = summarize(db, None, "16.21", 103, "mid", "gold", 61)
    assert vs.opponent is None and vs.keystones == overall.keystones  # the overall build, always
    assert [h.value for h in vs.hints] == [FLEET] and overall.hints == []
