import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta

import clean_env  # noqa: F401
from helpers import RegistryCase
from project_synergy import agents as A
from project_synergy import errors as E
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent


class PoolCase(RegistryCase):
    def agent(self, name: str, skills: str = "", model: str = "any", attach: bool = True, **kw: str) -> None:
        R.register_operator(self.conn, name, role="specialist", kind="AGENT", skills=skills, model_pref=model, **kw)
        if attach:
            R.ack_operator(self.conn, name, "a" * 64)

    def unread(self, op: str) -> list[str]:
        return [f"{n['kind']}:{n['subject']}" for n in R.list_notifications(self.conn, op)]

    def job(self, jid: str) -> dict:
        return dict(R.get_job(self.conn, jid))

    def agent_row(self, name: str) -> dict:
        return dict([o for o in R.list_operators(self.conn) if o["id"] == name][0])


class TestRegistration(PoolCase):
    def test_agents_are_registered_pending_and_are_not_operators(self) -> None:
        self.active("OPS1")
        self.agent("SEC", skills="security,review", attach=False)
        row = self.agent_row("SEC")
        self.assertEqual((row["kind"], row["status"], row["skills"]), ("AGENT", "PENDING", "security,review"))
        with self.assertRaises(E.DuplicateOperator):
            R.register_operator(self.conn, "sec", kind="AGENT")
        R.create_request(self.conn, "hello team", origin="OPS1")
        self.assertEqual(self.unread("SEC"), [], "agents do not receive operator requests")

    def test_an_agent_cannot_create_requests_or_take_them(self) -> None:
        self.active("OPS1")
        self.agent("SEC")
        with self.assertRaises(E.NotActive):
            R.create_request(self.conn, "x", origin="SEC")
        rid = R.create_request(self.conn, "y", origin="OPS1")
        with self.assertRaises(E.NotActive):
            R.take_request(self.conn, rid, "SEC")

    def test_an_unattached_agent_is_never_reserved(self) -> None:
        self.active("OPS1")
        self.agent("SEC", skills="security", attach=False)
        with self.assertRaises(E.NotActive):
            R.request_agent(self.conn, "OPS1", "check it", agent_id="SEC")
        placed = R.request_agent(self.conn, "OPS1", "check it", needs="security")
        self.assertEqual((placed["status"], placed["agent_id"]), ("QUEUED", ""))


class TestReservationAndQueue(PoolCase):
    def setUp(self) -> None:
        super().setUp()
        for op in ("OPS1", "OPS2", "OPS3"):
            self.active(op)
        self.agent("SEC", skills="security,review")
        self.agent("WRITER", skills="docs,writing")

    def test_a_free_matching_agent_is_reserved_atomically_and_notified(self) -> None:
        placed = R.request_agent(self.conn, "OPS1", "review the installer", needs="security")
        self.assertEqual((placed["status"], placed["agent_id"]), ("RESERVED", "SEC"))
        row = self.agent_row("SEC")
        self.assertEqual((row["current_job"], row["controller"]), (placed["id"], "OPS1"))
        self.assertIn(f"job:{placed['id']}", self.unread("SEC"))

    def test_a_busy_agent_keeps_its_assignment_and_the_new_request_waits(self) -> None:
        first = R.request_agent(self.conn, "OPS1", "first", needs="security")
        second = R.request_agent(self.conn, "OPS2", "second", needs="security")
        self.assertEqual(second["status"], "QUEUED")
        self.assertEqual(self.agent_row("SEC")["current_job"], first["id"])
        self.assertEqual(self.agent_row("SEC")["controller"], "OPS1")
        explicit = R.request_agent(self.conn, "OPS3", "third", agent_id="SEC")
        self.assertEqual((explicit["status"], explicit["agent_id"]), ("QUEUED", "SEC"))
        self.assertEqual(self.agent_row("SEC")["current_job"], first["id"])

    def test_finishing_promotes_the_highest_priority_oldest_matching_job(self) -> None:
        first = R.request_agent(self.conn, "OPS1", "first", needs="security")
        low = R.request_agent(self.conn, "OPS2", "low", needs="security", priority=3)
        urgent = R.request_agent(self.conn, "OPS3", "urgent", needs="security", priority=1)
        normal = R.request_agent(self.conn, "OPS2", "normal", needs="security", priority=2)
        order = []
        current = first["id"]
        for _ in range(4):
            R.agent_next(self.conn, "SEC")
            R.finish_job(self.conn, current, "SEC", result=f"{current} done")
            nxt = [j for j in (low, urgent, normal) if self.job(j["id"])["status"] == "RESERVED"]
            if nxt:
                current = nxt[0]["id"]
                order.append(current)
        self.assertEqual(order, [urgent["id"], normal["id"], low["id"]])

    def test_skills_and_model_decide_who_is_eligible(self) -> None:
        self.agent("DEEP", skills="security", model="opus")
        placed = R.request_agent(self.conn, "OPS1", "hard one", needs="security", model="opus")
        self.assertEqual(placed["agent_id"], "DEEP")
        sonnet_job = R.request_agent(self.conn, "OPS2", "routine", needs="security", model="sonnet")
        self.assertEqual(sonnet_job["agent_id"], "SEC", "an opus-only agent does not take a sonnet job")
        flexible = R.request_agent(self.conn, "OPS3", "docs on opus", needs="docs", model="opus")
        self.assertEqual(flexible["agent_id"], "WRITER", "an agent with no model preference accepts any model")
        none = R.request_agent(self.conn, "OPS3", "legal", needs="legal")
        self.assertEqual(none["status"], "QUEUED")

    def test_agents_out_of_usage_or_do_not_disturb_are_skipped(self) -> None:
        R.set_profile(self.conn, "SEC", availability="OUT_OF_USAGE")
        self.assertEqual(R.request_agent(self.conn, "OPS1", "x", needs="security")["status"], "QUEUED")
        R.set_profile(self.conn, "WRITER", availability="BUSY")
        self.assertEqual(R.request_agent(self.conn, "OPS1", "y", needs="docs")["status"], "QUEUED")

    def test_a_queued_job_is_picked_up_when_a_matching_agent_becomes_free(self) -> None:
        queued = R.request_agent(self.conn, "OPS1", "wait for me", needs="translation")
        self.assertEqual(queued["status"], "QUEUED")
        self.agent("LINGUIST", skills="translation")
        got = R.agent_next(self.conn, "LINGUIST")
        self.assertEqual(got["id"], queued["id"])
        self.assertEqual(self.job(queued["id"])["status"], "RUNNING")

    def test_nothing_is_launched_to_serve_requests(self) -> None:
        before = len(R.list_workers(self.conn))
        for i in range(6):
            R.request_agent(self.conn, f"OPS{1 + i % 3}", f"job {i}", needs="security")
        self.assertEqual(len(R.list_workers(self.conn)), before)


class TestCompletionAndControl(PoolCase):
    def setUp(self) -> None:
        super().setUp()
        for op in ("OPS1", "OPS2"):
            self.active(op)
        self.agent("SEC", skills="security")
        self.placed = R.request_agent(self.conn, "OPS1", "review it", needs="security")

    def test_only_the_assigned_agent_finishes_and_a_result_is_required(self) -> None:
        self.agent("OTHER", skills="security")
        with self.assertRaises(E.NotOwner):
            R.finish_job(self.conn, self.placed["id"], "OTHER", result="mine")
        with self.assertRaises(E.MissingEvidence):
            R.finish_job(self.conn, self.placed["id"], "SEC", result="  ")
        R.finish_job(self.conn, self.placed["id"], "SEC", result="no critical issues; 2 low")
        done = self.job(self.placed["id"])
        self.assertEqual((done["status"], done["result"]), ("DONE", "no critical issues; 2 low"))
        self.assertIn(f"job_done:{self.placed['id']}", self.unread("OPS1"))
        self.assertEqual(self.agent_row("SEC")["current_job"], "")
        self.assertEqual(self.agent_row("SEC")["controller"], "")

    def test_the_result_stays_available_to_every_operator_later(self) -> None:
        R.finish_job(self.conn, self.placed["id"], "SEC", result="report in review.md")
        mine = R.list_jobs(self.conn, requester="OPS1")
        everyone = R.list_jobs(self.conn)
        self.assertEqual(mine[0]["result"], "report in review.md")
        self.assertEqual(len(everyone), 1)
        again = R.request_agent(self.conn, "OPS2", "another review", needs="security")
        self.assertEqual(again["agent_id"], "SEC", "the same agent serves a different operator afterwards")

    def test_only_one_controller_directs_a_running_agent(self) -> None:
        with self.assertRaises(E.NotOwner):
            R.send_message(self.conn, "OPS2", "SEC", "also do this instead")
        R.send_message(self.conn, "OPS1", "SEC", "focus on the installer")
        self.assertIn("message:general", self.unread("SEC"))
        R.finish_job(self.conn, self.placed["id"], "SEC", result="done")
        R.send_message(self.conn, "OPS2", "SEC", "you are free now")

    def test_failing_cancelling_and_handing_over_control(self) -> None:
        second = R.request_agent(self.conn, "OPS2", "second", needs="security")
        with self.assertRaises(E.NotOwner):
            R.cancel_job(self.conn, second["id"], "OPS1")
        R.cancel_job(self.conn, second["id"], "OPS2")
        self.assertEqual(self.job(second["id"])["status"], "CANCELLED")
        R.handoff_job(self.conn, self.placed["id"], "OPS1", "OPS2")
        self.assertEqual(self.agent_row("SEC")["controller"], "OPS2")
        R.fail_job(self.conn, self.placed["id"], "SEC", reason="needs the source tree")
        self.assertEqual(self.job(self.placed["id"])["status"], "FAILED")
        self.assertIn(f"job_failed:{self.placed['id']}", self.unread("OPS2"))

    def test_a_running_job_cannot_be_cancelled_from_outside(self) -> None:
        R.agent_next(self.conn, "SEC")
        with self.assertRaises(E.NotClaimable):
            R.cancel_job(self.conn, self.placed["id"], "OPS1")

    def test_an_agent_that_went_quiet_is_recovered_without_losing_the_job_or_its_history(self) -> None:
        R.agent_next(self.conn, "SEC")
        with self.assertRaises(E.NotClaimable):
            R.requeue_job(self.conn, self.placed["id"], "OPS1", stale_minutes=60)
        old = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE operators SET last_seen=?, listening_at='' WHERE id='SEC'", (old,))
        R.requeue_job(self.conn, self.placed["id"], "OPS1", stale_minutes=60)
        self.assertEqual(self.job(self.placed["id"])["status"], "QUEUED")
        self.assertEqual(self.agent_row("SEC")["current_job"], "")
        self.agent("SEC2", skills="security")
        self.assertEqual(R.agent_next(self.conn, "SEC2")["id"], self.placed["id"])
        with self.assertRaises(E.NotOwner):
            R.finish_job(self.conn, self.placed["id"], "SEC", result="late result from the quiet one")
        actions = [e["action"] for e in self.conn.execute("SELECT action FROM events WHERE subject=?", (self.placed["id"],))]
        self.assertIn("job.requeue", actions)
        self.assertIn("job.create", actions)

    def test_the_board_shows_availability_assignments_and_the_queue(self) -> None:
        R.request_agent(self.conn, "OPS2", "waits", needs="security")
        text = R.pool_report(self.conn)
        for needle in ("SEC", "BUSY", self.placed["id"], "OPS1", "queued", "waits"):
            self.assertIn(needle, text)


def _request(args: tuple[str, str]) -> str:
    db, op = args
    conn = R.open_registry(db)
    try:
        return R.request_agent(conn, op, f"job from {op}", agent_id="SEC")["status"]
    finally:
        conn.close()


class TestConcurrency(PoolCase):
    def test_twenty_operators_requesting_one_agent_reserve_it_exactly_once(self) -> None:
        names = [f"OPS{i}" for i in range(1, 21)]
        for n in names:
            self.active(n)
        self.agent("SEC", skills="security")
        with ProcessPoolExecutor(max_workers=20) as pool:
            out = list(pool.map(_request, [(self.db, n) for n in names]))
        self.assertEqual(out.count("RESERVED"), 1)
        self.assertEqual(out.count("QUEUED"), 19)
        busy = [o for o in R.list_operators(self.conn) if o["kind"] == "AGENT" and o["current_job"]]
        self.assertEqual(len(busy), 1)

    def test_a_large_team_and_pool_serve_every_job_exactly_once(self) -> None:
        ops = [f"OPS{i}" for i in range(1, 11)]
        agents = [("SEC", "security"), ("SEC2", "security"), ("DOC", "docs"), ("DEV", "code"), ("QA", "testing")]
        for o in ops:
            self.active(o)
        for name, skill in agents:
            self.agent(name, skills=skill)
        jobs = []
        for i in range(30):
            skill = ["security", "docs", "code", "testing"][i % 4]
            jobs.append(R.request_agent(self.conn, ops[i % 10], f"work {i}", needs=skill, priority=1 + i % 3)["id"])
        served: dict[str, int] = {}
        for _ in range(40):
            progressed = False
            for name, _skill in agents:
                nxt = R.agent_next(self.conn, name)
                if nxt:
                    R.finish_job(self.conn, nxt["id"], name, result=f"{name} did {nxt['id']}")
                    served[nxt["id"]] = served.get(nxt["id"], 0) + 1
                    progressed = True
            if not progressed:
                break
        self.assertEqual(sorted(served), sorted(jobs))
        self.assertTrue(all(v == 1 for v in served.values()))
        for o in ops:
            mine = [n for n in R.list_notifications(self.conn, o) if n["kind"] == "job_done"]
            self.assertEqual(len(mine), 3, o)
        self.assertEqual([a for a in R.list_operators(self.conn) if a["kind"] == "AGENT" and a["current_job"]], [])


class TestDiscovery(unittest.TestCase):
    PANES = ["CODING:PROJX-SEC|claude|9000|/work/a", "CODING:PROJX-WRITER|claude|120|/work/b", "CODING:shell|bash|10|/home/x",
             "OPS:ops|claude|5|/home/x", "CODING:PROJX-SEC|claude|9000|/work/a"]

    def test_discovery_proposes_claude_windows_only_without_touching_them(self) -> None:
        found = A.parse_windows(self.PANES, protected=("OPS",))
        names = sorted(f["id"] for f in found)
        self.assertEqual(names, ["PROJX-SEC", "PROJX-WRITER"])
        self.assertTrue(all(f["window"].startswith("CODING:") for f in found))

    def test_ids_are_sanitised_and_already_registered_windows_are_skipped(self) -> None:
        found = A.parse_windows(["CODING:my window.1|claude|1|/x"], protected=())
        self.assertEqual(found[0]["id"], "MY-WINDOW-1")
        again = A.parse_windows(self.PANES, protected=("OPS",), known_windows={"CODING:PROJX-SEC"})
        self.assertEqual([f["id"] for f in again], ["PROJX-WRITER"])

    def test_the_agents_module_only_reads_and_never_sends_anything(self) -> None:
        text = (ROOT / "src" / "project_synergy" / "agents.py").read_text()
        for forbidden in ("send-keys", "kill-", "capture-pane", "new-window", "respawn", "run-shell"):
            self.assertNotIn(forbidden, text)


class TestCli(unittest.TestCase):
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

    def attach(self, name: str) -> None:
        import hashlib

        packet = (self.state / "packets" / f"{name}.md").read_bytes()
        done = self.cli("operator", "ack", name, hashlib.sha256(packet).hexdigest(), op=name)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_register_attach_request_work_and_result_flow_through_the_command_line(self) -> None:
        reg = self.cli("agent", "register", "SEC", "--approval-ref", "t", "--role", "Security Expert", "--skills", "security,review",
                       "--window", "CODING:PROJX-SEC", op="OPS1")
        self.assertEqual(reg.returncode, 0, reg.stderr)
        token = (self.state / "tokens" / "SEC.token").read_text().strip()
        self.assertNotIn(token, reg.stdout)
        self.assertIn("Nothing was sent", reg.stdout)
        board = self.cli("agent", "list", op="OPS1").stdout
        self.assertIn("SEC", board)
        self.assertIn("not attached", board)
        refused = self.cli("agent", "request", "review it", "--agent", "SEC", op="OPS1")
        self.assertEqual(refused.returncode, 3)
        self.attach("SEC")
        placed = self.cli("agent", "request", "Review the installer", "--needs", "security", op="OPS2")
        self.assertEqual(placed.returncode, 0, placed.stderr)
        self.assertIn("JOB-0001", placed.stdout)
        self.assertIn("job", self.cli("inbox", "list", op="SEC").stdout)
        nxt = self.cli("agent", "next", op="SEC")
        self.assertEqual(nxt.returncode, 0, nxt.stderr)
        self.assertIn("Review the installer", nxt.stdout)
        done = self.cli("agent", "done", "JOB-0001", "--result", "2 low findings", op="SEC")
        self.assertEqual(done.returncode, 0, done.stderr)
        mine = self.cli("inbox", "list", op="OPS2").stdout
        self.assertIn("2 low findings", mine)
        self.assertIn("2 low findings", self.cli("agent", "result", "JOB-0001", op="OPS1").stdout)

    def test_agents_cannot_run_owner_level_commands(self) -> None:
        self.cli("agent", "register", "SEC", "--approval-ref", "t", op="OPS1")
        self.attach("SEC")
        refused = self.cli("agent", "register", "EVIL", "--approval-ref", "t", op="SEC")
        self.assertEqual(refused.returncode, 3)
        self.assertEqual(self.cli("request", "add", "x", op="SEC").returncode, 3)

    def test_discover_lists_without_registering_unless_asked(self) -> None:
        out = self.cli("agent", "discover", op="OPS1")
        self.assertEqual(out.returncode, 0, out.stderr)
        before = self.cli("agent", "list", op="OPS1").stdout
        self.assertNotIn("discovered", before)

    def test_real_agent_processes_are_woken_by_jobs_and_serve_three_operators(self) -> None:
        self.cli("operator", "register", "OPS3", op="OPS1")
        for name, skill in (("SEC", "security"), ("DOC", "docs")):
            self.assertEqual(self.cli("agent", "register", name, "--approval-ref", "t", "--skills", skill, op="OPS1").returncode, 0)
            self.attach(name)
        woke: dict[str, subprocess.CompletedProcess[str]] = {}

        def wait(name: str) -> None:
            woke[name] = self.cli("inbox", "wait", "--timeout", "40", op=name)

        threads = [threading.Thread(target=wait, args=(n,)) for n in ("SEC", "DOC")]
        for t in threads:
            t.start()
        time.sleep(2.5)
        self.assertEqual(self.cli("agent", "request", "review", "--needs", "security", op="OPS1").returncode, 0)
        self.assertEqual(self.cli("agent", "request", "write", "--needs", "docs", op="OPS2").returncode, 0)
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive())
        self.assertIn("JOB-0001", woke["SEC"].stdout)
        self.assertIn("JOB-0002", woke["DOC"].stdout)
        for name, jid in (("SEC", "JOB-0001"), ("DOC", "JOB-0002")):
            self.assertEqual(self.cli("agent", "next", op=name).returncode, 0)
            self.assertEqual(self.cli("agent", "done", jid, "--result", f"{name} finished", op=name).returncode, 0)
        self.assertIn("SEC finished", self.cli("inbox", "list", op="OPS1").stdout)
        self.assertIn("DOC finished", self.cli("inbox", "list", op="OPS2").stdout)
