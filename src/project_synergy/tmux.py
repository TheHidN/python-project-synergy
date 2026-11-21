"""Managed tmux workspace on its own socket. Creates, lists and inspects only.

This module never closes, ends or types into anything: it has no command for that on purpose.
A separate socket keeps Synergy's windows apart from your own tmux; it is convenience, not a security boundary.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from typing import Any

from . import config as C
from . import errors as E
from . import registry as R

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,31}")
_PROMPT = chr(0x276F)  # the prompt glyph of an idle session
_WAITING = re.compile(r"Do you want to|Enter to confirm|\(y/n\)|\[Y/n\]|Esc to cancel")


def check_command_allowed(argv: Sequence[str], allow_bypass: bool = False) -> None:
    """New workers stay out of permission-bypass mode unless the caller says so explicitly."""
    joined = " ".join(argv)
    bypass = "dangerously-skip-permissions" in joined or re.search(r"permission-mode[ =]bypassPermissions", joined)
    if bypass and not allow_bypass:
        raise E.ValidationError("this command asks for permission-bypass mode; new workers run without it "
                                "(pass --allow-bypass only if you accept that risk)")


def check_target(cfg: C.Config) -> None:
    if not cfg.tmux_socket or cfg.tmux_socket == "default":
        raise E.ValidationError("the managed tmux socket must be a dedicated name, not the default server")
    if cfg.tmux_session in cfg.protected_sessions:
        raise E.ValidationError(f"{cfg.tmux_session} is a protected session; choose another session name")


def _run(cfg: C.Config, *args: str) -> subprocess.CompletedProcess[str]:
    check_target(cfg)
    if shutil.which("tmux") is None:
        raise E.SynergyError("tmux is not installed")
    return subprocess.run(["tmux", "-L", cfg.tmux_socket, *args], capture_output=True, text=True,
                          check=False, timeout=10)


def has_session(cfg: C.Config) -> bool:
    return _run(cfg, "has-session", "-t", f"={cfg.tmux_session}").returncode == 0


def ensure_session(cfg: C.Config, command: str = "") -> bool:
    """Create the managed session if it is missing. True when created now."""
    if has_session(cfg):
        return False
    args = ["new-session", "-d", "-s", cfg.tmux_session, "-n", "status"]
    if command:
        args.append(command)
    result = _run(cfg, *args)
    if result.returncode != 0:
        raise E.SynergyError(f"could not create the tmux session: {result.stderr.strip()}")
    return True


def list_windows(cfg: C.Config) -> list[dict[str, Any]]:
    fmt = "#{window_id}\t#{window_name}\t#{window_panes}\t#{pane_current_command}\t#{window_activity}\t#{pane_current_path}"
    result = _run(cfg, "list-windows", "-t", f"={cfg.tmux_session}", "-F", fmt)
    if result.returncode != 0:
        return []
    now = int(time.time())
    rows = []
    for line in result.stdout.splitlines():
        wid, name, panes, cmd, act, path = line.split("\t")
        rows.append({"id": wid, "name": name, "panes": int(panes), "command": cmd,
                     "idle_seconds": max(0, now - int(act)), "path": path})
    return rows


def start_window(cfg: C.Config, name: str, directory: str, argv: Sequence[str]) -> None:
    if not _NAME.fullmatch(name or ""):
        raise E.ValidationError("a window name is 1-32 letters, digits, '_' or '-'")
    if not os.path.isdir(directory):
        raise E.ValidationError(f"{directory} is not a directory")
    if not argv:
        raise E.ValidationError("a command is required")
    if not has_session(cfg):
        raise E.NotFound(f"no managed session {cfg.tmux_session}: run `synergy tmux ensure` first")
    if any(w["name"] == name for w in list_windows(cfg)):
        raise E.WorkerExists(f"a window named {name} already exists")
    result = _run(cfg, "new-window", "-d", "-t", f"={cfg.tmux_session}:", "-n", name, "-c", directory,
                  shlex.join(list(argv)))
    if result.returncode != 0:
        raise E.SynergyError(f"could not open the window: {result.stderr.strip()}")


def classify(text: str, command: str) -> str:
    """Read-only state guess from the last lines of a pane. FINISHED is never inferred here."""
    if command != "claude":
        return "RUNNING"
    if "esc to interrupt" in text:
        return "WORKING"
    if _WAITING.search(text):
        return "WAITING"
    if _PROMPT in text or "bypass permissions" in text:
        return "IDLE"
    return "UNKNOWN"


def inspect_window(cfg: C.Config, name: str, lines: int = 12, include_text: bool = False) -> dict[str, Any]:
    window = next((w for w in list_windows(cfg) if w["name"] == name), None)
    if window is None:
        raise E.NotFound(f"no window named {name} in {cfg.tmux_session}")
    result = _run(cfg, "capture-pane", "-p", "-t", window["id"], "-S", f"-{max(1, int(lines))}")
    text = result.stdout if result.returncode == 0 else ""
    info: dict[str, Any] = {"name": name, "state": classify(text, window["command"]),
                            "command": window["command"], "idle_seconds": window["idle_seconds"],
                            "path": window["path"]}
    if include_text:
        info["text"] = text
    return info


def start_worker(conn: sqlite3.Connection, cfg: C.Config, worker_id: str, op_id: str, *, directory: str,
                 argv: Sequence[str], task_id: str = "", allow_bypass: bool = False, token: str = "") -> None:
    """Record the worker in the registry (limit and ownership checked), then open its window."""
    check_target(cfg)
    check_command_allowed(argv, allow_bypass)
    if not has_session(cfg):
        raise E.NotFound(f"no managed session {cfg.tmux_session}: run `synergy tmux ensure` first")
    R.register_worker(conn, worker_id, op_id, task_id=task_id, window=f"{cfg.tmux_session}:{worker_id}",
                      directory=directory, token=token)
    try:
        start_window(cfg, worker_id, directory, argv)
    except BaseException as exc:
        R.set_worker_state(conn, worker_id, "FINISHED", by=op_id, token=token,
                           evidence=f"start failed: {type(exc).__name__}")
        raise
