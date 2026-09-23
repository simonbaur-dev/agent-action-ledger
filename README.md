# agent-action-ledger

A small, dependency-free transaction kernel for software agents that act on
real systems. It keeps a durable record of what an agent set out to do, what it
attempted, and what it can actually prove happened.

**It does not let an agent do anything.** Nothing in this package opens a
socket, sends a message or runs a command. It is the bookkeeping underneath an
agent, not the agent.

---

## What problem this solves

Suppose you have a program that reads a queue of work and does something about
each item: replies to a support ticket, files a document, updates a record in
some other system. The interesting part is not the happy path. It is what
happens when the boring things go wrong.

The program sends a reply, and the machine loses power before it can write down
that it did. It restarts. It sees an unanswered ticket. It replies again.

Or: you run two copies of the program to get through the queue faster, and both
pick up the same item within the same second.

Or: the program crashes halfway through a call to another system, and nobody —
not the program, not you — can say whether that call landed.

Or: you restore last week's backup after a disk failure, and the program
cheerfully redoes a week of work that was already done.

Each of these is a *duplicate action in the real world*, and in most systems a
duplicate action is not a cosmetic bug. It is a second refund, a second order, a
second email to a person who already received the first one.

This library is the piece that makes those situations boring:

- every unit of work has a **stable identity**, so seeing it twice does not
  create it twice;
- a worker **leases** a case for a bounded time, so two workers never hold the
  same one;
- an attempt to act is written down **before** the call is made, so a crash
  leaves a question ("did this land?") rather than a silent gap;
- an action is only ever marked done against **evidence from looking at the
  other system**, never because the code returned successfully;
- backups come back **switched off**, because restoring old state restores an
  old belief about the world, and acting on it would redo real work.

None of this is clever. All of it is the kind of thing that is obvious in
hindsight, at three in the morning, after it has already gone wrong once.

---

## The authority boundary

This is the most important thing in the repository, so it gets its own section.

**The ledger records and coordinates actions. It does not grant authority to
perform them, and it cannot tell whether one happened.**

Concretely:

- The ledger has no capability to reach any external system. Whatever your
  agent is allowed to do, it is allowed to do because of the credentials and
  the code *you* gave it, not because a ledger exists.
- When the ledger says an action is `confirmed`, it means: *a piece of your
  adapter code constructed a `Readback` saying it observed the effect.* The
  ledger checks that the evidence is fresh, is newer than the attempt, and is
  well-formed. It cannot check that it is **true**. That is your adapter's job,
  and it is the job that matters.
- Therefore: never build a `Readback` from a model's output, from the return
  value of the call you are trying to verify, or from anything other than a
  genuine authoritative read of the system you are asking about. A model that
  can write its own receipts can close any case it likes without doing the
  work.
- The write gate (`arm` / `disarm`) is an *operator* control. If the same
  component that decides to act can also arm the gate, there is no gate.

The ledger's contribution is that it makes the boundary visible and forces the
question to be answered explicitly, in a place you can audit afterwards.

---

## Architecture

One SQLite file. One connection per worker. No server, no daemon, no network.

```
                  register()                claim()
 upstream source ───────────▶  ┌──────────┐ ───────▶  worker (your code)
 (poll, webhook,              │  cases   │              │
  queue, inbox)               ├──────────┤              │ begin_action()
                              │  events  │ ◀────────────┤ ...external call...
                              ├──────────┤   append     │ confirm_action()
                              │ actions  │   only       │ finish()
                              ├──────────┤              ▼
                              │ gate_log │        adapter reads the
                              └──────────┘        external system and
                                   │              produces a Readback
                            snapshot() │
                                   ▼
                         disarmed, parked copy
                          + sha256 manifest
```

### Cases

A case is one unit of work. Its identity is derived from
`(namespace, kind, source_id)` with SHA-256, so the same upstream item always
maps to the same case, in any process, on any machine, forever. Registering an
item you have already seen is a no-op; registering it with new data updates the
payload and wakes a waiting case, but never reopens a finished one.

Four states:

| state | meaning |
| --- | --- |
| `pending` | ready to be worked on when `next_check` comes due |
| `waiting` | blocked on something else; will be looked at again |
| `human_decision` | parked on a question only a person can answer |
| `complete` | finished, with an authoritative read-back on record |

Ordinary workers claim `pending` and `waiting` cases by default. A
`human_decision` case stays parked until a person resumes it, unless a worker
explicitly opts into claiming that state.

Every open case must have a `next_check`. Completed cases must not have one.
Both are enforced by the database, not just by the Python code, so nothing can
quietly fall off the end of the world.

### Leases

`claim()` hands out time-bounded leases inside a single immediate transaction.
Two workers racing at the same instant cannot both get the same case, and a
shared ceiling (`max_concurrent_leases`) holds across processes.

When a worker dies, its lease expires. The case is **not** handed straight to
the next worker: it is parked for five minutes with a reason saying to check
the receipts first. The dead worker might have got as far as making an external
call. Handing the case on immediately is exactly how you get the duplicate.

The dead worker is also fenced out. If it comes back to life holding its old
token, every operation raises `LeaseLost`.

### Events

Every state change appends to an event log. The log is append-only, enforced by
SQLite triggers — you cannot rewrite history even from a `sqlite3` shell.

### Actions and receipts

An action is one intended external effect, identified by an idempotency key you
choose. The request is fingerprinted, so reusing a key for a different request
is refused rather than silently accepted.

`begin_action()` returns one of three verdicts:

| verdict | what it means | what to do |
| --- | --- | --- |
| `execute` | nothing has been attempted under this key | make the call, then confirm |
| `confirmed` | this exact action already happened and was verified | nothing |
| `reconcile` | an attempt was recorded and never settled | **go and look**; do not repeat it |

The attempt row is committed *before* the external call. That deliberately
accepts the opposite failure — an attempt recorded for a call that never left
the process — because the recoverable outcome of the two is "look before you
act again", not "act again and hope".

A `reconcile` verdict has exactly two honest exits, and both require an
authoritative read: `confirm_action()` if the effect is there, or
`record_action_absent()` if a read shows it genuinely is not, after which a
retry is a new numbered attempt.

### The write gate

New ledgers are disarmed. So are restored snapshots. While disarmed,
`begin_action()` raises `WritesDisarmed` and no action can be attempted.
Arming requires a note saying who verified what, and every arm and disarm is
kept in an append-only log.

### Snapshots

`snapshot()` uses the SQLite backup API (copying a live SQLite file is a
well-known way to produce a corrupt backup) and writes a manifest with a SHA-256
over the bytes. The copy is **disarmed** and its open cases are **parked**,
because restoring an old ledger restores an old belief about the outside world.
`verify_snapshot()` re-checks all of that read-only, and fails on anything it
cannot positively confirm.

---

## Quickstart

Requires Python 3.9 or newer. No other runtime dependencies.

```bash
git clone https://github.com/simonbaur-dev/agent-action-ledger.git
cd agent-action-ledger

python -m venv .venv
# Linux/macOS:  source .venv/bin/activate
# Windows:      .venv\Scripts\activate

pip install -e ".[dev]"
```

Run the tests:

```bash
pytest
# or, with no dev dependencies at all:
python -m unittest discover
```

Run the worked example — two workers, one parked decision, one confirmed
action, a snapshot and its verification, all on a fake in-memory support desk:

```bash
agent-ledger example
# or
python examples/support_desk.py
```

Keep the demo ledger around and poke at it:

```bash
agent-ledger example --directory ./demo-run
agent-ledger status   ./demo-run/ledger.sqlite
agent-ledger list     ./demo-run/ledger.sqlite
agent-ledger gate-log ./demo-run/ledger.sqlite
agent-ledger verify   ./demo-run/snapshots/ledger.sqlite
```

### In your own code

```python
import time
from agent_action_ledger import ActionLedger, Readback

ledger = ActionLedger("ledger.sqlite")

# 1. Register what exists. Safe to call on every poll.
for ticket in source.list_open_tickets():
    ledger.register(
        "support", "ticket", ticket["id"],
        owner="triage-bot",
        payload={"subject": ticket["subject"]},
    )

# 2. Take work, for a bounded time.
for case in ledger.claim(worker="worker-a", limit=2, lease_seconds=300):
    token = case["lease_token"]
    key = f"reply:{case['source_id']}"

    verdict = ledger.begin_action(
        case["id"], token, key,
        kind="reply", request={"ticket": case["source_id"], "template": "welcome"},
    )

    if verdict == "reconcile":
        # Something was attempted and never settled. Look, do not repeat.
        ledger.finish(
            case["id"], token, status="human_decision",
            reason="an earlier attempt was never confirmed",
            next_check=time.time() + 3600,
            question="Did the earlier reply go out? Check the desk and confirm.",
        )
        continue

    if verdict == "execute":
        reply_id = desk.post_reply(case["source_id"], "...")

        # 3. Evidence comes from looking, not from the call returning.
        record = desk.read_ticket(case["source_id"])
        if not any(r["id"] == reply_id for r in record["replies"]):
            raise RuntimeError("the desk does not show the reply")

        ledger.confirm_action(case["id"], token, key, Readback(
            source="support-desk",
            record_id=reply_id,
            observed_at=time.time(),
            assertion=f"reply {reply_id} is present on {case['source_id']}",
        ))

    # 4. Complete, against a fresh read-back.
    ledger.finish(
        case["id"], token, status="complete",
        reason="the customer has a reply",
        readback=Readback("support-desk", case["source_id"], time.time(),
                          "the ticket shows a reply"),
    )
```

Before any of that will work, an operator has to open the gate once:

```bash
agent-ledger arm ledger.sqlite --note "checked: no outstanding attempts"
```

### CLI

| command | what it does |
| --- | --- |
| `init` | create a ledger |
| `status` | counts, gate state, unsettled attempts |
| `list` | list cases, optionally filtered |
| `show` | one case with its actions and history |
| `history` | the event history of a case |
| `register` | register a case by hand |
| `resume` | record a person's answer to a parked case |
| `arm` / `disarm` | open or close the write gate (refuses to arm over unsettled attempts unless forced) |
| `gate-log` | every arming and disarming, with notes |
| `snapshot` | write a disarmed snapshot and manifest |
| `verify` | verify a snapshot, read-only |
| `example` | run the bundled example |

There is deliberately **no** command that confirms an action or completes a
case. A receipt typed at a terminal is not evidence; it is a person asserting
what an adapter was supposed to have seen.

---

## Limitations

Please read these before adopting it.

- **Single machine.** SQLite plus an OS file lock. It coordinates processes on
  one host. It is not a distributed lock manager and gives no guarantees over
  NFS, SMB or any other network filesystem.
- **Not a queue or a scheduler.** There is no broker, no fan-out, no
  at-least-once delivery machinery, no cron. `next_check` says when a case
  becomes eligible; something in your program still has to call `claim()`.
- **Not a workflow engine.** There are no DAGs, no step retries with backoff
  policies, no compensation logic. Four states and an event log.
- **Throughput is modest by design.** `claim()` re-validates every stored row
  by default, which is a superb tripwire and a poor idea at a million cases.
  Pass `validate_on_claim=False` when you outgrow it, and know what you gave up.
- **`Readback` freshness uses wall-clock time.** A large clock jump between the
  adapter and the ledger will be rejected as stale rather than silently
  accepted. That is the intended failure direction, but it is a real
  constraint.
- **Confirmation is only as honest as your adapter.** See
  [the authority boundary](#the-authority-boundary). The ledger enforces the
  *shape* of evidence, never its truth.
- **Verify a snapshot before you open it.** Opening a ledger switches it to WAL
  journalling, which writes to the file and so changes the bytes the manifest
  digest covers. `agent-ledger verify` first, then open — or open a copy.
- **Payloads are not encrypted.** The file is created mode `0600` on POSIX;
  Windows ACLs are left to the system. See [SECURITY.md](SECURITY.md).
- **One connection per worker, and instances are not thread-safe.** Give each
  thread or process its own `ActionLedger` on the same file. That is the
  supported concurrency model and the one the tests exercise.
- **Version 0.1.** The schema carries a version marker and an application id,
  but there is no migration tooling yet.

---

## Development

```bash
pip install -e ".[dev]"
pytest              # the full suite
ruff check .        # lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: the invariants in
`schema.py` and `ledger.py` are the product. Changes that simplify an
integration are welcome; changes that weaken an invariant need to explain, in
the pull request, which failure the invariant was there to prevent and why that
failure no longer matters.

## License

MIT. See [LICENSE](LICENSE).
