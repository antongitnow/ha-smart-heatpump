"""Pure decision function for the Smart Heatpump Controller — Solar Incremental Flow.

No Home Assistant dependency — can be unit tested with plain pytest.

This module implements the solar-incremental thermostat control:
  - During heating season, when solar surplus is detected, boost setpoint
    incrementally above current room temperature.
  - When import rises, step down or reset.
  - Thermal model ML learning continues independently (handled by coordinator).

Safety floor: setpoint is always >= temp_ideal (never goes below comfort).
"""

from __future__ import annotations


def is_heating_season(month: int, season_start: int, season_end: int) -> bool:
    """Check if the given month falls within the heating season.

    Heating season wraps around the year boundary, e.g. start=9 (Sep), end=4 (Apr)
    means months 9,10,11,12,1,2,3,4 are heating season.
    """
    if season_start <= season_end:
        # e.g. start=1, end=4 → Jan–Apr
        return season_start <= month <= season_end
    else:
        # e.g. start=9, end=4 → Sep–Apr (wraps around year)
        return month >= season_start or month <= season_end


def decide_solar(
    current_month: int,
    solar_boost_active: bool,
    current_export_w: float | None,
    avg_import_5min_w: float,
    current_temperature: float | None,
    current_setpoint: float | None,
    temp_ideal: float,
    solar_surplus_threshold: float,
    solar_release_threshold_high: float,
    solar_release_threshold_low: float,
    solar_step_delta: float,
    season_start_month: int,
    season_end_month: int,
) -> tuple[float, str, bool]:
    """Decide the target thermostat setpoint for the solar incremental flow.

    Args:
        current_month: Current month (1-12).
        solar_boost_active: Whether solar boost is currently active.
        current_export_w: Current real-time solar export in Watts (>=0), or None.
        avg_import_5min_w: 5-minute rolling average of grid import in Watts (>=0).
        current_temperature: Current room temperature in °C, or None.
        current_setpoint: Current thermostat setpoint in °C, or None.
        temp_ideal: Comfort setpoint / safety floor (°C).
        solar_surplus_threshold: Min export (W) to activate solar boost.
        solar_release_threshold_high: Import above which solar boost resets immediately.
        solar_release_threshold_low: Import above which solar boost steps down.
        solar_step_delta: °C to boost above current temp / step down per cycle.
        season_start_month: First month of heating season (1-12).
        season_end_month: Last month of heating season (1-12).

    Returns:
        (target_setpoint, rule_name, new_solar_boost_active)
    """
    # Not heating season → no solar action
    if not is_heating_season(current_month, season_start_month, season_end_month):
        return (
            current_setpoint if current_setpoint is not None else temp_ideal,
            "no_solar_action",
            False,
        )

    # Branch: solar boost is NOT currently active
    if not solar_boost_active:
        # Check real-time export
        if current_export_w is not None and current_export_w > solar_surplus_threshold:
            # Activate solar boost
            if current_temperature is not None:
                target = current_temperature + solar_step_delta
            else:
                target = temp_ideal + solar_step_delta
            target = max(target, temp_ideal)
            return target, "solar_incremental", True
        else:
            # No surplus → no action
            return (
                current_setpoint if current_setpoint is not None else temp_ideal,
                "no_solar_action",
                False,
            )

    # Branch: solar boost IS currently active
    # Check high import → immediate reset
    if avg_import_5min_w > solar_release_threshold_high:
        return temp_ideal, "solar_reset", False

    # Check moderate import → step down
    if avg_import_5min_w > solar_release_threshold_low:
        if current_setpoint is not None:
            target = current_setpoint - solar_step_delta
        else:
            target = temp_ideal
        target = max(target, temp_ideal)

        # If stepped down to ideal, deactivate boost
        if target <= temp_ideal:
            return temp_ideal, "solar_boost_deactivated", False

        return target, "solar_step_down", True

    # No excess import — boost: step up from current setpoint
    # Cap at max 1.0°C above current room temperature
    if current_setpoint is not None:
        target = current_setpoint + solar_step_delta
    elif current_temperature is not None:
        target = current_temperature + solar_step_delta
    else:
        target = temp_ideal + solar_step_delta
    if current_temperature is not None:
        target = min(target, current_temperature + 1.0)
    target = max(target, temp_ideal)
    return target, "solar_incremental", True
