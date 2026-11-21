import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

import clean_env  # noqa: F401
from helpers import RegistryCase
from project_synergy import errors as E
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


def ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


class LifeCase(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        for n in ("OPS1", "OPS2", "OPS3"):
            self.active(n)
        self.conn.execute("UPDATE operators SET admin=1 WHERE id='OPS1'")
        self.rid = R.create_request(self.conn, "Two parts", origin="OPS1")
        self.ids = R.plan_request(self.conn, self.rid, "OPS1", [{"title": "a"}, {"title": "b"}])
        R.dispatch_tick(self.conn, "OPS1")

    def owner_of(self, tid: str) -> str:
        return [t for t in R.list_tasks(self.conn) if t["id"] == tid][0]["owner"] or ""

    def age_claims(self, minutes: float) -> None:
        self.conn.execute("UPDATE tasks SET claimed_at=? WHERE owner IS NOT NULL", (ago(minutes),))


class TestStart(LifeCase):
    def test_the_owner_acknowledges_and_starts_and_the_asker_is_told(self) -> None:
        tid = self.ids[0]
        owner = self.owner_of(tid)
        with self.assertRaises(E.NotOwner):
            R.start_task(self.conn, tid, "OPS1" if owner != "OPS1" else "OPS2")
        R.start_task(self.conn, tid, owner)
        row = [t for t in R.list_tasks(self.conn) if t["id"] == tid][0]
        self.assertEqual(row["status"], "IN_PROGRESS")
        self.assertIn("task_started", [n["kind"] for n in R.list_notifications(self.conn, "OPS1")])
        self.assertNotIn(f"assigned:{tid}", [f"{n['kind']}:{n['subject']}" for n in R.list_notifications(self.conn, owner)])
        with self.assertRaises(E.NotClaimable):
            R.start_task(self.conn, tid, owner)

    def test_every_lifecycle_stage_is_named_in_the_request_report(self) -> None:
        a, b = self.ids
        R.start_task(self.conn, a, self.owner_of(a))
        R.set_task_status(self.conn, a, self.owner_of(a), "DONE", evidence="a done")
        text = R.request_report(self.conn, self.rid)
        self.assertIn("completed", text)
        self.assertIn("assigned", text)
        R.start_task(self.conn, b, self.owner_of(b))
        self.assertIn("running", R.request_report(self.conn, self.rid))
        R.set_task_status(self.conn, b, self.owner_of(b), "DONE", evidence="b done")
        R.close_request(self.conn, self.rid, "OPS1", summary="reviewed")
        self.assertIn("accepted", R.request_report(self.conn, self.rid))


class TestReclaim(LifeCase):
    def test_an_unacknowledged_automatic_assignment_goes_back_to_the_pool_when_its_owner_is_quiet(self) -> None:
        tid = self.ids[0]
        first = self.owner_of(tid)
        self.age_claims(120)
        R.dispatch_tick(self.conn, "OPS1")
        second = self.owner_of(tid)
        self.assertTrue(second)
        self.assertNotEqual(second, first, "never given straight back to the quiet operator")
        self.assertIn(f"reclaimed:{tid}", [f"{n['kind']}:{n['subject']}" for n in R.list_notifications(self.conn, first)])
        actions = [e["action"] for e in self.conn.execute("SELECT action FROM events WHERE subject=?", (tid,))]
        self.assertIn("task.reclaim", actions)

    def test_started_tasks_listening_owners_and_manual_claims_are_left_alone(self) -> None:
        a, b = self.ids
        owner_a, owner_b = self.owner_of(a), self.owner_of(b)
        R.start_task(self.conn, a, owner_a)
        R.set_listening(self.conn, owner_b)
        self.age_claims(120)
        R.dispatch_tick(self.conn, "OPS1")
        self.assertEqual((self.owner_of(a), self.owner_of(b)), (owner_a, owner_b))
        rid2 = R.create_request(self.conn, "manual", origin="OPS1")
        R.set_dispatch(self.conn, False, by="OPS1")
        R.take_request(self.conn, rid2, "OPS3")
        R.set_dispatch(self.conn, True, by="OPS1")
        self.age_claims(300)
        self.conn.execute("UPDATE operators SET listening_at=''")
        R.dispatch_tick(self.conn, "OPS1")
        self.assertEqual(self.owner_of(rid2), "OPS3", "work an operator took itself is never reclaimed")


class TestNudges(LifeCase):
    def test_operators_with_unread_work_that_are_not_listening_are_listed_with_their_session_name(self) -> None:
        owner = self.owner_of(self.ids[0])
        R.set_session_name(self.conn, owner, "my-session-name")
        quiet = {n["operator"]: n for n in R.pending_nudges(self.conn, min_age_seconds=0)}
        self.assertIn(owner, quiet)
        self.assertEqual(quiet[owner]["session"], "my-session-name")
        R.set_listening(self.conn, owner)
        self.assertNotIn(owner, [n["operator"] for n in R.pending_nudges(self.conn, min_age_seconds=0)])

    def test_a_session_name_is_validated(self) -> None:
        with self.assertRaises(E.ValidationError):
            R.set_session_name(self.conn, "OPS1", "bad name\nwith newline")
        with self.assertRaises(E.ValidationError):
            R.set_session_name(self.conn, "OPS1", "uds:/run/x.sock")


class TestLifecycleCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = pathlib.Path(self._tmp.name)
        (base / "cfg").mkdir()
        (base / "cfg" / "config.toml").write_text('[operators]\ninitial = ["OPS1", "OPS2"]\n')
        self.state = base / "state"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(base), "PYTHONPATH": str(ROOT / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1", "SYNERGY_STATE_DIR": str(self.state),
                    "SYNERGY_CONFIG_DIR": str(base / "cfg")}
        self.cli("init", "--no-require-tokens")
        self.cli("rules", "set", "--hash", "b" * 64)
        conn = R.open_registry(str(self.state / "registry.db"))
        conn.execute("UPDATE operators SET status='ACTIVE'")
        conn.execute("UPDATE settings SET value='1' WHERE key='require_tokens'")
        conn.close()

    def cli(self, *argv: str, op: str | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if op:
            env["SYNERGY_OPERATOR"] = op
        return subprocess.run([sys.executable, "-m", "project_synergy", *argv], env=env, capture_output=True, text=True,
                              check=False, timeout=60, cwd=str(ROOT))

    def test_start_session_name_and_nudge_hints_through_the_command_line(self) -> None:
        self.assertEqual(self.cli("operator", "session", "--name", "ops-two-session", op="OPS2").returncode, 0)
        added = self.cli("request", "add", "Do the small thing", op="OPS1")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertIn("NUDGE OPS2", added.stdout)
        self.assertIn("ops-two-session", added.stdout)
        nudges = self.cli("dispatch", "nudge", "--min-age", "0").stdout
        self.assertIn("OPS2", nudges)
        started = self.cli("task", "start", "REQ-0001", op="OPS2")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertIn("running", self.cli("request", "status", "REQ-0001").stdout)


class TestShortTaskStatus(TestLifecycleCli):
    def test_the_status_command_takes_the_operator_from_the_session_and_still_accepts_the_long_form(self) -> None:
        self.cli("request", "add", "Short form", "--no-auto", op="OPS1")
        self.assertEqual(self.cli("request", "take", "REQ-0001", op="OPS2").returncode, 0)
        short = self.cli("task", "status", "REQ-0001", "IN_PROGRESS", op="OPS2")
        self.assertEqual(short.returncode, 0, short.stdout + short.stderr)
        done = self.cli("task", "status", "REQ-0001", "DONE", "--evidence", "short form works", op="OPS2")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.cli("request", "add", "Long form", "--no-auto", op="OPS1")
        self.cli("request", "take", "REQ-0002", op="OPS2")
        long = self.cli("task", "status", "REQ-0002", "OPS2", "DONE", "--evidence", "long form works", op="OPS2")
        self.assertEqual(long.returncode, 0, long.stdout + long.stderr)
        nobody = self.cli("task", "status", "REQ-0002", "DONE", "--evidence", "x")
        self.assertEqual(nobody.returncode, 3, "the short form needs a session identity")
