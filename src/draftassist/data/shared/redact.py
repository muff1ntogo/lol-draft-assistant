"""No token in logs or job output (docs/shared-data.md, hard rules). Every secret the account loads is registered here,
and every log record is redacted as it is created, whichever handler (the app's log, a job's file) writes it later."""
from __future__ import annotations

import logging
import threading

MASK = "[redacted]"
_secrets: set[str] = set()
_lock = threading.Lock()
_installed = False


def redact(text: str) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        if s in text:
            text = text.replace(s, MASK)
    return text


def _install() -> None:
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if _secrets:
            try:
                message = record.getMessage()
            except Exception:  # a broken format string: leave it to logging's own error
                return record
            clean = redact(message)
            if clean != message:
                record.msg, record.args = clean, None
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def register_secret(secret: str | None) -> None:
    if not secret or len(secret) < 8:  # nothing to hide, and short strings would mask ordinary words
        return
    with _lock:
        _secrets.add(secret)
        _install()
