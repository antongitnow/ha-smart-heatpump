"""Unit tests for the SmartHeatpump decide() function.

Tests cover all 14 scenarios from PRD Section 11.
No AppDaemon or Home Assistant dependency — decide() is a pure Python function.

Run with:
    pytest tests/test_decision_logic.py -v
"""

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Make the app importable without installing AppDaemon.
# We patch the 'appdaemon' module namespace so the import of smart_heatpump.py
# succeeds even when AppDaemon is not installed.
# ---------------------------------------------------------------------------

import types

# Create a minimal stub for appdaemon so the module-level import doesn't fail.
_ad = types.ModuleType("appdaemon")
_plugins = types.ModuleType("appdaemon.plugins")
_hass_plugin = types.ModuleType("appdaemon.plugins.hass")
_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")


class _HassMock:
    """Minimal stub for appdaemon.plugins.hass.hassapi.Hass."""


_hassapi.Hass = _HassMock  # type: ignore[attr-defined]
sys.modules.setdefault("appdaemon", _ad)
sys.modules.setdefault("appdaemon.plugins", _plugins)
sys.modules.setdefault("appdaemon.plugins.hass", _hass_plugin)
sys.modules.setdefault("appdaemon.plugins.hass.hassapi", _hassapi)

# Now import the pure decision function.
_app_path = Path(__file__).parent.parent / "appdaemon" / "apps" / "smart_heatpump"
sys.path.insert(0, str(_app_path))

from smart_heatpump import decide  # noqa: E402

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
)


def _decide(**overrides: object) -> tuple[float, str]:
    """Call decide() with defaults, overriding specific kwargs."""
    kwargs = {
        "outdoor_temp_c": 8.0,
        "solar_surplus_w": 0.0,
        "solar_confirmed": False,
        "forecast_solar_w": None,
        "forecast_temps": [],
        "forecast_recovery_temps": [],
        **DEFAULTS,
        **overrides,
    }
    return decide(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T01 — Normal: no solar, mild outdoor, warm forecast
# ---------------------------------------------------------------------------

def test_t01_default_no_solar_mild_outdoor() -> None:
    """No special conditions active → default rule, ideal setpoint."""
    target, rule = _decide(
        outdoor_temp_c=8.0,
        solar_surplus_w=0.0,
        solar_confirmed=False,
        forecast_temps=[7.0, 6.5, 6.0],  # all above threshold=5
    )
    assert rule == "default"
    assert target == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# T02 — Solar surplus confirmed
# ---------------------------------------------------------------------------

def test_t02_solar_surplus_confirmed() -> None:
    """Confirmed solar export → solar_boost rule, boost setpoint."""
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
    """Export above threshold but confirmation timer not yet elapsed → default."""
    target, rule = _decide(
        outdoor_temp_c=12.0,
        solar_surplus_w=700.0,
        solar_confirmed=False,
        forecast_solar_w=None,
        forecast_temps=[10.0, 9.0],  # warm, no preheat needed
    )
    assert rule == "default"
    assert target == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# T04 — Solar predicted via Forecast.Solar
# ---------------------------------------------------------------------------

def test_t04_solar_predicted_forecast_solar() -> None:
    """Forecast.Solar predicts >= threshold → solar_predicted rule."""
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
    """Forecast dips below threshold while outdoor is still above → preheat."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        forecast_temps=[4.0, 2.0, 1.5],  # will go below threshold=5
    )
    assert rule == "preheat"
    assert target == pytest.approx(21.0 + 0.5)  # temp_ideal + preheat_delta


# ---------------------------------------------------------------------------
# T06 — COP poor now, no recovery coming
# ---------------------------------------------------------------------------

def test_t06_conserve_no_recovery() -> None:
    """Outdoor below threshold, recovery forecast also stays cold → conserve."""
    target, rule = _decide(
        outdoor_temp_c=2.0,
        forecast_recovery_temps=[1.0, 0.5, 1.0],  # max=1.0 < threshold=5
    )
    assert rule == "conserve"
    assert target == pytest.approx(20.5)


# ---------------------------------------------------------------------------
# T07 — COP poor now, recovery coming within horizon
# ---------------------------------------------------------------------------

def test_t07_conserve_await_recovery() -> None:
    """Outdoor below threshold, but recovery forecast shows COP improving → await."""
    target, rule = _decide(
        outdoor_temp_c=2.0,
        forecast_recovery_temps=[1.0, 4.0, 7.0],  # max=7.0 >= threshold=5
    )
    assert rule == "conserve_await_recovery"
    assert target == pytest.approx(20.5)


# ---------------------------------------------------------------------------
# T08 — Solar wins over conserve_await_recovery
# ---------------------------------------------------------------------------

def test_t08_solar_wins_over_conserve_await_recovery() -> None:
    """Confirmed solar export beats conservation mode (priority 1 > priority 3)."""
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
    """Confirmed solar export beats pre-heat rule (priority 1 > priority 2)."""
    target, rule = _decide(
        outdoor_temp_c=6.0,
        solar_surplus_w=700.0,
        solar_confirmed=True,
        forecast_temps=[2.0, 1.0],  # would trigger preheat if solar absent
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
    # Force a case where temp_ideal == temp_minimum so floor is apparent
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
    """When outdoor temp is None and forecast is empty → default fallback."""
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
    """No forecast data available → preheat cannot trigger, default applies."""
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
    """At exactly threshold (5.0 == 5.0) COP is considered poor → conserve.

    The decide() function uses strict > for "good COP" (preheat trigger) and
    <= for "poor COP" (conservation trigger), so the boundary belongs to conserve.
    """
    target, rule = _decide(
        outdoor_temp_c=5.0,
        cop_threshold_temp=5.0,
        forecast_temps=[],        # no cold forecast → preheat won't fire regardless
        forecast_recovery_temps=[],  # no recovery data → conserve (not await)
    )
    assert rule == "conserve"
    assert target == pytest.approx(20.5)


# ---------------------------------------------------------------------------
# T14 — Recovery boundary: max_recovery exactly equals threshold
# ---------------------------------------------------------------------------

def test_t14_max_recovery_exactly_equals_threshold() -> None:
    """When max recovery forecast exactly equals threshold → conserve_await_recovery.

    The recovery check uses >=, so an exact match qualifies as "COP recovery coming".
    """
    target, rule = _decide(
        outdoor_temp_c=2.0,
        cop_threshold_temp=5.0,
        forecast_recovery_temps=[3.0, 5.0],  # max=5.0 == threshold=5.0
    )
    assert rule == "conserve_await_recovery"
    assert target == pytest.approx(20.5)
