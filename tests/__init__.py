"""Test package.

The sys.path shim mirrors the root ``conftest.py`` so that
``python -m unittest discover`` works in a fresh clone too, not only pytest.
"""
from __future__ import annotations

from pathlib import Path
import sys

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
