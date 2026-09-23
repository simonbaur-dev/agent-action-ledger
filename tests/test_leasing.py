"""Leases are what make more than one worker safe."""
from __future__ import annotations

import concurrent.futures
import threading
import unittest

from agent_action_ledger import ActionLedger, LeaseLost, ValidationError

from .support import KIND, NAMESPACE, LedgerTestCase


class LeasingTests(LedgerTestCase):
    max_concurrent_leases = 2

    def test_concurrent_claims_never_overlap(self):
        for index in range(6):
            self.register(f"S-{index}")
        barrier = threading.Barrier(2)
        now = self.now

        def take(_):
            ledger = ActionLedger(self.path, clock=lambda: now)
            try:
                barrier.wait(timeout=10)
                return [case["id"] for case in ledger.claim(worker="racer", limit=2)]
            finally:
                ledger.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(take, range(2)))
        ids = results[0] + results[1]
        self.assertEqual(2, len(ids), "the shared ceiling was exceeded")
        self.assertEqual(2, len(set(ids)), "the same case was handed to two workers")

    def test_ceiling_is_shared_between_workers(self):
        for index in range(4):
            self.register(f"S-{index}")
        first = self.ledger.claim(worker="a", limit=2)
        self.assertEqual(2, len(first))
        other = self.open_ledger()
        self.addCleanup(other.close)
        self.assertEqual([], other.claim(worker="b", limit=2))

    def test_lease_records_its_holder_and_deadline(self):
        self.register("S-1")
        case = self.ledger.claim(worker="worker-7", limit=1, lease_seconds=120)[0]
        self.assertEqual("worker-7", case["lease_owner"])
        self.assertEqual(self.now + 120, case["lease_until"])
        self.assertEqual(1, case["attempts"])

    def test_expired_lease_parks_the_case_then_recovers_it(self):
        key = self.register("S-1")
        old = self.claim_one()

        self.now = 1601.0  # the 600 second lease has run out
        self.assertEqual([], self.ledger.claim(worker="next", limit=1),
                         "a case must not be handed on the instant its lease dies")
        parked = self.ledger.get(key)
        self.assertEqual("waiting", parked["status"])
        self.assertIsNone(parked["lease_token"])
        self.assertEqual(1901.0, parked["next_check"])
        self.assertIn("lease_expired", [e["kind"] for e in self.ledger.history(key)])

        self.now = 1901.0
        new = self.claim_one()
        self.assertNotEqual(old["lease_token"], new["lease_token"])

    def test_the_old_worker_is_fenced_out_after_recovery(self):
        key = self.register("S-1")
        old = self.claim_one()
        self.now = 1901.0
        self.ledger.claim(worker="next", limit=1)  # parks it
        self.now = 2300.0
        self.ledger.claim(worker="next", limit=1)  # takes it
        for call in (
            lambda: self.ledger.finish(
                key, old["lease_token"], status="complete",
                reason="stale worker", readback=self.receipt(),
            ),
            lambda: self.ledger.transient_retry(key, old["lease_token"]),
            lambda: self.ledger.renew(key, old["lease_token"]),
        ):
            with self.assertRaises(LeaseLost):
                call()

    def test_renewing_extends_a_live_lease(self):
        key = self.register("S-1")
        case = self.ledger.claim(worker="a", limit=1, lease_seconds=60)[0]
        self.now += 30
        deadline = self.ledger.renew(key, case["lease_token"], lease_seconds=60)
        self.assertEqual(self.now + 60, deadline)
        self.assertEqual(deadline, self.ledger.get(key)["lease_until"])

    def test_one_transient_retry_per_lease(self):
        key = self.register("S-1")
        case = self.claim_one()
        self.assertTrue(self.ledger.transient_retry(key, case["lease_token"]))
        self.assertFalse(self.ledger.transient_retry(key, case["lease_token"]))
        # A fresh lease gets a fresh retry.
        self.ledger.finish(
            key, case["lease_token"], status="pending",
            reason="retry later", next_check=self.now + 10,
        )
        self.now += 11
        again = self.claim_one()
        self.assertTrue(self.ledger.transient_retry(key, again["lease_token"]))

    def test_an_empty_or_wrong_token_is_never_accepted(self):
        key = self.register("S-1")
        self.claim_one()
        for token in ("", None, "deadbeef"):
            with self.assertRaises(LeaseLost):
                self.ledger.transient_retry(key, token)

    def test_claims_can_be_filtered(self):
        self.ledger.register("alpha", "ticket", "A", owner="o", payload={})
        self.ledger.register("beta", "invoice", "B", owner="o", payload={})
        taken = self.ledger.claim(worker="a", limit=2, kinds=["invoice"])
        self.assertEqual(["invoice"], [case["kind"] for case in taken])

        other = self.open_ledger()
        self.addCleanup(other.close)
        taken = other.claim(worker="b", limit=2, namespaces=["beta"])
        self.assertEqual([], taken, "the only beta case is already leased")

    def test_human_decision_is_parked_by_default_and_can_be_explicitly_claimed(self):
        key = self.register("S-1")
        case = self.claim_one()
        self.ledger.finish(
            case["id"], case["lease_token"], status="human_decision",
            reason="ambiguous", next_check=self.now + 1,
            question="Which record is meant?",
        )
        self.now += 2
        self.assertEqual([], self.ledger.claim(worker="a", limit=2))
        self.assertEqual(
            [key], [c["id"] for c in self.ledger.claim(
                worker="a", limit=2,
                statuses=["pending", "waiting", "human_decision"],
            )]
        )

    def test_exclusive_group_serialises_a_namespace(self):
        self.ledger.register(NAMESPACE, KIND, "S-1", owner="o", payload={})
        self.ledger.register(NAMESPACE, KIND, "S-2", owner="o", payload={})
        taken = self.ledger.claim(worker="a", limit=2, exclusive_group="namespace")
        self.assertEqual(1, len(taken))

        other = self.open_ledger()
        self.addCleanup(other.close)
        self.assertEqual(
            [], other.claim(worker="b", limit=1, exclusive_group="namespace")
        )

    def test_claim_arguments_are_validated(self):
        for kwargs in (
            {"limit": 0},
            {"limit": "two"},
            {"lease_seconds": 0},
            {"lease_seconds": 10 ** 9},
            {"kinds": []},
            {"kinds": "ticket"},
            {"namespaces": ["Bad Namespace"]},
            {"statuses": ["nonsense"]},
            {"statuses": ["complete"]},
            {"exclusive_group": "colour"},
            {"worker": "  "},
        ):
            with self.assertRaises(ValidationError, msg=f"accepted {kwargs}"):
                self.ledger.claim(**kwargs)

    def test_completed_cases_are_never_claimed(self):
        key = self.register("S-1")
        case = self.claim_one()
        self.ledger.finish(
            case["id"], case["lease_token"], status="complete",
            reason="done", readback=self.receipt(),
        )
        self.now += 10_000
        self.assertEqual([], self.ledger.claim(worker="a", limit=2))
        self.assertIsNone(self.ledger.get(key)["next_check"])


if __name__ == "__main__":
    unittest.main()
