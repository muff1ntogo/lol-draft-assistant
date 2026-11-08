"""What a fresh install needs before it's useful, done by the app itself: champion names and icons (Data Dragon) and the
OP.GG starting stats, neither of which needs a Riot API key; and saving the Riot API key the installer or Settings asks
for into the data folder's .env. The crawl stays a job you start (draftassist-cli crawl): it runs for hours.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.shared.pull import PullStatus, pull
from draftassist.db.models import BootstrapRoleStats, Champion

log = logging.getLogger(__name__)

BOOTSTRAP_JOBS = ("ddragon", "opgg")  # in this order: OP.GG's rows need the champions and the patch
KEY_NAME = "RIOT_API_KEY"
_KEY = re.compile(r"^[A-Za-z0-9-]{20,100}$")


def needs_bootstrap(db: Engine) -> bool:
    """No champions or no OP.GG starting stats yet. (The app fetches the champions itself at startup, so on a fresh
    install it's the stats that are missing.) Until both are in, Live runs on synthetic stats."""
    with Session(db) as s:
        return (s.exec(select(Champion).limit(1)).first() is None
                or s.exec(select(BootstrapRoleStats).limit(1)).first() is None)


def bootstrap(run_job=None) -> dict[str, str]:
    """Run the first-run jobs in order; one failing (no internet, say) is reported and the rest still run."""
    if run_job is None:
        from draftassist.commands import run as run_job
    out = {}
    for job in BOOTSTRAP_JOBS:
        try:
            code = run_job([job])
            out[job] = "done" if code == 0 else f"failed (exit {code})"
        except Exception as e:  # noqa: BLE001 - reported on the screen, the app keeps working on fixtures
            log.exception("first-run job %s failed", job)
            out[job] = f"failed ({type(e).__name__})"
    return out


def save_riot_key(key: str, env_file: Path) -> None:
    """Set RIOT_API_KEY in the .env file, keeping its other lines. Never logs or prints the key."""
    key = key.strip()
    if not _KEY.fullmatch(key):
        raise ValueError("that doesn't look like a Riot API key (RGAPI-...)")
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip().removeprefix("export ").strip() != KEY_NAME]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join([*kept, f"{KEY_NAME}={key}"]) + "\n", encoding="utf-8")


def pull_shared_first_run(db, store, log_fn=None) -> PullStatus:
    """Pull shared stats before local bootstrap on a signed-in install.

    ``pull`` is transactional and preserves the last good data on an offline
    response. This wrapper makes the first-run policy explicit and testable.
    """
    if store is None:
        return PullStatus("off", message="No shared database configured.")
    try:
        return pull(db, store, log=log_fn or (lambda _message: None))
    except Exception as exc:  # first run must remain usable offline
        log.exception("first-run shared pull failed")
        return PullStatus("error", message=str(exc))
