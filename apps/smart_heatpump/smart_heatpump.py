"""Smart Heatpump Controller for Home Assistant AppDaemon.

Optimises heat pump operation based on COP, solar surplus, and weather forecast.
All control happens via the thermostat setpoint — no direct heat pump API required.

Decision priority (highest wins):
  1. Solar surplus confirmed (FR-02) OR solar predicted (FR-03)
  2. COP pre-heat: cold coming within horizon, COP still good now (FR-04)
  3. COP conservation: COP poor now (FR-05)
  4. Default: maintain ideal temperature (FR-06)

Safety floor (FR-08): setpoint is always >= temp_minimum regardless of rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import appdaemon.plugins.hass.hassapi as hass

# ---------------------------------------------------------------------------
# Hardcoded defaults — used when an input_number entity is unavailable (FR-12)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Human-readable rule descriptions for notifications
# ---------------------------------------------------------------------------
_RULE_DESCRIPTIONS: dict[str, str] = {
    "solar_boost": "☀️ Solar surplus detected — storing free energy as heat",
    "solar_predicted": "🌤️ Solar surplus predicted — pre-heating before surplus starts",
    "preheat": "🔥 Cold period coming — pre-heating while COP is still efficient",
    "conserve": "❄️ COP poor, no recovery expected — holding minimum temperature",
    "conserve_await_recovery": "⏳ COP poor but recovery coming — waiting for efficient window",
    "default": "✅ Normal operation — maintaining ideal temperature",
    "error_fallback": "⚠️ Error occurred — using safe fallback temperature",
    "initialising": "🔄 Controller starting up",
}

_DEFAULTS: dict[str, float] = {
    "temp_ideal": 21.0,
    "temp_minimum": 20.5,
    "temp_solar_boost": 22.5,
    "preheat_delta": 0.5,
    "cop_threshold_temp": 5.0,
    "cop_recovery_horizon_hours": 6.0,
    "solar_surplus_threshold": 500.0,
    "solar_confirm_minutes": 10.0,
    "forecast_horizon_hours": 24.0,
    "thermal_lag_hours": 3.0,
    "evaluation_interval_min": 15.0,
}


# ---------------------------------------------------------------------------
# Pure decision function — no AppDaemon or HA dependency (enables unit testing)
# ---------------------------------------------------------------------------

def decide(
    outdoor_temp_c: float | None,
    solar_surplus_w: float | None,
    solar_confirmed: bool,
    forecast_solar_w: float | None,
    forecast_temps: list[float],           # preheat horizon: forecast_horizon + thermal_lag
    forecast_recovery_temps: list[float],  # COP recovery horizon: cop_recovery_horizon_hours
    temp_ideal: float,
    temp_minimum: float,
    temp_solar_boost: float,
    preheat_delta: float,
    cop_threshold_temp: float,
    solar_surplus_threshold: float,
) -> tuple[float, str]:
    """Decide the target thermostat setpoint and active rule name.

    Args:
        outdoor_temp_c: Current outdoor temperature in °C, or None if unavailable.
        solar_surplus_w: Current solar export in Watts (>=0), or None if unavailable.
        solar_confirmed: True when export has been sustained >= solar_confirm_minutes.
        forecast_solar_w: Predicted solar yield for the next hour in Wh (≈ avg W),
            or None when Forecast.Solar is not configured or unavailable.
            Note: Wh over a 1-hour window is numerically equivalent to average W.
        forecast_temps: Outdoor temperature forecasts up to the effective preheat
            horizon (forecast_horizon_hours + thermal_lag_hours) ahead.
        forecast_recovery_temps: Outdoor temperature forecasts up to
            cop_recovery_horizon_hours ahead (used to detect COP recovery).
        temp_ideal: Default comfort setpoint (°C).
        temp_minimum: Hard floor — setpoint never goes below this (°C).
        temp_solar_boost: Setpoint during confirmed or predicted solar surplus (°C).
        preheat_delta: Extra °C added to temp_ideal during pre-heat mode.
        cop_threshold_temp: Outdoor temperature below which COP is considered poor (°C).
            At or below this value → poor COP. Strictly above → good COP.
        solar_surplus_threshold: Minimum export in W to be considered solar surplus.

    Returns:
        (target_temp, rule_name) where target_temp is already clamped to >= temp_minimum.
    """
    # ------------------------------------------------------------------
    # Priority 1: Solar surplus — confirmed (FR-02) or predicted (FR-03)
    # ------------------------------------------------------------------
    solar_predicted = (
        forecast_solar_w is not None
        and forecast_solar_w >= solar_surplus_threshold
    )

    if solar_confirmed or solar_predicted:
        rule = "solar_boost" if solar_confirmed else "solar_predicted"
        target = temp_solar_boost
        return max(target, temp_minimum), rule

    # ------------------------------------------------------------------
    # If outdoor temp is unknown, fall back to ideal (sensor failure path)
    # ------------------------------------------------------------------
    if outdoor_temp_c is None:
        return max(temp_ideal, temp_minimum), "default"

    # Derived aggregates from forecast lists
    min_forecast_temp: float | None = min(forecast_temps) if forecast_temps else None
    max_recovery_temp: float | None = (
        max(forecast_recovery_temps) if forecast_recovery_temps else None
    )

    # ------------------------------------------------------------------
    # Priority 2: COP pre-heat (FR-04)
    # Cold is coming within the effective horizon AND COP is still good right now.
    # "Good COP" means outdoor temp is strictly above the threshold.
    # ------------------------------------------------------------------
    if (
        min_forecast_temp is not None
        and min_forecast_temp < cop_threshold_temp
        and outdoor_temp_c > cop_threshold_temp
    ):
        target = temp_ideal + preheat_delta
        return max(target, temp_minimum), "preheat"

    # ------------------------------------------------------------------
    # Priority 3: COP conservation (FR-05)
    # outdoor_temp_c <= cop_threshold_temp means COP is currently poor.
    # Two sub-rules for observability: conserve vs conserve_await_recovery.
    # ------------------------------------------------------------------
    if outdoor_temp_c <= cop_threshold_temp:
        target = temp_minimum
        if max_recovery_temp is not None and max_recovery_temp >= cop_threshold_temp:
            rule = "conserve_await_recovery"  # COP will improve soon — wait for it
        else:
            rule = "conserve"  # COP poor, no recovery expected
        return max(target, temp_minimum), rule

    # ------------------------------------------------------------------
    # Priority 4: Default — maintain ideal temperature (FR-06)
    # ------------------------------------------------------------------
    return max(temp_ideal, temp_minimum), "default"


# ---------------------------------------------------------------------------
# AppDaemon application class
# ---------------------------------------------------------------------------

class SmartHeatpump(hass.Hass):
    """Controls a heat pump via a smart thermostat setpoint.

    Reads all configuration from input_number helpers (prefix: shp_) on every
    evaluation cycle so that live changes take effect without restarting AppDaemon.

    Writes the active decision rule name to input_text.shp_active_rule after every
    evaluation cycle for dashboard visibility.

    Entity names are read exclusively from self.args (smart_heatpump.yaml) —
    no entity names are hardcoded in this file (FR-11).
    """

    def initialize(self) -> None:
        """Called by AppDaemon on application startup."""
        # Stateful solar confirmation tracker (FR-02)
        self._solar_surplus_since: datetime | None = None
        self.log("SmartHeatpump initialising", level="INFO")
        # Schedule first evaluation immediately; subsequent runs self-reschedule
        self.run_in(self._run_evaluation, 0)

    # ------------------------------------------------------------------
    # Evaluation cycle
    # ------------------------------------------------------------------

    def _run_evaluation(self, kwargs: dict) -> None:  # noqa: ARG002
        """Called by AppDaemon timer. Wraps _evaluate() with error recovery."""
        try:
            self._evaluate()
        except Exception as exc:  # noqa: BLE001
            self.log(f"Unhandled exception in evaluation: {exc}", level="ERROR")
            self._safe_set_fallback()
        finally:
            # Re-read interval every cycle to support live changes (FR-09, OQ-6)
            interval_min = self._read_config_float("evaluation_interval_min")
            self.run_in(self._run_evaluation, interval_min * 60)

    def _evaluate(self) -> None:
        """Core evaluation — reads sensors, calls decide(), applies result."""
        # --- Read all configuration from input_number entities ---
        temp_ideal = self._read_config_float("temp_ideal")
        temp_minimum = self._read_config_float("temp_minimum")
        temp_solar_boost = self._read_config_float("temp_solar_boost")
        preheat_delta = self._read_config_float("preheat_delta")
        cop_threshold_temp = self._read_config_float("cop_threshold_temp")
        cop_recovery_horizon_hours = self._read_config_float("cop_recovery_horizon_hours")
        solar_surplus_threshold = self._read_config_float("solar_surplus_threshold")
        solar_confirm_minutes = self._read_config_float("solar_confirm_minutes")
        forecast_horizon_hours = self._read_config_float("forecast_horizon_hours")
        thermal_lag_hours = self._read_config_float("thermal_lag_hours")

        effective_horizon = forecast_horizon_hours + thermal_lag_hours

        # --- Read sensors ---
        outdoor_temp_c = self._read_outdoor_temp()
        solar_surplus_w = self._read_solar_surplus()
        forecast_temps = self._read_forecast_temps(effective_horizon)
        forecast_recovery_temps = self._read_forecast_temps(cop_recovery_horizon_hours)
        forecast_solar_w = self._read_forecast_solar()

        # --- Solar confirmation tracking (stateful, FR-02) ---
        now_utc = datetime.now(timezone.utc)
        if (
            solar_surplus_w is not None
            and solar_surplus_w >= solar_surplus_threshold
        ):
            if self._solar_surplus_since is None:
                self._solar_surplus_since = now_utc
            elapsed_min = (now_utc - self._solar_surplus_since).total_seconds() / 60.0
            solar_confirmed = elapsed_min >= solar_confirm_minutes
        else:
            self._solar_surplus_since = None
            solar_confirmed = False

        # --- Call pure decision function ---
        target, rule = decide(
            outdoor_temp_c=outdoor_temp_c,
            solar_surplus_w=solar_surplus_w,
            solar_confirmed=solar_confirmed,
            forecast_solar_w=forecast_solar_w,
            forecast_temps=forecast_temps,
            forecast_recovery_temps=forecast_recovery_temps,
            temp_ideal=temp_ideal,
            temp_minimum=temp_minimum,
            temp_solar_boost=temp_solar_boost,
            preheat_delta=preheat_delta,
            cop_threshold_temp=cop_threshold_temp,
            solar_surplus_threshold=solar_surplus_threshold,
        )

        # --- Compare with current setpoint ---
        current_setpoint = self._read_current_setpoint()
        min_forecast_display = (
            f"{min(forecast_temps):.1f}" if forecast_temps else "N/A"
        )

        if current_setpoint is None or abs(target - current_setpoint) >= 0.1:
            self._set_thermostat(target)
            self.log(
                f"Setpoint change: {current_setpoint} → {target}°C | rule={rule} | "
                f"outdoor={outdoor_temp_c}°C | surplus={solar_surplus_w}W | "
                f"min_forecast={min_forecast_display}°C",
                level="INFO",
            )
        else:
            max_recovery_display = (
                f"{max(forecast_recovery_temps):.1f}"
                if forecast_recovery_temps
                else "N/A"
            )
            self.log(
                f"No change: setpoint={target}°C | rule={rule} | "
                f"outdoor={outdoor_temp_c}°C | surplus={solar_surplus_w}W | "
                f"min_forecast={min_forecast_display}°C | "
                f"max_recovery={max_recovery_display}°C",
                level="DEBUG",
            )

        # --- Write active rule to dashboard entity (FR-10) ---
        self._write_active_rule(rule)

        # --- Send notification on setpoint change ---
        if current_setpoint is None or abs(target - current_setpoint) >= 0.1:
            self._send_setpoint_change_notification(
                old_setpoint=current_setpoint,
                new_setpoint=target,
                rule=rule,
                outdoor_temp_c=outdoor_temp_c,
                solar_surplus_w=solar_surplus_w,
            )

    # ------------------------------------------------------------------
    # Sensor readers
    # ------------------------------------------------------------------

    def _read_outdoor_temp(self) -> float | None:
        """Read current outdoor temperature from the weather entity."""
        weather_entity: str = self.args.get("weather_entity", "weather.home")
        try:
            temp = self.get_state(weather_entity, attribute="temperature")
            if temp in (None, "unavailable", "unknown"):
                raise ValueError(f"temperature attribute is {temp}")
            return float(temp)
        except (TypeError, ValueError) as exc:
            self.log(
                f"Weather entity '{weather_entity}' unavailable ({exc}). "
                "Disabling FR-04 and FR-05 this cycle.",
                level="WARNING",
            )
            return None

    def _read_solar_surplus(self) -> float | None:
        """Read net grid power from P1 sensor and derive solar export.

        solar_surplus_w = max(0, -p1_net_power_w)
        Positive P1 value = importing from grid.
        Negative P1 value = exporting solar surplus.
        Returns None if the entity is not configured or unavailable.
        """
        p1_entity: str | None = self.args.get("p1_net_power_entity")
        if not p1_entity:
            return None
        try:
            raw = self.get_state(p1_entity)
            if raw in (None, "unavailable", "unknown"):
                raise ValueError(f"State is {raw}")
            p1_net = float(raw)
            return max(0.0, -p1_net)
        except (TypeError, ValueError) as exc:
            self.log(
                f"P1 sensor '{p1_entity}' unavailable ({exc}). "
                "Disabling FR-02 and FR-03 this cycle.",
                level="WARNING",
            )
            return None

    def _read_forecast_temps(self, horizon_hours: float) -> list[float]:
        """Read hourly forecast temperatures up to horizon_hours ahead.

        Returns an empty list when the forecast is unavailable or empty.
        If the requested horizon exceeds available forecast data, uses whatever
        entries are available and logs a DEBUG message.

        Met.no provides forecast data as a list on the weather entity's
        'forecast' attribute. Each entry contains:
          - datetime: ISO 8601 string (UTC)
          - temperature: float in °C
        """
        weather_entity: str = self.args.get("weather_entity", "weather.home")
        try:
            forecast = self.get_state(weather_entity, attribute="forecast")
            if not forecast:
                self.log(
                    f"Forecast attribute empty or missing on '{weather_entity}'. "
                    "Disabling FR-04 this cycle.",
                    level="WARNING",
                )
                return []

            now_utc = datetime.now(timezone.utc)
            cutoff_ts = now_utc.timestamp() + horizon_hours * 3600.0
            temps: list[float] = []

            for entry in forecast:
                try:
                    entry_dt_str: str = entry.get("datetime", "")
                    entry_dt = datetime.fromisoformat(
                        entry_dt_str.replace("Z", "+00:00")
                    )
                    if entry_dt.timestamp() <= cutoff_ts:
                        temps.append(float(entry["temperature"]))
                except (TypeError, ValueError, KeyError):
                    continue

            if not temps:
                self.log(
                    f"No usable forecast entries within {horizon_hours:.1f}h window "
                    f"on '{weather_entity}'.",
                    level="WARNING",
                )
            elif horizon_hours > len(forecast):
                self.log(
                    f"Requested horizon {horizon_hours:.1f}h exceeds available "
                    f"forecast data ({len(forecast)} entries). Using available data.",
                    level="DEBUG",
                )

            return temps

        except Exception as exc:  # noqa: BLE001
            self.log(
                f"Failed to read forecast from '{weather_entity}': {exc}",
                level="WARNING",
            )
            return []

    def _read_forecast_solar(self) -> float | None:
        """Read predicted solar yield for the next hour from Forecast.Solar entity.

        The sensor reports Wh (watt-hours for the coming 1-hour window).
        Since the window is exactly 1 hour, Wh is numerically equivalent to
        the average power in W over that window.

        Returns None when the entity is not configured or unavailable.
        This is expected behaviour when Forecast.Solar is not installed.
        """
        solar_entity: str | None = self.args.get("forecast_solar_entity")
        if not solar_entity:
            return None
        try:
            raw = self.get_state(solar_entity)
            if raw in (None, "unavailable", "unknown"):
                raise ValueError(f"State is {raw}")
            return float(raw)
        except (TypeError, ValueError):
            # Not an error — Forecast.Solar is optional (FR-03)
            self.log(
                f"Forecast.Solar entity '{solar_entity}' unavailable. "
                "Disabling FR-03 predictive solar boost this cycle.",
                level="DEBUG",
            )
            return None

    def _read_current_setpoint(self) -> float | None:
        """Read the current setpoint from the thermostat climate entity."""
        thermostat: str | None = self.args.get("thermostat_entity")
        if not thermostat:
            return None
        try:
            temp = self.get_state(thermostat, attribute="temperature")
            return float(temp) if temp is not None else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Actuators
    # ------------------------------------------------------------------

    def _set_thermostat(self, target: float) -> None:
        """Send a new setpoint to the thermostat climate entity."""
        thermostat: str | None = self.args.get("thermostat_entity")
        if not thermostat:
            self.log(
                "No thermostat_entity configured — cannot set setpoint.",
                level="ERROR",
            )
            return
        try:
            self.call_service(
                "climate/set_temperature",
                entity_id=thermostat,
                temperature=target,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"Failed to set thermostat '{thermostat}' to {target}°C: {exc}",
                level="ERROR",
            )

    def _write_active_rule(self, rule: str) -> None:
        """Write the active rule name to input_text.shp_active_rule (FR-10)."""
        try:
            self.call_service(
                "input_text/set_value",
                entity_id="input_text.shp_active_rule",
                value=rule,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"Failed to write active rule '{rule}': {exc}",
                level="WARNING",
            )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _send_setpoint_change_notification(
        self,
        old_setpoint: float | None,
        new_setpoint: float,
        rule: str,
        outdoor_temp_c: float | None,
        solar_surplus_w: float | None,
    ) -> None:
        """Send a push notification when the thermostat setpoint changes.

        Only sends if:
        - input_boolean.shp_notifications_enabled is on (or unavailable → default on)
        - notify_targets is configured and non-empty in smart_heatpump.yaml
        """
        # Check the dashboard toggle
        try:
            enabled = self.get_state("input_boolean.shp_notifications_enabled")
            if enabled == "off":
                self.log(
                    "Notifications disabled via dashboard toggle.",
                    level="DEBUG",
                )
                return
        except Exception:  # noqa: BLE001
            pass  # If toggle is unavailable, default to sending

        targets: list[str] = self.args.get("notify_targets") or []
        if not targets:
            return

        # Build human-readable message
        description = _RULE_DESCRIPTIONS.get(rule, rule)
        outdoor_str = f"{outdoor_temp_c:.1f}°C" if outdoor_temp_c is not None else "N/A"
        surplus_str = f"{solar_surplus_w:.0f}W" if solar_surplus_w is not None else "N/A"
        old_str = f"{old_setpoint:.1f}°C" if old_setpoint is not None else "N/A"

        title = "🏠 Smart Heatpump"
        message = (
            f"{description}\n"
            f"\n"
            f"Setpoint: {old_str} → {new_setpoint:.1f}°C\n"
            f"Outdoor: {outdoor_str}\n"
            f"Solar export: {surplus_str}\n"
            f"Rule: {rule}"
        )

        for target_name in targets:
            try:
                self.call_service(
                    f"notify/{target_name}",
                    title=title,
                    message=message,
                )
                self.log(
                    f"Notification sent to '{target_name}': setpoint {old_str} → {new_setpoint:.1f}°C ({rule})",
                    level="INFO",
                )
            except Exception as exc:  # noqa: BLE001
                self.log(
                    f"Failed to send notification to '{target_name}': {exc}",
                    level="WARNING",
                )

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _read_config_float(self, key: str) -> float:
        """Read a float value from an input_number helper entity.

        Entity name is derived as: input_number.shp_{key}
        Falls back to the hardcoded default in _DEFAULTS on any failure (FR-12).
        """
        entity_id = f"input_number.shp_{key}"
        default = _DEFAULTS[key]
        try:
            raw = self.get_state(entity_id)
            if raw in (None, "unavailable", "unknown"):
                raise ValueError(f"State is {raw}")
            return float(raw)
        except (TypeError, ValueError):
            self.log(
                f"Config entity '{entity_id}' unavailable. Using default: {default}",
                level="WARNING",
            )
            return default

    # ------------------------------------------------------------------
    # Error recovery
    # ------------------------------------------------------------------

    def _safe_set_fallback(self) -> None:
        """Fallback on unhandled exception (FR-12).

        Sets the thermostat to temp_ideal and writes 'error_fallback' to the
        rule entity so the user can see something went wrong on the dashboard.
        """
        try:
            self._set_thermostat(_DEFAULTS["temp_ideal"])
        except Exception:  # noqa: BLE001
            pass
        try:
            self._write_active_rule("error_fallback")
        except Exception:  # noqa: BLE001
            pass
