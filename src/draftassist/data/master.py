"""Master+ games, for the Matchup tree alone.

They live in their own database beside the main one, with their own stats and matchup fits. Synergy, traits and every fit
pool all the tiers of the database they read, so games in the main database would reach Live, bans and synergy. Filled by
`data.crawl --master`; the tree moves over once there are MIN_MATCHES.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, func, select

from draftassist.data.crawler import MASTER_PLUS
from draftassist.db.models import BracketTotal, Champion, CrawledMatch
from draftassist.db.session import get_engine, init_db

FIRST_PATCH = "16.21"  # crawled from this patch on, every game counting the same
SOLO_DUO = (420,)  # a Master+ player's Flex lobby can be any tier
MIN_MATCHES = 2_000  # under this the tree stays on the main database: the numbers would be almost all estimate


def master_path(main_path: Path) -> Path:
    return Path(main_path).with_name("master.sqlite")


def prepare_master(main: Engine, path: Path) -> Engine:
    """Create or update the database, with the main database's champions (the crawl and the fits need them)."""
    db = init_db(path)
    with Session(main) as s:
        champions = [c.model_dump() for c in s.exec(select(Champion)).all()]
    with Session(db) as s:
        for c in champions:
            s.merge(Champion(**c))
        s.commit()
    return db


@lru_cache(maxsize=4)
def _open(path: str) -> Engine:
    return get_engine(Path(path))


def master_matches(main: Engine) -> int:
    """Master+ games stored so far, 0 before the first crawl: crawled here, or pulled (bracket_totals, the counts)."""
    db = _master_engine(main)
    if db is None:
        return 0
    with Session(db) as s:
        crawled = s.exec(select(func.count()).select_from(CrawledMatch).where(CrawledMatch.bracket == MASTER_PLUS)).one()
        pulled = s.exec(select(func.coalesce(func.sum(BracketTotal.matches), 0))
                        .where(BracketTotal.bracket == MASTER_PLUS)).one()
    return max(crawled, pulled)


def tree_uses_master(main: Engine) -> bool:
    """A user can turn Master+ off for the Matchup tree (their data selection, docs/shared-data.md Stage 4)."""
    from draftassist.data.datasets import load_selection

    try:
        return load_selection(main).master_tree
    except Exception:
        return True


def usable_master(main: Engine) -> Engine | None:
    """The Master+ database, once it has enough games to read (and unless the user's selection turned it off)."""
    if not tree_uses_master(main):
        return None
    return _master_engine(main) if master_matches(main) >= MIN_MATCHES else None


def _master_engine(main: Engine) -> Engine | None:
    path = master_path(Path(main.url.database))
    return _open(str(path)) if path.is_file() else None
