"""Shared fixtures: a controllable clock and a ledger in a temporary directory."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from agent_action_ledger import ActionLedger, Readback

NAMESPACE = "demo"
KIND = "ticket"


def src_dir() -> str:
    """Absolute path of ``src/``, for subprocesses that need the package."""
    import agent_action_ledger

    return str(Path(agent_action_ledger.__file__).resolve().parent.parent)


class LedgerTestCase(unittest.TestCase):
    """A ledger at a fixed, movable point in time."""

    max_concurrent_leases = 4

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-ledger-tests-")
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "ledger.sqlite"
        self.now = 1000.0
        self.ledger = self.open_ledger(create=True)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.ledger.close)

    def open_ledger(self, *, create: bool = False, **kwargs) -> ActionLedger:
        if create:
            kwargs.setdefault("max_concurrent_leases", self.max_concurrent_leases)
        return ActionLedger(self.path, clock=lambda: self.now, **kwargs)

    # -- helpers ------------------------------------------------------

    def register(self, source: str = "S-1", *, namespace: str = NAMESPACE,
                 kind: str = KIND, owner: str = "triage", **payload) -> str:
        return self.ledger.register(
            namespace, kind, source, owner=owner, payload=payload or {"id": source}
        )

    def claim_one(self, source: str = "S-1", **kwargs) -> dict:
        taken = self.ledger.claim(worker="test-worker", limit=1, **kwargs)
        self.assertEqual(1, len(taken), "expected exactly one claimable case")
        return taken[0]

    def receipt(self, assertion: str = "record exists", *, at: float | None = None) -> Readback:
        return Readback(
            source="fake-system",
            record_id="REC-1",
            observed_at=self.now if at is None else at,
            assertion=assertion,
        )


def skip_without_subprocess() -> None:
    if not sys.executable:  # pragma: no cover - defensive
        raise unittest.SkipTest("no interpreter available for subprocess tests")
