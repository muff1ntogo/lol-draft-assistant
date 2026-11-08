"""docs/top-lane-power.md, Stages 4-5: lane power by milestone. Meraki-shaped data here is synthetic (the real file is
checked by hand against reputations: see the doc)."""
import json
import zlib

import pytest
from sqlmodel import Session, select

from draftassist.data.meraki import ChampionKit, parse_kit
from draftassist.data.timeline_map import parse_frames
from draftassist.db.models import CrawledMatch, MatchParticipant, PowerCurve
from draftassist.db.session import init_db
from draftassist.engine import power as P


def lv(attribute, *mods):
    return {"attribute": attribute, "modifiers": [{"values": list(v), "units": [u] * len(v)} for u, v in mods]}


def raw_kit(cid, key, ad=60.0, hp=600.0, q_damage=(80, 120, 160, 200, 240)):
    stats = {"health": {"flat": hp, "perLevel": 100}, "armor": {"flat": 35, "perLevel": 4.5},
             "magicResistance": {"flat": 32, "perLevel": 2}, "attackDamage": {"flat": ad, "perLevel": 3.5},
             "attackSpeed": {"flat": 0.65, "perLevel": 3}, "attackSpeedRatio": {"flat": 0.65, "perLevel": 0},
             "attackRange": {"flat": 125, "perLevel": 0}}
    q = {"name": "Strike", "damageType": "PHYSICAL_DAMAGE", "cooldown": {"modifiers": [{"values": [6, 6, 6, 6, 6]}]},
         "effects": [{"leveling": [lv("Physical Damage", ("", q_damage), ("% bonus AD", [100] * 5))]},
                     {"leveling": [lv("Empowered Physical Damage", ("", [999] * 5))]}]}
    e = {"name": "Spin", "damageType": "MAGIC_DAMAGE", "cooldown": {"modifiers": [{"values": [10] * 5}]},
         "effects": [{"leveling": [lv("Minimum Magic Damage", ("", [40] * 5), ("%  of target's maximum health", [2] * 5)),
                                   lv("Maximum Magic Damage", ("", [80] * 5), ("%  of target's maximum health", [4] * 5))]}]}
    r = {"name": "Burn", "damageType": "MAGIC_DAMAGE", "cooldown": {"modifiers": [{"values": [100, 90, 80]}]},
         "effects": [{"leveling": [lv("Magic Damage Per Tick", ("", [20, 30, 40])),
                                   lv("Total Magic Damage", ("", [500, 750, 1000]), ("% AP", [100] * 3))]}]}
    w = {"name": "Trap", "damageType": "MAGIC_DAMAGE", "cooldown": {"modifiers": [{"values": [0.25] * 5}]},
         "effects": [{"leveling": [lv("Minion Damage", ("", [50] * 5))]},
                     {"leveling": [lv("Magic Damage", ("", [100] * 5), ("per Bard chime", [5] * 5))]}]}
    return {"id": cid, "key": key, "attackType": "MELEE", "stats": stats,
            "abilities": {"Q": [q], "W": [w], "E": [e], "R": [r]}}


def test_meraki_abilities_are_read_by_the_rules():
    kit = parse_kit(raw_kit(1, "Alpha"))
    q, w, e, r = (kit.abilities[k] for k in "QWER")
    assert len(q.parts) == 1 and q.parts[0].per_rank[0][1][0] == 80  # the empowered effect isn't added
    assert e.parts[0].per_rank == (("flat", (60.0,) * 5), ("target_hp", (3.0,) * 5))  # min / max averaged; spaces
    assert r.parts[0].per_rank[0] == ("flat", (200.0, 300.0, 400.0))  # a total over time counts 40% in a trade
    assert w.modelled is False and w.parts[0].per_rank == (("flat", (100.0,) * 5),)  # an unknown ratio is dropped
    assert kit.stats["health"] == (600.0, 100.0) and not kit.ranged


def test_growth_ranks_and_axis():
    assert P.growth(100, 10, 1) == 100 and P.growth(100, 10, 18) == pytest.approx(100 + 10 * 17 * (0.7025 + 0.0175 * 17))
    assert P.ranks_at(1, "QEW") == {"Q": 1, "W": 0, "E": 0, "R": 0}
    assert P.ranks_at(3, "QEW") == {"Q": 1, "E": 1, "W": 1, "R": 0}
    assert P.ranks_at(9, "QEW") == {"Q": 5, "E": 2, "W": 1, "R": 1}
    assert P.ranks_at(18, "QEW") == {"Q": 5, "E": 5, "W": 5, "R": 3}
    top, mid = P.milestones("top"), P.milestones("mid")
    assert [m.label for m in top][-2:] == ["lvl 19", "lvl 20"] and len(mid) == len(top) - 2
    assert P.milestone_index(mid, 7, 0) == 2 and P.milestone_index(mid, 9, 1) == 4 and P.milestone_index(mid, 12, 2) == 6


SWORD = (("FlatPhysicalDamageMod", 60.0), ("FlatHPPoolMod", 300.0))


def test_theory_ranks_the_stronger_kit_higher_and_finds_spikes():
    kits = {1: parse_kit(raw_kit(1, "Strong", ad=75)), 2: parse_kit(raw_kit(2, "Weak", ad=50, hp=520)),
            3: parse_kit(raw_kit(3, "Mid", q_damage=(20, 30, 40, 50, 60)))}
    builds = {c: P.Build((SWORD, SWORD, SWORD)) for c in kits}
    builds[3] = P.Build(((("FlatPhysicalDamageMod", 200.0),), SWORD, SWORD))  # a huge first item
    curves = P.theory_curves(kits, builds, "mid")
    assert all(curves[1][i] > curves[2][i] for i in range(len(curves[1])))
    assert sum(curves[c][0] for c in curves) == pytest.approx(3.0)  # relative to the role's mean
    item1 = [m.label for m in P.milestones("mid")].index("item 1")
    assert item1 in P.spikes(curves)[3]


def test_slope_and_combine():
    assert P.fit_slope([(1.2, 40.0, 100), (0.8, -40.0, 100)]) == pytest.approx(200.0)
    assert P.fit_slope([]) == P.DEFAULT_SLOPE
    assert P.combine(1.1, None, 0, 200) == pytest.approx(20.0)  # theory only
    assert P.combine(1.1, 60.0, P.SHRINK_MINUTES, 200) == pytest.approx(40.0)  # half way
    assert P.combine(None, None, 0, 200) is None


def test_summary_words():
    def curve(vals, spike_at=()):
        return [{"label": f"m{i}", "combined": v, "spike": i in spike_at, "n": 1000} for i, v in enumerate(vals)]

    s = P.power_summary(curve([10, 10, -5, -5], spike_at=(1,)), curve([0, 0, 0, 0]))
    assert s["lines"][0] == "Your spikes: m1." and "you from m0 to m1; they from m2 to m3" in s["lines"][1]
    assert P.power_summary(curve([5, 5]), curve([0, 0]))["lines"] == ["You are stronger at every milestone."]


# ---------- Stage 5: frames from the timeline, and the job ----------
def test_frames_and_kills_from_a_timeline():
    match = {"info": {"participants": [{"participantId": 1, "championId": 10}, {"participantId": 2, "championId": 20}]}}
    frame = lambda pid, lvl, gold: {"level": lvl, "totalGold": gold, "xp": 0, "position": {"x": 1, "y": 2},  # noqa: E731
                                    "championStats": {"health": 500, "healthMax": 600, "armor": 30, "magicResist": 30,
                                                      "attackDamage": 60, "abilityPower": 0}}
    tl = {"info": {"frames": [
        {"timestamp": 0, "participantFrames": {"1": frame(1, 1, 500), "2": frame(2, 1, 500)}, "events": []},
        {"timestamp": 60000, "participantFrames": {"1": frame(1, 2, 700), "2": frame(2, 2, 650)},
         "events": [{"type": "CHAMPION_KILL", "timestamp": 59000, "killerId": 1, "victimId": 2,
                     "assistingParticipantIds": []}]}]}}
    out = parse_frames(tl, match)
    assert out[10]["frames"][1] == [2, 700, 0, 1, 2, 500, 600, 30, 30, 60, 0]
    assert out[10]["kills"] == [[59, 20, True, 1, 1]] and out[20]["kills"] == []


def write_power_static(cache):
    base = cache / "ddragon" / "16.21.1" / "en_US"
    base.mkdir(parents=True)
    item = lambda n, stats: {"name": n, "gold": {"total": 3000}, "tags": ["Damage"], "depth": 3, "stats": stats}  # noqa: E731
    (base / "item.json").write_text(json.dumps({"data": {
        "3001": item("Sword A", {"FlatPhysicalDamageMod": 60, "FlatHPPoolMod": 300}),
        "3002": item("Sword B", {"FlatPhysicalDamageMod": 50, "FlatHPPoolMod": 250}),
        "3003": item("Sword C", {"FlatPhysicalDamageMod": 40, "FlatArmorMod": 40})}}))


def seed_lane(db, n_matches, a=10, b=20, gain=100):
    """`a` gains `gain` gold a minute over `b` in top, every match; both build Sword A, B, C at minutes 8, 14, 20."""
    from draftassist.data.timelines import store_build_events

    with Session(db) as s:
        for i in range(n_matches):
            mid = f"P{i}"
            s.add(CrawledMatch(match_id=mid, patch="16.21", bracket="gold", queue_id=420, game_start_ms=i))
            s.flush()
            for champ, team, sign in ((a, 100, 1), (b, 200, -1)):
                s.add(MatchParticipant(match_id=mid, champ_id=champ, team_id=team, role="top", win=sign > 0))
                frames = [[min(1 + m // 1, 18), 500 + 400 * m + (gain * m if sign > 0 else 0), 0, 0, 0, 0, 0, 0, 0, 0, 0]
                          for m in range(26)]
                s.connection().exec_driver_sql(
                    "INSERT INTO match_frames (match_id, champ_id, frames_z, kills_json) VALUES (?, ?, ?, '[]')",
                    (mid, champ, zlib.compress(json.dumps(frames).encode())))
                store_build_events(s.connection(), mid, {champ: {
                    "purchases": [[3001, 480], [3002, 840], [3003, 1200]], "skills": "QWEQQRQEQEREEWWRWW",
                    "levels": {lvl: 60 * (lvl - 1) for lvl in range(2, 19)}}})
        s.commit()


def test_the_job_measures_and_combines(tmp_path):
    from draftassist.data.power import compute

    db = init_db(tmp_path / "main.sqlite")
    write_power_static(tmp_path)
    seed_lane(db, 40)
    kits = {10: parse_kit(raw_kit(10, "Alpha")), 20: parse_kit(raw_kit(20, "Beta", ad=55))}
    result = compute(db, tmp_path, "16.21", kits=kits)
    assert result["champion_roles"] == 2 and result["with_theory"] == 2
    with Session(db) as s:
        rows = {(r.champ_id, r.idx): r for r in s.exec(select(PowerCurve)).all()}
    early = rows[(10, 2)]  # lvl 6
    assert early.measured == pytest.approx(100.0) and rows[(20, 2)].measured == pytest.approx(-100.0)
    assert early.combined > 0 > rows[(20, 2)].combined and early.theory > rows[(20, 2)].theory
    assert early.label == "lvl 6" and len({k[1] for k in rows if k[0] == 10}) == len(P.milestones("top"))


def test_without_meraki_the_curves_are_measured_only(tmp_path, monkeypatch):
    import draftassist.data.meraki as M
    from draftassist.data.power import compute

    db = init_db(tmp_path / "main.sqlite")
    write_power_static(tmp_path)
    seed_lane(db, 40)
    monkeypatch.setattr(M, "load_kits", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert compute(db, tmp_path, "16.21")["with_theory"] == 0
    with Session(db) as s:
        r = s.exec(select(PowerCurve).where(PowerCurve.champ_id == 10, PowerCurve.idx == 2)).one()
    assert r.theory is None and r.combined == pytest.approx(r.measured * r.n / (r.n + P.SHRINK_MINUTES), abs=0.01)


def test_strategy_shows_the_curves(make_services, tmp_path):
    from draftassist.api.strategy import StrategyBuilds
    from draftassist.data.power import compute

    db = init_db(tmp_path / "main.sqlite")
    write_power_static(tmp_path)
    seed_lane(db, 40, a=266, b=122)  # Aatrox vs Darius: real ids the test engine knows
    compute(db, tmp_path, "16.21", kits={266: parse_kit(raw_kit(266, "Aatrox")), 122: parse_kit(raw_kit(122, "Darius"))})
    builds = StrategyBuilds(db, tmp_path, "16.21", {"top": "gold"})
    p = builds.power_for(266, "top", 122)
    assert p["ours"][2]["label"] == "lvl 6" and p["theirs"] and p["lines"]
    assert builds.power_for(999, "top", None) is None
