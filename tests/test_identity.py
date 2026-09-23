"""Case identity has to be stable, or nothing built on it is idempotent."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from agent_action_ledger import ValidationError, case_id, encode_json, fingerprint

from .support import KIND, NAMESPACE, LedgerTestCase, src_dir


class IdentityTests(unittest.TestCase):
    def test_identity_is_deterministic_and_prefixed(self):
        first = case_id("support", "ticket", "T-1")
        self.assertEqual(first, case_id("support", "ticket", "T-1"))
        self.assertTrue(first.startswith("support:ticket:"))
        self.assertEqual(len(first.rsplit(":", 1)[1]), 20)

    def test_identity_separates_every_component(self):
        ids = {
            case_id("support", "ticket", "T-1"),
            case_id("support", "ticket", "T-2"),
            case_id("support", "invoice", "T-1"),
            case_id("billing", "ticket", "T-1"),
        }
        self.assertEqual(4, len(ids))

    def test_identity_is_stable_across_processes(self):
        """Different interpreter, different hash seed, same identity."""
        code = (
            "from agent_action_ledger import case_id;"
            "print(case_id('support', 'ticket', 'T-1'))"
        )
        env = dict(os.environ, PYTHONPATH=src_dir(), PYTHONHASHSEED="1")
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(case_id("support", "ticket", "T-1"), result.stdout.strip())

    def test_slugs_are_restricted(self):
        for namespace in ("", "Support", "support namespace", "x" * 65, "-lead", None, 7):
            with self.assertRaises((ValueError, TypeError)):
                case_id(namespace, "ticket", "T-1")
        for source in ("", "   ", None):
            with self.assertRaises((ValueError, TypeError)):
                case_id("support", "ticket", source)

    def test_canonical_encoding_is_order_independent(self):
        self.assertEqual(encode_json({"a": 1, "b": 2}), encode_json({"b": 2, "a": 1}))
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": "1"}))
        self.assertEqual(64, len(fingerprint({"a": 1})))

    def test_canonical_encoding_refuses_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            encode_json({"amount": float("nan")})
        with self.assertRaises(ValueError):
            encode_json({"amount": float("inf")})

    def test_unicode_survives_encoding(self):
        self.assertIn("ü", encode_json({"note": "Rückfrage"}))


class IdentityInLedgerTests(LedgerTestCase):
    def test_registering_the_same_source_twice_yields_one_case(self):
        first = self.register("S-1")
        self.assertEqual(first, self.register("S-1"))
        self.assertEqual(1, self.ledger.stats()["cases"])

    def test_identity_does_not_depend_on_owner_or_payload(self):
        first = self.ledger.register(
            NAMESPACE, KIND, "S-9", owner="alice", payload={"v": 1}
        )
        second = self.ledger.register(
            NAMESPACE, KIND, "S-9", owner="bob", payload={"v": 2}
        )
        self.assertEqual(first, second)

    def test_stored_identity_matches_its_components(self):
        key = self.register("S-1")
        row = self.ledger.get(key)
        self.assertEqual(key, case_id(row["namespace"], row["kind"], row["source_id"]))

    def test_register_rejects_bad_arguments(self):
        with self.assertRaises(ValidationError):
            self.ledger.register(NAMESPACE, KIND, "S-1", owner="  ", payload={})
        with self.assertRaises(ValidationError):
            self.ledger.register(NAMESPACE, KIND, "S-1", owner="x", payload=["not a dict"])
        with self.assertRaises(ValidationError):
            self.ledger.register("BAD NAMESPACE", KIND, "S-1", owner="x", payload={})


if __name__ == "__main__":
    unittest.main()
