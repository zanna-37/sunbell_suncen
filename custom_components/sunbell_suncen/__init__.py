"""Sunbell SUNCEN integration entry point."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from ._protocol.synth import build_symbol_burst_signed
from .const import (
    ACTIONS,
    CONF_ACTION,
    CONF_CHANNELS,
    CONF_REMOTE,
    DOMAIN,
    REMOTES,
    SERVICE_SEND_GROUP,
)
from .models import SunbellConfigEntry, SunbellRuntimeData, build_runtime_data

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER]
ESPHOME_DOMAIN = "esphome"

SEND_GROUP_SCHEMA = vol.Schema({
    vol.Required(CONF_REMOTE): vol.In(REMOTES),
    vol.Required(CONF_CHANNELS): vol.All(
        cv.ensure_list,
        [vol.All(vol.Coerce(int), vol.Range(min=1, max=12))],
        vol.Length(min=1),
    ),
    vol.Required(CONF_ACTION): vol.In(ACTIONS),
})


async def async_setup_entry(hass: HomeAssistant, entry: SunbellConfigEntry) -> bool:
    runtime = build_runtime_data(entry)

    # Pre-flight: the ESPHome user service we'll call must exist. If the
    # ESPHome integration is still setting up its devices, services may not
    # be registered yet — raise ConfigEntryNotReady so HA retries with
    # backoff. If the user renamed/removed the ESPHome device, the retry
    # will keep failing and the user will see a clear setup error in the UI.
    if not hass.services.has_service(ESPHOME_DOMAIN, runtime.transmit_service_name):
        raise ConfigEntryNotReady(
            f"ESPHome service {ESPHOME_DOMAIN}.{runtime.transmit_service_name} "
            f"is not registered. The ESPHome device may still be connecting, or "
            f"the service was renamed/removed — reconfigure this integration if "
            f"so."
        )

    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _ensure_services_registered(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SunbellConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: SunbellConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _ensure_services_registered(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SEND_GROUP):
        return

    async def _handle_send_group(call: ServiceCall) -> None:
        data = SEND_GROUP_SCHEMA(dict(call.data))
        remote: str = data[CONF_REMOTE]
        channels = sorted(set(data[CONF_CHANNELS]))
        action: str = data[CONF_ACTION]

        runtime = _runtime_for_remote(hass, remote)
        if runtime is None:
            raise vol.Invalid(
                f"Remote {remote!r} is not configured in any Sunbell SUNCEN entry"
            )

        pulses = build_symbol_burst_signed(remote, channels, action)
        await hass.services.async_call(
            ESPHOME_DOMAIN,
            runtime.transmit_service_name,
            {"code": pulses},
            blocking=False,
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_GROUP, _handle_send_group, schema=SEND_GROUP_SCHEMA
    )


def _runtime_for_remote(hass: HomeAssistant, remote: str) -> SunbellRuntimeData | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime: SunbellRuntimeData = entry.runtime_data
        if any(rc.remote == remote for rc in runtime.remotes):
            return runtime
    return None
