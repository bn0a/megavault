"""pytest shim over the standalone harness.

The harness in `run_offline.py` is the source of truth and needs no test runner:

    python tests/run_offline.py

This file only lets `python -m pytest tests/ -q` collect the same cases.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "run_offline", Path(__file__).resolve().parent / "run_offline.py"
)
_harness = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_harness)

for _func in _harness.TESTS:
    globals()[_func.__name__] = _func
