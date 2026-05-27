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
    CONF_NAME,
    CONF_REMOTE_ID,
    CONF_REMOTES,
    CONF_TRANSMIT_SERVICE,
    DOMAIN,
    REMOTES,
)

ESPHOME_DOMAIN = "esphome"
TRANSMIT_SERVICE_SUFFIX = "_transmit_raw"


def _list_transmit_services(hass: HomeAssistant) -> list[str]:
    """ESPHome user services that look like SUNCEN transmitters."""
    services = hass.services.async_services().get(ESPHOME_DOMAIN, {})
    return sorted(name for name in services if name.endswith(TRANSMIT_SERVICE_SUFFIX))


def _channels_to_blinds(channels: list[int], remote_id: str) -> list[dict[str, Any]]:
    """Default name for each channel = 'chN'; user can rename in HA's entity UI."""
    return [{CONF_CHANNEL: c, CONF_NAME: f"ch{c}"} for c in channels]


class SunbellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two-step config flow: pick transmit service, then add the first remote."""

    VERSION = 1

    def __init__(self) -> None:
        self._transmit_service: str | None = None
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
    """Add or remove remotes post-install."""

    async def async_step_init(self, _user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_remote", "remove_remote"],
        )

    async def async_step_add_remote(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = list(self.config_entry.options.get(CONF_REMOTES,
                                                    self.config_entry.data.get(CONF_REMOTES, [])))
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
                    return self.async_create_entry(
                        title="",
                        data={CONF_REMOTES: current},
                    )

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
        current = list(self.config_entry.options.get(CONF_REMOTES,
                                                    self.config_entry.data.get(CONF_REMOTES, [])))
        if not current:
            return self.async_abort(reason="no_remote_to_remove")

        if user_input is not None:
            remote_id = user_input[CONF_REMOTE_ID]
            current = [r for r in current if r[CONF_REMOTE_ID] != remote_id]
            return self.async_create_entry(title="", data={CONF_REMOTES: current})

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
