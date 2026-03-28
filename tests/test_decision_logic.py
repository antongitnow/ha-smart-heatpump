"""Unit tests for the Smart Heatpump solar incremental decision logic.

Tests cover the solar-incremental flow from the flowchart:
  - Heating season check
  - Solar boost activation on surplus
  - Step-down on moderate import
  - Reset on high import
  - Deactivation when setpoint reaches ideal

No Home Assistant dependency — decide_solar() and is_heating_season()
are pure Python functions.

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
decide_solar = _module.decide_solar
is_heating_season = _module.is_heating_season

# ---------------------------------------------------------------------------
# Shared default config values
# ---------------------------------------------------------------------------

DEFAULTS = dict(
    temp_ideal=21.0,
    solar_surplus_threshold=500.0,
    solar_release_threshold_high=800.0,
    solar_release_threshold_low=300.0,
    solar_step_delta=0.5,
    season_start_month=9,
    season_end_month=4,
)


def _decide(**overrides: object) -> tuple[float, str, bool]:
    """Call decide_solar() with defaults, overriding specific kwargs."""
    kwargs = {
        "current_month": 11,  # November — heating season
        "solar_boost_active": False,
        "current_export_w": 0.0,
        "avg_import_5min_w": 0.0,
        "current_temperature": 21.0,
        "current_setpoint": 21.0,
        **DEFAULTS,
        **overrides,
    }
    return decide_solar(**kwargs)  # type: ignore[arg-type]


# ===========================================================================
# Heating season tests
# ===========================================================================

class TestHeatingSeasonCheck:
    """Tests for is_heating_season() logic."""

    def test_heating_season_wrap_around_november(self) -> None:
        """November is in heating season (Sep-Apr)."""
        assert is_heating_season(11, 9, 4) is True

    def test_heating_season_wrap_around_january(self) -> None:
        """January is in heating season (Sep-Apr)."""
        assert is_heating_season(1, 9, 4) is True

    def test_heating_season_wrap_around_april(self) -> None:
        """April (end month) is in heating season (Sep-Apr)."""
        assert is_heating_season(4, 9, 4) is True

    def test_heating_season_wrap_around_september(self) -> None:
        """September (start month) is in heating season (Sep-Apr)."""
        assert is_heating_season(9, 9, 4) is True

    def test_not_heating_season_june(self) -> None:
        """June is NOT in heating season (Sep-Apr)."""
        assert is_heating_season(6, 9, 4) is False

    def test_not_heating_season_august(self) -> None:
        """August is NOT in heating season (Sep-Apr)."""
        assert is_heating_season(8, 9, 4) is False

    def test_same_start_end_single_month(self) -> None:
        """Start=end means only that single month."""
        assert is_heating_season(3, 3, 3) is True
        assert is_heating_season(4, 3, 3) is False

    def test_linear_range_jan_to_apr(self) -> None:
        """Non-wrapping range Jan-Apr."""
        assert is_heating_season(2, 1, 4) is True
        assert is_heating_season(5, 1, 4) is False


# ===========================================================================
# Outside heating season — no solar action
# ===========================================================================

class TestOutsideHeatingSeasonNoAction:
    """Tests that no action is taken outside heating season."""

    def test_summer_no_action(self) -> None:
        """June with solar surplus → no_solar_action (not heating season)."""
        target, rule, boost = _decide(
            current_month=6,
            current_export_w=1000.0,
        )
        assert rule == "no_solar_action"
        assert boost is False

    def test_summer_preserves_current_setpoint(self) -> None:
        """Outside heating season, current setpoint is preserved."""
        target, rule, boost = _decide(
            current_month=7,
            current_setpoint=22.0,
        )
        assert rule == "no_solar_action"
        assert target == pytest.approx(22.0)


# ===========================================================================
# Solar boost activation — surplus detected
# ===========================================================================

class TestSolarBoostActivation:
    """Tests for activating solar boost when surplus is detected."""

    def test_activate_on_surplus(self) -> None:
        """Export > threshold → activate solar boost, setpoint = current_temp + delta."""
        target, rule, boost = _decide(
            current_export_w=700.0,
            current_temperature=21.2,
        )
        assert rule == "solar_incremental"
        assert boost is True
        assert target == pytest.approx(21.2 + 0.5)

    def test_no_activation_below_threshold(self) -> None:
        """Export below threshold → no activation."""
        target, rule, boost = _decide(
            current_export_w=300.0,
            current_temperature=21.0,
        )
        assert rule == "no_solar_action"
        assert boost is False

    def test_activation_clamps_to_ideal(self) -> None:
        """If current_temp + delta < ideal, clamp to ideal."""
        target, rule, boost = _decide(
            current_export_w=700.0,
            current_temperature=20.0,  # 20.0 + 0.5 = 20.5 < 21.0 ideal
        )
        assert rule == "solar_incremental"
        assert boost is True
        assert target >= 21.0

    def test_activation_no_temp_sensor(self) -> None:
        """No temperature sensor → use ideal + delta."""
        target, rule, boost = _decide(
            current_export_w=700.0,
            current_temperature=None,
        )
        assert rule == "solar_incremental"
        assert boost is True
        assert target == pytest.approx(21.0 + 0.5)


# ===========================================================================
# Solar boost active — import checks
# ===========================================================================

class TestSolarBoostActiveImportChecks:
    """Tests for behavior when solar boost is already active."""

    def test_high_import_resets(self) -> None:
        """5-min avg import > high threshold → reset to ideal, deactivate."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=900.0,
            current_setpoint=23.0,
        )
        assert rule == "solar_reset"
        assert boost is False
        assert target == pytest.approx(21.0)

    def test_moderate_import_steps_down(self) -> None:
        """5-min avg import > low threshold → step setpoint down by delta."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_setpoint=23.0,
        )
        assert rule == "solar_step_down"
        assert boost is True
        assert target == pytest.approx(23.0 - 0.5)

    def test_step_down_clamps_to_ideal(self) -> None:
        """Step-down that would go below ideal → deactivate boost."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_setpoint=21.3,  # 21.3 - 0.5 = 20.8 < 21.0 → clamp to 21.0
        )
        assert rule == "solar_boost_deactivated"
        assert boost is False
        assert target == pytest.approx(21.0)

    def test_step_down_exactly_at_ideal(self) -> None:
        """Step-down that lands exactly on ideal → deactivate."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_setpoint=21.5,  # 21.5 - 0.5 = 21.0 = ideal
        )
        assert rule == "solar_boost_deactivated"
        assert boost is False
        assert target == pytest.approx(21.0)

    def test_no_excess_import_boosts(self) -> None:
        """Low import → continue boosting (setpoint = current_setpoint + delta)."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=100.0,
            current_temperature=22.0,
            current_setpoint=22.5,
        )
        assert rule == "solar_incremental"
        assert boost is True
        assert target == pytest.approx(22.5 + 0.5)

    def test_boost_capped_at_1_degree_above_room(self) -> None:
        """Setpoint cannot exceed current room temp + 1.0°C."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=100.0,
            current_temperature=22.0,
            current_setpoint=23.0,  # 23.0 + 0.5 = 23.5, but cap is 22.0 + 1.0 = 23.0
        )
        assert rule == "solar_incremental"
        assert boost is True
        assert target == pytest.approx(23.0)


# ===========================================================================
# Boundary: thresholds exactly matched
# ===========================================================================

class TestThresholdBoundaries:
    """Tests for exact threshold boundary behavior."""

    def test_export_exactly_at_threshold_no_activation(self) -> None:
        """Export exactly at threshold → no activation (> not >=)."""
        target, rule, boost = _decide(
            current_export_w=500.0,  # not > 500
        )
        assert rule == "no_solar_action"
        assert boost is False

    def test_import_exactly_at_high_threshold_no_reset(self) -> None:
        """Import exactly at high threshold → no reset (> not >=)."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=800.0,  # not > 800
            current_temperature=22.0,
            current_setpoint=22.5,
        )
        assert rule != "solar_reset"

    def test_import_exactly_at_low_threshold_no_step_down(self) -> None:
        """Import exactly at low threshold → no step down (> not >=)."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=300.0,  # not > 300
            current_temperature=22.0,
            current_setpoint=22.5,
        )
        assert rule == "solar_incremental"


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:
    """Edge case tests."""

    def test_none_export_no_activation(self) -> None:
        """Export is None → no activation."""
        target, rule, boost = _decide(
            current_export_w=None,
        )
        assert rule == "no_solar_action"
        assert boost is False

    def test_boost_active_none_setpoint(self) -> None:
        """Boost active, no current setpoint → step-down uses ideal."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_setpoint=None,
        )
        assert rule == "solar_boost_deactivated"
        assert target == pytest.approx(21.0)

    def test_custom_step_delta(self) -> None:
        """Custom step delta is respected."""
        target, rule, boost = _decide(
            current_export_w=700.0,
            current_temperature=21.0,
            solar_step_delta=1.0,
        )
        assert rule == "solar_incremental"
        assert target == pytest.approx(22.0)
