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

from .const import (
    CONF_FORECAST_SOLAR,
    CONF_NOTIFY_TARGETS,
    CONF_P1_POWER,
    CONF_TEMP_SENSOR,
    CONF_THERMOSTAT,
    CONF_WEATHER,
    DOMAIN,
)


class SmartHeatpumpConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Smart Heatpump Controller."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the initial setup step — minimal, just gets it installed."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate required entities exist
            for key in (CONF_P1_POWER, CONF_WEATHER):
                entity_id = user_input.get(key)
                if entity_id and self.hass.states.get(entity_id) is None:
                    errors[key] = "entity_not_found"

            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title="Smart Heatpump Controller",
                    data={},
                    options=user_input,
                )

        data_schema = vol.Schema(
            {
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
    """Options flow — all entity settings are changeable here."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the options step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate entities that were provided
            for key in (CONF_THERMOSTAT, CONF_TEMP_SENSOR, CONF_P1_POWER, CONF_WEATHER):
                entity_id = user_input.get(key)
                if entity_id and self.hass.states.get(entity_id) is None:
                    errors[key] = "entity_not_found"

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_THERMOSTAT,
                    description={
                        "suggested_value": current.get(CONF_THERMOSTAT, "")
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                vol.Optional(
                    CONF_TEMP_SENSOR,
                    description={
                        "suggested_value": current.get(CONF_TEMP_SENSOR, "")
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="temperature",
                    )
                ),
                vol.Required(
                    CONF_P1_POWER,
                    default=current.get(CONF_P1_POWER, ""),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_WEATHER,
                    default=current.get(CONF_WEATHER, "weather.home"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
                vol.Optional(
                    CONF_FORECAST_SOLAR,
                    description={
                        "suggested_value": current.get(CONF_FORECAST_SOLAR, "")
                    },
                ): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=False)
                ),
                vol.Optional(
                    CONF_NOTIFY_TARGETS,
                    description={
                        "suggested_value": current.get(CONF_NOTIFY_TARGETS, "")
                    },
                ): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=False)
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
        )
