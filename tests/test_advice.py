import unittest

import clean_env  # noqa: F401
from helpers import RULES_HASH, RegistryCase
from project_synergy import registry as R


class AdviceCase(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        for name, skills in (("OPS1", ""), ("OPS2", "docs"), ("OPS3", "security,code")):
            R.register_operator(self.conn, name, account=f"A-{name}", skills=skills)
            R.approve_operator(self.conn, name, by="owner", approval_ref="install") if name == "OPS1" else None
            if name != "OPS1":
                self.conn.execute("UPDATE operators SET trust='APPROVED', approved_by='t', approval_ref='t', approved_at='t' WHERE id=?",
                                  (name,))
            R.ack_operator(self.conn, name, RULES_HASH) if name == "OPS1" else R.ack_operator(self.conn, name, RULES_HASH)
        R.set_setting(self.conn, "require_approval", "1")
        R.set_setting(self.conn, "bootstrapped", "1")
        for name in ("OPS2", "OPS3"):
            R.set_listening(self.conn, name)


class TestAdvice(AdviceCase):
    def test_a_simple_instruction_is_handled_locally(self) -> None:
        a = R.delegate_advice(self.conn, "OPS1", "What time zone is the server in?")
        self.assertEqual(a["decision"], "LOCAL")
        self.assertIn("simple", str(a["reason"]))

    def test_a_multi_part_instruction_is_delegated_and_names_who_would_get_it(self) -> None:
        a = R.delegate_advice(self.conn, "OPS1", "Prepare the release:\n- write the notes\n- check the docs\n- run the tests")
        self.assertEqual(a["decision"], "DELEGATE")
        self.assertEqual(a["parts"], 3)
        self.assertTrue(a["candidates"])

    def test_a_single_task_that_matches_a_colleagues_skill_is_delegated_to_a_listening_colleague(self) -> None:
        a = R.delegate_advice(self.conn, "OPS1", "Do a security review of the installer")
        self.assertEqual(a["decision"], "DELEGATE")
        self.assertEqual(a["candidates"][0]["id"], "OPS3")
        self.assertEqual(a["model"], "opus")

    def test_an_independent_check_is_delegated_because_it_must_not_be_done_by_the_author(self) -> None:
        a = R.delegate_advice(self.conn, "OPS1", "Please have someone independently verify the quick start works")
        self.assertEqual(a["decision"], "DELEGATE")
        self.assertIn("independent", str(a["reason"]))
        self.assertNotIn("OPS1", [c["id"] for c in a["candidates"]])

    def test_nobody_listening_or_eligible_means_local(self) -> None:
        self.conn.execute("UPDATE operators SET listening_at=''")
        a = R.delegate_advice(self.conn, "OPS1", "Prepare it:\n- one\n- two")
        self.assertEqual(a["decision"], "LOCAL")
        self.assertIn("no listening", str(a["reason"]))

    def test_holds_scopes_and_the_right_to_assign_are_respected(self) -> None:
        self.conn.execute("UPDATE operators SET admin=1 WHERE id='OPS1'")
        R.set_hold(self.conn, "beta", True, by="OPS1", reason="hold")
        a = R.delegate_advice(self.conn, "OPS1", "Prepare it:\n- one\n- two", project="beta")
        self.assertEqual(a["decision"], "LOCAL")
        self.assertIn("hold", str(a["reason"]))
        self.conn.execute("UPDATE operators SET can_assign=0 WHERE id='OPS1'")
        b = R.delegate_advice(self.conn, "OPS1", "Prepare it:\n- one\n- two")
        self.assertEqual(b["decision"], "LOCAL")
        self.assertIn("not allowed to assign", str(b["reason"]))

    def test_advice_changes_nothing(self) -> None:
        before = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        R.delegate_advice(self.conn, "OPS1", "Prepare it:\n- one\n- two")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
