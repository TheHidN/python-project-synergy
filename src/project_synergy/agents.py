"""Finding existing agent sessions for the shared pool. Read-only: it lists tmux window names and commands and
registers rows in the registry. It never sends anything to a session and never changes one."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable

FORMAT = "#{session_name}:#{window_name}|#{pane_current_command}|#{window_activity}|#{pane_current_path}"


def tmux_lines() -> list[str]:
    """Metadata of every pane on the default tmux server (names and commands only; no pane text)."""
    done = subprocess.run(["tmux", "list-panes", "-a", "-F", FORMAT], capture_output=True, text=True, check=False, timeout=10)
    return done.stdout.splitlines() if done.returncode == 0 else []


def _id(window: str) -> str:
    name = window.split(":", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").upper()
    return cleaned[:32]


def parse_windows(lines: Iterable[str], protected: Iterable[str] = (), known_windows: Iterable[str] = ()) -> list[dict[str, str]]:
    """Candidate agents: windows that run the claude command, outside protected sessions, not already registered."""
    blocked, known = set(protected), set(known_windows)
    found: dict[str, dict[str, str]] = {}
    for line in lines:
        parts = line.split("|")
        if len(parts) < 4 or parts[1] != "claude":
            continue
        window = parts[0]
        if window.split(":", 1)[0] in blocked or window in known or window in found:
            continue
        agent_id = _id(window)
        if agent_id:
            found[window] = {"id": agent_id, "window": window, "idle_seconds": parts[2], "path": parts[3]}
    return list(found.values())
