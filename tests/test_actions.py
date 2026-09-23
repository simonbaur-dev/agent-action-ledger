"""Action fingerprints, receipts, the write gate and crash recovery."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest

from agent_action_ledger import (
    ActionConflict,
    ActionLedger,
    ValidationError,
    WritesDisarmed,
)

from .support import LedgerTestCase, src_dir

REQUEST = {"template": "reset-link", "to": "record-1"}


class WriteGateTests(LedgerTestCase):
    def test_a_new_ledger_is_disarmed(self):
        self.assertFalse(self.ledger.writes_armed())

    def test_a_disarmed_ledger_refuses_every_action(self):
        key = self.register("S-1")
        case = self.claim_one()
        with self.assertRaises(WritesDisarmed):
            self.ledger.begin_action(
                key, case["lease_token"], "effect:1", kind="reply", request=REQUEST
            )
        self.assertIsNone(self.ledger.action("effect:1"))

    def test_arming_is_recorded_with_its_note(self):
        self.ledger.arm_writes("operator verified there is nothing outstanding")
        self.assertTrue(self.ledger.writes_armed())
        self.ledger.disarm_writes("maintenance window")
        self.assertFalse(self.ledger.writes_armed())
        history = self.ledger.write_gate_history()
        self.assertEqual([False, True, False], [entry["armed"] for entry in history])
        self.assertIn("operator verified", history[1]["note"])

    def test_arming_needs_a_note(self):
        with self.assertRaises(ValidationError):
            self.ledger.arm_writes("  ")
        with self.assertRaises(ValidationError):
            self.ledger.disarm_writes("")

    def test_arming_with_unsettled_attempts_says_so_on_the_record(self):
        key = self.register("S-1")
        self.ledger.arm_writes("first arming")
        case = self.claim_one()
        self.ledger.begin_action(
            key, case["lease_token"], "effect:1", kind="reply", request=REQUEST
        )
        self.ledger.disarm_writes("stopping to investigate")
        self.ledger.arm_writes("resuming")
        self.assertIn("unsettled actions: 1", self.ledger.write_gate_history()[-1]["note"])

    def test_the_write_gate_log_cannot_be_rewritten(self):
        self.ledger.arm_writes("on the record")
        with self.assertRaises(Exception):
            self.ledger.db.execute("DELETE FROM gate_log")
        with self.assertRaises(Exception):
            self.ledger.db.execute("UPDATE gate_log SET note='nothing to see here'")


class ActionTests(LedgerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.key = self.register("S-1")
        self.ledger.arm_writes("test fixture")
        self.case = self.claim_one()
        self.token = self.case["lease_token"]

    def begin(self, request=None, key="effect:1", kind="reply") -> str:
        return self.ledger.begin_action(
            self.key, self.token, key, kind=kind,
            request=REQUEST if request is None else request,
        )

    def test_the_same_action_is_only_ever_executed_once(self):
        self.assertEqual("execute", self.begin())
        self.assertEqual("reconcile", self.begin())
        self.ledger.confirm_action(
            self.key, self.token, "effect:1", self.receipt("the reply exists")
        )
        self.assertEqual("confirmed", self.begin())

    def test_an_unsettled_attempt_is_never_replayed_automatically(self):
        self.begin()
        self.assertEqual("attempted", self.ledger.action("effect:1")["status"])
        self.assertEqual(
            ["effect:1"], [a["key"] for a in self.ledger.unsettled_actions()]
        )
        self.assertEqual("reconcile", self.begin())
        self.assertEqual(1, self.ledger.action("effect:1")["attempts"])

    def test_reusing_a_key_for_a_different_request_is_refused(self):
        self.begin()
        with self.assertRaises(ActionConflict):
            self.begin(request={"template": "something-else"})
        with self.assertRaises(ActionConflict):
            self.begin(kind="delete")

    def test_an_invalid_action_identity_cannot_poison_the_ledger(self):
        for action_key, kind in (("", "reply"), ("   ", "reply"), ("ok", ""), ("ok", None)):
            with self.assertRaises(ValidationError):
                self.ledger.begin_action(
                    self.key, self.token, action_key, kind=kind, request=REQUEST
                )
        self.assertEqual(0, self.ledger.stats()["actions"])
        # The ledger is still openable and still hands out work.
        self.ledger.validate()

    def test_an_unencodable_request_is_refused(self):
        with self.assertRaises(ValidationError):
            self.begin(request={"amount": float("nan")})
        with self.assertRaises(ValidationError):
            self.begin(request=object())

    def test_a_receipt_must_be_a_read_back_taken_after_the_attempt(self):
        self.begin()
        with self.assertRaises(ValidationError):
            self.ledger.confirm_action(self.key, self.token, "effect:1", "looks done")
        with self.assertRaises(ValidationError):
            self.ledger.confirm_action(
                self.key, self.token, "effect:1", self.receipt(at=self.now - 1)
            )
        with self.assertRaises(ValidationError):
            self.ledger.confirm_action(
                self.key, self.token, "unknown-key", self.receipt()
            )
        self.assertEqual("attempted", self.ledger.action("effect:1")["status"])

    def test_a_confirmed_receipt_is_stored_and_readable(self):
        self.begin()
        self.ledger.confirm_action(
            self.key, self.token, "effect:1", self.receipt("the reply exists")
        )
        record = self.ledger.action("effect:1")
        self.assertEqual("confirmed", record["status"])
        self.assertEqual("the reply exists", record["receipt"]["assertion"])
        self.assertEqual("fake-system", record["receipt"]["source"])
        self.assertEqual([], self.ledger.unsettled_actions())

    def test_an_absent_effect_can_be_recorded_and_then_retried(self):
        self.begin()
        self.ledger.record_action_absent(
            self.key, self.token, "effect:1",
            self.receipt("no such reply exists on the record"),
        )
        self.assertEqual("not_performed", self.ledger.action("effect:1")["status"])
        self.assertEqual("execute", self.begin())
        record = self.ledger.action("effect:1")
        self.assertEqual("attempted", record["status"])
        self.assertEqual(2, record["attempts"])
        self.assertIn("action_absent", [e["kind"] for e in self.ledger.history(self.key)])
        self.assertIn("action_retried", [e["kind"] for e in self.ledger.history(self.key)])

    def test_actions_need_a_live_lease(self):
        from agent_action_ledger import LeaseLost

        self.now += 601  # the lease has expired
        with self.assertRaises(LeaseLost):
            self.begin()
        with self.assertRaises(LeaseLost):
            self.ledger.confirm_action(self.key, self.token, "effect:1", self.receipt())

    def test_history_records_every_step(self):
        self.begin()
        self.ledger.confirm_action(self.key, self.token, "effect:1", self.receipt())
        self.ledger.finish(
            self.key, self.token, status="complete",
            reason="verified", readback=self.receipt(),
        )
        self.assertEqual(
            ["opened", "claimed", "action_attempted", "action_confirmed", "complete"],
            [event["kind"] for event in self.ledger.history(self.key)],
        )

    def test_history_is_append_only(self):
        self.begin()
        with self.assertRaises(Exception):
            self.ledger.db.execute("DELETE FROM events")
        with self.assertRaises(Exception):
            self.ledger.db.execute("UPDATE events SET kind='rewritten'")
        with self.assertRaises(Exception):
            self.ledger.db.execute("DELETE FROM actions")


class CrashRecoveryTests(LedgerTestCase):
    def test_an_attempt_survives_the_death_of_its_process(self):
        """A worker that dies mid-call leaves an attempt nobody may replay."""
        child_ledger = self.dir / "child.sqlite"
        code = """
import os, sys
from agent_action_ledger import ActionLedger
ledger = ActionLedger(sys.argv[1], clock=lambda: 1000.0)
key = ledger.register('demo', 'ticket', 'S-1', owner='triage', payload={})
ledger.arm_writes('synthetic activation')
case = ledger.claim(worker='doomed', limit=1)[0]
assert ledger.begin_action(
    key, case['lease_token'], 'effect:1', kind='reply', request={}
) == 'execute'
os._exit(0)          # die exactly where a real crash hurts most
"""
        env = dict(os.environ, PYTHONPATH=src_dir())
        subprocess.run(
            [sys.executable, "-c", code, str(child_ledger)],
            env=env, check=True, timeout=60, capture_output=True,
        )

        now = [1601.0]
        ledger = ActionLedger(child_ledger, clock=lambda: now[0])
        self.addCleanup(ledger.close)
        self.assertEqual("attempted", ledger.action("effect:1")["status"])

        # The case is not handed straight on: recovery pauses first.
        self.assertEqual([], ledger.claim(worker="successor", limit=1))
        now[0] = 2201.0
        case = ledger.claim(worker="successor", limit=1)[0]
        self.assertEqual(
            "reconcile",
            ledger.begin_action(
                case["id"], case["lease_token"], "effect:1", kind="reply", request={}
            ),
        )

    def test_a_crashed_process_leaves_a_usable_ledger(self):
        child_ledger = self.dir / "child2.sqlite"
        code = """
import os, sys
from agent_action_ledger import ActionLedger
ledger = ActionLedger(sys.argv[1], clock=lambda: 1000.0)
for index in range(5):
    ledger.register('demo', 'ticket', 'S-%d' % index, owner='triage', payload={'i': index})
os._exit(0)
"""
        env = dict(os.environ, PYTHONPATH=src_dir())
        subprocess.run(
            [sys.executable, "-c", code, str(child_ledger)],
            env=env, check=True, timeout=60, capture_output=True,
        )
        ledger = ActionLedger(child_ledger, clock=lambda: 1000.0)
        self.addCleanup(ledger.close)
        ledger.validate()
        self.assertEqual(5, ledger.stats()["cases"])


class TransactionTests(LedgerTestCase):
    def test_a_failed_block_leaves_nothing_behind(self):
        key = self.register("S-1")
        before = len(self.ledger.history(key))
        with self.assertRaises(RuntimeError):
            with self.ledger.transaction():
                self.ledger._event(key, "half-written", {})
                self.ledger.db.execute(
                    "UPDATE cases SET owner='rewritten' WHERE id=?", (key,)
                )
                raise RuntimeError("something went wrong mid-way")
        self.assertEqual(before, len(self.ledger.history(key)))
        self.assertEqual("triage", self.ledger.get(key)["owner"])

    def test_a_constraint_violation_rolls_the_whole_block_back(self):
        key = self.register("S-1")
        before = len(self.ledger.history(key))
        with self.assertRaises(Exception):
            with self.ledger.transaction():
                self.ledger._event(key, "about to break", {})
                self.ledger.db.execute(
                    "UPDATE cases SET status='nonsense' WHERE id=?", (key,)
                )
        self.assertEqual(before, len(self.ledger.history(key)))
        self.assertEqual("pending", self.ledger.get(key)["status"])

    def test_the_schema_refuses_an_impossible_case(self):
        key = self.register("S-1")
        with self.assertRaises(Exception):
            self.ledger.db.execute(
                "UPDATE cases SET status='complete' WHERE id=?", (key,)
            )  # complete cases may not keep a next_check

    def test_a_damaged_row_fails_the_ledger_closed(self):
        key = self.register("S-1")
        # Simulate a file damaged by something that bypassed the constraints.
        self.ledger.db.execute("PRAGMA ignore_check_constraints=ON")
        self.ledger.db.execute(
            "UPDATE cases SET owner='', payload='not json' WHERE id=?", (key,)
        )
        with self.assertRaises(Exception):
            self.ledger.claim(worker="a", limit=1)
        with self.assertRaises(Exception):
            ActionLedger(self.path, clock=lambda: self.now)


class CorruptFileTests(unittest.TestCase):
    def test_empty_corrupt_and_unrelated_files_fail_closed(self):
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in (("empty.sqlite", b""), ("corrupt.sqlite", b"not sqlite")):
                target = root / name
                target.write_bytes(data)
                with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                    ActionLedger(target)
                # Nothing was repaired, truncated or overwritten.
                self.assertEqual(data, target.read_bytes())

            unrelated = root / "unrelated.sqlite"
            conn = sqlite3.connect(unrelated)
            conn.execute("CREATE TABLE something_else (x)")
            conn.close()
            with self.assertRaises(ValueError):
                ActionLedger(unrelated)


if __name__ == "__main__":
    unittest.main()
