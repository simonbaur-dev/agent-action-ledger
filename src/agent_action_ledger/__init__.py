"""agent-action-ledger: a durable transaction kernel for autonomous agents.

The ledger answers one question well: *what does this agent believe it has
done, and what can it prove?* It gives you durable cases, deterministic
identities, time-bounded leases so several workers can share the work, an
append-only history, and action receipts that only settle against an
authoritative read-back.

It does **not** give an agent authority to do anything. Nothing in this package
performs an external effect; the adapters you write around it do, and they are
what must verify that an effect really happened.

Typical use::

    from agent_action_ledger import ActionLedger, Readback

    ledger = ActionLedger("ledger.sqlite")
    case = ledger.register(
        "support", "ticket", "T-1042",
        owner="triage-bot", payload={"subject": "cannot log in"},
    )
    for claimed in ledger.claim(worker="worker-a", limit=1):
        ledger.finish(
            claimed["id"], claimed["lease_token"],
            status="waiting", reason="awaiting customer reply",
            next_check=time.time() + 3600,
        )
"""
from __future__ import annotations

from .errors import (
    ActionConflict,
    InvalidTransition,
    LeaseLost,
    LedgerError,
    StoreCorrupt,
    UnknownCase,
    ValidationError,
    WritesDisarmed,
)
from .identity import case_id, encode_json, fingerprint
from .ledger import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_CONCURRENT_LEASES,
    EXCLUSIVE_GROUPS,
    MAX_LEASE_SECONDS,
    RECOVERY_PAUSE_SECONDS,
    ActionLedger,
)
from .readback import Readback
from .schema import ACTION_STATES, SCHEMA_VERSION, STATES
from .snapshot import manifest_path, verify_snapshot

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ActionLedger",
    "Readback",
    "case_id",
    "encode_json",
    "fingerprint",
    "verify_snapshot",
    "manifest_path",
    "STATES",
    "ACTION_STATES",
    "SCHEMA_VERSION",
    "DEFAULT_LEASE_SECONDS",
    "MAX_LEASE_SECONDS",
    "DEFAULT_MAX_CONCURRENT_LEASES",
    "RECOVERY_PAUSE_SECONDS",
    "EXCLUSIVE_GROUPS",
    "LedgerError",
    "ValidationError",
    "StoreCorrupt",
    "UnknownCase",
    "InvalidTransition",
    "ActionConflict",
    "LeaseLost",
    "WritesDisarmed",
]
