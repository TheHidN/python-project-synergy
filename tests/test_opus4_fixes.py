import unittest

import clean_env  # noqa: F401
from helpers import RULES_HASH, RegistryCase
from project_synergy import errors as E
from project_synergy import registry as R


class Last(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.active("OPS1")
        self.conn.execute("UPDATE operators SET trust='APPROVED', admin=1, approved_by='o', approval_ref='i', approved_at='t' WHERE id='OPS1'")
        R.set_setting(self.conn, "require_approval", "1")
        R.set_setting(self.conn, "bootstrapped", "1")


class TestRulesHashNeedsTheCallersToken(Last):
    def test_with_tokens_required_a_wrong_or_missing_token_is_refused(self) -> None:
        self.conn.execute("UPDATE operators SET token_hash='' WHERE id='OPS1'")
        tok = R.issue_token(self.conn, "OPS1")
        R.set_setting(self.conn, "require_tokens", "1")
        with self.assertRaises(E.AuthFailed):
            R.set_rules_hash(self.conn, "c" * 64, by="OPS1", token="wrong")
        with self.assertRaises(E.AuthFailed):
            R.set_rules_hash(self.conn, "c" * 64, by="OPS1")
        R.set_rules_hash(self.conn, "c" * 64, by="OPS1", token=tok)


class TestAgentScopesBindJobs(Last):
    def test_an_agent_scoped_to_one_project_is_not_given_jobs_for_another(self) -> None:
        R.register_operator(self.conn, "AG", kind="AGENT", skills="x")
        R.approve_operator(self.conn, "AG", by="OPS1", approval_ref="r", scopes="alpha")
        R.ack_operator(self.conn, "AG", RULES_HASH)
        beta = R.create_request(self.conn, "beta", origin="OPS1", project="beta")
        alpha = R.create_request(self.conn, "alpha", origin="OPS1", project="alpha")
        wrong = R.request_agent(self.conn, "OPS1", "job", agent_id="AG", request_id=beta)
        self.assertEqual(wrong["status"], "QUEUED")
        also = R.request_agent(self.conn, "OPS1", "job2", needs="x", request_id=beta)
        self.assertEqual(also["status"], "QUEUED")
        right = R.request_agent(self.conn, "OPS1", "job3", agent_id="AG", request_id=alpha)
        self.assertEqual(right["status"], "RESERVED")


class TestPacketHashAndConsistency(Last):
    def test_a_packet_hash_can_only_be_set_for_an_operator_that_is_not_yet_active(self) -> None:
        R.register_operator(self.conn, "B")
        R.approve_operator(self.conn, "B", by="OPS1", approval_ref="r")
        R.ack_operator(self.conn, "B", RULES_HASH)
        with self.assertRaises(E.NotActive):
            R.set_packet_hash(self.conn, "B", "z" * 64)
        R.register_operator(self.conn, "C")
        R.set_packet_hash(self.conn, "C", "z" * 64)

    def test_reauthorize_refuses_admin_for_an_agent_like_approve_does(self) -> None:
        R.register_operator(self.conn, "AG", kind="AGENT")
        R.approve_operator(self.conn, "AG", by="OPS1", approval_ref="r")
        with self.assertRaises(E.ValidationError):
            R.reauthorize_operator(self.conn, "AG", by="OPS1", approval_ref="r2", admin=True)


if __name__ == "__main__":
    unittest.main()
