from fastapi.testclient import TestClient

from draftassist.api.main import create_app
from draftassist.engine.model import ROLES


def test_prep_lane_bans_returns_a_table_per_role(make_services):
    with TestClient(create_app(make_services())) as client:
        resp = client.get("/api/prep/lane-bans")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_source"] == "fixture" and body["note"]
    assert [r["role"] for r in body["roles"]] == list(ROLES)
    for role in body["roles"]:
        rows = role["rows"]
        assert rows and isinstance(role["mu"], float)
        assert [r["ban"] for r in rows] == sorted((r["ban"] for r in rows), reverse=True)
        assert set(rows[0]) >= {"opp", "pr", "resp", "via", "ban", "band"}
        for r in rows:
            expected = "positive" if r["ban"] > 0.05 else "negative" if r["ban"] < -0.05 else "neutral"
            assert r["band"] == expected
            assert 0 < r["pr"] <= 100


def test_root_explains_how_to_build_ui_when_missing(make_services, tmp_path):
    with TestClient(create_app(make_services(), web_dist=tmp_path)) as client:
        assert "npm run build" in client.get("/").text


def test_uvicorn_can_serve_websockets():
    # TestClient doesn't need a websocket library, but uvicorn does: without one the live socket 404s.
    import importlib.util

    assert importlib.util.find_spec("websockets") or importlib.util.find_spec("wsproto")


def test_ui_index_is_never_served_stale(make_services, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("", encoding="utf-8")
    with TestClient(create_app(make_services(), web_dist=tmp_path, stats_refresh_s=0)) as client:
        assert client.get("/").headers["cache-control"] == "no-cache"
        assert "cache-control" not in client.get("/app.js").headers


def test_hashed_ui_assets_are_cached_for_good(make_services, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "Pools-a1b2c3.js").write_text("", encoding="utf-8")
    with TestClient(create_app(make_services(), web_dist=tmp_path, stats_refresh_s=0)) as client:
        assert "immutable" in client.get("/assets/Pools-a1b2c3.js").headers["cache-control"]
        assert client.get("/").headers["cache-control"] == "no-cache"
