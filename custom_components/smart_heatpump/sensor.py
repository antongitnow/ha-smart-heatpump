"""Sensor entity for Smart Heatpump Controller active rule."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, RULE_DESCRIPTIONS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SmartHeatpumpRuleSensor(coordinator)])


class SmartHeatpumpRuleSensor(SensorEntity):
    """Shows the currently active decision rule."""

    _attr_has_entity_name = True
    _attr_translation_key = "active_rule"
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_active_rule"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.entry.entry_id)},
            name="Smart Heatpump Controller",
            manufacturer="Smart Heatpump",
            model="v2",
        )

    @property
    def native_value(self) -> str:
        """Return the active rule name."""
        return self._coordinator.active_rule

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return the human-readable description as an attribute."""
        rule = self._coordinator.active_rule
        return {"description": RULE_DESCRIPTIONS.get(rule, rule)}

    async def async_added_to_hass(self) -> None:
        """Register for coordinator updates."""
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        """Handle coordinator data update."""
        self.async_write_ha_state()
