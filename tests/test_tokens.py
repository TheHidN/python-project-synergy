import contextlib
import io
import os
import sqlite3
import stat
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor

import clean_env  # noqa: F401
from project_synergy import cli
from project_synergy import errors as E
from project_synergy import registry as R

HASH = "a" * 64


class TokenCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "registry.db")
        self.conn = R.open_registry(self.db)
        self.addCleanup(self.conn.close)
        R.set_setting(self.conn, "rules_hash", HASH)
        self.tok: dict[str, str] = {}

    def operator(self, op: str) -> str:
        self.tok[op] = R.register_operator(self.conn, op)
        R.ack_operator(self.conn, op, HASH, token=self.tok[op])
        return self.tok[op]


class TestTokenIssue(TokenCase):
    def test_fresh_registry_requires_tokens_by_default(self) -> None:
        self.assertEqual(R.get_setting(self.conn, "require_tokens"), "1")

    def test_register_returns_a_long_random_token_and_stores_only_a_hash(self) -> None:
        a, b = R.register_operator(self.conn, "OPS1"), R.register_operator(self.conn, "OPS2")
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 32)
        with open(self.db, "rb") as f, contextlib.suppress(FileNotFoundError):
            raw = f.read()
        wal = self.db + "-wal"
        if os.path.exists(wal):
            with open(wal, "rb") as f:
                raw += f.read()
        self.assertNotIn(a.encode(), raw)
        for row in self.conn.execute("SELECT detail FROM events"):
            self.assertNotIn(a, row["detail"])
        self.assertEqual(len(self.conn.execute("SELECT token_hash FROM operators WHERE id='OPS1'").fetchone()[0]), 64)


class TestEnforcement(TokenCase):
    def setUp(self) -> None:
        super().setUp()
        self.operator("OPS1")
        self.operator("OPS2")
        R.create_task(self.conn, "T1", "a", actor="OPS1", token=self.tok["OPS1"])

    def test_missing_wrong_and_foreign_tokens_are_refused(self) -> None:
        with self.assertRaises(E.AuthFailed):
            R.claim_task(self.conn, "T1", "OPS1")
        with self.assertRaises(E.AuthFailed):
            R.claim_task(self.conn, "T1", "OPS1", token="nope")
        with self.assertRaises(E.AuthFailed):
            R.claim_task(self.conn, "T1", "OPS1", token=self.tok["OPS2"])
        self.assertIsNone(R.list_tasks(self.conn)[0]["owner"])
        self.assertTrue(R.claim_task(self.conn, "T1", "OPS1", token=self.tok["OPS1"]))

    def test_every_mutating_call_checks_the_token(self) -> None:
        R.claim_task(self.conn, "T1", "OPS1", token=self.tok["OPS1"])
        R.register_worker(self.conn, "W1", "OPS1", token=self.tok["OPS1"])
        calls = [
            lambda t: R.create_task(self.conn, "T9", "x", actor="OPS2", token=t),
            lambda t: R.assign_task(self.conn, "T9", "OPS2", by="OPS2", token=t),
            lambda t: R.release_task(self.conn, "T1", "OPS1", token=t),
            lambda t: R.set_task_status(self.conn, "T1", "OPS1", "IN_PROGRESS", token=t),
            lambda t: R.reassign_task(self.conn, "T1", "OPS2", decision_ref="d", by="OPS2", token=t),
            lambda t: R.claim_file(self.conn, "/f", "OPS1", token=t),
            lambda t: R.release_file(self.conn, "/f", "OPS1", token=t),
            lambda t: R.register_worker(self.conn, "W2", "OPS2", token=t),
            lambda t: R.set_worker_state(self.conn, "W1", "IDLE", by="OPS1", token=t),
            lambda t: R.heartbeat(self.conn, "OPS1", token=t),
        ]
        for i, call in enumerate(calls):
            for bad in ("", "wrong"):
                with self.assertRaises(E.AuthFailed, msg=f"call {i} with token {bad!r}"):
                    call(bad)

    def test_unacknowledged_operator_cannot_act_even_with_its_token(self) -> None:
        tok = R.register_operator(self.conn, "OPS3")
        with self.assertRaises(E.NotActive):
            R.claim_task(self.conn, "T1", "OPS3", token=tok)

    def test_ack_needs_the_operators_own_token(self) -> None:
        tok = R.register_operator(self.conn, "OPS3")
        with self.assertRaises(E.AuthFailed):
            R.ack_operator(self.conn, "OPS3", HASH, token="wrong")
        R.ack_operator(self.conn, "OPS3", HASH, token=tok)

    def test_worker_state_only_by_the_workers_operator(self) -> None:
        R.register_worker(self.conn, "W1", "OPS1", token=self.tok["OPS1"])
        with self.assertRaises(E.NotOwner):
            R.set_worker_state(self.conn, "W1", "IDLE", by="OPS2", token=self.tok["OPS2"])
        R.set_worker_state(self.conn, "W1", "IDLE", by="OPS1", token=self.tok["OPS1"])

    def test_source_compares_tokens_in_constant_time(self) -> None:
        text = open(R.__file__).read()
        self.assertIn("hmac.compare_digest", text)


class TestRotationAndReset(TokenCase):
    def setUp(self) -> None:
        super().setUp()
        self.operator("OPS1")
        self.operator("OPS2")

    def test_rotation_needs_the_current_token_and_replaces_it(self) -> None:
        with self.assertRaises(E.AuthFailed):
            R.issue_token(self.conn, "OPS1", current_token="wrong")
        new = R.issue_token(self.conn, "OPS1", current_token=self.tok["OPS1"])
        self.assertNotEqual(new, self.tok["OPS1"])
        with self.assertRaises(E.AuthFailed):
            R.heartbeat(self.conn, "OPS1", token=self.tok["OPS1"])
        R.heartbeat(self.conn, "OPS1", token=new)

    def test_reset_needs_a_decision_and_an_authenticated_active_decider(self) -> None:
        self.conn.execute("UPDATE operators SET admin=1 WHERE id='OPS2'")
        with self.assertRaises(E.ValidationError):
            R.reset_token(self.conn, "OPS1", decision_ref="", by="OPS2", token=self.tok["OPS2"])
        with self.assertRaises(E.AuthFailed):
            R.reset_token(self.conn, "OPS1", decision_ref="d1", by="OPS2", token="wrong")
        new = R.reset_token(self.conn, "OPS1", decision_ref="d1", by="OPS2", token=self.tok["OPS2"])
        R.heartbeat(self.conn, "OPS1", token=new)
        last = self.conn.execute("SELECT action, detail FROM events WHERE action='token.reset'").fetchone()
        self.assertIn("d1", last["detail"])
        self.assertNotIn(new, last["detail"])

    def test_cannot_turn_checking_on_while_an_operator_has_no_token(self) -> None:
        R.set_setting(self.conn, "require_tokens", "0")
        self.conn.execute("UPDATE operators SET token_hash='' WHERE id='OPS2'")
        with self.assertRaises(E.ValidationError):
            R.set_require_tokens(self.conn, True)
        self.conn.execute("UPDATE operators SET token_hash=? WHERE id='OPS2'", ("b" * 64,))
        R.set_require_tokens(self.conn, True)
        self.assertEqual(R.get_setting(self.conn, "require_tokens"), "1")


class TestMigration(unittest.TestCase):
    def test_a_version_1_registry_migrates_with_a_backup_and_keeps_its_data(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "registry.db")
            conn = sqlite3.connect(db, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            for stmt in R._split(R._SCHEMA_V1):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version VALUES (1)")
            conn.execute("INSERT INTO settings VALUES ('max_workers','2')")
            conn.execute("INSERT INTO settings VALUES ('rules_hash','abcdef123456')")
            conn.execute("INSERT INTO operators(id, role, status, registered_at, last_seen) "
                         "VALUES ('OPS1','r','ACTIVE','t','t')")
            R._log(conn, "system", "registry.init", "registry", "schema 1")
            conn.execute("COMMIT")
            conn.close()
            migrated = R.open_registry(db)
            self.addCleanup(migrated.close)
            self.assertEqual(migrated.execute("SELECT version FROM schema_version").fetchone()[0], R.SCHEMA_VERSION)
            self.assertEqual(migrated.execute("SELECT id FROM operators").fetchone()[0], "OPS1")
            self.assertEqual(R.get_setting(migrated, "require_tokens"), "0")
            self.assertEqual(R.get_setting(migrated, "rules_hash"), "abcdef123456")
            self.assertTrue(R.verify_chain(migrated)[0])
            backups = [n for n in os.listdir(d) if ".bak-v1" in n]
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, backups[0])).st_mode) & 0o077, 0)
            again = R.open_registry(db)
            again.close()
            self.assertEqual(len([n for n in os.listdir(d) if ".bak-v1" in n]), 1)


class TestTokenCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        cfg = os.path.join(self._tmp.name, "cfg")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "config.toml"), "w") as f:
            f.write('[operators]\ninitial = ["OPS1", "OPS2"]\n')
        for k, v in (("SYNERGY_STATE_DIR", self.state), ("SYNERGY_CONFIG_DIR", cfg)):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        for k in ("SYNERGY_OPERATOR", "SYNERGY_TOKEN_FILE", "SYNERGY_TOKEN"):
            os.environ.pop(k, None)
            self.addCleanup(os.environ.pop, k, None)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def as_operator(self, op: str) -> None:
        os.environ["SYNERGY_OPERATOR"] = op

    def token_path(self, op: str) -> str:
        return os.path.join(self.state, "tokens", f"{op}.token")

    def test_init_creates_both_initial_operators_with_private_tokens_and_prints_none(self) -> None:
        self.run_cli("rules", "set", "--hash", HASH)  # before init: refused, no registry yet
        code, out, _ = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertEqual(self.run_cli("rules", "set", "--hash", HASH)[0], 0)
        for op in ("OPS1", "OPS2"):
            path = self.token_path(op)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            token = open(path).read().strip()
            self.assertGreaterEqual(len(token), 32)
            self.assertNotIn(token, out)
        self.assertNotEqual(open(self.token_path("OPS1")).read(), open(self.token_path("OPS2")).read())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.token_path("OPS1"))).st_mode), 0o700)
        self.assertEqual([o for o in R.list_operators(R.open_registry(os.path.join(self.state, "registry.db")))
                          if o["status"] != "PENDING"], [])

    def test_init_twice_keeps_the_existing_tokens(self) -> None:
        self.run_cli("init")
        before = open(self.token_path("OPS1")).read()
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(open(self.token_path("OPS1")).read(), before)

    def test_a_session_acts_automatically_as_itself_and_never_as_another_operator(self) -> None:
        self.run_cli("init")
        self.run_cli("rules", "set", "--hash", HASH)
        code, _, err = self.run_cli("operator", "ack", "OPS1", HASH)
        self.assertEqual(code, 3)
        self.assertIn("identity", err)
        self.as_operator("OPS1")
        self.assertEqual(self.run_cli("operator", "ack", "OPS1", HASH)[0], 0)
        self.assertEqual(self.run_cli("task", "create", "T1", "x", "--actor", "OPS1")[0], 0)
        code, _, err = self.run_cli("task", "claim", "T1", "OPS2")
        self.assertEqual(code, 3)
        self.assertIn("OPS1", err)
        self.assertEqual(self.run_cli("task", "claim", "T1", "OPS1")[0], 0)

    def test_missing_wrong_or_loose_token_files_are_refused(self) -> None:
        self.run_cli("init")
        self.run_cli("rules", "set", "--hash", HASH)
        self.as_operator("OPS1")
        path = self.token_path("OPS1")
        good = open(path).read()
        os.chmod(path, 0o644)
        code, _, err = self.run_cli("operator", "ack", "OPS1", HASH)
        self.assertEqual(code, 3)
        self.assertIn("owner-only", err)
        os.chmod(path, 0o600)
        with open(path, "w") as f:
            f.write("wrong-token\n")
        self.assertEqual(self.run_cli("operator", "ack", "OPS1", HASH)[0], 3)
        os.remove(path)
        code, _, err = self.run_cli("operator", "ack", "OPS1", HASH)
        self.assertEqual(code, 3)
        self.assertIn("token", err)
        with open(path, "w") as f:
            f.write(good)
        os.chmod(path, 0o600)
        self.assertEqual(self.run_cli("operator", "ack", "OPS1", HASH)[0], 0)

    def test_rotation_replaces_the_file_atomically_and_privately(self) -> None:
        self.run_cli("init")
        self.run_cli("rules", "set", "--hash", HASH)
        self.as_operator("OPS1")
        old = open(self.token_path("OPS1")).read()
        code, out, _ = self.run_cli("operator", "token", "rotate", "OPS1")
        self.assertEqual(code, 0)
        new = open(self.token_path("OPS1")).read()
        self.assertNotEqual(old, new)
        self.assertNotIn(new.strip(), out)
        self.assertEqual(stat.S_IMODE(os.stat(self.token_path("OPS1")).st_mode), 0o600)
        self.assertEqual(self.run_cli("operator", "heartbeat", "OPS1")[0], 0)
        with open(self.token_path("OPS1"), "w") as f:
            f.write(old)
        os.chmod(self.token_path("OPS1"), 0o600)
        self.assertEqual(self.run_cli("operator", "heartbeat", "OPS1")[0], 3)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.token_path("OPS1"))) if n.endswith(".tmp")], [])

    def test_onboarding_style_registration_creates_a_token_and_no_output_of_it(self) -> None:
        self.run_cli("init")
        code, out, _ = self.run_cli("operator", "register", "OPS3")
        self.assertEqual(code, 0)
        token = open(self.token_path("OPS3")).read().strip()
        self.assertNotIn(token, out)
        self.assertIn("SYNERGY_OPERATOR", out)
        self.assertEqual(self.run_cli("operator", "register", "OPS3")[0], 3)
        self.assertEqual(open(self.token_path("OPS3")).read().strip(), token)

    def test_backfill_issues_tokens_to_operators_that_have_none_then_requires_them(self) -> None:
        self.run_cli("init", "--no-require-tokens")
        conn = R.open_registry(os.path.join(self.state, "registry.db"))
        conn.execute("UPDATE operators SET token_hash=''")
        conn.close()
        os.remove(self.token_path("OPS1"))
        os.remove(self.token_path("OPS2"))
        code, out, _ = self.run_cli("operator", "token", "backfill")
        self.assertEqual(code, 0)
        self.assertIn("OPS1", out)
        for op in ("OPS1", "OPS2"):
            self.assertEqual(stat.S_IMODE(os.stat(self.token_path(op)).st_mode), 0o600)
        self.assertEqual(self.run_cli("operator", "token", "backfill")[0], 0)
        self.assertEqual(R.get_setting(R.open_registry(os.path.join(self.state, "registry.db")), "require_tokens"), "1")

    def test_tokens_never_appear_in_the_repository_or_docs(self) -> None:
        self.run_cli("init")
        tokens = [open(self.token_path(op)).read().strip() for op in ("OPS1", "OPS2")]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as f:
                    data = f.read()
                for tok in tokens:
                    self.assertNotIn(tok.encode(), data, name)


def _rotate(args: tuple[str, str]) -> str:
    db, current = args
    conn = R.open_registry(db)
    try:
        R.issue_token(conn, "OPS1", current_token=current)
        return "ok"
    except E.AuthFailed:
        return "denied"
    finally:
        conn.close()


def _claim_with(args: tuple[str, str, str]) -> str:
    db, op, token = args
    conn = R.open_registry(db)
    try:
        R.claim_task(conn, "T1", op, token=token)
        return "ok"
    except E.AuthFailed:
        return "denied"
    except E.AlreadyOwned:
        return "conflict"
    finally:
        conn.close()


class TestTokenConcurrency(TokenCase):
    def test_twenty_simultaneous_rotations_with_one_token_give_one_winner(self) -> None:
        tok = self.operator("OPS1")
        with ProcessPoolExecutor(max_workers=20) as pool:
            out = list(pool.map(_rotate, [(self.db, tok)] * 20))
        self.assertEqual(out.count("ok"), 1)
        self.assertEqual(out.count("denied"), 19)
        with self.assertRaises(E.AuthFailed):
            R.heartbeat(self.conn, "OPS1", token=tok)

    def test_token_checked_claims_race_gives_one_winner_and_wrong_tokens_never_win(self) -> None:
        names = [f"OPS{i}" for i in range(10)]
        tokens = {n: self.operator(n) for n in names}
        R.create_task(self.conn, "T1", "contested", actor="OPS0", token=tokens["OPS0"])
        good = [(self.db, n, tokens[n]) for n in names]
        bad = [(self.db, n, "wrong-token") for n in names]
        with ProcessPoolExecutor(max_workers=20) as pool:
            out = list(pool.map(_claim_with, good + bad))
        self.assertEqual(out[: len(good)].count("ok"), 1)
        self.assertEqual(out[len(good):], ["denied"] * len(bad))
        self.assertEqual(R.list_tasks(self.conn)[0]["owner"] in names, True)
