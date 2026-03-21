"""Switch entity for Smart Heatpump Controller notifications toggle."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SmartHeatpumpNotificationSwitch(coordinator)])


class SmartHeatpumpNotificationSwitch(RestoreEntity, SwitchEntity):
    """Toggle push notifications on/off."""

    _attr_has_entity_name = True
    _attr_translation_key = "notifications_enabled"
    _attr_icon = "mdi:bell-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_notifications"
        self._attr_is_on = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.entry.entry_id)},
            name="Smart Heatpump Controller",
            manufacturer="Smart Heatpump",
            model="v2",
        )

    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            self._attr_is_on = last.state == "on"
        self._coordinator.notifications_enabled = self._attr_is_on

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on notifications."""
        self._attr_is_on = True
        self._coordinator.notifications_enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off notifications."""
        self._attr_is_on = False
        self._coordinator.notifications_enabled = False
        self.async_write_ha_state()
