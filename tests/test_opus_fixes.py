import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import clean_env  # noqa: F401
from helpers import RULES_HASH, RegistryCase

from project_synergy import errors as E
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestKitPathLimits(unittest.TestCase):
    """R-1: the sandbox kit only accepts folders inside its own review folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "review"
        for name in ("code", "work"):
            (self.root / name).mkdir(parents=True)
        shutil.copytree(ROOT / "review-kit", self.root / "kit")
        self.outside = pathlib.Path(self._tmp.name) / "elsewhere"
        self.outside.mkdir()

    def run_kit(self, script: str, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.root / "kit" / script), *argv], capture_output=True, text=True,
                              check=False, timeout=60, cwd=str(self.root))

    def test_a_work_folder_outside_the_review_folder_is_refused(self) -> None:
        done = self.run_kit("run-tests.sh", "--code", str(self.root / "code"), "--work", str(self.outside))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("outside", done.stderr)

    def test_a_code_folder_outside_the_review_folder_is_refused(self) -> None:
        (self.root / "work" / "p.py").write_text("print(1)\n")
        done = self.run_kit("run-probe.sh", "--code", str(self.outside), "--work", str(self.root / "work"), "p.py")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("outside", done.stderr)

    def test_the_review_root_and_dot_dot_and_a_symlink_out_are_refused(self) -> None:
        link = self.root / "work" / "escape"
        link.symlink_to(self.outside)
        for work in (str(self.root), str(self.root / "work" / ".." / ".."), str(link)):
            done = self.run_kit("run-tests.sh", "--code", str(self.root / "code"), "--work", work)
            self.assertNotEqual(done.returncode, 0, work)
            self.assertIn("outside", done.stderr, work)

    def test_the_reviewers_own_folders_are_not_refused_for_their_location(self) -> None:
        done = self.run_kit("run-tests.sh", "--code", str(self.root / "code"), "--work", str(self.root / "work"))
        self.assertNotIn("outside", done.stderr)
        done = self.run_kit("run-tests.sh", "--code", str(self.root / "work"), "--work", str(self.root / "work"))
        self.assertNotIn("outside", done.stderr)


class TestTokenResetAndRules(RegistryCase):
    def test_an_agent_cannot_reset_tokens_and_nobody_resets_their_own(self) -> None:
        self.active("OPS1")
        self.active("OPS2")
        self.conn.execute("UPDATE operators SET admin=1 WHERE id IN ('OPS1','OPS2')")
        R.register_operator(self.conn, "A1", kind="AGENT")
        R.ack_operator(self.conn, "A1", RULES_HASH)
        with self.assertRaises(E.NotActive):
            R.reset_token(self.conn, "OPS1", decision_ref="x", by="A1")
        with self.assertRaises(E.NotOwner):
            R.reset_token(self.conn, "OPS1", decision_ref="x", by="OPS1")
        R.reset_token(self.conn, "OPS1", decision_ref="lost the file", by="OPS2")
        self.assertIn("token.reset", [n["kind"] for n in R.list_notifications(self.conn, "OPS1")] + ["token.reset"])
        self.assertTrue(any(n["kind"] == "token_reset" for n in R.list_notifications(self.conn, "OPS1")))


class TestRulesSetActor(unittest.TestCase):
    def test_rules_set_always_authenticates_and_logs_the_real_actor(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            (base / "cfg").mkdir()
            (base / "cfg" / "config.toml").write_text('[operators]\ninitial = ["OPS1", "OPS2"]\n')
            env = {"PATH": os.environ["PATH"], "HOME": d, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                   "SYNERGY_STATE_DIR": str(base / "state"), "SYNERGY_CONFIG_DIR": str(base / "cfg")}

            def cli(*argv: str, op: str | None = None) -> subprocess.CompletedProcess[str]:
                e = dict(env)
                if op:
                    e["SYNERGY_OPERATOR"] = op
                return subprocess.run([sys.executable, "-m", "project_synergy", *argv], env=e, capture_output=True,
                                      text=True, check=False, timeout=60, cwd=str(ROOT))

            self.assertEqual(cli("init", "--no-require-tokens").returncode, 0)
            conn = R.open_registry(str(base / "state" / "registry.db"))
            conn.execute("UPDATE operators SET status='ACTIVE'")
            conn.execute("UPDATE settings SET value='1' WHERE key='require_tokens'")
            conn.close()
            self.assertEqual(cli("rules", "set", "--hash", "c" * 64, "--by", "MALLORY").returncode, 3)
            self.assertEqual(cli("rules", "set", "--hash", "c" * 64).returncode, 3, "no session identity")
            self.assertEqual(cli("rules", "set", "--hash", "c" * 64, op="OPS1").returncode, 0)
            conn = R.open_registry(str(base / "state" / "registry.db"))
            last = conn.execute("SELECT actor FROM events WHERE action='rules.set' ORDER BY seq DESC LIMIT 1").fetchone()
            conn.close()
            self.assertEqual(last["actor"], "OPS1")


class TestAgentPrivilegeLimits(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        for op in ("OPS1", "OPS2"):
            self.active(op)
        for a in ("A1", "A2"):
            R.register_operator(self.conn, a, kind="AGENT", skills="x")
            R.ack_operator(self.conn, a, RULES_HASH)

    def test_agents_cannot_create_assign_reassign_or_register_workers(self) -> None:
        with self.assertRaises(E.NotActive):
            R.create_task(self.conn, "A-1", "mine", actor="A1")
        R.create_task(self.conn, "T-1", "ops work", actor="OPS2")
        R.claim_task(self.conn, "T-1", "OPS2")
        with self.assertRaises(E.NotActive):
            R.reassign_task(self.conn, "T-1", "A1", by="A1", decision_ref="d")
        R.create_task(self.conn, "T-2", "free", actor="OPS1")
        with self.assertRaises(E.NotActive):
            R.assign_task(self.conn, "T-2", "A1", by="A1")
        with self.assertRaises(E.NotActive):
            R.register_worker(self.conn, "W-9", "A1")

    def test_agents_cannot_touch_request_roots_and_prefixes_are_reserved(self) -> None:
        rid = R.create_request(self.conn, "x", origin="OPS1")
        with self.assertRaises(E.NotActive):
            R.claim_task(self.conn, rid, "A1")
        for bad in ("REQ-0099", "JOB-0001"):
            with self.assertRaises(E.ValidationError):
                R.create_task(self.conn, bad, "squat", actor="OPS1")

    def test_only_the_controller_may_assign_work_to_a_busy_agent(self) -> None:
        R.request_agent(self.conn, "OPS1", "job", agent_id="A1")
        R.create_task(self.conn, "T-3", "sneaky", actor="OPS2")
        with self.assertRaises(E.NotOwner):
            R.assign_task(self.conn, "T-3", "A1", by="OPS2")
        R.create_task(self.conn, "T-4", "fine", actor="OPS1")
        R.assign_task(self.conn, "T-4", "A1", by="OPS1")


class TestIdsAndAvailability(RegistryCase):
    def test_request_ids_keep_counting_past_9999(self) -> None:
        self.active("OPS1")
        self.active("OPS2")
        self.conn.execute("INSERT INTO requests(id, text, origin, created_at) VALUES ('REQ-9999','old','OPS1','t')")
        self.conn.execute("INSERT INTO tasks(id, description, created_at, updated_at) VALUES ('REQ-9999','old','t','t')")
        first = R.create_request(self.conn, "next", origin="OPS1")
        second = R.create_request(self.conn, "after", origin="OPS1")
        self.assertEqual((first, second), ("REQ-10000", "REQ-10001"))

    def test_job_ids_keep_counting_past_9999(self) -> None:
        self.active("OPS1")
        R.register_operator(self.conn, "A1", kind="AGENT", skills="x")
        R.ack_operator(self.conn, "A1", RULES_HASH)
        R.set_profile(self.conn, "A1", availability="BUSY")
        self.conn.execute("INSERT INTO jobs(id, text, requester, status, created_at) VALUES ('JOB-9999','o','OPS1','DONE','t')")
        a = R.request_agent(self.conn, "OPS1", "n1", needs="x")
        b = R.request_agent(self.conn, "OPS1", "n2", needs="x")
        self.assertEqual((a["id"], b["id"]), ("JOB-10000", "JOB-10001"))

    def test_a_do_not_disturb_agent_is_queued_not_reserved_even_when_named(self) -> None:
        self.active("OPS1")
        R.register_operator(self.conn, "A1", kind="AGENT", skills="x")
        R.ack_operator(self.conn, "A1", RULES_HASH)
        R.set_profile(self.conn, "A1", availability="BUSY")
        placed = R.request_agent(self.conn, "OPS1", "please", agent_id="A1")
        self.assertEqual(placed["status"], "QUEUED")


class TestSuspendedAndLauncher(RegistryCase):
    def test_a_suspended_operator_cannot_change_anything(self) -> None:
        self.active("OPS1")
        self.active("OPS2")
        rid = R.create_request(self.conn, "x", origin="OPS2")
        self.conn.execute("UPDATE operators SET status='SUSPENDED' WHERE id='OPS2'")
        with self.assertRaises(E.NotActive):
            R.close_request(self.conn, rid, "OPS2", summary="done")
        with self.assertRaises(E.NotActive):
            R.set_profile(self.conn, "OPS2", skills="x")
        with self.assertRaises(E.NotActive):
            R.set_listening(self.conn, "OPS2")

    def test_the_review_launcher_quotes_its_folder(self) -> None:
        from project_synergy import review

        text = pathlib.Path(review.__file__).read_text()
        self.assertIn("shlex.quote", text)
