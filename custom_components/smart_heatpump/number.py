"""Number entities for Smart Heatpump Controller configuration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULTS, DOMAIN, NUMBER_DEFINITIONS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        SmartHeatpumpNumber(coordinator, key, name, min_val, max_val, step, unit, icon)
        for key, name, min_val, max_val, step, unit, icon in NUMBER_DEFINITIONS
    ]

    async_add_entities(entities)


class SmartHeatpumpNumber(RestoreNumber):
    """A configurable parameter for the Smart Heatpump Controller."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator,
        key: str,
        name: str,
        min_val: float,
        max_val: float,
        step: float,
        unit: str,
        icon: str,
    ) -> None:
        self._coordinator = coordinator
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_native_min_value = min_val
        self._attr_native_max_value = max_val
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_native_value = DEFAULTS[key]

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to group all entities under one device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.entry.entry_id)},
            name="Smart Heatpump Controller",
            manufacturer="Smart Heatpump",
            model="v2",
        )

    async def async_added_to_hass(self) -> None:
        """Restore previous value on startup."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value is not None:
            self._attr_native_value = last.native_value

        # Push restored (or default) value to coordinator
        self._coordinator.set_config_value(self._key, self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update the value (called from the dashboard slider)."""
        self._attr_native_value = value
        self._coordinator.set_config_value(self._key, value)
        self.async_write_ha_state()
