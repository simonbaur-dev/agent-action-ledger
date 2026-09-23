"""Re-registering from a poll must never undo progress."""
from __future__ import annotations

import unittest

from .support import KIND, NAMESPACE, LedgerTestCase


class RegistrationTests(LedgerTestCase):
    def test_unchanged_payload_writes_nothing_new(self):
        key = self.register("S-1", v=1)
        before = len(self.ledger.history(key))
        self.register("S-1", v=1)
        self.assertEqual(before, len(self.ledger.history(key)))

    def test_changed_payload_is_recorded_as_an_event(self):
        key = self.register("S-1", v=1)
        self.register("S-1", v=2)
        kinds = [event["kind"] for event in self.ledger.history(key)]
        self.assertEqual(["opened", "source_changed"], kinds)
        self.assertEqual({"v": 2}, self.ledger.get(key)["payload"])

    def test_changed_payload_pulls_a_waiting_case_forward(self):
        key = self.register("S-1", v=1)
        case = self.claim_one()
        self.ledger.finish(
            case["id"], case["lease_token"],
            status="waiting", reason="blocked upstream", next_check=self.now + 86400,
        )
        self.assertEqual(self.now + 86400, self.ledger.get(key)["next_check"])
        self.ledger.register(NAMESPACE, KIND, "S-1", owner="triage", payload={"v": 2})
        self.assertEqual(self.now, self.ledger.get(key)["next_check"])

    def test_changed_payload_does_not_disturb_a_human_decision(self):
        key = self.register("S-1", v=1)
        case = self.claim_one()
        self.ledger.finish(
            case["id"], case["lease_token"],
            status="human_decision", reason="ambiguous", next_check=self.now + 3600,
            question="Which of the two records is meant?",
        )
        self.ledger.register(NAMESPACE, KIND, "S-1", owner="triage", payload={"v": 2})
        after = self.ledger.get(key)
        self.assertEqual("human_decision", after["status"])
        self.assertEqual(self.now + 3600, after["next_check"])
        self.assertEqual("Which of the two records is meant?", after["question"])

    def test_a_poll_cannot_reopen_a_completed_case(self):
        key = self.register("S-1", v=1)
        case = self.claim_one()
        self.ledger.finish(
            case["id"], case["lease_token"],
            status="complete", reason="verified", readback=self.receipt(),
        )
        completed_at = self.ledger.get(key)["updated_at"]

        self.now += 100
        self.ledger.register(NAMESPACE, KIND, "S-1", owner="triage", payload={"v": 2})
        after = self.ledger.get(key)
        self.assertEqual("complete", after["status"])
        self.assertIsNone(after["next_check"])
        # The completion timestamp is the fence that later reopening evidence
        # has to beat; a poll must not move it.
        self.assertEqual(completed_at, after["updated_at"])

    def test_next_check_can_be_scheduled_in_the_future(self):
        key = self.ledger.register(
            NAMESPACE, KIND, "S-2", owner="triage", payload={}, next_check=self.now + 60
        )
        self.assertEqual([], self.ledger.due())
        self.now += 61
        self.assertEqual([key], [case["id"] for case in self.ledger.due()])

    def test_payload_round_trips_as_a_dictionary(self):
        key = self.ledger.register(
            NAMESPACE, KIND, "S-3", owner="triage",
            payload={"nested": {"list": [1, 2, 3]}, "text": "Rückfrage"},
        )
        self.assertEqual(
            {"nested": {"list": [1, 2, 3]}, "text": "Rückfrage"},
            self.ledger.get(key)["payload"],
        )

    def test_unknown_case_reads_as_none(self):
        self.assertIsNone(self.ledger.get("support:ticket:doesnotexist00000000"))


if __name__ == "__main__":
    unittest.main()
