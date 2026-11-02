"""Unit tests for solar-powered heat pump energy accounting.

Run with:
    pytest tests/test_solar_energy.py -v
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

_base = Path(__file__).parent.parent / "custom_components" / "smart_heatpump"
_spec = importlib.util.spec_from_file_location("solar_energy", _base / "solar_energy.py")
_mod = importlib.util.module_from_spec(_spec)
import sys

sys.modules["solar_energy"] = _mod  # dataclasses needs the module registered
_spec.loader.exec_module(_mod)
SolarEnergyTracker = _mod.SolarEnergyTracker
solar_power_w = _mod.solar_power_w

DAY = "2026-10-23"


class TestSolarPower:
    def test_not_boosting_counts_nothing(self):
        assert solar_power_w(1000, -500, boost_active=False) == 0.0

    def test_export_counts_whole_draw(self):
        assert solar_power_w(1000, -500, True) == 1000

    def test_balanced_at_zero_counts_whole_draw(self):
        assert solar_power_w(1000, 0, True) == 1000

    def test_import_within_tolerance_counts(self):
        assert solar_power_w(1000, 40, True) == 1000

    def test_import_counts_nothing(self):
        assert solar_power_w(1000, 300, True) == 0.0


class TestIntegration:
    def test_one_hour_on_surplus_is_one_kwh(self):
        t = SolarEnergyTracker()
        t.update(0, DAY, 1000, -200, True)
        # Hold values in 1-minute steps for an hour
        for m in range(1, 61):
            t.update(m * 60, DAY, 1000, -200, True)
        assert t.solar_kwh(DAY) == pytest.approx(1.0)
        assert t.total_kwh(DAY) == pytest.approx(1.0)
        assert t.total_solar_kwh == pytest.approx(1.0)
        assert t.solar_share_pct(DAY) == pytest.approx(100.0)

    def test_boost_off_counts_total_only(self):
        t = SolarEnergyTracker()
        t.update(0, DAY, 2000, 500, False)
        t.update(300, DAY, 2000, 500, False)
        assert t.solar_kwh(DAY) == 0.0
        assert t.total_kwh(DAY) == pytest.approx(2000 * 300 / 3600 / 1000)
        assert t.solar_share_pct(DAY) == 0.0

    def test_uses_previous_sample_over_interval(self):
        t = SolarEnergyTracker()
        t.update(0, DAY, 1000, -100, True)  # boosting + exporting...
        t.update(360, DAY, 1000, 900, False)  # ...then boost ends
        # The 6 minutes belong to the first sample: 1000 W * 0.1 h
        assert t.solar_kwh(DAY) == pytest.approx(0.1)
        t.update(720, DAY, 1000, 900, False)
        assert t.solar_kwh(DAY) == pytest.approx(0.1)  # second interval not solar

    def test_gap_is_dropped(self):
        t = SolarEnergyTracker()
        t.update(0, DAY, 1000, -100, True)
        t.update(3600, DAY, 1000, -100, True)
        assert t.total_kwh(DAY) == 0.0

    def test_unavailable_sensor_closes_interval_and_resets(self):
        t = SolarEnergyTracker()
        t.update(0, DAY, 1000, -100, True)
        t.update(60, DAY, None, -100, True)  # closes the first minute
        t.update(120, DAY, 1000, -100, True)  # nothing held from before
        assert t.solar_kwh(DAY) == pytest.approx(1000 * 60 / 3600 / 1000)

    def test_days_are_separate(self):
        t = SolarEnergyTracker()
        t.update(0, "2026-10-22", 1000, -100, True)
        t.update(60, "2026-10-23", 1000, -100, True)
        t.update(120, "2026-10-23", 1000, -100, True)
        assert t.solar_kwh("2026-10-22") == 0.0
        assert t.solar_kwh("2026-10-23") == pytest.approx(2 * 1000 * 60 / 3600 / 1000)

    def test_range_sum(self):
        t = SolarEnergyTracker()
        t.days = {
            "2026-10-21": {"solar": 1000.0, "total": 2000.0},
            "2026-10-22": {"solar": 500.0, "total": 500.0},
            "2026-10-23": {"solar": 250.0, "total": 1000.0},
        }
        assert t.solar_kwh_since(date(2026, 10, 22), date(2026, 10, 23)) == pytest.approx(0.75)
        assert t.total_kwh_since(date(2026, 10, 1), date(2026, 10, 23)) == pytest.approx(3.5)

    def test_no_data_share_is_none(self):
        assert SolarEnergyTracker().solar_share_pct(DAY) is None

    def test_history_is_pruned(self):
        t = SolarEnergyTracker()
        for i in range(70):
            t.days[f"2026-01-{i:03d}"] = {"solar": 1.0, "total": 1.0}
        t.update(0, DAY, 1000, 0, True)
        t.update(60, DAY, 1000, 0, True)
        assert len(t.days) <= 62
        assert DAY in t.days


def test_persistence_roundtrip():
    t = SolarEnergyTracker()
    t.update(0, DAY, 1000, -100, True)
    t.update(60, DAY, 1000, -100, True)
    restored = SolarEnergyTracker()
    restored.load_dict(t.to_dict())
    assert restored.total_solar_kwh == pytest.approx(t.total_solar_kwh)
    assert restored.solar_kwh(DAY) == pytest.approx(t.solar_kwh(DAY))
