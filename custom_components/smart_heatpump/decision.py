"""Pure decision function for the Smart Heatpump Controller.

No Home Assistant dependency — can be unit tested with plain pytest.

Decision priority (highest wins):
  1. Solar surplus confirmed (FR-02) OR solar predicted (FR-03)
  2. COP pre-heat: cold coming within horizon, COP still good now (FR-04)
     — BUT skip if indoor temp has enough thermal buffer
  3. COP conservation: COP poor now (FR-05)
  4. Default: maintain ideal temperature (FR-06)

Safety floor (FR-08): setpoint is always >= temp_minimum regardless of rule.
"""

from __future__ import annotations


def decide(
    outdoor_temp_c: float | None,
    indoor_temp_c: float | None,
    solar_surplus_w: float | None,
    solar_confirmed: bool,
    forecast_solar_w: float | None,
    forecast_temps: list[float],
    forecast_recovery_temps: list[float],
    temp_ideal: float,
    temp_minimum: float,
    temp_solar_boost: float,
    preheat_delta: float,
    cop_threshold_temp: float,
    solar_surplus_threshold: float,
    hours_until_below_min: float | None = None,
    indoor_comfort_margin: float = 1.0,
) -> tuple[float, str]:
    """Decide the target thermostat setpoint and active rule name.

    Args:
        outdoor_temp_c: Current outdoor temperature (°C), or None if unavailable.
        indoor_temp_c: Current indoor temperature (°C), or None if unavailable.
        solar_surplus_w: Current solar export (W, >=0), or None if unavailable.
        solar_confirmed: True when export sustained >= solar_confirm_minutes.
        forecast_solar_w: Predicted solar yield next hour (Wh ≈ W), or None.
        forecast_temps: Outdoor forecast temps up to effective preheat horizon.
        forecast_recovery_temps: Outdoor forecast temps for COP recovery horizon.
        temp_ideal: Default comfort setpoint (°C).
        temp_minimum: Hard floor — setpoint never goes below this (°C).
        temp_solar_boost: Setpoint during solar surplus (°C).
        preheat_delta: Extra °C above ideal during pre-heat.
        cop_threshold_temp: Outdoor temp below which COP is considered poor (°C).
        solar_surplus_threshold: Minimum export (W) to count as surplus.
        hours_until_below_min: Thermal model prediction — hours until indoor temp
            drops below temp_minimum. None = model still learning (conservative).
        indoor_comfort_margin: °C above temp_minimum considered "comfortable enough"
            to skip pre-heating. Default 1.0°C.

    Returns:
        (target_temp, rule_name) where target_temp is clamped to >= temp_minimum.
    """
    # Priority 1: Solar surplus — confirmed or predicted
    solar_predicted = (
        forecast_solar_w is not None
        and forecast_solar_w >= solar_surplus_threshold
    )
    if solar_confirmed or solar_predicted:
        rule = "solar_boost" if solar_confirmed else "solar_predicted"
        return max(temp_solar_boost, temp_minimum), rule

    # If outdoor temp is unknown, fall back to ideal
    if outdoor_temp_c is None:
        return max(temp_ideal, temp_minimum), "default"

    min_forecast_temp: float | None = min(forecast_temps) if forecast_temps else None
    max_recovery_temp: float | None = (
        max(forecast_recovery_temps) if forecast_recovery_temps else None
    )

    # Priority 2: COP pre-heat — cold coming, COP still good now
    if (
        min_forecast_temp is not None
        and min_forecast_temp < cop_threshold_temp
        and outdoor_temp_c > cop_threshold_temp
    ):
        # Check if we can skip pre-heating based on indoor temperature buffer
        has_indoor_buffer = (
            indoor_temp_c is not None
            and indoor_temp_c > temp_minimum + indoor_comfort_margin
        )

        if has_indoor_buffer and hours_until_below_min is not None:
            # Thermal model is ready — find when cold weather arrives
            first_cold_hour = next(
                (i for i, t in enumerate(forecast_temps) if t < cop_threshold_temp),
                None,
            )
            # If indoor temp will stay above minimum longer than it takes
            # for cold to arrive and pass, skip pre-heating
            if first_cold_hour is not None and hours_until_below_min > first_cold_hour:
                return max(temp_ideal, temp_minimum), "indoor_buffer_ok"

        # No thermal data or not enough buffer — pre-heat as usual
        target = temp_ideal + preheat_delta
        return max(target, temp_minimum), "preheat"

    # Priority 3: COP conservation — COP poor now
    if outdoor_temp_c <= cop_threshold_temp:
        if max_recovery_temp is not None and max_recovery_temp >= cop_threshold_temp:
            rule = "conserve_await_recovery"
        else:
            rule = "conserve"
        return max(temp_minimum, temp_minimum), rule

    # Priority 4: Default
    return max(temp_ideal, temp_minimum), "default"
