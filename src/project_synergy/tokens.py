"""Owner-only token files. A token is written here and never printed."""

from __future__ import annotations

import os
import secrets


def token_file(db: str, op: str) -> str:
    """The token file of an operator. SYNERGY_TOKEN_FILE overrides it only for the operator this session is, so
    registering or rotating another operator can never overwrite it."""
    override = os.environ.get("SYNERGY_TOKEN_FILE")
    if override and os.environ.get("SYNERGY_OPERATOR", "").strip().upper() == op.strip().upper():
        return override
    return os.path.join(os.path.dirname(db), "tokens", f"{op}.token")


def write_token(db: str, op: str, token: str) -> str:
    """Store a token owner-only, replacing any earlier file atomically. The token is never printed."""
    path = token_file(db, op)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    state = os.path.abspath(os.path.dirname(db))
    if os.path.commonpath([os.path.abspath(folder), state]) == state:
        os.chmod(folder, 0o700)
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    os.replace(tmp, path)
    return path
