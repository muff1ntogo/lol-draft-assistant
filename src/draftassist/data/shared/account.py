"""Sign-in, roles and credentials (docs/shared-data.md, Stage 3).

Turso has no username and password without a separate auth server, so signing in is an invite code: the database URL and
a read-only token, encoded (`dav1.` + base64url JSON). The admin enters the full-access token and the Platform API token
once on their own machine; they are never in an invite.

Roles, from what this data folder holds:
- admin: a full-access database token and the Platform API token (crawls, fits, publishes, manages members)
- user: a read-only token from an invite (pulls; own-game jobs only)
- solo: nothing (today's behaviour: crawl locally, nothing shared)
Real enforcement is Turso's: a read-only token can't write. The role decides what the app offers.

Secrets live in Windows Credential Manager (keyring, decision 4), one entry per secret, named per data folder so a second
data folder (DRAFTASSIST_HOME) is a separate account; `credentials.json` in the data folder is the fallback where no
keyring works. Never in .env (the installer and import-data copy it). `account.json` holds only what isn't secret.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from draftassist.data.shared.redact import register_secret

INVITE_PREFIX = "dav1."
DEFAULT_DAYS = 90
WARN_DAYS = 14
SERVICE = "DraftAssistant"
DAY = 86_400.0

# Crawl-data jobs and commands: the admin's (and solo mode's); users pull instead. Own-game jobs (postgame, import-games,
# learn, scouting) stay for everyone: they use the user's own key and tables and are never shared.
ADMIN_JOBS = ("crawl", "timelines", "refetch", "master", "fit", "power", "backfill", "coverage", "publish")
ADMIN_COMMANDS = ("crawl", "fit", "power", "backfill", "coverage", "publish", "members")


class InviteError(ValueError):
    pass


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- invite codes ----------
@dataclass(frozen=True)
class Invite:
    url: str
    token: str
    name: str
    expires_at: float | None


def encode_invite(url: str, token: str, name: str, expires_at: float | None) -> str:
    payload = json.dumps({"u": url, "t": token, "n": name, "e": expires_at}, separators=(",", ":"))
    return INVITE_PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_invite(code: str, now: float | None = None) -> Invite:
    code = (code or "").strip()
    if not code.startswith(INVITE_PREFIX):
        raise InviteError("That isn't an invite code (it starts with dav1.)")
    body = code[len(INVITE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise InviteError("The invite code is damaged: copy it again in full") from None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data.get(k) for k in ("u", "t", "n")):
        raise InviteError("The invite code is incomplete: copy it again in full")
    expires = data.get("e")
    if expires is not None and (now if now is not None else time.time()) >= float(expires):
        raise InviteError("This invite has expired: ask the admin for a new one")
    return Invite(data["u"], data["t"], data["n"], None if expires is None else float(expires))


# ---------- where secrets live ----------
class MemorySecrets:
    """For tests."""

    def __init__(self):
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def set(self, key: str, value: str) -> None:
        self._d[key] = value

    def delete(self, key: str) -> None:
        self._d.pop(key, None)


class KeyringSecrets:
    """Windows Credential Manager, one entry per secret and data folder."""

    def __init__(self, data_root: Path):
        self._ns = hashlib.sha1(str(Path(data_root).resolve()).lower().encode()).hexdigest()[:10]

    def username(self, key: str) -> str:
        return f"{key}:{self._ns}"

    def get(self, key: str) -> str | None:
        import keyring

        return keyring.get_password(SERVICE, self.username(key))

    def set(self, key: str, value: str) -> None:
        import keyring

        keyring.set_password(SERVICE, self.username(key), value)

    def delete(self, key: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(SERVICE, self.username(key))
        except PasswordDeleteError:
            pass


class FileSecrets:
    """The fallback: credentials.json in the data folder (never .env)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _all(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, key: str) -> str | None:
        return self._all().get(key)

    def set(self, key: str, value: str) -> None:
        d = self._all()
        d[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d), encoding="utf-8")

    def delete(self, key: str) -> None:
        d = self._all()
        if d.pop(key, None) is not None:
            self.path.write_text(json.dumps(d), encoding="utf-8")


def secrets_for(data_root: Path):
    """Credential Manager when a real keyring is there, else the data folder's credentials.json."""
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if not isinstance(backend, fail.Keyring) and getattr(backend, "priority", 1) > 0:
            return KeyringSecrets(data_root)
    except Exception:
        pass
    return FileSecrets(Path(data_root) / "credentials.json")


# ---------- the account ----------
@dataclass(frozen=True)
class Account:
    role: str  # admin, user, solo
    name: str | None = None
    url: str | None = None
    org: str | None = None
    database: str | None = None
    expires_at: float | None = None
    warning: str | None = None

    @property
    def label(self) -> str:
        if self.role == "solo":
            return "Not signed in (solo mode)"
        return f"Signed in as {self.name} ({self.role.capitalize()})"

    def as_dict(self) -> dict:
        return {"role": self.role, "name": self.name, "label": self.label, "url": self.url, "org": self.org,
                "database": self.database, "expires_at": self.expires_at, "warning": self.warning}


class AccountStore:
    def __init__(self, data_root: Path, secrets=None, clock: Callable[[], float] = time.time):
        self.data_root = Path(data_root)
        self.secrets = secrets if secrets is not None else secrets_for(self.data_root)
        self.clock = clock

    @property
    def path(self) -> Path:
        return self.data_root / "account.json"

    def _meta(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, meta: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(meta, indent=1), encoding="utf-8")

    def load(self) -> Account:
        meta = self._meta()
        token = self.secrets.get("db_token")
        register_secret(token)
        role = meta.get("role")
        if role == "admin" and token and self.platform_token():
            pass
        elif role == "user" and token:
            pass
        else:
            return Account("solo")
        expires = meta.get("expires_at")
        warning = None
        if role == "user" and expires is not None:
            left = (float(expires) - self.clock()) / DAY
            if left <= 0:
                warning = "Your invite has expired: ask the admin for a new code."
            elif left <= WARN_DAYS:
                days = int(left) if left >= 1 else 0
                warning = (f"Your invite expires in {days} day{'' if days == 1 else 's'}: ask the admin to renew it."
                           if days else "Your invite expires today: ask the admin to renew it.")
        return Account(role, meta.get("name"), meta.get("url"), meta.get("org"), meta.get("database"), expires, warning)

    def platform_token(self) -> str | None:
        t = self.secrets.get("platform_token")
        register_secret(t)
        return t

    def shared_config(self):
        from draftassist.data.shared.config import SharedConfig

        acc = self.load()
        if acc.role == "solo":
            return None
        return SharedConfig(acc.url, self.secrets.get("db_token"))

    def join(self, code: str) -> Account:
        inv = decode_invite(code, now=self.clock())
        self.secrets.set("db_token", inv.token)
        self.secrets.delete("platform_token")
        register_secret(inv.token)
        self._save({"role": "user", "name": inv.name, "url": inv.url, "expires_at": inv.expires_at})
        return self.load()

    def set_admin(self, url: str, db_token: str, platform_token: str, org: str, database: str, name: str) -> Account:
        if not all(x and x.strip() for x in (url, db_token, platform_token, org, database, name)):
            raise ValueError("the database URL, both tokens, the organisation, the database name and your name are needed")
        self.secrets.set("db_token", db_token.strip())
        self.secrets.set("platform_token", platform_token.strip())
        register_secret(db_token.strip())
        register_secret(platform_token.strip())
        self._save({"role": "admin", "name": name.strip(), "url": url.strip(), "org": org.strip(),
                    "database": database.strip()})
        return self.load()

    def replace_db_token(self, token: str) -> None:
        """After a key rotation: the admin's own new full-access token."""
        register_secret(token)
        self.secrets.set("db_token", token)

    def sign_out(self) -> Account:
        """Removes the stored tokens; local data stays."""
        for key in ("db_token", "platform_token"):
            self.secrets.delete(key)
        self.path.unlink(missing_ok=True)
        return self.load()


def default_store() -> AccountStore:
    from draftassist.config import DATA_ROOT

    return AccountStore(DATA_ROOT)


# ---------- Turso's Platform API ----------
class PlatformApi:
    """Mint and rotate database tokens (POST /v1/organizations/{org}/databases/{db}/auth/tokens and .../auth/rotate)."""

    BASE = "https://api.turso.tech"

    def __init__(self, token: str, org: str, database: str, transport: httpx.BaseTransport | None = None):
        register_secret(token)
        self.org, self.database = org, database
        self._http = httpx.Client(timeout=30, transport=transport, headers={"Authorization": f"Bearer {token}"})

    def __repr__(self) -> str:
        return f"PlatformApi({self.org}/{self.database})"

    def _url(self, tail: str) -> str:
        return f"{self.BASE}/v1/organizations/{self.org}/databases/{self.database}/auth/{tail}"

    def _post(self, url: str, params: dict | None = None) -> dict:
        try:
            resp = self._http.post(url, params=params)
        except httpx.TransportError as e:
            raise RuntimeError(f"Turso's Platform API unreachable ({type(e).__name__})") from None
        if resp.status_code >= 400:
            raise RuntimeError(f"Turso's Platform API answered HTTP {resp.status_code}")
        return resp.json() if resp.content else {}

    def mint(self, read_only: bool = True, days: int | None = DEFAULT_DAYS) -> str:
        out = self._post(self._url("tokens"), {"expiration": f"{days}d" if days else "never",
                                               "authorization": "read-only" if read_only else "full-access"})
        token = out.get("jwt")
        if not token:
            raise RuntimeError("Turso's Platform API returned no token")
        register_secret(token)
        return token

    def rotate(self) -> None:
        self._post(self._url("rotate"))


def platform_for(store: AccountStore) -> PlatformApi:
    acc = store.load()
    if acc.role != "admin":
        raise PermissionError("only the admin manages members")
    return PlatformApi(store.platform_token(), acc.org, acc.database)
