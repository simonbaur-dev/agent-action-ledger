#!/usr/bin/env python3
"""Run the support-desk example.

    python examples/support_desk.py                 # temporary ledger, removed after
    python examples/support_desk.py ./demo-run      # keep the ledger to poke at

The example itself lives in ``agent_action_ledger.demo`` so that this script
and ``agent-ledger example`` run byte-for-byte the same code, and so that the
example is still available from an installed wheel. Open that module to read
it: it is written to be read.

Everything it touches is synthetic. ``FakeSupportDesk`` is a dictionary
standing in for a ticketing system. No network call is made, and nothing is
written outside the directory you pass in.

Afterwards, if you kept the directory::

    agent-ledger status    ./demo-run/ledger.sqlite
    agent-ledger list      ./demo-run/ledger.sqlite
    agent-ledger gate-log  ./demo-run/ledger.sqlite
    agent-ledger verify    ./demo-run/snapshots/ledger.sqlite
"""
from __future__ import annotations

import sys

from agent_action_ledger.demo import run


def main() -> int:
    directory = sys.argv[1] if len(sys.argv) > 1 else None
    run(directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
