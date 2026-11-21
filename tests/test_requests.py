import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import clean_env  # noqa: F401
from helpers import RegistryCase
from project_synergy import errors as E
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestRequestsAndInbox(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.active("OPS2")
        R.register_operator(self.conn, "OPS3")

    def inbox(self, op: str, unread: bool = True) -> list[str]:
        return [f"{n['kind']}:{n['subject']}" for n in R.list_notifications(self.conn, op, unread_only=unread)]

    def test_a_request_gets_an_automatic_id_a_root_task_and_notifies_the_other_active_operators(self) -> None:
        rid = R.create_request(self.conn, "Please tidy the project notes", origin="OPS1")
        self.assertEqual(rid, "REQ-0001")
        task = [t for t in R.list_tasks(self.conn) if t["id"] == rid][0]
        self.assertEqual((task["status"], task["owner"], task["request_id"]), ("OPEN", None, rid))
        self.assertEqual(self.inbox("OPS2"), ["request:REQ-0001"])
        self.assertEqual(self.inbox("OPS1"), [])
        self.assertEqual(self.inbox("OPS3"), [], "a PENDING operator gets nothing")
        self.assertEqual(R.create_request(self.conn, "Second one", origin="OPS2"), "REQ-0002")

    def test_a_request_needs_text_and_an_active_origin(self) -> None:
        with self.assertRaises(E.ValidationError):
            R.create_request(self.conn, "   ", origin="OPS1")
        with self.assertRaises(E.NotActive):
            R.create_request(self.conn, "hello", origin="OPS3")

    def test_the_other_operator_can_take_the_request_exactly_once_and_the_origin_hears_about_it(self) -> None:
        rid = R.create_request(self.conn, "Check the install", origin="OPS1")
        self.assertTrue(R.take_request(self.conn, rid, "OPS2"))
        with self.assertRaises(E.AlreadyOwned):
            R.take_request(self.conn, rid, "OPS1")
        self.assertIn("claimed:REQ-0001", self.inbox("OPS1"))

    def test_delegation_notifies_the_assignee_and_completion_notifies_the_origin_with_the_evidence(self) -> None:
        rid = R.create_request(self.conn, "Write the notes", origin="OPS1")
        R.create_task(self.conn, "N-1", "draft the notes", actor="OPS1", request_id=rid)
        R.assign_task(self.conn, "N-1", "OPS2", by="OPS1")
        self.assertIn("assigned:N-1", self.inbox("OPS2"))
        R.set_task_status(self.conn, "N-1", "OPS2", "DONE", evidence="notes.md written, 120 lines")
        done = [n for n in R.list_notifications(self.conn, "OPS1") if n["kind"] == "task_update"]
        self.assertEqual(len(done), 1)
        self.assertIn("notes.md written", done[0]["body"])

    def test_notifications_can_be_read_once_and_only_by_their_owner(self) -> None:
        R.create_request(self.conn, "x", origin="OPS1")
        first = R.list_notifications(self.conn, "OPS2")[0]["id"]
        R.mark_read(self.conn, "OPS2", [first])
        self.assertEqual(self.inbox("OPS2"), [])
        self.assertEqual(len(self.inbox("OPS2", unread=False)), 1)
        R.create_request(self.conn, "y", origin="OPS1")
        with self.assertRaises(E.NotOwner):
            R.mark_read(self.conn, "OPS1", [R.list_notifications(self.conn, "OPS2")[0]["id"]])

    def test_only_the_origin_closes_a_request_and_the_others_are_told(self) -> None:
        rid = R.create_request(self.conn, "Do it", origin="OPS1")
        with self.assertRaises(E.NotOwner):
            R.close_request(self.conn, rid, "OPS2", summary="done")
        with self.assertRaises(E.MissingEvidence):
            R.close_request(self.conn, rid, "OPS1", summary=" ")
        R.close_request(self.conn, rid, "OPS1", summary="all finished")
        self.assertEqual(R.get_request(self.conn, rid)["status"], "DONE")
        self.assertIn("request_done:REQ-0001", self.inbox("OPS2"))

    def test_the_report_gives_one_consolidated_view_of_a_request(self) -> None:
        rid = R.create_request(self.conn, "Prepare the release notes", origin="OPS1")
        R.take_request(self.conn, rid, "OPS2")
        R.create_task(self.conn, "N-1", "collect the changes", actor="OPS2", request_id=rid)
        R.claim_task(self.conn, "N-1", "OPS2")
        R.set_task_status(self.conn, "N-1", "OPS2", "DONE", evidence="48 changes listed")
        text = R.request_report(self.conn, rid)
        for needle in ("REQ-0001", "Prepare the release notes", "asked by OPS1", "N-1", "completed", "48 changes listed", "OPS2"):
            self.assertIn(needle, text)

    def test_the_report_says_who_is_listening_and_what_waits(self) -> None:
        rid = R.create_request(self.conn, "Review this", origin="OPS1")
        text = R.request_report(self.conn, rid)
        self.assertIn("nobody has taken it yet", text)
        self.assertRegex(text, r"OPS2.*not listening")
        R.set_listening(self.conn, "OPS2")
        self.assertRegex(R.request_report(self.conn, rid), r"OPS2.*listening")
        self.assertIn("1 unread", R.request_report(self.conn, rid))

    def test_an_unclaimed_request_can_be_taken_by_the_origin_when_the_other_is_unavailable(self) -> None:
        rid = R.create_request(self.conn, "Urgent", origin="OPS1")
        self.assertTrue(R.take_request(self.conn, rid, "OPS1"))
        self.assertEqual([t for t in R.list_tasks(self.conn) if t["id"] == rid][0]["owner"], "OPS1")


def _take(args: tuple[str, str]) -> str:
    db, op = args
    conn = R.open_registry(db)
    try:
        R.take_request(conn, "REQ-0001", op)
        return "ok"
    except E.AlreadyOwned:
        return "conflict"
    finally:
        conn.close()


class TestRequestRace(RegistryCase):
    def test_many_operators_racing_to_take_one_request_give_one_winner(self) -> None:
        from concurrent.futures import ProcessPoolExecutor

        names = [f"OPS{i}" for i in range(1, 9)]
        for n in names:
            self.active(n)
        R.create_request(self.conn, "contested", origin="OPS1")
        with ProcessPoolExecutor(max_workers=8) as pool:
            out = list(pool.map(_take, [(self.db, n) for n in names]))
        self.assertEqual(out.count("ok"), 1)
        self.assertEqual(out.count("conflict"), len(names) - 1)


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        self.state, self.cfg = self.base / "state", self.base / "cfg"
        self.cfg.mkdir()
        (self.cfg / "config.toml").write_text('[operators]\ninitial = ["OPS1", "OPS2"]\n')
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.base), "PYTHONPATH": str(ROOT / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1", "SYNERGY_STATE_DIR": str(self.state),
                    "SYNERGY_CONFIG_DIR": str(self.cfg)}
        self.cli("init", "--no-require-tokens")
        conn = __import__("project_synergy.registry", fromlist=["x"]).open_registry(str(self.state / "registry.db"))
        conn.execute("UPDATE operators SET status='ACTIVE'")
        conn.execute("UPDATE settings SET value='1' WHERE key='require_tokens'")
        conn.close()

    def cli(self, *argv: str, op: str | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if op:
            env["SYNERGY_OPERATOR"] = op
        return subprocess.run([sys.executable, "-m", "project_synergy", *argv], env=env, capture_output=True, text=True,
                              check=False, timeout=60, cwd=str(ROOT))


class TestTakerFinish(CliCase):
    def test_the_taker_reports_the_result_and_only_the_asker_closes(self) -> None:
        self.cli("request", "add", "Do the small thing", op="OPS1")
        self.assertEqual(self.cli("request", "take", "REQ-0001", op="OPS2").returncode, 0)
        refused = self.cli("request", "done", "REQ-0001", "--summary", "x", op="OPS2")
        self.assertEqual(refused.returncode, 3)
        none = self.cli("request", "finish", "REQ-0001", "--evidence", "  ", op="OPS2")
        self.assertEqual(none.returncode, 3)
        stranger = self.cli("request", "finish", "REQ-0001", "--evidence", "mine", op="OPS1")
        self.assertEqual(stranger.returncode, 3)
        ok = self.cli("request", "finish", "REQ-0001", "--evidence", "E2E OK", op="OPS2")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("E2E OK", self.cli("inbox", "list", op="OPS1").stdout)
        self.assertEqual(self.cli("request", "done", "REQ-0001", "--summary", "confirmed", op="OPS1").returncode, 0)


class TestRequestCli(CliCase):
    def test_a_request_is_typed_once_and_the_other_operator_sees_it_in_its_inbox(self) -> None:
        added = self.cli("request", "add", "Please check the installer on a clean machine", op="OPS1")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertIn("REQ-0001", added.stdout)
        inbox = self.cli("inbox", "list", op="OPS2")
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        self.assertIn("REQ-0001", inbox.stdout)
        self.assertIn("Please check the installer", inbox.stdout)
        self.assertEqual(self.cli("inbox", "list", op="OPS1").stdout.count("REQ-0001"), 0)

    def test_the_session_identity_is_required_for_the_inbox(self) -> None:
        self.cli("request", "add", "hello", op="OPS1")
        refused = self.cli("inbox", "list")
        self.assertEqual(refused.returncode, 3)
        self.assertIn("identity", refused.stderr)
        self.assertEqual(self.cli("request", "add", "x", op="OPS9").returncode, 3)

    def test_inbox_read_and_request_flow_through_the_command_line(self) -> None:
        self.cli("request", "add", "Write the notes", op="OPS1")
        self.assertEqual(self.cli("request", "take", "REQ-0001", op="OPS2").returncode, 0)
        refused = self.cli("request", "take", "REQ-0001", op="OPS1")
        self.assertEqual(refused.returncode, 3)
        self.assertIn("already owned by OPS2", refused.stderr)
        self.assertEqual(self.cli("inbox", "read", "all", op="OPS1").returncode, 0)
        self.assertNotIn("claimed", self.cli("inbox", "list", op="OPS1").stdout)
        done = self.cli("request", "done", "REQ-0001", "--summary", "notes written", op="OPS1")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = self.cli("request", "status", "REQ-0001", op="OPS1")
        self.assertIn("DONE", report.stdout)
        self.assertIn("notes written", report.stdout)
        self.assertIn("REQ-0001", self.cli("request", "list", op="OPS1").stdout)

    def test_inbox_wait_times_out_quietly_when_nothing_arrives(self) -> None:
        t0 = time.time()
        done = self.cli("inbox", "wait", "--timeout", "2", op="OPS2")
        self.assertEqual(done.returncode, 5)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("nothing new", done.stdout)

    def test_inbox_wait_returns_at_once_when_something_is_already_unread(self) -> None:
        self.cli("request", "add", "already here", op="OPS1")
        t0 = time.time()
        done = self.cli("inbox", "wait", "--timeout", "30", op="OPS2")
        self.assertEqual(done.returncode, 0)
        self.assertIn("REQ-0001", done.stdout)
        self.assertLess(time.time() - t0, 10)


class TestEndToEnd(CliCase):
    def test_a_request_flows_from_one_operator_to_the_other_and_back_without_polling_by_either(self) -> None:
        results: dict[str, subprocess.CompletedProcess[str]] = {}

        def listen(name: str, op: str) -> None:
            results[name] = self.cli("inbox", "wait", "--timeout", "40", op=op)

        waiter = threading.Thread(target=listen, args=("ops2-first", "OPS2"))
        waiter.start()
        time.sleep(1.5)
        t0 = time.time()
        added = self.cli("request", "add", "Please write the changelog", op="OPS1")
        self.assertEqual(added.returncode, 0, added.stderr)
        waiter.join(timeout=30)
        self.assertFalse(waiter.is_alive(), "the waiting operator must be woken by the new request")
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(results["ops2-first"].returncode, 0)
        self.assertIn("REQ-0001", results["ops2-first"].stdout)

        waiter = threading.Thread(target=listen, args=("ops1-back", "OPS1"))
        waiter.start()
        time.sleep(1.5)
        self.assertEqual(self.cli("request", "take", "REQ-0001", op="OPS2").returncode, 0)
        self.assertEqual(self.cli("task", "create", "CL-1", "collect the changes", "--actor", "OPS2",
                                  "--request", "REQ-0001", op="OPS2").returncode, 0)
        self.assertEqual(self.cli("task", "claim", "CL-1", "OPS2", op="OPS2").returncode, 0)
        done = self.cli("task", "status", "CL-1", "OPS2", "DONE", "--evidence", "changelog has 12 entries", op="OPS2")
        self.assertEqual(done.returncode, 0, done.stderr)
        waiter.join(timeout=30)
        self.assertFalse(waiter.is_alive(), "the originating operator must be woken by the result")
        self.assertIn("changelog has 12 entries", results["ops1-back"].stdout + self.cli("inbox", "list", op="OPS1").stdout)

        self.assertEqual(self.cli("request", "done", "REQ-0001", "--summary", "changelog delivered", op="OPS1").returncode, 0)
        report = self.cli("request", "status", "REQ-0001", op="OPS1").stdout
        for needle in ("asked by OPS1", "CL-1", "changelog has 12 entries", "DONE", "changelog delivered"):
            self.assertIn(needle, report)


class TestMigration(unittest.TestCase):
    def test_a_version_2_registry_migrates_to_the_new_schema_with_a_backup_and_keeps_its_data(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "registry.db")
            raw = sqlite3.connect(db, isolation_level=None)
            raw.row_factory = sqlite3.Row
            raw.execute("BEGIN IMMEDIATE")
            for stmt in R._split(R._SCHEMA_V1):
                raw.execute(stmt)
            for stmt in R._MIGRATIONS[2]:
                raw.execute(stmt)
            raw.execute("INSERT INTO schema_version VALUES (2)")
            raw.execute("INSERT INTO settings VALUES ('max_workers','2')")
            raw.execute("INSERT INTO operators(id, status, registered_at, last_seen) VALUES ('ZED9','ACTIVE','t','t')")
            R._log(raw, "system", "registry.init", "registry", "schema 2")
            raw.execute("COMMIT")
            raw.close()
            migrated = R.open_registry(db)
            self.addCleanup(migrated.close)
            self.assertEqual(migrated.execute("SELECT version FROM schema_version").fetchone()[0], R.SCHEMA_VERSION)
            tables = {r[0] for r in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"requests", "notifications"} <= tables)
            row = migrated.execute("SELECT id, skills, availability, max_tasks FROM operators").fetchone()
            self.assertEqual(tuple(row), ("ZED9", "", "AVAILABLE", 3))
            self.assertTrue(any(".bak-v2-" in n for n in os.listdir(d)))
            self.assertTrue(R.verify_chain(migrated)[0])
