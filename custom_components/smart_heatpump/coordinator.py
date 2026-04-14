"""Coordinator for the Smart Heatpump Controller.

Runs the evaluation loop on a configurable interval, reads sensors,
calls the solar incremental decision logic, and applies the result.
Thermal model ML learning continues independently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FORECAST_SOLAR,
    CONF_NOTIFY_TARGETS,
    CONF_P1_POWER,
    CONF_TEMP_SENSOR,
    CONF_THERMOSTAT,
    CONF_WEATHER,
    DEFAULTS,
    DOMAIN,
    RULE_DESCRIPTIONS,
)
from .decision import decide_solar
from .notifications import format_notification
from .thermal_model import predict_hours_until_below
from .thermal_store import ThermalStore

_LOGGER = logging.getLogger(__name__)

# How many seconds of import history to keep for the 5-minute rolling average
_IMPORT_HISTORY_SECONDS = 300  # 5 minutes


class SmartHeatpumpCoordinator:
    """Manages the evaluation loop and state for the Smart Heatpump Controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        # Mutable config values — number entities push updates here
        self.config_values: dict[str, float] = dict(DEFAULTS)

        # Switch entity pushes updates here
        self.notifications_enabled: bool = True

        # Thermal learning (continues independently — not used for thermostat control)
        self.thermal_store = ThermalStore(hass, entry.entry_id)

        # Evaluation state
        self.active_rule: str = "initialising"
        self.last_target: float | None = None  # Track computed setpoint
        self.hours_until_below_ideal: float | None = None
        self._solar_boost_active: bool = False
        self._boost_activated_at: float | None = None  # monotonic timestamp
        self._import_history: list[tuple[float, float]] = []  # [(timestamp, import_w)]
        self._export_history: list[tuple[float, float]] = []  # [(timestamp, export_w)]
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._listeners: list[callback] = []

        # Dry run — switch entity pushes updates here
        self.dry_run_enabled: bool = not self._opt(CONF_THERMOSTAT)
        self.virtual_thermostat_entity = None  # Set by number.py on setup

        # Snapshot values for notifications / sensor attributes
        self._last_avg_import_5min: float = 0.0
        self._last_net_power: float | None = None
        self._last_outdoor_temp: float | None = None
        self._last_indoor_temp: float | None = None

    def _opt(self, key: str, default: str = "") -> str:
        """Read a config value from options (primary) or data (migration fallback)."""
        return self.entry.options.get(key, self.entry.data.get(key, default))

    @property
    def dry_run(self) -> bool:
        """True when dry run is enabled or no thermostat configured."""
        return self.dry_run_enabled or not self._opt(CONF_THERMOSTAT)

    # ------------------------------------------------------------------
    # Listener management — entities register to get notified on changes
    # ------------------------------------------------------------------

    @callback
    def async_add_listener(self, update_callback: callback) -> callback:
        """Register a listener. Returns a callable to unregister."""
        self._listeners.append(update_callback)

        @callback
        def remove_listener() -> None:
            self._listeners.remove(update_callback)

        return remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Notify all registered listeners that state has changed."""
        for cb in self._listeners:
            cb()

    # ------------------------------------------------------------------
    # Config value management — number entities call this
    # ------------------------------------------------------------------

    def set_config_value(self, key: str, value: float) -> None:
        """Update a config value (called by number entities)."""
        self.config_values[key] = value

    # ------------------------------------------------------------------
    # Options helpers
    # ------------------------------------------------------------------

    @property
    def forecast_solar_entity(self) -> str | None:
        """Get the forecast solar entity from options."""
        entity = self._opt(CONF_FORECAST_SOLAR)
        return entity if entity else None

    @property
    def notify_targets(self) -> list[str]:
        """Get notification targets from options."""
        raw = self._opt(CONF_NOTIFY_TARGETS)
        return [t.strip() for t in raw.split(",") if t.strip()]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start the evaluation loop (called after entities are set up)."""
        # First evaluation after a short delay to let entities restore
        self._cancel_timer = async_call_later(
            self.hass, 10, self._async_evaluate_callback
        )

    @callback
    def async_stop(self) -> None:
        """Stop the evaluation loop."""
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None

    async def _async_evaluate_callback(self, _now: Any = None) -> None:
        """Timer callback — wraps evaluate with error recovery."""
        try:
            await self.async_evaluate()
        except Exception:
            _LOGGER.exception("Unhandled exception in evaluation")
            await self._async_safe_fallback()
        finally:
            self._schedule_next()

    @callback
    def _schedule_next(self) -> None:
        """Schedule the next evaluation based on current interval setting."""
        interval_min = self.config_values["evaluation_interval_min"]
        self._cancel_timer = async_call_later(
            self.hass, int(interval_min * 60), self._async_evaluate_callback
        )

    # ------------------------------------------------------------------
    # Core evaluation — Solar Incremental Flow
    # ------------------------------------------------------------------

    async def async_evaluate(self) -> None:
        """Read sensors, run solar incremental decision, apply result."""
        cfg = self.config_values

        # Read sensors
        outdoor_temp = self._read_outdoor_temp()
        indoor_temp = self._read_indoor_temp()
        net_power = self._read_net_power()
        solar_export = max(0.0, -net_power) if net_power is not None else None
        grid_import = max(0.0, net_power) if net_power is not None else None
        forecast_solar = self._read_forecast_solar()

        # Store snapshots for notification / sensor attributes
        self._last_net_power = net_power
        self._last_outdoor_temp = outdoor_temp
        self._last_indoor_temp = indoor_temp

        # ---- Thermal model learning (continues independently) ----
        now_utc = datetime.now(timezone.utc)
        if indoor_temp is not None and outdoor_temp is not None:
            heating_active = self._is_heating_active()
            solar_gain = self._is_solar_gain_likely(
                solar_surplus=solar_export,
                forecast_solar=forecast_solar,
            )
            self.thermal_store.add_observation(
                timestamp=now_utc.timestamp(),
                indoor_temp_c=indoor_temp,
                outdoor_temp_c=outdoor_temp,
                heating_active=heating_active,
                solar_gain_likely=solar_gain,
            )
            await self.thermal_store.async_save()

        # Thermal model prediction (informational — not used for control)
        forecast_temps = await self._async_read_forecast_temps(24)
        if (
            self.thermal_store.loss_coefficient is not None
            and indoor_temp is not None
            and forecast_temps
        ):
            self.hours_until_below_ideal = predict_hours_until_below(
                indoor_temp_c=indoor_temp,
                outdoor_temps=forecast_temps,
                threshold_temp_c=cfg["temp_ideal"],
                loss_coefficient_k=self.thermal_store.loss_coefficient,
            )
        else:
            self.hours_until_below_ideal = None

        # ---- 5-minute rolling averages (import and export) ----
        now_ts = now_utc.timestamp()
        cutoff_ts = now_ts - _IMPORT_HISTORY_SECONDS

        if grid_import is not None:
            self._import_history.append((now_ts, grid_import))
        self._import_history = [
            (ts, w) for ts, w in self._import_history if ts >= cutoff_ts
        ]
        if self._import_history:
            avg_import_5min = sum(w for _, w in self._import_history) / len(
                self._import_history
            )
        else:
            avg_import_5min = 0.0
        self._last_avg_import_5min = avg_import_5min

        if solar_export is not None:
            self._export_history.append((now_ts, solar_export))
        self._export_history = [
            (ts, w) for ts, w in self._export_history if ts >= cutoff_ts
        ]
        if self._export_history:
            avg_export_5min = sum(w for _, w in self._export_history) / len(
                self._export_history
            )
        else:
            avg_export_5min = 0.0

        # ---- Read current setpoint ----
        # Use our last commanded target (self.last_target) when boost is active,
        # because the thermostat may not have updated its reported setpoint yet
        # from the previous set_temperature call (poll lag).  Fall back to the
        # thermostat-reported value when we haven't commanded anything yet.
        thermostat_setpoint = self._read_current_setpoint()
        if self.dry_run:
            current_setpoint = self.last_target
        elif self._solar_boost_active and self.last_target is not None:
            current_setpoint = self.last_target
        else:
            current_setpoint = thermostat_setpoint

        # ---- Solar incremental decision ----
        now_local = dt_util.now()
        current_month = now_local.month

        import time as _time
        boost_active_seconds = 0.0
        if self._solar_boost_active and self._boost_activated_at is not None:
            boost_active_seconds = _time.monotonic() - self._boost_activated_at

        target, rule, new_boost_active = decide_solar(
            current_month=current_month,
            solar_boost_active=self._solar_boost_active,
            avg_export_5min_w=avg_export_5min,
            avg_import_5min_w=avg_import_5min,
            current_temperature=indoor_temp,
            current_setpoint=current_setpoint,
            temp_ideal=cfg["temp_ideal"],
            solar_surplus_threshold=cfg["solar_surplus_threshold"],
            solar_release_threshold_high=cfg["solar_release_threshold_high"],
            solar_release_threshold_low=cfg["solar_release_threshold_low"],
            solar_step_delta=cfg["solar_step_delta"],
            season_start_month=int(cfg["solar_season_start_month"]),
            season_end_month=int(cfg["solar_season_end_month"]),
            boost_active_seconds=boost_active_seconds,
            min_boost_minutes=cfg.get("solar_min_boost_minutes", 0.0),
        )

        prev_boost_active = self._solar_boost_active
        self._solar_boost_active = new_boost_active

        # Track when boost was activated
        if new_boost_active and not prev_boost_active:
            self._boost_activated_at = _time.monotonic()
        elif not new_boost_active:
            self._boost_activated_at = None

        # Determine if we should apply a change
        # Only act when the rule actually changes the thermostat
        actionable_rules = {
            "solar_incremental",
            "solar_step_down",
            "solar_reset",
            "solar_boost_deactivated",
            "solar_min_run",
        }
        is_actionable = rule in actionable_rules

        if self.dry_run:
            previous = self.last_target
            setpoint_changed = is_actionable and (
                previous is None or abs(target - previous) >= 0.1
            )
            _LOGGER.warning(
                "DRY RUN eval: rule=%s | target=%.1f | previous=%s | actionable=%s | changed=%s | export=%s | avg_import=%.0f",
                rule, target, previous, is_actionable, setpoint_changed, solar_export, avg_import_5min,
            )
            if setpoint_changed:
                # Update the virtual thermostat on the dashboard
                if self.virtual_thermostat_entity is not None:
                    self.virtual_thermostat_entity.set_value_from_coordinator(target)
                _LOGGER.warning("DRY RUN — calling _async_send_notification for rule=%s", rule)
                await self._async_send_notification(
                    old_setpoint=previous,
                    new_setpoint=target,
                    rule=rule,
                    outdoor_temp=outdoor_temp,
                    indoor_temp=indoor_temp,
                    net_power=net_power,
                    avg_import_5min=avg_import_5min,
                )
        else:
            setpoint_changed = is_actionable and (
                current_setpoint is None or abs(target - current_setpoint) >= 0.1
            )
            if setpoint_changed:
                await self._async_set_thermostat(target)
                _LOGGER.info(
                    "Setpoint change: %s -> %.1f°C | rule=%s | export=%sW | avg_import_5min=%.0fW",
                    current_setpoint,
                    target,
                    rule,
                    solar_export,
                    avg_import_5min,
                )
                await self._async_send_notification(
                    old_setpoint=current_setpoint,
                    new_setpoint=target,
                    rule=rule,
                    outdoor_temp=outdoor_temp,
                    indoor_temp=indoor_temp,
                    net_power=net_power,
                    avg_import_5min=avg_import_5min,
                )
            else:
                _LOGGER.debug(
                    "No change: setpoint=%.1f°C | rule=%s | export=%sW",
                    target,
                    rule,
                    solar_export,
                )

        self.last_target = target
        self.active_rule = rule
        self._notify_listeners()

    # ------------------------------------------------------------------
    # Sensor readers
    # ------------------------------------------------------------------

    def _is_heating_active(self) -> bool:
        """Check if the thermostat is currently calling for heat."""
        entity_id = self._opt(CONF_THERMOSTAT)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        action = state.attributes.get("hvac_action", "")
        return action == "heating"

    def _is_solar_gain_likely(
        self,
        solar_surplus: float | None,
        forecast_solar: float | None,
    ) -> bool:
        """Detect whether passive solar gain may be warming the house."""
        if solar_surplus is not None and solar_surplus > 0:
            return True
        if forecast_solar is not None and forecast_solar > 50:
            return True
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is not None:
            elevation = sun_state.attributes.get("elevation", -90)
            try:
                if float(elevation) > 5:
                    return True
            except (TypeError, ValueError):
                pass
        return False

    def _read_outdoor_temp(self) -> float | None:
        """Read outdoor temperature from the weather entity."""
        entity_id = self._opt(CONF_WEATHER)
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.warning("Weather entity '%s' unavailable", entity_id)
            return None
        temp = state.attributes.get("temperature")
        if temp is None:
            return None
        try:
            return float(temp)
        except (TypeError, ValueError):
            return None

    def _read_net_power(self) -> float | None:
        """Read net grid power in watts (positive=import, negative=export)."""
        entity_id = self._opt(CONF_P1_POWER)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.warning("P1 sensor '%s' unavailable", entity_id)
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def _async_read_forecast_temps(self, horizon_hours: float) -> list[float]:
        """Read hourly forecast temperatures using weather.get_forecasts service."""
        entity_id = self._opt(CONF_WEATHER)
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except Exception:
            _LOGGER.warning("Failed to call weather.get_forecasts for '%s'", entity_id)
            return []

        if not response:
            return []

        forecasts = response.get(entity_id, {}).get("forecast", [])
        if not forecasts:
            _LOGGER.warning("No forecast data from '%s'", entity_id)
            return []

        now = dt_util.utcnow()
        cutoff = now + timedelta(hours=horizon_hours)
        temps: list[float] = []

        for entry in forecasts:
            try:
                dt_str = entry.get("datetime", "")
                entry_dt = dt_util.parse_datetime(dt_str)
                if entry_dt and entry_dt <= cutoff:
                    temps.append(float(entry["temperature"]))
            except (TypeError, ValueError, KeyError):
                continue

        return temps

    def _read_forecast_solar(self) -> float | None:
        """Read predicted solar yield from Forecast.Solar entity."""
        entity_id = self.forecast_solar_entity
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_current_setpoint(self) -> float | None:
        """Read current setpoint from the thermostat."""
        entity_id = self._opt(CONF_THERMOSTAT)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        temp = state.attributes.get("temperature")
        if temp is None:
            return None
        try:
            return float(temp)
        except (TypeError, ValueError):
            return None

    def _read_indoor_temp(self) -> float | None:
        """Read indoor temperature from optional sensor or thermostat fallback."""
        temp_entity = self._opt(CONF_TEMP_SENSOR)
        if temp_entity:
            state = self.hass.states.get(temp_entity)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    return float(state.state)
                except (TypeError, ValueError):
                    pass
            _LOGGER.warning("Temperature sensor '%s' unavailable", temp_entity)

        therm_entity = self._opt(CONF_THERMOSTAT)
        if therm_entity:
            state = self.hass.states.get(therm_entity)
            if state:
                temp = state.attributes.get("current_temperature")
                if temp is not None:
                    try:
                        return float(temp)
                    except (TypeError, ValueError):
                        pass

        return None

    # ------------------------------------------------------------------
    # Actuators
    # ------------------------------------------------------------------

    async def _async_set_thermostat(self, target: float) -> None:
        """Set the thermostat to the target temperature."""
        entity_id = self._opt(CONF_THERMOSTAT)
        if not entity_id:
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": target},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("Failed to set thermostat '%s' to %.1f°C", entity_id, target)

    async def _async_send_notification(
        self,
        old_setpoint: float | None,
        new_setpoint: float,
        rule: str,
        outdoor_temp: float | None,
        indoor_temp: float | None,
        net_power: float | None,
        avg_import_5min: float,
    ) -> None:
        """Send enriched notification on setpoint change."""
        _LOGGER.warning(
            "NOTIFY entry: rule=%s | enabled=%s | targets=%s",
            rule, self.notifications_enabled, self.notify_targets,
        )
        if not self.notifications_enabled:
            _LOGGER.warning("Notification skipped — notifications disabled")
            return

        targets = self.notify_targets
        if not targets:
            _LOGGER.warning("Notification skipped — no targets configured")
            return

        cfg = self.config_values
        description = RULE_DESCRIPTIONS.get(rule, rule)
        title, message = format_notification(
            rule=rule,
            description=description,
            old_setpoint=old_setpoint,
            new_setpoint=new_setpoint,
            outdoor_temp=outdoor_temp,
            indoor_temp=indoor_temp,
            net_power=net_power,
            avg_import_5min=avg_import_5min,
            dry_run=self.dry_run,
            config=cfg,
        )

        _LOGGER.warning("Sending notification to targets: %s | message_len=%d", targets, len(message))
        for target_name in targets:
            try:
                await self.hass.services.async_call(
                    "notify",
                    target_name,
                    {"title": title, "message": message},
                    blocking=True,
                )
                _LOGGER.warning("Notification SENT OK to '%s' for rule=%s", target_name, rule)
            except Exception:
                _LOGGER.exception("Notification FAILED to '%s' for rule=%s", target_name, rule)

    async def _async_safe_fallback(self) -> None:
        """Safe fallback on unhandled exception."""
        if not self.dry_run:
            try:
                await self._async_set_thermostat(DEFAULTS["temp_ideal"])
            except Exception:
                pass
        self.active_rule = "error_fallback"
        self._notify_listeners()
