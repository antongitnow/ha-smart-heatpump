"""Home battery helpers: battery-adjusted grid power and discharge detection.

No Home Assistant dependency — can be unit tested with plain pytest.

A home battery that aims for zero on the meter hides solar surplus from the
P1 meter (it charges first) and covers import (it discharges).  The
controller therefore decides on what the meter would show without the
battery:

    effective net = P1 net + battery discharge − battery charge (heat pump first only)

Battery discharge always counts as import, so a boost never sees battery
power as surplus.  Battery charging counts as surplus only when the heat
pump gets solar before the battery; with "battery first" only real export
can start a boost.

Battery power here is always "discharge positive": W the battery delivers to
the house, negative while it charges.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence


def to_discharge_w(value: float, positive_discharge: bool) -> float:
    """Convert a battery sensor reading to discharge-positive watts."""
    return value if positive_discharge else -value


def effective_net_w(net_w: float, discharge_w: float, charge_w: float, heatpump_first: bool) -> float:
    """Grid power as it would be without the battery (positive = import)."""
    return net_w + max(0.0, discharge_w) - (max(0.0, charge_w) if heatpump_first else 0.0)


def time_weighted_mean(
    samples: Sequence[tuple[float, float]],
    start: float,
    end: float,
    f: Callable[[float], float] = lambda v: v,
) -> float | None:
    """Time-weighted mean of f(value) over (start, end] — each reading holds until the next.

    ``samples`` are (timestamp, value) sorted by time; a sample at or before
    ``start`` supplies the value held at the start.  Returns None when there
    is no reading by ``end``.  Mirrors timeMean() in the dashboard card.
    """
    i = 0
    while i < len(samples) and samples[i][0] <= start:
        i += 1
    i -= 1
    if i < 0:
        if not samples or samples[0][0] > end:
            return None
        i = 0
        start = samples[0][0]
    total, t, v = 0.0, start, f(samples[i][1])
    j = i + 1
    while j < len(samples) and samples[j][0] <= end:
        total += v * (samples[j][0] - t)
        t, v = samples[j][0], f(samples[j][1])
        j += 1
    total += v * (end - t)
    return total / (end - start) if end > start else v


def discharge_sustained_within(
    samples: Sequence[tuple[float, float]],
    start: float,
    end: float,
    limit_w: float,
    reaction_s: float,
) -> bool:
    """True if, at some moment in (start, end], discharge had stayed above limit_w for reaction_s.

    ``samples`` are discharge-positive (timestamp, watts), each holding until
    the next.  Mirrors dischargeSustainedWithin() in the dashboard card.
    """
    run_start: float | None = None
    for k, (ts, w) in enumerate(samples):
        if ts > end:
            break
        until = samples[k + 1][0] if k + 1 < len(samples) else end
        if w > limit_w:
            if run_start is None:
                run_start = ts
            reached = run_start + reaction_s
            # Still above the limit up to `until`; the run counts once it is
            # old enough, at a moment inside the window
            if reached <= min(until, end) and min(until, end) > start:
                return True
        else:
            run_start = None
    return False


class DischargeWatch:
    """Tracks how long the battery has been discharging above a limit.

    Fed with live readings; ``sustained()`` says whether the current
    discharge run has lasted at least the reaction time.
    """

    def __init__(self) -> None:
        self.over_since: float | None = None

    def update(self, ts: float, discharge_w: float | None, limit_w: float) -> None:
        if discharge_w is None or discharge_w <= limit_w:
            self.over_since = None
        elif self.over_since is None:
            self.over_since = ts

    def sustained(self, ts: float, reaction_s: float) -> bool:
        return self.over_since is not None and ts - self.over_since >= reaction_s

    def due_at(self, reaction_s: float) -> float | None:
        """When the current run reaches the reaction time, or None without a run."""
        return None if self.over_since is None else self.over_since + reaction_s


class UnavailableWatch:
    """Tracks how long the battery sensor has given no usable reading.

    While it can't be read, the battery may be feeding a boost unseen.
    """

    def __init__(self) -> None:
        self.lost_since: float | None = None

    def update(self, ts: float, available: bool) -> None:
        if available:
            self.lost_since = None
        elif self.lost_since is None:
            self.lost_since = ts

    def sustained(self, ts: float, reaction_s: float) -> bool:
        return self.lost_since is not None and ts - self.lost_since >= reaction_s

    def due_at(self, reaction_s: float) -> float | None:
        """When the current outage reaches the reaction time, or None while readable."""
        return None if self.lost_since is None else self.lost_since + reaction_s
