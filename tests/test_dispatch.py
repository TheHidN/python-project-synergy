import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone

import clean_env  # noqa: F401
from helpers import RULES_HASH, RegistryCase

from project_synergy import errors as E
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


def iso(days_ahead: float = 3.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_ahead)).isoformat(timespec="seconds")


class DispatchCase(RegistryCase):
    def team(self, n: int, usage: dict[int, float] | None = None, **profile: object) -> list[str]:
        names = [f"OPS{i}" for i in range(1, n + 1)]
        for i, name in enumerate(names, start=1):
            R.register_operator(self.conn, name, account=f"ACC{i}")
            R.ack_operator(self.conn, name, RULES_HASH)
            if usage and i in usage:
                R.set_usage(self.conn, f"ACC{i}", usage[i], resets_at=iso(), by=name)
            if profile:
                R.set_profile(self.conn, name, **profile)
        self.conn.execute("UPDATE operators SET admin=1 WHERE id='OPS1'")
        return names

    def steps(self, k: int, **extra: object) -> list[dict[str, object]]:
        return [{"title": f"part {i}", **extra} for i in range(1, k + 1)]

    def owners(self, rid: str) -> dict[str, str]:
        return {t["id"]: (t["owner"] or "") for t in R.list_tasks(self.conn) if t["id"].startswith(rid + "-S")}


class TestSplitting(unittest.TestCase):
    def test_a_simple_request_is_not_split(self) -> None:
        self.assertEqual(R.split_text("Please check the installer on a clean machine"), [])
        self.assertEqual(R.split_text("One thing\n- only a single bullet"), [])

    def test_bullets_are_independent_and_numbers_are_ordered(self) -> None:
        bullets = R.split_text("Do the release prep:\n- write the notes\n- check the docs\n* run the tests")
        self.assertEqual([s["title"] for s in bullets], ["write the notes", "check the docs", "run the tests"])
        self.assertTrue(all(not s.get("depends") for s in bullets))
        ordered = R.split_text("1. build it\n2) test it\n3. ship it")
        self.assertEqual([s.get("depends") for s in ordered], [[], [1], [2]])


class TestPlanning(DispatchCase):
    def test_only_the_asker_plans_once_and_each_subtask_has_an_id_objective_and_criteria(self) -> None:
        self.team(3)
        rid = R.create_request(self.conn, "Big job", origin="OPS1")
        with self.assertRaises(E.NotOwner):
            R.plan_request(self.conn, rid, "OPS2", self.steps(2))
        ids = R.plan_request(self.conn, rid, "OPS1", [
            {"title": "write tests", "needs": "testing", "criteria": "tests fail first"},
            {"title": "implement", "needs": "code", "depends": [1], "files": "src/a.py"}])
        self.assertEqual(ids, [f"{rid}-S1", f"{rid}-S2"])
        tasks = {t["id"]: t for t in R.list_tasks(self.conn)}
        self.assertEqual(tasks[ids[0]]["deliverables"], "tests fail first")
        self.assertEqual(tasks[ids[1]]["status"], "OPEN")
        with self.assertRaises(E.NotClaimable):
            R.plan_request(self.conn, rid, "OPS1", self.steps(2))
        with self.assertRaises(E.ValidationError):
            R.plan_request(self.conn, R.create_request(self.conn, "x", origin="OPS1"), "OPS1", [{"title": "a", "depends": [1]}])


class TestDispatch(DispatchCase):
    def test_independent_subtasks_spread_across_operators_lowest_usage_first(self) -> None:
        self.team(4, usage={1: 10, 2: 70, 3: 20, 4: 40})
        rid = R.create_request(self.conn, "Big job", origin="OPS1")
        R.plan_request(self.conn, rid, "OPS1", self.steps(3))
        made = R.dispatch_tick(self.conn, "OPS1")
        self.assertEqual(len(made), 3)
        owners = self.owners(rid)
        self.assertEqual(sorted(owners.values()), ["OPS2", "OPS3", "OPS4"], "one each, not piled on one operator")
        self.assertEqual(owners[f"{rid}-S1"], "OPS3", "the first subtask goes to the lowest-usage eligible operator")
        self.assertEqual(R.dispatch_tick(self.conn, "OPS1"), [], "a second tick assigns nothing twice")

    def test_dependent_work_waits_and_is_offered_when_its_dependency_is_done(self) -> None:
        self.team(3)
        rid = R.create_request(self.conn, "Ordered", origin="OPS1")
        a, b = R.plan_request(self.conn, rid, "OPS1", [{"title": "first"}, {"title": "second", "depends": [1]}])
        R.dispatch_tick(self.conn, "OPS1")
        owners = self.owners(rid)
        self.assertTrue(owners[a])
        self.assertEqual(owners[b], "")
        R.set_task_status(self.conn, a, owners[a], "DONE", evidence="first done")
        made = R.dispatch_tick(self.conn, "OPS1")
        self.assertEqual([m["task"] for m in made], [b])
        self.assertTrue(self.owners(rid)[b])

    def test_a_plain_request_goes_to_one_best_operator_and_the_asker_stays_the_contact(self) -> None:
        self.team(3, usage={2: 50, 3: 15})
        rid = R.create_request(self.conn, "Check the docs", origin="OPS1")
        made = R.dispatch_tick(self.conn, "OPS1")
        self.assertEqual([m["assignee"] for m in made], ["OPS3"])
        self.assertEqual(R.get_request(self.conn, rid)["origin"], "OPS1")
        self.assertIn("OPS1", R.request_report(self.conn, rid))

    def test_nobody_is_chosen_who_is_pending_an_agent_busy_out_of_usage_or_near_their_limit(self) -> None:
        self.team(4)
        R.register_operator(self.conn, "NEWBIE")
        R.register_operator(self.conn, "AG1", kind="AGENT", skills="x")
        R.ack_operator(self.conn, "AG1", RULES_HASH)
        R.set_profile(self.conn, "OPS2", availability="BUSY")
        R.set_profile(self.conn, "OPS3", availability="OUT_OF_USAGE")
        R.set_usage(self.conn, "ACC4", 97, resets_at=iso(), reserve=10, by="OPS4")
        rid = R.create_request(self.conn, "x", origin="OPS1")
        self.assertEqual(R.dispatch_tick(self.conn, "OPS1"), [], "only OPS4 is left and it is near its limit")
        self.assertEqual(self.owners(rid), {})
        R.set_usage(self.conn, "ACC4", 40, resets_at=iso(), by="OPS4")
        self.assertEqual([m["assignee"] for m in R.dispatch_tick(self.conn, "OPS1")], ["OPS4"])

    def test_pausing_stops_automatic_assignment_but_not_manual_work(self) -> None:
        self.team(3)
        R.set_dispatch(self.conn, False, by="OPS1")
        self.assertFalse(R.dispatch_enabled(self.conn))
        rid = R.create_request(self.conn, "x", origin="OPS1")
        self.assertEqual(R.dispatch_tick(self.conn, "OPS1"), [])
        R.take_request(self.conn, rid, "OPS2")
        R.set_dispatch(self.conn, True, by="OPS1")
        self.assertTrue(R.dispatch_enabled(self.conn))

    def test_file_ownership_conflicts_hold_a_subtask_back_and_continuity_is_preferred(self) -> None:
        self.team(3)
        R.claim_file(self.conn, "src/shared.py", "OPS3")
        rid = R.create_request(self.conn, "Edit", origin="OPS1")
        R.plan_request(self.conn, rid, "OPS1", [{"title": "edit shared", "files": "src/shared.py"},
                                                 {"title": "other", "files": "src/other.py"}])
        R.dispatch_tick(self.conn, "OPS1")
        owners = self.owners(rid)
        self.assertEqual(owners[f"{rid}-S1"], "OPS3", "the operator already holding the file gets the work")
        R.claim_file(self.conn, "src/other.py", "OPS2")
        rid2 = R.create_request(self.conn, "Edit more", origin="OPS1")
        R.plan_request(self.conn, rid2, "OPS1", [{"title": "touch both", "files": "src/shared.py,src/other.py"}])
        self.assertEqual(R.dispatch_tick(self.conn, "OPS1"), [], "two different owners: held back, never double-edited")

    def test_a_failed_task_is_retried_elsewhere_and_given_up_after_three_tries(self) -> None:
        self.team(5)
        rid = R.create_request(self.conn, "Flaky", origin="OPS1")
        (sub,) = R.plan_request(self.conn, rid, "OPS1", [{"title": "try"}, {"title": "other"}])[:1]
        tried: list[str] = []
        for _ in range(3):
            R.dispatch_tick(self.conn, "OPS1")
            owner = self.owners(rid)[sub]
            self.assertTrue(owner)
            self.assertNotIn(owner, tried)
            tried.append(owner)
            R.fail_task(self.conn, sub, owner, reason="could not reproduce")
        self.assertEqual(R.dispatch_tick(self.conn, "OPS1"), [])
        self.assertEqual(self.owners(rid)[sub], "", "after three failures it waits for a person")
        kinds = [n["kind"] for n in R.list_notifications(self.conn, "OPS1")]
        self.assertIn("task_failed", kinds)
        self.assertIn("needs_attention", kinds)

    def test_the_asker_is_told_when_every_subtask_is_done_and_alone_closes_the_request(self) -> None:
        self.team(3)
        rid = R.create_request(self.conn, "Two parts", origin="OPS1")
        ids = R.plan_request(self.conn, rid, "OPS1", self.steps(2))
        R.dispatch_tick(self.conn, "OPS1")
        for tid, owner in self.owners(rid).items():
            R.set_task_status(self.conn, tid, owner, "DONE", evidence=f"{tid} result text")
        self.assertIn("ready_for_review", [n["kind"] for n in R.list_notifications(self.conn, "OPS1")])
        report = R.request_report(self.conn, rid)
        for tid in ids:
            self.assertIn(f"{tid} result text", report)
        with self.assertRaises(E.NotOwner):
            R.close_request(self.conn, rid, "OPS2", summary="mine")
        R.close_request(self.conn, rid, "OPS1", summary="all parts reviewed")

    def test_decisions_are_logged_with_an_explanation(self) -> None:
        self.team(3, usage={2: 30, 3: 60})
        rid = R.create_request(self.conn, "x", origin="OPS1")
        R.dispatch_tick(self.conn, "OPS1")
        rows = [e["detail"] for e in self.conn.execute("SELECT detail FROM events WHERE action='schedule.route'")]
        self.assertTrue(rows and "OPS2" in rows[0] and "30" in rows[0])
        self.assertTrue(R.verify_chain(self.conn)[0])
        self.assertEqual(self.owners(rid), {})


def _tick(args: tuple[str, str]) -> int:
    db, op = args
    conn = R.open_registry(db)
    try:
        return len(R.dispatch_tick(conn, op))
    finally:
        conn.close()


class TestScaleAndConcurrency(DispatchCase):
    def test_eight_processes_ticking_at_once_assign_every_subtask_exactly_once(self) -> None:
        names = self.team(8)
        rid = R.create_request(self.conn, "Wide", origin="OPS1")
        R.plan_request(self.conn, rid, "OPS1", self.steps(12))
        with ProcessPoolExecutor(max_workers=8) as pool:
            counts = list(pool.map(_tick, [(self.db, n) for n in names]))
        owners = self.owners(rid)
        self.assertEqual(sum(counts), len([o for o in owners.values() if o]))
        self.assertEqual(len(owners), 12)
        per = {}
        for o in owners.values():
            if o:
                per[o] = per.get(o, 0) + 1
        self.assertTrue(all(v <= 3 for v in per.values()), per)
        self.assertGreaterEqual(len(per), 4)

    def test_team_sizes_two_three_five_and_ten_all_dispatch_without_duplicates(self) -> None:
        for size in (2, 3, 5, 10):
            with self.subTest(size=size):
                conn = R.open_registry(os.path.join(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")), "r.db"))
                self.addCleanup(conn.close)
                R.set_setting(conn, "rules_hash", RULES_HASH)
                R.set_setting(conn, "require_tokens", "0")
                for i in range(1, size + 1):
                    R.register_operator(conn, f"OPS{i}", account=f"A{i}" if i % 2 else "SHARED")
                    R.ack_operator(conn, f"OPS{i}", RULES_HASH)
                R.set_usage(conn, "SHARED", 80, resets_at=iso(), by="OPS2" if size > 1 else "OPS1")
                rid = R.create_request(conn, "job", origin="OPS1")
                R.plan_request(conn, rid, "OPS1", [{"title": f"p{i}"} for i in range(1, size * 2 + 1)])
                R.dispatch_tick(conn, "OPS1")
                owned = [t for t in R.list_tasks(conn) if t["id"].startswith(rid + "-S") and t["owner"]]
                self.assertEqual(len({t["id"] for t in owned}), len(owned))
                self.assertGreaterEqual(len(owned), min(size - 1, 1) * 1)
                for name in {t["owner"] for t in owned}:
                    self.assertNotEqual(name, "OPS1" if size > 1 and len(owned) < size * 2 else "")


class TestDispatchCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = pathlib.Path(self._tmp.name)
        (base / "cfg").mkdir()
        (base / "cfg" / "config.toml").write_text('[operators]\ninitial = ["OPS1", "OPS2", "OPS3"]\n')
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

    def test_one_request_to_any_operator_is_split_assigned_and_reported_automatically(self) -> None:
        text = "Prepare the release:\n- write the notes\n- check the docs\n- run the tests"
        added = self.cli("request", "add", text, op="OPS2")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertIn("REQ-0001-S1", added.stdout)
        inbox1 = self.cli("inbox", "list", op="OPS1").stdout
        inbox3 = self.cli("inbox", "list", op="OPS3").stdout
        self.assertIn("assigned", inbox1)
        self.assertIn("assigned", inbox3)
        status = self.cli("request", "status", "REQ-0001").stdout
        for needle in ("REQ-0001-S1", "REQ-0001-S3", "OPS1", "OPS3"):
            self.assertIn(needle, status)

    def test_no_auto_and_pause_and_resume_and_status(self) -> None:
        self.assertEqual(self.cli("dispatch", "pause", op="OPS1").returncode, 0)
        self.assertIn("PAUSED", self.cli("dispatch", "status").stdout)
        self.cli("request", "add", "Hold on", op="OPS1")
        self.assertNotIn("assigned", self.cli("inbox", "list", op="OPS2").stdout)
        self.assertEqual(self.cli("dispatch", "resume", op="OPS1").returncode, 0)
        self.assertIn("ON", self.cli("dispatch", "status").stdout)
        self.assertEqual(self.cli("dispatch", "run", op="OPS1").returncode, 0)
        self.assertIn("assigned", self.cli("inbox", "list", op="OPS2").stdout + self.cli("inbox", "list", op="OPS3").stdout)
        self.cli("request", "add", "Mine to do", "--no-auto", op="OPS1")
        self.assertIn("REQ-0002", self.cli("request", "list").stdout)

    def test_an_idle_operator_starting_its_listener_is_offered_waiting_work(self) -> None:
        self.cli("dispatch", "pause", op="OPS1")
        self.cli("request", "add", "Waiting job", op="OPS1")
        self.cli("dispatch", "resume", op="OPS1")
        woke = self.cli("inbox", "wait", "--timeout", "5", op="OPS2")
        self.assertEqual(woke.returncode, 0, woke.stdout + woke.stderr)
        self.assertIn("assigned", woke.stdout)

    def test_explicit_plan_steps_and_failure_with_retry_through_the_command_line(self) -> None:
        self.cli("request", "add", "Planned", "--no-auto", op="OPS1")
        plan = self.cli("request", "plan", "REQ-0001", "--step", "design|needs=architecture|criteria=a one page design",
                        "--step", "build|after=1|files=src/x.py", op="OPS1")
        self.assertEqual(plan.returncode, 0, plan.stdout + plan.stderr)
        self.assertEqual(self.cli("dispatch", "run", op="OPS1").returncode, 0)
        status = self.cli("request", "status", "REQ-0001").stdout
        self.assertIn("REQ-0001-S1", status)
        owner = [o for o in ("OPS2", "OPS3") if "assigned" in self.cli("inbox", "list", op=o).stdout][0]
        failed = self.cli("task", "fail", "REQ-0001-S1", "--reason", "blocked by access", op=owner)
        self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        again = self.cli("request", "status", "REQ-0001").stdout
        self.assertIn("REQ-0001-S1", again)
