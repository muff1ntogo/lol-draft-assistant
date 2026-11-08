"""OP.GG builds for Live's Strategy tab (docs/top-lane-power.md, Stage 1).

Two tools of OP.GG's MCP server (the sanctioned channel, see docs/top-lane-power-research.md):
- `lol_get_champion_analysis`: builds per champion and lane, **filtered by tier** (checked 8 Nov 2026: `tier` takes
  iron ... diamond, master, and _plus variants): core items, boots, starters, fourth items, summoner spells, the top
  rune page, skill order and skill max, each with games, wins and pick rate. Answers in OP.GG's compact repr
  (`class X: a,b` headers, then `X(1,"two",[...])`), parsed by `parse_compact`.
- `lol_get_lane_matchup_guide`: one lane matchup (every tier together; the tool has no tier filter): the matchup's own
  builds, skill order, who has lane and solo-kill advantage, the recommended play style, and a tip. Answers in JSON.

Fetched in the background (one at a time) and cached on disk for a day, so Live never waits on OP.GG: the Strategy tab
shows OP.GG's block once it's in (`on_ready` re-publishes the screen).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_TTL_S = 86_400
GAME_MODE = "ranked"  # Solo/Duo: OP.GG's biggest sample; "flex" exists too
TIERS = {"iron", "bronze", "silver", "gold", "platinum", "emerald", "diamond", "master"}
ANALYSIS_FIELDS = [
    "data.core_items.{ids[],ids_names[],pick_rate,play,win}",
    "data.boots.{ids[],ids_names[],pick_rate,play,win}",
    "data.starter_items.{ids[],ids_names[],pick_rate,play,win}",
    "data.fourth_items[].{ids[],ids_names[],pick_rate,play,win}",
    "data.summoner_spells.{ids[],ids_names[],pick_rate,play,win}",
    "data.runes.{id,pick_rate,play,win,primary_page_name,primary_rune_names[],secondary_page_name,"
    "secondary_rune_names[],stat_mod_names[]}",
    "data.skills.{order[],pick_rate,play,win}",
    "data.skill_masteries.{ids[],pick_rate,play,win}",
]


def opgg_name(display_name: str) -> str:
    """OP.GG's UPPER_SNAKE_CASE champion name from the display name: "Lee Sin" -> LEE_SIN, "Kai'Sa" -> KAISA. (Checked:
    the server also accepts WUKONG / MONKEY_KING, CHO_GATH / CHOGATH, NUNU / NUNU_WILLUMP.)"""
    name = display_name.replace("'", "").replace(".", "").replace("&", " ")
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def opgg_tier(bracket: str | None) -> str | None:
    """Our bracket name as OP.GG's tier; None (every tier) for anything it doesn't take."""
    return bracket if bracket in TIERS else None


# ---------- OP.GG's compact repr ----------
_TOKEN = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)|(?P<name>[A-Za-z_]\w*)'
                    r'|(?P<sym>[()\[\],]))')
_HEADER = re.compile(r"^class (\w+): (.*)$", re.MULTILINE)


def parse_compact(raw: str) -> object:
    """`class Row: a,b` headers, then one expression like `Top(Row(1,"x"),[Row(2,"y")])` -> nested dicts and lists."""
    classes = {name: [f.strip() for f in fields.split(",") if f.strip()] for name, fields in _HEADER.findall(raw)}
    body = _HEADER.sub("", raw).strip()
    tokens = []
    pos = 0
    while pos < len(body):
        m = _TOKEN.match(body, pos)
        if m is None or m.end() == pos:
            if body[pos].isspace():
                pos += 1
                continue
            raise ValueError(f"OP.GG: can't read {body[pos:pos + 20]!r}")
        tokens.append(m)
        pos = m.end()
    i = 0

    def value():
        nonlocal i
        t = tokens[i]
        i += 1
        if t.group("str") is not None:
            return json.loads(t.group("str"))
        if t.group("num") is not None:
            n = t.group("num")
            return float(n) if any(c in n for c in ".eE") else int(n)
        if t.group("sym") == "[":
            items = []
            while tokens[i].group("sym") != "]":
                items.append(value())
                if tokens[i].group("sym") == ",":
                    i += 1
            i += 1
            return items
        name = t.group("name")
        if name in ("true", "false"):
            return name == "true"
        if name in ("null", "None"):
            return None
        if i < len(tokens) and tokens[i].group("sym") == "(":
            i += 1
            args = []
            while tokens[i].group("sym") != ")":
                args.append(value())
                if tokens[i].group("sym") == ",":
                    i += 1
            i += 1
            fields = classes.get(name)
            return dict(zip(fields, args)) if fields and len(fields) >= len(args) else {"_": name, "args": args}
        return name

    return value() if tokens else None


# ---------- normalising ----------
def _option(o: dict | None) -> dict | None:
    if not isinstance(o, dict):
        return None
    play, win = o.get("play") or 0, o.get("win") or 0
    out = {"names": o.get("ids_names") or [], "ids": o.get("ids") or [], "games": play,
           "wr": round(win / play, 3) if play else None, "pick_rate": o.get("pick_rate")}
    if "order" in o:
        out["order"] = o["order"]
    return out


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _runes(r: dict | None) -> dict | None:
    if not isinstance(r, dict):
        return None
    play, win = r.get("play") or 0, r.get("win") or 0
    names = r.get("primary_rune_names") or []
    return {"keystone": names[0] if names else None, "primary": r.get("primary_page_name"),
            "primary_runes": names, "secondary": r.get("secondary_page_name"),
            "secondary_runes": r.get("secondary_rune_names") or [], "shards": r.get("stat_mod_names") or [],
            "games": play, "wr": round(win / play, 3) if play else None, "pick_rate": r.get("pick_rate")}


def normalise_analysis(parsed: object) -> dict | None:
    data = (parsed or {}).get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, dict):
        return None
    spells = _option(_first(data.get("summoner_spells")))
    if spells and spells["names"] and all(isinstance(n, int) for n in spells["names"]):
        spells["names"] = []  # OP.GG sometimes repeats the ids in ids_names: the app names them from Data Dragon
    skills = _first(data.get("skills"))
    mastery = _first(data.get("skill_masteries"))
    return {
        "core": _option(_first(data.get("core_items"))), "boots": _option(_first(data.get("boots"))),
        "starter": _option(_first(data.get("starter_items"))),
        "fourth": [o for o in (_option(x) for x in data.get("fourth_items") or []) if o][:3],
        "spells": spells, "runes": _runes(_first(data.get("runes"))),
        "skill_order": (skills or {}).get("order") if isinstance(skills, dict) else None,
        "skill_max": (mastery or {}).get("ids") if isinstance(mastery, dict) else None,
        "skill_order_games": (skills or {}).get("play") if isinstance(skills, dict) else None,
    }


def normalise_matchup(raw: str) -> dict | None:
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    spells = _option(_first(data.get("summoner_spells")))
    skills = _first(data.get("skills"))
    return {
        "me": body.get("my_champion"), "opponent": body.get("opponent_champion"),
        "tip": data.get("opponent_champion_tip"), "play_style": data.get("recommended_play_style"),
        "lane_advantage": data.get("lane_advantage_champion"),
        "solo_kill_advantage": data.get("lane_solo_kill_advantage_champion"),
        "core": _option(_first(data.get("core_items"))), "boots": _option(_first(data.get("boots"))),
        "starter": _option(_first(data.get("starter_items"))), "spells": spells,
        "runes": _runes(_first(data.get("runes"))),
        "skill_order": skills.get("order") if isinstance(skills, dict) else None,
    }


# ---------- fetching, cached, in the background ----------
class OpggBuilds:
    """Analysis per (champion, lane, tier) and guides per (champion, opponent, lane), from a day-old-at-most disk cache,
    else fetched by one background thread. `get_*` never blocks: None until it's in, then `on_ready()` is called."""

    def __init__(self, cache_dir: Path, client_factory: Callable[[], object] | None = None,
                 on_ready: Callable[[], None] | None = None, clock=time.time):
        self.cache_dir = Path(cache_dir) / "opgg" / "builds"
        self._client_factory = client_factory
        self.on_ready = on_ready
        self._clock = clock
        self._lock = threading.Lock()
        self._queue: list[tuple] = []
        self._running = False
        self._failed: set[tuple] = set()  # asked this run and failed: not asked again

    def _path(self, key: tuple) -> Path:
        return self.cache_dir / ("__".join(str(k) for k in key) + ".json")

    def _cached(self, key: tuple) -> dict | None | bool:
        """The cached value (may be None: OP.GG had nothing), or False when there's no fresh entry."""
        p = self._path(key)
        if not p.is_file():
            return False
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return entry.get("value") if self._clock() - entry.get("fetched_at", 0) < CACHE_TTL_S else False

    def _get(self, key: tuple) -> dict | None:
        hit = self._cached(key)
        if hit is not False:
            return hit
        if key not in self._failed:
            self._enqueue(key)
        return None

    def analysis(self, champ_name: str, role: str, bracket: str | None) -> dict | None:
        return self._get(("analysis", opgg_name(champ_name), role, opgg_tier(bracket) or "all"))

    def matchup(self, champ_name: str, opponent_name: str, role: str) -> dict | None:
        return self._get(("matchup", opgg_name(champ_name), opgg_name(opponent_name), role))

    def _enqueue(self, key: tuple) -> None:
        with self._lock:
            if key in self._queue:
                return
            self._queue.append(key)
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._work, name="opgg-builds", daemon=True).start()

    def fetch(self, client, key: tuple) -> dict | None:
        kind = key[0]
        if kind == "analysis":
            _, name, role, tier = key
            args = {"game_mode": GAME_MODE, "champion": name, "position": role, "lang": "en_US",
                    "desired_output_fields": ANALYSIS_FIELDS}
            if tier != "all":
                args["tier"] = tier
            return normalise_analysis(parse_compact(client.call_tool("lol_get_champion_analysis", args)))
        _, name, opp, role = key
        return normalise_matchup(client.call_tool("lol_get_lane_matchup_guide", {
            "position": role, "my_champion": name, "opponent_champion": opp, "lang": "en_US"}))

    def _work(self) -> None:
        client = None
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        self._running = False
                        return
                    key = self._queue.pop(0)
                try:
                    if client is None:
                        from draftassist.data.opgg import McpClient

                        client = (self._client_factory or McpClient)()
                    value = self.fetch(client, key)
                    p = self._path(key)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps({"fetched_at": self._clock(), "value": value}), encoding="utf-8")
                except Exception:
                    log.warning("OP.GG %s unavailable", key, exc_info=True)
                    self._failed.add(key)
                    continue
                if self.on_ready is not None:
                    try:
                        self.on_ready()
                    except Exception:
                        log.exception("after an OP.GG fetch")
        finally:
            with self._lock:
                self._running = False
            close = getattr(client, "close", None)
            if close:
                close()

    def wait(self, timeout: float = 10.0) -> bool:
        """Tests: until the background fetches are done."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if not self._running and not self._queue:
                    return True
            time.sleep(0.02)
        return False
