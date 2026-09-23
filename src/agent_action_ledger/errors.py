"""Exception hierarchy for the ledger.

Every error raised by this package derives from :class:`LedgerError`. The
input-validation errors additionally derive from :class:`ValueError`, so code
that already guards ledger calls with ``except ValueError`` keeps working.
"""
from __future__ import annotations

__all__ = [
    "LedgerError",
    "ValidationError",
    "StoreCorrupt",
    "UnknownCase",
    "InvalidTransition",
    "ActionConflict",
    "LeaseLost",
    "WritesDisarmed",
]


class LedgerError(Exception):
    """Base class for every error this package raises."""


class ValidationError(LedgerError, ValueError):
    """A caller supplied an argument the ledger refuses to store."""


class StoreCorrupt(ValidationError):
    """Persisted data failed validation; the ledger fails closed.

    Raised when an on-disk record violates an invariant the ledger guarantees
    (identity mismatch, impossible lease pair, unparsable payload, ...). The
    ledger never repairs such a file: restore a verified snapshot instead.
    """


class UnknownCase(ValidationError):
    """The referenced case does not exist in this ledger."""


class InvalidTransition(ValidationError):
    """The requested state change is not permitted from the current state."""


class ActionConflict(ValidationError):
    """An action key was reused for a different case, kind or request."""


class LeaseLost(LedgerError, RuntimeError):
    """The worker no longer holds the lease it is trying to act under.

    The lease expired, was taken over by another worker, or the case was
    restored from a snapshot. A worker that sees this must stop acting on the
    case immediately: another worker may already be working on it.
    """


class WritesDisarmed(LedgerError, RuntimeError):
    """An action was attempted while the write gate is disarmed.

    New ledgers and restored snapshots are disarmed by default. An operator
    arms writes explicitly, after reconciling outstanding action receipts.
    """
