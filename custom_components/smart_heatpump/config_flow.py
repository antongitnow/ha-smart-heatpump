"""Config flow for Smart Heatpump Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_FORECAST_SOLAR, CONF_NOTIFY_TARGETS, CONF_P1_POWER, CONF_THERMOSTAT, CONF_WEATHER, DOMAIN


class SmartHeatpumpConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Smart Heatpump Controller."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate that selected entities exist
            for key in (CONF_THERMOSTAT, CONF_P1_POWER, CONF_WEATHER):
                entity_id = user_input.get(key)
                if entity_id and self.hass.states.get(entity_id) is None:
                    errors[key] = "entity_not_found"

            if not errors:
                # Prevent duplicate entries
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title="Smart Heatpump Controller",
                    data=user_input,
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_THERMOSTAT): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                vol.Required(CONF_P1_POWER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_WEATHER, default="weather.home"
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow handler."""
        return SmartHeatpumpOptionsFlow(config_entry)


class SmartHeatpumpOptionsFlow(OptionsFlow):
    """Options flow for Smart Heatpump Controller."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the options step."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_FORECAST_SOLAR,
                    default=current.get(CONF_FORECAST_SOLAR, ""),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                    )
                ),
                vol.Optional(
                    CONF_NOTIFY_TARGETS,
                    default=current.get(CONF_NOTIFY_TARGETS, ""),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        multiline=False,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema)
