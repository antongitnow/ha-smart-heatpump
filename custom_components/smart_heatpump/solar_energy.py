"""Solar-powered heat pump energy accounting.

No Home Assistant dependency — can be unit tested with plain pytest.

Integrates heat pump power over time, counting an interval as solar only
while a solar boost is active AND the house is not importing from the grid
(net power <= NET_TOLERANCE_W — exporting or balanced at zero).  During such
an interval the heat pump's whole draw is counted.  If the grid is being
imported, or no boost is active, nothing counts as solar.

Every interval's heat pump energy is also added to a per-day "total" bucket,
so a solar share can be derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

# Intervals longer than this are dropped rather than integrated — a gap this
# long means a restart or an outage, and the held values are no longer trustworthy.
MAX_GAP_SECONDS = 600.0

# Grid import up to this level (W) still counts as "no import" — absorbs
# smart meter noise while the heat pump soaks up the whole surplus.
NET_TOLERANCE_W = 50.0

# Days of per-day history kept (enough for "this month" and a 30-day view).
HISTORY_DAYS = 62


def solar_power_w(heatpump_w: float, net_w: float, boost_active: bool) -> float:
    """Heat pump power (W) counted as solar: all of it while boosting without import."""
    if not boost_active or net_w > NET_TOLERANCE_W:
        return 0.0
    return max(0.0, heatpump_w)


@dataclass
class _Sample:
    ts: float
    heatpump_w: float
    net_w: float
    boost_active: bool


class SolarEnergyTracker:
    """Accumulates solar-powered and total heat pump energy per day."""

    def __init__(self) -> None:
        self.total_solar_wh: float = 0.0
        # "YYYY-MM-DD" -> {"solar": Wh, "total": Wh}
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
    ) -> bool:
        """Integrate the interval since the previous sample, then store this one.

        The previous sample's values are held over the interval (the sensors
        only report on change).  ``day`` is the local date of ``ts``.  Pass
        ``None`` for a power when the sensor is unavailable — the previous
        interval is still closed, and integration restarts at the next valid
        sample.  Returns True when energy was added.
        """
        added = False
        prev = self._last
        if prev is not None:
            dt = ts - prev.ts
            if 0 < dt <= MAX_GAP_SECONDS:
                hours = dt / 3600.0
                bucket = self.days.setdefault(day, {"solar": 0.0, "total": 0.0})
                total_wh = max(0.0, prev.heatpump_w) * hours
                solar_wh = (
                    solar_power_w(prev.heatpump_w, prev.net_w, prev.boost_active)
                    * hours
                )
                bucket["total"] += total_wh
                bucket["solar"] += solar_wh
                self.total_solar_wh += solar_wh
                added = total_wh > 0
                self._prune(day)

        if heatpump_w is None or net_w is None:
            self._last = None
        else:
            self._last = _Sample(ts, heatpump_w, net_w, boost_active)
        return added

    def _prune(self, today: str) -> None:
        if len(self.days) <= HISTORY_DAYS:
            return
        for key in sorted(self.days)[: len(self.days) - HISTORY_DAYS]:
            if key != today:
                del self.days[key]

    # ------------------------------------------------------------------
    # Queries (kWh)
    # ------------------------------------------------------------------

    @property
    def total_solar_kwh(self) -> float:
        return self.total_solar_wh / 1000.0

    def solar_kwh(self, day: str) -> float:
        return self.days.get(day, {}).get("solar", 0.0) / 1000.0

    def total_kwh(self, day: str) -> float:
        return self.days.get(day, {}).get("total", 0.0) / 1000.0

    def solar_kwh_since(self, first_day: date, last_day: date) -> float:
        """Solar kWh summed over first_day..last_day inclusive."""
        return self._sum("solar", first_day, last_day)

    def total_kwh_since(self, first_day: date, last_day: date) -> float:
        return self._sum("total", first_day, last_day)

    def _sum(self, field: str, first_day: date, last_day: date) -> float:
        wh = 0.0
        day = first_day
        while day <= last_day:
            wh += self.days.get(day.isoformat(), {}).get(field, 0.0)
            day += timedelta(days=1)
        return wh / 1000.0

    def solar_share_pct(self, day: str) -> float | None:
        """Share of the day's heat pump energy that was solar, or None if no data."""
        total = self.days.get(day, {}).get("total", 0.0)
        if total <= 0:
            return None
        return 100.0 * self.days[day]["solar"] / total

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_solar_wh": self.total_solar_wh,
            "days": {
                k: {"solar": v["solar"], "total": v["total"]}
                for k, v in self.days.items()
            },
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        self.total_solar_wh = float(data.get("total_solar_wh", 0.0))
        self.days = {
            k: {"solar": float(v.get("solar", 0.0)), "total": float(v.get("total", 0.0))}
            for k, v in data.get("days", {}).items()
            if isinstance(v, dict)
        }
