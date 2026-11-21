import json
import os
import pathlib
import sqlite3
import stat
import tempfile
import unittest

from helpers import RULES_HASH, RegistryCase
from project_synergy import cli
from project_synergy import config as C
from project_synergy import errors as E
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestAckRules(RegistryCase):
    def test_ack_refused_while_no_rules_hash_is_set(self) -> None:
        self.conn.execute("DELETE FROM settings WHERE key='rules_hash'")
        R.register_operator(self.conn, "OPS1")
        with self.assertRaises(E.BadAck):
            R.ack_operator(self.conn, "OPS1", "anything")
        self.assertEqual(R.list_operators(self.conn)[0]["status"], "PENDING")

    def test_suspended_operator_cannot_ack_itself_back(self) -> None:
        self.active("OPS1")
        self.conn.execute("UPDATE operators SET status='SUSPENDED' WHERE id='OPS1'")
        with self.assertRaises(E.NotActive):
            R.ack_operator(self.conn, "OPS1", RULES_HASH)


class TestTaskRules(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.active("OPS2")
        R.create_task(self.conn, "T1", "a", actor="OPS1")

    def test_reassign_needs_an_owned_live_task_and_an_active_decider(self) -> None:
        with self.assertRaises(E.NotClaimable):
            R.reassign_task(self.conn, "T1", "OPS2", decision_ref="d1", by="OPS1")
        R.claim_task(self.conn, "T1", "OPS1")
        with self.assertRaises(E.NotFound):
            R.reassign_task(self.conn, "T1", "OPS2", decision_ref="d1", by="GHOST")
        R.register_operator(self.conn, "OPS9")
        with self.assertRaises(E.NotActive):
            R.reassign_task(self.conn, "T1", "OPS2", decision_ref="d1", by="OPS9")
        R.set_task_status(self.conn, "T1", "OPS1", "DONE", evidence="e")
        with self.assertRaises(E.NotClaimable):
            R.reassign_task(self.conn, "T1", "OPS2", decision_ref="d1", by="OPS1")
        self.assertEqual(R.list_tasks(self.conn)[0]["owner"], "OPS1")

    def test_done_and_cancelled_are_terminal(self) -> None:
        R.claim_task(self.conn, "T1", "OPS1")
        R.set_task_status(self.conn, "T1", "OPS1", "DONE", evidence="e")
        with self.assertRaises(E.NotClaimable):
            R.set_task_status(self.conn, "T1", "OPS1", "IN_PROGRESS")
        with self.assertRaises(E.NotClaimable):
            R.release_task(self.conn, "T1", "OPS1")
        row = R.list_tasks(self.conn)[0]
        self.assertEqual((row["status"], row["owner"], row["completion_evidence"]), ("DONE", "OPS1", "e"))

    def test_suspended_owner_cannot_change_status_or_release(self) -> None:
        R.claim_task(self.conn, "T1", "OPS1")
        self.conn.execute("UPDATE operators SET status='SUSPENDED' WHERE id='OPS1'")
        with self.assertRaises(E.NotActive):
            R.set_task_status(self.conn, "T1", "OPS1", "IN_PROGRESS")
        with self.assertRaises(E.NotActive):
            R.release_task(self.conn, "T1", "OPS1")


class TestWorkerRules(RegistryCase):
    def test_finished_is_terminal_so_the_limit_cannot_be_dodged(self) -> None:
        self.active("OPS1")
        R.register_worker(self.conn, "W1", "OPS1")
        R.set_worker_state(self.conn, "W1", "FINISHED", evidence="deliverable checked")
        with self.assertRaises(E.ValidationError):
            R.set_worker_state(self.conn, "W1", "WORKING")
        self.assertEqual(R.list_workers(self.conn)[0]["state"], "FINISHED")


class TestWorkerFinishNeedsEvidence(RegistryCase):
    def test_finished_needs_evidence_and_it_is_recorded(self) -> None:
        self.active("OPS1")
        R.register_worker(self.conn, "W1", "OPS1")
        for blank in ("", "   "):
            with self.assertRaises(E.MissingEvidence):
                R.set_worker_state(self.conn, "W1", "FINISHED", evidence=blank)
        self.assertEqual(R.list_workers(self.conn)[0]["state"], "RUNNING")
        R.set_worker_state(self.conn, "W1", "FINISHED", evidence="REVIEW.md read and checked")
        last = self.conn.execute("SELECT detail FROM events WHERE action='worker.state' ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertIn("REVIEW.md read and checked", last["detail"])

    def test_other_states_need_no_evidence(self) -> None:
        self.active("OPS1")
        R.register_worker(self.conn, "W1", "OPS1")
        R.set_worker_state(self.conn, "W1", "IDLE")


class TestEventLog(RegistryCase):
    def test_insert_or_replace_cannot_rewrite_an_event(self) -> None:
        self.active("OPS1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "INSERT OR REPLACE INTO events(seq, ts, actor, action, subject, detail, prev_hash, hash) "
                "VALUES (1,'t','a','b','c','d','e','f')")
        self.assertTrue(R.verify_chain(self.conn)[0])

    def test_verify_reports_the_head_hash_for_an_outside_copy(self) -> None:
        ok, msg = R.verify_chain(self.conn)
        head = self.conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0]
        self.assertTrue(ok)
        self.assertIn(head[:12], msg)


class TestPermissions(unittest.TestCase):
    def test_new_state_folder_and_files_are_private_even_with_a_loose_umask(self) -> None:
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "state")
            conn = R.open_registry(os.path.join(folder, "registry.db"))
            R.register_operator(conn, "OPS1")
            conn.close()
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
            for name in os.listdir(folder):
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, name)).st_mode) & 0o077, 0, name)


class TestConfigNames(unittest.TestCase):
    def test_strict_names(self) -> None:
        for bad in ("ok\n", "-x", ".x", "a.b", "a:b"):
            with self.assertRaises(E.ValidationError):
                C.load({"tmux": {"socket": bad}})
        self.assertEqual(C.load({"tmux": {"socket": "ok-name_1"}}).tmux_socket, "ok-name_1")


class TestCliRobustness(unittest.TestCase):
    def test_unwritable_state_dir_gives_a_message_not_a_traceback(self) -> None:
        os.environ["SYNERGY_STATE_DIR"] = "/proc/synergy-no-such/state"
        self.addCleanup(os.environ.pop, "SYNERGY_STATE_DIR", None)
        self.assertEqual(cli.main(["init"]), 3)


class TestWorkerSettingsTemplate(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads((ROOT / "templates" / "worker-settings.json").read_text())["permissions"]

    def test_no_allow_rule_runs_arbitrary_code(self) -> None:
        for rule in self.data.get("allow", []):
            for word in ("unittest", "python", "sh ", "bash", "ls"):
                self.assertNotIn(word, rule)

    def test_ask_rules_cover_the_known_destructive_forms(self) -> None:
        ask = " | ".join(self.data["ask"])
        for needle in ("tmux kill-window", "tmux kill-session", "tmux kill-server", "kill *", "pkill", "killall",
                       "rm *", "git reset --hard", "git clean", "git branch -D", "git worktree remove",
                       "git push --force", "sh -c", "bash -c", "find * -delete", "respawn-pane",
                       "systemctl --user stop"):
            self.assertIn(needle, ask, needle)
