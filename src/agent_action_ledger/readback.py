"""Authoritative read-back: the only evidence the ledger accepts as proof.

The ledger never infers that something happened in the outside world. It only
records that a trusted adapter *looked* and *saw*. A :class:`Readback` is that
statement, and it carries the four things needed to judge it later:

``source``
    Which authoritative system was queried (not which agent claims it was).
``record_id``
    Which record in that system was observed.
``observed_at``
    When the read happened, as a POSIX timestamp.
``assertion``
    What the adapter concluded, in the adapter's own words.

A ``Readback`` must be constructed by adapter code *after* an authoritative
read. It must never be built by deserialising model output: a model that can
mint its own receipts can close any case it likes without doing the work.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, ClassVar, Dict

__all__ = ["Readback"]


@dataclass(frozen=True)
class Readback:
    """Evidence from a fresh, authoritative read of an external system."""

    source: str
    record_id: str
    observed_at: float
    assertion: str

    #: How old a read-back may be and still count as evidence, in seconds.
    MAX_AGE_SECONDS: ClassVar[float] = 600.0
    #: Tolerance for a small clock difference between adapter and ledger.
    CLOCK_SKEW_SECONDS: ClassVar[float] = 5.0

    def validate(
        self,
        now: float,
        *,
        after: float = 0.0,
        max_age: float | None = None,
        skew: float | None = None,
    ) -> None:
        """Raise ``ValueError`` unless this read-back can support a decision.

        ``after`` is the moment the read-back has to be newer than: the time an
        action was attempted, or the time a case was completed. A read taken
        *before* the thing it is supposed to prove cannot prove it, no matter
        how confident the assertion sounds.
        """
        max_age = self.MAX_AGE_SECONDS if max_age is None else float(max_age)
        skew = self.CLOCK_SKEW_SECONDS if skew is None else float(skew)
        if not (
            isinstance(self.source, str)
            and isinstance(self.record_id, str)
            and isinstance(self.assertion, str)
            and self.source.strip()
            and self.record_id.strip()
            and self.assertion.strip()
        ):
            raise ValueError("read-back requires a source, a record and an assertion")
        try:
            observed = float(self.observed_at)
        except (TypeError, ValueError):
            raise ValueError("read-back timestamp must be a number") from None
        if not math.isfinite(observed):
            raise ValueError("read-back timestamp must be finite")
        if not float(after) <= observed <= float(now) + skew:
            raise ValueError("read-back does not cover the event it is offered for")
        if float(now) - observed > max_age:
            raise ValueError("read-back is stale; observe the source again")

    def as_dict(self) -> Dict[str, Any]:
        """Return the plain-dict form stored in receipts and event history."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Readback":
        """Rebuild a read-back from stored history.

        Use this to *inspect* an existing receipt. Do not use it to manufacture
        evidence for a new decision: a receipt read back out of the ledger is a
        record of a past observation, not a fresh one.
        """
        return cls(
            source=data["source"],
            record_id=data["record_id"],
            observed_at=float(data["observed_at"]),
            assertion=data["assertion"],
        )
