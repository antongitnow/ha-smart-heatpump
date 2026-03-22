"""Coordinator for the Smart Heatpump Controller.

Runs the evaluation loop on a configurable interval, reads sensors,
calls the pure decide() function, and applies the result.
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
from .decision import decide
from .thermal_model import predict_hours_until_below
from .thermal_store import ThermalStore

_LOGGER = logging.getLogger(__name__)


class SmartHeatpumpCoordinator:
    """Manages the evaluation loop and state for the Smart Heatpump Controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        # Mutable config values — number entities push updates here
        self.config_values: dict[str, float] = dict(DEFAULTS)

        # Switch entity pushes updates here
        self.notifications_enabled: bool = True

        # Thermal learning
        self.thermal_store = ThermalStore(hass, entry.entry_id)

        # Evaluation state
        self.active_rule: str = "initialising"
        self.last_target: float | None = None  # Track computed setpoint (for dry run)
        self._solar_surplus_since: datetime | None = None
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._listeners: list[callback] = []

        # Dry run — switch entity pushes updates here
        self.dry_run_enabled: bool = not self._opt(CONF_THERMOSTAT)

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
    # Core evaluation
    # ------------------------------------------------------------------

    async def async_evaluate(self) -> None:
        """Read sensors, call decide(), apply result."""
        cfg = self.config_values
        effective_horizon = cfg["forecast_horizon_hours"] + cfg["thermal_lag_hours"]

        # Read sensors
        outdoor_temp = self._read_outdoor_temp()
        indoor_temp = self._read_indoor_temp()
        solar_surplus = self._read_solar_surplus()
        forecast_temps = await self._async_read_forecast_temps(effective_horizon)
        forecast_recovery = await self._async_read_forecast_temps(
            cfg["cop_recovery_horizon_hours"]
        )
        forecast_solar = self._read_forecast_solar()

        # Record thermal observation for learning
        now_utc = datetime.now(timezone.utc)
        if indoor_temp is not None and outdoor_temp is not None:
            heating_active = self._is_heating_active()
            solar_gain = self._is_solar_gain_likely(
                solar_surplus=solar_surplus,
                forecast_solar=forecast_solar,
            )
            self.thermal_store.add_observation(
                timestamp=now_utc.timestamp(),
                indoor_temp_c=indoor_temp,
                outdoor_temp_c=outdoor_temp,
                heating_active=heating_active,
                solar_gain_likely=solar_gain,
            )
            # Save periodically (every evaluation cycle)
            await self.thermal_store.async_save()

        # Thermal model prediction
        hours_prediction: float | None = None
        if (
            self.thermal_store.loss_coefficient is not None
            and indoor_temp is not None
            and forecast_temps
        ):
            hours_prediction = predict_hours_until_below(
                indoor_temp_c=indoor_temp,
                outdoor_temps=forecast_temps,
                threshold_temp_c=cfg["temp_minimum"],
                loss_coefficient_k=self.thermal_store.loss_coefficient,
            )

        # Solar confirmation tracking
        if (
            solar_surplus is not None
            and solar_surplus >= cfg["solar_surplus_threshold"]
        ):
            if self._solar_surplus_since is None:
                self._solar_surplus_since = now_utc
            elapsed = (now_utc - self._solar_surplus_since).total_seconds() / 60.0
            solar_confirmed = elapsed >= cfg["solar_confirm_minutes"]
        else:
            self._solar_surplus_since = None
            solar_confirmed = False

        # Call pure decision function
        target, rule = decide(
            outdoor_temp_c=outdoor_temp,
            indoor_temp_c=indoor_temp,
            solar_surplus_w=solar_surplus,
            solar_confirmed=solar_confirmed,
            forecast_solar_w=forecast_solar,
            forecast_temps=forecast_temps,
            forecast_recovery_temps=forecast_recovery,
            temp_ideal=cfg["temp_ideal"],
            temp_minimum=cfg["temp_minimum"],
            temp_solar_boost=cfg["temp_solar_boost"],
            preheat_delta=cfg["preheat_delta"],
            cop_threshold_temp=cfg["cop_threshold_temp"],
            solar_surplus_threshold=cfg["solar_surplus_threshold"],
            hours_until_below_min=hours_prediction,
            indoor_comfort_margin=cfg["indoor_comfort_margin"],
        )

        # Apply setpoint if changed
        if self.dry_run:
            # Dry run — compare against last computed target instead of thermostat
            previous = self.last_target
            setpoint_changed = previous is None or abs(target - previous) >= 0.1

            if setpoint_changed:
                _LOGGER.info(
                    "DRY RUN — would set: %s -> %.1f°C | rule=%s | outdoor=%s°C | surplus=%sW",
                    previous,
                    target,
                    rule,
                    outdoor_temp,
                    solar_surplus,
                )
                await self._async_send_notification(
                    old_setpoint=previous,
                    new_setpoint=target,
                    rule=rule,
                    outdoor_temp=outdoor_temp,
                    solar_surplus=solar_surplus,
                )
            else:
                _LOGGER.debug(
                    "DRY RUN — no change: setpoint=%.1f°C | rule=%s",
                    target,
                    rule,
                )
        else:
            current_setpoint = self._read_current_setpoint()
            setpoint_changed = (
                current_setpoint is None or abs(target - current_setpoint) >= 0.1
            )

            if setpoint_changed:
                await self._async_set_thermostat(target)
                _LOGGER.info(
                    "Setpoint change: %s -> %.1f°C | rule=%s | outdoor=%s°C | surplus=%sW",
                    current_setpoint,
                    target,
                    rule,
                    outdoor_temp,
                    solar_surplus,
                )
                await self._async_send_notification(
                    old_setpoint=current_setpoint,
                    new_setpoint=target,
                    rule=rule,
                    outdoor_temp=outdoor_temp,
                    solar_surplus=solar_surplus,
                )
            else:
                _LOGGER.debug(
                    "No change: setpoint=%.1f°C | rule=%s | outdoor=%s°C",
                    target,
                    rule,
                    outdoor_temp,
                )

        self.last_target = target
        self.active_rule = rule
        self._notify_listeners()

    # ------------------------------------------------------------------
    # Sensor readers
    # ------------------------------------------------------------------

    def _is_heating_active(self) -> bool:
        """Check if the thermostat is currently calling for heat.

        Reads the hvac_action attribute from the climate entity.
        Returns False if no thermostat configured or attribute unavailable.
        """
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
        """Detect whether passive solar gain may be warming the house.

        Solar gain inflates apparent insulation when the sun shines through
        windows, but the heat dissipates quickly after sunset. We exclude
        these periods from thermal learning to avoid overestimating insulation.

        Returns True when any of these conditions hold:
        - Solar panels are currently exporting (surplus > 0W)
        - Forecast.Solar predicts significant production this hour
        - It's daytime (sun is up) — conservative fallback when no solar
          data is available
        """
        # Direct evidence: panels are producing / exporting
        if solar_surplus is not None and solar_surplus > 0:
            return True

        # Predicted production from Forecast.Solar
        if forecast_solar is not None and forecast_solar > 50:
            return True

        # Fallback: use sun elevation from HA (if available)
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is not None:
            elevation = sun_state.attributes.get("elevation", -90)
            try:
                if float(elevation) > 5:  # Sun more than 5° above horizon
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

    def _read_solar_surplus(self) -> float | None:
        """Read net grid power and derive solar export.

        Positive P1 = importing. Negative P1 = exporting surplus.
        """
        entity_id = self._opt(CONF_P1_POWER)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.warning("P1 sensor '%s' unavailable", entity_id)
            return None
        try:
            return max(0.0, -float(state.state))
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
        """Read indoor temperature from the optional temp sensor.

        Falls back to the thermostat's current_temperature attribute
        when no standalone sensor is configured.
        """
        # Try standalone temperature sensor first
        temp_entity = self._opt(CONF_TEMP_SENSOR)
        if temp_entity:
            state = self.hass.states.get(temp_entity)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    return float(state.state)
                except (TypeError, ValueError):
                    pass
            _LOGGER.warning("Temperature sensor '%s' unavailable", temp_entity)

        # Fall back to thermostat's current_temperature
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
        solar_surplus: float | None,
    ) -> None:
        """Send notification on setpoint change."""
        if not self.notifications_enabled:
            return

        targets = self.notify_targets
        if not targets:
            return

        description = RULE_DESCRIPTIONS.get(rule, rule)
        outdoor_str = f"{outdoor_temp:.1f}°C" if outdoor_temp is not None else "N/A"
        surplus_str = f"{solar_surplus:.0f}W" if solar_surplus is not None else "N/A"
        old_str = f"{old_setpoint:.1f}°C" if old_setpoint is not None else "N/A"

        title = "Smart Heatpump"
        message = (
            f"{description}\n\n"
            f"Setpoint: {old_str} → {new_setpoint:.1f}°C\n"
            f"Outdoor: {outdoor_str}\n"
            f"Solar export: {surplus_str}\n"
            f"Rule: {rule}"
        )

        for target_name in targets:
            try:
                await self.hass.services.async_call(
                    "notify",
                    target_name,
                    {"title": title, "message": message},
                    blocking=True,
                )
            except Exception:
                _LOGGER.warning("Failed to send notification to '%s'", target_name)

    async def _async_safe_fallback(self) -> None:
        """Safe fallback on unhandled exception."""
        if not self.dry_run:
            try:
                await self._async_set_thermostat(DEFAULTS["temp_ideal"])
            except Exception:
                pass
        self.active_rule = "error_fallback"
        self._notify_listeners()
