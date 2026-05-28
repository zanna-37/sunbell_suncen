"""Cover entity per (remote, channel) pair.

The entity is a thin state holder: it owns `_tilt_level` and `_position` and
hands every Home-Assistant op (open / close / stop / set_tilt_position) off to
the per-entry `BurstScheduler` as a pre-decomposed chain of `BurstStep`s with
entity-bound callbacks for the per-step state commits.

State machine summary:
- Fast UP/DOWN: chain is `[UP|DOWN(motion_time=travel_time)]`.
  on_dispatch clears tilt + position to "in motion"; on_complete (fired at
  busy_until expiry) commits the post-reversal anchor (UP -> level 1 / pos 100,
  DOWN -> level 7 / pos 0).
- STOP: chain is `[STOP(motion_time=0)]`. on_dispatch clears both; no
  on_complete (state stays unknown).
- set_tilt_position(level=T) uses the entity's committed `_tilt_level` to
  pick the chain:
    current is None  -> [DOWN, LONG_DOWN x (7 - T)]   re-anchor + walk
    current == T     -> empty (idempotent short-circuit)
    current > T      -> [LONG_DOWN x (current - T)]   walk down directly
    current < T      -> [LONG_UP   x (T - current)]   walk up directly
  Each LONG_*'s on_complete commits the per-step tilt level so the entity
  state always reflects the physical position.

`tilt_level + position` survive HA restarts via `RestoreEntity`; the
scheduler queue is volatile (lost on restart).
"""
from __future__ import annotations

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

from .const import (
    ATTR_POSITION_INTERNAL,
    ATTR_TILT_LEVEL,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    TILT_LEVELS,
    TILT_LEVEL_DOWN_ANCHOR,
    TILT_LEVEL_UP_ANCHOR,
    TILT_POSITION_TICKS,
)
from .models import BlindConfig, SunbellConfigEntry, SunbellRuntimeData
from .scheduler import BlindKey, BurstStep, BurstScheduler

ATTR_TILT_POSITION_TICKS = "tilt_position_ticks"

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
    """Snap a 0..100 percent input to the nearest 1..7 tilt level via TILT_POSITION_TICKS."""
    pct = max(0, min(100, int(pct)))
    best_idx = min(
        range(TILT_LEVELS),
        key=lambda i: (abs(TILT_POSITION_TICKS[i] - pct), i),
    )
    return best_idx + 1


def tilt_level_to_position(level: int) -> int:
    """1..7 -> one of TILT_POSITION_TICKS."""
    return TILT_POSITION_TICKS[level - 1]


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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.attributes:
            stored_tilt = last.attributes.get(ATTR_TILT_LEVEL)
            self._tilt_level = int(stored_tilt) if stored_tilt is not None else None
            self._position = last.attributes.get(ATTR_POSITION_INTERNAL)

    async def async_will_remove_from_hass(self) -> None:
        # Drop any pending steps so the scheduler stops emitting bursts for us.
        self._runtime.scheduler.submit(self.key, ())
        await super().async_will_remove_from_hass()

    # --------------------------------------------------------------- props
    @property
    def key(self) -> BlindKey:
        return (self._blind.remote, self._blind.channel)

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
    def runtime(self) -> SunbellRuntimeData:
        return self._runtime

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
            ATTR_TILT_POSITION_TICKS: list(TILT_POSITION_TICKS),
        }

    # ------------------------------------------------------------------ HA
    async def async_open_cover(self, **_kwargs: Any) -> None:
        self._runtime.scheduler.submit(self.key, self.build_open_chain(), entity=self)

    async def async_close_cover(self, **_kwargs: Any) -> None:
        self._runtime.scheduler.submit(self.key, self.build_close_chain(), entity=self)

    async def async_stop_cover(self, **_kwargs: Any) -> None:
        self._runtime.scheduler.submit(self.key, self.build_stop_chain(), entity=self)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        target = tilt_position_to_level(kwargs[ATTR_TILT_POSITION])
        chain = self.build_tilt_chain(target)
        if not chain:
            return
        self._runtime.scheduler.submit(self.key, chain, entity=self)

    # ----------------------------------------------------- chain builders
    def build_open_chain(self) -> list[BurstStep]:
        return [self._fast_step("UP", on_complete=self._commit_up_anchor)]

    def build_close_chain(self) -> list[BurstStep]:
        return [self._fast_step("DOWN", on_complete=self._commit_down_anchor)]

    def build_stop_chain(self) -> list[BurstStep]:
        return [
            BurstScheduler.fast_step(
                self.key,
                "STOP",
                motion_time=0.0,
                on_dispatch=self._begin_motion,
                on_complete=None,
            )
        ]

    def build_tilt_chain(self, target_level: int) -> list[BurstStep]:
        """Pre-decompose set_tilt_position(target_level) into burst steps.

        Uses the entity's committed `_tilt_level`:
          - unknown  -> re-anchor with DOWN then walk LONG_DOWN x (7 - target)
          - known and == target -> empty (idempotent)
          - known and > target -> walk LONG_DOWN x (current - target)
          - known and < target -> walk LONG_UP   x (target - current)
        """
        current = self._tilt_level
        if current is not None and current == target_level:
            return []
        if current is None:
            chain: list[BurstStep] = [
                self._fast_step("DOWN", on_complete=self._commit_down_anchor)
            ]
            for i in range(1, TILT_LEVEL_DOWN_ANCHOR - target_level + 1):
                level_after = TILT_LEVEL_DOWN_ANCHOR - i
                chain.append(self._tilt_walk_step("LONG_DOWN", level_after))
            return chain
        if current > target_level:
            chain = []
            for i in range(1, current - target_level + 1):
                chain.append(self._tilt_walk_step("LONG_DOWN", current - i))
            return chain
        # current < target_level
        chain = []
        for i in range(1, target_level - current + 1):
            chain.append(self._tilt_walk_step("LONG_UP", current + i))
        return chain

    def build_raw_chain(self, action: str, *, invalidate_position: bool) -> list[BurstStep]:
        """Single raw burst with state invalidation (send_group_raw on configured channel)."""
        motion_time = float(self.travel_time) if action in ("UP", "DOWN") else 0.0
        return [
            BurstScheduler.fast_step(
                self.key,
                action,
                motion_time=motion_time,
                on_dispatch=self._invalidator_for_raw(invalidate_position),
                on_complete=None,
            )
        ]

    # --------------------------------------------------------- callbacks
    def _begin_motion(self) -> None:
        self._tilt_level = None
        self._position = None
        self.async_write_ha_state()

    def _commit_up_anchor(self) -> None:
        self._tilt_level = TILT_LEVEL_UP_ANCHOR
        self._position = 100
        self.async_write_ha_state()

    def _commit_down_anchor(self) -> None:
        self._tilt_level = TILT_LEVEL_DOWN_ANCHOR
        self._position = 0
        self.async_write_ha_state()

    def _commit_tilt_level(self, level: int) -> None:
        self._tilt_level = level
        self.async_write_ha_state()

    def _invalidator_for_raw(self, invalidate_position: bool):
        def _do() -> None:
            self._tilt_level = None
            if invalidate_position:
                self._position = None
            self.async_write_ha_state()
        return _do

    # ----------------------------------------------------- step helpers
    def _fast_step(
        self,
        action: str,
        *,
        on_complete,
    ) -> BurstStep:
        return BurstScheduler.fast_step(
            self.key,
            action,
            motion_time=float(self.travel_time),
            on_dispatch=self._begin_motion,
            on_complete=on_complete,
        )

    def _tilt_walk_step(self, action: str, level_after: int) -> BurstStep:
        commit = lambda lv=level_after: self._commit_tilt_level(lv)
        return BurstScheduler.tilt_step(
            self.key,
            action,
            on_complete=commit,
        )
