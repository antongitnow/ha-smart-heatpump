"""Pure decision function for the Smart Heatpump Controller.

No Home Assistant dependency — can be unit tested with plain pytest.

Decision priority (highest wins):
  1. Solar surplus confirmed (FR-02) OR solar predicted (FR-03)
  2. COP pre-heat: cold coming within horizon, COP still good now (FR-04)
  3. COP conservation: COP poor now (FR-05)
  4. Default: maintain ideal temperature (FR-06)

Safety floor (FR-08): setpoint is always >= temp_minimum regardless of rule.
"""

from __future__ import annotations


def decide(
    outdoor_temp_c: float | None,
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
) -> tuple[float, str]:
    """Decide the target thermostat setpoint and active rule name.

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
