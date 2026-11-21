import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import clean_env  # noqa: F401
from helpers import RULES_HASH, RegistryCase

from project_synergy import errors as E
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent
REF = "owner-decision-2026-11-21.md sha256 abc123"


class TrustCase(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.conn.execute("UPDATE operators SET trust='APPROVED', approved_by='migration', approval_ref='installation', "
                          "approved_at='t', admin=1 WHERE id='OPS1'")
        R.set_setting(self.conn, "require_approval", "1")

    def join(self, name: str, scopes: str = "*", **kw: object) -> None:
        R.register_operator(self.conn, name, account=f"A-{name}")
        R.approve_operator(self.conn, name, by="OPS1", approval_ref=REF, scopes=scopes, **kw)
        R.ack_operator(self.conn, name, RULES_HASH)


class TestMembership(TrustCase):
    def test_an_operator_cannot_become_active_until_the_owner_approval_is_recorded(self) -> None:
        R.register_operator(self.conn, "OPS2")
        with self.assertRaises(E.NotActive):
            R.ack_operator(self.conn, "OPS2", RULES_HASH)
        R.approve_operator(self.conn, "OPS2", by="OPS1", approval_ref=REF, scopes="synergy,docs")
        R.ack_operator(self.conn, "OPS2", RULES_HASH)
        row = [o for o in R.list_operators(self.conn) if o["id"] == "OPS2"][0]
        self.assertEqual((row["status"], row["trust"], row["approved_by"], row["scopes"]), ("ACTIVE", "APPROVED", "OPS1", "synergy,docs"))
        self.assertEqual(row["approval_ref"], REF)
        self.assertTrue(row["approved_at"])

    def test_approval_needs_a_reference_an_active_operator_and_never_self(self) -> None:
        R.register_operator(self.conn, "OPS2")
        R.register_operator(self.conn, "OPS3")
        with self.assertRaises(E.ValidationError):
            R.approve_operator(self.conn, "OPS2", by="OPS1", approval_ref="  ")
        with self.assertRaises(E.NotActive):
            R.approve_operator(self.conn, "OPS3", by="OPS2", approval_ref=REF)
        with self.assertRaises(E.NotOwner):
            R.approve_operator(self.conn, "OPS1", by="OPS1", approval_ref=REF)
        with self.assertRaises(E.ValidationError):
            R.approve_operator(self.conn, "OPS2", by="OPS1", approval_ref=REF, scopes="bad scope!")

    def test_the_owner_shortcut_works_only_before_anyone_is_active(self) -> None:
        with self.assertRaises(E.NotActive):
            R.approve_operator(self.conn, "OPS1", by="owner", approval_ref="again")

    def test_membership_survives_closing_and_reopening_the_registry(self) -> None:
        self.join("OPS2", scopes="synergy")
        self.conn.close()
        again = R.open_registry(self.db)
        self.addCleanup(again.close)
        row = [o for o in R.list_operators(again) if o["id"] == "OPS2"][0]
        self.assertEqual((row["status"], row["trust"]), ("ACTIVE", "APPROVED"))

    def test_an_existing_registry_migrates_with_its_active_operators_approved(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "registry.db")
            raw = sqlite3.connect(db, isolation_level=None)
            raw.row_factory = sqlite3.Row
            raw.execute("BEGIN IMMEDIATE")
            for stmt in R._split(R._SCHEMA_V1):
                raw.execute(stmt)
            for v in range(2, 7):
                for stmt in R._MIGRATIONS[v]:
                    raw.execute(stmt)
            raw.execute("INSERT INTO schema_version VALUES (6)")
            raw.execute("INSERT INTO settings VALUES ('max_workers','2')")
            raw.execute("INSERT INTO operators(id, status, registered_at, last_seen) VALUES ('OLD','ACTIVE','t','t')")
            raw.execute("INSERT INTO operators(id, status, registered_at, last_seen) VALUES ('NEW','PENDING','t','t')")
            R._log(raw, "system", "registry.init", "registry", "schema 6")
            raw.execute("COMMIT")
            raw.close()
            conn = R.open_registry(db)
            self.addCleanup(conn.close)
            rows = {o["id"]: o for o in R.list_operators(conn)}
            self.assertEqual(rows["OLD"]["trust"], "APPROVED")
            self.assertIn("before the trust layer", rows["OLD"]["approval_ref"])
            self.assertEqual(rows["NEW"]["trust"], "")
            self.assertEqual(R.get_setting(conn, "require_approval"), "1")


class TestScopeEnforcement(TrustCase):
    def test_requests_and_assignments_respect_project_scopes(self) -> None:
        self.join("OPS2", scopes="synergy")
        self.join("OPS3", scopes="delta")
        self.join("OPS4", scopes="*")
        rid = R.create_request(self.conn, "docs", origin="OPS1", project="synergy")
        chosen = {m["assignee"] for m in R.dispatch_tick(self.conn, "OPS1")}
        self.assertEqual(len(chosen), 1)
        self.assertNotIn("OPS3", chosen, "an operator without the synergy scope never gets synergy work")
        with self.assertRaises(E.NotActive):
            R.take_request(self.conn, R.create_request(self.conn, "more", origin="OPS1", project="synergy"), "OPS3")
        self.assertTrue(rid)

    def test_a_request_outside_the_askers_own_scope_is_refused(self) -> None:
        self.join("OPS2", scopes="synergy")
        with self.assertRaises(E.NotActive):
            R.create_request(self.conn, "other project", origin="OPS2", project="delta")

    def test_a_held_project_gets_no_new_requests_or_assignments(self) -> None:
        self.join("OPS2")
        rid = R.create_request(self.conn, "work", origin="OPS1", project="delta")
        R.set_hold(self.conn, "delta", True, by="OPS1", reason="owner hold")
        self.assertEqual(R.dispatch_tick(self.conn, "OPS1"), [])
        with self.assertRaises(E.NotActive):
            R.take_request(self.conn, rid, "OPS2")
        with self.assertRaises(E.NotActive):
            R.create_request(self.conn, "new", origin="OPS1", project="delta")
        R.set_hold(self.conn, "delta", False, by="OPS1", reason="lifted by the owner")
        self.assertEqual(len(R.dispatch_tick(self.conn, "OPS1")), 1)

    def test_only_members_allowed_to_assign_may_hand_work_to_others(self) -> None:
        self.join("OPS2")
        self.join("OPS3", can_assign=False)
        R.create_task(self.conn, "T-1", "x", actor="OPS1")
        with self.assertRaises(E.NotActive):
            R.assign_task(self.conn, "T-1", "OPS2", by="OPS3")
        R.assign_task(self.conn, "T-1", "OPS2", by="OPS1")

    def test_pending_unapproved_and_agent_receivers_are_never_given_work(self) -> None:
        self.join("OPS2")
        R.register_operator(self.conn, "NEWBIE")
        rid = R.create_request(self.conn, "x", origin="OPS1")
        made = R.dispatch_tick(self.conn, "OPS1")
        self.assertEqual([m["assignee"] for m in made], ["OPS2"])
        self.assertTrue(rid)


class TestSuspendRevokeReauthorize(TrustCase):
    def test_suspending_stops_new_work_but_not_what_is_running(self) -> None:
        self.join("OPS2")
        R.create_task(self.conn, "T-1", "running", actor="OPS1")
        R.assign_task(self.conn, "T-1", "OPS2", by="OPS1")
        R.suspend_work(self.conn, "OPS2", by="OPS1", reason="busy week")
        R.create_task(self.conn, "T-2", "new", actor="OPS1")
        with self.assertRaises(E.NotActive):
            R.assign_task(self.conn, "T-2", "OPS2", by="OPS1")
        rid = R.create_request(self.conn, "x", origin="OPS1")
        self.assertEqual(R.dispatch_tick(self.conn, "OPS1"), [])
        R.set_task_status(self.conn, "T-1", "OPS2", "DONE", evidence="finished what it had")
        R.resume_work(self.conn, "OPS2", by="OPS1")
        self.assertEqual([m["assignee"] for m in R.dispatch_tick(self.conn, "OPS1")], ["OPS2"])
        self.assertTrue(rid)

    def test_revoking_blocks_access_and_new_work_without_deleting_anything(self) -> None:
        self.join("OPS2")
        R.create_task(self.conn, "T-1", "owned", actor="OPS1")
        R.assign_task(self.conn, "T-1", "OPS2", by="OPS1")
        R.revoke_operator(self.conn, "OPS2", by="OPS1", reason="left the team")
        row = [o for o in R.list_operators(self.conn) if o["id"] == "OPS2"][0]
        self.assertEqual((row["trust"], row["status"]), ("REVOKED", "SUSPENDED"))
        with self.assertRaises(E.NotActive):
            R.create_request(self.conn, "x", origin="OPS2")
        with self.assertRaises(E.NotActive):
            R.set_task_status(self.conn, "T-1", "OPS2", "DONE", evidence="late")
        task = [t for t in R.list_tasks(self.conn) if t["id"] == "T-1"][0]
        self.assertEqual((task["owner"], task["status"]), ("OPS2", "CLAIMED"), "work and ownership are kept")
        with self.assertRaises(E.NotActive):
            R.ack_operator(self.conn, "OPS2", RULES_HASH)
        self.assertEqual(R.dispatch_tick(self.conn, "OPS1"), [])

    def test_reauthorizing_restores_membership_with_a_new_reference_and_scope(self) -> None:
        self.join("OPS2", scopes="synergy")
        R.revoke_operator(self.conn, "OPS2", by="OPS1", reason="paused")
        with self.assertRaises(E.ValidationError):
            R.reauthorize_operator(self.conn, "OPS2", by="OPS1", approval_ref="")
        R.reauthorize_operator(self.conn, "OPS2", by="OPS1", approval_ref="approval record 2", scopes="synergy,docs")
        row = [o for o in R.list_operators(self.conn) if o["id"] == "OPS2"][0]
        self.assertEqual((row["trust"], row["status"], row["scopes"], row["approval_ref"]), ("APPROVED", "PENDING", "synergy,docs", "approval record 2"))

    def test_nobody_revokes_themselves_and_the_events_are_logged(self) -> None:
        self.join("OPS2")
        with self.assertRaises(E.NotOwner):
            R.revoke_operator(self.conn, "OPS1", by="OPS1", reason="x")
        R.suspend_work(self.conn, "OPS2", by="OPS1", reason="r")
        actions = [e["action"] for e in self.conn.execute("SELECT action FROM events")]
        for needed in ("team.approve", "team.suspend"):
            self.assertIn(needed, actions)
        self.assertTrue(R.verify_chain(self.conn)[0])


class TestTeamViews(TrustCase):
    def test_team_report_and_my_authorization_are_readable(self) -> None:
        self.join("OPS2", scopes="synergy", can_assign=False)
        report = R.team_report(self.conn)
        for needle in ("OPS1", "OPS2", "APPROVED", "synergy", REF, "receive", "assign"):
            self.assertIn(needle, report)
        me = R.team_me(self.conn, "OPS2")
        self.assertIn("APPROVED", me)
        self.assertIn("synergy", me)
        self.assertIn("routine", me)
        self.assertIn("still need the owner", me)


class TestLargeTeam(TrustCase):
    def test_twelve_members_with_mixed_scopes_only_get_work_in_their_scope(self) -> None:
        for i in range(2, 14):
            self.join(f"OPS{i}", scopes="alpha" if i % 2 else "beta")
        rid = R.create_request(self.conn, "alpha job", origin="OPS1", project="alpha")
        R.plan_request(self.conn, rid, "OPS1", [{"title": f"p{n}"} for n in range(1, 9)])
        R.dispatch_tick(self.conn, "OPS1")
        owners = {t["owner"] for t in R.list_tasks(self.conn) if t["id"].startswith(rid + "-S") and t["owner"]}
        self.assertTrue(owners)
        for owner in owners:
            self.assertEqual(int(owner[3:]) % 2, 1, owner)


class TestTrustCli(unittest.TestCase):
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

    def test_one_command_joins_a_new_operator_and_views_and_controls_work(self) -> None:
        onboard = self.cli("onboard", "OPS3", "--approval-ref", REF, "--scopes", "synergy", op="OPS1")
        self.assertEqual(onboard.returncode, 0, onboard.stdout + onboard.stderr)
        self.assertIn("APPROVED", self.cli("team", "show").stdout)
        self.assertIn("synergy", self.cli("team", "show").stdout)
        self.assertEqual(self.cli("team", "suspend", "OPS3", "--reason", "week off", op="OPS1").returncode, 0)
        self.assertIn("no new work", self.cli("team", "show").stdout.lower())
        self.assertEqual(self.cli("team", "resume", "OPS3", op="OPS1").returncode, 0)
        self.assertEqual(self.cli("team", "revoke", "OPS3", "--reason", "left", op="OPS1").returncode, 0)
        self.assertIn("REVOKED", self.cli("team", "show").stdout)
        self.assertEqual(self.cli("team", "reauthorize", "OPS3", "--approval-ref", "again", op="OPS1").returncode, 0)
        self.assertIn("APPROVED", self.cli("team", "show").stdout)
        self.assertEqual(self.cli("team", "revoke", "OPS3", "--reason", "x", op="OPS3").returncode, 3)
        self.assertIn("OPS2", self.cli("team", "me", op="OPS2").stdout)

    def test_onboard_without_an_approval_reference_is_refused_once_the_team_exists(self) -> None:
        refused = self.cli("onboard", "OPS3", op="OPS1")
        self.assertEqual(refused.returncode, 3)
        self.assertIn("approval", refused.stderr)

    def test_a_scoped_request_via_the_command_line(self) -> None:
        self.cli("onboard", "OPS3", "--approval-ref", REF, "--scopes", "delta", op="OPS1")
        added = self.cli("request", "add", "Synergy thing", "--project", "synergy", op="OPS1")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertNotIn("OPS3", added.stdout)
        self.assertEqual(self.cli("team", "hold", "synergy", "--reason", "pause", op="OPS1").returncode, 0)
        self.assertEqual(self.cli("request", "add", "More", "--project", "synergy", op="OPS1").returncode, 3)
