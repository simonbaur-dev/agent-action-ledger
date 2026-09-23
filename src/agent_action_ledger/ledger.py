"""The ledger itself: durable cases, leases, events and action receipts.

Authority boundary
------------------
This module records and coordinates work. It never performs it. Nothing here
opens a socket, sends a message, runs a subprocess or touches any system other
than its own SQLite file. Creating a ledger grants an agent no authority
whatsoever; the adapters you write around it are where authority lives, and
they are what must actually verify that an external effect happened.

The ledger's job is narrower and harder to get right by hand: making sure that
what an agent *believes* about the outside world is durable, shared between
workers, and never quietly upgraded from "attempted" to "done".
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from .errors import (
    ActionConflict,
    InvalidTransition,
    LeaseLost,
    StoreCorrupt,
    UnknownCase,
    ValidationError,
    WritesDisarmed,
)
from .filelock import file_lock
from .identity import case_id as make_case_id
from .identity import encode_json, fingerprint, normalise_slug
from .readback import Readback
from .schema import ACTION_STATES, APP_ID, SCHEMA, SCHEMA_VERSION, STATES
from .snapshot import create_snapshot, verify_snapshot

__all__ = [
    "ActionLedger",
    "DEFAULT_LEASE_SECONDS",
    "MAX_LEASE_SECONDS",
    "DEFAULT_MAX_CONCURRENT_LEASES",
    "RECOVERY_PAUSE_SECONDS",
    "EXCLUSIVE_GROUPS",
]

DEFAULT_LEASE_SECONDS = 600.0
MAX_LEASE_SECONDS = 3600.0
DEFAULT_MAX_CONCURRENT_LEASES = 4
#: How long a case rests after its worker vanished, before it is offered again.
#: The pause is deliberate: it gives an operator (or the worker's own restart)
#: a window to reconcile receipts before anyone picks the case back up.
RECOVERY_PAUSE_SECONDS = 300.0

#: Optional mutual exclusion while claiming, for work that must not run twice
#: in parallel against the same upstream system.
EXCLUSIVE_GROUPS = (None, "namespace", "kind", "namespace_kind")

LEASE_EXPIRED_REASON = (
    "Worker stopped or exceeded its lease; resume after checking action receipts"
)


def _default_worker_id() -> str:
    """A stable-per-process identifier, for humans reading the ledger."""
    return f"{platform.node() or 'unknown-host'}:{os.getpid()}"


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} must be a number") from None
    if not math.isfinite(number):
        raise ValidationError(f"{label} must be finite")
    return number


class ActionLedger:
    """A durable, multi-worker ledger of cases and the actions taken on them.

    One connection per worker. Instances are not thread-safe; give each thread
    or process its own :class:`ActionLedger` pointing at the same file, which
    is exactly the coordination case the leasing rules are built for.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        max_concurrent_leases: int = DEFAULT_MAX_CONCURRENT_LEASES,
        validate_on_claim: bool = True,
    ) -> None:
        """Open (or create) the ledger at ``path``.

        ``clock`` is injectable so tests and simulations can control time.
        ``max_concurrent_leases`` is recorded when the ledger is *created* and
        shared by every worker afterwards; passing a different value when
        opening an existing ledger is ignored (use
        :meth:`set_max_concurrent_leases`). ``validate_on_claim`` re-validates
        every stored row on each claim, which is a cheap and very effective
        tripwire for small ledgers; turn it off for large ones.
        """
        if not isinstance(max_concurrent_leases, int) or max_concurrent_leases < 1:
            raise ValidationError("max_concurrent_leases must be a positive integer")
        self.path = Path(path)
        self.clock = clock
        self.validate_on_claim = bool(validate_on_claim)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Creation is serialised across processes: two workers starting at the
        # same moment must not both decide they are the one creating the file.
        with file_lock(str(self.path) + ".init.lock"):
            exists = self.path.exists()
            if exists and self.path.stat().st_size == 0:
                raise StoreCorrupt(
                    "existing ledger file is empty; restore a verified snapshot"
                )
            if not exists:
                # Make the file private before SQLite writes a single page into
                # it. A ledger's payloads are operational metadata, not public.
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(handle)
            self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            self.db.row_factory = sqlite3.Row
            try:
                self.db.execute("PRAGMA foreign_keys=ON")
                self.db.execute("PRAGMA busy_timeout=5000")
                if exists:
                    self._check_existing_file()
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=FULL")
                if not exists:
                    self._create(max_concurrent_leases)
                self.validate()
            except BaseException:
                self.db.close()
                raise

        for candidate in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            if candidate.exists():
                os.chmod(candidate, 0o600)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _check_existing_file(self) -> None:
        if self.db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
            raise StoreCorrupt("not an agent action ledger")
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise StoreCorrupt("ledger failed the SQLite integrity check")
        row = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            raise StoreCorrupt("unsupported ledger schema version")

    def _create(self, max_concurrent_leases: int) -> None:
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA application_id={APP_ID}")
        self.db.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
        # New ledgers start disarmed. Arming is an operator decision, never a
        # side effect of creating a file.
        self.db.execute("INSERT INTO meta VALUES ('writes_armed', '0')")
        self.db.execute(
            "INSERT INTO meta VALUES ('max_concurrent_leases', ?)",
            (str(max_concurrent_leases),),
        )
        self.db.execute(
            "INSERT INTO gate_log(at, armed, note) VALUES(?, 0, ?)",
            (self.clock(), "ledger created; writes disarmed"),
        )

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        self.db.close()

    def __enter__(self) -> "ActionLedger":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"ActionLedger({str(self.path)!r})"

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block inside one immediate SQLite transaction.

        Everything the ledger writes goes through here: a case update and the
        event that explains it either both land or neither does. Any exception
        rolls the whole block back, including exceptions raised by caller code
        inside a ``with ledger.transaction():`` block of your own.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_case_row(row: sqlite3.Row) -> None:
        try:
            if row["id"] != make_case_id(row["namespace"], row["kind"], row["source_id"]):
                raise StoreCorrupt("case identity does not match its source triple")
            if not row["owner"].strip() or row["status"] not in STATES:
                raise StoreCorrupt("case has no owner or an unknown status")
            if not isinstance(json.loads(row["payload"]), dict):
                raise StoreCorrupt("case payload must be a JSON object")
            leased = row["lease_token"] is not None
            if (
                leased != (row["lease_until"] is not None)
                or leased != (row["lease_owner"] is not None)
                or (leased and not row["lease_token"].strip())
            ):
                raise StoreCorrupt("case has an incoherent lease")
            if row["status"] == "complete":
                if leased or row["next_check"] is not None:
                    raise StoreCorrupt("completed case is still leased or scheduled")
            elif row["next_check"] is None:
                raise StoreCorrupt("open case has no next check")
            if row["status"] == "human_decision" and not row["question"].strip():
                raise StoreCorrupt("case awaits a human decision but asks no question")
            for field in ("next_check", "lease_until", "created_at", "updated_at"):
                value = row[field]
                if value is not None and not math.isfinite(float(value)):
                    raise StoreCorrupt("case carries a non-finite timestamp")
            if (
                row["updated_at"] < row["created_at"]
                or row["attempts"] < 0
                or row["transient_retries"] < 0
            ):
                raise StoreCorrupt("case counters or timestamps are inconsistent")
        except StoreCorrupt:
            raise
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise StoreCorrupt("invalid persisted case") from exc

    @staticmethod
    def _validate_action_row(row: sqlite3.Row) -> None:
        if (
            not row["key"]
            or not row["kind"]
            or len(row["fingerprint"]) != 64
            or row["status"] not in ACTION_STATES
            or row["attempts"] < 1
            or not all(
                math.isfinite(float(row[field]))
                for field in ("attempted_at", "updated_at")
            )
            or row["updated_at"] < row["attempted_at"]
        ):
            raise StoreCorrupt("invalid persisted action")
        if row["status"] == "attempted":
            return
        try:
            receipt = json.loads(row["receipt"])
            if not all(receipt.get(f) for f in ("source", "record_id", "assertion")):
                raise StoreCorrupt("settled action carries an incomplete receipt")
            if not math.isfinite(float(receipt["observed_at"])):
                raise StoreCorrupt("settled action carries a non-finite receipt time")
        except StoreCorrupt:
            raise
        except (TypeError, AttributeError, KeyError, ValueError) as exc:
            raise StoreCorrupt("invalid persisted receipt") from exc

    def validate(self) -> None:
        """Re-check every invariant against what is actually on disk.

        Raises :class:`~agent_action_ledger.errors.StoreCorrupt` on the first
        problem found. This is the fail-closed path: a ledger that cannot prove
        it is consistent refuses to hand out work.
        """
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StoreCorrupt("ledger references records that do not exist")
        meta = dict(self.db.execute("SELECT key, value FROM meta"))
        if meta.get("schema") != SCHEMA_VERSION:
            raise StoreCorrupt("unsupported ledger schema version")
        if meta.get("writes_armed") not in ("0", "1"):
            raise StoreCorrupt("write gate is in an unknown state")
        try:
            if int(meta.get("max_concurrent_leases", "")) < 1:
                raise ValueError
        except ValueError:
            raise StoreCorrupt("invalid worker limit in ledger metadata") from None
        for row in self.db.execute("SELECT * FROM cases"):
            self._validate_case_row(row)
        for row in self.db.execute("SELECT * FROM actions"):
            self._validate_action_row(row)
        for row in self.db.execute("SELECT at, detail FROM events"):
            if not math.isfinite(float(row["at"])):
                raise StoreCorrupt("event carries a non-finite timestamp")
            try:
                json.loads(row["detail"])
            except ValueError as exc:
                raise StoreCorrupt("event detail is not valid JSON") from exc

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    @staticmethod
    def _as_case(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        case = dict(row)
        case["payload"] = json.loads(case["payload"])
        return case

    def get(self, case_id: str) -> Optional[Dict[str, Any]]:
        """Return a case as a dictionary, or ``None`` if it does not exist."""
        row = self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return self._as_case(row)

    def require(self, case_id: str) -> Dict[str, Any]:
        """Like :meth:`get`, but raises :class:`UnknownCase` when missing."""
        case = self.get(case_id)
        if case is None:
            raise UnknownCase(f"unknown case: {case_id}")
        return case

    def cases(
        self,
        *,
        status: Optional[str] = None,
        namespace: Optional[str] = None,
        kind: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """List cases, most urgent first, optionally filtered."""
        clauses: List[str] = []
        params: List[Any] = []
        if status is not None:
            if status not in STATES:
                raise ValidationError(f"unknown status: {status!r}")
            clauses.append("status = ?")
            params.append(status)
        try:
            if namespace is not None:
                clauses.append("namespace = ?")
                params.append(normalise_slug(namespace, label="namespace"))
            if kind is not None:
                clauses.append("kind = ?")
                params.append(normalise_slug(kind, label="kind"))
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT * FROM cases" + where
            + " ORDER BY (next_check IS NULL), next_check, id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._as_case(row) for row in self.db.execute(sql, params)]

    def due(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Cases that are ready to be claimed right now."""
        moment = self.clock() if now is None else _finite(now, "now")
        rows = self.db.execute(
            """SELECT * FROM cases WHERE status != 'complete' AND next_check <= ?
                 AND (lease_until IS NULL OR lease_until <= ?)
               ORDER BY next_check, id""",
            (moment, moment),
        )
        return [self._as_case(row) for row in rows]

    def history(self, case_id: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """The append-only event history of a case, oldest first."""
        sql = "SELECT id, kind, at, detail FROM events WHERE case_id=? ORDER BY id"
        params: List[Any] = [case_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [
            {"id": r["id"], "kind": r["kind"], "at": r["at"], "detail": json.loads(r["detail"])}
            for r in self.db.execute(sql, params)
        ]

    def action(self, action_key: str) -> Optional[Dict[str, Any]]:
        """Return a recorded action, with its receipt parsed, or ``None``."""
        row = self.db.execute(
            "SELECT * FROM actions WHERE key=?", (action_key,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["receipt"] = json.loads(record["receipt"]) if record["receipt"] else None
        return record

    def unsettled_actions(self) -> List[Dict[str, Any]]:
        """Actions that were attempted and never settled either way.

        Each of these is a genuine unknown: something was started against an
        external system and nobody has since looked to see whether it landed.
        They are the work list for a person before writes are armed again.
        """
        rows = self.db.execute(
            "SELECT key FROM actions WHERE status='attempted' ORDER BY attempted_at"
        ).fetchall()
        return [self.action(row["key"]) for row in rows]

    @property
    def max_concurrent_leases(self) -> int:
        """How many cases may be leased at once across all workers."""
        return int(
            self.db.execute(
                "SELECT value FROM meta WHERE key='max_concurrent_leases'"
            ).fetchone()[0]
        )

    def set_max_concurrent_leases(self, limit: int) -> None:
        """Change the shared worker limit. An operator decision, not a worker's."""
        if not isinstance(limit, int) or limit < 1:
            raise ValidationError("max_concurrent_leases must be a positive integer")
        with self.transaction():
            self.db.execute(
                "UPDATE meta SET value=? WHERE key='max_concurrent_leases'", (str(limit),)
            )

    def stats(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """A small snapshot of ledger health, for status output and monitoring."""
        moment = self.clock() if now is None else _finite(now, "now")
        by_status = {status: 0 for status in STATES}
        for row in self.db.execute("SELECT status, count(*) AS n FROM cases GROUP BY status"):
            by_status[row["status"]] = row["n"]
        leased = self.db.execute(
            "SELECT count(*) FROM cases WHERE lease_until > ?", (moment,)
        ).fetchone()[0]
        overdue = self.db.execute(
            """SELECT count(*) FROM cases WHERE status != 'complete' AND next_check < ?
                 AND (lease_until IS NULL OR lease_until <= ?)""",
            (moment - 3600, moment),
        ).fetchone()[0]
        return {
            "path": str(self.path),
            "writes_armed": self.writes_armed(),
            "cases": sum(by_status.values()),
            "by_status": by_status,
            "due_now": len(self.due(now=moment)),
            "leased_now": leased,
            "overdue_over_an_hour": overdue,
            "max_concurrent_leases": self.max_concurrent_leases,
            "actions": self.db.execute("SELECT count(*) FROM actions").fetchone()[0],
            "unsettled_actions": self.db.execute(
                "SELECT count(*) FROM actions WHERE status='attempted'"
            ).fetchone()[0],
            "events": self.db.execute("SELECT count(*) FROM events").fetchone()[0],
        }

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def _event(self, case_id: str, kind: str, detail: Dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events(case_id, kind, at, detail) VALUES(?, ?, ?, ?)",
            (case_id, kind, self.clock(), encode_json(detail)),
        )

    def record_event(self, case_id: str, kind: str, detail: Dict[str, Any]) -> None:
        """Append an application-defined event to a case's history."""
        if not isinstance(kind, str) or not kind.strip():
            raise ValidationError("event kind must be a non-empty string")
        if not isinstance(detail, dict):
            raise ValidationError("event detail must be a JSON object")
        with self.transaction():
            if self.get(case_id) is None:
                raise UnknownCase(f"unknown case: {case_id}")
            self._event(case_id, kind, detail)

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

    def register(
        self,
        namespace: str,
        kind: str,
        source_id: str,
        *,
        owner: str,
        payload: Dict[str, Any],
        next_check: Optional[float] = None,
    ) -> str:
        """Record that a unit of work exists, and return its stable case ID.

        Safe to call on every poll. Registering an item that is already known
        does not open a second case and does not reset progress:

        * an unchanged payload changes nothing at all;
        * a changed payload updates the payload and pulls a *waiting* case
          forward, because the source has new information;
        * a case parked on a human keeps its schedule, because new upstream
          data does not answer the question that was asked;
        * a *complete* case stays complete. New evidence that genuinely
          reopens finished work goes through :meth:`reopen_with_evidence`,
          which demands a read-back newer than the completion. Otherwise a
          routine poll could undo a verified outcome.
        """
        if not isinstance(owner, str) or not owner.strip():
            raise ValidationError("every case needs an owner")
        if not isinstance(payload, dict):
            raise ValidationError("case payload must be a dictionary")
        try:
            key = make_case_id(namespace, kind, source_id)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        now = self.clock()
        due = now if next_check is None else _finite(next_check, "next_check")
        body = encode_json(payload)
        with self.transaction():
            row = self.db.execute(
                "SELECT payload FROM cases WHERE id=?", (key,)
            ).fetchone()
            if row is None:
                self.db.execute(
                    """INSERT INTO cases
                         (id, namespace, kind, source_id, owner, status, payload,
                          next_check, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                    (key, namespace, kind, source_id, owner, body, due, now, now),
                )
                self._event(key, "opened", {"source_id": source_id, "owner": owner})
            elif row[0] != body:
                self.db.execute(
                    """UPDATE cases SET payload = ?,
                         updated_at = CASE WHEN status = 'complete'
                                           THEN updated_at ELSE ? END,
                         next_check = CASE
                             WHEN status = 'complete' THEN NULL
                             WHEN status = 'human_decision' THEN next_check
                             ELSE min(coalesce(next_check, ?), ?) END
                       WHERE id = ?""",
                    (body, now, due, due, key),
                )
                self._event(key, "source_changed", {})
        return key

    # ------------------------------------------------------------------
    # leasing
    # ------------------------------------------------------------------

    def claim(
        self,
        *,
        worker: Optional[str] = None,
        limit: int = 1,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        kinds: Optional[Sequence[str]] = None,
        namespaces: Optional[Sequence[str]] = None,
        statuses: Optional[Sequence[str]] = None,
        exclusive_group: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Take time-bounded leases on up to ``limit`` due cases.

        Claiming is a single immediate transaction, so two workers racing at
        the same instant cannot both take the same case, and the shared
        ``max_concurrent_leases`` ceiling holds across processes.

        Before handing anything out, expired leases are reclaimed: a case whose
        worker vanished is un-leased, moved to ``waiting`` and scheduled
        ``RECOVERY_PAUSE_SECONDS`` into the future. It is deliberately *not*
        handed straight to the next worker — the previous worker may have got
        as far as attempting an external action, and that has to be reconciled
        before anyone acts again.

        ``exclusive_group`` additionally allows at most one active lease per
        ``namespace``, per ``kind`` or per pair, for work that must not run
        concurrently against the same upstream system.

        By default only ``pending`` and ``waiting`` cases are offered. Cases
        parked on a human stay parked unless a specialised worker explicitly
        includes ``human_decision`` in ``statuses``. This makes the safe
        behaviour the default for ordinary worker loops.
        """
        worker = worker or _default_worker_id()
        if not isinstance(worker, str) or not worker.strip():
            raise ValidationError("worker must be a non-empty string")
        if not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        lease = _finite(lease_seconds, "lease_seconds")
        if not 1 <= lease <= MAX_LEASE_SECONDS:
            raise ValidationError(
                f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS:.0f}"
            )
        if exclusive_group not in EXCLUSIVE_GROUPS:
            raise ValidationError(f"unknown exclusive_group: {exclusive_group!r}")
        kinds = self._slug_filter(kinds, label="kind")
        namespaces = self._slug_filter(namespaces, label="namespace")
        statuses = ["pending", "waiting"] if statuses is None else list(statuses)
        if not statuses or any(s not in STATES for s in statuses):
            raise ValidationError("statuses filter must be non-empty known states")
        if "complete" in statuses:
            raise ValidationError("completed cases are never claimable")

        now = self.clock()
        claimed: List[Dict[str, Any]] = []
        with self.transaction():
            if self.validate_on_claim:
                self.validate()
            self._reclaim_expired(now)

            active = self.db.execute(
                "SELECT count(*) FROM cases WHERE lease_until > ?", (now,)
            ).fetchone()[0]
            available = max(0, min(limit, self.max_concurrent_leases - active))
            if available == 0:
                return []

            sql = """SELECT * FROM cases WHERE status != 'complete' AND next_check <= ?
                       AND (lease_until IS NULL OR lease_until <= ?)"""
            params: List[Any] = [now, now]
            if kinds:
                sql += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
                params.extend(kinds)
            if namespaces:
                sql += " AND namespace IN (" + ",".join("?" * len(namespaces)) + ")"
                params.extend(namespaces)
            if statuses:
                sql += " AND status IN (" + ",".join("?" * len(statuses)) + ")"
                params.extend(statuses)
            sql += " ORDER BY next_check, id"
            rows = self.db.execute(sql, params).fetchall()

            busy = self._busy_groups(now, exclusive_group)
            for row in rows:
                if len(claimed) >= available:
                    break
                group = self._group_of(row, exclusive_group)
                if group is not None:
                    if group in busy:
                        continue
                    busy.add(group)
                token = uuid.uuid4().hex
                self.db.execute(
                    """UPDATE cases SET lease_token = ?, lease_until = ?, lease_owner = ?,
                         attempts = attempts + 1, transient_retries = 0, updated_at = ?
                       WHERE id = ?""",
                    (token, now + lease, worker, now, row["id"]),
                )
                self._event(
                    row["id"],
                    "claimed",
                    {"worker": worker, "deadline": now + lease},
                )
                claimed.append(self.get(row["id"]))
        return claimed

    @staticmethod
    def _slug_filter(values: Optional[Sequence[str]], *, label: str) -> List[str]:
        if values is None:
            return []
        if isinstance(values, str) or not isinstance(values, (list, tuple, set, frozenset)):
            raise ValidationError(f"{label}s filter must be a sequence of slugs")
        if not values:
            raise ValidationError(f"{label}s filter must not be empty")
        try:
            return [normalise_slug(value, label=label) for value in values]
        except ValueError as exc:
            raise ValidationError(str(exc)) from None

    @staticmethod
    def _group_of(row: sqlite3.Row, exclusive_group: Optional[str]) -> Optional[tuple]:
        if exclusive_group is None:
            return None
        if exclusive_group == "namespace":
            return (row["namespace"],)
        if exclusive_group == "kind":
            return (row["kind"],)
        return (row["namespace"], row["kind"])

    def _busy_groups(self, now: float, exclusive_group: Optional[str]) -> set:
        if exclusive_group is None:
            return set()
        rows = self.db.execute(
            "SELECT namespace, kind FROM cases WHERE lease_until > ?", (now,)
        ).fetchall()
        return {self._group_of(row, exclusive_group) for row in rows}

    def _reclaim_expired(self, now: float) -> None:
        expired = self.db.execute(
            """SELECT id FROM cases WHERE lease_token IS NOT NULL
                 AND lease_until <= ? AND status != 'complete'""",
            (now,),
        ).fetchall()
        for row in expired:
            self.db.execute(
                """UPDATE cases SET lease_token = NULL, lease_until = NULL,
                     lease_owner = NULL, status = 'waiting', next_check = ?,
                     reason = ?, updated_at = ?
                   WHERE id = ?""",
                (now + RECOVERY_PAUSE_SECONDS, LEASE_EXPIRED_REASON, now, row["id"]),
            )
            self._event(
                row["id"],
                "lease_expired",
                {"next_check": now + RECOVERY_PAUSE_SECONDS},
            )

    def _leased(self, case_id: str, token: Optional[str]) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is not None:
            self._validate_case_row(row)
        if (
            row is None
            or not token
            or row["lease_token"] != token
            or row["lease_until"] <= self.clock()
        ):
            raise LeaseLost("lease expired, or the case belongs to another worker")
        return row

    def renew(
        self, case_id: str, token: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> float:
        """Extend a lease the caller still holds; returns the new deadline.

        Renewing requires the lease to be *currently valid*. A worker that let
        its lease lapse does not get it back: the case may already be somebody
        else's, and taking it back silently is exactly how two workers end up
        acting on the same thing.
        """
        lease = _finite(lease_seconds, "lease_seconds")
        if not 1 <= lease <= MAX_LEASE_SECONDS:
            raise ValidationError(
                f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS:.0f}"
            )
        with self.transaction():
            self._leased(case_id, token)
            now = self.clock()
            deadline = now + lease
            self.db.execute(
                "UPDATE cases SET lease_until = ?, updated_at = ? WHERE id = ?",
                (deadline, now, case_id),
            )
            self._event(case_id, "lease_renewed", {"deadline": deadline})
        return deadline

    def transient_retry(self, case_id: str, token: str) -> bool:
        """Consume the single retry allowed per lease; ``False`` if spent.

        One retry absorbs a genuine blip. An unlimited retry loop just turns a
        real failure into a fast one repeated forever, so the second attempt to
        retry within the same lease is refused and the case should be parked.
        """
        with self.transaction():
            row = self._leased(case_id, token)
            if row["transient_retries"] >= 1:
                return False
            self.db.execute(
                "UPDATE cases SET transient_retries = transient_retries + 1 WHERE id = ?",
                (case_id,),
            )
            self._event(case_id, "transient_retry", {})
            return True

    # ------------------------------------------------------------------
    # state transitions
    # ------------------------------------------------------------------

    def finish(
        self,
        case_id: str,
        token: str,
        *,
        status: str,
        reason: str,
        next_check: Optional[float] = None,
        question: str = "",
        readback: Optional[Readback] = None,
    ) -> Dict[str, Any]:
        """Release the lease and put the case into its next state.

        ``complete`` is the only state that costs evidence, and it costs the
        right kind: a :class:`~agent_action_ledger.readback.Readback` that is
        fresh, and newer than the moment the case was last touched. "The script
        exited zero" is not evidence that anything happened in another system;
        "I read record X in system Y just now and it says Z" is.

        Every other state needs a concrete reason and a future ``next_check``,
        so no open case can be left without a time at which someone looks
        again. Parking on ``human_decision`` additionally requires the actual
        ``question`` being asked.
        """
        if status not in STATES:
            raise ValidationError(f"unknown status: {status!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("a concrete reason is required")
        if status == "human_decision" and (
            not isinstance(question, str) or not question.strip()
        ):
            raise ValidationError("parking on a human requires the question to ask")
        if status == "complete":
            if not isinstance(readback, Readback):
                raise ValidationError(
                    "completion requires an authoritative read-back; "
                    "the ledger does not infer that work happened"
                )
            try:
                readback.validate(self.clock())
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            next_check = None
        else:
            due = _finite(next_check, "next_check")
            if due <= self.clock():
                raise ValidationError("every open case needs a next check in the future")
            next_check = due

        with self.transaction():
            row = self._leased(case_id, token)
            if readback is not None:
                try:
                    readback.validate(self.clock(), after=row["updated_at"])
                except ValueError as exc:
                    raise ValidationError(str(exc)) from None
            self.db.execute(
                """UPDATE cases SET status = ?, reason = ?, next_check = ?, question = ?,
                     lease_token = NULL, lease_until = NULL, lease_owner = NULL,
                     updated_at = ?
                   WHERE id = ?""",
                (
                    status,
                    reason,
                    next_check,
                    question if status == "human_decision" else "",
                    self.clock(),
                    case_id,
                ),
            )
            self._event(
                case_id,
                status,
                {
                    "reason": reason,
                    "question": question if status == "human_decision" else "",
                    "readback": readback.as_dict() if readback else None,
                },
            )
        return self.require(case_id)

    def resume_human_decision(
        self,
        case_id: str,
        *,
        reason: str,
        answer: str = "",
        next_check: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record a person's answer and make the case workable again.

        The case goes to ``pending``, never straight to ``complete``. An
        administrative answer settles the *question*, not the *work*: knowing
        which of two records an item belongs to says nothing about whether
        anything was then done about it. The owner re-observes and decides.

        Two situations refuse, because in both of them "look again" could
        double an external effect: a live lease (a worker is on this case right
        now) and an unsettled action attempt (something was started and nobody
        has confirmed whether it landed).
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("a concrete reason is required")
        now = self.clock()
        due = now + 60.0 if next_check is None else _finite(next_check, "next_check")
        if due <= now:
            raise ValidationError("every open case needs a next check in the future")
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM cases WHERE id=?", (case_id,)
            ).fetchone()
            if row is None:
                raise UnknownCase(f"unknown case: {case_id}")
            self._validate_case_row(row)
            if row["status"] != "human_decision":
                raise InvalidTransition(
                    "only a case awaiting a human decision can be resumed this way"
                )
            if row["lease_token"] is not None and row["lease_until"] > now:
                raise InvalidTransition(
                    "a worker holds this case; resume after its lease ends"
                )
            unsettled = self.db.execute(
                "SELECT count(*) FROM actions WHERE case_id=? AND status='attempted'",
                (case_id,),
            ).fetchone()[0]
            if unsettled:
                raise InvalidTransition(
                    "an attempted action is unsettled; confirm its receipt first"
                )
            self.db.execute(
                """UPDATE cases SET status = 'pending', reason = ?, next_check = ?,
                     lease_token = NULL, lease_until = NULL, lease_owner = NULL,
                     updated_at = ?
                   WHERE id = ?""",
                (reason, due, now, case_id),
            )
            self._event(
                case_id,
                "human_decision_resumed",
                {
                    "reason": reason,
                    "question": row["question"],
                    "answer": str(answer)[:500],
                    "next_check": due,
                },
            )
        return self.require(case_id)

    def reopen_with_evidence(
        self, case_id: str, *, reason: str, owner: str, readback: Readback
    ) -> Dict[str, Any]:
        """Reopen a completed case, against evidence newer than the completion.

        This is the only route from ``complete`` back to ``pending``, and the
        read-back must have been observed *after* the case was completed.
        Without that rule, a stale observation taken before the work finished
        could reopen settled work and cause it to be done twice.
        """
        if not isinstance(readback, Readback):
            raise ValidationError("reopening requires an authoritative read-back")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("a concrete reason is required")
        if not isinstance(owner, str) or not owner.strip():
            raise ValidationError("an owner is required")
        try:
            readback.validate(self.clock())
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM cases WHERE id=?", (case_id,)
            ).fetchone()
            if row is None:
                raise UnknownCase(f"unknown case: {case_id}")
            self._validate_case_row(row)
            if row["status"] != "complete":
                raise InvalidTransition(
                    "only completed cases can be reopened; open work keeps its lease"
                )
            if readback.observed_at <= row["updated_at"]:
                raise ValidationError(
                    "reopening requires evidence observed after completion"
                )
            now = self.clock()
            self.db.execute(
                """UPDATE cases SET status = 'pending', reason = ?, owner = ?,
                     next_check = ?, lease_token = NULL, lease_until = NULL,
                     lease_owner = NULL, updated_at = ?
                   WHERE id = ?""",
                (reason, owner, now, now, case_id),
            )
            self._event(
                case_id,
                "reopened",
                {"reason": reason, "owner": owner, "readback": readback.as_dict()},
            )
        return self.require(case_id)

    # ------------------------------------------------------------------
    # write gate
    # ------------------------------------------------------------------

    def writes_armed(self) -> bool:
        """Whether external actions may currently be attempted."""
        return (
            self.db.execute(
                "SELECT value FROM meta WHERE key='writes_armed'"
            ).fetchone()[0]
            == "1"
        )

    def arm_writes(self, note: str) -> None:
        """Allow actions to be attempted. An operator action, not an agent's.

        The ``note`` is mandatory and recorded: arming is a decision somebody
        made, and six months later the only useful question is who decided it
        and on what basis. Do not expose this to whatever dispatches model
        output — a gate an agent can open is not a gate.

        Arming while :meth:`unsettled_actions` is non-empty is allowed but
        logged as such, because sometimes the reconciliation *is* "these two
        are known duplicates, proceed". It should be a conscious choice.
        """
        if not isinstance(note, str) or not note.strip():
            raise ValidationError("arming requires a note recording who verified what")
        with self.transaction():
            unsettled = self.db.execute(
                "SELECT count(*) FROM actions WHERE status='attempted'"
            ).fetchone()[0]
            self.db.execute("UPDATE meta SET value='1' WHERE key='writes_armed'")
            self.db.execute(
                "INSERT INTO gate_log(at, armed, note) VALUES(?, 1, ?)",
                (
                    self.clock(),
                    note if not unsettled else f"{note} [unsettled actions: {unsettled}]",
                ),
            )

    def disarm_writes(self, note: str) -> None:
        """Stop any further external actions. Always available, never refused."""
        if not isinstance(note, str) or not note.strip():
            raise ValidationError("disarming requires a note")
        with self.transaction():
            self.db.execute("UPDATE meta SET value='0' WHERE key='writes_armed'")
            self.db.execute(
                "INSERT INTO gate_log(at, armed, note) VALUES(?, 0, ?)",
                (self.clock(), note),
            )

    def write_gate_history(self) -> List[Dict[str, Any]]:
        """Every arming and disarming, oldest first."""
        return [
            {"at": r["at"], "armed": bool(r["armed"]), "note": r["note"]}
            for r in self.db.execute("SELECT at, armed, note FROM gate_log ORDER BY id")
        ]

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def begin_action(
        self,
        case_id: str,
        token: str,
        action_key: str,
        *,
        kind: str,
        request: Any,
    ) -> str:
        """Ask permission to attempt one external action. Returns what to do.

        ``"execute"``
            Nothing has been attempted under this key. Perform the call, then
            call :meth:`confirm_action` with an authoritative read-back.
        ``"confirmed"``
            This exact action already happened and was verified. Do nothing.
        ``"reconcile"``
            An attempt was recorded and never settled. **Do not repeat it.**
            Go and look at the external system: confirm it with
            :meth:`confirm_action`, or, if an authoritative read shows the
            effect is genuinely absent, record that with
            :meth:`record_action_absent` and only then retry.

        ``action_key`` is the caller's idempotency key — it must identify the
        intended effect, not the attempt (``"reply-to-ticket-9"``, not
        ``"reply-attempt-3"``). ``request`` is fingerprinted, so reusing a key
        for a different request is refused rather than silently accepted.

        The attempt is committed *before* the external call, which is what
        makes it survive a crash mid-call. That deliberately admits the
        opposite failure — an attempt recorded for a call that never left the
        process — because the recoverable outcome of the two is "look before
        you act again", not "act again and hope".
        """
        if not isinstance(action_key, str) or not action_key.strip():
            raise ValidationError("action key must be a non-empty string")
        if not isinstance(kind, str) or not kind.strip():
            raise ValidationError("action kind must be a non-empty string")
        try:
            digest = fingerprint(request)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"action request is not encodable: {exc}") from None

        with self.transaction():
            self._leased(case_id, token)
            if not self.writes_armed():
                raise WritesDisarmed(
                    "writes are disarmed; an operator must arm them after "
                    "reconciling outstanding receipts"
                )
            existing = self.action(action_key)
            now = self.clock()
            if existing is not None:
                if (
                    existing["case_id"],
                    existing["kind"],
                    existing["fingerprint"],
                ) != (case_id, kind, digest):
                    raise ActionConflict(
                        "action key already used for a different case, kind or request"
                    )
                if existing["status"] == "confirmed":
                    return "confirmed"
                if existing["status"] == "attempted":
                    return "reconcile"
                # not_performed: an authoritative read showed the effect is
                # absent, so retrying is safe and is a new attempt.
                self.db.execute(
                    """UPDATE actions SET status = 'attempted', attempts = attempts + 1,
                         attempted_at = ?, updated_at = ?, receipt = NULL
                       WHERE key = ?""",
                    (now, now, action_key),
                )
                self._event(
                    case_id,
                    "action_retried",
                    {"key": action_key, "kind": kind, "attempt": existing["attempts"] + 1},
                )
                return "execute"

            self.db.execute(
                """INSERT INTO actions
                     (key, case_id, kind, fingerprint, status, attempted_at, updated_at)
                   VALUES(?, ?, ?, ?, 'attempted', ?, ?)""",
                (action_key, case_id, kind, digest, now, now),
            )
            self._event(case_id, "action_attempted", {"key": action_key, "kind": kind})
            return "execute"

    def confirm_action(
        self, case_id: str, token: str, action_key: str, readback: Readback
    ) -> None:
        """Record that an authoritative read showed the effect exists."""
        self._settle_action(case_id, token, action_key, readback, status="confirmed")

    def record_action_absent(
        self, case_id: str, token: str, action_key: str, readback: Readback
    ) -> None:
        """Record that an authoritative read showed the effect does **not** exist.

        This is the only safe way out of a ``"reconcile"`` verdict other than
        confirming it. It is a statement about the external system, made by an
        adapter that looked — not a way to clear an inconvenient attempt. Once
        recorded, :meth:`begin_action` will return ``"execute"`` again for the
        same key, as a new numbered attempt.
        """
        self._settle_action(case_id, token, action_key, readback, status="not_performed")

    def _settle_action(
        self,
        case_id: str,
        token: str,
        action_key: str,
        readback: Readback,
        *,
        status: str,
    ) -> None:
        if not isinstance(readback, Readback):
            raise ValidationError("settling an action requires an authoritative read-back")
        with self.transaction():
            self._leased(case_id, token)
            existing = self.db.execute(
                "SELECT * FROM actions WHERE key=?", (action_key,)
            ).fetchone()
            if existing is None or existing["case_id"] != case_id:
                raise ValidationError("unknown action for this case")
            try:
                readback.validate(self.clock(), after=existing["attempted_at"])
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            now = self.clock()
            self.db.execute(
                "UPDATE actions SET status = ?, receipt = ?, updated_at = ? WHERE key = ?",
                (status, encode_json(readback.as_dict()), now, action_key),
            )
            self._event(
                case_id,
                "action_confirmed" if status == "confirmed" else "action_absent",
                {"key": action_key, "readback": readback.as_dict()},
            )

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------

    def snapshot(self, destination: Path | str) -> Dict[str, Any]:
        """Write a consistent, disarmed snapshot and its manifest.

        Returns the manifest. Verify it later with :func:`verify_snapshot`.
        """
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValidationError("a snapshot must not overwrite the live ledger")
        return create_snapshot(
            self.db,
            destination,
            now=self.clock(),
            recovery_pause=RECOVERY_PAUSE_SECONDS,
        )

    @staticmethod
    def verify_snapshot(path: Path | str) -> Dict[str, Any]:
        """Read-only verification of a snapshot file. See :mod:`.snapshot`."""
        return verify_snapshot(path)
