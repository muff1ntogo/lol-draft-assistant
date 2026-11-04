import json

import httpx
from conftest import SAMPLE_CHAMPIONS, SAMPLE_VERSION
from sqlmodel import select

from draftassist.data import ddragon
from draftassist.db.models import Champion


def make_client(calls: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/versions.json":
            return httpx.Response(200, json=[SAMPLE_VERSION, "15.20.1"])
        if request.url.path == f"/cdn/{SAMPLE_VERSION}/data/en_US/champion.json":
            return httpx.Response(200, json=SAMPLE_CHAMPIONS)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_latest_version_is_first_entry():
    assert ddragon.latest_version(make_client([])) == SAMPLE_VERSION


def test_parse_champions_maps_ids_keys_and_names():
    champs = {c.champ_id: c for c in ddragon.parse_champions(SAMPLE_CHAMPIONS, SAMPLE_VERSION)}
    assert champs[62].key == "MonkeyKing" and champs[62].name == "Wukong"
    assert champs[897].name == "K'Sante"
    assert all(c.patch == SAMPLE_VERSION and c.roles == [] for c in champs.values())


def test_fetch_uses_disk_cache(tmp_path):
    calls: list[str] = []
    client = make_client(calls)
    first = ddragon.fetch_champions(client, SAMPLE_VERSION, tmp_path)
    second = ddragon.fetch_champions(client, SAMPLE_VERSION, tmp_path)
    assert first == second == SAMPLE_CHAMPIONS
    assert len(calls) == 1
    cached = tmp_path / "ddragon" / SAMPLE_VERSION / "en_US" / "champion.json"
    assert json.loads(cached.read_text(encoding="utf-8")) == SAMPLE_CHAMPIONS


def test_refresh_fills_champions_for_current_patch(session, tmp_path):
    version, count = ddragon.refresh(make_client([]), session, tmp_path)
    rows = session.exec(select(Champion)).all()
    assert version == SAMPLE_VERSION
    assert count == len(rows) == len(SAMPLE_CHAMPIONS["data"])
    assert {r.patch for r in rows} == {SAMPLE_VERSION}


def test_refresh_is_idempotent_and_keeps_roles(session, tmp_path):
    client = make_client([])
    ddragon.refresh(client, session, tmp_path)
    ksante = session.get(Champion, 897)
    ksante.roles = ["top"]
    session.add(ksante)
    session.commit()

    ddragon.refresh(client, session, tmp_path)
    assert len(session.exec(select(Champion)).all()) == len(SAMPLE_CHAMPIONS["data"])
    assert session.get(Champion, 897).roles == ["top"]
