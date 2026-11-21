import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import clean_env  # noqa: F401

from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


class HookCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        (self.base / "cfg").mkdir()
        (self.base / "cfg" / "config.toml").write_text('[operators]\ninitial = ["OPS1", "OPS2", "OPS3"]\n')
        self.state = self.base / "state"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.base), "PYTHONPATH": str(ROOT / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1", "SYNERGY_STATE_DIR": str(self.state),
                    "SYNERGY_CONFIG_DIR": str(self.base / "cfg")}
        self.cli("init", "--no-require-tokens")
        self.cli("rules", "set", "--hash", "b" * 64)
        self.db = str(self.state / "registry.db")
        conn = R.open_registry(self.db)
        conn.execute("UPDATE operators SET status='ACTIVE'")
        conn.execute("UPDATE settings SET value='1' WHERE key='require_tokens'")
        conn.close()

    def cli(self, *argv: str, op: str | None = None, extra: dict[str, str] | None = None,
            stdin: str = "") -> subprocess.CompletedProcess[str]:
        env = {**self.env, **(extra or {})}
        if op:
            env["SYNERGY_OPERATOR"] = op
        return subprocess.run([sys.executable, "-m", "project_synergy", *argv], env=env, input=stdin, capture_output=True,
                              text=True, check=False, timeout=60, cwd=str(ROOT))

    def hook(self, op: str | None = "OPS2", payload: str = "{}", extra: dict[str, str] | None = None) -> str:
        done = self.cli("inbox", "hook", op=op, extra=extra, stdin=payload)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def assign(self, text: str = "Do the small thing", project: str = "general") -> None:
        self.cli("request", "add", text, "--project", project, op="OPS1")


class TestWhenItResumes(HookCase):
    def test_silent_with_nothing_pending_and_for_broadcast_requests_that_are_not_assignments(self) -> None:
        self.assertEqual(self.hook("OPS2"), "")
        self.cli("request", "add", "Plain broadcast", "--no-auto", op="OPS1")
        self.assertEqual(self.hook("OPS2"), "", "a broadcast request is not an assignment")

    def test_blocks_only_for_an_assignment_to_this_operator(self) -> None:
        self.assign()
        owner = [o for o in ("OPS2", "OPS3") if self.hook(o)][0]
        other = "OPS3" if owner == "OPS2" else "OPS2"
        data = json.loads(self.hook(owner))
        self.assertEqual(data["decision"], "block")
        self.assertIn("synergy inbox list", data["reason"])
        self.assertEqual(self.hook(other), "")

    def test_silent_once_the_assignment_is_started_or_read(self) -> None:
        self.assign()
        owner = [o for o in ("OPS2", "OPS3") if self.hook(o)][0]
        self.assertEqual(self.cli("task", "start", "REQ-0001", op=owner).returncode, 0)
        self.assertEqual(self.hook(owner), "")

    def test_silent_for_sessions_that_are_not_operator_sessions(self) -> None:
        self.assign()
        self.assertEqual(self.hook(None), "", "no SYNERGY_OPERATOR in the session: unrelated sessions are never touched")
        self.assertEqual(self.hook("NOBODY"), "")

    def test_silent_when_the_operator_may_not_receive_work_or_the_project_is_on_hold(self) -> None:
        self.assign()
        owner = [o for o in ("OPS2", "OPS3") if self.hook(o)][0]
        self.cli("team", "suspend", owner, "--reason", "r", op="OPS1")
        self.assertEqual(self.hook(owner), "")
        self.cli("team", "resume", owner, op="OPS1")
        self.assertTrue(self.hook(owner))
        self.cli("team", "hold", "general", "--reason", "hold", op="OPS1")
        self.assertEqual(self.hook(owner), "")
        self.cli("team", "release", "general", "--reason", "lifted", op="OPS1")
        self.cli("team", "revoke", owner, "--reason", "left", op="OPS1")
        self.assertEqual(self.hook(owner), "")


class TestNoEndlessLoop(HookCase):
    def test_the_hook_never_continues_a_stop_it_already_continued(self) -> None:
        self.assign()
        owner = [o for o in ("OPS2", "OPS3") if self.hook(o)][0]
        self.assertEqual(self.hook(owner, '{"stop_hook_active": true}'), "")

    def test_each_assignment_blocks_at_most_twice_even_if_the_session_never_acts(self) -> None:
        self.assign()
        owner = [o for o in ("OPS2", "OPS3") if self.hook(o, '{"stop_hook_active": true}') == "" and self.hook(o)][0]
        outputs = [self.hook(owner) for _ in range(8)]
        self.assertLessEqual(len([o for o in outputs if o]), 1, "two blocks in total, one already used above")
        self.assertEqual(outputs[-1], "")

    def test_a_total_cap_per_hour_bounds_many_assignments(self) -> None:
        for n in range(1, 15):
            self.cli("request", "add", f"Job {n}", op="OPS1")
        counts = {"OPS2": 0, "OPS3": 0}
        for _ in range(40):
            for op in counts:
                if self.hook(op):
                    counts[op] += 1
        for op, n in counts.items():
            self.assertLessEqual(n, 6, f"{op} was blocked {n} times in an hour")


class TestPauseAndDisable(HookCase):
    def test_every_kill_switch_silences_it_without_touching_sessions(self) -> None:
        self.assign()
        owner = [o for o in ("OPS2", "OPS3") if self.hook(o, '{"stop_hook_active": true}') == "" and self.hook(o)][0]
        self.cli("inbox", "list", op=owner)
        self.assertEqual(self.hook(owner, extra={"SYNERGY_HOOK_DISABLED": "1"}), "")
        flag = self.base / "cfg" / "hook.disabled"
        flag.write_text("off\n")
        self.assertEqual(self.hook(owner), "")
        flag.unlink()
        self.assertEqual(self.cli("hook", "off", op="OPS1").returncode, 0)
        self.assertIn("OFF", self.cli("hook", "status").stdout)
        self.assertEqual(self.hook(owner), "")
        self.assertEqual(self.cli("hook", "on", op="OPS1").returncode, 0)
        self.assertIn("ON", self.cli("hook", "status").stdout)

    def test_pausing_automatic_delegation_also_silences_it(self) -> None:
        self.assign()
        owner = [o for o in ("OPS2", "OPS3") if self.hook(o, '{"stop_hook_active": true}') == "" and self.hook(o)][0]
        self.cli("dispatch", "pause", op="OPS1")
        self.assertEqual(self.hook(owner), "")


class TestFailsSafe(HookCase):
    def test_a_missing_or_broken_registry_or_bad_input_never_blocks_or_fails(self) -> None:
        for extra in ({"SYNERGY_STATE_DIR": "/nonexistent/x"}, {}):
            done = self.cli("inbox", "hook", op="OPS2", extra=extra, stdin="not json at all {")
            self.assertEqual((done.returncode, done.stdout.strip()), (0, ""))
        (self.state / "registry.db").write_bytes(b"this is not a database")
        done = self.cli("inbox", "hook", op="OPS2", stdin="{}")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ""))


class TestNeverMigrates(HookCase):
    def test_an_older_registry_is_left_alone_and_the_hook_stays_silent(self) -> None:
        import sqlite3

        raw = sqlite3.connect(self.db, isolation_level=None)
        raw.execute("UPDATE schema_version SET version=7")
        raw.close()
        done = self.cli("inbox", "hook", op="OPS2", stdin="{}")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ""))
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("SELECT version FROM schema_version").fetchone()[0], 7, "the hook never migrates")
        self.assertEqual([p for p in os.listdir(self.state) if ".bak-" in p], [])
