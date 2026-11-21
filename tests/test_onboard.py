import contextlib
import hashlib
import io
import os
import stat
import subprocess
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from unittest import mock

import clean_env  # noqa: F401
from project_synergy import cli
from project_synergy import registry as R

HASH = "c" * 64


def _race(args: tuple[str, str]) -> str:
    state, op = args
    os.environ["SYNERGY_STATE_DIR"] = state
    os.environ["SYNERGY_CONFIG_DIR"] = os.path.join(state, "..", "cfg")
    os.environ["SYNERGY_OPERATOR"] = "OPS1"
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["onboard", op, "--approval-ref", "t"])
    return "ok" if code == 0 else f"refused:{code}"


class OnboardCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        self.cfg = os.path.join(self._tmp.name, "cfg")
        os.makedirs(self.cfg)
        self.rules = os.path.join(self._tmp.name, "RULES.md")
        with open(self.rules, "w") as f:
            f.write("the shared operating rules\n")
        with open(os.path.join(self.cfg, "config.toml"), "w") as f:
            f.write(f'[operators]\ninitial = ["OPS1", "OPS2"]\n[onboarding]\nrules_files = ["{self.rules}"]\n')
        for k, v in (("SYNERGY_STATE_DIR", self.state), ("SYNERGY_CONFIG_DIR", self.cfg)):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        os.environ.pop("SYNERGY_OPERATOR", None)
        self.addCleanup(os.environ.pop, "SYNERGY_OPERATOR", None)
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("rules", "set", "--hash", HASH)[0], 0)
        self.db = os.path.join(self.state, "registry.db")
        for op in ("OPS1", "OPS2"):
            os.environ["SYNERGY_OPERATOR"] = op
            self.assertEqual(self.run_cli("operator", "ack", op, HASH)[0], 0)
        os.environ["SYNERGY_OPERATOR"] = "OPS1"
        self.run_cli("task", "create", "T1", "an open task", "--actor", "OPS1")
        self.run_cli("task", "claim", "T1", "OPS1")

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def paths(self, op: str) -> tuple[str, str, str]:
        return (os.path.join(self.state, "packets", f"{op}.md"), os.path.join(self.state, "launchers", f"{op}.sh"),
                os.path.join(self.state, "tokens", f"{op}.token"))

    def packet_hash(self, op: str) -> str:
        with open(self.paths(op)[0], "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()


class TestOnboard(OnboardCase):
    def test_onboard_registers_pending_with_private_packet_launcher_and_token(self) -> None:
        code, out, _ = self.run_cli("onboard", "OPS3", "--approval-ref", "t")
        self.assertEqual(code, 0)
        packet, launcher, token = self.paths("OPS3")
        self.assertEqual(stat.S_IMODE(os.stat(packet).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(launcher).st_mode), 0o700)
        secret = open(token).read().strip()
        for text in (open(packet).read(), open(launcher).read(), out):
            self.assertNotIn(secret, text)
        self.assertIn("SYNERGY_OPERATOR=OPS3", open(launcher).read())
        self.assertIn("NOT COMPLETE", out)
        conn = R.open_registry(self.db)
        row = [o for o in R.list_operators(conn) if o["id"] == "OPS3"][0]
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["packet_hash"], self.packet_hash("OPS3"))
        conn.close()

    def test_packet_lists_operators_tasks_rules_with_hashes_and_the_safety_rules(self) -> None:
        self.run_cli("onboard", "OPS3", "--approval-ref", "t")
        text = open(self.paths("OPS3")[0]).read()
        for needle in ("OPS1", "OPS2", "T1", HASH, self.rules, hashlib.sha256(b"the shared operating rules\n").hexdigest(),
                       "never close", "claim", "wait for an assignment", "ack OPS3"):
            self.assertIn(needle, text)
        self.assertNotIn(self.db, text)

    def test_complete_only_after_the_instance_acknowledges_its_own_packet(self) -> None:
        self.run_cli("onboard", "OPS3", "--approval-ref", "t")
        code, out, _ = self.run_cli("onboard", "--status", "OPS3")
        self.assertEqual(code, 3)
        self.assertIn("NOT COMPLETE", out)
        os.environ["SYNERGY_OPERATOR"] = "OPS3"
        self.assertEqual(self.run_cli("operator", "ack", "OPS3", HASH)[0], 3)
        self.assertEqual(self.run_cli("operator", "ack", "OPS3", "d" * 64)[0], 3)
        self.assertEqual(self.run_cli("operator", "ack", "OPS3", self.packet_hash("OPS3"))[0], 0)
        code, out, _ = self.run_cli("onboard", "--status", "OPS3")
        self.assertEqual(code, 0)
        self.assertIn("COMPLETE", out)
        self.assertNotIn("NOT COMPLETE", out)

    def test_unacknowledged_operator_cannot_claim_or_be_assigned(self) -> None:
        self.run_cli("onboard", "OPS3", "--approval-ref", "t")
        os.environ["SYNERGY_OPERATOR"] = "OPS1"
        self.run_cli("task", "create", "T2", "another task", "--actor", "OPS1")
        code, _, err = self.run_cli("task", "assign", "T2", "OPS3", "--by", "OPS1")
        self.assertEqual(code, 3)
        self.assertIn("ACTIVE", err)
        os.environ["SYNERGY_OPERATOR"] = "OPS3"
        code, _, err = self.run_cli("task", "claim", "T2", "OPS3")
        self.assertEqual(code, 3)
        self.assertIn("ACTIVE", err)

    def test_an_onboarded_operator_cannot_take_an_owned_task_or_act_as_another(self) -> None:
        self.run_cli("onboard", "OPS3", "--approval-ref", "t")
        os.environ["SYNERGY_OPERATOR"] = "OPS3"
        self.run_cli("operator", "ack", "OPS3", self.packet_hash("OPS3"))
        code, _, err = self.run_cli("task", "claim", "T1", "OPS3")
        self.assertEqual(code, 3)
        self.assertIn("OPS1", err)
        code, _, err = self.run_cli("task", "release", "T1", "OPS1")
        self.assertEqual(code, 3)
        self.assertIn("refusing", err)

    def test_duplicate_onboarding_is_refused_and_changes_nothing(self) -> None:
        self.run_cli("onboard", "OPS3", "--approval-ref", "t")
        before = [open(p).read() for p in self.paths("OPS3")]
        for name in ("OPS3", "ops3"):
            self.assertEqual(self.run_cli("onboard", name)[0], 3)
        self.assertEqual(before, [open(p).read() for p in self.paths("OPS3")])
        self.assertEqual(self.run_cli("onboard", "OPS1", "--approval-ref", "t")[0], 3)

    def test_prerequisites_are_checked_before_anything_is_created(self) -> None:
        os.remove(self.rules)
        code, _, err = self.run_cli("onboard", "OPS3", "--approval-ref", "t")
        self.assertEqual(code, 3)
        self.assertIn("RULES.md", err)
        conn = R.open_registry(self.db)
        self.assertNotIn("OPS3", [o["id"] for o in R.list_operators(conn)])
        conn.close()
        self.assertFalse(os.path.exists(self.paths("OPS3")[0]))

    def test_no_rules_hash_means_no_onboarding(self) -> None:
        conn = R.open_registry(self.db)
        conn.execute("DELETE FROM settings WHERE key='rules_hash'")
        conn.close()
        code, _, err = self.run_cli("onboard", "OPS3", "--approval-ref", "t")
        self.assertEqual(code, 3)
        self.assertIn("rules hash", err)

    def test_nothing_is_launched_during_onboarding(self) -> None:
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("launched a process")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("launched a process")), \
                mock.patch.object(os, "system", side_effect=AssertionError("launched a process")):
            self.assertEqual(self.run_cli("onboard", "OPS3", "--approval-ref", "t")[0], 0)
        text = open(os.path.join(os.path.dirname(cli.__file__), "onboard.py")).read()
        self.assertNotRegex(text, r"import subprocess|from \. import tmux|os\.system|Popen|os\.exec|os\.spawn")

    def test_twenty_simultaneous_onboardings_of_one_id_give_one_winner(self) -> None:
        with ProcessPoolExecutor(max_workers=20) as pool:
            out = list(pool.map(_race, [(self.state, "OPS9")] * 20))
        self.assertEqual(out.count("ok"), 1)
        self.assertEqual(out.count("refused:3"), 19)
        self.assertEqual(len([n for n in os.listdir(os.path.join(self.state, "packets")) if n.startswith("OPS9")]), 1)

    def test_invalid_ids_are_refused(self) -> None:
        for bad in ("", "has space", "../x", "x" * 40):
            self.assertNotEqual(self.run_cli("onboard", bad)[0], 0)

    def test_status_of_an_unknown_operator(self) -> None:
        code, out, err = self.run_cli("onboard", "--status", "NOPE")
        self.assertEqual(code, 3)
