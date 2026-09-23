"""Make ``src/`` importable so the tests run in a fresh clone without install.

An editable install (``pip install -e .[dev]``) is the recommended setup and
makes this file redundant; it exists so that ``pytest`` works immediately after
``git clone``, which is the first thing most people try.
"""
from __future__ import annotations

from pathlib import Path
import sys

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
