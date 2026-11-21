import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from concurrent.futures import ProcessPoolExecutor

from project_synergy import errors as E
from project_synergy import registry as R

N = 20
HASH = "a" * 64


def _claim_task(args: tuple[str, str]) -> str:
    db, op = args
    conn = R.open_registry(db)
    try:
        R.claim_task(conn, "T1", op)
        return "ok"
    except E.AlreadyOwned as e:
        return f"conflict:{e.owner}"
    finally:
        conn.close()


def _claim_file(args: tuple[str, str]) -> str:
    db, op = args
    conn = R.open_registry(db)
    try:
        R.claim_file(conn, "/shared/settings.conf", op)
        return "ok"
    except E.FileOwned as e:
        return f"conflict:{e.owner}"
    finally:
        conn.close()


def _register(args: tuple[str, str]) -> str:
    db, op = args
    conn = R.open_registry(db)
    try:
        R.register_operator(conn, op)
        return "ok"
    except E.DuplicateOperator:
        return "dup"
    finally:
        conn.close()


def _worker(args: tuple[str, str]) -> str:
    db, op = args
    conn = R.open_registry(db)
    try:
        R.register_worker(conn, f"W-{op}", op)
        return "ok"
    except E.WorkerLimit:
        return "limit"
    finally:
        conn.close()


class TestConcurrency(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "registry.db")
        self.conn = R.open_registry(self.db)
        self.addCleanup(self.conn.close)
        R.set_setting(self.conn, "rules_hash", HASH)
        R.set_setting(self.conn, "require_tokens", "0")
        self.ops = [f"OPS{i}" for i in range(N)]
        for op in self.ops:
            R.register_operator(self.conn, op)
            R.ack_operator(self.conn, op, HASH)

    def race(self, fn, items):  # type: ignore[no-untyped-def]
        with ProcessPoolExecutor(max_workers=N) as pool:
            return list(pool.map(fn, items))

    def test_twenty_racers_one_task_exactly_one_winner(self) -> None:
        R.create_task(self.conn, "T1", "contested", actor="OPS0")
        out = self.race(_claim_task, [(self.db, op) for op in self.ops])
        winners = [r for r in out if r == "ok"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len([r for r in out if r.startswith("conflict:")]), N - 1)
        owner = R.list_tasks(self.conn)[0]["owner"]
        self.assertEqual({r for r in out if r.startswith("conflict:")}, {f"conflict:{owner}"})

    def test_twenty_racers_one_file_exactly_one_winner(self) -> None:
        out = self.race(_claim_file, [(self.db, op) for op in self.ops])
        self.assertEqual(out.count("ok"), 1)
        self.assertEqual(len(R.list_files(self.conn)), 1)

    def test_twenty_registrations_of_one_id_one_row(self) -> None:
        out = self.race(_register, [(self.db, "NEWOP")] * N)
        self.assertEqual(out.count("ok"), 1)
        self.assertEqual(out.count("dup"), N - 1)
        self.assertEqual(len([o for o in R.list_operators(self.conn) if o["id"] == "NEWOP"]), 1)

    def test_worker_limit_holds_under_race(self) -> None:
        out = self.race(_worker, [(self.db, op) for op in self.ops])
        self.assertEqual(out.count("ok"), 2)
        self.assertEqual(out.count("limit"), N - 2)

    def test_interrupted_transaction_rolls_back_without_reassignment(self) -> None:
        R.create_task(self.conn, "T1", "x", actor="OPS0")
        R.claim_task(self.conn, "T1", "OPS0")
        code = textwrap.dedent(
            """
            import os, sqlite3, sys
            c = sqlite3.connect(sys.argv[1], isolation_level=None)
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE tasks SET status='DONE' WHERE id='T1'")
            os._exit(1)
            """
        )
        before = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        proc = subprocess.run([sys.executable, "-c", code, self.db], check=False)
        self.assertEqual(proc.returncode, 1)
        row = R.list_tasks(self.conn)[0]
        self.assertEqual((row["owner"], row["status"]), ("OPS0", "CLAIMED"))
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], before)
        self.assertTrue(R.verify_chain(self.conn)[0])
        with self.assertRaises(E.AlreadyOwned):
            R.claim_task(self.conn, "T1", "OPS1")

    def test_repeated_operations_do_not_corrupt(self) -> None:
        R.create_task(self.conn, "T1", "x", actor="OPS0")
        for _ in range(5):
            R.open_registry(self.db).close()
            R.claim_task(self.conn, "T1", "OPS0")
            R.claim_file(self.conn, "/f", "OPS0")
        self.assertEqual(len(R.list_files(self.conn)), 1)
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertTrue(R.verify_chain(self.conn)[0])
