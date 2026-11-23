"""Unit tests for the home battery helpers.

Run with:
    pytest tests/test_battery.py -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_path = Path(__file__).parent.parent / "custom_components" / "smart_heatpump" / "battery.py"
_spec = importlib.util.spec_from_file_location("battery", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DischargeWatch = _mod.DischargeWatch
boost_discharge_series = _mod.boost_discharge_series
boost_discharge_w = _mod.boost_discharge_w
discharge_limit_w = _mod.discharge_limit_w
UnavailableWatch = _mod.UnavailableWatch
discharge_sustained_within = _mod.discharge_sustained_within
effective_net_w = _mod.effective_net_w
time_weighted_mean = _mod.time_weighted_mean
to_discharge_w = _mod.to_discharge_w


class TestEffectiveNet:
    def test_discharge_counts_as_import(self):
        # Battery covers 400 W, the meter reads 0 — without it you'd import 400 W
        assert effective_net_w(0, 400, 0, heatpump_first=False) == 400
        assert effective_net_w(0, 400, 0, heatpump_first=True) == 400

    def test_charging_is_surplus_only_with_heat_pump_first(self):
        # Battery soaks up 600 W of solar, the meter reads 0
        assert effective_net_w(0, 0, 600, heatpump_first=False) == 0
        assert effective_net_w(0, 0, 600, heatpump_first=True) == -600

    def test_grid_charging_is_not_surplus(self):
        # Night: battery charges 800 W from the grid on top of a 200 W house load
        assert effective_net_w(1000, 0, 800, heatpump_first=True) == 200

    def test_sign_conversion(self):
        assert to_discharge_w(300, positive_discharge=True) == 300
        assert to_discharge_w(300, positive_discharge=False) == -300


class TestTimeWeightedMean:
    def test_value_held_from_before_the_window(self):
        # 100 W for 40 s, then 300 W for 60 s
        assert time_weighted_mean([(0, 100), (50, 300)], 10, 110) == pytest.approx(220)

    def test_starts_at_first_reading_inside_the_window(self):
        assert time_weighted_mean([(60, 100)], 10, 110) == pytest.approx(100)

    def test_no_reading_by_the_end(self):
        assert time_weighted_mean([], 0, 10) is None
        assert time_weighted_mean([(20, 5)], 0, 10) is None

    def test_mapping_function(self):
        samples = [(0, 200), (50, -400)]
        assert time_weighted_mean(samples, 0, 100, lambda v: max(0, v)) == pytest.approx(100)
        assert time_weighted_mean(samples, 0, 100, lambda v: max(0, -v)) == pytest.approx(200)


class TestDischargeSustainedWithin:
    def test_long_enough_inside_window(self):
        assert discharge_sustained_within([(0, 0), (100, 300)], 0, 300, 50, 120)

    def test_too_short(self):
        assert not discharge_sustained_within([(0, 0), (100, 300), (200, 0)], 0, 300, 50, 120)

    def test_run_not_old_enough_by_window_end(self):
        assert not discharge_sustained_within([(0, 0), (250, 300)], 0, 300, 50, 120)

    def test_run_started_before_the_window(self):
        assert discharge_sustained_within([(-500, 300)], 0, 300, 50, 120)

    def test_run_ended_before_the_window(self):
        assert not discharge_sustained_within([(-500, 300), (-100, 0)], 0, 300, 50, 120)

    def test_at_or_below_limit_never_counts(self):
        assert not discharge_sustained_within([(0, 50), (100, -800)], 0, 300, 50, 0)

    def test_run_continues_across_readings(self):
        samples = [(0, 100), (60, 400), (120, 200), (180, 0)]
        assert discharge_sustained_within(samples, 0, 300, 50, 150)
        assert not discharge_sustained_within(samples, 0, 300, 50, 200)


class TestDischargeWatch:
    def test_sustained_after_reaction_time(self):
        w = DischargeWatch()
        w.update(0, 300, 50)
        w.update(60, 280, 50)  # still the same run
        assert not w.sustained(100, 120)
        assert w.sustained(120, 120)
        assert w.due_at(120) == 120

    def test_drop_ends_the_run(self):
        w = DischargeWatch()
        w.update(0, 300, 50)
        w.update(60, 20, 50)
        assert not w.sustained(500, 120)
        assert w.due_at(120) is None

    def test_unavailable_ends_the_run(self):
        w = DischargeWatch()
        w.update(0, 300, 50)
        w.update(10, None, 50)
        assert w.over_since is None


class TestUnavailableWatch:
    def test_outage_sustained_after_reaction_time(self):
        w = UnavailableWatch()
        w.update(0, available=False)
        w.update(30, available=False)
        assert not w.sustained(60, 120)
        assert w.sustained(120, 120)
        assert w.due_at(120) == 120

    def test_reading_ends_the_outage(self):
        w = UnavailableWatch()
        w.update(0, available=False)
        w.update(60, available=True)
        assert not w.sustained(500, 120)
        assert w.due_at(120) is None


class TestBoostDischarge:
    def test_limit_never_below_floor(self):
        assert discharge_limit_w(50) == 300
        assert discharge_limit_w(450) == 450

    def test_export_is_subtracted(self):
        # The reported case: 288 W discharge while exporting 155 W → 133 W counts
        assert boost_discharge_w(288, -155) == 133
        assert boost_discharge_w(100, -155) == 0
        assert boost_discharge_w(500, 200) == 500  # importing: all of it counts
        assert boost_discharge_w(500, None) == 500  # meter unknown: all of it counts
        assert boost_discharge_w(None, -100) is None

    def test_meter_jitter_around_zero_still_detects_discharge(self):
        # A battery holding the meter at zero: ±30 W must not hide 800 W discharge
        assert boost_discharge_w(800, -30) == 770

    def test_series_merges_and_goes_stale(self):
        bat = [(100, 400)]
        net = [(0, -200), (150, 100)]
        assert boost_discharge_series(bat, net, stale_s=300) == [
            (100, 200), (150, 400), (300, 400), (450, 400),
        ]
        # A silent meter stops counting as export after stale_s
        assert boost_discharge_series([(0, 400)], [(0, -1000)], stale_s=60) == [(0, 0), (60, 400)]

    def test_series_empty_without_battery(self):
        assert boost_discharge_series([], [(0, -100)]) == []
