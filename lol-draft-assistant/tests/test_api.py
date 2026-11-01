from fastapi.testclient import TestClient

from draftassist.api.main import app
from draftassist.engine.model import ROLES


def test_prep_lane_bans_returns_a_table_per_role():
    resp = TestClient(app).get("/api/prep/lane-bans")
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
