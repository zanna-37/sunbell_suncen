"""Sunbell SUNCEN integration entry point."""
from __future__ import annotations

import asyncio
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
    RAW_ACTIONS,
    RAW_INVALIDATES_POSITION,
    REMOTES,
    SERVICE_SEND_GROUP,
    SERVICE_SEND_GROUP_RAW,
    TILT_LEVEL_DOWN_ANCHOR,
    TILT_LEVEL_UP_ANCHOR,
)
from .cover import SunbellBlind, tilt_position_to_level
from .models import SunbellConfigEntry, SunbellRuntimeData, build_runtime_data

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER]
ESPHOME_DOMAIN = "esphome"

# Schema for send_group (HA-level commands: open / close / stop / set_tilt_position).
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

# Schema for send_group_raw (passthrough SUNCEN burst names — no tilt tracking).
SEND_GROUP_RAW_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional(CONF_REMOTE): vol.In(REMOTES),
        vol.Optional(CONF_CHANNELS): vol.All(
            cv.ensure_list,
            [vol.All(vol.Coerce(int), vol.Range(min=1, max=12))],
        ),
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
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _prune_orphan_devices(hass, entry, runtime)
    _ensure_services_registered(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SunbellConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
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

        resolved = _resolve_targets(
            hass, call, data,
            allow_raw_without_entity=action != ACTION_SET_TILT_POSITION,
        )
        if not resolved:
            raise vol.Invalid(
                "send_group needs target entities/devices or a remote + channels pair."
            )

        if action == ACTION_SET_TILT_POSITION:
            await _execute_tilt(hass, resolved, tilt_position_to_level(tilt_position))
        else:
            await _execute_fast(hass, resolved, action)

    return _handle_send_group


def _make_send_group_raw_handler(hass: HomeAssistant):
    async def _handle_send_group_raw(call: ServiceCall) -> None:
        data = SEND_GROUP_RAW_SCHEMA(dict(call.data))
        action: str = data[CONF_ACTION]
        resolved = _resolve_targets(hass, call, data, allow_raw_without_entity=True)
        if not resolved:
            raise vol.Invalid(
                "send_group_raw needs target entities/devices or a remote + channels pair."
            )
        await _execute_raw(hass, resolved, action)

    return _handle_send_group_raw


# --- target resolution ------------------------------------------------------


def _resolve_targets(
    hass: HomeAssistant,
    call: ServiceCall,
    data: dict,
    *,
    allow_raw_without_entity: bool,
) -> list[tuple[str, int, "SunbellBlind | None"]]:
    """Collect (remote, channel, blind) triples from entity/device targets and raw mode.

    Each triple represents one channel to act on. `blind` is the live entity
    when known (target mode and raw mode for already-configured channels) or
    None when only raw remote/channels were given for an unconfigured channel.
    `allow_raw_without_entity=False` rejects raw mode entirely — used by tilt
    since it needs each blind's current tilt state.
    """
    resolved: list[tuple[str, int, SunbellBlind | None]] = []
    seen: set[tuple[str, int]] = set()
    blinds = _blinds_by_entity_id(hass)
    blind_by_key: dict[tuple[str, int], SunbellBlind] = {
        (b.remote_id, b.channel): b for b in blinds.values()
    }

    selected = service_helper.async_extract_referenced_entity_ids(hass, call)
    entity_ids = selected.referenced | selected.indirectly_referenced
    if entity_ids:
        ent_reg = er.async_get(hass)
        for entity_id in entity_ids:
            reg_entry = ent_reg.async_get(entity_id)
            if reg_entry is None or reg_entry.platform != DOMAIN:
                continue
            remote, channel = _parse_unique_id(reg_entry.unique_id)
            if remote is None:
                continue
            key = (remote, channel)
            if key in seen:
                continue
            seen.add(key)
            resolved.append((remote, channel, blinds.get(entity_id)))

    raw_remote = data.get(CONF_REMOTE)
    raw_channels = data.get(CONF_CHANNELS) or []
    if raw_remote is not None and raw_channels:
        if not allow_raw_without_entity:
            raise vol.Invalid(
                "set_tilt_position requires entity/device targets so each "
                "blind's current tilt state is known. Raw remote/channels "
                "only supports open / close / stop."
            )
        for c in raw_channels:
            ch = int(c)
            key = (raw_remote, ch)
            if key in seen:
                continue
            seen.add(key)
            resolved.append((raw_remote, ch, blind_by_key.get(key)))
    elif (raw_remote is None) ^ (not raw_channels):
        raise vol.Invalid(
            "Provide 'remote' and 'channels' together, or omit both and "
            "select target entities/devices."
        )

    return resolved


# --- execute paths ----------------------------------------------------------


def _settle_target_for(action: str) -> tuple[int, int] | None:
    """Where a fast burst should land state after `full_movement_time`."""
    if action == ACTION_OPEN:
        return TILT_LEVEL_UP_ANCHOR, 100
    if action == ACTION_CLOSE:
        return TILT_LEVEL_DOWN_ANCHOR, 0
    return None   # STOP: state stays unknown


async def _execute_fast(
    hass: HomeAssistant,
    resolved: list[tuple[str, int, "SunbellBlind | None"]],
    action: str,
) -> None:
    """One fast burst per remote (multi-channel union); each blind schedules its own settle."""
    channels_by_remote, entities_by_remote = _partition(resolved)
    if action == ACTION_OPEN:
        burst = "UP"
    elif action == ACTION_CLOSE:
        burst = "DOWN"
    elif action == ACTION_STOP:
        burst = "STOP"
    else:
        raise ValueError(f"_execute_fast called with non-fast action {action!r}")

    settle = _settle_target_for(action)

    for remote, channel_set in channels_by_remote.items():
        runtime = _runtime_for_remote(hass, remote)
        if runtime is None:
            raise vol.Invalid(
                f"Remote {remote!r} is not configured in any Sunbell SUNCEN entry"
            )
        channels = sorted(channel_set)
        pulses = build_symbol_burst_signed(remote, channels, burst)
        await runtime.transmit_queue.send(pulses)
        for blind in entities_by_remote[remote]:
            if settle is None:
                blind.begin_motion_for_group(None, None)
            else:
                blind.begin_motion_for_group(settle[0], settle[1])


async def _execute_raw(
    hass: HomeAssistant,
    resolved: list[tuple[str, int, "SunbellBlind | None"]],
    action: str,
) -> None:
    """Pass-through SUNCEN burst; invalidate tilt and (for UP/DOWN/STOP) position."""
    channels_by_remote, entities_by_remote = _partition(resolved)
    invalidate_position = action in RAW_INVALIDATES_POSITION

    for remote, channel_set in channels_by_remote.items():
        runtime = _runtime_for_remote(hass, remote)
        if runtime is None:
            raise vol.Invalid(
                f"Remote {remote!r} is not configured in any Sunbell SUNCEN entry"
            )
        channels = sorted(channel_set)
        pulses = build_symbol_burst_signed(remote, channels, action)
        await runtime.transmit_queue.send(pulses)
        for blind in entities_by_remote[remote]:
            blind.invalidate_for_raw(invalidate_position)


async def _execute_tilt(
    hass: HomeAssistant,
    resolved: list[tuple[str, int, "SunbellBlind | None"]],
    target_level: int,
) -> None:
    """Drive each remote's group to target_level: ensure full-down, then LONG_DOWN × delta.

    For every remote in the resolved set:
    1. If any blind isn't already at full-down, fire a multi-channel DOWN
       burst and wait the longest `full_movement_time` among the non-anchored
       blinds.
    2. Walk `7 - target_level` multi-channel LONG_DOWN bursts. Each blind's
       tilt_level is then set to target_level (position stays 0).

    Channels with no live entity in the group are sent the bursts but
    don't update state (no entity to update).
    """
    per_remote: dict[str, list[SunbellBlind]] = defaultdict(list)
    for remote, _channel, blind in resolved:
        if blind is not None and blind not in per_remote[remote]:
            per_remote[remote].append(blind)

    if not per_remote:
        raise vol.Invalid(
            "set_tilt_position needs target entities (or devices) — no live "
            "Sunbell cover entity was found in the target."
        )

    for remote, blinds in per_remote.items():
        runtime = _runtime_for_remote(hass, remote)
        if runtime is None:
            raise vol.Invalid(
                f"Remote {remote!r} is not configured in any Sunbell SUNCEN entry"
            )
        channels = sorted(b.channel for b in blinds)

        not_anchored: list[SunbellBlind] = []
        for b in blinds:
            if not await b.at_full_down_with_settled_state():
                not_anchored.append(b)

        if not_anchored:
            pulses = build_symbol_burst_signed(remote, channels, "DOWN")
            await runtime.transmit_queue.send(pulses)
            for b in blinds:
                b.begin_motion_for_group(None, None)
            wait_time = max(b.travel_time for b in not_anchored)
            await asyncio.sleep(wait_time)
            for b in blinds:
                b.commit_full_down()

        delta = TILT_LEVEL_DOWN_ANCHOR - target_level
        for _ in range(delta):
            pulses = build_symbol_burst_signed(remote, channels, "LONG_DOWN")
            await runtime.transmit_queue.send(pulses)
        for b in blinds:
            b.commit_tilt_target(target_level)


def _partition(
    resolved: list[tuple[str, int, "SunbellBlind | None"]],
) -> tuple[dict[str, set[int]], dict[str, list[SunbellBlind]]]:
    """Group resolved triples by remote: channels (union of ints) + entities (live blinds)."""
    channels_by_remote: dict[str, set[int]] = defaultdict(set)
    entities_by_remote: dict[str, list[SunbellBlind]] = defaultdict(list)
    for remote, channel, blind in resolved:
        channels_by_remote[remote].add(channel)
        if blind is not None and blind not in entities_by_remote[remote]:
            entities_by_remote[remote].append(blind)
    return channels_by_remote, entities_by_remote


# --- entity lookup ----------------------------------------------------------


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
