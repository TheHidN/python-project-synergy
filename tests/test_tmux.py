import contextlib
import io
import itertools
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import RULES_HASH

import clean_env  # noqa: F401
from project_synergy import cli
from project_synergy import config as C
from project_synergy import registry as R
from project_synergy import errors as E
from project_synergy import registry as R
from project_synergy import tmux as T

HAVE_TMUX = shutil.which("tmux") is not None
_COUNTER = itertools.count()


def _socket(prefix: str) -> str:
    return f"{prefix}{os.getpid()}x{next(_COUNTER)}"


class TestClassify(unittest.TestCase):
    def test_states(self) -> None:
        self.assertEqual(T.classify("working... esc to interrupt", "claude"), "WORKING")
        self.assertEqual(T.classify("Do you want to proceed?\n1. Yes", "claude"), "WAITING")
        self.assertEqual(T.classify("some output\n" + chr(0x276F) + " \n", "claude"), "IDLE")
        self.assertEqual(T.classify("", "claude"), "UNKNOWN")
        self.assertEqual(T.classify("anything", "bash"), "RUNNING")

    def test_waiting_beats_idle_footer(self) -> None:
        text = "Do you want to proceed?\n" + chr(0x276F) + " 1. Yes\nbypass permissions on"
        self.assertEqual(T.classify(text, "claude"), "WAITING")


class TestGuards(unittest.TestCase):
    def test_bypass_refused_unless_explicit(self) -> None:
        for argv in (["claude", "--permission-mode", "bypassPermissions"],
                     ["claude", "--dangerously-skip-permissions"],
                     ["claude", "--permission-mode=bypassPermissions"]):
            with self.assertRaises(E.ValidationError):
                T.check_command_allowed(argv)
            T.check_command_allowed(argv, allow_bypass=True)
        T.check_command_allowed(["claude", "--permission-mode", "acceptEdits"])

    def test_default_socket_and_protected_sessions_refused(self) -> None:
        with self.assertRaises(E.ValidationError):
            T.check_target(C.Config(tmux_socket="default"))
        with self.assertRaises(E.ValidationError):
            T.check_target(C.Config(tmux_session="CODING", protected_sessions=("CODING",)))
        T.check_target(C.Config(tmux_socket="synergy", tmux_session="SYNERGY"))


@unittest.skipUnless(HAVE_TMUX, "tmux is not installed")
class TestManagedTmux(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = C.Config(tmux_socket=_socket("syntest"), tmux_session="SYNTEST",
                            protected_sessions=("CODING",))
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = R.open_registry(os.path.join(self._tmp.name, "registry.db"))
        self.addCleanup(self.conn.close)
        R.set_setting(self.conn, "rules_hash", RULES_HASH)
        R.set_setting(self.conn, "require_tokens", "0")
        for op in ("OPS1", "OPS2"):
            R.register_operator(self.conn, op)
            R.ack_operator(self.conn, op, RULES_HASH)

    def default_server_sessions(self) -> str:
        out = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True,
                             check=False)
        return out.stdout

    def test_ensure_is_idempotent_and_isolated(self) -> None:
        before = self.default_server_sessions()
        self.assertTrue(T.ensure_session(self.cfg, command="sleep 12"))
        self.assertFalse(T.ensure_session(self.cfg, command="sleep 12"))
        self.assertTrue(T.has_session(self.cfg))
        self.assertEqual(before, self.default_server_sessions())
        self.assertNotIn("SYNTEST", self.default_server_sessions())

    def test_start_list_inspect(self) -> None:
        T.ensure_session(self.cfg, command="sleep 12")
        T.start_window(self.cfg, "W1", self._tmp.name, ["sleep", "10"])
        names = [w["name"] for w in T.list_windows(self.cfg)]
        self.assertIn("W1", names)
        info = T.inspect_window(self.cfg, "W1")
        self.assertEqual(info["state"], "RUNNING")
        self.assertNotIn("text", info)
        self.assertIn("text", T.inspect_window(self.cfg, "W1", include_text=True))

    def test_duplicate_window_and_missing_session_refused(self) -> None:
        with self.assertRaises(E.NotFound):
            T.start_window(self.cfg, "W1", self._tmp.name, ["sleep", "5"])
        T.ensure_session(self.cfg, command="sleep 12")
        T.start_window(self.cfg, "W1", self._tmp.name, ["sleep", "10"])
        with self.assertRaises(E.WorkerExists):
            T.start_window(self.cfg, "W1", self._tmp.name, ["sleep", "10"])

    def test_bad_names_and_directory_refused(self) -> None:
        T.ensure_session(self.cfg, command="sleep 12")
        with self.assertRaises(E.ValidationError):
            T.start_window(self.cfg, "bad name;", self._tmp.name, ["sleep", "5"])
        with self.assertRaises(E.ValidationError):
            T.start_window(self.cfg, "W9", "/no/such/dir", ["sleep", "5"])
        with self.assertRaises(E.NotFound):
            T.inspect_window(self.cfg, "NOPE")

    def test_worker_start_registers_then_opens_window_and_respects_limit(self) -> None:
        T.ensure_session(self.cfg, command="sleep 12")
        T.start_worker(self.conn, self.cfg, "W1", "OPS1", directory=self._tmp.name, argv=["sleep", "10"])
        T.start_worker(self.conn, self.cfg, "W2", "OPS2", directory=self._tmp.name, argv=["sleep", "10"])
        windows_before = len(T.list_windows(self.cfg))
        with self.assertRaises(E.WorkerLimit):
            T.start_worker(self.conn, self.cfg, "W3", "OPS1", directory=self._tmp.name, argv=["sleep", "10"])
        self.assertEqual(windows_before, len(T.list_windows(self.cfg)))
        self.assertEqual(len(R.list_workers(self.conn)), 2)

    def test_worker_start_refuses_bypass_before_registering(self) -> None:
        T.ensure_session(self.cfg, command="sleep 12")
        with self.assertRaises(E.ValidationError):
            T.start_worker(self.conn, self.cfg, "W1", "OPS1", directory=self._tmp.name,
                           argv=["claude", "--dangerously-skip-permissions"])
        self.assertEqual(R.list_workers(self.conn), [])

    def test_failed_window_leaves_worker_unknown_not_deleted(self) -> None:
        with self.assertRaises(E.NotFound):
            T.start_worker(self.conn, self.cfg, "W1", "OPS1", directory=self._tmp.name, argv=["sleep", "5"])
        self.assertEqual(R.list_workers(self.conn), [])

    def test_protected_session_untouched_by_managed_operations(self) -> None:
        def snap() -> str | None:
            out = subprocess.run(["tmux", "list-windows", "-t", "CODING", "-F", "#{window_id} #{window_name}"],
                                 capture_output=True, text=True, check=False)
            return out.stdout if out.returncode == 0 else None

        before = snap()
        if before is None:
            self.skipTest("no CODING session here")
        T.ensure_session(self.cfg, command="sleep 12")
        T.start_worker(self.conn, self.cfg, "W1", "OPS1", directory=self._tmp.name, argv=["sleep", "10"])
        T.list_windows(self.cfg)
        T.inspect_window(self.cfg, "W1")
        self.assertEqual(before, snap())


@unittest.skipUnless(HAVE_TMUX, "tmux is not installed")
class TestTmuxCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        cfgdir = os.path.join(self._tmp.name, "cfg")
        os.makedirs(cfgdir)
        sock = _socket("syncli")
        with open(os.path.join(cfgdir, "config.toml"), "w") as f:
            f.write(f'[tmux]\nsocket = "{sock}"\nsession = "SYNCLI"\n')
        for k, v in (("SYNERGY_CONFIG_DIR", cfgdir), ("SYNERGY_STATE_DIR", os.path.join(self._tmp.name, "st")),
                     ("SYNERGY_TMUX_TEST_COMMAND", "sleep 12"), ("SYNERGY_ALLOW_TEST_HOOKS", "1")):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_flow(self) -> None:
        self.assertEqual(self.run_cli("init", "--no-require-tokens")[0], 0)
        self.run_cli("rules", "set", "--hash", "x" * 64)
        self.run_cli("operator", "register", "OPS1")
        conn = R.open_registry(os.path.join(self._tmp.name, "st", "registry.db"))
        conn.execute("UPDATE operators SET trust='APPROVED', admin=1, approved_by='test', approval_ref='test' WHERE id='OPS1'")
        conn.close()
        self.run_cli("operator", "ack", "OPS1", "x" * 64)
        code, out, _ = self.run_cli("tmux", "ensure")
        self.assertEqual(code, 0)
        self.assertIn("SYNCLI", out)
        code, out, err = self.run_cli("worker", "start", "W1", "OPS1", "--dir", self._tmp.name, "--", "sleep", "10")
        self.assertEqual(code, 0, out + err)
        code, out, _ = self.run_cli("tmux", "windows")
        self.assertEqual(code, 0)
        self.assertIn("W1", out)
        code, out, _ = self.run_cli("tmux", "inspect", "W1")
        self.assertEqual(code, 0)
        self.assertIn("RUNNING", out)
        code, out, _ = self.run_cli("tmux", "attach-hint")
        self.assertIn("tmux -L", out)
        code, _, err = self.run_cli("worker", "start", "W2", "OPS1", "--dir", self._tmp.name, "--",
                                    "claude", "--dangerously-skip-permissions")
        self.assertEqual(code, 3)
        self.assertIn("bypass", err)
