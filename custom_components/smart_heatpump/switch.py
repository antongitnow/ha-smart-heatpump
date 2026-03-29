"""Switch entity for Smart Heatpump Controller notifications toggle."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        SmartHeatpumpNotificationSwitch(coordinator),
        SmartHeatpumpDryRunSwitch(coordinator),
    ])


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


class SmartHeatpumpDryRunSwitch(RestoreEntity, SwitchEntity):
    """Toggle dry run mode — decisions are logged but thermostat is not touched."""

    _attr_has_entity_name = True
    _attr_translation_key = "dry_run"
    _attr_icon = "mdi:test-tube"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_dry_run"
        # Default: on if no thermostat configured, off otherwise
        self._attr_is_on = not coordinator.entry.data.get("thermostat_entity")

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
        # If no thermostat configured, always force dry run on
        if not self._coordinator._opt("thermostat_entity"):
            self._attr_is_on = True
        else:
            last = await self.async_get_last_state()
            if last and last.state is not None:
                self._attr_is_on = last.state == "on"
        self._coordinator.dry_run_enabled = self._attr_is_on

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on dry run mode."""
        self._attr_is_on = True
        self._coordinator.dry_run_enabled = True
        self.async_write_ha_state()
        await self._send_mode_notification("Dry run mode enabled — thermostat will not be touched")

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off dry run mode."""
        if not self._coordinator._opt("thermostat_entity"):
            # Can't disable dry run without a thermostat
            return
        self._attr_is_on = False
        self._coordinator.dry_run_enabled = False
        self.async_write_ha_state()
        await self._send_mode_notification("Dry run mode disabled — thermostat control is now active")

    async def _send_mode_notification(self, message: str) -> None:
        """Send a notification about dry run mode change."""
        targets = self._coordinator.notify_targets
        if not targets:
            return
        for target_name in targets:
            try:
                await self.hass.services.async_call(
                    "notify",
                    target_name,
                    {"title": "Smart Heatpump", "message": message},
                    blocking=True,
                )
            except Exception:
                _LOGGER.warning("Failed to send dry run notification to '%s'", target_name)
