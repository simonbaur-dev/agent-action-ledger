"""A runnable end-to-end example on a fictional support desk.

Everything here is synthetic. :class:`FakeSupportDesk` is a dictionary
pretending to be a ticketing system; no network, no files outside the directory
you pass in, no credentials, no real service of any kind.

The example is the honest shape of an integration, not a toy: the ledger never
learns anything about the desk except through an adapter that *looked*, and the
adapter is the only thing allowed to say what happened out there.

Run it with ``agent-ledger example`` or ``python examples/support_desk.py``.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

from .errors import WritesDisarmed
from .ledger import ActionLedger
from .readback import Readback

__all__ = ["FakeSupportDesk", "run"]

NAMESPACE = "support"
KIND = "ticket"


class FakeSupportDesk:
    """An in-memory stand-in for whatever external system you actually use.

    Two kinds of method, and the distinction is the whole point:

    * :meth:`post_reply` *changes* the outside world;
    * :meth:`read_ticket` *observes* it.

    Only the second one is allowed to produce evidence. In a real adapter
    ``read_ticket`` would be an authenticated API call to the vendor, and it
    would be the same call whether or not this process believes it succeeded.
    """

    def __init__(self) -> None:
        self.tickets: Dict[str, Dict[str, Any]] = {
            "T-1001": {
                "subject": "Password reset link expired",
                "state": "open",
                "replies": [],
            },
            "T-1002": {
                "subject": "Refund for duplicate charge",
                "state": "open",
                "replies": [],
            },
            "T-1003": {
                "subject": "Feature request: dark mode",
                "state": "awaiting_customer",
                "replies": [],
            },
        }
        self.calls: List[str] = []

    def list_open_tickets(self) -> List[Dict[str, Any]]:
        return [
            {"id": ticket_id, **{k: v for k, v in record.items() if k != "replies"}}
            for ticket_id, record in sorted(self.tickets.items())
        ]

    def post_reply(self, ticket_id: str, body: str) -> str:
        """The external side effect. The ledger never calls this itself."""
        self.calls.append(f"post_reply({ticket_id})")
        reply_id = f"R-{ticket_id}-{len(self.tickets[ticket_id]['replies']) + 1}"
        self.tickets[ticket_id]["replies"].append({"id": reply_id, "body": body})
        return reply_id

    def read_ticket(self, ticket_id: str) -> Dict[str, Any]:
        """The authoritative read. This is what evidence is made of."""
        self.calls.append(f"read_ticket({ticket_id})")
        return {"id": ticket_id, **self.tickets[ticket_id]}


def readback_for_reply(desk: FakeSupportDesk, ticket_id: str, reply_id: str) -> Optional[Readback]:
    """Adapter: look at the desk, and report what is actually there.

    Returns a read-back only when the reply is genuinely present. Note what is
    *not* consulted: whether ``post_reply`` returned, whether it raised, what
    the agent thinks it did. The adapter's job is to make the ledger's belief
    match the external system, which it can only do by asking the system.
    """
    record = desk.read_ticket(ticket_id)
    present = any(reply["id"] == reply_id for reply in record["replies"])
    if not present:
        return None
    return Readback(
        source="support-desk",
        record_id=reply_id,
        observed_at=time.time(),
        assertion=f"reply {reply_id} is present on ticket {ticket_id}",
    )


def run(directory: Path | str | None = None, echo: Callable[[str], Any] = print) -> Dict[str, Any]:
    """Run the whole example and return a small result summary.

    ``directory`` defaults to a temporary directory that is removed afterwards.
    Pass one in to keep the ledger and snapshot around and poke at them with
    ``agent-ledger status`` / ``agent-ledger history``.
    """
    if directory is None:
        with tempfile.TemporaryDirectory(prefix="agent-ledger-demo-") as tmp:
            return _run_in(Path(tmp), echo)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return _run_in(directory, echo)


def _heading(echo: Callable[[str], Any], text: str) -> None:
    echo("")
    echo(text)
    echo("-" * len(text))


def _run_in(directory: Path, echo: Callable[[str], Any]) -> Dict[str, Any]:
    desk = FakeSupportDesk()
    path = directory / "ledger.sqlite"
    now = time.time()

    # Each worker gets its own connection to the same file, exactly as two
    # processes would. Nothing below depends on them sharing memory.
    supervisor = ActionLedger(path)
    worker_a = ActionLedger(path)
    worker_b = ActionLedger(path)

    try:
        _heading(echo, "1. Poll the desk and register what exists")
        schedule = {"T-1001": now, "T-1002": now + 1, "T-1003": now + 3600}
        for ticket in desk.list_open_tickets():
            case = supervisor.register(
                NAMESPACE,
                KIND,
                ticket["id"],
                owner="support-triage",
                payload={"subject": ticket["subject"], "state": ticket["state"]},
                next_check=schedule[ticket["id"]],
            )
            echo(f"   {ticket['id']:>7} -> {case}")
        # Polling again is a no-op: identity comes from the ticket, not the poll.
        for ticket in desk.list_open_tickets():
            supervisor.register(
                NAMESPACE, KIND, ticket["id"], owner="support-triage",
                payload={"subject": ticket["subject"], "state": ticket["state"]},
                next_check=schedule[ticket["id"]],
            )
        echo(f"   polled twice, cases in ledger: {supervisor.stats()['cases']}")

        _heading(echo, "2. The write gate starts disarmed")
        probe = worker_a.claim(worker="probe", limit=1)[0]
        try:
            worker_a.begin_action(
                probe["id"], probe["lease_token"], "probe", kind="reply", request={}
            )
            echo("   UNEXPECTED: a disarmed ledger allowed an action")
        except WritesDisarmed as exc:
            echo(f"   refused as designed: {exc}")
        worker_a.finish(
            probe["id"], probe["lease_token"],
            status="pending", reason="probe only", next_check=time.time() + 0.2,
        )
        supervisor.arm_writes("demo run: verified there are no outstanding receipts")
        echo("   operator armed writes, with a note on the record")

        _heading(echo, "3. Two workers claim disjoint work")
        time.sleep(1.1)  # the first two synthetic tickets are now due
        taken_a = worker_a.claim(worker="worker-a", limit=1)
        taken_b = worker_b.claim(worker="worker-b", limit=1)
        for label, taken in (("worker-a", taken_a), ("worker-b", taken_b)):
            for case in taken:
                echo(f"   {label} holds {case['source_id']} ({case['id']})")
        assert {c["id"] for c in taken_a}.isdisjoint({c["id"] for c in taken_b})

        # Work the cases in a fixed order regardless of which worker got which,
        # so the example reads the same on every run.
        held = {case["source_id"]: (ledger, case)
                for ledger, taken in ((worker_a, taken_a), (worker_b, taken_b))
                for case in taken}

        _heading(echo, "4. One attempted action, confirmed by reading the desk")
        ledger, case = _ensure_held(held, "T-1001", worker_a, "worker-a")
        action_key = f"reply:{case['source_id']}"
        verdict = ledger.begin_action(
            case["id"], case["lease_token"], action_key,
            kind="reply", request={"ticket": case["source_id"], "template": "reset-link"},
        )
        echo(f"   begin_action -> {verdict}")
        reply_id = None
        if verdict == "execute":
            reply_id = desk.post_reply(case["source_id"], "Here is a fresh reset link.")
            echo(f"   external call made, desk returned {reply_id}")
        receipt = readback_for_reply(desk, case["source_id"], reply_id)
        if receipt is None:
            raise AssertionError("the desk does not show the reply; nothing to confirm")
        ledger.confirm_action(case["id"], case["lease_token"], action_key, receipt)
        echo(f"   confirmed against a fresh read: {receipt.assertion}")

        repeat = ledger.begin_action(
            case["id"], case["lease_token"], action_key,
            kind="reply", request={"ticket": case["source_id"], "template": "reset-link"},
        )
        echo(f"   asking to do it again -> {repeat} (no second call is made)")

        closing = readback_for_reply(desk, case["source_id"], reply_id)
        ledger.finish(
            case["id"], case["lease_token"],
            status="complete", reason="customer was sent a working reset link",
            readback=closing,
        )
        echo("   case completed, with the read-back on record")

        _heading(echo, "5. One case a person has to decide")
        ledger, case = _ensure_held(held, "T-1002", worker_b, "worker-b")
        ledger.finish(
            case["id"], case["lease_token"],
            status="human_decision",
            reason="refund amount is above the automated limit",
            next_check=time.time() + 3600,
            question="Refund the duplicate charge in full, or only the difference?",
        )
        parked = supervisor.get(case["id"])
        echo(f"   parked: {parked['question']}")
        answered = supervisor.resume_human_decision(
            case["id"],
            reason="operator answered the refund question",
            answer="Refund in full.",
            next_check=time.time() + 5,
        )
        echo(f"   answered -> back to {answered['status']} (not to complete)")

        _heading(echo, "6. History is append-only and reads like a story")
        completed = supervisor.cases(status="complete")[0]
        for event in supervisor.history(completed["id"]):
            echo(f"   {event['kind']}")

        _heading(echo, "7. Snapshot, and verify it")
        snapshot_path = directory / "snapshots" / "ledger.sqlite"
        manifest = supervisor.snapshot(snapshot_path)
        report = ActionLedger.verify_snapshot(snapshot_path)
        echo(f"   wrote {snapshot_path.name} ({manifest['bytes']} bytes)")
        echo(f"   sha256 {manifest['sha256'][:16]}...")
        echo(
            f"   verify -> ok={report['ok']} cases={report['cases']} "
            f"armed={report['writes_armed']}"
        )

        restored = ActionLedger(snapshot_path)
        try:
            echo(f"   restored copy: writes_armed={restored.writes_armed()}")
            for restored_case in restored.cases():
                if restored_case["status"] != "complete":
                    echo(
                        f"   restored copy parked {restored_case['source_id']} "
                        f"as {restored_case['status']}"
                    )
        finally:
            restored.close()

        _heading(echo, "8. Where things stand")
        stats = supervisor.stats()
        for key in (
            "cases", "by_status", "due_now", "leased_now",
            "actions", "unsettled_actions", "events", "writes_armed",
        ):
            echo(f"   {key}: {stats[key]}")
        echo("")
        echo(f"   calls made to the desk: {len(desk.calls)}")
        echo(f"   of those, writes: {sum(1 for c in desk.calls if c.startswith('post_reply'))}")

        return {
            "path": str(path),
            "snapshot": str(snapshot_path),
            "manifest": manifest,
            "verification": report,
            "stats": stats,
            "desk_calls": list(desk.calls),
        }
    finally:
        for ledger in (worker_b, worker_a, supervisor):
            ledger.close()


def _ensure_held(held, source_id, fallback_ledger, fallback_worker):
    """Return (ledger, case) for ``source_id``, claiming it if nobody has it."""
    if source_id in held:
        return held.pop(source_id)
    taken = fallback_ledger.claim(worker=fallback_worker, limit=1)
    for case in taken:
        held[case["source_id"]] = (fallback_ledger, case)
    if source_id not in held:
        raise AssertionError(f"expected {source_id} to be claimable")
    return held.pop(source_id)
