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


def iso(hours_ago: float = 0.0, days_ahead: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago) + timedelta(days=days_ahead)
    return moment.isoformat(timespec="seconds")


class BalanceCase(RegistryCase):
    def op(self, name: str, account: str = "", skills: str = "", percent: float | None = None, **kw: object) -> None:
        R.register_operator(self.conn, name, account=account, skills=skills)
        R.ack_operator(self.conn, name, RULES_HASH)
        if percent is not None:
            R.set_usage(self.conn, account, percent, resets_at=iso(days_ahead=4), source="manual", by=name)
        if kw:
            R.set_profile(self.conn, name, **kw)

    def request(self, origin: str, needs: str = "") -> str:
        return R.create_request(self.conn, "work item", origin=origin, needs=needs)

    def owner(self, rid: str) -> str:
        return [t for t in R.list_tasks(self.conn) if t["id"] == rid][0]["owner"] or ""


class TestUsageTracking(BalanceCase):
    def test_usage_is_validated_and_shows_its_source_and_age(self) -> None:
        self.op("OPS1", "ACC1")
        for bad in (-1, 101, "x"):
            with self.assertRaises(E.ValidationError):
                R.set_usage(self.conn, "ACC1", bad, resets_at=iso(days_ahead=3))
        with self.assertRaises(E.ValidationError):
            R.set_usage(self.conn, "bad name!", 10)
        R.set_usage(self.conn, "ACC1", 42.5, resets_at=iso(days_ahead=3), source="manual", by="OPS1")
        view = R.get_usage(self.conn)["ACC1"]
        self.assertEqual((view["percent"], view["state"], view["source"]), (42.5, "fresh", "manual"))
        self.assertEqual(view["left"], 57.5)

    def test_missing_stale_and_expired_figures_are_flagged_not_trusted(self) -> None:
        self.op("OPS1", "NEW")
        self.assertEqual(R.get_usage(self.conn)["NEW"]["state"], "unknown")
        R.set_usage(self.conn, "NEW", 10, resets_at=iso(days_ahead=3))
        self.conn.execute("UPDATE accounts SET verified_at=? WHERE id='NEW'", (iso(hours_ago=10),))
        self.assertEqual(R.get_usage(self.conn)["NEW"]["state"], "stale")
        self.conn.execute("UPDATE accounts SET verified_at=?, resets_at=? WHERE id='NEW'", (iso(), iso(hours_ago=1)))
        self.assertEqual(R.get_usage(self.conn)["NEW"]["state"], "reset", "the week has rolled over: the old figure is not used")

    def test_operators_sharing_an_account_are_one_pool(self) -> None:
        self.op("OPS1", "SHARED", percent=50)
        self.op("OPS2", "SHARED")
        self.op("OPS3", "OTHER", percent=20)
        accounts = R.get_usage(self.conn)
        self.assertEqual(sorted(a for a in accounts), ["OTHER", "SHARED"])
        self.assertEqual(sorted(accounts["SHARED"]["operators"]), ["OPS1", "OPS2"])
        self.assertEqual(R.get_usage(self.conn)["SHARED"]["percent"], 50)

    def test_ingest_reads_the_local_usage_history_for_the_matching_week_only(self) -> None:
        self.op("OPS1", "ACC1")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.tsv")
            rows = ["captured_at\tsession_pct\tsession_resets_at\tweek_pct\tweek_resets_at",
                    f"{int(datetime.now(timezone.utc).timestamp()) - 60}\t\t\t7\t111",
                    f"{int(datetime.now(timezone.utc).timestamp()) - 30}\t\t\t9\t222",
                    f"{int(datetime.now(timezone.utc).timestamp()) - 20}\t\t\t11\t111"]
            open(path, "w").write("\n".join(rows) + "\n")
            got = R.ingest_usage(self.conn, path, "ACC1", reset_epoch=111, by="OPS1")
            self.assertEqual(got, 11.0)
            with self.assertRaises(E.NotFound):
                R.ingest_usage(self.conn, path, "ACC1", reset_epoch=999, by="OPS1")
        self.assertEqual(R.get_usage(self.conn)["ACC1"]["source"], "file")


class TestRouting(BalanceCase):
    def test_the_lowest_utilisation_eligible_operator_gets_new_work_and_says_why(self) -> None:
        self.op("OPS1", "A", percent=10)
        self.op("OPS2", "B", percent=70)
        self.op("OPS3", "C", percent=20)
        self.op("OPS4", "D", percent=45)
        rid = self.request("OPS1")
        chosen = R.route_request(self.conn, rid, by="OPS1")
        self.assertEqual(chosen["assignee"], "OPS3", "OPS1 asked, so the lowest of the others")
        self.assertEqual(self.owner(rid), "OPS3")
        self.assertIn("20", chosen["explanation"])
        self.assertIn("assigned:" + rid, [f"{n['kind']}:{n['subject']}" for n in R.list_notifications(self.conn, "OPS3")])
        events = [e["detail"] for e in self.conn.execute("SELECT detail FROM events WHERE action='schedule.route'")]
        self.assertTrue(events and "OPS3" in events[0])

    def test_skills_outweigh_a_small_usage_gap_but_not_a_huge_one(self) -> None:
        self.op("OPS1", "A", percent=5)
        self.op("OPS2", "B", percent=40, skills="security")
        self.op("OPS3", "C", percent=39)
        rid = self.request("OPS1", needs="security")
        self.assertEqual(R.route_request(self.conn, rid, by="OPS1")["assignee"], "OPS2")

    def test_accounts_near_their_limit_are_avoided_when_others_have_capacity(self) -> None:
        self.op("OPS1", "A", percent=10)
        self.op("OPS2", "B", percent=96)
        R.set_usage(self.conn, "B", 96, resets_at=iso(days_ahead=2), reserve=10, by="OPS2")
        self.op("OPS3", "C", percent=88)
        rid = self.request("OPS1")
        self.assertEqual(R.route_request(self.conn, rid, by="OPS1")["assignee"], "OPS3")
        only = self.request("OPS3")
        self.conn.execute("UPDATE operators SET availability='BUSY' WHERE id IN ('OPS1','OPS3')")
        self.assertEqual(R.route_request(self.conn, only, by="OPS3")["assignee"], "OPS2", "chosen only because nobody else can")

    def test_stale_or_missing_figures_do_not_beat_a_fresh_moderate_one(self) -> None:
        self.op("OPS1", "A", percent=5)
        self.op("OPS2", "B", percent=10)
        self.conn.execute("UPDATE accounts SET verified_at=? WHERE id='B'", (iso(hours_ago=20),))
        self.op("OPS3", "C", percent=40)
        self.op("OPS4", "D")
        rid = self.request("OPS1")
        chosen = R.route_request(self.conn, rid, by="OPS1")
        self.assertEqual(chosen["assignee"], "OPS3")
        self.assertIn("stale", R.explain_candidates(self.conn, rid))

    def test_unavailable_busy_out_of_usage_and_quiet_operators_are_skipped(self) -> None:
        self.op("OPS1", "A", percent=50)
        self.op("OPS2", "B", percent=1, availability="OUT_OF_USAGE")
        self.op("OPS3", "C", percent=2, availability="BUSY")
        self.op("OPS4", "D", percent=3)
        self.conn.execute("UPDATE operators SET last_seen=? WHERE id='OPS4'", (iso(hours_ago=30),))
        self.op("OPS5", "E", percent=60)
        rid = self.request("OPS1")
        self.assertEqual(R.route_request(self.conn, rid, by="OPS1")["assignee"], "OPS5")

    def test_an_owned_request_is_never_moved(self) -> None:
        self.op("OPS1", "A", percent=50)
        self.op("OPS2", "B", percent=60)
        self.op("OPS3", "C", percent=1)
        rid = self.request("OPS1")
        R.take_request(self.conn, rid, "OPS2")
        with self.assertRaises(E.NotClaimable):
            R.route_request(self.conn, rid, by="OPS1")
        self.assertEqual(self.owner(rid), "OPS2")

    def test_no_eligible_operator_leaves_the_request_open_for_anyone_to_take(self) -> None:
        self.op("OPS1", "A", percent=10)
        rid = self.request("OPS1")
        self.assertIsNone(R.route_request(self.conn, rid, by="OPS1"))
        self.assertEqual(self.owner(rid), "")

    def test_equal_usage_rotates_fairly_with_no_starvation(self) -> None:
        names = [f"OPS{i}" for i in range(1, 7)]
        for n in names:
            self.op(n, f"ACC-{n}", percent=30)
        counts = dict.fromkeys(names[1:], 0)
        for _ in range(10):
            rid = self.request("OPS1")
            who = R.route_request(self.conn, rid, by="OPS1")["assignee"]
            counts[who] += 1
            R.set_task_status(self.conn, rid, who, "DONE", evidence="done")
        self.assertEqual(sorted(counts.values()), [2, 2, 2, 2, 2])

    def test_a_cap_on_open_tasks_and_a_lower_usage_preference_both_hold_across_team_sizes(self) -> None:
        for size in (2, 3, 5, 10, 12):
            with self.subTest(size=size):
                conn = R.open_registry(os.path.join(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")), "r.db"))
                self.addCleanup(conn.close)
                R.set_setting(conn, "rules_hash", RULES_HASH)
                R.set_setting(conn, "require_tokens", "0")
                for i in range(1, size + 1):
                    R.register_operator(conn, f"OPS{i}", account=f"A{i}")
                    R.ack_operator(conn, f"OPS{i}", RULES_HASH)
                    R.set_usage(conn, f"A{i}", 10.0 * i if i < 10 else 95.0, resets_at=iso(days_ahead=3), by=f"OPS{i}")
                    R.set_profile(conn, f"OPS{i}", max_tasks=2)
                first = None
                owners = []
                for _ in range(size * 2):
                    rid = R.create_request(conn, "job", origin="OPS1")
                    chosen = R.route_request(conn, rid, by="OPS1")
                    if chosen is None:
                        break
                    first = first or chosen["assignee"]
                    owners.append(chosen["assignee"])
                self.assertEqual(first, "OPS2", "the lowest-utilisation operator other than the asker goes first")
                for name in set(owners):
                    self.assertLessEqual(owners.count(name), 2)
                if size >= 5:
                    self.assertGreaterEqual(owners.count("OPS2"), owners.count(f"OPS{size}"))
                self.assertEqual(len(owners), len(set(r["id"] for r in R.list_tasks(conn) if r["owner"])))

    def test_models_follow_difficulty_and_explicit_choice_wins(self) -> None:
        self.assertEqual(R.choose_model("security", "review the token design"), "opus")
        self.assertEqual(R.choose_model("architecture", "plan it"), "opus")
        self.assertEqual(R.choose_model("docs,writing", "update the guide"), "sonnet")
        self.assertEqual(R.choose_model("code", "add a test"), "sonnet")
        self.assertEqual(R.choose_model("security", "x", explicit="sonnet"), "sonnet")


def _route(args: tuple[str, str, str]) -> str:
    db, rid, op = args
    conn = R.open_registry(db)
    try:
        return str(R.route_request(conn, rid, by=op)["assignee"])
    except (E.NotClaimable, E.AlreadyOwned):
        return "refused"
    finally:
        conn.close()


class TestConcurrentRouting(BalanceCase):
    def test_ten_operators_routing_the_same_request_assign_it_exactly_once(self) -> None:
        names = [f"OPS{i}" for i in range(1, 11)]
        for i, n in enumerate(names):
            self.op(n, f"A{i}", percent=10 + i)
        rid = self.request("OPS1")
        with ProcessPoolExecutor(max_workers=10) as pool:
            out = list(pool.map(_route, [(self.db, rid, n) for n in names]))
        self.assertEqual(len([o for o in out if o != "refused"]), 1, out)
        self.assertEqual(len(set(o for o in out if o != "refused")), 1)
        owner = self.owner(rid)
        self.assertNotEqual(owner, "")


class TestAgentPoolUsesBalancing(BalanceCase):
    def test_equal_agents_prefer_the_account_with_the_lowest_usage(self) -> None:
        self.op("OPS1", "A", percent=10)
        self.conn.execute("UPDATE operators SET admin=1 WHERE id='OPS1'")
        for name, acct, pct in (("SEC1", "X", 80), ("SEC2", "Y", 15)):
            R.register_operator(self.conn, name, kind="AGENT", skills="security", account=acct)
            R.ack_operator(self.conn, name, RULES_HASH)
            R.set_usage(self.conn, acct, pct, resets_at=iso(days_ahead=3), by="OPS1")
        placed = R.request_agent(self.conn, "OPS1", "review", needs="security")
        self.assertEqual(placed["agent_id"], "SEC2")


class TestStatusAndCli(unittest.TestCase):
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
        conn.execute("UPDATE operators SET account='ACC1' WHERE id IN ('OPS1','OPS2')")
        conn.execute("UPDATE operators SET account='ACC3' WHERE id='OPS3'")
        conn.execute("UPDATE settings SET value='1' WHERE key='require_tokens'")
        conn.close()

    def cli(self, *argv: str, op: str | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if op:
            env["SYNERGY_OPERATOR"] = op
        return subprocess.run([sys.executable, "-m", "project_synergy", *argv], env=env, capture_output=True, text=True,
                              check=False, timeout=60, cwd=str(ROOT))

    def test_usage_set_show_status_and_automatic_routing_through_the_command_line(self) -> None:
        resets = iso(days_ahead=3)
        self.assertEqual(self.cli("usage", "set", "ACC1", "--percent", "55", "--resets", resets, op="OPS1").returncode, 0)
        self.assertEqual(self.cli("usage", "set", "ACC3", "--percent", "12", "--resets", resets, op="OPS3").returncode, 0)
        shown = self.cli("usage", "show", op="OPS1").stdout
        for needle in ("ACC1", "55", "ACC3", "12", "fresh", "OPS1", "OPS2"):
            self.assertIn(needle, shown)
        added = self.cli("request", "add", "Do a thing", "--auto", op="OPS1")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertIn("OPS3", added.stdout)
        self.assertIn("12", added.stdout)
        status = self.cli("status", op="OPS1").stdout
        self.assertIn("USAGE", status)
        self.assertIn("ACC3", status)
        self.assertEqual(self.cli("usage", "set", "ACC1", "--percent", "140", op="OPS1").returncode, 3)

    def test_weights_have_defaults_and_can_be_changed(self) -> None:
        shown = self.cli("schedule", "show", op="OPS1").stdout
        for needle in ("usage", "skill", "load", "listening"):
            self.assertIn(needle, shown)
        self.assertEqual(self.cli("schedule", "set", "usage", "3", op="OPS1").returncode, 0)
        self.assertIn("usage", self.cli("schedule", "show", op="OPS1").stdout)
        self.assertEqual(self.cli("schedule", "set", "nonsense", "1", op="OPS1").returncode, 3)
        self.assertEqual(self.cli("schedule", "set", "usage", "-5", op="OPS1").returncode, 3)


    def test_read_only_views_work_without_an_operator_identity(self) -> None:
        for argv in (("usage", "show"), ("schedule", "show"), ("request", "list"), ("request", "status"),
                     ("agent", "list"), ("operator", "list"), ("status",)):
            done = self.cli(*argv)
            self.assertEqual(done.returncode, 0, f"{argv}: {done.stderr}")
        self.assertEqual(self.cli("usage", "set", "ACC1", "--percent", "5").returncode, 3, "writes still need an identity")
