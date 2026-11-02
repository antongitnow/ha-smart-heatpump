"""Solar boost accounting: heat pump energy, boost statistics and savings.

No Home Assistant dependency — can be unit tested with plain pytest.

Integrates the heat pump and P1 readings over time (each reading holds until
the next).  While a solar boost is active, the heat pump's draw is split into
a grid part and a solar part: other loads in the house get the solar first,
so the heat pump's grid share is min(heat pump draw, grid import) and the rest
is solar.  Outside a boost nothing counts as solar.

With a heat output meter, the heat delivered while boosting is split the
same way: the heat from solar is the heat output times the solar share of
the heat pump's draw.

Per day it also keeps boost time, the number of boosts and resets, export
left unused (exported while no boost was active) and the money saved — the
solar kWh valued at the import price minus what an exported kWh earns, at the
prices in force when the energy was used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

# Intervals longer than this are dropped rather than integrated — a gap this
# long means a restart or an outage, and the held values are no longer trustworthy.
MAX_GAP_SECONDS = 600.0

# Days of per-day history kept (enough for "this month" and a 30-day view).
HISTORY_DAYS = 62

# Per-day fields: Wh, seconds, counts and euros
_FIELDS = (
    "solar", "total", "grid_boost", "export_unused", "boost_s", "boosts", "resets", "savings", "heat_solar",
)


def grid_power_w(heatpump_w: float, net_w: float, boost_active: bool) -> float:
    """Heat pump power (W) drawn from the grid while boosting: min(draw, import)."""
    if not boost_active:
        return 0.0
    return min(max(0.0, heatpump_w), max(0.0, net_w))


def solar_power_w(heatpump_w: float, net_w: float, boost_active: bool) -> float:
    """Heat pump power (W) counted as solar: its draw beyond the grid share."""
    if not boost_active:
        return 0.0
    return max(0.0, heatpump_w) - grid_power_w(heatpump_w, net_w, boost_active)


def solar_heat_w(heat_w: float, heatpump_w: float, net_w: float, boost_active: bool) -> float:
    """Heat output (W) produced from solar: heat × the solar share of the draw."""
    if heatpump_w <= 0:
        return 0.0
    return max(0.0, heat_w) * solar_power_w(heatpump_w, net_w, boost_active) / heatpump_w


@dataclass
class _Sample:
    ts: float
    heatpump_w: float
    net_w: float
    boost_active: bool
    value_eur_per_kwh: float
    heat_w: float


class SolarEnergyTracker:
    """Accumulates solar boost energy, statistics and savings per day."""

    def __init__(self) -> None:
        self.total_solar_wh: float = 0.0
        self.total_savings_eur: float = 0.0
        self.total_heat_solar_wh: float = 0.0
        # "YYYY-MM-DD" -> {field: value} for every field in _FIELDS
        self.days: dict[str, dict[str, float]] = {}
        self._last: _Sample | None = None

    # ------------------------------------------------------------------
    # Integration
    # ------------------------------------------------------------------

    def update(
        self,
        ts: float,
        day: str,
        heatpump_w: float | None,
        net_w: float | None,
        boost_active: bool,
        value_eur_per_kwh: float = 0.0,
        heat_w: float | None = None,
    ) -> bool:
        """Integrate the interval since the previous sample, then store this one.

        The previous sample's values are held over the interval (the sensors
        only report on change).  ``day`` is the local date of ``ts``.  Pass
        ``None`` for a power when the sensor is unavailable — the previous
        interval is still closed, and integration restarts at the next valid
        sample.  Pass 0 for ``heatpump_w`` when no heat pump meter exists, so
        boost time and unused export are still tracked.  ``value_eur_per_kwh``
        is what a solar kWh used by the heat pump is worth right now.
        ``heat_w`` is the heat pump's heat output; None (no meter, or
        unavailable) counts no heat for the interval.
        Returns True when anything was added.
        """
        added = False
        prev = self._last
        if prev is not None:
            dt = ts - prev.ts
            if 0 < dt <= MAX_GAP_SECONDS:
                hours = dt / 3600.0
                bucket = self._bucket(day)
                solar_wh = solar_power_w(prev.heatpump_w, prev.net_w, prev.boost_active) * hours
                saved = solar_wh / 1000.0 * prev.value_eur_per_kwh
                bucket["total"] += max(0.0, prev.heatpump_w) * hours
                bucket["solar"] += solar_wh
                bucket["grid_boost"] += grid_power_w(prev.heatpump_w, prev.net_w, prev.boost_active) * hours
                bucket["savings"] += saved
                heat_wh = solar_heat_w(prev.heat_w, prev.heatpump_w, prev.net_w, prev.boost_active) * hours
                bucket["heat_solar"] += heat_wh
                self.total_heat_solar_wh += heat_wh
                if prev.boost_active:
                    bucket["boost_s"] += dt
                else:
                    bucket["export_unused"] += max(0.0, -prev.net_w) * hours
                self.total_solar_wh += solar_wh
                self.total_savings_eur += saved
                added = True
                self._prune(day)

        if heatpump_w is None or net_w is None:
            self._last = None
        else:
            self._last = _Sample(
                ts, heatpump_w, net_w, boost_active, value_eur_per_kwh, heat_w or 0.0
            )
        return added

    def count(self, day: str, field: str) -> None:
        """Count a boost start ("boosts") or a high-import reset ("resets")."""
        self._bucket(day)[field] += 1

    def _bucket(self, day: str) -> dict[str, float]:
        bucket = self.days.setdefault(day, {})
        for field in _FIELDS:
            bucket.setdefault(field, 0.0)
        return bucket

    def _prune(self, today: str) -> None:
        if len(self.days) <= HISTORY_DAYS:
            return
        for key in sorted(self.days)[: len(self.days) - HISTORY_DAYS]:
            if key != today:
                del self.days[key]

    # ------------------------------------------------------------------
    # Queries (kWh unless named otherwise)
    # ------------------------------------------------------------------

    @property
    def total_solar_kwh(self) -> float:
        return self.total_solar_wh / 1000.0

    def value(self, day: str, field: str) -> float:
        """Raw per-day value of a field (Wh, seconds, count or euros)."""
        return self.days.get(day, {}).get(field, 0.0)

    def solar_kwh(self, day: str) -> float:
        return self.value(day, "solar") / 1000.0

    def total_kwh(self, day: str) -> float:
        return self.value(day, "total") / 1000.0

    def solar_kwh_since(self, first_day: date, last_day: date) -> float:
        """Solar kWh summed over first_day..last_day inclusive."""
        return self.sum_since("solar", first_day, last_day) / 1000.0

    def total_kwh_since(self, first_day: date, last_day: date) -> float:
        return self.sum_since("total", first_day, last_day) / 1000.0

    def sum_since(self, field: str, first_day: date, last_day: date) -> float:
        """Raw field summed over first_day..last_day inclusive."""
        total = 0.0
        day = first_day
        while day <= last_day:
            total += self.value(day.isoformat(), field)
            day += timedelta(days=1)
        return total

    def solar_share_pct(self, day: str) -> float | None:
        """Share of the day's heat pump energy that was solar, or None if no data."""
        total = self.value(day, "total")
        if total <= 0:
            return None
        return 100.0 * self.value(day, "solar") / total

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_solar_wh": self.total_solar_wh,
            "total_savings_eur": self.total_savings_eur,
            "total_heat_solar_wh": self.total_heat_solar_wh,
            "days": {k: dict(v) for k, v in self.days.items()},
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        self.total_solar_wh = float(data.get("total_solar_wh", 0.0))
        self.total_savings_eur = float(data.get("total_savings_eur", 0.0))
        self.total_heat_solar_wh = float(data.get("total_heat_solar_wh", 0.0))
        self.days = {
            k: {field: float(v.get(field, 0.0)) for field in _FIELDS}
            for k, v in data.get("days", {}).items()
            if isinstance(v, dict)
        }
