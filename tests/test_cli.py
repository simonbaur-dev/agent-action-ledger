"""The command line is the operator's surface; it must not be able to lie."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent_action_ledger import ActionLedger, Readback
from agent_action_ledger.cli import main


def run(*argv):
    """Run the CLI, returning (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(arg) for arg in argv])
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-ledger-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "ledger.sqlite"

    def test_init_then_status(self):
        code, out, _ = run("init", self.path, "--json")
        self.assertEqual(0, code)
        created = json.loads(out)
        self.assertTrue(created["created"])
        self.assertFalse(created["writes_armed"])

        code, out, _ = run("status", self.path, "--json")
        self.assertEqual(0, code)
        stats = json.loads(out)
        self.assertEqual(0, stats["cases"])
        self.assertFalse(stats["writes_armed"])

    def test_register_list_show_and_history(self):
        run("init", self.path)
        code, out, _ = run(
            "register", self.path, "support", "ticket", "T-1",
            "--owner", "triage", "--payload", '{"subject": "hello"}',
        )
        self.assertEqual(0, code)
        case_id = out.strip()
        self.assertTrue(case_id.startswith("support:ticket:"))

        code, out, _ = run("list", self.path, "--json")
        self.assertEqual([case_id], [case["id"] for case in json.loads(out)])

        code, out, _ = run("show", self.path, case_id)
        self.assertEqual(0, code)
        self.assertIn("support/ticket/T-1", out)
        self.assertIn("opened", out)

        code, out, _ = run("history", self.path, case_id, "--json")
        self.assertEqual(["opened"], [event["kind"] for event in json.loads(out)])

    def test_list_filters_and_empty_result(self):
        run("init", self.path)
        run("register", self.path, "support", "ticket", "T-1", "--owner", "triage")
        code, out, _ = run("list", self.path, "--kind", "invoice")
        self.assertEqual(0, code)
        self.assertIn("no cases match", out)

    def test_arm_disarm_and_gate_log(self):
        run("init", self.path)
        code, _, _ = run("arm", self.path, "--note", "verified nothing outstanding")
        self.assertEqual(0, code)
        code, out, _ = run("status", self.path, "--json")
        self.assertTrue(json.loads(out)["writes_armed"])

        code, _, _ = run("disarm", self.path, "--note", "end of window")
        self.assertEqual(0, code)
        code, out, _ = run("gate-log", self.path, "--json")
        self.assertEqual([False, True, False], [e["armed"] for e in json.loads(out)])

    def test_arm_refuses_while_an_attempt_is_unsettled(self):
        run("init", self.path)
        ledger = ActionLedger(self.path)
        try:
            key = ledger.register(
                "support", "ticket", "T-1", owner="triage", payload={}
            )
            ledger.arm_writes("fixture")
            case = ledger.claim(worker="w", limit=1)[0]
            ledger.begin_action(
                key, case["lease_token"], "effect:1", kind="reply", request={}
            )
            ledger.disarm_writes("stopping")
        finally:
            ledger.close()

        code, _, err = run("arm", self.path, "--note", "resume please")
        self.assertEqual(2, code)
        self.assertIn("effect:1", err)
        code, out, _ = run("status", self.path, "--json")
        self.assertFalse(json.loads(out)["writes_armed"])

        code, _, _ = run("arm", self.path, "--note", "reconciled by hand", "--force")
        self.assertEqual(0, code)
        code, out, _ = run("status", self.path, "--json")
        self.assertTrue(json.loads(out)["writes_armed"])

    def test_resume_a_parked_case(self):
        run("init", self.path)
        ledger = ActionLedger(self.path)
        try:
            key = ledger.register("support", "ticket", "T-1", owner="triage", payload={})
            case = ledger.claim(worker="w", limit=1)[0]
            import time

            ledger.finish(
                key, case["lease_token"], status="human_decision",
                reason="ambiguous", next_check=time.time() + 3600,
                question="Which record?",
            )
        finally:
            ledger.close()
        code, out, _ = run(
            "resume", self.path, key, "--reason", "answered", "--answer", "the first"
        )
        self.assertEqual(0, code)
        self.assertIn("pending", out)

    def test_snapshot_and_verify(self):
        run("init", self.path)
        run("register", self.path, "support", "ticket", "T-1", "--owner", "triage")
        destination = self.dir / "snapshots" / "ledger.sqlite"
        code, out, _ = run("snapshot", self.path, destination)
        self.assertEqual(0, code)
        self.assertIn("disarmed", out)

        code, out, _ = run("verify", destination, "--json")
        self.assertEqual(0, code)
        self.assertTrue(json.loads(out)["ok"])

        code, _, err = run("verify", self.dir / "missing.sqlite")
        self.assertEqual(1, code)
        self.assertIn("NOT VERIFIED", err)

    def test_errors_are_reported_not_raised(self):
        run("init", self.path)
        code, _, err = run("show", self.path, "support:ticket:nope")
        self.assertEqual(1, code)
        self.assertIn("UnknownCase", err)

        code, _, err = run(
            "register", self.path, "BAD", "ticket", "T-1", "--owner", "triage"
        )
        self.assertEqual(1, code)
        self.assertIn("namespace", err)

    def test_there_is_no_command_that_manufactures_a_receipt(self):
        """Evidence has to come from an adapter that looked, not from a shell."""
        from agent_action_ledger.cli import build_parser

        parser = build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(getattr(action, "choices", None), dict)
        ]
        commands = set(subparsers[0].choices)
        self.assertIn("status", commands)
        self.assertEqual(
            set(), commands & {"confirm", "confirm-action", "complete", "act"}
        )

    def test_example_runs_end_to_end(self):
        directory = self.dir / "demo"
        code, out, _ = run("example", "--directory", directory)
        self.assertEqual(0, code)
        self.assertIn("refused as designed", out)
        self.assertIn("verify -> ok=True", out)
        self.assertTrue((directory / "ledger.sqlite").exists())
        self.assertTrue((directory / "snapshots" / "ledger.sqlite").exists())


class ReadbackHelperTests(unittest.TestCase):
    def test_readback_round_trips_through_its_dict_form(self):
        original = Readback("system", "REC-1", 1000.0, "it exists")
        self.assertEqual(original, Readback.from_dict(original.as_dict()))


if __name__ == "__main__":
    unittest.main()
