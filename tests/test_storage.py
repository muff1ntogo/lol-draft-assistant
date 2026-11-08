"""Clearing and optimising local storage from the Crawl page (data/storage.py): what takes space, safe clears, and the
storage jobs. Raw crawl data is never deleted: counts, fits and backfills are computed from it."""
import json
import os

import pytest

from draftassist.data.storage import clear, storage_report
from draftassist.db.session import init_db


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "DraftAssistant"
    init_db(root / "data" / "draftassist.sqlite").dispose()
    init_db(root / "data" / "master.sqlite").dispose()
    (root / "data" / "backup-before-stage2").mkdir()
    (root / "data" / "backup-before-stage2" / "draftassist.sqlite").write_bytes(b"x" * 5000)
    updates = root / "cache" / "updates"
    updates.mkdir(parents=True)
    for v in ("0.1.8", "0.1.9"):
        (updates / f"DraftAssistant-Setup-{v}.exe").write_bytes(b"y" * 3000)
    riot = root / "cache" / "riot" / "europe"
    riot.mkdir(parents=True)
    (riot / ("a" * 40 + ".json")).write_text(json.dumps({"body": {}}), encoding="utf-8")
    (root / "cache" / "jobs").mkdir()
    return root


def test_the_report_names_what_takes_space(folder):
    r = storage_report(folder, folder / "data" / "draftassist.sqlite", folder / "cache")
    items = {i["id"]: i for i in r["items"]}
    assert items["updates"]["bytes"] == 6000 and items["updates"]["files"] == 2 and items["updates"]["clearable"]
    assert items["backups"]["bytes"] == 5000 and items["backups"]["clearable"]
    assert items["cache_files"]["files"] == 1 and items["cache_files"]["action"] == "pack"
    assert items["database"]["bytes"] > 0 and items["database"]["action"] == "optimize"
    assert items["master"]["bytes"] > 0
    assert r["total"] == sum(i["bytes"] for i in r["items"])


def test_clearing_downloaded_installers_and_backups(folder):
    out = clear(folder, folder / "cache", "updates")
    assert out["freed"] == 6000 and not any((folder / "cache" / "updates").iterdir())
    out = clear(folder, folder / "cache", "backups")
    assert out["freed"] == 5000 and not (folder / "data" / "backup-before-stage2").exists()
    assert (folder / "data" / "draftassist.sqlite").exists()  # the databases themselves are never cleared
    with pytest.raises(ValueError):
        clear(folder, folder / "cache", "database")


def test_installers_are_kept_while_an_update_installs(folder):
    with pytest.raises(RuntimeError):
        clear(folder, folder / "cache", "updates", updating=True)


def test_the_storage_jobs_exist_and_db_optimize_runs_as_one(tmp_path):
    from draftassist.db.optimize import running_job
    from draftassist.jobs import JOBS, job_argv

    assert job_argv("pack") == ["cache-pack"] and job_argv("optimize") == ["db-optimize"]
    assert job_argv("archive") == ["cache-archive", "--older-than-patches", "6"]
    assert not any(JOBS[j].needs_key for j in ("pack", "optimize", "archive"))
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "running.json").write_text(json.dumps({"job": "optimize", "pid": os.getpid()}), encoding="utf-8")
    assert running_job(tmp_path) is None  # db-optimize started as a job doesn't refuse because of itself
    (jobs / "running.json").write_text(json.dumps({"job": "crawl", "pid": os.getpid()}), encoding="utf-8")
    assert running_job(tmp_path)["job"] == "crawl"


def test_the_storage_api(folder, make_services, monkeypatch):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app
    from draftassist.db.session import get_engine

    monkeypatch.setattr("draftassist.config.DATA_ROOT", folder)
    svc = make_services(db=get_engine(folder / "data" / "draftassist.sqlite"))
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        r = client.get("/api/storage").json()
        assert {i["id"] for i in r["items"]} >= {"database", "updates", "backups", "cache_files"}
        assert client.post("/api/storage/clear", json={"what": "updates"}).json()["freed"] == 6000
        assert client.post("/api/storage/clear", json={"what": "database"}).status_code == 422
