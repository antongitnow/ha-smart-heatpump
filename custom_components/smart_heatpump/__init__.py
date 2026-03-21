"""Smart Heatpump Controller — Home Assistant custom integration.

Optimises heat pump operation based on COP, solar surplus, and weather forecast.
Controls the heat pump by adjusting a thermostat setpoint. All logic runs locally.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SmartHeatpumpCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smart Heatpump Controller from a config entry."""
    coordinator = SmartHeatpumpCoordinator(hass, entry)
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
