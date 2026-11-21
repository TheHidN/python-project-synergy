import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import clean_env  # noqa: F401
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


class AvailCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        (self.base / "cfg").mkdir()
        (self.base / "cfg" / "config.toml").write_text('[operators]\ninitial = ["OPS1", "OPS2", "OPS3"]\n')
        self.state = self.base / "state"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.base), "PYTHONPATH": str(ROOT / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1", "SYNERGY_STATE_DIR": str(self.state),
                    "SYNERGY_CONFIG_DIR": str(self.base / "cfg"), "SYNERGY_WAIT_POLL": "0.2"}
        self.cli("init", "--no-require-tokens")
        self.cli("rules", "set", "--hash", "b" * 64)
        self.db = str(self.state / "registry.db")
        conn = R.open_registry(self.db)
        conn.execute("UPDATE operators SET status='ACTIVE'")
        conn.execute("UPDATE settings SET value='1' WHERE key='require_tokens'")
        conn.close()
        self.procs: list[subprocess.Popen[str]] = []
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()

    def cli(self, *argv: str, op: str | None = None, extra: dict[str, str] | None = None,
            stdin: str = "") -> subprocess.CompletedProcess[str]:
        env = {**self.env, **(extra or {})}
        if op:
            env["SYNERGY_OPERATOR"] = op
        return subprocess.run([sys.executable, "-m", "project_synergy", *argv], env=env, input=stdin, capture_output=True,
                              text=True, check=False, timeout=60, cwd=str(ROOT))

    def spawn(self, *argv: str, op: str) -> subprocess.Popen[str]:
        p = subprocess.Popen([sys.executable, "-m", "project_synergy", *argv], env={**self.env, "SYNERGY_OPERATOR": op},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT))
        self.procs.append(p)
        return p

    def listening(self, op: str) -> bool:
        conn = R.open_registry(self.db)
        try:
            row = conn.execute("SELECT listening_at FROM operators WHERE id=?", (op,)).fetchone()
        finally:
            conn.close()
        return bool(row["listening_at"])

    def wait_until(self, cond, seconds: float = 15.0) -> bool:  # noqa: ANN001
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        return False


class TestWaitStaysAvailableWithoutWaking(AvailCase):
    def test_wait_without_a_timeout_stays_quiet_and_listening_until_real_work_arrives(self) -> None:
        p = self.spawn("inbox", "wait", op="OPS2")
        self.assertTrue(self.wait_until(lambda: self.listening("OPS2")))
        time.sleep(3)
        self.assertIsNone(p.poll(), "no idle wake-up: it must not exit on its own")
        self.cli("request", "add", "Do something", "--no-auto", op="OPS1")
        out, _ = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0)
        self.assertIn("REQ-0001", out)
        self.assertFalse(self.listening("OPS2"), "it clears its listening mark when it ends")

    def test_a_timeout_is_still_available_and_ends_with_the_nothing_new_code(self) -> None:
        done = self.cli("inbox", "wait", "--timeout", "1", op="OPS2")
        self.assertEqual(done.returncode, 5)

    def test_sigterm_clears_listening_and_ends_cleanly(self) -> None:
        p = self.spawn("inbox", "wait", op="OPS3")
        self.assertTrue(self.wait_until(lambda: self.listening("OPS3")))
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        self.assertEqual(p.returncode, 7)
        self.assertFalse(self.listening("OPS3"))


class TestReconnectAndRevocation(AvailCase):
    def test_work_that_arrived_while_offline_is_delivered_when_the_listener_restarts(self) -> None:
        first = self.spawn("inbox", "wait", op="OPS2")
        self.assertTrue(self.wait_until(lambda: self.listening("OPS2")))
        first.send_signal(signal.SIGTERM)
        first.communicate(timeout=20)
        self.cli("request", "add", "While you were away", "--no-auto", op="OPS1")
        again = self.cli("inbox", "wait", "--timeout", "5", op="OPS2")
        self.assertEqual(again.returncode, 0)
        self.assertIn("REQ-0001", again.stdout)

    def test_a_revoked_operator_is_told_and_stops_waiting(self) -> None:
        p = self.spawn("inbox", "wait", op="OPS3")
        self.assertTrue(self.wait_until(lambda: self.listening("OPS3")))
        self.assertEqual(self.cli("team", "revoke", "OPS3", "--reason", "left", op="OPS1").returncode, 0)
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 6)
        self.assertIn("no longer", out + err)

    def test_a_pending_or_unapproved_operator_cannot_wait_and_a_reauthorized_one_must_acknowledge_again(self) -> None:
        self.cli("operator", "register", "NEWB", op="OPS1")
        self.assertEqual(self.cli("inbox", "wait", "--timeout", "1", op="NEWB").returncode, 3)


class TestKeepaliveHook(AvailCase):
    def hook(self, op: str | None, payload: str = "{}", extra: dict[str, str] | None = None) -> str:
        done = self.cli("inbox", "hook", op=op, stdin=payload, extra=extra)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_a_session_about_to_stop_without_a_listener_is_asked_to_start_one_a_bounded_number_of_times(self) -> None:
        data = json.loads(self.hook("OPS2"))
        self.assertEqual(data["decision"], "block")
        self.assertIn("synergy inbox wait", data["reason"])
        outputs = [self.hook("OPS2") for _ in range(8)]
        self.assertLessEqual(len([o for o in outputs if o]), 2, "three reminders an hour in total")

    def test_it_is_silent_while_a_listener_is_running_or_the_operator_is_away_or_not_authorized(self) -> None:
        p = self.spawn("inbox", "wait", op="OPS2")
        self.assertTrue(self.wait_until(lambda: self.listening("OPS2")))
        self.assertEqual(self.hook("OPS2"), "")
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        self.assertEqual(self.cli("operator", "profile", "--availability", "BUSY", op="OPS2").returncode, 0)
        self.assertEqual(self.hook("OPS2"), "", "an operator that set itself away is left alone")
        self.cli("team", "suspend", "OPS3", "--reason", "r", op="OPS1")
        self.assertEqual(self.hook("OPS3"), "")
        self.cli("team", "revoke", "OPS3", "--reason", "r", op="OPS1")
        self.assertEqual(self.hook("OPS3"), "")
        self.assertEqual(self.hook(None), "")
        self.assertEqual(self.hook("OPS1", '{"stop_hook_active": true}'), "")
        self.assertEqual(self.hook("OPS1", extra={"SYNERGY_HOOK_DISABLED": "1"}), "")
        self.cli("dispatch", "pause", op="OPS1")
        self.assertEqual(self.hook("OPS1"), "")


class TestSessionStartHook(AvailCase):
    def start(self, op: str | None) -> str:
        done = self.cli("inbox", "session-start", op=op, stdin="{}")
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_a_started_or_resumed_operator_session_is_told_how_to_come_back_online(self) -> None:
        data = json.loads(self.start("OPS2"))
        text = data["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "SessionStart")
        for needle in ("OPS2", "synergy team me", "synergy inbox wait", "background"):
            self.assertIn(needle, text)

    def test_unrelated_unknown_and_revoked_sessions_get_nothing(self) -> None:
        self.assertEqual(self.start(None), "")
        self.assertEqual(self.start("NOBODY"), "")
        self.cli("team", "revoke", "OPS3", "--reason", "r", op="OPS1")
        self.assertEqual(self.start("OPS3"), "")


class TestReminderFooterAndWatchdog(AvailCase):
    def test_an_operator_command_reminds_a_session_that_is_not_listening_at_most_every_ten_minutes(self) -> None:
        conn = R.open_registry(self.db)
        conn.execute("UPDATE operators SET last_seen='2000-01-01T00:00:00+00:00'")
        conn.close()
        first = self.cli("status", op="OPS2")
        self.assertIn("inbox wait", first.stderr)
        second = self.cli("status", op="OPS2")
        self.assertNotIn("inbox wait", second.stderr)
        self.assertNotIn("inbox wait", self.cli("status").stderr, "no operator identity: no reminder")

    def test_no_reminder_while_listening(self) -> None:
        p = self.spawn("inbox", "wait", op="OPS2")
        self.assertTrue(self.wait_until(lambda: self.listening("OPS2")))
        self.assertNotIn("not listening", self.cli("status", op="OPS2").stderr)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)

    def test_the_watchdog_reports_operators_that_are_offline_with_work_waiting(self) -> None:
        self.cli("request", "add", "Waiting for you", "--no-auto", op="OPS1")
        conn = R.open_registry(self.db)
        conn.execute("UPDATE notifications SET created_at='2000-01-01T00:00:00+00:00'")
        conn.close()
        out = self.cli("watchdog", "--minutes", "5")
        self.assertEqual(out.returncode, 3)
        self.assertIn("OPS2", out.stdout)
        self.assertIn("unread", out.stdout)
        self.cli("inbox", "read", "all", op="OPS2")
        self.cli("inbox", "read", "all", op="OPS3")
        self.assertEqual(self.cli("watchdog", "--minutes", "5").returncode, 0)


class TestSharedPoolAndManyOperators(AvailCase):
    def test_listening_operators_and_an_agent_are_all_woken_by_their_own_work(self) -> None:
        self.assertEqual(self.cli("agent", "register", "SEC", "--approval-ref", "t", "--skills", "security", op="OPS1").returncode, 0)
        import hashlib

        packet = (self.state / "packets" / "SEC.md").read_bytes()
        self.assertEqual(self.cli("operator", "ack", "SEC", hashlib.sha256(packet).hexdigest(), op="SEC").returncode, 0)
        waiters = {n: self.spawn("inbox", "wait", op=n) for n in ("OPS2", "OPS3", "SEC")}
        for n in waiters:
            self.assertTrue(self.wait_until(lambda n=n: self.listening(n)), n)
        self.assertEqual(self.cli("agent", "request", "Review it", "--needs", "security", op="OPS1").returncode, 0)
        out, _ = waiters["SEC"].communicate(timeout=20)
        self.assertIn("JOB-0001", out)
        self.assertIsNone(waiters["OPS2"].poll())
        self.assertIsNone(waiters["OPS3"].poll())
        self.cli("request", "add", "For the team", op="OPS1")
        woke = 0
        for n in ("OPS2", "OPS3"):
            try:
                waiters[n].communicate(timeout=20)
                woke += 1
            except subprocess.TimeoutExpired:
                pass
        self.assertGreaterEqual(woke, 1, "the dispatched request reached a listening operator")


if __name__ == "__main__":
    unittest.main()
