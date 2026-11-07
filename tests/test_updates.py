"""One-click updates (updates.py) and the first-run setup (first_run.py)."""
import hashlib
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from draftassist.api.main import create_app
from draftassist.first_run import bootstrap, needs_bootstrap, save_riot_key
from draftassist.updates import ASSET, INSTALL_ARGS, LATEST_URL, Updater, build_info, latest_release, parse_version

INSTALLER = b"MZ" + b"\0" * 1000  # stands in for DraftAssistant-Setup.exe
DOWNLOAD = "https://github.com/x/releases/download/v0.1.43/DraftAssistant-Setup.exe"


def release(version="0.1.43", digest=None, size=len(INSTALLER), asset=ASSET):
    digest = digest if digest is not None else "sha256:" + hashlib.sha256(INSTALLER).hexdigest()
    return {"tag_name": f"v{version}", "body": "What's new", "assets": [
        {"name": asset, "browser_download_url": DOWNLOAD, "digest": digest, "size": size}]}


def fake_github(latest=None, status=200, body=INSTALLER, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if str(request.url) == LATEST_URL:
            if status != 200:
                return httpx.Response(status)
            return httpx.Response(200, json=latest or release())
        if str(request.url) == DOWNLOAD:
            return httpx.Response(200, content=body)
        return httpx.Response(404)
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def updater(tmp_path, current="0.1.42", **kw):
    launched, exited = [], []
    u = Updater(current=current, frozen=True, platform="win32", download_dir=tmp_path,
                launch=launched.append, exit_app=lambda: exited.append(True), **kw)
    return u, launched, exited


# ---------- versions and releases ----------
def test_versions():
    assert parse_version("v0.1.42") == (0, 1, 42) and parse_version("0.2.0") == (0, 2, 0)
    assert parse_version("dev") is None and parse_version(None) is None
    assert parse_version("0.1.10") > parse_version("0.1.9")


def test_the_latest_release_and_its_installer():
    with fake_github()() as http:
        r = latest_release(http)
    assert r.version == "0.1.43" and r.url == DOWNLOAD and r.sha256 == hashlib.sha256(INSTALLER).hexdigest()
    assert r.notes == "What's new" and r.size == len(INSTALLER)


def test_no_release_or_no_installer_in_it():
    with fake_github(status=404)() as http:
        assert latest_release(http) is None  # none yet, or the repository is private
    with fake_github(latest=release(asset="something.zip"))() as http:
        assert latest_release(http) is None


def test_build_info_is_read_from_the_bundle(tmp_path):
    assert build_info(tmp_path) == {}
    (tmp_path / "build_info.json").write_text(json.dumps({"version": "0.1.42", "commit": "abc"}))
    assert build_info(tmp_path)["version"] == "0.1.42"


# ---------- checking ----------
def test_a_source_checkout_doesnt_update_itself(tmp_path):
    u = Updater(current=None, frozen=False, http_factory=fake_github())
    assert u.unsupported and u.check()["available"] is False and u.checked_at is None


def test_a_newer_release_is_available(tmp_path):
    u, _, _ = updater(tmp_path, http_factory=fake_github())
    status = u.check()
    assert status["available"] is True and status["latest"] == "0.1.43" and status["current"] == "0.1.42"


def test_the_same_or_older_isnt(tmp_path):
    for current in ("0.1.43", "0.2.0"):
        u, _, _ = updater(tmp_path, current=current, http_factory=fake_github())
        assert u.check()["available"] is False


def test_checks_are_spaced_out_unless_forced(tmp_path):
    calls = []
    u, _, _ = updater(tmp_path, http_factory=fake_github(calls=calls))
    u.check()
    u.check()
    assert len(calls) == 1
    u.check(force=True)
    assert len(calls) == 2


def test_no_network_is_an_error_not_a_crash(tmp_path):
    def offline():
        def handler(request):
            raise httpx.ConnectError("offline")
        return httpx.Client(transport=httpx.MockTransport(handler))
    u, _, _ = updater(tmp_path, http_factory=offline)
    status = u.check()
    assert status["error"].startswith("couldn't check") and status["available"] is False


# ---------- installing ----------
def test_install_downloads_checks_runs_silently_and_exits(tmp_path):
    u, launched, exited = updater(tmp_path, http_factory=fake_github())
    u.check()
    u.install()
    path = tmp_path / "DraftAssistant-Setup-0.1.43.exe"
    assert path.read_bytes() == INSTALLER
    assert launched == [[str(path), *INSTALL_ARGS]] and "/VERYSILENT" in INSTALL_ARGS
    assert exited == [True] and u.state == "installing"


def test_a_download_that_doesnt_match_the_checksum_is_never_run(tmp_path):
    tampered = b"MZ" + b"\1" * (len(INSTALLER) - 2)  # the right size, the wrong bytes
    u, launched, exited = updater(tmp_path, http_factory=fake_github(body=tampered))
    u.check()
    with pytest.raises(ValueError, match="checksum"):
        u.install()
    assert launched == [] and exited == [] and not list(tmp_path.glob("*.exe")) and u.state == "idle"


def test_an_incomplete_download_is_never_run(tmp_path):
    u, launched, _ = updater(tmp_path, http_factory=fake_github(body=INSTALLER[:10]))
    u.check()
    with pytest.raises(ValueError, match="incomplete"):
        u.install()
    assert launched == []


def test_nothing_to_install(tmp_path):
    u, launched, _ = updater(tmp_path, current="0.1.43", http_factory=fake_github())
    u.check()
    with pytest.raises(ValueError):
        u.install()
    assert launched == []


def test_api_never_updates_mid_draft_or_mid_game(make_services, tmp_path):
    svc = make_services()
    u, launched, _ = updater(tmp_path, http_factory=fake_github())
    svc.updater_override = u
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.post("/api/update/check").json()["available"] is True
        svc.live.gameflow_phase = "InProgress"
        assert client.post("/api/update/install").status_code == 409
        svc.live.gameflow_phase = "Lobby"
        assert client.post("/api/update/install").json()["state"] == "installing"
        assert client.get("/api/update").json()["latest"] == "0.1.43"
    assert len(launched) == 1


# ---------- first run ----------
def test_the_key_is_saved_and_other_lines_kept(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# mine\nRIOT_API_KEY=RGAPI-old-old-old-old-old\nDRAFT_FORMAT=auto\n")
    save_riot_key(" RGAPI-1234abcd-5678-90ef-aaaa-bbbbccccdddd ", env)
    text = env.read_text()
    assert text.count("RIOT_API_KEY=") == 1 and "RGAPI-1234abcd-5678-90ef-aaaa-bbbbccccdddd" in text
    assert "# mine" in text and "DRAFT_FORMAT=auto" in text and "old" not in text


def test_a_bad_key_isnt_saved(tmp_path):
    with pytest.raises(ValueError):
        save_riot_key("not a key\nEVIL=1", tmp_path / ".env")
    assert not (tmp_path / ".env").exists()


def test_bootstrap_runs_both_jobs_in_order_and_survives_a_failure():
    ran = []

    def job(argv):
        ran.append(argv[0])
        if argv[0] == "ddragon":
            raise httpx.ConnectError("offline")
        return 0

    assert bootstrap(job) == {"ddragon": "failed (ConnectError)", "opgg": "done"}
    assert ran == ["ddragon", "opgg"]


def test_champions_alone_arent_enough(engine):
    from sqlmodel import Session

    from draftassist.db.models import BootstrapRoleStats, Champion

    assert needs_bootstrap(engine)
    with Session(engine) as s:
        s.add(Champion(champ_id=1, key="Annie", name="Annie", patch="16.22.1"))
        s.commit()
    assert needs_bootstrap(engine)  # the app fetches champions at startup; the OP.GG stats are what's missing
    with Session(engine) as s:
        s.add(BootstrapRoleStats(patch="16.22", champ_id=1, role="mid", games=10, wins=5, pick_rate=0.1, ban_rate=0.0,
                                 role_rate=1.0, fetched_at=datetime.now(UTC)))
        s.commit()
    assert not needs_bootstrap(engine)


def test_a_fresh_install_sets_itself_up(make_services, engine):
    assert needs_bootstrap(engine)
    svc = make_services(db=engine)
    ran = []
    assert svc.start_bootstrap(lambda argv: ran.append(argv[0]) or 0) is True
    deadline = time.monotonic() + 5
    while svc.bootstrap["state"] != "done" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert svc.bootstrap == {"state": "done", "jobs": {"ddragon": "done", "opgg": "done"}} and ran == ["ddragon", "opgg"]


def test_setup_api_saves_the_key_and_never_echoes_it(make_services, tmp_path, monkeypatch):
    monkeypatch.setattr("draftassist.config.DATA_ROOT", tmp_path)
    svc = make_services()
    key = "RGAPI-1234abcd-5678-90ef-aaaa-bbbbccccdddd"
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.get("/api/setup").json()["riot_key_set"] is False
        assert client.post("/api/setup/riot-key", json={"key": "nope"}).status_code == 422
        body = client.post("/api/setup/riot-key", json={"key": key})
    assert body.json()["riot_key_set"] is True and key not in body.text
    assert svc.settings.riot_api_key == key and key in (tmp_path / ".env").read_text()
