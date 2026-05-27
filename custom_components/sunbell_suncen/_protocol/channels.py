"""Channel/action parsing helpers (used by the send_group service)."""
from __future__ import annotations

import re

ACTIONS = ("UP", "DOWN", "STOP", "LONG_UP", "LONG_DOWN")
REMOTES = tuple(f"r{i}" for i in range(13))

CHANNEL_TOKEN = re.compile(r"(?i)^(?:ch)?(\d+)$")


def parse_channels(spec: str | list[int]) -> list[int]:
    """`ch1` -> [1]; `ch1,ch6` -> [1, 6]; `1,2,6` -> [1, 2, 6]; [1,2,6] -> [1,2,6].

    Returns the canonical sorted list. Raises ValueError on bad input.
    """
    if isinstance(spec, list):
        parts: list[str] = [str(p) for p in spec]
    else:
        parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"empty channel spec {spec!r}")
    out: list[int] = []
    for p in parts:
        m = CHANNEL_TOKEN.match(p)
        if not m:
            raise ValueError(f"bad channel token {p!r} in {spec!r}; expected e.g. ch1 or 1")
        n = int(m.group(1))
        if not 1 <= n <= 12:
            raise ValueError(f"channel out of range 1..12: {p!r}")
        out.append(n)
    if len(set(out)) != len(out):
        raise ValueError(f"duplicate channels in {spec!r}")
    return sorted(out)


def parse_action(action: str) -> str:
    a = action.upper()
    if a not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
    return a
