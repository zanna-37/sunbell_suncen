"""Sunbell SUNCEN integration entry point."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_platform,
    service as service_helper,
)

from .const import (
    ACTION_CLOSE,
    ACTION_OPEN,
    ACTION_SET_TILT_POSITION,
    ACTION_STOP,
    ACTIONS,
    CONF_ACTION,
    CONF_REMOTE_ID,
    CONF_REMOTES,
    CONF_TILT_POSITION,
    DOMAIN,
    RAW_ACTIONS,
    RAW_INVALIDATES_POSITION,
    SERVICE_SEND_GROUP,
    SERVICE_SEND_GROUP_RAW,
)
from .cover import SunbellBlind, tilt_position_to_level
from .models import SunbellConfigEntry, SunbellRuntimeData, build_runtime_data
from .scheduler import BurstStep

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER]
ESPHOME_DOMAIN = "esphome"

# Schema for send_group (HA-level commands: open / close / stop / set_tilt_position).
SEND_GROUP_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required(CONF_ACTION): vol.In(ACTIONS),
        vol.Optional(CONF_TILT_POSITION): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        ),
    }
)

# Schema for send_group_raw (passthrough SUNCEN burst names — no tilt tracking).
SEND_GROUP_RAW_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required(CONF_ACTION): vol.In(RAW_ACTIONS),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: SunbellConfigEntry) -> bool:
    runtime = build_runtime_data(hass, entry)

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

    runtime.transmit_queue.start()
    runtime.scheduler.start()
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _prune_orphan_devices(hass, entry, runtime)
    _ensure_services_registered(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SunbellConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.scheduler.async_close()
        await entry.runtime_data.transmit_queue.stop()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: SunbellConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Let users delete a Sunbell remote from the device card.

    Translates the device removal into dropping the matching remote from the
    entry's options; the update listener then reloads and prunes any leftover
    registry entries.
    """
    remote_to_remove = next(
        (
            ident_value
            for ident_domain, ident_value in device_entry.identifiers
            if ident_domain == DOMAIN
        ),
        None,
    )
    if remote_to_remove is None:
        return True

    current = list(entry.options.get(CONF_REMOTES, entry.data.get(CONF_REMOTES, [])))
    new_remotes = [r for r in current if r[CONF_REMOTE_ID] != remote_to_remove]
    if len(new_remotes) != len(current):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_REMOTES: new_remotes}
        )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SunbellConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _prune_orphan_devices(
    hass: HomeAssistant,
    entry: SunbellConfigEntry,
    runtime: SunbellRuntimeData,
) -> None:
    """Drop devices whose (DOMAIN, remote_id) is no longer in runtime.

    Removing the device cascades to its entities in the entity registry.
    """
    dev_reg = dr.async_get(hass)
    valid_remotes = {rc.remote for rc in runtime.remotes}
    for device in list(dr.async_entries_for_config_entry(dev_reg, entry.entry_id)):
        for ident_domain, ident_value in device.identifiers:
            if ident_domain == DOMAIN and ident_value not in valid_remotes:
                dev_reg.async_remove_device(device.id)
                break


# --- service registration ---------------------------------------------------


def _ensure_services_registered(hass: HomeAssistant) -> None:
    if not hass.services.has_service(DOMAIN, SERVICE_SEND_GROUP):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_GROUP,
            _make_send_group_handler(hass),
            schema=SEND_GROUP_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SEND_GROUP_RAW):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_GROUP_RAW,
            _make_send_group_raw_handler(hass),
            schema=SEND_GROUP_RAW_SCHEMA,
        )


def _make_send_group_handler(hass: HomeAssistant):
    async def _handle_send_group(call: ServiceCall) -> None:
        data = SEND_GROUP_SCHEMA(dict(call.data))
        action: str = data[CONF_ACTION]
        tilt_position: int | None = data.get(CONF_TILT_POSITION)

        if action == ACTION_SET_TILT_POSITION and tilt_position is None:
            raise vol.Invalid(
                "tilt_position is required when action is 'set_tilt_position'."
            )
        if action != ACTION_SET_TILT_POSITION and tilt_position is not None:
            raise vol.Invalid(
                "tilt_position only applies when action is 'set_tilt_position'."
            )

        resolved = _resolve_targets(hass, call)
        if not resolved:
            raise vol.Invalid("send_group needs target entities or devices.")

        target_level = (
            tilt_position_to_level(tilt_position) if tilt_position is not None else None
        )

        for blind in resolved:
            chain = _build_group_chain(action, blind, target_level)
            if not chain:
                continue
            blind.runtime.scheduler.submit(blind.key, chain, entity=blind)

    return _handle_send_group


def _make_send_group_raw_handler(hass: HomeAssistant):
    async def _handle_send_group_raw(call: ServiceCall) -> None:
        data = SEND_GROUP_RAW_SCHEMA(dict(call.data))
        action: str = data[CONF_ACTION]
        resolved = _resolve_targets(hass, call)
        if not resolved:
            raise vol.Invalid("send_group_raw needs target entities or devices.")
        invalidate_position = action in RAW_INVALIDATES_POSITION
        for blind in resolved:
            chain = blind.build_raw_chain(action, invalidate_position=invalidate_position)
            blind.runtime.scheduler.submit(blind.key, chain, entity=blind)

    return _handle_send_group_raw


# --- target resolution ------------------------------------------------------


def _resolve_targets(
    hass: HomeAssistant,
    call: ServiceCall,
) -> list["SunbellBlind"]:
    """Collect live SunbellBlind entities from the call's entity/device targets."""
    blinds = _blinds_by_entity_id(hass)
    selected = service_helper.async_extract_referenced_entity_ids(hass, call)
    return [
        blinds[entity_id]
        for entity_id in selected.referenced | selected.indirectly_referenced
        if entity_id in blinds
    ]


# --- chain builders ---------------------------------------------------------


def _build_group_chain(
    action: str,
    blind: "SunbellBlind",
    target_level: int | None,
) -> list[BurstStep]:
    """Build the scheduler chain for one HA-level op on a live blind."""
    if action == ACTION_OPEN:
        return blind.build_open_chain()
    if action == ACTION_CLOSE:
        return blind.build_close_chain()
    if action == ACTION_STOP:
        return blind.build_stop_chain()
    if action == ACTION_SET_TILT_POSITION:
        assert target_level is not None
        return blind.build_tilt_chain(target_level)
    raise ValueError(f"unknown send_group action {action!r}")


# --- entity lookup ----------------------------------------------------------


def _blinds_by_entity_id(hass: HomeAssistant) -> dict[str, SunbellBlind]:
    """All currently-registered SunbellBlind entities, indexed by entity_id."""
    out: dict[str, SunbellBlind] = {}
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        for entity_id, entity in platform.entities.items():
            if isinstance(entity, SunbellBlind):
                out[entity_id] = entity
    return out
