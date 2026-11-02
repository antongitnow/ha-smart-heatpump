"""Checks the Lovelace card's JS decision port against decide_solar().

The card replays a day of P1 history with decideSolar() from
frontend/smart-heatpump-card.js. If that port drifts from decision.py, the
card would show a replay the controller never makes — these tests run both
on the same inputs and require identical output.

Needs Node.js (skipped otherwise). Run with:
    pytest tests/test_card_replay.py -v
"""

from __future__ import annotations

import importlib.util
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "smart_heatpump"
_CARD = _COMPONENT / "frontend" / "smart-heatpump-card.js"

_spec = importlib.util.spec_from_file_location("decision", _COMPONENT / "decision.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
decide_solar = _module.decide_solar

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")

AVG_WINDOW_S = 5 * 60
MIN_READINGS = 3


def _run_node(script: str, payload: object) -> object:
    """Run an ES module snippet with the card imported as `card`; stdin → `input`."""
    prelude = (
        "globalThis.HTMLElement = class {};\n"  # the card class extends it at load time
        f"const card = await import({json.dumps(_CARD.as_uri())});\n"
        "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         "import { createRequire } from 'module'; const require = createRequire(import.meta.url);\n"
         + prelude + script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _random_case(rng: random.Random) -> dict:
    net = rng.choice([rng.uniform(-3000, 3000), rng.choice([0.0, -300.0, 300.0, 700.0])])
    return {
        "active": rng.random() < 0.6,
        "avgExport": max(0.0, -net),
        "avgImport": max(0.0, net),
        "temp": rng.choice([None, round(rng.uniform(19, 24), 1)]),
        "setpoint": rng.choice([None, round(rng.uniform(20, 25) * 2) / 2, round(rng.uniform(20, 25), 2)]),
        "ideal": rng.choice([20.5, 21.0, 21.5]),
        "S": rng.choice([0.0, 200.0, 300.0, 500.0]),
        "L": rng.choice([0.0, 200.0, 300.0]),
        "H": rng.choice([300.0, 600.0, 700.0, 1500.0]),
        "step": rng.choice([0.1, 0.3, 0.5, 1.0]),
        "boostSeconds": rng.choice([0.0, 300.0, 1199.0, 1200.0, 5000.0]),
        "minBoostMin": rng.choice([0.0, 20.0]),
        "maxBoost": rng.choice([22.0, 25.0]),
        "exportNow": rng.choice([0.0, rng.uniform(0, 2000)]),
    }


def _python_decide(c: dict) -> list:
    target, rule, active = decide_solar(
        current_month=1,
        solar_boost_active=c["active"],
        avg_export_5min_w=c["avgExport"],
        avg_import_5min_w=c["avgImport"],
        current_temperature=c["temp"],
        current_setpoint=c["setpoint"],
        temp_ideal=c["ideal"],
        solar_surplus_threshold=c["S"],
        solar_release_threshold_high=c["H"],
        solar_release_threshold_low=c["L"],
        solar_step_delta=c["step"],
        active_months={1},
        boost_active_seconds=c["boostSeconds"],
        min_boost_minutes=c["minBoostMin"],
        max_boost_temp=c["maxBoost"],
        current_export_w=c["exportNow"],
    )
    return [target, rule, active]


def test_decide_matches_python():
    rng = random.Random(42)
    cases = [_random_case(rng) for _ in range(5000)]
    js = _run_node(
        "console.log(JSON.stringify(input.map((c) => card.decideSolar(c))));",
        cases,
    )
    for case, got in zip(cases, js):
        expected = _python_decide(case)
        assert got[1:] == expected[1:], case
        assert got[0] == pytest.approx(expected[0]), case


def _python_replay(readings: list, temps: list, p: dict) -> list:
    """Reference replay with the coordinator's evaluation semantics."""
    out = []
    active, sp, activated_at = False, None, None
    t = p["start"] + p["interval"] * 60
    while t <= p["end"]:
        window = [w for (ts, w) in readings if t - AVG_WINDOW_S < ts <= t]
        if len(window) < MIN_READINGS:
            out.append([sp if sp is not None else p["ideal"], "waiting_for_data", active])
        else:
            avg = sum(window) / len(window)
            past = [c for (ts, c) in temps if ts <= t]
            sp, rule, new_active = decide_solar(
                current_month=1,
                solar_boost_active=active,
                avg_export_5min_w=max(0.0, -avg),
                avg_import_5min_w=max(0.0, avg),
                current_temperature=past[-1] if past else None,
                current_setpoint=sp,
                temp_ideal=p["ideal"],
                solar_surplus_threshold=p["S"],
                solar_release_threshold_high=p["H"],
                solar_release_threshold_low=p["L"],
                solar_step_delta=p["step"],
                active_months={1},
                boost_active_seconds=(t - activated_at) if active and activated_at is not None else 0.0,
                min_boost_minutes=p["minBoost"],
                max_boost_temp=p["maxBoost"],
                current_export_w=max(0.0, -window[-1]),
            )
            if new_active and not active:
                activated_at = t
            elif not new_active:
                activated_at = None
            active = new_active
            out.append([sp, rule, active])
        t += p["interval"] * 60
    return out


def test_replay_matches_coordinator_semantics():
    rng = random.Random(7)
    # A day of ~10 s readings: night import, sunny spells, cloud and heat pump bursts
    readings, temps = [], []
    level = 300.0
    for ts in range(0, 86400, 10):
        hour = ts / 3600
        sun = max(0.0, 2500 * (1 - abs(hour - 13) / 6)) * (0.3 if rng.random() < 0.15 else 1)
        if rng.random() < 0.02:
            level = rng.choice([250.0, 400.0, 2200.0])  # heat pump / appliance bursts
        if rng.random() < 0.01:
            continue  # dropped readings
        readings.append((ts, round(level - sun + rng.uniform(-50, 50))))
    for ts in range(0, 86400, 900):
        temps.append((ts, round(20.8 + 1.2 * max(0.0, 1 - abs(ts / 3600 - 15) / 5), 1)))
    params = dict(start=0, end=86400, interval=5, ideal=21.0, S=300.0, L=200.0, H=600.0,
                  step=0.5, minBoost=20.0, maxBoost=24.5)

    expected = _python_replay(readings, temps, params)
    to_ms = lambda series: [[ts * 1000, v] for ts, v in series]  # noqa: E731
    js = _run_node(
        "const p = input.p; const r = card.replay(input.readings, input.temps, p);"
        "console.log(JSON.stringify(r.ticks.map((k) => [k.sp, k.rule, k.active])));",
        {
            "readings": to_ms(readings),
            "temps": to_ms(temps),
            "p": {**params, "start": 0, "end": 86400 * 1000},
        },
    )
    assert len(js) == len(expected)
    rules = {row[1] for row in expected}
    assert {"solar_incremental", "solar_reset"} <= rules, "fixture should exercise boost and reset"
    for i, (got, want) in enumerate(zip(js, expected)):
        assert got[1:] == want[1:], f"tick {i}: {got} != {want}"
        assert got[0] == pytest.approx(want[0]), f"tick {i}"
