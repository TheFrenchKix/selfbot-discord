"""Logs rotatifs, avec masquage automatique des secrets."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Optional

# Token Discord (user/bot): 3 segments base64url séparés par des points, + variante mfa.
_TOKEN_RE = re.compile(r"(mfa\.[\w-]{20,}|[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,})")
_CRED_URL_RE = re.compile(r"(\w+://)([^/\s:@]+):([^/\s@]+)@")
_AUTH_RE = re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)")

_extra_secrets: set[str] = set()


def register_secret(value: Optional[str]) -> None:
    """Déclare une valeur à masquer partout dans les logs."""
    if value and len(value) >= 6:
        _extra_secrets.add(value)


def redact(text: str) -> str:
    for secret in _extra_secrets:
        text = text.replace(secret, "***")
    text = _CRED_URL_RE.sub(r"\1***:***@", text)
    text = _AUTH_RE.sub(r"\1***", text)
    return _TOKEN_RE.sub("[token]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # pragma: no cover
            pass
        return True


def setup_logging(
    name: str = "selfbot",
    log_dir: str | Path = "logs",
    level: int = logging.INFO,
    console: bool = True,
    secrets: Iterable[str] = (),
) -> logging.Logger:
    for s in secrets:
        register_secret(s)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    flt = RedactingFilter()

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        Path(log_dir) / f"{name}.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handlers: list[logging.Handler] = [file_handler]
    if console:
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(flt)
        logger.addHandler(h)
    return logger
