"""Sunbell SUNCEN integration entry point."""
from __future__ import annotations

import logging
from collections import defaultdict

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_platform,
    entity_registry as er,
    service as service_helper,
)

from ._protocol.synth import build_symbol_burst_signed
from .const import (
    ACTION_CLOSE,
    ACTION_OPEN,
    ACTION_SET_TILT_POSITION,
    ACTION_STOP,
    ACTIONS,
    CONF_ACTION,
    CONF_CHANNELS,
    CONF_REMOTE,
    CONF_REMOTE_ID,
    CONF_REMOTES,
    CONF_TILT_POSITION,
    DOMAIN,
    REMOTES,
    SERVICE_SEND_GROUP,
    TILT_LEVELS,
    TILT_LEVEL_DOWN_ANCHOR,
    TILT_LEVEL_UP_ANCHOR,
)
from .cover import SunbellBlind, tilt_position_to_level
from .models import SunbellConfigEntry, SunbellRuntimeData, build_runtime_data

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER]
ESPHOME_DOMAIN = "esphome"

# Accept either entity/device/area targets (resolved at call time) or a raw
# remote + channels pair. cv.make_entity_service_schema injects the target
# fields (entity_id, device_id, area_id, floor_id, label_id) as Optional.
SEND_GROUP_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional(CONF_REMOTE): vol.In(REMOTES),
        vol.Optional(CONF_CHANNELS): vol.All(
            cv.ensure_list,
            [vol.All(vol.Coerce(int), vol.Range(min=1, max=12))],
        ),
        vol.Required(CONF_ACTION): vol.In(ACTIONS),
        vol.Optional(CONF_TILT_POSITION): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        ),
    }
)


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
    _prune_orphan_devices(hass, entry, runtime)
    _ensure_services_registered(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SunbellConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


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


def _plan_bursts(action: str, target_level: int | None) -> list[str]:
    """Minimum SUNCEN burst sequence to drive a group to the desired end state.

    For open/close/stop: a single fast burst is enough. For set_tilt_position:
    anchor at the closer extreme (DOWN for levels 1..4, UP for levels 5..7),
    then step toward the target with LONG_UP / LONG_DOWN. Anchoring directly
    onto an extreme (1 or 7) needs no stepping and no extra-step padding.
    """
    if action == ACTION_OPEN:
        return ["UP"]
    if action == ACTION_CLOSE:
        return ["DOWN"]
    if action == ACTION_STOP:
        return ["STOP"]
    assert action == ACTION_SET_TILT_POSITION
    if target_level == TILT_LEVEL_DOWN_ANCHOR:
        return ["DOWN"]
    if target_level == TILT_LEVEL_UP_ANCHOR:
        return ["UP"]
    if target_level <= TILT_LEVELS // 2 + 1:
        return ["DOWN", *["LONG_UP"] * (target_level - TILT_LEVEL_DOWN_ANCHOR)]
    return ["UP", *["LONG_DOWN"] * (TILT_LEVEL_UP_ANCHOR - target_level)]


def _end_state(
    action: str, target_level: int | None
) -> tuple[str, int, int] | None:
    """(last_direction, tilt_level, position) after _plan_bursts; None means no update."""
    if action == ACTION_OPEN:
        return "UP", TILT_LEVEL_UP_ANCHOR, 100
    if action == ACTION_CLOSE:
        return "DOWN", TILT_LEVEL_DOWN_ANCHOR, 0
    if action == ACTION_STOP:
        return None
    assert action == ACTION_SET_TILT_POSITION and target_level is not None
    if target_level <= TILT_LEVELS // 2 + 1:
        return "DOWN", target_level, 0
    return "UP", target_level, 100


def _ensure_services_registered(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SEND_GROUP):
        return

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
        target_level = (
            tilt_position_to_level(tilt_position)
            if action == ACTION_SET_TILT_POSITION
            else None
        )

        channels_by_remote: dict[str, set[int]] = defaultdict(set)
        entities_by_remote: dict[str, list[SunbellBlind]] = defaultdict(list)

        # Target mode: resolve entity/device/area picks to (remote, channel) pairs.
        selected = service_helper.async_extract_referenced_entity_ids(hass, call)
        entity_ids = selected.referenced | selected.indirectly_referenced
        if entity_ids:
            ent_reg = er.async_get(hass)
            blinds = _blinds_by_entity_id(hass)
            for entity_id in entity_ids:
                reg_entry = ent_reg.async_get(entity_id)
                if reg_entry is None or reg_entry.platform != DOMAIN:
                    continue
                remote, channel = _parse_unique_id(reg_entry.unique_id)
                if remote is None:
                    continue
                channels_by_remote[remote].add(channel)
                blind = blinds.get(entity_id)
                if blind is not None:
                    entities_by_remote[remote].append(blind)

        # Raw mode: explicit remote + channels. End state still gets applied to
        # any entities matching the channels (so the UI stays consistent).
        raw_remote = data.get(CONF_REMOTE)
        raw_channels = data.get(CONF_CHANNELS) or []
        if raw_remote is not None and raw_channels:
            channels_by_remote[raw_remote].update(int(c) for c in raw_channels)
            blinds = _blinds_by_entity_id(hass)
            for blind in blinds.values():
                if (
                    blind.remote_id == raw_remote
                    and blind.channel in channels_by_remote[raw_remote]
                    and blind not in entities_by_remote[raw_remote]
                ):
                    entities_by_remote[raw_remote].append(blind)
        elif (raw_remote is None) ^ (not raw_channels):
            raise vol.Invalid(
                "Provide 'remote' and 'channels' together, or omit both and "
                "select target entities/devices."
            )

        if not channels_by_remote:
            raise vol.Invalid(
                "send_group needs target entities/devices or a remote + channels pair."
            )

        plan = _plan_bursts(action, target_level)
        end_state = _end_state(action, target_level)

        for remote, channel_set in channels_by_remote.items():
            runtime = _runtime_for_remote(hass, remote)
            if runtime is None:
                raise vol.Invalid(
                    f"Remote {remote!r} is not configured in any Sunbell SUNCEN entry"
                )
            channels = sorted(channel_set)
            for burst_action in plan:
                pulses = build_symbol_burst_signed(remote, channels, burst_action)
                await hass.services.async_call(
                    ESPHOME_DOMAIN,
                    runtime.transmit_service_name,
                    {"code": pulses},
                    blocking=False,
                )
            if end_state is not None:
                last_direction, tilt_level, position = end_state
                for blind in entities_by_remote.get(remote, ()):
                    blind.apply_group_update(last_direction, tilt_level, position)

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_GROUP, _handle_send_group, schema=SEND_GROUP_SCHEMA
    )


def _blinds_by_entity_id(hass: HomeAssistant) -> dict[str, SunbellBlind]:
    """All currently-registered SunbellBlind entities, indexed by entity_id."""
    out: dict[str, SunbellBlind] = {}
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        for entity_id, entity in platform.entities.items():
            if isinstance(entity, SunbellBlind):
                out[entity_id] = entity
    return out


def _parse_unique_id(unique_id: str) -> tuple[str | None, int]:
    """Extract (remote, channel) from a SunbellBlind unique_id '{remote}_ch{channel}'."""
    remote, sep, ch = unique_id.partition("_ch")
    if not sep or not ch.isdigit():
        return None, 0
    return remote, int(ch)


def _runtime_for_remote(hass: HomeAssistant, remote: str) -> SunbellRuntimeData | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime: SunbellRuntimeData = entry.runtime_data
        if any(rc.remote == remote for rc in runtime.remotes):
            return runtime
    return None
