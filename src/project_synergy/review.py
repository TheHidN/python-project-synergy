"""Prepare an isolated, ready-made review folder: a frozen read-only copy of the code, the sandbox kit, a brief,
a settings file that never prompts and a launcher. It starts nothing."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path

from . import config as C
from . import errors as E

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_MODEL = re.compile(r"(?:sonnet|opus|haiku|claude-[a-z0-9][a-z0-9.-]{2,60})")
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,100}")
_ROOT = Path(__file__).resolve().parents[2]


def _read_only(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            os.chmod(path, stat.S_IMODE(path.lstat().st_mode) & ~0o222)
        os.chmod(dirpath, stat.S_IMODE(Path(dirpath).stat().st_mode) & ~0o222)


def review_root(env: Mapping[str, str], state_dir: str, config_dir: str) -> Path:
    """Where review folders live: outside the state and config folders, because the reviewer's settings deny
    reading those (a review folder inside them could not even write its own report)."""
    if env.get("SYNERGY_REVIEW_DIR"):
        root = Path(env["SYNERGY_REVIEW_DIR"])
    else:
        base = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".local", "share")
        root = Path(base) / "synergy" / "reviews"
    resolved = Path(os.path.abspath(root))
    for protected in (state_dir, config_dir):
        guard = Path(os.path.abspath(protected))
        if resolved == guard or guard in resolved.parents:
            raise E.ValidationError(f"the review folder {resolved} is inside {guard}, which reviewers are denied; "
                                    "set SYNERGY_REVIEW_DIR to another place")
    return resolved


def prepare(cfg: C.Config, root: Path, state_dir: str, config_dir: str, name: str, repo: str,
            ref: str = "HEAD", model: str = "sonnet") -> Path:
    if not _NAME.fullmatch(name or ""):
        raise E.ValidationError("a review name is 1-32 letters, digits, '_' or '-'")
    if not _MODEL.fullmatch(model or ""):
        raise E.ValidationError("a model is sonnet, opus, haiku or a full model id such as claude-opus-5-5")
    if not _REF.fullmatch(ref or ""):
        raise E.ValidationError("that git reference is not allowed")
    kit, tpl_settings, tpl_brief = _ROOT / "review-kit", _ROOT / "templates" / "reviewer-settings.json", _ROOT / "templates" / "review-brief.md"
    for needed in (kit, tpl_settings, tpl_brief):
        if not needed.exists():
            raise E.ValidationError(f"{needed.name} was not found next to the installed source")
    folder = root / name
    if folder.exists():
        raise E.ValidationError(f"review folder {folder} already exists; choose another name")
    done = subprocess.run(["git", "-C", repo, "archive", "--format=tar", ref], capture_output=True, check=False)
    if done.returncode != 0:
        raise E.ValidationError(f"could not read {ref} from {repo} (is it a git repository?)")
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    code = folder / "code"
    code.mkdir()
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        tar.extractall(code, filter="data")
    _read_only(code)
    (folder / "work").mkdir()
    shutil.copytree(kit, folder / "kit")
    for script in (folder / "kit").glob("*.sh"):
        os.chmod(script, 0o755)
    settings = json.loads(tpl_settings.read_text())
    perms = settings["permissions"]
    for protected in (state_dir, config_dir):
        perms["deny"].append(f"Read(/{os.path.abspath(protected)}/**)")
    perms["deny"] += ["Read(~/.ssh/**)", "Read(~/.local/share/claude-swap/**)"]
    (folder / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    shutil.copy2(tpl_brief, folder / "BRIEF.md")
    launcher = folder / "launch.sh"
    launcher.write_text("#!/usr/bin/env bash\n"
                        f"# Written by `synergy review prepare {name}`. Starts one reviewer session that never prompts.\n"
                        f'cd {shlex.quote(str(folder))}\n'
                        f'exec {shlex.quote(cfg.launch_command)} --model {model} --permission-mode dontAsk --settings ./settings.json '
                        '"Read ./BRIEF.md and follow it exactly."\n')
    os.chmod(launcher, 0o755)
    return folder
