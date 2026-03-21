"""Thermal model for the Smart Heatpump Controller.

Pure Python — no Home Assistant dependency. Enables unit testing with plain pytest.

Uses Newton's law of cooling to model the building's heat loss:
    dT_indoor/dt = -k * (T_indoor - T_outdoor)

Where k is the heat loss coefficient (per hour):
  - Well-insulated home: k ≈ 0.02–0.05
  - Average home:        k ≈ 0.05–0.10
  - Poorly insulated:    k ≈ 0.10–0.20
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class ThermalObservation:
    """A single temperature observation."""

    timestamp: float  # Unix epoch seconds
    indoor_temp_c: float
    outdoor_temp_c: float
    heating_active: bool  # True if thermostat was calling for heat


# Minimum indoor-outdoor delta to avoid numerical instability in ln()
_MIN_DELTA = 1.0

# Minimum number of valid cooling samples to produce a coefficient
MIN_SAMPLES = 24  # ~6 hours at 15-minute intervals


def compute_loss_coefficient(
    observations: list[ThermalObservation],
    min_samples: int = MIN_SAMPLES,
) -> float | None:
    """Compute the heat loss coefficient k from observed cooling periods.

    Only uses observation pairs where:
    - Heating was not active (both points)
    - Indoor temp was falling or flat
    - Indoor-outdoor delta was large enough for stable ln()

    Returns None if not enough valid samples.
    """
    k_estimates: list[float] = []

    for i in range(1, len(observations)):
        prev = observations[i - 1]
        curr = observations[i]

        # Only use cooling periods (heating off)
        if prev.heating_active or curr.heating_active:
            continue

        # Only use periods where indoor temp is falling or flat
        if curr.indoor_temp_c > prev.indoor_temp_c + 0.05:
            continue

        dt_hours = (curr.timestamp - prev.timestamp) / 3600.0
        if dt_hours <= 0 or dt_hours > 2.0:
            # Skip gaps (>2h) or zero/negative intervals
            continue

        # Average outdoor temp for the interval
        t_out = (prev.outdoor_temp_c + curr.outdoor_temp_c) / 2.0

        delta_prev = prev.indoor_temp_c - t_out
        delta_curr = curr.indoor_temp_c - t_out

        # Need sufficient delta for stable computation
        if abs(delta_prev) < _MIN_DELTA or abs(delta_curr) < _MIN_DELTA:
            continue

        # Both deltas must be positive (indoor warmer than outdoor)
        if delta_prev <= 0 or delta_curr <= 0:
            continue

        ratio = delta_curr / delta_prev
        if ratio <= 0 or ratio >= 1.5:
            # Ratio > 1 means indoor got warmer (shouldn't happen with heating off)
            # Allow small ratio > 1 due to measurement noise, but cap at 1.5
            continue

        # k = -ln(ratio) / dt
        k_est = -math.log(ratio) / dt_hours

        # Sanity bounds: k between 0.001 and 0.5 per hour
        if 0.001 <= k_est <= 0.5:
            k_estimates.append(k_est)

    if len(k_estimates) < min_samples:
        return None

    # Use median for robustness against outliers
    k_estimates.sort()
    mid = len(k_estimates) // 2
    if len(k_estimates) % 2 == 0:
        return (k_estimates[mid - 1] + k_estimates[mid]) / 2.0
    return k_estimates[mid]


def predict_hours_until_below(
    indoor_temp_c: float,
    outdoor_temps: list[float],
    threshold_temp_c: float,
    loss_coefficient_k: float,
) -> float:
    """Predict hours until indoor temp drops below threshold.

    Steps through the forecast hour by hour, applying Newton's law of
    cooling with each hour's outdoor temperature.

    Args:
        indoor_temp_c: Current indoor temperature.
        outdoor_temps: Hourly outdoor temperature forecast. If empty,
            returns infinity (can't predict without outdoor data).
        threshold_temp_c: The indoor temperature threshold (temp_minimum).
        loss_coefficient_k: The learned heat loss coefficient (per hour).

    Returns:
        Hours until indoor temp drops below threshold.
        Returns float('inf') if it never drops below within the forecast window.
    """
    if not outdoor_temps:
        return float("inf")

    if indoor_temp_c <= threshold_temp_c:
        return 0.0

    t_indoor = indoor_temp_c

    for hour, t_outdoor in enumerate(outdoor_temps):
        # Apply Newton's cooling for 1 hour
        # T(1h) = T_out + (T_in - T_out) * exp(-k * 1)
        delta = t_indoor - t_outdoor
        if delta > 0:
            t_indoor = t_outdoor + delta * math.exp(-loss_coefficient_k)
        # If indoor <= outdoor, indoor won't drop further from cooling

        if t_indoor <= threshold_temp_c:
            # Interpolate within this hour for more precision
            # At start of hour: t_indoor was delta above outdoor
            # Solve for t: threshold = t_outdoor + delta * exp(-k*t)
            if delta > 0:
                target_delta = threshold_temp_c - t_outdoor
                if target_delta > 0:
                    t_frac = -math.log(target_delta / delta) / loss_coefficient_k
                    return hour + min(t_frac, 1.0)
            return float(hour)

    return float("inf")
