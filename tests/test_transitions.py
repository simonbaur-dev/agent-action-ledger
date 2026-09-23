"""State transitions, and the evidence each one costs."""
from __future__ import annotations

import unittest

from agent_action_ledger import (
    InvalidTransition,
    Readback,
    UnknownCase,
    ValidationError,
)

from .support import LedgerTestCase


class CompletionTests(LedgerTestCase):
    def test_completion_requires_a_read_back(self):
        key = self.register("S-1")
        case = self.claim_one()
        with self.assertRaises(ValidationError):
            self.ledger.finish(
                key, case["lease_token"], status="complete", reason="the script exited 0"
            )
        self.assertEqual("pending", self.ledger.get(key)["status"])

    def test_completion_refuses_a_read_back_older_than_the_case(self):
        key = self.register("S-1")
        case = self.claim_one()
        self.now += 50
        stale = self.receipt(at=self.now - 100)
        with self.assertRaises(ValidationError):
            self.ledger.finish(
                key, case["lease_token"], status="complete",
                reason="looked before the work", readback=stale,
            )

    def test_completion_refuses_a_stale_read_back(self):
        key = self.register("S-1")
        case = self.claim_one()
        observed = self.receipt()
        self.now += Readback.MAX_AGE_SECONDS + 1
        with self.assertRaises(ValidationError):
            self.ledger.finish(
                key, case["lease_token"], status="complete",
                reason="evidence has gone cold", readback=observed,
            )

    def test_completion_refuses_a_hand_made_receipt(self):
        key = self.register("S-1")
        case = self.claim_one()
        for bad in (
            {"source": "x", "record_id": "y", "observed_at": self.now, "assertion": "z"},
            "looks fine to me",
            None,
        ):
            with self.assertRaises(ValidationError):
                self.ledger.finish(
                    key, case["lease_token"], status="complete",
                    reason="not a Readback", readback=bad,
                )

    def test_completion_clears_the_schedule_and_the_lease(self):
        key = self.register("S-1")
        case = self.claim_one()
        done = self.ledger.finish(
            key, case["lease_token"], status="complete",
            reason="verified in the source system", readback=self.receipt(),
        )
        self.assertEqual("complete", done["status"])
        self.assertIsNone(done["next_check"])
        self.assertIsNone(done["lease_token"])
        self.assertIsNone(done["lease_owner"])

    def test_read_back_fields_must_all_be_present(self):
        for bad in (
            Readback("", "r", 1000.0, "a"),
            Readback("s", "", 1000.0, "a"),
            Readback("s", "r", 1000.0, ""),
            Readback("s", "r", float("nan"), "a"),
        ):
            with self.assertRaises(ValueError):
                bad.validate(1000.0)


class OpenStateTests(LedgerTestCase):
    def test_open_states_need_a_future_next_check(self):
        key = self.register("S-1")
        case = self.claim_one()
        for next_check in (None, self.now, self.now - 1):
            with self.assertRaises(ValidationError):
                self.ledger.finish(
                    key, case["lease_token"], status="waiting",
                    reason="blocked", next_check=next_check,
                )

    def test_every_transition_needs_a_concrete_reason(self):
        key = self.register("S-1")
        case = self.claim_one()
        with self.assertRaises(ValidationError):
            self.ledger.finish(
                key, case["lease_token"], status="waiting",
                reason="   ", next_check=self.now + 5,
            )

    def test_unknown_status_is_refused(self):
        key = self.register("S-1")
        case = self.claim_one()
        with self.assertRaises(ValidationError):
            self.ledger.finish(
                key, case["lease_token"], status="done",
                reason="typo", next_check=self.now + 5,
            )

    def test_parking_on_a_human_requires_the_question(self):
        key = self.register("S-1")
        case = self.claim_one()
        with self.assertRaises(ValidationError):
            self.ledger.finish(
                key, case["lease_token"], status="human_decision",
                reason="ambiguous", next_check=self.now + 5,
            )
        parked = self.ledger.finish(
            key, case["lease_token"], status="human_decision",
            reason="two plausible matches", next_check=self.now + 5,
            question="Which of the two records does this belong to?",
        )
        self.assertEqual(
            "Which of the two records does this belong to?", parked["question"]
        )


class HumanDecisionTests(LedgerTestCase):
    def park(self) -> str:
        key = self.register("S-1")
        case = self.claim_one()
        self.ledger.finish(
            key, case["lease_token"], status="human_decision",
            reason="ambiguous", next_check=self.now + 3600,
            question="Which of the two records does this belong to?",
        )
        return key

    def test_answering_makes_the_case_pending_not_complete(self):
        key = self.park()
        resumed = self.ledger.resume_human_decision(
            key, reason="operator answered", answer="the second one",
            next_check=self.now + 10,
        )
        self.assertEqual("pending", resumed["status"])
        event = self.ledger.history(key)[-1]
        self.assertEqual("human_decision_resumed", event["kind"])
        self.assertEqual("the second one", event["detail"]["answer"])
        self.assertEqual(
            "Which of the two records does this belong to?",
            event["detail"]["question"],
        )

    def test_only_a_parked_case_can_be_resumed(self):
        key = self.register("S-2")
        with self.assertRaises(InvalidTransition):
            self.ledger.resume_human_decision(key, reason="not parked")
        with self.assertRaises(UnknownCase):
            self.ledger.resume_human_decision("demo:ticket:nope", reason="missing")

    def test_a_live_lease_blocks_resuming(self):
        key = self.park()
        self.now += 3601
        self.claim_one(statuses=["human_decision"])
        with self.assertRaises(InvalidTransition):
            self.ledger.resume_human_decision(key, reason="racing the worker")

    def test_an_unsettled_action_blocks_resuming(self):
        key = self.park()
        self.ledger.arm_writes("test fixture")
        self.now += 3601
        case = self.claim_one(statuses=["human_decision"])
        self.ledger.begin_action(
            key, case["lease_token"], "effect:1", kind="reply", request={}
        )
        self.ledger.finish(
            key, case["lease_token"], status="human_decision",
            reason="still ambiguous", next_check=self.now + 60,
            question="Which record?",
        )
        with self.assertRaises(InvalidTransition):
            self.ledger.resume_human_decision(key, reason="unknown effect outstanding")

    def test_resume_needs_a_reason_and_a_future_check(self):
        key = self.park()
        with self.assertRaises(ValidationError):
            self.ledger.resume_human_decision(key, reason=" ")
        with self.assertRaises(ValidationError):
            self.ledger.resume_human_decision(
                key, reason="ok", next_check=self.now - 1
            )


class ReopeningTests(LedgerTestCase):
    def complete(self) -> str:
        key = self.register("S-1")
        case = self.claim_one()
        self.ledger.finish(
            key, case["lease_token"], status="complete",
            reason="verified", readback=self.receipt(),
        )
        return key

    def test_reopening_requires_evidence_newer_than_the_completion(self):
        key = self.complete()
        stale = self.receipt("something changed", at=self.now)
        self.now += 10
        with self.assertRaises(ValidationError):
            self.ledger.reopen_with_evidence(
                key, reason="stale evidence", owner="triage", readback=stale
            )
        fresh = self.receipt("the record was reversed")
        reopened = self.ledger.reopen_with_evidence(
            key, reason="the record was reversed", owner="triage", readback=fresh
        )
        self.assertEqual("pending", reopened["status"])
        self.assertIsNotNone(reopened["next_check"])

    def test_only_completed_cases_can_be_reopened(self):
        key = self.register("S-5")
        self.now += 1
        with self.assertRaises(InvalidTransition):
            self.ledger.reopen_with_evidence(
                key, reason="still open", owner="triage", readback=self.receipt()
            )

    def test_a_replayed_reopen_cannot_revoke_a_new_worker(self):
        key = self.complete()
        self.now += 10
        evidence = self.receipt("reversed")
        self.ledger.reopen_with_evidence(
            key, reason="reversed", owner="triage", readback=evidence
        )
        case = self.claim_one()
        with self.assertRaises(InvalidTransition):
            self.ledger.reopen_with_evidence(
                key, reason="duplicate delivery", owner="triage", readback=evidence
            )
        self.assertEqual(case["lease_token"], self.ledger.get(key)["lease_token"])

    def test_reopening_validates_its_arguments(self):
        key = self.complete()
        self.now += 10
        for kwargs in (
            {"reason": " ", "owner": "triage", "readback": self.receipt()},
            {"reason": "ok", "owner": "", "readback": self.receipt()},
            {"reason": "ok", "owner": "triage", "readback": "not a readback"},
        ):
            with self.assertRaises(ValidationError):
                self.ledger.reopen_with_evidence(key, **kwargs)


if __name__ == "__main__":
    unittest.main()
