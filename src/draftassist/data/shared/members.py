"""The admin's members (docs/shared-data.md, Stage 3): a display name each, a read-only token from Turso's Platform API,
shown once as an invite code. The `members` table in the shared database keeps names, roles, a sha256 fingerprint of the
token, created, expires and a revoked flag, for the UI only: Turso's token permissions are the real enforcement.

Turso can only invalidate all of a database's tokens at once (signing-key rotation). So "Remove member" rotates the
keys, re-mints a token for everyone remaining and for the admin, and returns the new invite codes to send. "Renew"
re-mints one member's token without rotating (their old token stays valid until it expires).

A rotation takes a little while to settle: a token minted straight after it can be signed with the key being retired,
and is then refused for good (seen on the real database, 8 Nov 2026). So after a rotation every new token is checked
against the database, and minted again until one works.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from draftassist.data.shared.redact import register_secret
from draftassist.data.shared.account import (
    DAY,
    DEFAULT_DAYS,
    WARN_DAYS,
    AccountStore,
    encode_invite,
    fingerprint,
)

SETTLE_ATTEMPTS = 6
SETTLE_WAIT_S = 10.0

MEMBERS_SCHEMA = ("CREATE TABLE IF NOT EXISTS members (name TEXT PRIMARY KEY, role TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                  "created_at REAL NOT NULL, expires_at REAL, days INTEGER, revoked INTEGER NOT NULL DEFAULT 0)")


class Members:
    def __init__(self, store, platform, account: AccountStore, clock: Callable[[], float] = time.time,
                 reopen: Callable[[], object] | None = None, works: Callable[[str], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        """`reopen`: the store again with the account's current token (after a rotation the old one is refused).
        `works(token)`: whether the database accepts a token (checked after a rotation)."""
        self.store, self.platform, self.account, self.clock, self.reopen = store, platform, account, clock, reopen
        self.works, self.sleep = works, sleep
        self.store.execute(MEMBERS_SCHEMA)

    def _mint_working(self, read_only: bool, days: int | None) -> str:
        """After a rotation: a token the database accepts, minting again while the new key settles."""
        for attempt in range(SETTLE_ATTEMPTS):
            token = self.platform.mint(read_only=read_only, days=days)
            register_secret(token)
            if self.works is None or self.works(token):
                return token
            self.sleep(SETTLE_WAIT_S * (attempt + 1))
        raise RuntimeError("The new keys haven't settled yet: try Remove again in a minute (nobody was locked out "
                           "for good; the Platform API token still works).")

    def _url(self) -> str:
        return self.account.load().url

    def list(self, include_revoked: bool = False) -> list[dict]:
        rows = self.store.execute("SELECT name, role, fingerprint, created_at, expires_at, days, revoked FROM members "
                                  "ORDER BY name")
        now = self.clock()
        out = []
        for name, role, fp, created, expires, days, revoked in rows:
            if revoked and not include_revoked:
                continue
            out.append({"name": name, "role": role, "fingerprint": fp, "created_at": created, "expires_at": expires,
                        "days": days, "revoked": bool(revoked),
                        "expiring": bool(expires is not None and not revoked and expires - now <= WARN_DAYS * DAY)})
        return out

    def _issue(self, name: str, days: int, created: float | None = None, after_rotation: bool = False) -> str:
        if after_rotation:
            token = self._mint_working(True, days)
        else:
            token = self.platform.mint(read_only=True, days=days)
        register_secret(token)  # the admin's machine never logs a member's token either
        now = self.clock()
        expires = now + days * DAY if days else None
        self.store.batch([("INSERT OR REPLACE INTO members (name, role, fingerprint, created_at, expires_at, days, revoked) "
                           "VALUES (?, 'user', ?, ?, ?, ?, 0)", (name, fingerprint(token), created or now, expires, days))])
        return encode_invite(self._url(), token, name, expires)

    def add(self, name: str, days: int = DEFAULT_DAYS) -> str:
        name = (name or "").strip()
        if not name:
            raise ValueError("a display name is needed")
        if any(m["name"].lower() == name.lower() for m in self.list()):
            raise ValueError(f"{name} is already a member")
        return self._issue(name, days)

    def renew(self, name: str) -> str:
        m = next((m for m in self.list() if m["name"] == name), None)
        if m is None:
            raise ValueError(f"no member {name}")
        return self._issue(name, m["days"] or DEFAULT_DAYS, m["created_at"])

    def remove(self, name: str) -> dict[str, str]:
        """Rotate, re-mint for everyone else and the admin; returns {name: new invite code}."""
        members = self.list()
        if not any(m["name"] == name for m in members):
            raise ValueError(f"no member {name}")
        self.platform.rotate()  # every token of the database is invalid from here, the admin's included
        self.account.replace_db_token(self._mint_working(False, None))
        if self.reopen is not None:
            self.store = self.reopen()
        self.store.batch([("UPDATE members SET revoked = 1 WHERE name = ?", (name,))])
        return {m["name"]: self._issue(m["name"], m["days"] or DEFAULT_DAYS, m["created_at"], after_rotation=True)
                for m in members if m["name"] != name}


def members_for(account: AccountStore) -> Members:
    from draftassist.data.shared.account import platform_for
    from draftassist.data.shared.config import open_store

    config = account.shared_config()
    if config is None or account.load().role != "admin":
        raise PermissionError("only the admin manages members")
    def works(token: str) -> bool:
        from draftassist.data.shared.config import SharedConfig
        from draftassist.data.shared.store import SharedStoreError

        try:
            return open_store(SharedConfig(config.url, token)).execute("SELECT 1") == [(1,)]
        except SharedStoreError:
            return False

    return Members(open_store(config), platform_for(account), account,
                   reopen=lambda: open_store(account.shared_config()), works=works)
