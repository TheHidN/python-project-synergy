import contextlib
import io
import pathlib
import re
import subprocess
import unittest

import clean_env  # noqa: F401
from project_synergy import cli

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "project_synergy"
FORBIDDEN = [
    r"kill-(window|pane|session|server)",
    r"send-keys",
    r"\bpkill\b",
    r"\bkillall\b",
    r"os\.kill\(",
    r"shutil\.rmtree",
    r"os\.(remove|unlink|rmdir)\(",
    r"\.unlink\(",
    r"git (worktree remove|branch -D|reset --hard|clean)",
    r"rm -",
]


class TestNoDestructiveCalls(unittest.TestCase):
    def test_source_has_no_destructive_commands(self) -> None:
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text()
            for pat in FORBIDDEN:
                if re.search(pat, text):
                    offenders.append((path.name, pat))
        self.assertEqual(offenders, [])


def _tmux_snapshot(session: str) -> str | None:
    try:
        out = subprocess.run(
            ["tmux", "list-windows", "-t", session, "-F", "#{window_id} #{window_name} #{window_panes}"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


class TestProtectedSessionUntouched(unittest.TestCase):
    def test_protected_session_identical_before_and_after(self) -> None:
        import os
        import tempfile

        session = os.environ.get("SYNERGY_PROTECT_SESSION", "CODING")
        before = _tmux_snapshot(session)
        if before is None:
            self.skipTest(f"no tmux session named {session} here")
        with tempfile.TemporaryDirectory() as d:
            os.environ["SYNERGY_STATE_DIR"] = d
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    for argv in (["init"], ["status"], ["doctor"], ["verify"]):
                        cli.main(argv)
            finally:
                os.environ.pop("SYNERGY_STATE_DIR", None)
        self.assertEqual(before, _tmux_snapshot(session))
