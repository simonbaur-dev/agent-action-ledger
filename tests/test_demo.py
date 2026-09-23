"""The example has to keep working, and has to keep telling the truth."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_action_ledger import ActionLedger
from agent_action_ledger.demo import FakeSupportDesk, readback_for_reply, run


class DemoTests(unittest.TestCase):
    def test_the_example_runs_and_leaves_a_verifiable_ledger(self):
        lines = []
        with tempfile.TemporaryDirectory(prefix="agent-ledger-demo-test-") as tmp:
            result = run(Path(tmp), echo=lines.append)

            self.assertTrue(result["verification"]["ok"], result["verification"])
            self.assertEqual(0, result["stats"]["unsettled_actions"])
            self.assertGreaterEqual(result["stats"]["cases"], 3)

            # The reply was posted exactly once, however many times the ledger
            # was asked about it.
            writes = [c for c in result["desk_calls"] if c.startswith("post_reply")]
            self.assertEqual(1, len(writes))

            ledger = ActionLedger(result["path"])
            try:
                ledger.validate()
                self.assertEqual(1, len(ledger.cases(status="complete")))
            finally:
                ledger.close()

        joined = "\n".join(lines)
        self.assertIn("refused as designed", joined)
        self.assertIn("(no second call is made)", joined)

    def test_the_example_cleans_up_after_itself_by_default(self):
        result = run(echo=lambda _line: None)
        self.assertFalse(Path(result["path"]).exists())

    def test_the_adapter_reports_absence_rather_than_guessing(self):
        desk = FakeSupportDesk()
        self.assertIsNone(readback_for_reply(desk, "T-1001", "R-does-not-exist"))
        reply_id = desk.post_reply("T-1001", "hello")
        evidence = readback_for_reply(desk, "T-1001", reply_id)
        self.assertIsNotNone(evidence)
        self.assertEqual(reply_id, evidence.record_id)
        self.assertEqual("support-desk", evidence.source)


if __name__ == "__main__":
    unittest.main()
