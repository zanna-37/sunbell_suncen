"""Tests for the options-flow channel-editing helpers.

`config_flow.py` is loaded in isolation so importing the integration package's
``__init__.py`` (which pulls in the full Home Assistant runtime) is not required
-- mirroring how ``conftest.py`` loads ``scheduler.py``. The two helpers under
test are pure list/dict reconciliation with no Home Assistant dependency.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parent.parent
_BASE = _REPO / "custom_components" / "sunbell_suncen"
_PKG = "custom_components.sunbell_suncen"


def _load(module_name: str, relpath: str, package: str | None = None) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, _BASE / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    if package is not None:
        module.__package__ = package
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _stub_package(name: str) -> None:
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = []  # mark as a (namespace) package so submodule imports resolve
        sys.modules[name] = pkg


# Register the package hierarchy as stubs so config_flow's relative imports
# resolve to the preloaded pure modules instead of executing the real
# __init__.py (which imports the full Home Assistant runtime).
_stub_package("custom_components")
_stub_package(_PKG)
_stub_package(f"{_PKG}._protocol")
_load(f"{_PKG}.const", "const.py", package=_PKG)
_load(f"{_PKG}._protocol.channels", "_protocol/channels.py", package=f"{_PKG}._protocol")

try:
    _cf = _load(f"{_PKG}.config_flow", "config_flow.py", package=_PKG)
except Exception as exc:  # pragma: no cover - depends on installed HA version
    pytest.skip(
        f"config_flow.py could not be imported in this environment: {exc}",
        allow_module_level=True,
    )

_channels_to_spec = _cf._channels_to_spec
_merge_channels = _cf._merge_channels

from custom_components.sunbell_suncen.const import (  # noqa: E402
    CONF_CHANNEL,
    CONF_FULL_MOVEMENT_TIME,
    CONF_NAME,
)


def _blind(channel: int, name: str, travel: int | None = None) -> dict[str, Any]:
    blind: dict[str, Any] = {CONF_CHANNEL: channel, CONF_NAME: name}
    if travel is not None:
        blind[CONF_FULL_MOVEMENT_TIME] = travel
    return blind


def test_channels_to_spec_sorts_and_joins() -> None:
    assert _channels_to_spec([3, 1, 2]) == "1,2,3"
    assert _channels_to_spec([]) == ""


def test_merge_channels_preserves_name_and_override() -> None:
    existing = [_blind(1, "Kitchen", travel=42), _blind(2, "Hall")]
    merged = _merge_channels(existing, [1, 2], "0")
    assert merged == [_blind(1, "Kitchen", travel=42), _blind(2, "Hall")]


def test_merge_channels_adds_new_with_default_name() -> None:
    existing = [_blind(1, "Kitchen")]
    merged = _merge_channels(existing, [1, 3], "5")
    assert merged[0] == _blind(1, "Kitchen")
    assert merged[1] == {CONF_CHANNEL: 3, CONF_NAME: "R5 ch3"}


def test_merge_channels_drops_removed_blinds() -> None:
    existing = [_blind(1, "Kitchen"), _blind(2, "Hall"), _blind(3, "Bath")]
    merged = _merge_channels(existing, [2], "0")
    assert merged == [_blind(2, "Hall")]


def test_merge_channels_returns_sorted_channels() -> None:
    existing = [_blind(2, "Hall", travel=10)]
    merged = _merge_channels(existing, [3, 1, 2], "0")
    assert [b[CONF_CHANNEL] for b in merged] == [1, 2, 3]
    # The retained channel keeps its custom name and override.
    assert merged[1] == _blind(2, "Hall", travel=10)
