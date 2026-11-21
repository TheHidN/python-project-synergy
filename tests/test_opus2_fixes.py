import os
import tempfile
import unittest

import clean_env  # noqa: F401
from helpers import RULES_HASH, RegistryCase
from project_synergy import errors as E
from project_synergy import registry as R

REF = "owner approval record"


class AdminCase(RegistryCase):
    """OPS1 is an administrator installed by the owner; everyone else is an ordinary approved member."""

    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.conn.execute("UPDATE operators SET trust='APPROVED', approved_by='owner', approval_ref='installation', "
                          "approved_at='t', admin=1 WHERE id='OPS1'")
        R.set_setting(self.conn, "require_approval", "1")

    def member(self, name: str, scopes: str = "*", **kw: object) -> None:
        R.register_operator(self.conn, name, account=f"A-{name}")
        R.approve_operator(self.conn, name, by="OPS1", approval_ref=REF, scopes=scopes, **kw)
        R.ack_operator(self.conn, name, RULES_HASH)


class TestAdminRole(AdminCase):
    def test_ordinary_members_cannot_approve_reauthorize_revoke_suspend_hold_or_reset(self) -> None:
        self.member("A", scopes="alpha")
        self.member("B", scopes="beta")
        R.register_operator(self.conn, "X")
        for call in (lambda: R.approve_operator(self.conn, "X", by="A", approval_ref=REF, scopes="*"),
                     lambda: R.reauthorize_operator(self.conn, "B", by="A", approval_ref=REF),
                     lambda: R.revoke_operator(self.conn, "B", by="A", reason="x"),
                     lambda: R.suspend_work(self.conn, "B", by="A", reason="x"),
                     lambda: R.resume_work(self.conn, "B", by="A"),
                     lambda: R.set_hold(self.conn, "beta", True, by="A", reason="x"),
                     lambda: R.reset_token(self.conn, "B", decision_ref="d", by="A")):
            with self.assertRaises(E.NotActive):
                call()

    def test_only_an_administrator_can_lift_a_hold_and_admin_rights_are_granted_only_by_an_administrator(self) -> None:
        self.member("B", scopes="beta")
        R.set_hold(self.conn, "beta", True, by="OPS1", reason="hold")
        with self.assertRaises(E.NotActive):
            R.set_hold(self.conn, "beta", False, by="B", reason="lift")
        R.set_hold(self.conn, "beta", False, by="OPS1", reason="lift")
        R.register_operator(self.conn, "NEWADMIN")
        R.approve_operator(self.conn, "NEWADMIN", by="OPS1", approval_ref=REF, admin=True)
        row = [o for o in R.list_operators(self.conn) if o["id"] == "NEWADMIN"][0]
        self.assertEqual(row["admin"], 1)
        R.register_operator(self.conn, "PLAIN")
        R.approve_operator(self.conn, "PLAIN", by="OPS1", approval_ref=REF)
        self.assertEqual([o for o in R.list_operators(self.conn) if o["id"] == "PLAIN"][0]["admin"], 0)

    def test_reauthorizing_returns_the_operator_to_pending_until_it_acknowledges_again(self) -> None:
        self.member("C")
        R.revoke_operator(self.conn, "C", by="OPS1", reason="left")
        R.reauthorize_operator(self.conn, "C", by="OPS1", approval_ref="again")
        row = [o for o in R.list_operators(self.conn) if o["id"] == "C"][0]
        self.assertEqual((row["trust"], row["status"]), ("APPROVED", "PENDING"))
        R.ack_operator(self.conn, "C", RULES_HASH)
        self.assertEqual([o for o in R.list_operators(self.conn) if o["id"] == "C"][0]["status"], "ACTIVE")

    def test_the_owner_shortcut_ends_for_good_once_the_registry_has_had_an_active_operator(self) -> None:
        self.conn.execute("UPDATE operators SET status='SUSPENDED'")
        R.register_operator(self.conn, "Z")
        with self.assertRaises(E.NotActive):
            R.approve_operator(self.conn, "Z", by="owner", approval_ref="again")


class TestScopeAndHoldGaps(AdminCase):
    def test_reassign_checks_scope_holds_and_the_right_to_receive(self) -> None:
        self.member("A", scopes="alpha")
        self.member("C", scopes="*")
        rid = R.create_request(self.conn, "beta work", origin="OPS1", project="beta")
        R.plan_request(self.conn, rid, "OPS1", [{"title": "p"}])
        sub = f"{rid}-S1"
        R.take_request  # noqa: B018
        R.reassign_task  # noqa: B018
        self.conn.execute("DROP TRIGGER IF EXISTS tasks_owner_change")
        self.conn.execute("UPDATE tasks SET owner='C', status='CLAIMED', claimed_at='t' WHERE id=?", (sub,))
        with self.assertRaises(E.NotActive):
            R.reassign_task(self.conn, sub, "A", decision_ref="d", by="OPS1")
        R.set_hold(self.conn, "beta", True, by="OPS1", reason="hold")
        self.member("D", scopes="beta")
        with self.assertRaises(E.NotActive):
            R.reassign_task(self.conn, sub, "D", decision_ref="d", by="OPS1")

    def test_agent_jobs_respect_the_project_hold_and_the_requesters_scope(self) -> None:
        self.member("A", scopes="alpha")
        R.register_operator(self.conn, "AG", kind="AGENT", skills="x")
        R.approve_operator(self.conn, "AG", by="OPS1", approval_ref=REF)
        R.ack_operator(self.conn, "AG", RULES_HASH)
        rid = R.create_request(self.conn, "beta", origin="OPS1", project="beta")
        with self.assertRaises(E.NotActive):
            R.request_agent(self.conn, "A", "job", needs="x", request_id=rid)
        R.set_hold(self.conn, "beta", True, by="OPS1", reason="hold")
        with self.assertRaises(E.NotActive):
            R.request_agent(self.conn, "OPS1", "job", needs="x", request_id=rid)

    def test_only_the_asker_attaches_tasks_to_a_request(self) -> None:
        self.member("B")
        rid = R.create_request(self.conn, "mine", origin="OPS1")
        with self.assertRaises(E.NotOwner):
            R.create_task(self.conn, "INJ-1", "sneaky", actor="B", request_id=rid)
        R.create_task(self.conn, "OK-1", "mine", actor="OPS1", request_id=rid)

    def test_handoff_adopt_and_plan_respect_scope_and_holds(self) -> None:
        self.member("A", scopes="alpha")
        rid = R.create_request(self.conn, "beta", origin="OPS1", project="beta")
        with self.assertRaises(E.NotActive):
            R.handoff_request(self.conn, rid, "OPS1", "A")
        R.set_hold(self.conn, "beta", True, by="OPS1", reason="hold")
        with self.assertRaises(E.NotActive):
            R.plan_request(self.conn, rid, "OPS1", [{"title": "x"}])

    def test_a_tick_by_a_scoped_member_skips_out_of_scope_requests_instead_of_failing(self) -> None:
        self.member("A", scopes="alpha")
        self.member("B", scopes="alpha")
        R.create_request(self.conn, "beta job", origin="OPS1", project="beta")
        mine = R.create_request(self.conn, "alpha job", origin="OPS1", project="alpha")
        made = R.dispatch_tick(self.conn, "A")
        self.assertEqual([m["task"] for m in made], [mine])


class TestRobustness(AdminCase):
    def test_a_naive_reset_time_is_accepted_as_utc_and_never_breaks_status_or_dispatch(self) -> None:
        self.member("A")
        R.set_usage(self.conn, "A-A", 10, resets_at="2026-12-31T00:00", by="OPS1")
        self.assertIn("A-A", R.get_usage(self.conn))
        R.status_report(self.conn)
        R.create_request(self.conn, "x", origin="OPS1")
        R.dispatch_tick(self.conn, "OPS1")
        self.conn.execute("UPDATE accounts SET resets_at='2026-12-31T00:00' WHERE id='A-A'")
        R.get_usage(self.conn)

    def test_a_bad_usage_history_is_refused_cleanly(self) -> None:
        self.member("A")
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "h.tsv")
            open(bad, "w").write("captured_at\tweek_pct\tweek_resets_at\nnot-a-number\t5\t123\n")
            with self.assertRaises((E.ValidationError, E.NotFound)):
                R.ingest_usage(self.conn, bad, "A-A", 123, by="OPS1")
            fifo = os.path.join(d, "fifo")
            os.mkfifo(fifo)
            with self.assertRaises((E.ValidationError, E.NotFound)):
                R.ingest_usage(self.conn, fifo, "A-A", 123, by="OPS1")

    def test_usage_and_tunables_need_an_authenticated_actor_when_tokens_are_required(self) -> None:
        R.set_setting(self.conn, "require_tokens", "1")
        with self.assertRaises(E.AuthFailed):
            R.set_usage(self.conn, "X", 10, by="")
        with self.assertRaises(E.AuthFailed):
            R.set_tunable(self.conn, "usage", 2, by="")

    def test_a_registry_from_a_newer_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "r.db")
            c = R.open_registry(db)
            c.execute("UPDATE schema_version SET version=99")
            c.close()
            with self.assertRaises(E.ValidationError):
                R.open_registry(db)


class TestHookCurrentOwnership(AdminCase):
    def test_the_hook_only_counts_assignments_the_operator_still_owns(self) -> None:
        self.member("A")
        self.member("C")
        R.set_setting(self.conn, "hook_keepalive", "off")
        R.create_task(self.conn, "T-1", "x", actor="OPS1")
        R.assign_task(self.conn, "T-1", "A", by="OPS1")
        self.assertTrue(R.hook_decision(self.conn, "A"))
        R.reassign_task(self.conn, "T-1", "C", decision_ref="d", by="OPS1")
        self.conn.execute("UPDATE hook_blocks SET blocks=0")
        self.assertIsNone(R.hook_decision(self.conn, "A"))


if __name__ == "__main__":
    unittest.main()
