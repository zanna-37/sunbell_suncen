"""Test bootstrap: load scheduler.py directly without triggering the
integration's __init__.py (which imports Home Assistant)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCHED_PATH = _REPO / "custom_components" / "sunbell_suncen" / "scheduler.py"

_spec = importlib.util.spec_from_file_location(
    "sunbell_scheduler_under_test", _SCHED_PATH
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["sunbell_scheduler_under_test"] = _mod
_spec.loader.exec_module(_mod)
