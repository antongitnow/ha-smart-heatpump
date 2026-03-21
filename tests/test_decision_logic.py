"""Unit tests for the Smart Heatpump decide() function.

Tests cover all scenarios from PRD Section 11, plus indoor temperature
and thermal model tests.

No Home Assistant dependency — decide() is a pure Python function.

Run with:
    pytest tests/test_decision_logic.py -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Import decision.py directly without triggering __init__.py (which needs homeassistant).
_decision_path = (
    Path(__file__).parent.parent
    / "custom_components"
    / "smart_heatpump"
    / "decision.py"
)
_spec = importlib.util.spec_from_file_location("decision", _decision_path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
decide = _module.decide

# ---------------------------------------------------------------------------
# Shared default config values (match PRD Section 6 defaults)
# ---------------------------------------------------------------------------

DEFAULTS = dict(
    temp_ideal=21.0,
    temp_minimum=20.5,
    temp_solar_boost=22.5,
    preheat_delta=0.5,
    cop_threshold_temp=5.0,
    solar_surplus_threshold=500.0,
    indoor_comfort_margin=1.0,
)


def _decide(**overrides: object) -> tuple[float, str]:
    """Call decide() with defaults, overriding specific kwargs."""
    kwargs = {
        "outdoor_temp_c": 8.0,
        "indoor_temp_c": None,
        "solar_surplus_w": 0.0,
        "solar_confirmed": False,
        "forecast_solar_w": None,
        "forecast_temps": [],
        "forecast_recovery_temps": [],
        "hours_until_below_min": None,
        **DEFAULTS,
        **overrides,
    }
    return decide(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T01 — Normal: no solar, mild outdoor, warm forecast
# ---------------------------------------------------------------------------

def test_t01_default_no_solar_mild_outdoor() -> None:
    """No special conditions active -> default rule, ideal setpoint."""
    target, rule = _decide(
        outdoor_temp_c=8.0,
        solar_surplus_w=0.0,
        solar_confirmed=False,
        forecast_temps=[7.0, 6.5, 6.0],
    )
    assert rule == "default"
    assert target == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# T02 — Solar surplus confirmed
# ---------------------------------------------------------------------------

def test_t02_solar_surplus_confirmed() -> None:
    """Confirmed solar export -> solar_boost rule, boost setpoint."""
    target, rule = _decide(
        outdoor_temp_c=12.0,
        solar_surplus_w=700.0,
        solar_confirmed=True,
    )
    assert rule == "solar_boost"
    assert target == pytest.approx(22.5)


# ---------------------------------------------------------------------------
# T03 — Solar surplus present but not yet confirmed, no Forecast.Solar
# ---------------------------------------------------------------------------

def test_t03_solar_not_yet_confirmed() -> None:
    """Export above threshold but not confirmed -> default."""
    target, rule = _decide(
        outdoor_temp_c=12.0,
        solar_surplus_w=700.0,
        solar_confirmed=False,
        forecast_solar_w=None,
        forecast_temps=[10.0, 9.0],
    )
    assert rule == "default"
    assert target == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# T04 — Solar predicted via Forecast.Solar
# ---------------------------------------------------------------------------

def test_t04_solar_predicted_forecast_solar() -> None:
    """Forecast.Solar predicts >= threshold -> solar_predicted rule."""
    target, rule = _decide(
        outdoor_temp_c=8.0,
        solar_surplus_w=0.0,
        solar_confirmed=False,
        forecast_solar_w=600.0,
    )
    assert rule == "solar_predicted"
    assert target == pytest.approx(22.5)


# ---------------------------------------------------------------------------
# T05 — Pre-heat: cold coming within horizon, COP still good now
# ---------------------------------------------------------------------------

def test_t05_preheat_cold_coming_cop_good() -> None:
    """Forecast dips below threshold while outdoor is still above -> preheat."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        forecast_temps=[4.0, 2.0, 1.5],
    )
    assert rule == "preheat"
    assert target == pytest.approx(21.0 + 0.5)


# ---------------------------------------------------------------------------
# T06 — COP poor now, no recovery coming
# ---------------------------------------------------------------------------

def test_t06_conserve_no_recovery() -> None:
    """Outdoor below threshold, recovery also cold -> conserve."""
    target, rule = _decide(
        outdoor_temp_c=2.0,
        forecast_recovery_temps=[1.0, 0.5, 1.0],
    )
    assert rule == "conserve"
    assert target == pytest.approx(20.5)


# ---------------------------------------------------------------------------
# T07 — COP poor now, recovery coming within horizon
# ---------------------------------------------------------------------------

def test_t07_conserve_await_recovery() -> None:
    """Outdoor below threshold, recovery forecast shows improvement -> await."""
    target, rule = _decide(
        outdoor_temp_c=2.0,
        forecast_recovery_temps=[1.0, 4.0, 7.0],
    )
    assert rule == "conserve_await_recovery"
    assert target == pytest.approx(20.5)


# ---------------------------------------------------------------------------
# T08 — Solar wins over conserve_await_recovery
# ---------------------------------------------------------------------------

def test_t08_solar_wins_over_conserve_await_recovery() -> None:
    """Confirmed solar export beats conservation mode."""
    target, rule = _decide(
        outdoor_temp_c=2.0,
        solar_surplus_w=700.0,
        solar_confirmed=True,
        forecast_recovery_temps=[1.0, 4.0, 7.0],
    )
    assert rule == "solar_boost"
    assert target == pytest.approx(22.5)


# ---------------------------------------------------------------------------
# T09 — Solar wins over pre-heat simultaneously
# ---------------------------------------------------------------------------

def test_t09_solar_wins_over_preheat() -> None:
    """Confirmed solar export beats pre-heat rule."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        solar_surplus_w=700.0,
        solar_confirmed=True,
        forecast_temps=[2.0, 1.0],
    )
    assert rule == "solar_boost"
    assert target == pytest.approx(22.5)


# ---------------------------------------------------------------------------
# T10 — Safety floor: computed target cannot go below temp_minimum
# ---------------------------------------------------------------------------

def test_t10_safety_floor_enforced() -> None:
    """Even with conserve rule the setpoint is always >= temp_minimum."""
    target, rule = _decide(
        outdoor_temp_c=2.0,
        temp_minimum=20.5,
        forecast_recovery_temps=[],
    )
    assert rule == "conserve"
    assert target >= 20.5


def test_t10_safety_floor_custom_minimum() -> None:
    """Safety floor applies regardless of which rule is active."""
    target, rule = _decide(
        outdoor_temp_c=8.0,
        temp_ideal=20.5,
        temp_minimum=20.5,
        forecast_temps=[7.0],
    )
    assert target >= 20.5


# ---------------------------------------------------------------------------
# T11 — All sensors unavailable (None inputs)
# ---------------------------------------------------------------------------

def test_t11_all_sensors_unavailable() -> None:
    """When outdoor temp is None and forecast is empty -> default fallback."""
    target, rule = _decide(
        outdoor_temp_c=None,
        solar_surplus_w=None,
        solar_confirmed=False,
        forecast_solar_w=None,
        forecast_temps=[],
        forecast_recovery_temps=[],
    )
    assert rule == "default"
    assert target == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# T12 — Empty forecast list
# ---------------------------------------------------------------------------

def test_t12_empty_forecast_list() -> None:
    """No forecast data available -> default applies."""
    target, rule = _decide(
        outdoor_temp_c=8.0,
        forecast_temps=[],
        forecast_recovery_temps=[],
    )
    assert rule == "default"
    assert target == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# T13 — Boundary: outdoor exactly equals cop_threshold
# ---------------------------------------------------------------------------

def test_t13_outdoor_exactly_equals_cop_threshold() -> None:
    """At exactly threshold COP is considered poor -> conserve."""
    target, rule = _decide(
        outdoor_temp_c=5.0,
        cop_threshold_temp=5.0,
        forecast_temps=[],
        forecast_recovery_temps=[],
    )
    assert rule == "conserve"
    assert target == pytest.approx(20.5)


# ---------------------------------------------------------------------------
# T14 — Recovery boundary: max_recovery exactly equals threshold
# ---------------------------------------------------------------------------

def test_t14_max_recovery_exactly_equals_threshold() -> None:
    """When max recovery equals threshold -> conserve_await_recovery."""
    target, rule = _decide(
        outdoor_temp_c=2.0,
        cop_threshold_temp=5.0,
        forecast_recovery_temps=[3.0, 5.0],
    )
    assert rule == "conserve_await_recovery"
    assert target == pytest.approx(20.5)


# ===========================================================================
# Indoor temperature and thermal model tests
# ===========================================================================

# ---------------------------------------------------------------------------
# T15 — Indoor buffer + thermal model says safe → skip preheat
# ---------------------------------------------------------------------------

def test_t15_indoor_buffer_thermal_model_safe() -> None:
    """Indoor 22°C (1.5 above min), thermal model says 12h buffer,
    cold arrives at hour 3 → skip preheat."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        indoor_temp_c=22.0,
        forecast_temps=[6.0, 5.5, 4.0, 3.0, 2.0],  # cold at index 2
        hours_until_below_min=12.0,
    )
    assert rule == "indoor_buffer_ok"
    assert target == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# T16 — Indoor too close to minimum → preheat regardless of thermal model
# ---------------------------------------------------------------------------

def test_t16_indoor_close_to_minimum_still_preheats() -> None:
    """Indoor 21.0 (only 0.5 above min, below 1.0 margin) → preheat."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        indoor_temp_c=21.0,
        forecast_temps=[4.0, 2.0, 1.5],
        hours_until_below_min=12.0,
    )
    assert rule == "preheat"
    assert target == pytest.approx(21.5)


# ---------------------------------------------------------------------------
# T17 — Indoor buffer OK but thermal model still learning → preheat (conservative)
# ---------------------------------------------------------------------------

def test_t17_thermal_model_learning_preheats_conservatively() -> None:
    """Indoor 22°C, but thermal model is None (learning) → preheat."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        indoor_temp_c=22.0,
        forecast_temps=[4.0, 2.0, 1.5],
        hours_until_below_min=None,
    )
    assert rule == "preheat"
    assert target == pytest.approx(21.5)


# ---------------------------------------------------------------------------
# T18 — Indoor buffer OK but thermal model says NOT safe → preheat
# ---------------------------------------------------------------------------

def test_t18_indoor_buffer_thermal_model_not_safe() -> None:
    """Indoor 22°C, but thermal model says only 2h buffer, cold at hour 3 → preheat.
    2h < 3h means we'll drop below min before cold even fully arrives."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        indoor_temp_c=22.0,
        forecast_temps=[6.0, 5.5, 4.0, 3.0],  # cold at index 2
        hours_until_below_min=2.0,
    )
    assert rule == "preheat"
    assert target == pytest.approx(21.5)


# ---------------------------------------------------------------------------
# T19 — Indoor temp unknown → preheat as usual (conservative)
# ---------------------------------------------------------------------------

def test_t19_indoor_temp_unknown_preheats() -> None:
    """No indoor temp sensor → preheat (conservative, same as before)."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        indoor_temp_c=None,
        forecast_temps=[4.0, 2.0, 1.5],
        hours_until_below_min=12.0,
    )
    assert rule == "preheat"
    assert target == pytest.approx(21.5)
