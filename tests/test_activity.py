import unittest
from datetime import datetime, timedelta, timezone

import clean_env  # noqa: F401
from helpers import RegistryCase

from project_synergy import registry as R


def ago(**kw: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


class TestOperatorActivity(RegistryCase):
    def line(self, text: str, op: str) -> str:
        section = text.split("OPERATORS", 1)[1].split("\nUSAGE", 1)[0]
        return [ln for ln in section.splitlines() if ln.strip().startswith(op)][0]

    def test_connection_work_state_tasks_and_last_activity_are_shown_for_each_operator(self) -> None:
        for name in ("OPS1", "OPS2", "OPS3", "OPS4"):
            self.active(name)
        R.register_operator(self.conn, "OPS5")
        R.set_listening(self.conn, "OPS1")
        R.create_task(self.conn, "T-1", "first", actor="OPS1")
        R.claim_task(self.conn, "T-1", "OPS1")
        R.create_task(self.conn, "T-2", "second", actor="OPS2")
        R.claim_task(self.conn, "T-2", "OPS2")
        R.set_task_status(self.conn, "T-2", "OPS2", "BLOCKED", evidence="waiting for a decision")
        self.conn.execute("UPDATE operators SET last_seen=? WHERE id='OPS2'", (ago(minutes=20),))
        self.conn.execute("UPDATE operators SET last_seen=? WHERE id='OPS3'", (ago(hours=30),))
        R.set_profile(self.conn, "OPS4", availability="OUT_OF_USAGE")
        text = R.status_report(self.conn)
        one, two = self.line(text, "OPS1"), self.line(text, "OPS2")
        self.assertIn("connected", one)
        self.assertIn("BUSY", one)
        self.assertIn("T-1", one)
        self.assertIn("BLOCKED", two)
        self.assertIn("T-2", two)
        self.assertIn("recent", two)
        self.assertIn("offline", self.line(text, "OPS3"))
        self.assertIn("AVAILABLE", self.line(text, "OPS3"))
        self.assertIn("OUT_OF_USAGE", self.line(text, "OPS4"))
        self.assertIn("not onboarded", self.line(text, "OPS5"))
        self.assertRegex(one, r"last activity \d{4}-\d{2}-\d{2}T")

    def test_an_idle_connected_operator_is_available(self) -> None:
        self.active("OPS1")
        R.set_listening(self.conn, "OPS1")
        line = self.line(R.status_report(self.conn), "OPS1")
        self.assertIn("connected", line)
        self.assertIn("AVAILABLE", line)


class TestRequestCloseFinishesTheRootTask(RegistryCase):
    def test_closing_a_request_marks_its_task_done_so_nobody_looks_busy(self) -> None:
        self.active("OPS1")
        self.active("OPS2")
        rid = R.create_request(self.conn, "work", origin="OPS1")
        R.take_request(self.conn, rid, "OPS2")
        R.close_request(self.conn, rid, "OPS1", summary="done")
        root = [x for x in R.list_tasks(self.conn) if x["id"] == rid][0]
        self.assertEqual(root["status"], "DONE")
        self.assertIn("AVAILABLE", self.conn.execute("SELECT 1").fetchone() and R.status_report(self.conn).split("OPS2")[1].split("\n")[0])



if __name__ == "__main__":
    unittest.main()
