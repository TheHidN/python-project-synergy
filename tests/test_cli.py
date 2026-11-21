import contextlib
import io
import os
import tempfile
import unittest

import clean_env  # noqa: F401
from project_synergy import cli
from project_synergy import registry as R


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        os.environ["SYNERGY_STATE_DIR"] = self.state
        os.environ["SYNERGY_CONFIG_DIR"] = os.path.join(self._tmp.name, "cfg")
        self.addCleanup(os.environ.pop, "SYNERGY_STATE_DIR", None)
        self.addCleanup(os.environ.pop, "SYNERGY_CONFIG_DIR", None)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def activate(self, *ops: str) -> None:
        self.assertEqual(self.run_cli("rules", "set", "--hash", "x" * 64)[0], 0)
        for op in ops:
            self.assertEqual(self.run_cli("operator", "register", op)[0], 0)
        conn = R.open_registry(os.path.join(self.state, "registry.db"))
        for op in ops:
            conn.execute("UPDATE operators SET trust='APPROVED', admin=1, approved_by='test', approval_ref='test' WHERE id=?", (op,))
        conn.close()
        for op in ops:
            self.assertEqual(self.run_cli("operator", "ack", op, "x" * 64)[0], 0)


class TestCli(CliCase):
    def test_init_twice(self) -> None:
        self.assertEqual(self.run_cli("init", "--no-require-tokens")[0], 0)
        self.assertEqual(self.run_cli("init", "--no-require-tokens")[0], 0)
        self.assertTrue(os.path.exists(os.path.join(self.state, "registry.db")))
        self.assertEqual(os.stat(self.state).st_mode & 0o077, 0)

    def test_task_conflict_exit_code_and_message(self) -> None:
        self.run_cli("init", "--no-require-tokens")
        self.activate("OPS1", "OPS2")
        self.assertEqual(self.run_cli("task", "create", "T1", "do the thing", "--actor", "OPS1")[0], 0)
        self.assertEqual(self.run_cli("task", "claim", "T1", "OPS1")[0], 0)
        code, _, err = self.run_cli("task", "claim", "T1", "OPS2")
        self.assertEqual(code, 3)
        self.assertIn("OPS1", err)
        self.assertIn("T1", err)

    def test_file_conflict_exit_code(self) -> None:
        self.run_cli("init", "--no-require-tokens")
        self.activate("OPS1", "OPS2")
        self.assertEqual(self.run_cli("file", "claim", "/a/b.conf", "OPS1")[0], 0)
        code, _, err = self.run_cli("file", "claim", "/a/b.conf", "OPS2")
        self.assertEqual(code, 3)
        self.assertIn("OPS1", err)

    def test_pending_operator_refused(self) -> None:
        self.run_cli("init", "--no-require-tokens")
        self.activate("OPS1")
        os.environ["SYNERGY_OPERATOR"] = "OPS1"
        self.run_cli("operator", "register", "OPS3")
        os.environ.pop("SYNERGY_OPERATOR", None)
        self.run_cli("task", "create", "T1", "x", "--actor", "OPS1")
        code, _, err = self.run_cli("task", "claim", "T1", "OPS3")
        self.assertEqual(code, 3)
        self.assertIn("ACTIVE", err)

    def test_status_lists_everything(self) -> None:
        self.run_cli("init", "--no-require-tokens")
        self.activate("OPS1")
        self.run_cli("task", "create", "T1", "do the thing", "--actor", "OPS1")
        self.run_cli("task", "claim", "T1", "OPS1")
        self.run_cli("worker", "register", "W1", "OPS1", "--task", "T1", "--window", "CLAUDE-OPS:w1")
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        for needle in ("OPS1", "T1", "W1", "CLAIMED"):
            self.assertIn(needle, out)

    def test_lists_and_state_commands(self) -> None:
        self.run_cli("init", "--no-require-tokens")
        self.activate("OPS1")
        self.run_cli("task", "create", "T1", "x", "--actor", "OPS1")
        self.run_cli("task", "claim", "T1", "OPS1")
        self.assertEqual(self.run_cli("task", "status", "T1", "OPS1", "IN_PROGRESS")[0], 0)
        code, _, err = self.run_cli("task", "status", "T1", "OPS1", "DONE")
        self.assertEqual(code, 3)
        self.assertIn("evidence", err)
        self.assertEqual(self.run_cli("task", "status", "T1", "OPS1", "DONE", "--evidence", "tests")[0], 0)
        self.assertIn("T1", self.run_cli("task", "list")[1])
        self.run_cli("worker", "register", "W1", "OPS1")
        self.assertEqual(self.run_cli("worker", "state", "W1", "IDLE")[0], 0)
        self.assertIn("W1", self.run_cli("worker", "list")[1])
        self.assertIn("OPS1", self.run_cli("operator", "list")[1])
        self.assertEqual(self.run_cli("operator", "heartbeat", "OPS1")[0], 0)
        self.assertEqual(self.run_cli("stale", "--minutes", "60")[0], 0)
        self.run_cli("file", "claim", "/a", "OPS1")
        self.assertIn("/a", self.run_cli("file", "list")[1])
        self.assertEqual(self.run_cli("file", "release", "/a", "OPS1")[0], 0)

    def test_worker_finish_needs_evidence_on_the_command_line(self) -> None:
        self.run_cli("init", "--no-require-tokens")
        self.activate("OPS1")
        self.run_cli("worker", "register", "W1", "OPS1")
        code, _, err = self.run_cli("worker", "state", "W1", "FINISHED")
        self.assertEqual(code, 3)
        self.assertIn("evidence", err)
        self.assertEqual(self.run_cli("worker", "state", "W1", "FINISHED", "--evidence", "report read")[0], 0)

    def test_verify_and_doctor(self) -> None:
        self.run_cli("init", "--no-require-tokens")
        self.assertEqual(self.run_cli("verify")[0], 0)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("python", out.lower())

    def test_usage_errors(self) -> None:
        self.assertEqual(self.run_cli("nosuchcommand")[0], 2)
        self.assertEqual(self.run_cli()[0], 2)

    def test_commands_before_init_say_so(self) -> None:
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 4)
        self.assertIn("init", err)
