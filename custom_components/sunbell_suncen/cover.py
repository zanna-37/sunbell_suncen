"""Cover entity per (remote, channel) pair.

State machine summary:
- Open/close are optimistic-with-settle: send the burst, mark tilt + position
  unknown immediately, then schedule a background task that flips state to
  the post-reversal anchor (UP -> level 1 / pos 100, DOWN -> level 7 / pos 0)
  after `full_movement_time` seconds. A subsequent fast action cancels the
  pending settle.
- Stop invalidates both tilt and position; the motor halted somewhere we
  can't predict.
- Tilt only operates from the full-down anchor (level 7, pos 0). When asked
  to set a tilt position, the entity first ensures it is at full-down (by
  awaiting a pending settle that will land there, or by issuing a fresh
  DOWN + sleep) and then steps LONG_DOWN exactly (7 - target) times.
- `full_movement_time` per blind comes from the runtime helper (override or
  entry default).
- tilt_level + position survive HA restarts via RestoreEntity; an in-flight
  settle does NOT survive (tilt becomes unknown until the user opens/closes
  again).
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
    ATTR_POSITION_INTERNAL,
    ATTR_TILT_LEVEL,
    DOMAIN,
    MANUFACTURER,
    MODEL,
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
        self._tilt_level: int | None = None
        self._position: int | None = None
        self._tilt_lock = asyncio.Lock()
        self._settle_task: asyncio.Task[None] | None = None
        self._settle_target: tuple[int, int] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.attributes:
            stored_tilt = last.attributes.get(ATTR_TILT_LEVEL)
            self._tilt_level = int(stored_tilt) if stored_tilt is not None else None
            self._position = last.attributes.get(ATTR_POSITION_INTERNAL)

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_settle()
        await super().async_will_remove_from_hass()

    @property
    def remote_id(self) -> str:
        return self._blind.remote

    @property
    def channel(self) -> int:
        return self._blind.channel

    @property
    def tilt_level(self) -> int | None:
        return self._tilt_level

    @property
    def travel_time(self) -> int:
        return self._runtime.travel_time_for(self._blind)

    @property
    def is_closed(self) -> bool | None:
        return None if self._position is None else self._position == 0

    @property
    def current_cover_position(self) -> int | None:
        return self._position

    @property
    def current_cover_tilt_position(self) -> int | None:
        return None if self._tilt_level is None else tilt_level_to_position(self._tilt_level)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_TILT_LEVEL: self._tilt_level,
            ATTR_POSITION_INTERNAL: self._position,
        }

    # ------------------------------------------------------------------ HA
    async def async_open_cover(self, **_kwargs: Any) -> None:
        await self._send("UP")
        self._begin_motion()
        self._schedule_settle(TILT_LEVEL_UP_ANCHOR, 100)
        self.async_write_ha_state()

    async def async_close_cover(self, **_kwargs: Any) -> None:
        await self._send("DOWN")
        self._begin_motion()
        self._schedule_settle(TILT_LEVEL_DOWN_ANCHOR, 0)
        self.async_write_ha_state()

    async def async_stop_cover(self, **_kwargs: Any) -> None:
        await self._send("STOP")
        self._cancel_settle()
        self._tilt_level = None
        self._position = None
        self.async_write_ha_state()

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        target = tilt_position_to_level(kwargs[ATTR_TILT_POSITION])
        async with self._tilt_lock:
            await self._ensure_at_full_down()
            delta = TILT_LEVEL_DOWN_ANCHOR - target
            for _ in range(delta):
                await self._send("LONG_DOWN")
            self._tilt_level = target
            self.async_write_ha_state()

    # ---------------------------------------------------------------- group
    def begin_motion_for_group(self, target_tilt: int | None, target_position: int | None) -> None:
        """Called by the group service before its multi-channel burst.

        target_tilt / target_position are the values the settle should land at
        (None disables scheduling — e.g. for STOP, where both stay unknown).
        """
        self._begin_motion()
        if target_tilt is not None and target_position is not None:
            self._schedule_settle(target_tilt, target_position)
        self.async_write_ha_state()

    def commit_tilt_target(self, target_level: int) -> None:
        """Group tilt finished its LONG_DOWN walk; record the resulting level."""
        self._tilt_level = target_level
        # Position stays at 0 — the group tilt path always runs from full-down
        # and LONG_DOWN doesn't move the blind up or down.
        self.async_write_ha_state()

    def commit_full_down(self) -> None:
        """Group orchestrator finished waiting out a DOWN cycle; snap to anchor."""
        self._cancel_settle()
        self._tilt_level = TILT_LEVEL_DOWN_ANCHOR
        self._position = 0
        self.async_write_ha_state()

    async def at_full_down_with_settled_state(self) -> bool:
        """True iff currently at level 7 / pos 0 with no in-flight settle.

        If a settle is pending whose target is full-down, awaits it and then
        reports the result.
        """
        if (
            self._tilt_level == TILT_LEVEL_DOWN_ANCHOR
            and self._position == 0
            and self._settle_task is None
        ):
            return True
        if (
            self._settle_task is not None
            and self._settle_target == (TILT_LEVEL_DOWN_ANCHOR, 0)
        ):
            await self._await_settle()
            return (
                self._tilt_level == TILT_LEVEL_DOWN_ANCHOR
                and self._position == 0
            )
        return False

    # ------------------------------------------------------------- internal
    async def _send(self, action: str) -> None:
        """Fire a single RF burst via the integration's transmit queue."""
        pulses = build_symbol_burst_signed(self._blind.remote, [self._blind.channel], action)
        await self._runtime.transmit_queue.send(pulses)

    def _begin_motion(self) -> None:
        """Cancel any pending settle and mark state as in-flight."""
        self._cancel_settle()
        self._tilt_level = None
        self._position = None

    def _cancel_settle(self) -> None:
        if self._settle_task is not None and not self._settle_task.done():
            self._settle_task.cancel()
        self._settle_task = None
        self._settle_target = None

    def _schedule_settle(self, target_tilt: int, target_position: int) -> None:
        """Schedule the post-motion state commit `full_movement_time` seconds out."""
        travel_time = self._runtime.travel_time_for(self._blind)
        self._settle_target = (target_tilt, target_position)
        target = (target_tilt, target_position)

        async def _settle() -> None:
            try:
                await asyncio.sleep(travel_time)
            except asyncio.CancelledError:
                return
            # Verify we still own this settle slot (a newer action may have
            # cancelled us between sleep-wakeup and re-entry).
            if self._settle_target != target:
                return
            self._tilt_level = target_tilt
            self._position = target_position
            self._settle_task = None
            self._settle_target = None
            self.async_write_ha_state()

        self._settle_task = self.hass.async_create_task(_settle())

    async def _await_settle(self) -> None:
        task = self._settle_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _ensure_at_full_down(self) -> None:
        """Block until the blind is at full-down (tilt 7, position 0).

        Three cases:
        - already there: return immediately;
        - a pending settle is on its way to full-down: await it;
        - otherwise: cancel any pending settle, issue a fresh DOWN, and sleep
          for the configured travel time before recording the anchor.
        """
        if await self.at_full_down_with_settled_state():
            return

        self._cancel_settle()
        await self._send("DOWN")
        self._tilt_level = None
        self._position = None
        self.async_write_ha_state()

        travel_time = self._runtime.travel_time_for(self._blind)
        await asyncio.sleep(travel_time)
        self._tilt_level = TILT_LEVEL_DOWN_ANCHOR
        self._position = 0

    # --------------------------------------------------------------- raw
    def invalidate_for_raw(self, invalidate_position: bool) -> None:
        """Called by send_group_raw after sending a raw burst on this channel.

        Tilt is always invalidated; position is invalidated for UP/DOWN/STOP
        and left alone for LONG_UP / LONG_DOWN.
        """
        self._cancel_settle()
        self._tilt_level = None
        if invalidate_position:
            self._position = None
        self.async_write_ha_state()
