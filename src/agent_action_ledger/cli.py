"""Command line interface: ``agent-ledger``.

Deliberately limited to what is safe for a person at a terminal to do:
inspect the ledger, register work, answer a parked question, arm or disarm the
write gate, take and verify snapshots. There is no command that performs an
external action or confirms a receipt, because a receipt typed by hand is not
evidence — it is a person asserting what an adapter is supposed to have seen.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Dict, Optional, Sequence

from . import __version__
from .errors import LedgerError
from .ledger import ActionLedger
from .snapshot import verify_snapshot

__all__ = ["main", "build_parser"]


def _emit(value: Any, *, as_json: bool, stream=None) -> None:
    stream = stream or sys.stdout
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True, default=str), file=stream)
    elif isinstance(value, str):
        print(value, file=stream)
    else:
        print(json.dumps(value, indent=2, sort_keys=True, default=str), file=stream)


def _stamp(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))


def _open(args: argparse.Namespace) -> ActionLedger:
    return ActionLedger(Path(args.path), validate_on_claim=not args.no_validate)


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    existed = path.exists()
    ledger = ActionLedger(path, max_concurrent_leases=args.max_concurrent_leases)
    try:
        summary = {
            "path": str(path.resolve()),
            "created": not existed,
            "schema": "1",
            "writes_armed": ledger.writes_armed(),
            "max_concurrent_leases": ledger.max_concurrent_leases,
        }
    finally:
        ledger.close()
    if args.json:
        _emit(summary, as_json=True)
    else:
        verb = "created" if summary["created"] else "opened existing"
        _emit(f"{verb} ledger at {summary['path']}", as_json=False)
        _emit(
            f"writes armed: {summary['writes_armed']} | "
            f"max concurrent leases: {summary['max_concurrent_leases']}",
            as_json=False,
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        stats = ledger.stats()
        unsettled = ledger.unsettled_actions()
    finally:
        ledger.close()
    if args.json:
        _emit({**stats, "unsettled": unsettled}, as_json=True)
        return 0
    lines = [
        f"ledger           {stats['path']}",
        f"writes armed     {stats['writes_armed']}",
        f"cases            {stats['cases']}",
    ]
    for status, count in stats["by_status"].items():
        lines.append(f"  {status:<14} {count}")
    lines += [
        f"due now          {stats['due_now']}",
        f"leased now       {stats['leased_now']} of {stats['max_concurrent_leases']}",
        f"overdue > 1h     {stats['overdue_over_an_hour']}",
        f"actions          {stats['actions']} ({stats['unsettled_actions']} unsettled)",
        f"events           {stats['events']}",
    ]
    for action in unsettled:
        lines.append(
            f"  unsettled: {action['key']} on {action['case_id']} "
            f"since {_stamp(action['attempted_at'])}"
        )
    _emit("\n".join(lines), as_json=False)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        cases = ledger.cases(
            status=args.status,
            namespace=args.namespace,
            kind=args.kind,
            limit=args.limit,
        )
    finally:
        ledger.close()
    if args.json:
        _emit(cases, as_json=True)
        return 0
    if not cases:
        _emit("no cases match", as_json=False)
        return 0
    lines = [f"{'STATUS':<15} {'NEXT CHECK':<20} {'OWNER':<18} SOURCE / ID"]
    for case in cases:
        lines.append(
            f"{case['status']:<15} {_stamp(case['next_check']):<20} "
            f"{case['owner'][:18]:<18} {case['source_id']}  {case['id']}"
        )
    _emit("\n".join(lines), as_json=False)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        case = ledger.require(args.case_id)
        events = ledger.history(args.case_id)
        actions = [
            ledger.action(row["key"])
            for row in ledger.db.execute(
                "SELECT key FROM actions WHERE case_id=? ORDER BY attempted_at",
                (args.case_id,),
            )
        ]
    finally:
        ledger.close()
    if args.json:
        _emit({"case": case, "actions": actions, "events": events}, as_json=True)
        return 0
    lines = [
        f"id          {case['id']}",
        f"source      {case['namespace']}/{case['kind']}/{case['source_id']}",
        f"owner       {case['owner']}",
        f"status      {case['status']}",
        f"reason      {case['reason'] or '-'}",
        f"question    {case['question'] or '-'}",
        f"next check  {_stamp(case['next_check'])}",
        f"lease       {case['lease_owner'] or '-'} until {_stamp(case['lease_until'])}",
        f"payload     {json.dumps(case['payload'], sort_keys=True)}",
    ]
    if actions:
        lines.append("actions")
        for action in actions:
            lines.append(
                f"  {action['status']:<14} {action['key']} "
                f"(attempt {action['attempts']}, {action['kind']})"
            )
    lines.append("history")
    for event in events:
        lines.append(f"  {_stamp(event['at'])}  {event['kind']}")
    _emit("\n".join(lines), as_json=False)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        events = ledger.history(args.case_id, limit=args.limit)
    finally:
        ledger.close()
    if args.json:
        _emit(events, as_json=True)
        return 0
    for event in events:
        detail = json.dumps(event["detail"], sort_keys=True)
        _emit(f"{_stamp(event['at'])}  {event['kind']:<24} {detail}", as_json=False)
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    payload: Dict[str, Any] = json.loads(args.payload) if args.payload else {}
    if not isinstance(payload, dict):
        raise SystemExit("payload must be a JSON object")
    ledger = _open(args)
    try:
        case_id = ledger.register(
            args.namespace,
            args.kind,
            args.source_id,
            owner=args.owner,
            payload=payload,
        )
    finally:
        ledger.close()
    _emit(case_id if not args.json else {"id": case_id}, as_json=args.json)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        case = ledger.resume_human_decision(
            args.case_id, reason=args.reason, answer=args.answer
        )
    finally:
        ledger.close()
    _emit(
        case if args.json else f"{case['id']} is {case['status']} again",
        as_json=args.json,
    )
    return 0


def cmd_arm(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        unsettled = ledger.unsettled_actions()
        if unsettled and not args.force:
            keys = ", ".join(action["key"] for action in unsettled)
            _emit(
                "refusing to arm: these actions were attempted and never settled, "
                f"so nobody knows whether they happened: {keys}\n"
                "Reconcile them against the external system first, or pass --force "
                "if you have already established what is true.",
                as_json=False,
                stream=sys.stderr,
            )
            return 2
        ledger.arm_writes(args.note)
        armed = ledger.writes_armed()
    finally:
        ledger.close()
    _emit(
        {"writes_armed": armed} if args.json else "writes armed",
        as_json=args.json,
    )
    return 0


def cmd_disarm(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        ledger.disarm_writes(args.note)
        armed = ledger.writes_armed()
    finally:
        ledger.close()
    _emit(
        {"writes_armed": armed} if args.json else "writes disarmed",
        as_json=args.json,
    )
    return 0


def cmd_gate_log(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        entries = ledger.write_gate_history()
    finally:
        ledger.close()
    if args.json:
        _emit(entries, as_json=True)
        return 0
    for entry in entries:
        state = "ARMED   " if entry["armed"] else "DISARMED"
        _emit(f"{_stamp(entry['at'])}  {state}  {entry['note']}", as_json=False)
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    ledger = _open(args)
    try:
        manifest = ledger.snapshot(Path(args.destination))
    finally:
        ledger.close()
    if args.json:
        _emit(manifest, as_json=True)
        return 0
    _emit(
        f"snapshot written to {args.destination}\n"
        f"  sha256 {manifest['sha256']}\n"
        f"  bytes  {manifest['bytes']}\n"
        "  the snapshot is disarmed and its open cases are parked for review",
        as_json=False,
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_snapshot(Path(args.snapshot))
    if args.json:
        _emit(report, as_json=True)
    elif report["ok"]:
        _emit(
            f"ok: {report['cases']} cases, {report['unsettled_actions']} unsettled "
            f"actions, disarmed, sha256 {report['sha256']}",
            as_json=False,
        )
    else:
        _emit(f"NOT VERIFIED: {report['reason']}", as_json=False, stream=sys.stderr)
    return 0 if report["ok"] else 1


def cmd_example(args: argparse.Namespace) -> int:
    from .demo import run

    result = run(Path(args.directory) if args.directory else None)
    if args.json:
        _emit(result, as_json=True)
    return 0


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-ledger",
        description=(
            "Inspect and operate an agent action ledger. This tool records and "
            "coordinates work; it never performs external actions."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def with_path(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("path", help="path to the ledger SQLite file")
        sub.add_argument("--json", action="store_true", help="machine-readable output")
        sub.add_argument(
            "--no-validate",
            action="store_true",
            help="skip full re-validation of stored rows (large ledgers)",
        )
        return sub

    init = with_path(subparsers.add_parser("init", help="create a ledger"))
    init.add_argument(
        "--max-concurrent-leases",
        type=int,
        default=4,
        help="how many cases may be leased at once, across all workers",
    )
    init.set_defaults(func=cmd_init)

    status = with_path(
        subparsers.add_parser("status", help="counts, gate state, unsettled actions")
    )
    status.set_defaults(func=cmd_status)

    listing = with_path(subparsers.add_parser("list", help="list cases"))
    listing.add_argument("--status")
    listing.add_argument("--namespace")
    listing.add_argument("--kind")
    listing.add_argument("--limit", type=int)
    listing.set_defaults(func=cmd_list)

    show = with_path(subparsers.add_parser("show", help="one case, its actions and history"))
    show.add_argument("case_id")
    show.set_defaults(func=cmd_show)

    history = with_path(subparsers.add_parser("history", help="event history of a case"))
    history.add_argument("case_id")
    history.add_argument("--limit", type=int)
    history.set_defaults(func=cmd_history)

    register = with_path(subparsers.add_parser("register", help="register a case"))
    register.add_argument("namespace")
    register.add_argument("kind")
    register.add_argument("source_id")
    register.add_argument("--owner", required=True)
    register.add_argument("--payload", help="JSON object", default="{}")
    register.set_defaults(func=cmd_register)

    resume = with_path(
        subparsers.add_parser("resume", help="record a human answer and reopen a parked case")
    )
    resume.add_argument("case_id")
    resume.add_argument("--reason", required=True)
    resume.add_argument("--answer", default="")
    resume.set_defaults(func=cmd_resume)

    arm = with_path(subparsers.add_parser("arm", help="allow actions to be attempted"))
    arm.add_argument("--note", required=True, help="who verified what, on the record")
    arm.add_argument(
        "--force", action="store_true", help="arm despite unsettled action attempts"
    )
    arm.set_defaults(func=cmd_arm)

    disarm = with_path(subparsers.add_parser("disarm", help="stop further actions"))
    disarm.add_argument("--note", required=True)
    disarm.set_defaults(func=cmd_disarm)

    gate_log = with_path(
        subparsers.add_parser("gate-log", help="history of arming and disarming")
    )
    gate_log.set_defaults(func=cmd_gate_log)

    snapshot = with_path(subparsers.add_parser("snapshot", help="write a disarmed snapshot"))
    snapshot.add_argument("destination")
    snapshot.set_defaults(func=cmd_snapshot)

    verify = subparsers.add_parser("verify", help="verify a snapshot, read-only")
    verify.add_argument("snapshot")
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)

    example = subparsers.add_parser("example", help="run the bundled support-desk example")
    example.add_argument(
        "--directory", help="keep the demo ledger here instead of a temporary directory"
    )
    example.add_argument("--json", action="store_true")
    example.set_defaults(func=cmd_example)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LedgerError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
