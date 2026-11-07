"""One-click updates for the installed app, from the GitHub releases that every push to `main` publishes
(.github/workflows/release.yml).

The app checks the latest release at startup and every few hours. When it's newer than the installed build, Live shows
an Update button; installing downloads the release's DraftAssistant-Setup.exe, checks its SHA-256 against what GitHub
reports, runs it silently (Inno Setup closes the app, replaces the program files and starts it again) and exits. Never
during a draft. A source checkout has no build number: it updates with `git pull`.
"""
from __future__ import annotations

import hashlib
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

import httpx

from draftassist.config import DATA_ROOT, RESOURCE_ROOT, is_frozen

log = logging.getLogger(__name__)

REPO = "muff1ntogo/lol-draft-assistant"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "DraftAssistant-Setup.exe"
BUILD_INFO = "build_info.json"  # written into the bundle by packaging/build-installer.ps1
CHECK_EVERY_S = 6 * 3600
# Inno Setup: no wizard, no message boxes, close the running app; the script restarts it after a silent install
INSTALL_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS")


def build_info(resources: Path | None = None) -> dict:
    path = (resources or RESOURCE_ROOT) / BUILD_INFO
    try:  # utf-8-sig: Windows PowerShell 5 writes a byte-order mark in front
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """"v0.1.42" or "0.1.42" -> (0, 1, 42); None if it isn't one."""
    if not text:
        return None
    parts = text.strip().lstrip("vV").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


@dataclass(frozen=True)
class Release:
    version: str
    url: str  # the installer's download URL
    notes: str
    sha256: str | None  # from GitHub's asset digest, when it gives one
    size: int


def latest_release(http: httpx.Client) -> Release | None:
    resp = http.get(LATEST_URL, headers={"Accept": "application/vnd.github+json"})
    if resp.status_code == 404:  # no release yet, or the repository isn't public
        return None
    resp.raise_for_status()
    body = resp.json()
    asset = next((a for a in body.get("assets", []) if a.get("name") == ASSET), None)
    version = (body.get("tag_name") or "").lstrip("vV")
    if asset is None or parse_version(version) is None:
        return None
    digest = asset.get("digest") or ""
    return Release(version, asset["browser_download_url"], (body.get("body") or "").strip(),
                   digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None, int(asset.get("size") or 0))


def _launch_detached(args: list[str]) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, creationflags=flags, close_fds=True)


class Updater:
    """Checks for, downloads and starts updates. Everything it touches outside is injectable for tests."""

    def __init__(self, current: str | None = None, frozen: bool | None = None, platform: str | None = None,
                 http_factory: Callable[[], httpx.Client] | None = None, download_dir: Path | None = None,
                 launch: Callable[[list[str]], None] = _launch_detached, exit_app: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.current = current if current is not None else build_info().get("version")
        self._frozen = is_frozen() if frozen is None else frozen
        self._platform = platform or sys.platform
        self._http = http_factory or (lambda: httpx.Client(timeout=30, follow_redirects=True))
        self._dir = download_dir or DATA_ROOT / "cache" / "updates"
        self._launch, self._clock = launch, clock
        self._exit = exit_app or (lambda: threading.Timer(1.0, lambda: os._exit(0)).start())  # after the reply is sent
        self.latest: Release | None = None
        self.checked_at: float | None = None
        self.error: str | None = None
        self.state = "idle"  # idle | checking | downloading | installing
        self._lock = threading.Lock()

    @property
    def unsupported(self) -> str | None:
        """Why this copy can't update itself, or None."""
        if not self._frozen or not self.current:
            return "a source checkout: update with git pull"
        if self._platform != "win32":
            return "updates install on Windows only"
        return None

    @property
    def available(self) -> bool:
        cur, new = parse_version(self.current), parse_version(self.latest.version if self.latest else None)
        return self.unsupported is None and cur is not None and new is not None and new > cur

    def status(self) -> dict:
        return {"current": self.current, "latest": self.latest.version if self.latest else None,
                "available": self.available, "notes": self.latest.notes if self.latest else "",
                "checked_at": self.checked_at, "error": self.error, "state": self.state, "unsupported": self.unsupported}

    def check(self, force: bool = False) -> dict:
        if self.unsupported is not None:
            return self.status()
        if not force and self.checked_at is not None and self._clock() - self.checked_at < CHECK_EVERY_S:
            return self.status()
        with self._lock:
            self.state = "checking"
            try:
                with self._http() as http:
                    self.latest = latest_release(http)
                self.error = None
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                self.error = ("GitHub is limiting requests from this network: try again in an hour" if code in (403, 429)
                              else f"couldn't check for updates (GitHub answered {code})")
                log.warning("update check failed: %s", e)
            except httpx.HTTPError as e:
                self.error = f"couldn't reach GitHub to check for updates ({type(e).__name__})"
                log.warning("update check failed: %s", e)
            finally:
                self.checked_at = self._clock()
                self.state = "idle"
        return self.status()

    def install(self) -> dict:
        """Download the newer installer, check it, start it silently and exit. Raises ValueError if there's nothing to
        install or it can't be checked."""
        if not self.available:
            raise ValueError(self.unsupported or "no newer version")
        release = self.latest
        with self._lock:
            self.state = "downloading"
            try:
                path = self._download(release)
            except Exception:
                self.state = "idle"
                raise
            self.state = "installing"
        log.info("installing %s from %s", release.version, path)
        self._launch([str(path), *INSTALL_ARGS])
        self._exit()
        return self.status()

    def _download(self, release: Release) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"DraftAssistant-Setup-{release.version}.exe"
        digest = hashlib.sha256()
        with self._http() as http, http.stream("GET", release.url) as resp:
            resp.raise_for_status()
            with open(path, "wb") as out:
                for chunk in resp.iter_bytes():
                    digest.update(chunk)
                    out.write(chunk)
        if release.size and path.stat().st_size != release.size:
            path.unlink(missing_ok=True)
            raise ValueError("the download is incomplete: try again")
        if release.sha256 and digest.hexdigest() != release.sha256:
            path.unlink(missing_ok=True)
            raise ValueError("the download doesn't match GitHub's checksum: not installed")
        return path
