"""Unit tests for the thermal model — pure Python, no HA dependency.

Run with:
    pytest tests/test_thermal_model.py -v
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

# Import thermal_model.py directly
import sys

_model_path = (
    Path(__file__).parent.parent
    / "custom_components"
    / "smart_heatpump"
    / "thermal_model.py"
)
_spec = importlib.util.spec_from_file_location("thermal_model", _model_path)
_module = importlib.util.module_from_spec(_spec)
sys.modules["thermal_model"] = _module  # Required for @dataclass to resolve __module__
_spec.loader.exec_module(_module)
ThermalObservation = _module.ThermalObservation
compute_loss_coefficient = _module.compute_loss_coefficient
predict_hours_until_below = _module.predict_hours_until_below


# ---------------------------------------------------------------------------
# compute_loss_coefficient tests
# ---------------------------------------------------------------------------

def _make_cooling_observations(
    k: float,
    indoor_start: float,
    outdoor: float,
    interval_min: int = 15,
    count: int = 50,
) -> list[ThermalObservation]:
    """Generate synthetic cooling observations for a known k."""
    obs = []
    t_indoor = indoor_start
    for i in range(count):
        ts = i * interval_min * 60
        obs.append(ThermalObservation(
            timestamp=ts,
            indoor_temp_c=t_indoor,
            outdoor_temp_c=outdoor,
            heating_active=False,
        ))
        # Apply Newton's cooling for one interval
        dt_h = interval_min / 60.0
        t_indoor = outdoor + (t_indoor - outdoor) * math.exp(-k * dt_h)
    return obs


def test_compute_k_well_insulated() -> None:
    """Compute k for a well-insulated home (k=0.03)."""
    obs = _make_cooling_observations(k=0.03, indoor_start=22.0, outdoor=5.0)
    k = compute_loss_coefficient(obs, min_samples=10)
    assert k is not None
    assert k == pytest.approx(0.03, abs=0.005)


def test_compute_k_poorly_insulated() -> None:
    """Compute k for a poorly insulated home (k=0.12)."""
    obs = _make_cooling_observations(k=0.12, indoor_start=22.0, outdoor=2.0)
    k = compute_loss_coefficient(obs, min_samples=10)
    assert k is not None
    assert k == pytest.approx(0.12, abs=0.01)


def test_compute_k_not_enough_data() -> None:
    """Not enough data points → None."""
    obs = _make_cooling_observations(k=0.05, indoor_start=21.0, outdoor=5.0, count=5)
    k = compute_loss_coefficient(obs, min_samples=24)
    assert k is None


def test_compute_k_ignores_heating_periods() -> None:
    """Observations with heating_active=True should be ignored."""
    obs = _make_cooling_observations(k=0.05, indoor_start=22.0, outdoor=5.0, count=50)
    # Mark half as heating
    for i in range(0, 50, 2):
        obs[i] = ThermalObservation(
            timestamp=obs[i].timestamp,
            indoor_temp_c=obs[i].indoor_temp_c,
            outdoor_temp_c=obs[i].outdoor_temp_c,
            heating_active=True,
        )
    # With every other point marked as heating, all pairs include at least
    # one heating point → not enough valid samples
    k = compute_loss_coefficient(obs, min_samples=24)
    assert k is None


# ---------------------------------------------------------------------------
# predict_hours_until_below tests
# ---------------------------------------------------------------------------

def test_predict_well_insulated_home() -> None:
    """Well-insulated home (k=0.03), 22°C indoor, 5°C outdoor.
    Should take many hours to drop to 20.5°C."""
    hours = predict_hours_until_below(
        indoor_temp_c=22.0,
        outdoor_temps=[5.0] * 48,  # constant 5°C for 48 hours
        threshold_temp_c=20.5,
        loss_coefficient_k=0.03,
    )
    assert hours > 2.0
    assert hours < 48.0


def test_predict_poorly_insulated_home() -> None:
    """Poorly insulated (k=0.15), 21.5°C indoor, 0°C outdoor.
    Should drop to 20.5°C relatively quickly."""
    hours = predict_hours_until_below(
        indoor_temp_c=21.5,
        outdoor_temps=[0.0] * 48,
        threshold_temp_c=20.5,
        loss_coefficient_k=0.15,
    )
    assert hours < 8.0
    assert hours > 0.0


def test_predict_already_below() -> None:
    """Indoor already below threshold → 0 hours."""
    hours = predict_hours_until_below(
        indoor_temp_c=20.0,
        outdoor_temps=[5.0] * 24,
        threshold_temp_c=20.5,
        loss_coefficient_k=0.05,
    )
    assert hours == 0.0


def test_predict_warm_outdoor_never_drops() -> None:
    """Outdoor warmer than threshold → indoor never drops below → inf."""
    hours = predict_hours_until_below(
        indoor_temp_c=22.0,
        outdoor_temps=[21.0] * 24,
        threshold_temp_c=20.5,
        loss_coefficient_k=0.05,
    )
    assert hours == float("inf")


def test_predict_empty_forecast() -> None:
    """No forecast data → inf (can't predict)."""
    hours = predict_hours_until_below(
        indoor_temp_c=22.0,
        outdoor_temps=[],
        threshold_temp_c=20.5,
        loss_coefficient_k=0.05,
    )
    assert hours == float("inf")


def test_predict_with_varying_outdoor() -> None:
    """Forecast shows cold night then warm morning.
    Should give different result than constant outdoor."""
    # Cold night (0°C) for 6 hours, then warming to 10°C
    outdoor = [0.0] * 6 + [10.0] * 18
    hours = predict_hours_until_below(
        indoor_temp_c=22.0,
        outdoor_temps=outdoor,
        threshold_temp_c=20.5,
        loss_coefficient_k=0.05,
    )
    # With k=0.05 and 0°C outdoor, the drop is about 1.1°C/hour
    # 22 - 20.5 = 1.5°C buffer at 0°C → should take ~3 hours
    assert hours > 1.0
    assert hours < 10.0
