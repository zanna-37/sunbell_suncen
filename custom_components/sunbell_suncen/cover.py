"""Cover entity per (remote, channel) pair.

State machine summary:
- Open/close/stop are optimistic fast actions; UP anchors tilt at level 7,
  DOWN at level 1.
- Tilt direction = opposite of last_direction (UP fast -> LONG_DOWN tilt,
  DOWN fast -> LONG_UP tilt). A tilt that would need to reverse direction
  anchor-resets first via the closer extreme.
- Targets at level 1 or 7 get one extra LONG_ step beyond the computed
  delta to push past motor desync into the mechanical limit.
- First-ever tilt with no recorded last_direction auto-issues a short DOWN
  to anchor at level 1, then proceeds.
- last_direction + tilt_level + position survive HA restarts via
  RestoreEntity.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from ._protocol.synth import build_symbol_burst_signed
from .const import (
    ATTR_LAST_DIRECTION,
    ATTR_POSITION_INTERNAL,
    ATTR_TILT_LEVEL,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    TILT_EXTREMES,
    TILT_LEVELS,
    TILT_LEVEL_DOWN_ANCHOR,
    TILT_LEVEL_UP_ANCHOR,
)
from .models import BlindConfig, SunbellConfigEntry, SunbellRuntimeData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    entities = [
        SunbellBlind(blind, runtime)
        for remote in runtime.remotes
        for blind in remote.blinds
    ]
    async_add_entities(entities)


def tilt_position_to_level(pct: int) -> int:
    """0..100 -> 1..7 by linear quantization."""
    pct = max(0, min(100, int(pct)))
    return round(pct / 100 * (TILT_LEVELS - 1)) + 1


def tilt_level_to_position(level: int) -> int:
    """1..7 -> 0..100, inverse of tilt_position_to_level."""
    return round((level - 1) / (TILT_LEVELS - 1) * 100)


class SunbellBlind(RestoreEntity, CoverEntity):
    """Optimistic venetian-blind cover for a single (remote, channel)."""

    _attr_has_entity_name = True
    _attr_assumed_state = True
    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_TILT_POSITION
    )

    def __init__(self, blind: BlindConfig, runtime: SunbellRuntimeData) -> None:
        self._blind = blind
        self._runtime = runtime
        self._attr_unique_id = f"{blind.remote}_ch{blind.channel}"
        self._attr_name = blind.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, blind.remote)},
            name=f"Sunbell SUNCEN remote {blind.remote}",
            manufacturer=MANUFACTURER,
            model=f"{MODEL} remote {blind.remote}",
        )
        self._last_direction: str | None = None
        self._tilt_level: int = TILT_LEVEL_DOWN_ANCHOR
        self._position: int | None = None
        self._tilt_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.attributes:
            self._last_direction = last.attributes.get(ATTR_LAST_DIRECTION)
            self._tilt_level = int(last.attributes.get(ATTR_TILT_LEVEL, TILT_LEVEL_DOWN_ANCHOR))
            self._position = last.attributes.get(ATTR_POSITION_INTERNAL)

    @property
    def remote_id(self) -> str:
        return self._blind.remote

    @property
    def channel(self) -> int:
        return self._blind.channel

    @property
    def tilt_level(self) -> int:
        return self._tilt_level

    @property
    def is_closed(self) -> bool | None:
        return None if self._position is None else self._position == 0

    @property
    def current_cover_position(self) -> int | None:
        return self._position

    @property
    def current_cover_tilt_position(self) -> int:
        return tilt_level_to_position(self._tilt_level)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_LAST_DIRECTION: self._last_direction,
            ATTR_TILT_LEVEL: self._tilt_level,
            ATTR_POSITION_INTERNAL: self._position,
        }

    async def async_open_cover(self, **_kwargs: Any) -> None:
        await self._send("UP")
        self._last_direction = "UP"
        self._tilt_level = TILT_LEVEL_UP_ANCHOR
        self._position = 100
        self.async_write_ha_state()

    async def async_close_cover(self, **_kwargs: Any) -> None:
        await self._send("DOWN")
        self._last_direction = "DOWN"
        self._tilt_level = TILT_LEVEL_DOWN_ANCHOR
        self._position = 0
        self.async_write_ha_state()

    async def async_stop_cover(self, **_kwargs: Any) -> None:
        # Motor halts mid-flight; we don't know where it ended up, so position
        # and tilt_level stay where they were optimistically.
        await self._send("STOP")
        self.async_write_ha_state()

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        target = tilt_position_to_level(kwargs[ATTR_TILT_POSITION])
        async with self._tilt_lock:
            await self._move_to_tilt_level(target)
        self.async_write_ha_state()

    async def _move_to_tilt_level(self, target: int) -> None:
        # Auto-anchor on the very first tilt after a fresh install.
        if self._last_direction is None:
            await self._send("DOWN")
            self._last_direction = "DOWN"
            self._tilt_level = TILT_LEVEL_DOWN_ANCHOR
            self._position = 0
            if target == TILT_LEVEL_DOWN_ANCHOR:
                return

        current = self._tilt_level
        if current == target:
            return

        # Tilt direction depends on which fast movement was last.
        natural = "LONG_UP" if self._last_direction == "DOWN" else "LONG_DOWN"
        natural_goes_up = natural == "LONG_UP"
        going_up = target > current

        if going_up != natural_goes_up:
            # Direction conflict — anchor-reset to the closer extreme.
            if target <= TILT_LEVELS // 2 + 1:   # bottom half incl. middle -> DOWN
                await self._send("DOWN")
                self._last_direction = "DOWN"
                self._tilt_level = TILT_LEVEL_DOWN_ANCHOR
                self._position = 0
                current = TILT_LEVEL_DOWN_ANCHOR
                natural = "LONG_UP"
            else:
                await self._send("UP")
                self._last_direction = "UP"
                self._tilt_level = TILT_LEVEL_UP_ANCHOR
                self._position = 100
                current = TILT_LEVEL_UP_ANCHOR
                natural = "LONG_DOWN"

        steps = abs(target - current)
        # +1 extra step at the extremes to clear motor desync into the limit,
        # but only when we actually have to tilt (steps>0). Anchoring directly
        # onto the extreme via a fast UP/DOWN doesn't need the extra.
        if target in TILT_EXTREMES and steps > 0:
            steps += 1

        for _ in range(steps):
            await self._send(natural)
        self._tilt_level = target

    async def _send(self, action: str) -> None:
        """Fire a single RF burst via the integration's transmit queue."""
        pulses = build_symbol_burst_signed(self._blind.remote, [self._blind.channel], action)
        await self._runtime.transmit_queue.send(pulses)

    def apply_group_update(
        self,
        last_direction: str | None,
        tilt_level: int,
        position: int | None,
        *,
        update_position: bool = True,
    ) -> None:
        """Apply state predicted by send_group after a group burst sequence.

        last_direction=None leaves the recorded direction untouched (e.g. a
        delta-only tilt doesn't change which fast movement was last). Pass
        update_position=False to leave the position unchanged (also used by
        tilt — tilting doesn't move the blind up or down).
        """
        if last_direction is not None:
            self._last_direction = last_direction
        self._tilt_level = tilt_level
        if update_position:
            self._position = position
        self.async_write_ha_state()
