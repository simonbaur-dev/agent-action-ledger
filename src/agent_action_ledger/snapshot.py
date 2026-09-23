"""Consistent, disarmed snapshots and read-only snapshot verification.

Backing up a live SQLite database by copying the file is a well-known way to
produce a corrupt backup: the write-ahead log is a separate file and the copy
can land between them. These helpers use the SQLite backup API instead, which
takes a transactionally consistent copy of a database that is being written to.

A snapshot taken here is also **disarmed** and **parked**:

* the write gate in the copy is set to disarmed;
* every lease in the copy is cleared;
* every open case in the copy is moved to ``waiting``, scheduled a little way
  out, with a reason telling whoever restores it to reconcile receipts first.

That is the difference between a backup of data and a backup of an actor's
state. Restoring an old ledger restores an old *belief* about the outside
world: cases that were finished after the snapshot look unfinished again, and
actions that were confirmed after the snapshot look unconfirmed. If such a
restore came back armed and due, the first worker to run would happily redo
external work that had already been done. Disarmed and parked means the restore
stops, and a person decides what is actually true before anything is armed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Dict

from .filelock import file_lock
from .schema import APP_ID, SCHEMA_VERSION, TABLES

__all__ = [
    "MAX_VERIFIED_SNAPSHOT_BYTES",
    "consistent_copy",
    "create_snapshot",
    "manifest_path",
    "verify_snapshot",
]

#: A snapshot larger than this is reported unverified rather than hashed. The
#: verifier must not be a way to make a process read an unbounded file.
MAX_VERIFIED_SNAPSHOT_BYTES = 256 * 1024 * 1024

RESTORE_REASON = (
    "Restored from a snapshot; reconcile authoritative receipts before resuming"
)


def manifest_path(destination: Path | str) -> Path:
    """Return the manifest path that belongs to a snapshot file."""
    destination = Path(destination)
    return destination.with_name(destination.name + ".manifest.json")


def consistent_copy(conn: sqlite3.Connection, destination: Path | str) -> Path:
    """Copy a live database to ``destination`` via the SQLite backup API.

    Written to a temporary name next to the destination and then renamed, so a
    half-written file is never what a restore picks up.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".tmp")
    out = sqlite3.connect(str(staging))
    try:
        conn.backup(out)
    finally:
        out.close()
    os.chmod(staging, 0o600)
    os.replace(staging, destination)
    return destination


def create_snapshot(
    conn: sqlite3.Connection,
    destination: Path | str,
    *,
    now: float,
    recovery_pause: float = 300.0,
) -> Dict[str, Any]:
    """Write a disarmed, parked snapshot of ``conn`` plus its manifest.

    Returns the manifest dictionary. The caller is responsible for making sure
    ``destination`` is not the live database.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(destination) + ".lock"):
        # The copy is disarmed while it is still private: no reader outside
        # this process ever sees a file that is both complete and armed.
        with tempfile.TemporaryDirectory(
            prefix=".ledger-snapshot-", dir=str(destination.parent)
        ) as private:
            os.chmod(private, 0o700)
            staging = Path(private) / "snapshot.sqlite"
            consistent_copy(conn, staging)
            copy = sqlite3.connect(str(staging))
            try:
                copy.execute("UPDATE meta SET value='0' WHERE key='writes_armed'")
                copy.execute(
                    "INSERT INTO gate_log(at, armed, note) VALUES(?, 0, ?)",
                    (now, "snapshot taken; restores start disarmed"),
                )
                copy.execute(
                    """UPDATE cases SET
                         lease_token = NULL, lease_until = NULL, lease_owner = NULL,
                         status = CASE WHEN status = 'human_decision'
                                       THEN status ELSE 'waiting' END,
                         reason = ?, next_check = ?, updated_at = ?
                       WHERE status != 'complete'""",
                    (RESTORE_REASON, now + recovery_pause, now),
                )
                copy.commit()
            finally:
                copy.close()
            os.chmod(staging, 0o600)
            os.replace(staging, destination)

        manifest = {
            "path": destination.name,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "bytes": destination.stat().st_size,
            "created_at": now,
            "schema": SCHEMA_VERSION,
            "consistent_sqlite": True,
            "writes_armed": False,
            "restore_requires": (
                "reconciliation of authoritative receipts for every unsettled "
                "action before the write gate is armed again"
            ),
        }
        target = manifest_path(destination)
        staged_manifest = target.with_name(target.name + ".tmp")
        staged_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(staged_manifest, target)
    return manifest


def verify_snapshot(path: Path | str) -> Dict[str, Any]:
    """Prove, read-only, that a snapshot is what a restore needs.

    Checks the manifest schema, the SHA-256 over the actual bytes, SQLite
    integrity, the application marker, the expected tables, the schema version,
    and that the snapshot is disarmed with no leases left behind.

    Returns ``{"ok": True, ...}`` or ``{"ok": False, "reason": ...}``. Nothing
    here writes to, repairs or arms the file, and anything unreadable or
    unrecognised is a failure rather than a shrug.

    Verify *before* opening a snapshot with :class:`~.ledger.ActionLedger`.
    Opening one switches it to WAL journalling, which writes to the file and
    therefore changes the bytes the manifest digest covers. Verify first, or
    copy the snapshot and open the copy.
    """
    path = Path(path)
    if not path.exists():
        return {"ok": False, "reason": "snapshot missing"}
    try:
        manifest = json.loads(manifest_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": f"manifest unreadable: {type(exc).__name__}"}
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("sha256"), str)
        or len(manifest["sha256"]) != 64
        or manifest.get("consistent_sqlite") is not True
        or manifest.get("writes_armed") is not False
        or not isinstance(manifest.get("created_at"), (int, float))
    ):
        return {"ok": False, "reason": "manifest schema invalid"}

    size = path.stat().st_size
    if size > MAX_VERIFIED_SNAPSHOT_BYTES:
        return {
            "ok": False,
            "reason": "snapshot exceeds the verification bound",
            "bytes": size,
        }
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        return {
            "ok": False,
            "reason": "snapshot bytes do not match the manifest digest",
            "bytes": size,
        }

    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
                return {"ok": False, "reason": "not an agent action ledger"}
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return {"ok": False, "reason": "SQLite integrity check failed"}
            tables = {
                row[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = set(TABLES) - tables
            if missing:
                return {
                    "ok": False,
                    "reason": "expected tables missing: " + ", ".join(sorted(missing)),
                }
            meta = dict(db.execute("SELECT key, value FROM meta"))
            if meta.get("schema") != SCHEMA_VERSION:
                return {"ok": False, "reason": "unsupported ledger schema"}
            if meta.get("writes_armed") != "0":
                return {"ok": False, "reason": "snapshot is not disarmed"}
            leased = db.execute(
                "SELECT count(*) FROM cases WHERE lease_token IS NOT NULL"
            ).fetchone()[0]
            if leased:
                return {"ok": False, "reason": "snapshot still carries active leases"}
            cases = db.execute("SELECT count(*) FROM cases").fetchone()[0]
            unsettled = db.execute(
                "SELECT count(*) FROM actions WHERE status = 'attempted'"
            ).fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error as exc:
        return {"ok": False, "reason": f"snapshot unreadable: {type(exc).__name__}"}

    return {
        "ok": True,
        "sha256": manifest["sha256"],
        "bytes": size,
        "cases": cases,
        "unsettled_actions": unsettled,
        "created_at": manifest["created_at"],
        "age_seconds": max(0.0, time.time() - float(manifest["created_at"])),
        "writes_armed": False,
    }
