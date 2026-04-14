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
_snap_half = _module._snap_half

# ---------------------------------------------------------------------------
# Shared default config values
# ---------------------------------------------------------------------------

DEFAULTS = dict(
    temp_ideal=21.0,
    solar_surplus_threshold=300.0,
    solar_release_threshold_high=700.0,
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
        "avg_export_5min_w": 0.0,
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
            avg_export_5min_w=1000.0,
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
        """Export > threshold → activate solar boost, setpoint snapped to 0.5."""
        target, rule, boost = _decide(
            avg_export_5min_w=700.0,
            current_temperature=21.2,
        )
        assert rule == "solar_incremental"
        assert boost is True
        # 21.2 + 0.5 = 21.7, floor-snap → 21.5
        assert target == pytest.approx(21.5)

    def test_no_activation_below_threshold(self) -> None:
        """Export below threshold → no activation."""
        target, rule, boost = _decide(
            avg_export_5min_w=300.0,
            current_temperature=21.0,
        )
        assert rule == "no_solar_action"
        assert boost is False

    def test_activation_clamps_to_ideal(self) -> None:
        """If current_temp + delta < ideal, clamp to ideal."""
        target, rule, boost = _decide(
            avg_export_5min_w=700.0,
            current_temperature=20.0,  # 20.0 + 0.5 = 20.5 < 21.0 ideal
        )
        assert rule == "solar_incremental"
        assert boost is True
        assert target >= 21.0

    def test_activation_no_temp_sensor(self) -> None:
        """No temperature sensor → use ideal + delta."""
        target, rule, boost = _decide(
            avg_export_5min_w=700.0,
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
        """Step-down that would go below ideal → clamp to ideal, deactivate boost.

        Room temp must be at/below ideal for deactivation to happen,
        because the room-temp floor (room + delta) keeps setpoint above ideal
        when the room is warm.
        """
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_temperature=20.0,  # at/below ideal → floor is 20.5
            current_setpoint=21.3,  # 21.3 - 0.5 = 20.8, floor max(20.8, 20.5) = 20.8 → clamp to 21.0
        )
        assert rule == "solar_boost_deactivated"
        assert boost is False
        assert target == pytest.approx(21.0)

    def test_step_down_exactly_at_ideal(self) -> None:
        """Step-down that lands exactly on ideal → deactivate boost."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_temperature=20.0,  # below ideal → floor is 20.5
            current_setpoint=21.5,  # 21.5 - 0.5 = 21.0, floor max(21.0, 20.5) = 21.0 = ideal
        )
        assert rule == "solar_boost_deactivated"
        assert boost is False
        assert target == pytest.approx(21.0)

    def test_step_down_respects_room_temp_floor(self) -> None:
        """Step-down cannot drop setpoint below room temp + delta (prevents heatpump cycling)."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_temperature=22.0,
            current_setpoint=22.5,  # 22.5 - 0.5 = 22.0, but floor = 22.0 + 0.5 = 22.5
        )
        assert rule == "solar_step_down"
        assert boost is True
        assert target == pytest.approx(22.5)  # held at floor, heatpump stays on

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

    def test_setpoint_tracks_rising_room_temp(self) -> None:
        """When room temp rises to match setpoint, setpoint must stay ahead."""
        # Room warmed up to 22.5, setpoint is also 22.5 → must go to 23.0
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=100.0,
            current_temperature=22.5,
            current_setpoint=22.5,
        )
        assert rule == "solar_incremental"
        assert boost is True
        assert target == pytest.approx(23.0)

    def test_setpoint_stays_above_room_temp(self) -> None:
        """Setpoint must always be at least room_temp + delta during boost."""
        # Room is 22.0, setpoint somehow at 22.0 → must go to at least 22.5
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=100.0,
            current_temperature=22.0,
            current_setpoint=22.0,
        )
        assert rule == "solar_incremental"
        assert boost is True
        assert target >= 22.0 + 0.5


# ===========================================================================
# Boundary: thresholds exactly matched
# ===========================================================================

class TestThresholdBoundaries:
    """Tests for exact threshold boundary behavior."""

    def test_export_exactly_at_threshold_no_activation(self) -> None:
        """Export exactly at threshold → no activation (> not >=)."""
        target, rule, boost = _decide(
            avg_export_5min_w=300.0,  # not > 300
        )
        assert rule == "no_solar_action"
        assert boost is False

    def test_import_exactly_at_high_threshold_no_reset(self) -> None:
        """Import exactly at high threshold → no reset (> not >=)."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=700.0,  # not > 700
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

    def test_zero_export_no_activation(self) -> None:
        """Zero export → no activation."""
        target, rule, boost = _decide(
            avg_export_5min_w=0.0,
        )
        assert rule == "no_solar_action"
        assert boost is False

    def test_boost_active_none_setpoint(self) -> None:
        """Boost active, no current setpoint, moderate import → room-temp floor keeps boost on."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_setpoint=None,
            current_temperature=21.0,  # room at ideal → floor = 21.5
        )
        assert rule == "solar_step_down"
        assert boost is True
        assert target == pytest.approx(21.5)  # held above room temp

    def test_custom_step_delta(self) -> None:
        """Custom step delta is respected."""
        target, rule, boost = _decide(
            avg_export_5min_w=700.0,
            current_temperature=21.0,
            solar_step_delta=1.0,
        )
        assert rule == "solar_incremental"
        assert target == pytest.approx(22.0)


# ===========================================================================
# Rounding — setpoint always snaps to nearest 0.5°C
# ===========================================================================

class TestSetpointRounding:
    """Ensure setpoints are always clean 0.5 steps, never raw sensor floats."""

    def test_snap_half_examples(self) -> None:
        """_snap_half floors to nearest 0.5."""
        assert _snap_half(21.699) == pytest.approx(21.5)
        assert _snap_half(22.277) == pytest.approx(22.0)
        assert _snap_half(21.0) == pytest.approx(21.0)
        assert _snap_half(21.5) == pytest.approx(21.5)
        assert _snap_half(21.99) == pytest.approx(21.5)
        assert _snap_half(22.0) == pytest.approx(22.0)

    def test_activation_rounds_setpoint(self) -> None:
        """Activation with fractional room temp produces clean 0.5 step."""
        target, rule, boost = _decide(
            avg_export_5min_w=700.0,
            current_temperature=21.699,  # raw sensor value
        )
        assert rule == "solar_incremental"
        # 21.699 + 0.5 = 22.199 → snaps to 22.0
        assert target == pytest.approx(22.0)

    def test_continued_boost_rounds_setpoint(self) -> None:
        """Continued boost with fractional room temp produces clean steps."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=0.0,
            current_temperature=21.699,
            current_setpoint=22.0,
        )
        assert rule == "solar_incremental"
        # 22.0 + 0.5 = 22.5, capped at 21.699 + 1.0 = 22.699, snap → 22.5
        assert target == pytest.approx(22.5)

    def test_step_down_rounds_setpoint(self) -> None:
        """Step-down produces a clean 0.5 value."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,
            current_setpoint=22.5,
        )
        assert rule == "solar_step_down"
        assert target == pytest.approx(22.0)


# ===========================================================================
# Minimum boost duration — prevent short cycling
# ===========================================================================

class TestMinimumBoostDuration:
    """Ensure boost stays active during minimum run period."""

    def test_min_run_blocks_high_import_reset(self) -> None:
        """High import during min run → keep boost active, don't reset."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=900.0,  # Would normally trigger reset
            current_temperature=21.5,
            current_setpoint=22.0,
            boost_active_seconds=300,  # 5 minutes
            min_boost_minutes=20,
        )
        assert rule == "solar_min_run"
        assert boost is True
        assert target >= 22.0  # Setpoint stays ahead of room temp

    def test_min_run_blocks_step_down(self) -> None:
        """Moderate import during min run → keep boost active."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=400.0,  # Would normally step down
            current_temperature=21.5,
            current_setpoint=22.0,
            boost_active_seconds=600,  # 10 minutes
            min_boost_minutes=20,
        )
        assert rule == "solar_min_run"
        assert boost is True

    def test_min_run_expired_allows_reset(self) -> None:
        """After min run expires, high import triggers normal reset."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=900.0,
            current_temperature=21.5,
            current_setpoint=22.0,
            boost_active_seconds=1200 + 1,  # 20 min + 1 sec
            min_boost_minutes=20,
        )
        assert rule == "solar_reset"
        assert boost is False

    def test_min_run_tracks_room_temp(self) -> None:
        """During min run, setpoint stays ahead of rising room temp."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=500.0,
            current_temperature=22.0,  # Room warmed up
            current_setpoint=22.0,     # Setpoint caught up
            boost_active_seconds=300,
            min_boost_minutes=20,
        )
        assert rule == "solar_min_run"
        assert boost is True
        assert target >= 22.5  # Must stay ahead of room temp

    def test_min_run_zero_disables_protection(self) -> None:
        """min_boost_minutes=0 means no protection."""
        target, rule, boost = _decide(
            solar_boost_active=True,
            avg_import_5min_w=900.0,
            current_temperature=21.5,
            current_setpoint=22.0,
            boost_active_seconds=60,
            min_boost_minutes=0,
        )
        assert rule == "solar_reset"
        assert boost is False
