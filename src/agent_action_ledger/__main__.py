"""Allow ``python -m agent_action_ledger`` as well as the ``agent-ledger`` script."""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
