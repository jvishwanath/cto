"""
Shared test setup. Importing this (pytest auto-loads conftest, and the
hand-rolled runners import from smoke_test which lives here) puts
src/ on sys.path so `import rag.*` works, and makes sibling test
modules importable without per-file path hacks.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_SRC = _ROOT / "src"

for p in (str(_SRC), str(Path(__file__).parent)):
    if p not in sys.path:
        sys.path.insert(0, p)
