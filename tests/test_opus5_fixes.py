import os
import signal
import sqlite3
import tempfile
import unittest
from datetime import UTC

import clean_env  # noqa: F401
from helpers import RegistryCase
from project_synergy import registry as R
from test_availability import AvailCase


class TestHookAndReminderFixes(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.active("OPS2")

    def test_a_revoked_operator_gets_no_hook_blocks_even_when_approval_is_not_required(self) -> None:
        self.conn.execute("UPDATE operators SET trust='REVOKED', status='ACTIVE' WHERE id='OPS2'")
        self.assertIsNone(R.hook_decision(self.conn, "OPS2"))

    def test_the_keepalive_reminder_is_capped_per_day_as_well_as_per_hour(self) -> None:
        from datetime import datetime, timedelta

        start = datetime.now(UTC)
        blocks = 0
        for i in range(22):
            if R.hook_decision(self.conn, "OPS2", now=start + timedelta(minutes=61 * i)):
                blocks += 1
        self.assertLessEqual(blocks, R.KEEPALIVE_MAX_PER_DAY)
        self.assertGreater(blocks, 0)

    def test_reminder_due_takes_no_write_lock_when_nothing_is_due(self) -> None:
        R.set_listening(self.conn, "OPS2")
        raw = sqlite3.connect(self.db, isolation_level=None, timeout=0.2)
        self.addCleanup(raw.close)
        raw.execute("BEGIN IMMEDIATE")
        other = R.open_registry(self.db, timeout=0.2)
        self.addCleanup(other.close)
        self.assertFalse(R.reminder_due(other, "OPS2"))
        raw.execute("ROLLBACK")

    def test_an_upgraded_registry_starts_with_the_keepalive_off(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "r.db")
            c = R.open_registry(db)
            R.register_operator(c, "X")
            c.execute("ALTER TABLE operators DROP COLUMN reminded_at")
            c.execute("DROP TABLE keepalive_log")
            c.execute("DELETE FROM settings WHERE key='hook_keepalive'")
            c.execute("UPDATE schema_version SET version=10")
            c.close()
            c = R.open_registry(db)
            self.addCleanup(c.close)
            self.assertEqual(R.get_setting(c, "hook_keepalive"), "off")

    def test_clearing_the_listening_mark_needs_no_authentication_for_a_suspended_operator(self) -> None:
        R.set_listening(self.conn, "OPS2")
        self.conn.execute("UPDATE operators SET status='SUSPENDED' WHERE id='OPS2'")
        R.clear_listening(self.conn, "OPS2")
        self.assertEqual(self.conn.execute("SELECT listening_at FROM operators WHERE id='OPS2'").fetchone()[0], "")


class TestWaitExitCodesAndHandlers(AvailCase):
    def test_a_signal_ends_the_wait_with_its_own_exit_code_and_the_reminder_can_be_switched_off(self) -> None:
        p = self.spawn("inbox", "wait", op="OPS3")
        self.assertTrue(self.wait_until(lambda: self.listening("OPS3")))
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        self.assertEqual(p.returncode, 7)
        conn = R.open_registry(self.db)
        conn.execute("UPDATE operators SET last_seen='2000-01-01T00:00:00+00:00', reminded_at=''")
        conn.close()
        self.assertNotIn("inbox wait", self.cli("status", op="OPS2", extra={"SYNERGY_NO_REMIND": "1"}).stderr)


if __name__ == "__main__":
    unittest.main()
