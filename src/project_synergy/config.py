"""Configuration and path resolution. No user name, host name or home directory is hard-coded."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ValidationError

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")


@dataclass(frozen=True)
class Config:
    state_dir: str = ""
    max_workers: int = 2
    stale_after_minutes: int = 60
    tmux_socket: str = "synergy"
    tmux_session: str = "SYNERGY"
    protected_sessions: tuple[str, ...] = ()
    initial_operators: tuple[str, ...] = ()
    onboarding_rules_files: tuple[str, ...] = ()
    launch_command: str = "claude"
    launch_args: tuple[str, ...] = ()


def _int(section: Mapping[str, object], key: str, default: int, minimum: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{key} must be an integer of at least {minimum}")
    return value


def _name(section: Mapping[str, object], key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValidationError(f"tmux {key} must be 1-64 letters, digits, '_' or '-', not starting with '-'")
    return value


def load(data: Mapping[str, object]) -> Config:
    limits = data.get("limits", {})
    tmux = data.get("tmux", {})
    safety = data.get("safety", {})
    if not (isinstance(limits, dict) and isinstance(tmux, dict) and isinstance(safety, dict)):
        raise ValidationError("[limits], [tmux] and [safety] must be tables")
    state_dir = data.get("state_dir", "")
    if not isinstance(state_dir, str):
        raise ValidationError("state_dir must be a string")
    protected = safety.get("protected_sessions", [])
    if not isinstance(protected, list) or not all(isinstance(p, str) for p in protected):
        raise ValidationError("protected_sessions must be a list of strings")
    operators = data.get("operators", {})
    if not isinstance(operators, dict):
        raise ValidationError("[operators] must be a table")
    initial = operators.get("initial", [])
    if not isinstance(initial, list) or not all(isinstance(o, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", o)
                                                for o in initial):
        raise ValidationError("[operators] initial must be a list of operator ids (letters, digits, '_' or '-')")
    onboarding = data.get("onboarding", {})
    if not isinstance(onboarding, dict):
        raise ValidationError("[onboarding] must be a table")
    rules_files = onboarding.get("rules_files", [])
    launch_args = onboarding.get("launch_args", [])
    launch_command = onboarding.get("launch_command", "claude")
    if not (isinstance(rules_files, list) and all(isinstance(x, str) for x in rules_files)):
        raise ValidationError("[onboarding] rules_files must be a list of paths")
    if not (isinstance(launch_args, list) and all(isinstance(x, str) for x in launch_args)):
        raise ValidationError("[onboarding] launch_args must be a list of strings")
    if not isinstance(launch_command, str) or not launch_command.strip():
        raise ValidationError("[onboarding] launch_command must be a command name")
    return Config(
        onboarding_rules_files=tuple(os.path.expanduser(x) for x in rules_files),
        launch_command=launch_command.strip(),
        launch_args=tuple(launch_args),
        initial_operators=tuple(o.upper() for o in initial),
        state_dir=state_dir,
        max_workers=_int(limits, "max_workers", 2, 0),
        stale_after_minutes=_int(limits, "stale_after_minutes", 60, 1),
        tmux_socket=_name(tmux, "socket", "synergy"),
        tmux_session=_name(tmux, "session", "SYNERGY"),
        protected_sessions=tuple(protected),
    )


def load_file(path: str) -> Config:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return load({})
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"{path}: not valid TOML ({exc})") from exc
    return load(data)


def config_dir(env: Mapping[str, str]) -> str:
    if env.get("SYNERGY_CONFIG_DIR"):
        return env["SYNERGY_CONFIG_DIR"]
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    return os.path.join(base, "synergy")


def state_dir(env: Mapping[str, str], cfg: Config) -> str:
    if env.get("SYNERGY_STATE_DIR"):
        return env["SYNERGY_STATE_DIR"]
    if cfg.state_dir:
        return os.path.expanduser(cfg.state_dir)
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".local", "state")
    return os.path.join(base, "synergy")
