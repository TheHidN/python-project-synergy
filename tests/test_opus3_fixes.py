import os
import sqlite3
import tempfile
import threading
import time
import unittest

import clean_env  # noqa: F401
from helpers import RULES_HASH, RegistryCase

from project_synergy import errors as E
from project_synergy import registry as R

REF = "approval record"


class AdminCase(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.conn.execute("UPDATE operators SET trust='APPROVED', approved_by='owner', approval_ref='installation', "
                          "approved_at='t', admin=1 WHERE id='OPS1'")
        R.set_setting(self.conn, "require_approval", "1")
        R.set_setting(self.conn, "bootstrapped", "1")

    def member(self, name: str, scopes: str = "*", kind: str = "OPERATOR", **kw: object) -> None:
        R.register_operator(self.conn, name, account=f"A-{name}", kind=kind, skills="x")
        R.approve_operator(self.conn, name, by="OPS1", approval_ref=REF, scopes=scopes, **kw)
        R.ack_operator(self.conn, name, RULES_HASH)


class TestMigrationNineGrantsAdminOnlyToApprovedActiveOperators(unittest.TestCase):
    def test_revoked_suspended_and_unapproved_operators_do_not_become_administrators(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "r.db")
            c = R.open_registry(db)
            for name, status, trust in (("GOOD", "ACTIVE", "APPROVED"), ("REVOKED", "SUSPENDED", "REVOKED"),
                                        ("PENDING1", "PENDING", ""), ("SUSP", "SUSPENDED", "APPROVED")):
                c.execute("INSERT INTO operators(id, status, trust, registered_at, last_seen) VALUES (?,?,?,'t','t')", (name, status, trust))
            c.execute("ALTER TABLE operators DROP COLUMN admin")
            c.execute("ALTER TABLE jobs DROP COLUMN project")
            c.execute("DELETE FROM settings WHERE key='bootstrapped'")
            c.execute("UPDATE schema_version SET version=8")
            c.close()
            c = R.open_registry(db)
            self.addCleanup(c.close)
            got = {r["id"]: r["admin"] for r in c.execute("SELECT id, admin FROM operators")}
            self.assertEqual(got["GOOD"], 1)
            self.assertEqual((got["REVOKED"], got["PENDING1"], got["SUSP"]), (0, 0, 0))


class TestTokensCannotBeSwitchedOffByAnyone(AdminCase):
    def test_only_an_authenticated_administrator_turns_checks_off(self) -> None:
        self.member("G", kind="AGENT")
        self.member("B")
        for who in ("G", "B"):
            with self.assertRaises((E.NotActive, E.AuthFailed)):
                R.set_require_tokens(self.conn, False, by=who)
        with self.assertRaises((E.NotActive, E.AuthFailed)):
            R.set_require_tokens(self.conn, False)
        R.set_require_tokens(self.conn, False, by="OPS1")


class TestAdminFlagLifecycle(AdminCase):
    def test_revoke_clears_admin_and_reauthorize_does_not_restore_it(self) -> None:
        R.register_operator(self.conn, "B")
        R.approve_operator(self.conn, "B", by="OPS1", approval_ref=REF, admin=True)
        R.ack_operator(self.conn, "B", RULES_HASH)
        R.revoke_operator(self.conn, "B", by="OPS1", reason="left")
        R.reauthorize_operator(self.conn, "B", by="OPS1", approval_ref="again")
        self.assertEqual([o for o in R.list_operators(self.conn) if o["id"] == "B"][0]["admin"], 0)

    def test_agents_are_never_administrators(self) -> None:
        R.register_operator(self.conn, "G", kind="AGENT")
        with self.assertRaises(E.ValidationError):
            R.approve_operator(self.conn, "G", by="OPS1", approval_ref=REF, admin=True)


class TestOwnerLevelCommandsNeedAnAdministrator(AdminCase):
    def test_rules_hash_pause_hook_and_tunables_are_administrator_only_after_setup(self) -> None:
        self.member("B")
        with self.assertRaises(E.NotActive):
            R.set_rules_hash(self.conn, "c" * 64, by="B")
        with self.assertRaises(E.NotActive):
            R.set_dispatch(self.conn, False, by="B")
        with self.assertRaises(E.NotActive):
            R.set_hook(self.conn, False, by="B")
        with self.assertRaises(E.NotActive):
            R.set_tunable(self.conn, "usage", 2, by="B")
        R.set_rules_hash(self.conn, "c" * 64, by="OPS1")
        R.set_dispatch(self.conn, False, by="OPS1")
        R.set_tunable(self.conn, "usage", 2, by="OPS1")

    def test_agents_cannot_record_usage_and_members_only_for_their_own_account(self) -> None:
        self.member("B")
        self.member("G", kind="AGENT")
        with self.assertRaises(E.NotActive):
            R.set_usage(self.conn, "A-B", 10, by="G")
        with self.assertRaises(E.NotActive):
            R.set_usage(self.conn, "A-OPS1", 10, by="B")
        R.set_usage(self.conn, "A-B", 10, by="B")
        R.set_usage(self.conn, "A-B", 20, by="OPS1")

    def test_only_the_requester_or_an_administrator_requeues_a_job(self) -> None:
        self.member("B")
        self.member("C")
        self.member("G", kind="AGENT")
        placed = R.request_agent(self.conn, "B", "job", agent_id="G")
        self.conn.execute("UPDATE operators SET last_seen='2000-01-01T00:00:00+00:00', listening_at='' WHERE id='G'")
        with self.assertRaises(E.NotOwner):
            R.requeue_job(self.conn, placed["id"], "C", stale_minutes=1)
        R.requeue_job(self.conn, placed["id"], "B", stale_minutes=1)


class TestJobGapsInThePool(AdminCase):
    def test_a_suspended_agent_is_not_reserved_even_when_named(self) -> None:
        self.member("G", kind="AGENT")
        R.suspend_work(self.conn, "G", by="OPS1", reason="r")
        placed = R.request_agent(self.conn, "OPS1", "job", agent_id="G")
        self.assertEqual(placed["status"], "QUEUED")

    def test_a_queued_job_is_not_promoted_after_its_project_goes_on_hold_and_handoff_checks_scope(self) -> None:
        self.member("G", kind="AGENT")
        self.member("B", scopes="alpha")
        rid = R.create_request(self.conn, "beta", origin="OPS1", project="beta")
        first = R.request_agent(self.conn, "OPS1", "first", agent_id="G")
        queued = R.request_agent(self.conn, "OPS1", "second", agent_id="G", request_id=rid)
        self.assertEqual(queued["status"], "QUEUED")
        R.set_hold(self.conn, "beta", True, by="OPS1", reason="hold")
        R.agent_next(self.conn, "G")
        R.finish_job(self.conn, first["id"], "G", result="done")
        self.assertEqual(R.get_job(self.conn, queued["id"])["status"], "QUEUED", "held project: not promoted")
        R.set_hold(self.conn, "beta", False, by="OPS1", reason="lift")
        with self.assertRaises(E.NotActive):
            R.handoff_job(self.conn, queued["id"], "OPS1", "B")

    def test_the_project_of_a_job_follows_its_task_too(self) -> None:
        self.member("G", kind="AGENT")
        rid = R.create_request(self.conn, "beta", origin="OPS1", project="beta")
        R.set_hold(self.conn, "beta", True, by="OPS1", reason="hold")
        with self.assertRaises(E.NotActive):
            R.request_agent(self.conn, "OPS1", "job", agent_id="G", task_id=rid)


class TestHookDoesNotWaitOnTheDatabase(AdminCase):
    def test_opening_a_current_registry_takes_no_write_lock(self) -> None:
        holder = sqlite3.connect(self.db, isolation_level=None, timeout=1)
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        started = time.time()
        conn = R.open_registry(self.db, timeout=1)
        self.addCleanup(conn.close)
        self.assertLess(time.time() - started, 3, "opening must not queue behind a writer")
        holder.execute("ROLLBACK")


if __name__ == "__main__":
    unittest.main()
