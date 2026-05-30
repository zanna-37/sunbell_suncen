"""Typed runtime-data dataclasses + config-entry deserialization."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ._protocol.synth import build_symbol_burst_signed
from .const import (
    BURST_GAP_SECONDS,
    CONF_AT_ANCHOR_SETTLE_TIME,
    CONF_BLINDS,
    CONF_CHANNEL,
    CONF_FULL_MOVEMENT_TIME,
    CONF_NAME,
    CONF_REMOTE_ID,
    CONF_REMOTES,
    CONF_TRANSMIT_SERVICE,
    DEFAULT_AT_ANCHOR_SETTLE_TIME,
    DEFAULT_FULL_MOVEMENT_TIME,
)
from .scheduler import BurstScheduler
from .transmit_queue import TransmitQueue


@dataclass(frozen=True, slots=True)
class BlindConfig:
    """One (remote, channel) blind."""
    remote: str
    channel: int
    name: str
    full_movement_time: int | None = None    # None -> fall back to entry default


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    """All blinds belonging to one Sunbell remote."""
    remote: str
    blinds: tuple[BlindConfig, ...]


@dataclass(slots=True)
class SunbellRuntimeData:
    """Per-config-entry runtime state."""
    transmit_service_name: str   # "<device>_transmit_raw" under the esphome domain
    remotes: tuple[RemoteConfig, ...]
    transmit_queue: TransmitQueue
    scheduler: BurstScheduler
    default_full_movement_time: int
    at_anchor_settle_time: int

    def travel_time_for(self, blind: BlindConfig) -> int:
        """Effective full-movement time for `blind` — its override or the entry default."""
        return blind.full_movement_time if blind.full_movement_time is not None \
            else self.default_full_movement_time


type SunbellConfigEntry = ConfigEntry[SunbellRuntimeData]


def build_runtime_data(
    hass: HomeAssistant, entry: SunbellConfigEntry
) -> SunbellRuntimeData:
    """Deserialize a config entry's stored data + options into typed runtime state."""
    merged = {**entry.data, **entry.options}
    remotes_raw = merged.get(CONF_REMOTES, [])
    remotes = tuple(
        RemoteConfig(
            remote=rc[CONF_REMOTE_ID],
            blinds=tuple(
                BlindConfig(
                    remote=rc[CONF_REMOTE_ID],
                    channel=int(b[CONF_CHANNEL]),
                    name=str(b[CONF_NAME]),
                    full_movement_time=_coerce_optional_int(
                        b.get(CONF_FULL_MOVEMENT_TIME)
                    ),
                )
                for b in rc[CONF_BLINDS]
            ),
        )
        for rc in remotes_raw
    )
    transmit_service_name = str(merged[CONF_TRANSMIT_SERVICE])
    default_full_movement_time = int(
        merged.get(CONF_FULL_MOVEMENT_TIME, DEFAULT_FULL_MOVEMENT_TIME)
    )
    at_anchor_settle_time = int(
        merged.get(CONF_AT_ANCHOR_SETTLE_TIME, DEFAULT_AT_ANCHOR_SETTLE_TIME)
    )
    transmit_queue = TransmitQueue(hass, transmit_service_name)
    scheduler = BurstScheduler(
        hass.loop,
        transmit_queue,
        build_symbol_burst_signed,
        wire_gap_seconds=BURST_GAP_SECONDS,
    )
    return SunbellRuntimeData(
        transmit_service_name=transmit_service_name,
        remotes=remotes,
        transmit_queue=transmit_queue,
        scheduler=scheduler,
        default_full_movement_time=default_full_movement_time,
        at_anchor_settle_time=at_anchor_settle_time,
    )


def _coerce_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
