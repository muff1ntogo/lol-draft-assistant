"""Where everything taken from a match-v5 timeline comes from (docs/draft-vs-execution.md, Stage 1). All field names and
event types live here, so a Riot change breaks one module and its test.

Checked on 20 real games (16.21-16.22). `teamId` on a plate or a building is the team that lost it. Wards of type UNDEFINED
are ability objects (Yorick, Shen, Jhin, Illaoi...), not vision. A third of plates have killerId 0: no player gets them, the
team still does. Unknown values are None, never 0. Only derived numbers leave this module, never the raw timeline: it holds
PUUIDs and names.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass

MINUTE_MS = 60_000
AT_10, AT_14, AT_15, AT_25 = (m * MINUTE_MS for m in (10, 14, 15, 25))
AHEAD_GOLD = 2_000  # deaths_while_ahead: the team's gold lead at the previous minute frame

KILL, PLATE, BUILDING, MONSTER = "CHAMPION_KILL", "TURRET_PLATE_DESTROYED", "BUILDING_KILL", "ELITE_MONSTER_KILL"
WARD_PLACED, WARD_KILLED = "WARD_PLACED", "WARD_KILL"
TOWER = "TOWER_BUILDING"
DRAGON, GRUBS, HERALD, BARON = "DRAGON", "HORDE", "RIFTHERALD", "BARON_NASHOR"
REAL_WARDS = frozenset(("YELLOW_TRINKET", "SIGHT_WARD", "CONTROL_WARD", "BLUE_TRINKET"))
TEAMS = (100, 200)
SAME_LANE = {"UTILITY": "BOTTOM"}  # ADC and support share a lane: a 2v2 kill isn't a gank

PARTICIPANT_COLUMNS = ("lane_gold_15", "lane_xp_15", "lane_cs_14", "cs_15", "jungle_cs_10", "jungle_cs_15", "level_15",
                       "deaths_solo_14", "deaths_ganked_14", "deaths_other_14", "kp_15", "plates_14",
                       "wards_placed_15", "wards_killed_15")
TEAM_COLUMNS = ("gold_15", "kills_15", "towers_15", "plates_15", "dragons_15", "grubs_15", "herald_15", "gold_25",
                "dragons_after_15", "barons", "towers_after_15", "deaths_while_ahead")

# What a fixture needs and nothing more (sanitize_for_fixture)
_FRAME_FIELDS = ("totalGold", "xp", "minionsKilled", "jungleMinionsKilled", "level")
_EVENT_FIELDS = ("type", "timestamp", "killerId", "victimId", "assistingParticipantIds", "teamId", "buildingType",
                 "killerTeamId", "monsterType", "creatorId", "wardType")
_MATCH_FIELDS = ("participantId", "teamId", "teamPosition", "championId", "win")


@dataclass(frozen=True)
class TimelineStats:
    participants: Mapping[int, Mapping[str, float | None]]  # champ -> PARTICIPANT_COLUMNS
    teams: Mapping[int, Mapping[str, float | None]]  # team -> TEAM_COLUMNS


def classify_death(event: Mapping, team_of: Mapping[int, int], lane_of: Mapping[int, str]) -> str:
    """"solo": the killer alone. "ganked": two or more enemies, one from outside the victim's lane. "other": the rest,
    such as a tower or a lane's own two players."""
    victim, killer = event["victimId"], event.get("killerId", 0)
    assists = event.get("assistingParticipantIds") or []
    enemies = {p for p in (killer, *assists) if p and team_of.get(p) not in (None, team_of.get(victim))}
    if enemies == {killer} and not assists:
        return "solo"
    lane = _lane(lane_of.get(victim))
    if len(enemies) >= 2 and any(_lane(lane_of.get(e)) != lane for e in enemies):
        return "ganked"
    return "other"


def _lane(position: str | None) -> str | None:
    return SAME_LANE.get(position, position)


def _frame_at(frames: list[dict], ms: int) -> dict | None:
    return next((f for f in frames if f.get("timestamp", 0) >= ms), None)


def _sum(*values) -> float | None:
    return None if any(v is None for v in values) else sum(values)


def _diff(a, b) -> float | None:
    return None if a is None or b is None else float(a - b)


def _row(frame: dict | None, pid: int) -> dict:
    return ((frame or {}).get("participantFrames") or {}).get(str(pid)) or {}


def _cs(frame: dict | None, pid: int) -> float | None:
    row = _row(frame, pid)
    return _sum(row.get("minionsKilled"), row.get("jungleMinionsKilled"))


def _team_gold(frame: dict | None, pids: list[int]) -> float | None:
    return None if frame is None else _sum(*(_row(frame, p).get("totalGold") for p in pids))


def _other(team: int) -> int:
    return TEAMS[1] if team == TEAMS[0] else TEAMS[0]


def _event_counts(events: list[dict], pids: Mapping[int, int], team_of: Mapping[int, int], lane_of: Mapping[int, str],
                  frames: list[dict]) -> tuple[dict, dict]:
    """Everything counted from events: per participant id and per team."""
    who = {p: dict.fromkeys(("deaths_solo_14", "deaths_ganked_14", "deaths_other_14", "plates_14", "wards_placed_15",
                             "wards_killed_15", "kills_assists_15"), 0) for p in pids}
    team = {t: dict.fromkeys(("kills_15", "towers_15", "plates_15", "dragons_15", "grubs_15", "herald_15",
                              "dragons_after_15", "barons", "towers_after_15", "deaths_while_ahead"), 0) for t in TEAMS}
    stamps = [f.get("timestamp", 0) for f in frames]
    members = {t: [p for p, side in team_of.items() if side == t] for t in TEAMS}

    def ahead_at(at: int, side: int) -> bool:
        at_frame = bisect_right(stamps, at) - 1
        if at_frame < 0:
            return False
        ours, theirs = _team_gold(frames[at_frame], members[side]), _team_gold(frames[at_frame], members[_other(side)])
        return ours is not None and theirs is not None and ours - theirs >= AHEAD_GOLD

    for e in events:
        kind, at = e.get("type"), e.get("timestamp", 0)
        if kind == KILL:
            victim, killer = e.get("victimId"), e.get("killerId", 0)
            if victim in who and at < AT_14:
                who[victim][f"deaths_{classify_death(e, team_of, lane_of)}_14"] += 1
            if at < AT_15:
                if killer in who:
                    who[killer]["kills_assists_15"] += 1
                    team[team_of[killer]]["kills_15"] += 1
                for p in e.get("assistingParticipantIds") or []:
                    if p in who:
                        who[p]["kills_assists_15"] += 1
            elif victim in who and ahead_at(at, team_of[victim]):
                team[team_of[victim]]["deaths_while_ahead"] += 1
        elif kind == PLATE:
            if e.get("killerId") in who and at < AT_14:
                who[e["killerId"]]["plates_14"] += 1
            if e.get("teamId") in team and at < AT_15:
                team[_other(e["teamId"])]["plates_15"] += 1
        elif kind == BUILDING and e.get("buildingType") == TOWER and e.get("teamId") in team:
            team[_other(e["teamId"])]["towers_15" if at < AT_15 else "towers_after_15"] += 1
        elif kind == MONSTER and e.get("killerTeamId") in team:
            side, monster = team[e["killerTeamId"]], e.get("monsterType")
            if monster == DRAGON:
                side["dragons_15" if at < AT_15 else "dragons_after_15"] += 1
            elif monster == BARON:
                side["barons"] += 1
            elif at < AT_15 and monster == GRUBS:
                side["grubs_15"] += 1
            elif at < AT_15 and monster == HERALD:
                side["herald_15"] += 1
        elif kind == WARD_PLACED and at < AT_15 and e.get("wardType") in REAL_WARDS and e.get("creatorId") in who:
            who[e["creatorId"]]["wards_placed_15"] += 1
        elif kind == WARD_KILLED and at < AT_15 and e.get("wardType") in REAL_WARDS and e.get("killerId") in who:
            who[e["killerId"]]["wards_killed_15"] += 1
    return who, team


def parse_timeline(timeline: Mapping | None, match: Mapping) -> TimelineStats | None:
    """None for a missing timeline or a game that ended before 15:00: such a game has nothing to store."""
    frames = sorted((timeline or {}).get("info", {}).get("frames") or [], key=lambda f: f.get("timestamp", 0))
    at_15 = _frame_at(frames, AT_15)
    if at_15 is None:
        return None
    at_10, at_14, at_25 = _frame_at(frames, AT_10), _frame_at(frames, AT_14), _frame_at(frames, AT_25)
    players = match["info"]["participants"]
    pids = {p.get("participantId", i + 1): p["championId"] for i, p in enumerate(players)}
    team_of = {p.get("participantId", i + 1): p["teamId"] for i, p in enumerate(players)}
    lane_of = {p.get("participantId", i + 1): p.get("teamPosition") or None for i, p in enumerate(players)}
    opponent = {}
    for p, lane in lane_of.items():
        rivals = [q for q in pids if lane and lane_of[q] == lane and team_of[q] != team_of[p]]
        opponent[p] = rivals[0] if len(rivals) == 1 else None

    known = all("events" in f for f in frames)  # no event list at all: counts are unknown, not zero
    events = [e for f in frames for e in f.get("events", [])] if known else []
    who, teams = _event_counts(events, pids, team_of, lane_of, frames)

    out = {}
    for p, champ in pids.items():
        row, rival = _row(at_15, p), opponent[p]
        rival_row = _row(at_15, rival) if rival else {}
        counts = who[p]
        kills = teams[team_of[p]]["kills_15"]
        out[champ] = {
            "lane_gold_15": _diff(row.get("totalGold"), rival_row.get("totalGold")),
            "lane_xp_15": _diff(row.get("xp"), rival_row.get("xp")),
            "lane_cs_14": _diff(_cs(at_14, p), _cs(at_14, rival) if rival else None),
            "cs_15": _cs(at_15, p),
            "jungle_cs_10": _row(at_10, p).get("jungleMinionsKilled"),
            "jungle_cs_15": row.get("jungleMinionsKilled"),
            "level_15": row.get("level"),
            "kp_15": (counts["kills_assists_15"] / kills if kills else None) if known else None,
            **{c: (counts[c] if known else None) for c in ("deaths_solo_14", "deaths_ganked_14", "deaths_other_14",
                                                           "plates_14", "wards_placed_15", "wards_killed_15")},
        }
    team_out = {}
    for t in TEAMS:
        members = [p for p, side in team_of.items() if side == t]
        team_out[t] = {"gold_15": _team_gold(at_15, members), "gold_25": _team_gold(at_25, members),
                       **{c: (v if known else None) for c, v in teams[t].items()}}
    return TimelineStats(out, team_out)


def sanitize_for_fixture(timeline: Mapping, match: Mapping) -> tuple[dict, dict]:
    """A timeline and match cut down to the fields read here, with no PUUID, name, game id or position left in, so a real
    game can be committed as a test fixture."""
    frames = []
    for f in timeline["info"]["frames"]:
        rows = {pid: {k: row[k] for k in _FRAME_FIELDS if k in row} for pid, row in f.get("participantFrames", {}).items()}
        clean = {"timestamp": f["timestamp"], "participantFrames": rows}
        if "events" in f:
            clean["events"] = [{k: e[k] for k in _EVENT_FIELDS if k in e} for e in f["events"]
                               if e.get("type") in (KILL, PLATE, BUILDING, MONSTER, WARD_PLACED, WARD_KILLED)]
        frames.append(clean)
    players = [{k: p[k] for k in _MATCH_FIELDS if k in p} for p in match["info"]["participants"]]
    return {"info": {"frames": frames}}, {"info": {"gameDuration": match["info"].get("gameDuration"), "participants": players}}
