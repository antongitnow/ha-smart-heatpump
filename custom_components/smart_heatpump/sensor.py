"""Sensor entities for Smart Heatpump Controller."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, RULE_DESCRIPTIONS
from .thermal_model import MIN_SAMPLES, count_valid_cooling_samples


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        SmartHeatpumpRuleSensor(coordinator),
        ThermalLearningSensor(coordinator),
    ])


class _BaseSensor(SensorEntity):
    """Base class with shared device info."""

    _attr_has_entity_name = True

    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.entry.entry_id)},
            name="Smart Heatpump Controller",
            manufacturer="Smart Heatpump",
            model="v2",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class SmartHeatpumpRuleSensor(_BaseSensor):
    """Shows the currently active decision rule."""

    _attr_translation_key = "active_rule"
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_active_rule"

    @property
    def native_value(self) -> str:
        return self._coordinator.active_rule

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        rule = self._coordinator.active_rule
        attrs: dict[str, str | float | None] = {
            "description": RULE_DESCRIPTIONS.get(rule, rule),
        }
        if self._coordinator.dry_run:
            attrs["mode"] = "dry_run"
            attrs["computed_setpoint"] = self._coordinator.last_target
        return attrs


class ThermalLearningSensor(_BaseSensor):
    """Shows thermal model learning status."""

    _attr_translation_key = "thermal_learning"
    _attr_icon = "mdi:school-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_thermal_learning"

    @property
    def native_value(self) -> str:
        if self._coordinator.thermal_store.is_ready:
            return "ready"
        return "learning"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        store = self._coordinator.thermal_store
        k = store.loss_coefficient
        valid = count_valid_cooling_samples(store.observations)

        attrs: dict[str, object] = {
            "total_observations": store.sample_count,
            "valid_cooling_samples": valid,
            "min_samples_needed": MIN_SAMPLES,
        }

        if k is not None:
            attrs["loss_coefficient_k"] = round(k, 5)
            # Human-readable: approximate °C drop per hour at 10°C delta
            attrs["approx_drop_per_hour_at_10c_delta"] = round(k * 10.0, 2)
            # Insulation quality label
            if k <= 0.03:
                quality = "excellent"
            elif k <= 0.05:
                quality = "well insulated"
            elif k <= 0.10:
                quality = "average"
            else:
                quality = "poorly insulated"
            attrs["insulation_quality"] = quality

        # Prediction — hours until indoor drops below ideal
        h = self._coordinator.hours_until_below_ideal
        if h is not None:
            if h == float("inf"):
                attrs["hours_until_below_ideal"] = "not expected"
            else:
                attrs["hours_until_below_ideal"] = round(h, 1)

        return attrs
