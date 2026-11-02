"""Sensor entities for Smart Heatpump Controller."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_P1_POWER,
    CONF_TEMP_SENSOR,
    CONF_THERMOSTAT,
    DOMAIN,
    RULE_DESCRIPTIONS,
)
from .thermal_model import MIN_SAMPLES, count_valid_cooling_samples


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        SmartHeatpumpRuleSensor(coordinator),
        ThermalLearningSensor(coordinator),
    ]
    if coordinator.heatpump_power_entity:
        entities += [
            SolarEnergyTotalSensor(coordinator),
            SolarEnergyTodaySensor(coordinator),
        ]
    async_add_entities(entities)


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
        c = self._coordinator
        rule = c.active_rule
        attrs: dict[str, str | float | None] = {
            "description": RULE_DESCRIPTIONS.get(rule, rule),
            "solar_boost_active": c._solar_boost_active,
            "computed_setpoint": c.last_target,
        }
        if c.dry_run:
            attrs["mode"] = "dry_run"
        if c._last_net_power is not None:
            if c._last_net_power < 0:
                attrs["current_power"] = f"Export {abs(c._last_net_power):.0f}W"
            else:
                attrs["current_power"] = f"Import {c._last_net_power:.0f}W"
        attrs["avg_import_5min_w"] = round(c._last_avg_import_5min, 0)
        if c._last_indoor_temp is not None:
            attrs["indoor_temp"] = c._last_indoor_temp
        if c._last_outdoor_temp is not None:
            attrs["outdoor_temp"] = c._last_outdoor_temp
        if c._last_avg_forecast_temp is not None:
            attrs["forecast_avg_24h"] = c._last_avg_forecast_temp
        attrs["notify_targets"] = c.notify_targets
        attrs["notifications_enabled"] = c.notifications_enabled
        # Source entities, so the Lovelace card can replay their history
        attrs["power_entity"] = c._opt(CONF_P1_POWER) or None
        attrs["indoor_temp_entity"] = (
            c._opt(CONF_TEMP_SENSOR) or c._opt(CONF_THERMOSTAT) or None
        )
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
            attrs["approx_drop_per_hour_at_10c_delta"] = round(k * 10.0, 2)
            if k <= 0.03:
                quality = "excellent"
            elif k <= 0.05:
                quality = "well insulated"
            elif k <= 0.10:
                quality = "average"
            else:
                quality = "poorly insulated"
            attrs["insulation_quality"] = quality

        h = self._coordinator.hours_until_below_ideal
        if h is not None:
            if h == float("inf"):
                attrs["hours_until_below_ideal"] = "not expected"
            else:
                attrs["hours_until_below_ideal"] = round(h, 1)

        return attrs


class SolarEnergyTotalSensor(_BaseSensor):
    """Lifetime kWh the heat pump used on solar surplus during a boost.

    A total_increasing energy sensor, so Home Assistant's long-term statistics
    (and the Energy dashboard / statistics-graph card) give per-day, per-week
    and per-month bars for free.
    """

    _attr_translation_key = "solar_energy_total"
    _attr_icon = "mdi:solar-power-variant"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_solar_energy_total"

    @property
    def native_value(self) -> float:
        return round(self._coordinator.solar_energy.total_solar_kwh, 3)


class SolarEnergyTodaySensor(_BaseSensor):
    """Solar-powered heat pump kWh today, with share and recent history."""

    _attr_translation_key = "solar_energy_today"
    _attr_icon = "mdi:weather-sunny"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_solar_energy_today"

    @property
    def last_reset(self) -> datetime:
        return dt_util.start_of_local_day()

    @property
    def native_value(self) -> float:
        today = dt_util.now().date().isoformat()
        return round(self._coordinator.solar_energy.solar_kwh(today), 3)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        tracker = self._coordinator.solar_energy
        today = dt_util.now().date()
        key = today.isoformat()
        yesterday = today - timedelta(days=1)

        attrs: dict[str, object] = {
            "heatpump_kwh_today": round(tracker.total_kwh(key), 3),
            "solar_share_today_pct": (
                round(share, 1) if (share := tracker.solar_share_pct(key)) is not None else None
            ),
            "solar_kwh_yesterday": round(tracker.solar_kwh(yesterday.isoformat()), 3),
            "solar_kwh_last_7_days": round(
                tracker.solar_kwh_since(today - timedelta(days=6), today), 3
            ),
            "solar_kwh_this_month": round(
                tracker.solar_kwh_since(today.replace(day=1), today), 3
            ),
            "heatpump_kwh_this_month": round(
                tracker.total_kwh_since(today.replace(day=1), today), 3
            ),
            "solar_kwh_per_day": {
                (today - timedelta(days=i)).isoformat(): round(
                    tracker.solar_kwh((today - timedelta(days=i)).isoformat()), 3
                )
                for i in range(13, -1, -1)
            },
        }
        if self._coordinator.dry_run:
            attrs["note"] = "Dry run: no solar energy is counted while the thermostat is not controlled"
        return attrs
