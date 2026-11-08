"""Where the shared database is and the token to reach it: the signed-in account (data/shared/account.py; an invite for
users, the admin's own tokens for the admin). The rest of the app only ever calls `load_config` and `open_store`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SharedConfig:
    url: str
    token: str = field(repr=False)


def load_config(account=None) -> SharedConfig | None:
    """None when signed out (solo mode)."""
    from draftassist.data.shared.account import default_store

    return (account or default_store()).shared_config()


def open_store(config: SharedConfig):
    from draftassist.data.shared.store import HttpSharedStore

    return HttpSharedStore(config.url, config.token)
