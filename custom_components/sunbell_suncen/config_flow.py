"""Config + options flow for Sunbell SUNCEN."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from ._protocol.channels import parse_channels
from .const import (
    CONF_BLINDS,
    CONF_CHANNEL,
    CONF_FULL_MOVEMENT_TIME,
    CONF_NAME,
    CONF_REMOTE_ID,
    CONF_REMOTES,
    CONF_TRANSMIT_SERVICE,
    DEFAULT_FULL_MOVEMENT_TIME,
    DOMAIN,
    REMOTES,
)

ESPHOME_DOMAIN = "esphome"
TRANSMIT_SERVICE_SUFFIX = "_transmit_raw"

# Bounds for the travel-time selector. 1s is the floor (anything shorter is
# meaningless for a real blind motor); 600s (10 min) is a generous upper bound
# for very large installations.
TRAVEL_TIME_MIN = 1
TRAVEL_TIME_MAX = 600


def _list_transmit_services(hass: HomeAssistant) -> list[str]:
    """ESPHome user services that look like SUNCEN transmitters."""
    services = hass.services.async_services().get(ESPHOME_DOMAIN, {})
    return sorted(name for name in services if name.endswith(TRANSMIT_SERVICE_SUFFIX))


def _channels_to_blinds(channels: list[int], remote_id: str) -> list[dict[str, Any]]:
    """Default name = 'Sunbell R{remote} ch{channel}'; user can rename in HA's entity UI."""
    return [
        {CONF_CHANNEL: c, CONF_NAME: f"Sunbell R{remote_id} ch{c}"}
        for c in channels
    ]


def _travel_time_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=TRAVEL_TIME_MIN,
            max=TRAVEL_TIME_MAX,
            step=1,
            unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _blind_picker_options(remotes: list[dict[str, Any]]) -> list[selector.SelectOptionDict]:
    """Flatten remotes -> per-blind picker entries: 'r{remote}_ch{channel}' = label."""
    options: list[selector.SelectOptionDict] = []
    for rc in remotes:
        for b in rc.get(CONF_BLINDS, []):
            ch = int(b[CONF_CHANNEL])
            key = f"{rc[CONF_REMOTE_ID]}|{ch}"
            label = f"r{rc[CONF_REMOTE_ID]} ch{ch} ({b.get(CONF_NAME, f'ch{ch}')})"
            options.append({"value": key, "label": label})
    return options


class SunbellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two-step config flow: pick transmit service, then add the first remote."""

    VERSION = 1

    def __init__(self) -> None:
        self._transmit_service: str | None = None
        self._default_travel_time: int = DEFAULT_FULL_MOVEMENT_TIME
        self._remotes: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        available = _list_transmit_services(self.hass)
        if not available:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({}),
                errors={"base": "no_transmit_services"},
            )

        if user_input is not None:
            self._transmit_service = user_input[CONF_TRANSMIT_SERVICE]
            self._default_travel_time = int(user_input[CONF_FULL_MOVEMENT_TIME])
            return await self.async_step_remote()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_TRANSMIT_SERVICE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=available,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_FULL_MOVEMENT_TIME,
                    default=DEFAULT_FULL_MOVEMENT_TIME,
                ): _travel_time_selector(),
            }),
        )

    async def async_step_remote(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            remote_id = user_input[CONF_REMOTE_ID]
            try:
                channels = parse_channels(user_input["channels"])
            except ValueError:
                errors["channels"] = "invalid_channels"
            else:
                if any(r[CONF_REMOTE_ID] == remote_id for r in self._remotes):
                    errors[CONF_REMOTE_ID] = "duplicate_remote"
                elif not channels:
                    errors["channels"] = "no_channels"
                else:
                    self._remotes.append({
                        CONF_REMOTE_ID: remote_id,
                        CONF_BLINDS: _channels_to_blinds(channels, remote_id),
                    })
                    if user_input.get("add_another"):
                        return await self.async_step_remote()
                    return self.async_create_entry(
                        title=f"Sunbell SUNCEN ({len(self._remotes)} remote(s))",
                        data={
                            CONF_TRANSMIT_SERVICE: self._transmit_service,
                            CONF_FULL_MOVEMENT_TIME: self._default_travel_time,
                            CONF_REMOTES: self._remotes,
                        },
                    )

        return self.async_show_form(
            step_id="remote",
            data_schema=vol.Schema({
                vol.Required(CONF_REMOTE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(REMOTES))
                ),
                vol.Required("channels"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional("add_another", default=False): selector.BooleanSelector(),
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SunbellOptionsFlow()


class SunbellOptionsFlow(OptionsFlow):
    """Add/remove remotes, set the default travel time, or override a blind's travel time."""

    def __init__(self) -> None:
        self._selected_blind: tuple[str, int] | None = None

    async def async_step_init(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "add_remote",
                "remove_remote",
                "set_default_travel_time",
                "configure_blind",
            ],
        )

    async def async_step_add_remote(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self._current_remotes()
        if user_input is not None:
            remote_id = user_input[CONF_REMOTE_ID]
            try:
                channels = parse_channels(user_input["channels"])
            except ValueError:
                errors["channels"] = "invalid_channels"
            else:
                if any(r[CONF_REMOTE_ID] == remote_id for r in current):
                    errors[CONF_REMOTE_ID] = "duplicate_remote"
                elif not channels:
                    errors["channels"] = "no_channels"
                else:
                    current.append({
                        CONF_REMOTE_ID: remote_id,
                        CONF_BLINDS: _channels_to_blinds(channels, remote_id),
                    })
                    return self._persist_remotes(current)

        return self.async_show_form(
            step_id="add_remote",
            data_schema=vol.Schema({
                vol.Required(CONF_REMOTE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(REMOTES))
                ),
                vol.Required("channels"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
            }),
            errors=errors,
        )

    async def async_step_remove_remote(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self._current_remotes()
        if not current:
            return self.async_abort(reason="no_remote_to_remove")

        if user_input is not None:
            remote_id = user_input[CONF_REMOTE_ID]
            current = [r for r in current if r[CONF_REMOTE_ID] != remote_id]
            return self._persist_remotes(current)

        return self.async_show_form(
            step_id="remove_remote",
            data_schema=vol.Schema({
                vol.Required(CONF_REMOTE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[r[CONF_REMOTE_ID] for r in current]
                    )
                ),
            }),
        )

    async def async_step_set_default_travel_time(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current_default = self._current_default_travel_time()
        if user_input is not None:
            new_options = {
                **self.config_entry.options,
                CONF_REMOTES: self._current_remotes(),
                CONF_FULL_MOVEMENT_TIME: int(user_input[CONF_FULL_MOVEMENT_TIME]),
            }
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="set_default_travel_time",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_FULL_MOVEMENT_TIME,
                    default=current_default,
                ): _travel_time_selector(),
            }),
        )

    async def async_step_configure_blind(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self._current_remotes()
        options = _blind_picker_options(current)
        if not options:
            return self.async_abort(reason="no_blinds")

        if user_input is not None:
            remote_id, ch_str = user_input["blind"].split("|", 1)
            self._selected_blind = (remote_id, int(ch_str))
            return await self.async_step_set_blind_travel_time()

        return self.async_show_form(
            step_id="configure_blind",
            data_schema=vol.Schema({
                vol.Required("blind"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
        )

    async def async_step_set_blind_travel_time(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._selected_blind is not None
        remote_id, channel = self._selected_blind
        current = self._current_remotes()
        existing_override = _find_blind_override(current, remote_id, channel)
        default = (
            existing_override
            if existing_override is not None
            else self._current_default_travel_time()
        )

        if user_input is not None:
            override_raw = user_input.get(CONF_FULL_MOVEMENT_TIME)
            use_default = bool(user_input.get("use_default"))
            override = None if use_default else int(override_raw)
            updated = _apply_blind_override(current, remote_id, channel, override)
            return self._persist_remotes(updated)

        return self.async_show_form(
            step_id="set_blind_travel_time",
            data_schema=vol.Schema({
                vol.Optional("use_default", default=existing_override is None):
                    selector.BooleanSelector(),
                vol.Required(
                    CONF_FULL_MOVEMENT_TIME,
                    default=default,
                ): _travel_time_selector(),
            }),
            description_placeholders={
                "remote": remote_id,
                "channel": str(channel),
                "default": str(self._current_default_travel_time()),
            },
        )

    # --- internal helpers ---------------------------------------------------

    def _current_remotes(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(
            CONF_REMOTES,
            self.config_entry.data.get(CONF_REMOTES, []),
        ))

    def _current_default_travel_time(self) -> int:
        return int(self.config_entry.options.get(
            CONF_FULL_MOVEMENT_TIME,
            self.config_entry.data.get(CONF_FULL_MOVEMENT_TIME, DEFAULT_FULL_MOVEMENT_TIME),
        ))

    def _persist_remotes(self, remotes: list[dict[str, Any]]) -> ConfigFlowResult:
        new_options = {
            **self.config_entry.options,
            CONF_REMOTES: remotes,
            CONF_FULL_MOVEMENT_TIME: self._current_default_travel_time(),
        }
        return self.async_create_entry(title="", data=new_options)


def _find_blind_override(
    remotes: list[dict[str, Any]], remote_id: str, channel: int
) -> int | None:
    for rc in remotes:
        if rc[CONF_REMOTE_ID] != remote_id:
            continue
        for b in rc.get(CONF_BLINDS, []):
            if int(b[CONF_CHANNEL]) == channel:
                val = b.get(CONF_FULL_MOVEMENT_TIME)
                return int(val) if val not in (None, "") else None
    return None


def _apply_blind_override(
    remotes: list[dict[str, Any]], remote_id: str, channel: int, override: int | None
) -> list[dict[str, Any]]:
    """Return a new remotes list with the override applied to the matching blind."""
    out: list[dict[str, Any]] = []
    for rc in remotes:
        if rc[CONF_REMOTE_ID] != remote_id:
            out.append(rc)
            continue
        new_blinds: list[dict[str, Any]] = []
        for b in rc.get(CONF_BLINDS, []):
            if int(b[CONF_CHANNEL]) != channel:
                new_blinds.append(b)
                continue
            updated = {**b}
            if override is None:
                updated.pop(CONF_FULL_MOVEMENT_TIME, None)
            else:
                updated[CONF_FULL_MOVEMENT_TIME] = override
            new_blinds.append(updated)
        out.append({**rc, CONF_BLINDS: new_blinds})
    return out
