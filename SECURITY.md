# Security

## A ledger file is sensitive. Do not commit one.

This is the single most important line in this document.

A ledger is not a cache and not a log of internal chatter. It is a record of
what an automated system did to *other* systems, and it accumulates exactly the
metadata an attacker would otherwise have to work for:

- **payloads** — whatever you put in them: record identifiers, subjects,
  amounts, names, references into other systems;
- **source identifiers** — which tickets, invoices, documents or accounts exist
  at all, and how they are numbered;
- **owners and worker names** — your internal component and host naming;
- **action keys and receipts** — which external systems you talk to, what you
  do there, what succeeded, and when;
- **the event log** — a timestamped trace of your operational behaviour,
  including when you were down, when something crashed, and how often a person
  had to intervene;
- **the write-gate log** — who armed the system and what they said about it.

Aggregated over a few months, that is a map of your operations.

### Practical rules

1. **Never commit `*.sqlite`, `*.db`, their `-wal`/`-shm` sidecars, or a
   snapshot manifest.** `.gitignore` in this repository already excludes them,
   and `tests/test_repository_hygiene.py` fails the build if one appears.
2. **Keep ledgers out of the repository tree entirely.** Put them in a data
   directory outside your checkout. A file that is not in the tree cannot be
   committed by an autocompleted `git add`.
3. **Treat snapshots like the ledger.** A snapshot is disarmed, not redacted.
   It contains every payload and every receipt.
4. **Do not paste ledger output into issues.** `agent-ledger show` and
   `--json` print payloads verbatim. Redact before sharing.
5. **Put nothing in a payload that you would not want in a backup.** Secrets,
   tokens, full documents and personal data do not belong there. Store a
   reference and resolve it at use time.

### File permissions

The ledger, its WAL and SHM sidecars, and snapshots are created with mode
`0600` on POSIX systems, and snapshots are staged inside a `0700` temporary
directory so that no partial file is ever world-readable.

On Windows, `os.chmod` cannot express this, and the files inherit the ACL of
their parent directory. **If you run on Windows, place ledgers in a directory
whose ACL you have set deliberately.** The library cannot do this for you.

Encryption at rest is not provided. If you need it, use an encrypted volume or
filesystem.

## Threat model

### What the library does defend against

- **Duplicate external effects** caused by crashes, restarts, concurrent
  workers, expired leases, replayed evidence or restored backups. This is the
  reason the library exists; see the README section on the authority boundary.
- **Silent corruption of its own state.** Invariants are enforced by SQLite
  `CHECK` constraints and triggers as well as in Python, the file carries an
  application marker, and a ledger that cannot prove it is consistent refuses
  to hand out work rather than guessing.
- **History being rewritten.** The event log and the write-gate log are
  append-only by trigger, and action receipts cannot be deleted — including
  from a direct `sqlite3` session.
- **A partially written or tampered snapshot being restored.**
  `verify_snapshot()` checks a SHA-256 over the actual bytes, SQLite integrity,
  the application marker, the schema version, and that the copy is disarmed and
  lease-free. Anything unreadable or unrecognised is a failure, not a shrug.

### What it explicitly does not defend against

- **An adapter that lies.** The ledger validates the *shape* and *freshness* of
  a `Readback`, never its truth. If your adapter constructs evidence without
  actually reading the external system — or worse, from model output — the
  ledger will faithfully record a confirmed action that never happened. Build
  `Readback` objects only in code you trust, only after an authoritative read.
- **An agent that can arm its own write gate.** `arm_writes()` is an operator
  control. Exposing it to the component that decides whether to act removes the
  control entirely.
- **Anyone with write access to the file.** SQLite offers no access control. A
  process that can write the file can damage it; the constraints and triggers
  make that hard to do *quietly*, not impossible to do.
- **Untrusted multi-tenancy.** One ledger is one trust domain. Do not share a
  ledger between parties who should not see each other's payloads.
- **Untrusted input to `register()`.** Namespaces and kinds are restricted to
  short lowercase slugs, and payloads must be JSON-encodable objects, but the
  library does not sanitise payload *content*. Do not render payloads into
  HTML, shell commands or SQL elsewhere in your system without escaping.
- **Denial of service.** An attacker who can call `register()` freely can grow
  the file without bound.

## Reporting a vulnerability

Please report suspected vulnerabilities privately, through this repository's
GitHub Security Advisories ("Report a vulnerability" under the Security tab),
rather than in a public issue.

Useful reports include: the version, a description of the invariant you believe
is broken, and — ideally — a failing test in the style of `tests/`. A
reproduction that shows a duplicate external effect becoming possible, or a
`complete`/`confirmed` state reachable without a valid read-back, will get
attention fastest.

Please do not include real ledger files, payloads or operational data in a
report. A synthetic reproduction is more useful and safer for both of us.
