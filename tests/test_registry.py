import sqlite3

from helpers import RULES_HASH, RegistryCase

from project_synergy import errors as E
from project_synergy import registry as R


class TestInitAndOperators(RegistryCase):
    def test_init_is_idempotent(self) -> None:
        before = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        again = R.open_registry(self.db)
        self.addCleanup(again.close)
        after = again.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(again.execute("SELECT version FROM schema_version").fetchone()[0], R.SCHEMA_VERSION)

    def test_register_starts_pending(self) -> None:
        R.register_operator(self.conn, "ops3")
        row = [o for o in R.list_operators(self.conn) if o["id"] == "OPS3"][0]
        self.assertEqual(row["status"], "PENDING")

    def test_duplicate_registration_rejected_even_with_other_case(self) -> None:
        R.register_operator(self.conn, "OPS3")
        with self.assertRaises(E.DuplicateOperator):
            R.register_operator(self.conn, "ops3")
        self.assertEqual(len(R.list_operators(self.conn)), 1)

    def test_bad_ids_rejected(self) -> None:
        for bad in ("", "has space", "x" * 33, "a;b", "../x"):
            with self.assertRaises(E.ValidationError):
                R.register_operator(self.conn, bad)

    def test_ack_needs_matching_rules_hash(self) -> None:
        R.register_operator(self.conn, "OPS1")
        with self.assertRaises(E.BadAck):
            R.ack_operator(self.conn, "OPS1", "b" * 64)
        R.ack_operator(self.conn, "OPS1", RULES_HASH)
        self.assertEqual(R.list_operators(self.conn)[0]["status"], "ACTIVE")

    def test_ack_unknown_operator(self) -> None:
        with self.assertRaises(E.NotFound):
            R.ack_operator(self.conn, "NOPE", RULES_HASH)

    def test_pending_operator_cannot_claim_or_be_assigned(self) -> None:
        self.active("OPS1")
        R.register_operator(self.conn, "OPS3")
        R.create_task(self.conn, "T1", "x", actor="OPS1")
        with self.assertRaises(E.NotActive):
            R.claim_task(self.conn, "T1", "OPS3")
        with self.assertRaises(E.NotActive):
            R.assign_task(self.conn, "T1", "OPS3", by="OPS1")
        self.assertIsNone(R.list_tasks(self.conn)[0]["owner"])


class TestTasks(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.active("OPS2")

    def test_task_ids_unique(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        with self.assertRaises(E.TaskExists):
            R.create_task(self.conn, "T1", "b", actor="OPS1")

    def test_claim_and_conflict_message(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        self.assertTrue(R.claim_task(self.conn, "T1", "OPS1"))
        with self.assertRaises(E.AlreadyOwned) as cm:
            R.claim_task(self.conn, "T1", "OPS2")
        self.assertEqual(cm.exception.owner, "OPS1")
        self.assertIn("OPS1", str(cm.exception))
        self.assertEqual(R.list_tasks(self.conn)[0]["owner"], "OPS1")

    def test_claim_by_owner_is_idempotent(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        self.assertTrue(R.claim_task(self.conn, "T1", "OPS1"))
        self.assertFalse(R.claim_task(self.conn, "T1", "OPS1"))

    def test_assign_gives_one_owner(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.assign_task(self.conn, "T1", "OPS2", by="OPS1")
        with self.assertRaises(E.AlreadyOwned):
            R.assign_task(self.conn, "T1", "OPS1", by="OPS1")
        self.assertEqual(R.list_tasks(self.conn)[0]["owner"], "OPS2")

    def test_dependencies_block_claim(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.create_task(self.conn, "T2", "b", actor="OPS1", depends_on=["T1"])
        with self.assertRaises(E.DependencyNotDone):
            R.claim_task(self.conn, "T2", "OPS2")
        R.claim_task(self.conn, "T1", "OPS1")
        R.set_task_status(self.conn, "T1", "OPS1", "DONE", evidence="tests green")
        self.assertTrue(R.claim_task(self.conn, "T2", "OPS2"))

    def test_unknown_dependency_rejected(self) -> None:
        with self.assertRaises(E.NotFound):
            R.create_task(self.conn, "T2", "b", actor="OPS1", depends_on=["NOPE"])

    def test_only_owner_changes_status_and_done_needs_evidence(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        with self.assertRaises(E.NotOwner):
            R.set_task_status(self.conn, "T1", "OPS2", "IN_PROGRESS")
        with self.assertRaises(E.MissingEvidence):
            R.set_task_status(self.conn, "T1", "OPS1", "DONE")
        R.set_task_status(self.conn, "T1", "OPS1", "IN_PROGRESS")
        R.set_task_status(self.conn, "T1", "OPS1", "DONE", evidence="log abc")
        self.assertEqual(R.list_tasks(self.conn)[0]["status"], "DONE")

    def test_bad_status_rejected(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        with self.assertRaises(E.ValidationError):
            R.set_task_status(self.conn, "T1", "OPS1", "WHATEVER")

    def test_only_owner_releases(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        with self.assertRaises(E.NotOwner):
            R.release_task(self.conn, "T1", "OPS2")
        R.release_task(self.conn, "T1", "OPS1")
        self.assertTrue(R.claim_task(self.conn, "T1", "OPS2"))

    def test_stale_operator_keeps_its_task(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        self.conn.execute("UPDATE operators SET last_seen='2000-01-01T00:00:00+00:00' WHERE id='OPS1'")
        self.assertEqual([o["id"] for o in R.stale_operators(self.conn, 60)], ["OPS1"])
        with self.assertRaises(E.AlreadyOwned):
            R.claim_task(self.conn, "T1", "OPS2")
        self.assertEqual(R.list_tasks(self.conn)[0]["owner"], "OPS1")

    def test_reassign_needs_a_decision_reference(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        with self.assertRaises(E.ValidationError):
            R.reassign_task(self.conn, "T1", "OPS2", decision_ref="", by="OPS1")
        R.reassign_task(self.conn, "T1", "OPS2", decision_ref="owner-decision-7", by="OPS1")
        self.assertEqual(R.list_tasks(self.conn)[0]["owner"], "OPS2")
        last = self.conn.execute("SELECT action, detail FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(last["action"], "task.reassign")
        self.assertIn("owner-decision-7", last["detail"])

    def test_direct_sql_cannot_steal_or_give_to_pending(self) -> None:
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        raw = self.raw()
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("UPDATE tasks SET owner='OPS2' WHERE id='T1'")
        R.register_operator(self.conn, "OPS3")
        R.create_task(self.conn, "T2", "b", actor="OPS1")
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("UPDATE tasks SET owner='OPS3' WHERE id='T2'")
        self.assertEqual(R.list_tasks(self.conn)[0]["owner"], "OPS1")


class TestFiles(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.active("OPS2")

    def test_exclusive_file_claim(self) -> None:
        self.assertTrue(R.claim_file(self.conn, "/x/a.conf", "OPS1"))
        with self.assertRaises(E.FileOwned) as cm:
            R.claim_file(self.conn, "/x/a.conf", "OPS2")
        self.assertEqual(cm.exception.owner, "OPS1")
        self.assertFalse(R.claim_file(self.conn, "/x/a.conf", "OPS1"))

    def test_worktree_covers_files_under_it_both_ways(self) -> None:
        R.claim_file(self.conn, "/w/tree", "OPS1", kind="worktree")
        with self.assertRaises(E.FileOwned):
            R.claim_file(self.conn, "/w/tree/src/a.py", "OPS2")
        R.claim_file(self.conn, "/w/other/a.py", "OPS2")
        with self.assertRaises(E.FileOwned):
            R.claim_file(self.conn, "/w/other", "OPS1", kind="worktree")
        self.assertTrue(R.claim_file(self.conn, "/w/treeish/a.py", "OPS2"))

    def test_paths_are_normalized(self) -> None:
        R.claim_file(self.conn, "/x/y/../a.conf", "OPS1")
        with self.assertRaises(E.FileOwned):
            R.claim_file(self.conn, "/x/a.conf", "OPS2")

    def test_release_only_by_owner(self) -> None:
        R.claim_file(self.conn, "/x/a.conf", "OPS1")
        with self.assertRaises(E.NotOwner):
            R.release_file(self.conn, "/x/a.conf", "OPS2")
        R.release_file(self.conn, "/x/a.conf", "OPS1")
        self.assertTrue(R.claim_file(self.conn, "/x/a.conf", "OPS2"))

    def test_pending_operator_cannot_claim_files(self) -> None:
        R.register_operator(self.conn, "OPS3")
        with self.assertRaises(E.NotActive):
            R.claim_file(self.conn, "/x/a.conf", "OPS3")


class TestWorkers(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.active("OPS2")

    def test_limit_is_two_across_operators(self) -> None:
        R.register_worker(self.conn, "W1", "OPS1")
        R.register_worker(self.conn, "W2", "OPS2")
        with self.assertRaises(E.WorkerLimit):
            R.register_worker(self.conn, "W3", "OPS1")

    def test_finished_worker_frees_a_slot(self) -> None:
        R.register_worker(self.conn, "W1", "OPS1")
        R.register_worker(self.conn, "W2", "OPS2")
        R.set_worker_state(self.conn, "W1", "FINISHED", evidence="deliverable checked")
        R.register_worker(self.conn, "W3", "OPS1")
        self.assertEqual(len([w for w in R.list_workers(self.conn) if w["state"] != "FINISHED"]), 2)

    def test_worker_states_validated(self) -> None:
        R.register_worker(self.conn, "W1", "OPS1")
        with self.assertRaises(E.ValidationError):
            R.set_worker_state(self.conn, "W1", "DONE")
        for s in ("RUNNING", "WORKING", "IDLE", "WAITING", "BLOCKED", "UNKNOWN"):
            R.set_worker_state(self.conn, "W1", s)
        R.set_worker_state(self.conn, "W1", "FINISHED", evidence="deliverable checked")

    def test_worker_needs_active_operator_and_owned_task(self) -> None:
        R.register_operator(self.conn, "OPS3")
        with self.assertRaises(E.NotActive):
            R.register_worker(self.conn, "W1", "OPS3")
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        with self.assertRaises(E.NotOwner):
            R.register_worker(self.conn, "W1", "OPS2", task_id="T1")
        R.register_worker(self.conn, "W1", "OPS1", task_id="T1")

    def test_duplicate_worker_id_rejected(self) -> None:
        R.register_worker(self.conn, "W1", "OPS1")
        with self.assertRaises(E.WorkerExists):
            R.register_worker(self.conn, "W1", "OPS2")


class TestEventsAndStatus(RegistryCase):
    def test_chain_verifies_and_detects_tampering(self) -> None:
        self.active("OPS1")
        R.create_task(self.conn, "T1", "a", actor="OPS1")
        ok, _ = R.verify_chain(self.conn)
        self.assertTrue(ok)
        raw = self.raw()
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("UPDATE events SET detail='x' WHERE seq=1")
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("DELETE FROM events WHERE seq=1")
        for trig in ("events_no_update", "events_no_delete"):
            raw.execute(f"DROP TRIGGER {trig}")
        raw.execute("UPDATE events SET detail='forged' WHERE seq=1")
        ok, msg = R.verify_chain(self.conn)
        self.assertFalse(ok)
        self.assertIn("1", msg)

    def test_status_report_is_read_only_and_lists_state(self) -> None:
        self.active("OPS1", role="orchestration")
        R.create_task(self.conn, "T1", "write the thing", actor="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        before = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        text = R.status_report(self.conn)
        after = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertEqual(before, after)
        for needle in ("OPS1", "T1", "ACTIVE", "CLAIMED"):
            self.assertIn(needle, text)
