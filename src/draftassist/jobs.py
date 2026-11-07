"""The batch jobs (crawl, fit, learning, ...) started from the app's Crawl page, one at a time.

Each runs as its own process (draftassist-cli.exe <job> in the installed app, `python -m draftassist.launcher <job>` in a
checkout), with no console window and its output in cache/jobs/<job>-<time>.log, so:
- it has its own Riot rate limiter, like a crawl started by hand, and a long crawl keeps going if the app is closed;
- a lock file (cache/jobs/running.json) says what runs, so the app reopened mid-crawl knows, and never starts a second.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from draftassist.config import is_frozen

log = logging.getLogger(__name__)

TAIL_LINES = 40
HISTORY = 10


@dataclass(frozen=True)
class JobSpec:
    label: str
    argv: tuple[str, ...]  # the subcommand and its arguments; "{hours}" is filled in
    about: str
    needs_key: bool = True


JOBS: dict[str, JobSpec] = {
    "crawl": JobSpec("Crawl", ("crawl", "--hours", "{hours}"), "Ranked matches of the current patch, every tier (~1,800/h)"),
    "timelines": JobSpec("Gold leads first", ("crawl", "--timelines-first", "--hours", "{hours}"),
                         "Fetch the timelines missing for stored matches, then crawl on"),
    "refetch": JobSpec("Refetch timelines", ("crawl", "--timelines-refetch", "--share", "1"),
                       "Every stored match's timeline again, for the newer stats; stops when done"),
    "master": JobSpec("Master+ crawl", ("crawl", "--master", "--ids-per-queue", "100", "--hours", "{hours}"),
                      "Master+ Solo/Duo, for the Matchup tree only (its own database)"),
    "fit": JobSpec("Fit", ("fit",), "Expectations and the state model, after a big crawl or a refetch (a few minutes)",
                   needs_key=False),
    "learn": JobSpec("Learning job", ("learn",), "Link players, read their history, re-score comfort"),
    "postgame": JobSpec("Confirm games", ("postgame",), "Check logged games against match-v5 and store their timelines"),
    "ddragon": JobSpec("Champions", ("ddragon",), "Champion names and icons for a new patch (stop the crawl first)",
                       needs_key=False),
    "opgg": JobSpec("OP.GG stats", ("opgg",), "OP.GG's starting stats for the patch", needs_key=False),
    "backfill": JobSpec("Backfill", ("backfill",), "Damage, game length and traits from the cache, then re-aggregate",
                        needs_key=False),
    "coverage": JobSpec("Coverage report", ("coverage",), "Matches per tier, matchup and calibration reports",
                        needs_key=False),
}
HOURS = (0.5, 24.0)


def job_argv(job: str, hours: float | None = None) -> list[str]:
    spec = JOBS[job]
    if "{hours}" in spec.argv:
        h = 8.0 if hours is None else float(hours)
        if not HOURS[0] <= h <= HOURS[1]:
            raise ValueError(f"hours must be between {HOURS[0]:g} and {HOURS[1]:g}")
        return [a.replace("{hours}", f"{h:g}") for a in spec.argv]
    return list(spec.argv)


def command(argv: list[str], frozen: bool | None = None) -> list[str]:
    """The program to run a job with: the console sibling of the installed app, or this Python in a checkout."""
    if is_frozen() if frozen is None else frozen:
        cli = Path(sys.executable).with_name("draftassist-cli.exe" if sys.platform == "win32" else "draftassist-cli")
        return [str(cli), *argv]
    return [sys.executable, "-m", "draftassist.launcher", *argv]


def pid_alive(pid: int) -> bool:
    """Whether a process still runs. (os.kill(pid, 0) would end it on Windows: there it's TerminateProcess.)"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tail(path: Path, lines: int = TAIL_LINES) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16_000))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-lines:]


class JobRunner:
    def __init__(self, folder: Path, has_key: Callable[[], bool], on_done: Callable[[str], None] | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen, frozen: bool | None = None,
                 clock: Callable[[], float] = time.time):
        self.folder = folder
        self._has_key, self._on_done, self._popen, self._frozen, self._clock = has_key, on_done, popen, frozen, clock
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stopping = False
        self.history: list[dict] = []

    @property
    def _lockfile(self) -> Path:
        return self.folder / "running.json"

    def running(self) -> dict | None:
        """What runs now: started here, or still running from before the app was reopened."""
        try:
            info = json.loads(self._lockfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self._proc is not None and self._proc.poll() is None:
            return info
        if self._proc is None and pid_alive(int(info.get("pid", 0))):
            return info
        if self._proc is None:  # it ended while the app was closed
            self._finish(info, None)
        return None

    def start(self, job: str, hours: float | None = None) -> dict:
        if job not in JOBS:
            raise ValueError(f"unknown job {job!r}")
        if JOBS[job].needs_key and not self._has_key():
            raise ValueError("this job needs your Riot API key (Settings)")
        argv = job_argv(job, hours)
        with self._lock:
            if (current := self.running()) is not None:
                raise RuntimeError(f"{JOBS[current['job']].label} is running: stop it first (one job at a time)")
            self.folder.mkdir(parents=True, exist_ok=True)
            started = self._clock()
            logfile = self.folder / f"{job}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(started))}.log"
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # no console window
            with open(logfile, "ab") as out:
                proc = self._popen(command(argv, self._frozen), stdout=out, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env, creationflags=flags)
            info = {"job": job, "label": JOBS[job].label, "argv": argv, "pid": proc.pid, "started": started,
                    "log": str(logfile)}
            self._lockfile.write_text(json.dumps(info), encoding="utf-8")
            self._proc = proc
        threading.Thread(target=self._wait, args=(proc, info), name=f"job-{job}", daemon=True).start()
        return info

    def _wait(self, proc, info: dict) -> None:
        code = proc.wait()
        with self._lock:
            self._finish(info, code)
            self._proc = None

    def _finish(self, info: dict, code: int | None) -> None:
        self._lockfile.unlink(missing_ok=True)
        self.history.insert(0, {**info, "ended": self._clock(), "exit": code, "stopped": self._stopping})
        self._stopping = False
        del self.history[HISTORY:]
        if self._on_done is not None:
            try:
                self._on_done(info["job"])
            except Exception:
                log.exception("after %s", info["job"])

    def stop(self) -> bool:
        """End the running job (every job here is resumable). False if nothing runs."""
        info = self.running()
        if info is None:
            return False
        self._stopping = True
        if self._proc is not None:
            self._proc.terminate()
        elif sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(info["pid"]), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.kill(int(info["pid"]), 15)
        return True

    def status(self) -> dict:
        current = self.running()
        last = self.history[0] if self.history else None
        shown = current or last
        return {"running": current, "last": last, "history": self.history,
                "log": tail(Path(shown["log"])) if shown else [],
                "jobs": [{"id": k, "label": s.label, "about": s.about, "hours": "{hours}" in s.argv,
                          "needs_key": s.needs_key} for k, s in JOBS.items()]}
