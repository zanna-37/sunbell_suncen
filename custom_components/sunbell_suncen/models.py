"""Typed runtime-data dataclasses + config-entry deserialization."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_BLINDS,
    CONF_CHANNEL,
    CONF_NAME,
    CONF_REMOTE_ID,
    CONF_REMOTES,
    CONF_TRANSMIT_SERVICE,
)


@dataclass(frozen=True, slots=True)
class BlindConfig:
    """One (remote, channel) blind."""
    remote: str
    channel: int
    name: str


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


type SunbellConfigEntry = ConfigEntry[SunbellRuntimeData]


def build_runtime_data(entry: SunbellConfigEntry) -> SunbellRuntimeData:
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
                )
                for b in rc[CONF_BLINDS]
            ),
        )
        for rc in remotes_raw
    )
    return SunbellRuntimeData(
        transmit_service_name=str(merged[CONF_TRANSMIT_SERVICE]),
        remotes=remotes,
    )
