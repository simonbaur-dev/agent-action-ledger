"""Snapshots are backups of an actor's state, so they come back disarmed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import unittest

from agent_action_ledger import (
    ActionLedger,
    LeaseLost,
    ValidationError,
    manifest_path,
    verify_snapshot,
)

from .support import LedgerTestCase


class SnapshotTests(LedgerTestCase):
    def prepare(self) -> str:
        """A ledger with one completed case, one parked case and one receipt."""
        key = self.register("S-1")
        self.register("S-2")
        self.ledger.arm_writes("test fixture")
        case = self.claim_one()
        self.ledger.begin_action(
            case["id"], case["lease_token"], "effect:1", kind="reply", request={"x": 1}
        )
        self.ledger.confirm_action(
            case["id"], case["lease_token"], "effect:1", self.receipt("it exists")
        )
        self.ledger.finish(
            case["id"], case["lease_token"], status="complete",
            reason="verified", readback=self.receipt(),
        )
        return key

    def test_a_snapshot_is_consistent_disarmed_and_parked(self):
        self.prepare()
        leased = self.ledger.claim(worker="mid-flight", limit=1)[0]
        destination = self.dir / "snapshots" / "ledger.sqlite"
        manifest = self.ledger.snapshot(destination)

        self.assertTrue(destination.exists())
        self.assertFalse(manifest["writes_armed"])
        self.assertTrue(self.ledger.writes_armed(), "the live ledger is untouched")

        restored = ActionLedger(destination, clock=lambda: self.now)
        self.addCleanup(restored.close)
        self.assertEqual("ok", restored.db.execute("PRAGMA quick_check").fetchone()[0])
        self.assertFalse(restored.writes_armed())

        parked = restored.get(leased["id"])
        self.assertEqual("waiting", parked["status"])
        self.assertIsNone(parked["lease_token"])
        self.assertEqual(self.now + 300, parked["next_check"])
        self.assertIn("reconcile", parked["reason"])

        # Receipts survive: they are the only thing that tells a person what
        # the outside world actually looked like.
        self.assertEqual("confirmed", restored.action("effect:1")["status"])
        self.assertEqual("it exists", restored.action("effect:1")["receipt"]["assertion"])

    def test_a_restored_snapshot_fences_out_the_old_workers(self):
        key = self.register("S-1")
        case = self.claim_one()
        destination = self.dir / "snapshots" / "ledger.sqlite"
        self.ledger.snapshot(destination)
        restored = ActionLedger(destination, clock=lambda: self.now)
        self.addCleanup(restored.close)
        for call in (
            lambda: restored.finish(
                key, case["lease_token"], status="complete",
                reason="old worker", readback=self.receipt(),
            ),
            lambda: restored.transient_retry(key, case["lease_token"]),
            lambda: restored.renew(key, case["lease_token"]),
        ):
            with self.assertRaises(LeaseLost):
                call()

    def test_a_snapshot_never_overwrites_the_live_ledger(self):
        self.register("S-1")
        with self.assertRaises(ValidationError):
            self.ledger.snapshot(self.path)

    def test_a_human_decision_stays_a_human_decision(self):
        key = self.register("S-1")
        case = self.claim_one()
        self.ledger.finish(
            case["id"], case["lease_token"], status="human_decision",
            reason="ambiguous", next_check=self.now + 60,
            question="Which record is meant?",
        )
        destination = self.dir / "snap.sqlite"
        self.ledger.snapshot(destination)
        restored = ActionLedger(destination, clock=lambda: self.now)
        self.addCleanup(restored.close)
        self.assertEqual("human_decision", restored.get(key)["status"])
        self.assertEqual("Which record is meant?", restored.get(key)["question"])

    def test_the_snapshot_is_written_privately(self):
        self.register("S-1")
        destination = self.dir / "public" / "snap.sqlite"
        self.ledger.snapshot(destination)
        if os.name != "nt":
            self.assertEqual(0o600, destination.stat().st_mode & 0o777)
        # No staging directory or half-written file is left behind.
        leftovers = {entry.name for entry in destination.parent.iterdir()}
        self.assertIn("snap.sqlite", leftovers)
        self.assertIn("snap.sqlite.manifest.json", leftovers)
        self.assertEqual(
            [],
            [
                name
                for name in leftovers
                if name.startswith(".ledger-snapshot-") or name.endswith(".tmp")
            ],
        )


class VerificationTests(LedgerTestCase):
    def snapshot(self) -> Path:
        self.register("S-1")
        self.register("S-2")
        destination = self.dir / "snapshots" / "ledger.sqlite"
        self.ledger.snapshot(destination)
        return destination

    def test_a_good_snapshot_verifies(self):
        report = verify_snapshot(self.snapshot())
        self.assertTrue(report["ok"], report)
        self.assertEqual(2, report["cases"])
        self.assertEqual(0, report["unsettled_actions"])
        self.assertFalse(report["writes_armed"])
        self.assertEqual(64, len(report["sha256"]))

    def test_the_verifier_can_fail(self):
        """A check whose negative result is indistinguishable from 'it never
        ran' proves nothing, so prove this one detects each kind of damage."""
        destination = self.snapshot()

        self.assertFalse(verify_snapshot(self.dir / "absent.sqlite")["ok"])

        # 1. Tampered bytes.
        with open(destination, "r+b") as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(b"\x00")
        report = verify_snapshot(destination)
        self.assertFalse(report["ok"])
        self.assertIn("digest", report["reason"])

        # 2. Missing manifest.
        destination = self.dir / "s2" / "ledger.sqlite"
        self.ledger.snapshot(destination)
        manifest_path(destination).unlink()
        self.assertIn("manifest", verify_snapshot(destination)["reason"])

        # 3. Manifest that does not describe a disarmed snapshot.
        destination = self.dir / "s3" / "ledger.sqlite"
        manifest = self.ledger.snapshot(destination)
        manifest["writes_armed"] = True
        manifest_path(destination).write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("manifest schema", verify_snapshot(destination)["reason"])

        # 4. A snapshot someone armed, with a manifest rewritten to match.
        destination = self.dir / "s4" / "ledger.sqlite"
        manifest = self.ledger.snapshot(destination)
        conn = sqlite3.connect(destination)
        conn.execute("UPDATE meta SET value='1' WHERE key='writes_armed'")
        conn.commit()
        conn.close()
        import hashlib

        manifest["sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        manifest_path(destination).write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual("snapshot is not disarmed", verify_snapshot(destination)["reason"])

        # 5. Not a ledger at all.
        destination = self.dir / "s5" / "ledger.sqlite"
        destination.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(destination)
        conn.execute("CREATE TABLE unrelated (x)")
        conn.close()
        manifest["sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        manifest_path(destination).write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual("not an agent action ledger", verify_snapshot(destination)["reason"])

    def test_an_oversized_snapshot_is_reported_rather_than_read(self):
        from agent_action_ledger import snapshot as snapshot_module

        destination = self.snapshot()
        original = snapshot_module.MAX_VERIFIED_SNAPSHOT_BYTES
        snapshot_module.MAX_VERIFIED_SNAPSHOT_BYTES = 1
        try:
            report = snapshot_module.verify_snapshot(destination)
        finally:
            snapshot_module.MAX_VERIFIED_SNAPSHOT_BYTES = original
        self.assertFalse(report["ok"])
        self.assertIn("bound", report["reason"])


if __name__ == "__main__":
    unittest.main()
