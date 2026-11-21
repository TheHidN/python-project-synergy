import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

import clean_env  # noqa: F401
from helpers import RegistryCase

from project_synergy import errors as E
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TeamCase(RegistryCase):
    def team(self, names: list[str]) -> None:
        for n in names:
            self.active(n)

    def unread(self, op: str) -> list[str]:
        return [f"{n['kind']}:{n['subject']}" for n in R.list_notifications(self.conn, op)]


class TestProfilesAndRouting(TeamCase):
    def test_profiles_are_validated(self) -> None:
        self.team(["ALPHA"])
        R.set_profile(self.conn, "ALPHA", skills="security, review", model_pref="opus", max_tasks=2, availability="BUSY")
        row = [o for o in R.list_operators(self.conn) if o["id"] == "ALPHA"][0]
        self.assertEqual((row["skills"], row["model_pref"], row["max_tasks"], row["availability"]),
                         ("security,review", "opus", 2, "BUSY"))
        for bad in ({"availability": "ASLEEP"}, {"model_pref": "x y"}, {"max_tasks": 0}, {"skills": "a;b"}):
            with self.assertRaises(E.ValidationError):
                R.set_profile(self.conn, "ALPHA", **bad)

    def test_suggestions_rank_by_skill_availability_load_and_listening(self) -> None:
        self.team(["OPS1", "OPS2", "OPS3", "OPS4", "OPS5"])
        R.set_profile(self.conn, "OPS2", skills="security")
        R.set_profile(self.conn, "OPS3", skills="security,docs")
        R.set_profile(self.conn, "OPS4", skills="security", availability="OUT_OF_USAGE")
        R.set_profile(self.conn, "OPS5", skills="docs", max_tasks=1)
        R.set_listening(self.conn, "OPS3")
        rid = R.create_request(self.conn, "Review the security of the installer", origin="OPS1", needs="security")
        R.create_task(self.conn, "X-1", "busywork", actor="OPS1")
        R.assign_task(self.conn, "X-1", "OPS5", by="OPS1")
        ranking = R.suggest_assignees(self.conn, rid)
        ids = [r["id"] for r in ranking]
        self.assertEqual(ids[0], "OPS3", ranking)
        self.assertNotIn("OPS1", ids, "the operator who asked is not suggested")
        self.assertNotIn("OPS4", ids, "an operator out of usage is not suggested")
        self.assertNotIn("OPS5", ids, "an operator at capacity is not suggested")
        self.assertIn("security", ranking[0]["reason"])
        self.assertEqual(ids, [r["id"] for r in R.suggest_assignees(self.conn, rid)], "stable order")

    def test_no_suitable_operator_gives_an_empty_list_not_an_error(self) -> None:
        self.team(["OPS1"])
        rid = R.create_request(self.conn, "alone", origin="OPS1")
        self.assertEqual(R.suggest_assignees(self.conn, rid), [])


class TestMessages(TeamCase):
    def test_direct_and_broadcast_messages_are_durable_and_carry_the_subject(self) -> None:
        self.team(["OPS1", "OPS2", "OPS3"])
        R.send_message(self.conn, "OPS1", "OPS2", "Findings: the lock test is flaky", subject="REQ-0001")
        self.assertEqual(self.unread("OPS2"), ["message:REQ-0001"])
        self.assertEqual(self.unread("OPS3"), [])
        R.send_message(self.conn, "OPS2", None, "All: release freeze at noon")
        self.assertEqual(sorted(self.unread("OPS1")), ["message:general"])
        self.assertIn("message:general", self.unread("OPS3"))

    def test_messages_to_unknown_pending_or_empty_are_refused(self) -> None:
        self.team(["OPS1"])
        R.register_operator(self.conn, "OPS7")
        for target in ("NOBODY", "OPS7"):
            with self.assertRaises((E.NotFound, E.NotActive)):
                R.send_message(self.conn, "OPS1", target, "hi")
        with self.assertRaises(E.ValidationError):
            R.send_message(self.conn, "OPS1", "OPS1", "   ")


class TestTakeover(TeamCase):
    def setUp(self) -> None:
        super().setUp()
        self.team(["OPS1", "OPS2", "OPS3"])
        self.rid = R.create_request(self.conn, "Long job", origin="OPS1")
        R.take_request(self.conn, self.rid, "OPS2")

    def test_the_origin_can_hand_the_request_to_another_active_operator(self) -> None:
        R.handoff_request(self.conn, self.rid, "OPS1", "OPS3")
        self.assertEqual(R.get_request(self.conn, self.rid)["origin"], "OPS3")
        self.assertIn(f"handoff:{self.rid}", self.unread("OPS3"))
        with self.assertRaises(E.NotOwner):
            R.close_request(self.conn, self.rid, "OPS1", summary="mine?")
        R.close_request(self.conn, self.rid, "OPS3", summary="done by the new owner")

    def test_only_the_origin_can_hand_off(self) -> None:
        with self.assertRaises(E.NotOwner):
            R.handoff_request(self.conn, self.rid, "OPS2", "OPS3")

    def test_another_operator_can_adopt_only_when_the_origin_has_gone_quiet(self) -> None:
        with self.assertRaises(E.NotClaimable):
            R.adopt_request(self.conn, self.rid, "OPS3", stale_minutes=60)
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE operators SET last_seen=?, listening_at='' WHERE id='OPS1'", (old,))
        R.adopt_request(self.conn, self.rid, "OPS3", stale_minutes=60)
        self.assertEqual(R.get_request(self.conn, self.rid)["origin"], "OPS3")
        self.assertIn(f"adopted:{self.rid}", self.unread("OPS1"))
        events = [e["action"] for e in self.conn.execute("SELECT action FROM events WHERE subject=?", (self.rid,))]
        self.assertIn("request.adopt", events)
        self.assertIn("request.create", events, "the history is preserved")

    def test_a_task_in_progress_keeps_its_owner_through_a_takeover(self) -> None:
        R.handoff_request(self.conn, self.rid, "OPS1", "OPS3")
        root = [t for t in R.list_tasks(self.conn) if t["id"] == self.rid][0]
        self.assertEqual(root["owner"], "OPS2")


class TestLargeTeamSimulation(TeamCase):
    NAMES = [f"OPS{i}" for i in range(1, 13)] + ["ZED9", "LEAD-ONE"]

    def test_arbitrary_operator_names_work_and_a_request_reaches_everyone_once(self) -> None:
        self.team(self.NAMES)
        rid = R.create_request(self.conn, "Prepare the quarterly report", origin="LEAD-ONE")
        for name in self.NAMES:
            expected = [] if name == "LEAD-ONE" else [f"request:{rid}"]
            self.assertEqual(self.unread(name), expected, name)

    def test_one_winner_delegation_handoff_and_offline_backlog_in_a_large_team(self) -> None:
        self.team(self.NAMES)
        rid = R.create_request(self.conn, "Big job", origin="OPS1")
        self.assertTrue(R.take_request(self.conn, rid, "OPS7"))
        for other in self.NAMES:
            if other not in ("OPS7",):
                with self.assertRaises(E.AlreadyOwned):
                    R.take_request(self.conn, rid, other)
        for i, helper in enumerate(["OPS8", "OPS9", "OPS10"], start=1):
            R.create_task(self.conn, f"J-{i}", f"part {i}", actor="OPS7", request_id=rid)
            R.assign_task(self.conn, f"J-{i}", helper, by="OPS7")
            self.assertIn(f"assigned:J-{i}", self.unread(helper))
            R.set_task_status(self.conn, f"J-{i}", helper, "DONE", evidence=f"part {i} complete")
        updates = [n for n in R.list_notifications(self.conn, "OPS1") if n["kind"] == "task_update"]
        self.assertEqual(len(updates), 3)
        backlog = R.list_notifications(self.conn, "OPS12")
        self.assertEqual(len(backlog), 1, "an operator that was offline keeps its missed notification")
        text = R.request_report(self.conn, rid)
        for needle in ("J-1", "J-2", "J-3", "part 3 complete", "OPS7", "not listening"):
            self.assertIn(needle, text)

    def test_the_worker_limit_still_holds_for_a_large_team(self) -> None:
        self.team(self.NAMES)
        R.register_worker(self.conn, "W1", "OPS1")
        R.register_worker(self.conn, "W2", "OPS9")
        with self.assertRaises(E.WorkerLimit):
            R.register_worker(self.conn, "W3", "OPS12")


class TestRealProcessTeam(unittest.TestCase):
    """Real separate processes acting as five operators against one registry (simulated team, no Claude sessions)."""

    def test_five_operator_processes_are_woken_by_one_request_and_exactly_one_takes_it(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            (base / "cfg").mkdir()
            (base / "cfg" / "config.toml").write_text('[operators]\ninitial = ["OPS1", "OPS2", "OPS3", "OPS4", "OPS5"]\n')
            env = {"PATH": os.environ["PATH"], "HOME": d, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                   "SYNERGY_STATE_DIR": str(base / "state"), "SYNERGY_CONFIG_DIR": str(base / "cfg")}

            def run(*argv: str, op: str | None = None) -> subprocess.CompletedProcess[str]:
                e = dict(env)
                if op:
                    e["SYNERGY_OPERATOR"] = op
                return subprocess.run([sys.executable, "-m", "project_synergy", *argv], env=e, capture_output=True,
                                      text=True, check=False, timeout=60, cwd=str(ROOT))

            self.assertEqual(run("init", "--no-require-tokens").returncode, 0)
            conn = R.open_registry(str(base / "state" / "registry.db"))
            conn.execute("UPDATE operators SET status='ACTIVE'")
            conn.execute("UPDATE settings SET value='1' WHERE key='require_tokens'")
            conn.close()
            woke: dict[str, subprocess.CompletedProcess[str]] = {}

            def listen(op: str) -> None:
                woke[op] = run("inbox", "wait", "--timeout", "40", op=op)

            threads = [threading.Thread(target=listen, args=(f"OPS{i}",)) for i in range(2, 6)]
            for t in threads:
                t.start()
            time.sleep(2.5)
            self.assertEqual(run("request", "add", "Everyone please look", op="OPS1").returncode, 0)
            for t in threads:
                t.join(timeout=30)
                self.assertFalse(t.is_alive())
            for i in range(2, 6):
                self.assertEqual(woke[f"OPS{i}"].returncode, 0, woke[f"OPS{i}"].stderr)
                self.assertIn("REQ-0001", woke[f"OPS{i}"].stdout)
            results = []
            threads = [threading.Thread(target=lambda o=f"OPS{i}": results.append(run("request", "take", "REQ-0001", op=o).returncode))
                       for i in range(2, 6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(sorted(results), [0, 3, 3, 3], "exactly one operator takes the request")


class TestNoHardCodedOperators(unittest.TestCase):
    def test_the_source_does_not_name_specific_operators(self) -> None:
        import re

        offenders = []
        for path in (ROOT / "src" / "project_synergy").glob("*.py"):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                if re.search(r"\bOPS[0-9]\b", line) and "Example" not in line and "help=" not in line and "e.g." not in line:
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [])
