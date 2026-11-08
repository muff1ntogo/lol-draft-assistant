"""Stage 3 (docs/shared-data.md): sign-in by invite, roles, members, and no token anywhere it shouldn't be."""
import json
import logging
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from draftassist.data.shared.account import (
    ADMIN_COMMANDS,
    ADMIN_JOBS,
    AccountStore,
    FileSecrets,
    InviteError,
    MemorySecrets,
    PlatformApi,
    decode_invite,
    encode_invite,
    fingerprint,
    secrets_for,
)
from draftassist.data.shared.members import Members
from draftassist.data.shared.redact import redact, register_secret
from draftassist.data.shared.store import FakeSharedStore

DAY = 86_400.0
NOW = 1_793_000_000.0
URL = "libsql://team-stats-ajtan.turso.io"
FULL = "eyJhbGciOiJFZERTQSJ9.ZmFrZS1mdWxsLWFjY2Vzcy10b2tlbg.c2lnbmF0dXJlLWZ1bGw"
PLATFORM = "eyJhbGciOiJSUzI1NiJ9.ZmFrZS1wbGF0Zm9ybS10b2tlbg.c2lnbmF0dXJlLXBsYXRmb3Jt"


def store_at(tmp_path, clock=lambda: NOW):
    return AccountStore(tmp_path, MemorySecrets(), clock=clock)


class FakePlatform:
    """Turso's Platform API for one database: tokens are only valid until the next rotation."""

    def __init__(self):
        self.generation = 0
        self.minted: list[tuple[str, str, int]] = []  # (token, authorization, days)
        self.rotations = 0

    def mint(self, read_only: bool = True, days: int | None = 90) -> str:
        token = f"eyJ0eXAiOiJKV1QifQ.dG9rZW4tZ2VuLXtzZWxmLmdlbmVyYXRpb259.sig{len(self.minted)}x{self.generation}"
        self.minted.append((token, "read-only" if read_only else "full-access", days))
        return token

    def rotate(self) -> None:
        self.rotations += 1
        self.generation += 1


# ---------- invite codes ----------
def test_invite_round_trip_and_rejections():
    code = encode_invite(URL, "eyJ.read.only", "Mika", NOW + 90 * DAY)
    assert code.startswith("dav1.") and "eyJ.read.only" not in code  # encoded, not readable at a glance
    inv = decode_invite(code, now=NOW)
    assert (inv.url, inv.token, inv.name, inv.expires_at) == (URL, "eyJ.read.only", "Mika", NOW + 90 * DAY)
    assert decode_invite("  " + code + "\n", now=NOW).name == "Mika"  # pasted with whitespace
    for bad in ("", "dav2.xxx", "dav1.not-base64!!", "dav1." + "e30"):
        with pytest.raises(InviteError):
            decode_invite(bad, now=NOW)
    with pytest.raises(InviteError, match="expired"):
        decode_invite(code, now=NOW + 91 * DAY)


# ---------- roles ----------
def test_roles_from_the_stored_credentials(tmp_path):
    acc = store_at(tmp_path)
    assert acc.load().role == "solo" and acc.shared_config() is None
    acc.join(encode_invite(URL, "eyJ.read.only", "Mika", NOW + 90 * DAY))
    a = acc.load()
    assert a.role == "user" and a.name == "Mika" and a.label == "Signed in as Mika (User)"
    assert acc.shared_config().token == "eyJ.read.only"
    acc.sign_out()
    assert acc.load().role == "solo" and acc.secrets.get("db_token") is None
    acc.set_admin(URL, FULL, PLATFORM, org="ajtan", database="team-stats", name="AJ")
    a = acc.load()
    assert a.role == "admin" and a.label == "Signed in as AJ (Admin)" and acc.platform_token() == PLATFORM


def test_secrets_never_go_into_the_account_file(tmp_path):
    acc = store_at(tmp_path)
    acc.set_admin(URL, FULL, PLATFORM, org="ajtan", database="team-stats", name="AJ")
    text = (tmp_path / "account.json").read_text(encoding="utf-8")
    assert FULL not in text and PLATFORM not in text and "team-stats" in text


def test_without_a_usable_keyring_the_data_folder_file_is_the_fallback(tmp_path, monkeypatch):
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    s = secrets_for(tmp_path)
    assert isinstance(s, FileSecrets)
    s.set("db_token", "eyJ.x.y")
    assert json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))["db_token"] == "eyJ.x.y"
    assert not (tmp_path / ".env").exists()


def test_two_data_folders_keep_separate_credentials(tmp_path):
    from draftassist.data.shared.account import KeyringSecrets

    a, b = KeyringSecrets(tmp_path / "a"), KeyringSecrets(tmp_path / "b")
    assert a.username("db_token") != b.username("db_token")


# ---------- the Platform API ----------
def test_platform_api_mints_read_only_tokens_with_an_expiry_and_rotates():
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path.endswith("/auth/tokens"):
            return httpx.Response(200, json={"jwt": "eyJ.minted.token"})
        return httpx.Response(200, json={})

    api = PlatformApi(PLATFORM, "ajtan", "team-stats", transport=httpx.MockTransport(handler))
    assert api.mint(read_only=True, days=90) == "eyJ.minted.token"
    api.mint(read_only=False, days=None)
    api.rotate()
    assert seen[0].url.path == "/v1/organizations/ajtan/databases/team-stats/auth/tokens"
    assert dict(seen[0].url.params) == {"expiration": "90d", "authorization": "read-only"}
    assert dict(seen[1].url.params) == {"expiration": "never", "authorization": "full-access"}
    assert seen[2].method == "POST" and seen[2].url.path.endswith("/auth/rotate")
    assert seen[0].headers["Authorization"] == f"Bearer {PLATFORM}" and PLATFORM not in repr(api)


# ---------- members ----------
@pytest.fixture
def team(tmp_path):
    acc = store_at(tmp_path)
    acc.set_admin(URL, FULL, PLATFORM, org="ajtan", database="team-stats", name="AJ")
    platform = FakePlatform()
    return acc, platform, Members(FakeSharedStore(), platform, acc, clock=lambda: NOW)


def test_adding_a_member_gives_a_read_only_invite_and_stores_only_a_fingerprint(team):
    acc, platform, members = team
    code = members.add("Mika")
    inv = decode_invite(code, now=NOW)
    assert inv.name == "Mika" and inv.url == URL and inv.expires_at == NOW + 90 * DAY
    assert platform.minted[-1] == (inv.token, "read-only", 90)
    (row,) = members.list()
    assert row["name"] == "Mika" and row["role"] == "user" and row["fingerprint"] == fingerprint(inv.token)
    assert inv.token not in json.dumps(members.list())
    with pytest.raises(ValueError):
        members.add("Mika")


def test_renewing_reissues_one_member_without_rotating(team):
    acc, platform, members = team
    first = decode_invite(members.add("Mika"), now=NOW).token
    renewed = decode_invite(members.renew("Mika"), now=NOW).token
    assert renewed != first and platform.rotations == 0
    assert members.list()[0]["fingerprint"] == fingerprint(renewed)


def test_removing_a_member_rotates_and_reissues_everyone_else(team):
    acc, platform, members = team
    members.add("Mika")
    members.add("Jo")
    members.add("Sam")
    old_admin = acc.shared_config().token
    reissued = members.remove("Jo")
    assert platform.rotations == 1
    assert set(reissued) == {"Mika", "Sam"}  # new codes to send, shown once
    for name, code in reissued.items():
        assert decode_invite(code, now=NOW).name == name
    assert acc.shared_config().token != old_admin  # the rotation invalidated the admin's token too: a new one is stored
    assert platform.minted[-1][1] == "full-access" or any(m[1] == "full-access" for m in platform.minted)
    rows = {r["name"]: r for r in members.list(include_revoked=True)}
    assert rows["Jo"]["revoked"] and not rows["Mika"]["revoked"]
    assert {r["name"] for r in members.list()} == {"Mika", "Sam"}


def test_tokens_minted_while_a_rotation_settles_are_minted_again(tmp_path):
    """Turso, 8 Nov 2026: a token minted straight after a rotation was refused for good; one minted later worked."""

    class SettlingPlatform(FakePlatform):
        def __init__(self):
            super().__init__()
            self.dead: set[str] = set()
            self.unsettled = 0

        def rotate(self):
            super().rotate()
            self.unsettled = 2  # the next two mints are signed with the retiring key

        def mint(self, read_only=True, days=90):
            token = super().mint(read_only, days)
            if self.unsettled:
                self.unsettled -= 1
                self.dead.add(token)
            return token

    acc = store_at(tmp_path)
    acc.set_admin(URL, FULL, PLATFORM, org="ajtan", database="team-stats", name="AJ")
    platform = SettlingPlatform()
    waits = []
    members = Members(FakeSharedStore(), platform, acc, clock=lambda: NOW,
                      works=lambda t: t not in platform.dead, sleep=waits.append)
    members.add("Mika")
    members.add("Jo")
    codes = members.remove("Jo")
    assert acc.shared_config().token not in platform.dead and len(waits) == 2
    assert decode_invite(codes["Mika"], now=NOW).token not in platform.dead


def test_expiring_invites_warn_14_days_ahead(team, tmp_path):
    acc, platform, members = team
    members.add("Mika", days=20)
    members.add("Jo", days=10)
    assert [r["name"] for r in members.list() if r["expiring"]] == ["Jo"]
    user = store_at(tmp_path / "user", clock=lambda: NOW + 5 * DAY)
    user.join(encode_invite(URL, "eyJ.read.only", "Jo", NOW + 10 * DAY))
    assert "expires in 5 days" in user.load().warning
    later = store_at(tmp_path / "user2", clock=lambda: NOW)
    later.join(encode_invite(URL, "eyJ.read.only", "Mika", NOW + 60 * DAY))
    assert later.load().warning is None


# ---------- enforcement ----------
def test_user_mode_hides_admin_controls_and_routes_refuse_them(tmp_path, make_services):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app
    from draftassist.db.session import init_db

    db = init_db(tmp_path / "db.sqlite")
    svc = make_services(db=db)
    svc.account_store = store_at(tmp_path / "acc")
    svc.account_store.join(encode_invite(URL, "eyJ.read.only", "Mika", NOW + 90 * DAY))
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        me = client.get("/api/account").json()
        assert me["role"] == "user" and me["label"] == "Signed in as Mika (User)" and "eyJ" not in json.dumps(me)
        jobs = client.get("/api/jobs").json()
        assert jobs["role"] == "user"
        assert not {j["id"] for j in jobs["jobs"]} & set(ADMIN_JOBS)  # no crawl or fit buttons
        assert {"postgame", "import"} <= {j["id"] for j in jobs["jobs"]}  # own-game jobs stay
        for job in ("crawl", "fit", "publish", "master"):
            assert client.post("/api/jobs/start", json={"job": job}).status_code == 403
        assert client.get("/api/members").status_code == 403
        assert client.post("/api/members", json={"name": "X"}).status_code == 403


def test_admin_and_solo_keep_every_job(tmp_path, make_services):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    from draftassist.db.session import init_db

    svc = make_services(db=init_db(tmp_path / "db.sqlite"))
    svc.account_store = store_at(tmp_path / "acc")
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.get("/api/account").json()["role"] == "solo"
        assert set(ADMIN_JOBS) - {"publish"} <= {j["id"] for j in client.get("/api/jobs").json()["jobs"]}


def test_joining_and_signing_out_through_the_api(tmp_path, make_services):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    svc = make_services()
    svc.account_store = store_at(tmp_path)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.post("/api/account/join", json={"invite": "dav1.garbage"}).status_code == 422
        code = encode_invite(URL, "eyJ.read.only", "Mika", NOW + 90 * DAY)
        assert client.post("/api/account/join", json={"invite": code}).json()["role"] == "user"
        assert client.post("/api/account/sign-out").json()["role"] == "solo"


def test_the_cli_refuses_admin_commands_for_users(tmp_path, monkeypatch, capsys):
    from draftassist import commands

    acc = store_at(tmp_path)
    acc.join(encode_invite(URL, "eyJ.read.only", "Mika", NOW + 90 * DAY))
    monkeypatch.setattr(commands, "account_store", lambda: acc)
    for name in ("crawl", "fit", "publish", "members"):
        assert name in ADMIN_COMMANDS
        assert commands.run([name]) == 2
        assert "admin" in capsys.readouterr().out.lower()


# ---------- no token where it shouldn't be ----------
def test_tokens_are_redacted_from_logs_and_job_output(tmp_path, team, caplog):
    acc, platform, members = team
    code = members.add("Mika")
    token = decode_invite(code, now=NOW).token
    jobs = tmp_path / "cache" / "jobs"
    jobs.mkdir(parents=True)
    handler = logging.FileHandler(jobs / "publish-20261108.log", encoding="utf-8")
    log = logging.getLogger("draftassist.test")
    log.addHandler(handler)
    try:
        log.warning("request failed with Bearer %s and %s", FULL, token)
        log.warning(f"inline {PLATFORM}")
    finally:
        handler.close()
        log.removeHandler(handler)
    text = "".join(p.read_text(encoding="utf-8") for p in jobs.glob("*.log")) + caplog.text
    for secret in (FULL, PLATFORM, token):
        assert secret not in text
    assert "[redacted]" in text
    assert redact(f"x {FULL} y") == "x [redacted] y"
    register_secret("")  # empty values are ignored


def test_account_status_never_prints_a_token(tmp_path, team, capsys, monkeypatch):
    from draftassist import commands

    acc, _, _ = team
    monkeypatch.setattr(commands, "account_store", lambda: acc)
    assert commands.run(["account", "status"]) == 0
    out = capsys.readouterr().out
    assert "AJ (Admin)" in out and FULL not in out and PLATFORM not in out


TOKEN_SHAPED = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|RGAPI-[0-9a-f]{8}-[0-9a-f]{4}")


def test_no_token_shaped_string_in_the_repo():
    root = Path(__file__).resolve().parents[1]
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()
    hits = []
    for rel in files:
        if rel.startswith("tests/") or rel.endswith((".png", ".ico", ".lock", ".sqlite")):
            continue  # the tests' fake tokens are fake on purpose
        p = root / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits += [rel for _ in TOKEN_SHAPED.finditer(text)]
    assert hits == []


def test_no_token_shaped_string_in_the_bundle():
    root = Path(__file__).resolve().parents[1] / "dist" / "DraftAssistant"
    if not root.is_dir():
        pytest.skip("no bundle built (packaging/build-installer.ps1)")
    hits = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".json", ".txt", ".yaml", ".env", ".js", ".html", ".toml"):
            if TOKEN_SHAPED.search(p.read_text(encoding="utf-8", errors="ignore")):
                hits.append(str(p))
    assert hits == []


def test_import_data_never_copies_credentials(tmp_path):
    from draftassist.commands import import_data
    from draftassist.db.session import init_db

    src = tmp_path / "checkout"
    init_db(src / "data" / "draftassist.sqlite").dispose()
    (src / "account.json").write_text("{}", encoding="utf-8")
    (src / "credentials.json").write_text(json.dumps({"db_token": FULL}), encoding="utf-8")
    copied = import_data(src, tmp_path / "app")
    assert not any("account" in c or "credentials" in c for c in copied)
    assert not (tmp_path / "app" / "credentials.json").exists()


def test_user_import_skips_shared_crawl_databases(tmp_path):
    from draftassist.commands import import_data
    from draftassist.db.session import init_db

    src = tmp_path / "checkout"
    init_db(src / "data" / "draftassist.sqlite").dispose()
    init_db(src / "data" / "master.sqlite").dispose()
    copied = import_data(src, tmp_path / "app", user=True)
    assert "data/draftassist.sqlite" in copied
    assert "data/master.sqlite" not in copied


def test_the_admin_manages_members_through_the_api(tmp_path, make_services, team):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app
    from draftassist.db.session import init_db

    acc, platform, members = team
    svc = make_services(db=init_db(tmp_path / "db.sqlite"))
    svc.account_store, svc.members_override = acc, members
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        added = client.post("/api/members", json={"name": "Mika"}).json()
        assert decode_invite(added["invite"], now=NOW).name == "Mika"
        client.post("/api/members", json={"name": "Jo"})
        listed = client.get("/api/members").json()["members"]
        assert [m["name"] for m in listed] == ["Jo", "Mika"] and "eyJ" not in json.dumps(listed)
        assert client.post("/api/members", json={"name": "Mika"}).status_code == 422
        renewed = client.post("/api/members/Mika/renew").json()
        assert renewed["invite"] != added["invite"]
        removed = client.delete("/api/members/Jo").json()
        assert set(removed["reissued"]) == {"Mika"} and platform.rotations == 1
        assert client.get("/api/jobs").json()["role"] == "admin"
        assert "publish" in {j["id"] for j in client.get("/api/jobs").json()["jobs"]}


def test_a_user_import_keeps_own_data_and_drops_crawl_data(tmp_path):
    import sqlite3

    from sqlmodel import Session

    from draftassist.commands import import_data
    from draftassist.db.models import AppSetting, CrawledMatch, Player, SeedPlayer
    from draftassist.db.session import init_db

    src = tmp_path / "checkout"
    db = init_db(src / "data" / "draftassist.sqlite")
    with Session(db) as s:
        s.add(CrawledMatch(match_id="EUW1_1", patch="16.21", bracket="gold", queue_id=420, game_start_ms=0))
        s.add(SeedPlayer(puuid="ladder-puuid", bracket="gold", tier="GOLD", division="I"))
        s.add(Player(puuid="unresolved:mid", riot_id="Me#EUW", role="mid", rank="GOLD II", bracket="gold"))
        for key in ("shared_quota", "datasets_fitted", "pinned_plan"):
            s.add(AppSetting(key=key, value="{}"))
        s.commit()
    db.dispose()
    import_data(src, tmp_path / "app", user=True)
    conn = sqlite3.connect(tmp_path / "app" / "data" / "draftassist.sqlite")
    try:
        assert conn.execute("SELECT COUNT(*) FROM crawled_matches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM seed_players").fetchone()[0] == 0  # the admin key's PUUIDs
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1  # our own
        assert {r[0] for r in conn.execute("SELECT key FROM app_settings")} == {"pinned_plan"}
    finally:
        conn.close()


def test_the_installer_hands_secrets_over_in_a_file_that_is_deleted(tmp_path, monkeypatch, capsys):
    from draftassist import commands

    acc = store_at(tmp_path / "acc")
    monkeypatch.setattr(commands, "account_store", lambda: acc)
    invite = tmp_path / "invite.txt"
    invite.write_text("﻿" + encode_invite(URL, "eyJ.read.only", "Mika", NOW + 90 * DAY) + "\r\n", encoding="utf-8")
    monkeypatch.setattr("time.time", lambda: NOW)
    assert commands.run(["account", "join", "--file", str(invite)]) == 0
    assert acc.load().role == "user" and not invite.exists()
    admin = tmp_path / "admin.txt"
    admin.write_text(f"url={URL}\r\norg=ajtan\r\ndatabase=team-stats\r\nname=AJ\r\ndb_token={FULL}\r\n"
                     f"platform_token={PLATFORM}\r\n", encoding="utf-8")
    assert commands.run(["account", "admin", "--file", str(admin)]) == 0
    assert acc.load().label == "Signed in as AJ (Admin)" and acc.platform_token() == PLATFORM and not admin.exists()
    bad = tmp_path / "bad.txt"
    bad.write_text("dav1.garbage", encoding="utf-8")
    assert commands.run(["account", "join", "--file", str(bad)]) == 1 and not bad.exists()  # deleted even so
    assert FULL not in capsys.readouterr().out


def test_a_teammates_first_run_pulls_with_progress_before_its_own_setup(tmp_path, make_services):
    from draftassist.data.aggregate import aggregate
    from draftassist.data.shared.publish import publish
    from draftassist.db.session import init_db
    from test_counts import crawl

    admin = init_db(tmp_path / "admin.sqlite")
    crawl(admin, n=60, patches=("16.21",))
    aggregate(admin, "16.21")
    store = FakeSharedStore()
    publish(admin, None, store, app_version="x", log=lambda *_: None)

    user = init_db(tmp_path / "user" / "draftassist.sqlite")
    svc = make_services(db=user)
    svc.account_store = store_at(tmp_path / "acc")
    svc.account_store.join(encode_invite(URL, "eyJ.read.only", "Mika", NOW + 90 * DAY))
    svc.shared_store = store
    ran = []
    assert svc.first_pull_needed()
    assert svc.start_bootstrap(run_job=lambda argv: ran.append(argv[0]) or 0, wait=True)
    assert svc.bootstrap["state"] == "done" and svc.bootstrap["jobs"]["pull"] == "done"
    assert svc.bootstrap["pull"]["done"] == svc.bootstrap["pull"]["total"] > 0
    assert ran == ["ddragon"]  # champion names still; OP.GG's stats aren't needed once the team's are in
    assert not svc.first_pull_needed()
