"""The SQLite schema, and the invariants it enforces in the database itself.

Constraints live in the schema rather than only in Python, because a constraint
in application code protects the code paths you remembered, while a CHECK
protects the file. Anything that opens this database with the ``sqlite3`` CLI
still cannot write a case that is complete *and* leased.

The event log is append-only, enforced by triggers: history can be added to but
never rewritten, including by a direct SQL session.
"""
from __future__ import annotations

__all__ = ["APP_ID", "SCHEMA_VERSION", "SCHEMA", "STATES", "ACTION_STATES", "TABLES"]

#: ``PRAGMA application_id`` marker, ASCII "AAL1". Opening a SQLite file that
#: does not carry it fails closed rather than silently writing cases into some
#: unrelated database.
APP_ID = 1094798385

SCHEMA_VERSION = "1"

#: Case lifecycle states.
#:
#: ``pending``         ready to be worked on when ``next_check`` comes due
#: ``waiting``         blocked on something else; retried after ``next_check``
#: ``human_decision``  parked on a question only a person can answer
#: ``complete``        finished, with an authoritative read-back on record
STATES = ("pending", "waiting", "human_decision", "complete")

#: Action lifecycle states.
#:
#: ``attempted``      an external call was started; nobody knows the outcome
#: ``confirmed``      an authoritative read-back showed the effect exists
#: ``not_performed``  an authoritative read-back showed the effect is absent
ACTION_STATES = ("attempted", "confirmed", "not_performed")

TABLES = ("meta", "cases", "events", "actions", "gate_log")

SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE cases (
  id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL CHECK(length(trim(namespace)) > 0 AND length(namespace) <= 64),
  kind TEXT NOT NULL CHECK(length(trim(kind)) > 0 AND length(kind) <= 64),
  source_id TEXT NOT NULL CHECK(length(trim(source_id)) > 0),
  owner TEXT NOT NULL CHECK(length(trim(owner)) > 0),
  status TEXT NOT NULL CHECK(status IN ('pending','waiting','human_decision','complete')),
  payload TEXT NOT NULL CHECK(length(trim(payload)) > 0),
  next_check REAL,
  reason TEXT NOT NULL DEFAULT '',
  question TEXT NOT NULL DEFAULT '',
  lease_token TEXT,
  lease_until REAL,
  lease_owner TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  transient_retries INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,

  -- One case per upstream item. This is what makes re-registration idempotent.
  UNIQUE(namespace, kind, source_id),

  -- A lease is all three columns or none of them, and a finished case is never
  -- leased: otherwise a dead worker could still appear to own settled work.
  CHECK((lease_token IS NULL AND lease_until IS NULL AND lease_owner IS NULL)
        OR (length(trim(lease_token)) > 0 AND lease_until IS NOT NULL
            AND length(trim(lease_owner)) > 0 AND status != 'complete')),

  -- Open work is always scheduled; complete work is never scheduled. A case
  -- can therefore not be silently dropped by losing its next check.
  CHECK((status = 'complete' AND next_check IS NULL)
        OR (status != 'complete' AND next_check IS NOT NULL)),

  -- Parking a case on a human requires stating the question being asked.
  CHECK(status != 'human_decision' OR length(trim(question)) > 0),

  CHECK(attempts >= 0 AND transient_retries >= 0),
  CHECK(updated_at >= created_at)
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id),
  kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
  at REAL NOT NULL,
  detail TEXT NOT NULL
);

CREATE TABLE actions (
  key TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id),
  kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
  fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64),
  status TEXT NOT NULL CHECK(status IN ('attempted','confirmed','not_performed')),
  attempted_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts >= 1),
  receipt TEXT,
  updated_at REAL NOT NULL,

  -- A settled action always carries the read-back that settled it.
  CHECK(status = 'attempted' OR receipt IS NOT NULL),
  CHECK(updated_at >= attempted_at)
);

CREATE TABLE gate_log (
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  armed INTEGER NOT NULL CHECK(armed IN (0, 1)),
  note TEXT NOT NULL CHECK(length(trim(note)) > 0)
);

CREATE INDEX due_cases ON cases(status, next_check, lease_until);
CREATE INDEX case_events ON events(case_id, id);
CREATE INDEX case_actions ON actions(case_id, status);

-- History is append-only. Not "by convention": by trigger.
CREATE TRIGGER events_immutable_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'event history is append-only'); END;

CREATE TRIGGER events_immutable_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'event history is append-only'); END;

CREATE TRIGGER gate_log_immutable_update BEFORE UPDATE ON gate_log
BEGIN SELECT RAISE(ABORT, 'write-gate history is append-only'); END;

CREATE TRIGGER gate_log_immutable_delete BEFORE DELETE ON gate_log
BEGIN SELECT RAISE(ABORT, 'write-gate history is append-only'); END;

-- Receipts may be settled, never erased: deleting one would turn a known
-- outcome back into an unknown one.
CREATE TRIGGER actions_receipts_kept BEFORE DELETE ON actions
BEGIN SELECT RAISE(ABORT, 'action receipts cannot be deleted'); END;
"""
