"""Smart Heatpump Controller — Home Assistant custom integration.

Optimises heat pump operation based on COP, solar surplus, and weather forecast.
Controls the heat pump by adjusting a thermostat setpoint. All logic runs locally.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import CARD_URL, DOMAIN
from .coordinator import SmartHeatpumpCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the Lovelace card and load it on every dashboard."""
    card_path = Path(__file__).parent / "frontend" / "smart-heatpump-card.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(card_path), True)]
    )
    # Version query string busts the browser cache after an update
    integration = await async_get_integration(hass, DOMAIN)
    add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smart Heatpump Controller from a config entry."""
    coordinator = SmartHeatpumpCoordinator(hass, entry)
    await coordinator.thermal_store.async_load()
    await coordinator.async_load_energy()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Create all entities (number sliders, sensor, switch)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start evaluation loop (entities have restored their values by now)
    coordinator.async_start()

    # Listen for options updates (Forecast.Solar, notify targets)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("Smart Heatpump Controller started")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: SmartHeatpumpCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    _LOGGER.info("Smart Heatpump Controller stopped")
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
