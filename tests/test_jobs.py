"""The Crawl page's jobs (jobs.py, api/jobs_routes.py)."""
import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from draftassist.api.main import create_app
from draftassist.db.models import CrawledMatch
from draftassist.jobs import JOBS, JobRunner, command, job_argv, pid_alive, tail


def fake_popen(script: str):
    """Runs a little Python program instead of the job, so a job really starts, writes and ends."""
    calls = []

    def popen(cmd, **kw):
        calls.append(cmd)
        kw.pop("creationflags", None)
        return subprocess.Popen([sys.executable, "-c", script], **kw)
    popen.calls = calls
    return popen


def wait_until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert check()


def runner(tmp_path, script="print('crawling', flush=True)", key=True, **kw):
    done = []
    r = JobRunner(tmp_path, has_key=lambda: key, on_done=done.append, popen=fake_popen(script), frozen=False, **kw)
    return r, done


# ---------- what runs ----------
def test_hours_are_filled_in_and_checked():
    assert job_argv("crawl", 6) == ["crawl", "--hours", "6"]
    assert job_argv("crawl") == ["crawl", "--hours", "8"]
    assert job_argv("refetch") == ["crawl", "--timelines-refetch", "--share", "1"]
    with pytest.raises(ValueError):
        job_argv("crawl", 100)


def test_the_installed_app_runs_its_console_sibling():
    assert command(["fit"], frozen=False) == [sys.executable, "-m", "draftassist.launcher", "fit"]
    cli = command(["fit"], frozen=True)
    assert cli[0].endswith(("draftassist-cli.exe", "draftassist-cli")) and cli[1:] == ["fit"]


def test_every_job_is_a_subcommand():
    from draftassist.commands import COMMANDS

    assert all(spec.argv[0] in COMMANDS for spec in JOBS.values())


def test_is_this_process_alive():
    assert pid_alive(os.getpid()) and not pid_alive(0)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not pid_alive(proc.pid)


# ---------- running ----------
def test_a_job_runs_logs_and_finishes(tmp_path):
    r, done = runner(tmp_path)
    info = r.start("crawl", 2)
    assert info["argv"] == ["crawl", "--hours", "2"] and (tmp_path / "running.json").is_file()
    wait_until(lambda: r.running() is None and done)
    assert done == ["crawl"] and not (tmp_path / "running.json").exists()
    assert r.history[0]["exit"] == 0 and r.history[0]["stopped"] is False
    assert "crawling" in r.status()["log"]
    assert r._popen.calls[0][-4:] == ["draftassist.launcher", "crawl", "--hours", "2"]


def test_one_job_at_a_time(tmp_path):
    r, _ = runner(tmp_path, script="import time; time.sleep(5)")
    r.start("fit")
    with pytest.raises(RuntimeError, match="one job at a time"):
        r.start("crawl")
    r.stop()
    wait_until(lambda: r.running() is None)


def test_stopping_a_job(tmp_path):
    r, done = runner(tmp_path, script="import time; print('started', flush=True); time.sleep(30)")
    r.start("crawl")
    assert r.stop() is True
    wait_until(lambda: r.running() is None and done)
    assert r.history[0]["stopped"] is True and r.history[0]["exit"] != 0
    assert r.stop() is False


def test_jobs_that_use_the_riot_api_need_the_key(tmp_path):
    r, _ = runner(tmp_path, key=False)
    with pytest.raises(ValueError, match="Riot API key"):
        r.start("crawl")
    r.start("fit")  # the fit doesn't
    wait_until(lambda: r.running() is None)


def test_a_crawl_still_running_from_before_the_app_was_reopened_is_seen(tmp_path):
    (tmp_path / "running.json").write_text(json.dumps(
        {"job": "crawl", "label": "Crawl", "pid": os.getpid(), "started": 0, "log": str(tmp_path / "x.log")}))
    r, _ = runner(tmp_path)
    assert r.running()["job"] == "crawl"
    with pytest.raises(RuntimeError):
        r.start("fit")


def test_one_that_ended_while_the_app_was_closed_goes_to_history(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    (tmp_path / "running.json").write_text(json.dumps(
        {"job": "crawl", "label": "Crawl", "pid": proc.pid, "started": 0, "log": str(tmp_path / "x.log")}))
    r, done = runner(tmp_path)
    assert r.running() is None and r.history[0]["job"] == "crawl" and done == ["crawl"]


def test_tail_reads_the_end_of_a_long_log(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {i}" for i in range(5000)))
    assert tail(log, 3) == ["line 4997", "line 4998", "line 4999"]
    assert tail(tmp_path / "missing.log") == []


# ---------- the page's API ----------
def test_api_shows_the_crawl_and_runs_jobs(make_services, engine, tmp_path):
    with Session(engine) as s:
        for i, (b, p, t) in enumerate([("gold", "16.22", 2), ("gold", "16.21", 2), ("iron", "16.22", 0)]):
            s.add(CrawledMatch(match_id=f"EUW1_{i}", patch=p, bracket=b, queue_id=420, game_start_ms=0, timeline_version=t))
        s.commit()
    svc = make_services(db=engine)
    svc.job_runner_override, done = runner(tmp_path, key=True)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        body = client.get("/api/jobs").json()
        brackets = {r["bracket"]: r for r in body["crawl"]["brackets"]}
        assert brackets["gold"]["all"] == 2 and brackets["gold"]["timelines"] == 2 and brackets["iron"]["all"] == 1
        assert {j["id"] for j in body["jobs"]} == set(JOBS)
        assert client.post("/api/jobs/start", json={"job": "nope"}).status_code == 422
        assert client.post("/api/jobs/start", json={"job": "crawl", "hours": 99}).status_code == 422
        assert client.post("/api/jobs/start", json={"job": "crawl", "hours": 1}).json()["running"]["job"] == "crawl"
        wait_until(lambda: client.get("/api/jobs").json()["running"] is None)
        assert client.get("/api/jobs").json()["last"]["exit"] == 0


def test_no_update_while_a_job_runs(make_services, tmp_path):
    from test_updates import fake_github, updater

    svc = make_services()
    svc.job_runner_override, _ = runner(tmp_path, script="import time; time.sleep(30)")
    u, launched, _ = updater(tmp_path, http_factory=fake_github())
    svc.updater_override = u
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        client.post("/api/update/check")
        svc.job_runner_override.start("fit")
        res = client.post("/api/update/install")
        assert res.status_code == 409 and "Crawl page" in res.json()["detail"]
        svc.job_runner_override.stop()
    assert launched == []


# ---------- crawl options and live progress (docs/crawl-page.md) ----------
def test_crawl_options_become_arguments():
    argv = job_argv("crawl", 2, {"patch": "16.20", "brackets": ["Gold", "iron"], "ids_per_queue": 50,
                                 "no_timelines": True})
    assert argv == ["crawl", "--hours", "2", "--patch", "16.20", "--brackets", "gold", "iron", "--ids-per-queue", "50",
                    "--no-timelines"]
    assert job_argv("master", 2, {"patch": "16.20"}) == job_argv("master", 2)  # Master+ takes no options
    assert job_argv("crawl", 2, {"patch": None, "brackets": None}) == ["crawl", "--hours", "2"]
    for bad in ({"patch": "16.20; rm -rf"}, {"brackets": ["challenger"]}, {"ids_per_queue": 500}):
        with pytest.raises(ValueError):
            job_argv("crawl", 2, bad)


def test_a_running_job_reports_its_progress(tmp_path):
    script = ("import json, os, time; p = os.environ['DRAFTASSIST_PROGRESS']; "
              "open(p, 'w').write(json.dumps({'phase': 'crawling', 'stored': {'gold': 7}})); time.sleep(1.5)")
    r, _ = runner(tmp_path, script=script)
    r.start("crawl", 1, {"patch": "16.21"})
    wait_until(lambda: (r.status()["progress"] or {}).get("stored") == {"gold": 7})
    assert r._popen.calls[0][-2:] == ["--patch", "16.21"]
    wait_until(lambda: r.running() is None)
    assert r.status()["progress"]["phase"] == "crawling"  # the last run's numbers stay readable


def test_progress_file_is_throttled_and_optional(tmp_path):
    from draftassist.data.progress import Progress, read_progress

    t = [100.0]
    p = Progress(tmp_path / "progress.json", clock=lambda: t[0])
    p.update(force=True, phase="crawling", stored={"gold": 1})
    p.update(stored={"gold": 2})  # within 2 s: not written yet
    assert read_progress(tmp_path / "progress.json")["stored"] == {"gold": 1}
    t[0] += 3
    p.update(stored={"gold": 3})
    assert read_progress(tmp_path / "progress.json")["stored"] == {"gold": 3}
    Progress(None).update(force=True, phase="x")  # no path (run by hand): nothing written, no error


def test_the_summary_follows_a_patch_and_lists_patches(make_services, engine, tmp_path):
    with Session(engine) as s:
        for i, p in enumerate(["16.22", "16.21", "16.21"]):
            s.add(CrawledMatch(match_id=f"EUW1_{i}", patch=p, bracket="gold", queue_id=420, game_start_ms=0))
        s.commit()
    svc = make_services(db=engine)
    svc.job_runner_override, _ = runner(tmp_path)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        body = client.get("/api/jobs", params={"patch": "16.21"}).json()
        assert body["crawl"]["patch"] == "16.21" and body["crawl"]["brackets"][0]["patch"] == 2
        assert body["crawl"]["patches"][:2] == ["16.22", "16.21"] and body["database"] == "main"
        assert body["master_exists"] is False
        assert client.get("/api/jobs", params={"database": "master"}).json()["crawl"]["total"] == 3  # falls back to main


def test_the_crawl_pauses_while_a_ranked_5s_champ_select_scouts(tmp_path):
    from draftassist.data.progress import YIELD_FRESH_S, request_yield, wait_while_yielding, yield_requested

    t = [1000.0]
    clock = lambda: t[0]  # noqa: E731
    f = tmp_path / "yield.json"
    assert not yield_requested(f, clock) and wait_while_yielding(f, clock=clock) == 0
    request_yield(f, clock)
    assert yield_requested(f, clock)
    paused = []

    def sleep(s):
        t[0] += s

    waited = wait_while_yielding(f, sleep=sleep, clock=clock, on_pause=lambda: paused.append(1))
    assert waited >= YIELD_FRESH_S - 2 and paused == [1]  # until the request went stale
    r, _ = runner(tmp_path / "jobs", script="import time; time.sleep(1)")
    r.make_way()  # nothing running: nothing written
    assert not r.yield_file.exists()
    r.start("crawl", 1)
    r.make_way()
    assert r.yield_file.exists()
    wait_until(lambda: r.running() is None)
