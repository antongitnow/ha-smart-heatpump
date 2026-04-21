# PRD: Smart Heatpump Controller for Home Assistant

**Project:** ha-smart-heatpump  
**Status:** v1.0 — Ready for implementation  
**License:** MIT  
**Date:** 2026-03-19  
**Target:** Home Assistant custom integration

---

## Table of Contents

1. Problem Statement
2. Goals & Success Criteria
3. User & Hardware Requirements
4. Functional Requirements
5. Control Logic & Algorithm
6. Configuration Parameters
7. Technical Architecture
8. File Structure & File Specs
9. Entity Contract
10. Error Handling & Fallback Matrix
11. Test Scenarios
12. Non-Functional Requirements
13. Out of Scope
14. Open Questions
15. Glossary

---

## 1. Problem Statement

Modern heat pumps are significantly more efficient at higher outdoor temperatures (COP ~4.0 at 10°C vs ~1.8 at -5°C), yet most thermostats control them reactively — only responding to current indoor temperature. This wastes energy in two ways:

1. **No COP awareness:** The heat pump runs at low efficiency during cold periods when it could have pre-heated the home earlier at better COP.
2. **No solar surplus awareness:** Available solar energy goes unexploited for heating when grid export could instead be stored as thermal energy in the home.

Most heat pumps have no API. Control must happen via a smart thermostat acting as intermediary — the thermostat is the only actuator.

Floor heating compounds the challenge: with a 2–4 hour thermal response time, reactive control is insufficient. The system must anticipate temperature needs hours in advance.

This project builds an intelligent thermostat controller for Home Assistant that optimizes heat pump operation based on real-time COP, solar surplus, and weather forecast, while respecting user-defined comfort boundaries. All logic runs locally in AppDaemon — no cloud dependency.

---

## 2. Goals & Success Criteria

### Primary Goals
- Maintain indoor temperature within user-defined comfort band at all times
- Minimize grid energy consumption for heating by optimizing timing against COP and solar availability
- Pre-heat the home ahead of predicted cold periods while COP is still favorable
- Exploit solar surplus by storing thermal energy in the building's thermal mass (floor heating)
- Be installable by a non-developer HA user via HACS

### Success Criteria

| Metric | Target |
|---|---|
| Indoor temp within comfort band | ≥ 95% of occupied hours |
| Solar surplus used for heating | Measurable increase vs. thermostat-only baseline |
| Heating during low-COP periods | Reduced vs. baseline |
| User configuration | No YAML editing required after install |
| Resilience | Degrades gracefully to `temp_ideal` on any sensor failure |
| Observability | Every setpoint change logged with rule + sensor values |

### Non-Goals (v1)
- Replacing thermostat hardware
- Direct heat pump API integration
- HVAC cooling control
- Multi-zone support
- Occupancy / presence detection
- Dynamic electricity price optimization

---

## 3. User & Hardware Requirements

### Reference Installation (author)

| Component | Details |
|---|---|
| Heat pump | No API — controlled via thermostat setpoint only |
| Thermostat | Honeywell — native HA integration, exposes `climate` entity |
| Energy meter | HomeWizard P1 — single net power sensor (W), positive = import, negative = export |
| Heating system | **Floor heating** — thermal response time 2–4 hours |
| HA instance | Home Assistant OS or Supervised, with Add-on support |
| Weather source | Met.no (built-in HA integration, no API key needed) |
| Solar forecast | Forecast.Solar (HACS, optional but recommended) |

### Minimum Requirements for Any User

- Home Assistant (any recent version with AppDaemon add-on support)
- Any thermostat with a controllable `climate` entity (`climate.set_temperature` service)
- Any energy sensor providing grid net power in Watts (positive = import, negative = export)
- Met.no or compatible weather integration providing hourly forecast with `temperature` per entry

### Optional / Enhancing

- Forecast.Solar integration — provides predicted solar yield per hour, used to boost proactively before surplus starts
- Separate indoor temperature sensor (if thermostat sensor is inaccurate)

---

## 4. Functional Requirements

### FR-01: Comfort Band Management

- The system MUST maintain indoor temperature at or above `temp_minimum` at all times
- The system MUST target `temp_ideal` as the default setpoint
- There is NO hard upper limit — the system MAY set setpoint above `temp_ideal` when solar surplus or pre-heating conditions justify it
- All temperature bounds MUST be configurable via HA UI (`input_number` helpers) without editing any file

### FR-02: Solar Surplus Heating (Reactive)

- When real-time grid export exceeds `solar_surplus_threshold` (W) continuously for `solar_confirm_minutes`, the system MUST raise the setpoint to `temp_solar_boost`
- Confirmation delay prevents reacting to brief export spikes (e.g. clouds passing)
- When surplus drops below threshold, the system MUST return setpoint to `temp_ideal` (or whichever other rule applies)
- Solar surplus is derived from the P1 net power sensor: `surplus_w = max(0, -p1_net_power_w)`

### FR-03: Solar Surplus Heating (Predictive, optional)

- If a Forecast.Solar entity is configured, the system SHOULD also trigger solar boost when predicted yield for the next hour exceeds `solar_surplus_threshold`
- This allows pre-heating before actual export starts (e.g. morning ramp-up)
- Predictive solar boost uses the same `temp_solar_boost` target and does NOT require a confirmation delay
- If Forecast.Solar entity is not configured or unavailable, this rule is silently skipped

### FR-04: COP-Aware Pre-Heating

- Every evaluation cycle, the system MUST fetch hourly outdoor temperature forecast for the next `effective_horizon_hours` = `forecast_horizon_hours + thermal_lag_hours`
- `forecast_horizon_hours` defaults to **24 hours** and supports up to 48 hours, allowing the system to anticipate cold periods a full day in advance
- If the minimum forecast temperature within this horizon is below `cop_threshold_temp` AND the current outdoor temperature is at or above `cop_threshold_temp`:
  - The system MUST raise the setpoint to `temp_ideal + preheat_delta`
  - Rationale: COP is good now, will be poor within 24h — heat the thermal mass while cheap
- This rule only triggers when current COP is still good (outdoor temp ≥ threshold)

### FR-05: COP Conservation Mode

Conservation mode applies whenever the current outdoor temperature is below `cop_threshold_temp` and no solar rule (FR-02/FR-03) is active. Result is always setpoint = `temp_minimum`. Two sub-cases determine the rule name shown in the dashboard:

**Case A — COP poor, no recovery expected soon (`conserve`):**
- Outdoor temp < threshold
- Forecast does NOT show outdoor temp rising above threshold within `cop_recovery_horizon_hours`
- Rationale: COP is poor and will stay poor — minimize energy, coast on stored thermal mass

**Case B — COP poor now, but recovery forecast soon (`conserve_await_recovery`):**
- Outdoor temp < threshold
- Forecast DOES show outdoor temp rising above threshold within `cop_recovery_horizon_hours`
- Rationale: COP will improve soon — deliberately withhold heating now and wait for the efficient window, then FR-04 or FR-06 will take over at better COP
- This is the deliberate mirror of FR-04: "don't heat at poor COP when good COP is coming"

Both cases set `temp_minimum`. The distinction is observability only — the user can see in the dashboard whether the system is waiting for a recovery window or simply conserving indefinitely.

**New config parameter:** `cop_recovery_horizon_hours` — hours ahead to look for COP recovery. Default: 6. Range: 1–24.

### FR-06: Default Mode

- When none of FR-02 through FR-05 conditions are active:
  - The system MUST set the setpoint to `temp_ideal`

### FR-07: Setpoint Priority (strict ordering, highest wins)

```
Priority 1 (highest): Solar surplus confirmed (FR-02) OR solar predicted (FR-03)
Priority 2:           COP pre-heat — cold coming within horizon, COP still good now (FR-04)
Priority 3:           COP conservation — COP poor now (FR-05)
                        Sub-rule A: conserve         — no recovery expected
                        Sub-rule B: conserve_await_recovery — recovery coming, wait for it
Priority 4 (default): Maintain ideal temperature (FR-06)
```

Solar (P1) always wins over COP-based rules. FR-04 and FR-05 are mutually exclusive by definition: FR-04 requires `outdoor >= threshold`, FR-05 requires `outdoor < threshold`.

### FR-08: Safety Floor

- The setpoint sent to the thermostat MUST NEVER be lower than `temp_minimum`, regardless of any rule
- Enforced as a hard clamp in code, after all rule logic completes

### FR-09: Configurable Evaluation Interval

- The controller MUST re-evaluate all rules every `evaluation_interval_minutes`
- Default: 15 minutes. Range: 5–60 minutes
- The interval value MUST be re-read from the `input_number` entity on every cycle (allows live changes without AppDaemon restart)

### FR-10: Logging & Observability

- Every evaluation cycle MUST produce a DEBUG log entry with: rule triggered, current and new setpoint, outdoor temp, net power, solar surplus, min forecast temp
- Every setpoint **change** MUST produce an INFO log entry
- The active rule name MUST be written to `input_text.shp_active_rule` after every evaluation so it is visible in the HA dashboard

### FR-11: Portability

- All entity names (thermostat, P1 sensor, weather, Forecast.Solar) MUST be configurable in `smart_heatpump.yaml`
- No entity names may be hardcoded in Python

### FR-12: Graceful Degradation

See Section 10 for full error handling matrix. On any unhandled exception: log ERROR, fall back to `temp_ideal`, write `error_fallback` to rule entity.

---

## 5. Control Logic & Algorithm

### 5.1 Decision Cycle (pseudo-code)

Runs every `evaluation_interval_minutes`:

```python
# --- Inputs ---
p1_net_power_w    = read_sensor(p1_net_power_entity)   # W, positive=import, negative=export
outdoor_temp_c    = read_weather_current(weather_entity)
forecast_temps    = read_forecast(weather_entity,
                     hours=forecast_horizon_hours + thermal_lag_hours)
forecast_recovery = read_forecast(weather_entity,
                     hours=cop_recovery_horizon_hours)  # separate shorter window
forecast_solar_w  = read_forecast_solar()               # Wh next hour ≈ avg W, optional

# --- Derived ---
solar_surplus_w   = max(0.0, -p1_net_power_w)
min_forecast_temp = min(forecast_temps) if forecast_temps else None
max_recovery_temp = max(forecast_recovery) if forecast_recovery else None

# --- Solar confirmation tracking (stateful) ---
if solar_surplus_w >= solar_surplus_threshold:
    if solar_surplus_since is None:
        solar_surplus_since = now()
    solar_confirmed = (now() - solar_surplus_since).minutes >= solar_confirm_minutes
else:
    solar_surplus_since = None
    solar_confirmed = False

solar_predicted = (forecast_solar_w is not None
                   and forecast_solar_w >= solar_surplus_threshold)

# --- Decision (priority order) ---
if solar_confirmed or solar_predicted:
    target = temp_solar_boost
    rule   = "solar_boost" if solar_confirmed else "solar_predicted"

elif (min_forecast_temp is not None
      and min_forecast_temp < cop_threshold_temp
      and outdoor_temp_c >= cop_threshold_temp):
    target = temp_ideal + preheat_delta
    rule   = "preheat"

elif outdoor_temp_c < cop_threshold_temp:
    target = temp_minimum
    # Sub-rule: is COP recovery coming within cop_recovery_horizon_hours?
    if (max_recovery_temp is not None
            and max_recovery_temp >= cop_threshold_temp):
        rule = "conserve_await_recovery"  # COP improving soon — wait for it
    else:
        rule = "conserve"                 # COP poor, no recovery expected

else:
    target = temp_ideal
    rule   = "default"

# --- Safety floor ---
target = max(target, temp_minimum)

# --- Apply ---
if target != current_setpoint:
    set_thermostat(target)
    log_info(rule, target, outdoor_temp_c, solar_surplus_w, min_forecast_temp)

write_state(input_text.shp_active_rule, rule)
log_debug(rule, target, outdoor_temp_c, solar_surplus_w, min_forecast_temp, max_recovery_temp)
```

### 5.2 COP Reference Table

| Outdoor Temp | Approx COP | Heating Cost Relative | Strategy |
|---|---|---|---|
| > 10°C | ~4.0 | 1.0× (cheapest) | Heat freely, use floor as thermal buffer |
| 5–10°C | ~3.0–3.5 | 1.1–1.3× | Heat normally |
| 0–5°C | ~2.5 | 1.6× | Moderate — pre-heat if cold ahead |
| -5–0°C | ~1.8–2.2 | 1.8–2.2× | Poor — minimize, coast on stored heat |
| < -5°C | ~1.5 | 2.7× | Very poor — hold minimum only |

### 5.3 Floor Heating Thermal Lag

Floor heating response time: 2–4 hours (default: 3 hours, configurable as `thermal_lag_hours`).

The effective forecast horizon is extended by `thermal_lag_hours`:

```
effective_horizon = forecast_horizon_hours + thermal_lag_hours

Example:
  forecast_horizon_hours = 24
  thermal_lag_hours      = 3
  effective_horizon      = 27 hours

If temperature drops below threshold anywhere in the next 27 hours,
pre-heating starts NOW so the floor is warm before the cold arrives.
This allows the system to anticipate a cold night starting tomorrow
morning while it is still mild today.
```

### 5.4 Solar Surplus Confirmation Logic

A confirmation delay prevents reacting to transient export spikes:

```
t=0 min:  Export 600W → start timer (solar_surplus_since = now)
t=5 min:  Export 300W → reset timer (below threshold)
t=0 min:  Export 700W → start timer again
t=10 min: Export 650W → elapsed >= solar_confirm_minutes → BOOST triggered
```

---

## 6. Configuration Parameters

All parameters exposed as `input_number` helpers. Entity names use prefix `shp_`.

| Entity | Default | Min | Max | Step | Unit | Description |
|---|---|---|---|---|---|---|
| `input_number.shp_temp_ideal` | 21.0 | 16 | 26 | 0.5 | °C | Default comfort setpoint |
| `input_number.shp_temp_minimum` | 20.5 | 14 | 24 | 0.5 | °C | Hard floor — never go below this |
| `input_number.shp_temp_solar_boost` | 22.5 | 18 | 26 | 0.5 | °C | Setpoint during solar surplus |
| `input_number.shp_preheat_delta` | 0.5 | 0 | 2.0 | 0.5 | °C | Extra °C above ideal when pre-heating |
| `input_number.shp_cop_threshold_temp` | 5.0 | -10 | 15 | 1.0 | °C | Outdoor temp below which COP is considered poor |
| `input_number.shp_cop_recovery_horizon_hours` | 6 | 1 | 24 | 1 | h | How far ahead to look for COP recovery (outdoor rising above threshold) |
| `input_number.shp_solar_surplus_threshold` | 500 | 0 | 5000 | 100 | W | Min export (W) to trigger solar boost |
| `input_number.shp_solar_confirm_minutes` | 10 | 0 | 60 | 5 | min | Surplus must persist this long before boost |
| `input_number.shp_forecast_horizon_hours` | 24 | 1 | 48 | 1 | h | How far ahead to check for cold periods |
| `input_number.shp_thermal_lag_hours` | 3 | 0 | 6 | 0.5 | h | Floor heating warm-up lag |
| `input_number.shp_evaluation_interval_min` | 15 | 5 | 60 | 5 | min | Controller re-evaluation frequency |

Plus one `input_text` helper for observability:

| Entity | Description |
|---|---|
| `input_text.shp_active_rule` | Displays the currently active decision rule in the dashboard |

---

## 7. Technical Architecture

### 7.1 Implementation Choice: AppDaemon

AppDaemon chosen over YAML automations and Node-RED because:
- Multi-variable decision logic is readable and maintainable in Python
- Stateful tracking (`solar_surplus_since` timer) is natural in Python, awkward in YAML
- Thermal learning (v2) requires stateful computation over days/weeks
- Unit-testable outside of HA with standard `pytest`
- Active HA community support and documented patterns

### 7.2 Component Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        Home Assistant                             │
│                                                                   │
│  ┌──────────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ HomeWizard P1    │  │  Met.no      │  │  Forecast.Solar  │  │
│  │ Integration      │  │  Integration │  │  (HACS, opt.)    │  │
│  │                  │  │              │  │                  │  │
│  │ sensor.*_net_    │  │ weather.home │  │ sensor.*_next_   │  │
│  │ power            │  │ + forecast   │  │ hour             │  │
│  └────────┬─────────┘  └──────┬───────┘  └────────┬─────────┘  │
│           └───────────────────┴───────────────────┘             │
│                               │                                  │
│                               ▼                                  │
│           ┌────────────────────────────────────────┐            │
│           │           AppDaemon Add-on              │            │
│           │                                         │            │
│           │   apps/smart_heatpump/                  │            │
│           │   ├── smart_heatpump.py  (controller)   │            │
│           │   └── smart_heatpump.yaml (app config)  │            │
│           │                                         │            │
│           │   Reads:  input_number.shp_*            │            │
│           │   Writes: climate setpoint               │            │
│           │           input_text.shp_active_rule     │            │
│           └──────────────────┬─────────────────────┘            │
│                              │                                   │
│              ┌───────────────┴──────────────┐                   │
│              ▼                              ▼                    │
│  ┌───────────────────────┐  ┌──────────────────────────────┐   │
│  │  Thermostat (climate) │  │  Lovelace Dashboard Card     │   │
│  │  → Heat Pump          │  │  input_number sliders        │   │
│  └───────────────────────┘  │  input_text active rule      │   │
│                              │  current setpoint display    │   │
│                              └──────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 8. File Structure & File Specs

### 8.1 Repository Structure

```
ha-smart-heatpump/
│
├── README.md
├── LICENSE                                    # MIT
├── VERSION                                    # Contains: 1.0.0
├── hacs.json
├── info.md                                    # Short HACS store description
│
├── appdaemon/
│   └── apps/
│       └── smart_heatpump/
│           ├── smart_heatpump.py              # Main AppDaemon controller
│           └── smart_heatpump.yaml           # App instance config (user edits)
│
├── config/
│   └── packages/
│       └── smart_heatpump.yaml               # input_number + input_text helpers
│                                              # Include via HA packages mechanism
│
├── lovelace/
│   └── dashboard_card.yaml                   # Ready-to-paste Lovelace card
│
└── tests/
    ├── test_decision_logic.py                 # pytest unit tests, no HA needed
    └── fixtures/
        └── forecast_sample.json              # Sample Met.no forecast payload
```

### 8.2 File Specs

#### `appdaemon/apps/smart_heatpump/smart_heatpump.py`

- Python 3.10+, type hints throughout
- Class `SmartHeatpump(hass.Hass)`
- All entity names read from `self.args`, never hardcoded
- All config values read from `input_number` entities via `self.get_state()` every cycle
- Evaluation loop via `self.run_every()`, initial interval from config (default 15 min)
- Instance variable `self._solar_surplus_since: datetime | None = None`
- Active rule written to `input_text.shp_active_rule` every cycle
- Decision logic extracted as a standalone pure function `decide(...)` importable without AppDaemon (enables unit testing)
- Log levels: INFO on setpoint change, DEBUG on no-change, WARNING on sensor failure, ERROR on exception
- On unhandled exception in `_run_evaluation`: log ERROR, set setpoint to `temp_ideal`, write `error_fallback` to rule entity

#### `appdaemon/apps/smart_heatpump/smart_heatpump.yaml`

- AppDaemon app registration
- All user-editable fields with PLACEHOLDER values clearly named
- Extensively commented
- Example:
```yaml
smart_heatpump:
  module: smart_heatpump
  class: SmartHeatpump
  # REQUIRED: replace with your actual entity names
  thermostat_entity:    "climate.PLACEHOLDER_THERMOSTAT"
  p1_net_power_entity:  "sensor.PLACEHOLDER_P1_NET_POWER"
  # OPTIONAL
  weather_entity:       "weather.home"
  forecast_solar_entity: null
```

#### `config/packages/smart_heatpump.yaml`

- All 10 `input_number` helpers as defined in Section 6
- `input_text.shp_active_rule` helper
- User adds `packages: !include_dir_named packages/` to `configuration.yaml`
- File must be self-contained and not conflict with typical HA setups

#### `lovelace/dashboard_card.yaml`

Entities card containing:
- Current thermostat setpoint (state of climate entity)
- Active rule (`input_text.shp_active_rule`)
- Current outdoor temperature (from `weather.home`)
- All 10 `input_number` sliders with labels
- Instructions comment at top: how to add to dashboard

#### `README.md`

Must contain all of the following sections, in order:

1. What it does (3–4 sentences, non-technical)
2. How it works (COP explanation, solar boost, pre-heat — 1 paragraph each)
3. Prerequisites checklist (HA version, AppDaemon, HACS, required integrations)
4. Installation — step by step:
   a. Install AppDaemon add-on
   b. Copy app files to AppDaemon apps directory
   c. Edit `smart_heatpump.yaml` with entity names
   d. How to find entity names (Developer Tools → States)
   e. Add `config/packages/smart_heatpump.yaml` via packages
   f. Restart HA
   g. Add Lovelace card
5. Forecast.Solar setup (separate subsection — install via HACS, configure, find entity name)
6. Configuration reference (table from Section 6)
7. Observability — how to read logs, what the active rule values mean
8. Troubleshooting — at minimum: entity not found, AppDaemon not starting, setpoint not changing
9. Roadmap (v2 ideas: ML thermal learning, multi-zone, tariff optimization)
10. Contributing
11. License (MIT)

#### `hacs.json`

```json
{
  "name": "Smart Heatpump Controller",
  "content_in_root": false,
  "appdaemon": true,
  "render_readme": true
}
```

#### `tests/test_decision_logic.py`

- Uses `pytest`
- Imports `decide()` function directly from `smart_heatpump.py` (no AppDaemon)
- All 12 test cases from Section 11 must be implemented
- No mocking of HA or AppDaemon — `decide()` must be pure Python

---

## 9. Entity Contract

### 9.1 Entities the App Reads

| Entity | Type | Expected value | Notes |
|---|---|---|---|
| `climate.PLACEHOLDER_THERMOSTAT` | climate | `temperature` attribute = float °C | Any HA climate entity |
| `sensor.PLACEHOLDER_P1_NET_POWER` | sensor | float, Watts | Positive = importing, negative = exporting |
| `weather.home` | weather | `temperature` attribute = float °C | Current outdoor temp |
| `weather.home` → `forecast` | list of dicts | `datetime` (ISO8601 str) + `temperature` (float) | Met.no hourly entries |
| `sensor.forecast_solar_*_next_hour` | sensor | float, Wh | Optional. Treat Wh ≈ avg W over 1h window |
| `input_number.shp_*` (×10) | input_number | float | All config parameters |

### 9.2 Entities the App Writes

| Entity | Written value | When |
|---|---|---|
| `climate.PLACEHOLDER_THERMOSTAT` | float °C via `climate/set_temperature` | When computed target ≠ current setpoint |
| `input_text.shp_active_rule` | string: `solar_boost`, `solar_predicted`, `preheat`, `conserve`, `conserve_await_recovery`, `default`, or `error_fallback` | Every evaluation cycle |

### 9.3 Notes on Met.no Forecast Format

Met.no exposes forecast as a list on the `weather` entity's `forecast` attribute. Each entry contains:
- `datetime`: ISO 8601 string (UTC)
- `temperature`: float in °C (temperature at that hour)
- `templow` may also be present on daily entries — use `temperature` for hourly entries

Met.no provides forecast data for up to **48 hours ahead** in hourly resolution, which fully supports the 24h default horizon plus thermal lag. The code must parse `datetime` strings and compare against `now() + effective_horizon_hours` to select only relevant entries.

**Important:** If `effective_horizon_hours` exceeds the number of forecast entries available (e.g. forecast only covers 24h but effective horizon is 27h), the system MUST use whatever entries are available rather than failing. Log a DEBUG message when the requested horizon exceeds available forecast data.

### 9.4 Forecast.Solar Entity Note

`sensor.forecast_solar_energy_production_next_hour` reports **Wh** (watt-hours for the coming hour). Since the window is exactly 1 hour, this is numerically equivalent to average watts. Code must document this assumption.

---

## 10. Error Handling & Fallback Matrix

| Failure | Detection | Fallback | Log Level |
|---|---|---|---|
| P1 sensor unavailable | `get_state()` non-numeric or `unavailable` | Disable FR-02 and FR-03. Continue forecast + COP rules. | WARNING |
| Weather entity unavailable | Temperature attribute non-numeric | Disable FR-04 and FR-05. Fall back to `temp_ideal`. | WARNING |
| Forecast attribute empty or missing | `forecast` is None or empty list | Disable FR-04. Continue with real-time outdoor temp for FR-05. | WARNING |
| Forecast.Solar unavailable | Entity missing, state non-numeric | Disable FR-03. Continue with reactive solar FR-02 only. | DEBUG (expected if not installed) |
| `input_number` entity unavailable | `get_state()` fails or non-numeric | Use hardcoded default for that parameter. Log which default was used. | WARNING |
| Thermostat set_temperature fails | `call_service` raises exception | Log error. Skip this cycle. Do not retry. | ERROR |
| Any unhandled Python exception | `try/except Exception` wrapping `_run_evaluation` | Set setpoint to `temp_ideal`. Write `error_fallback` to rule entity. | ERROR |
| AppDaemon restart / HA restart | App re-initializes | `_solar_surplus_since` resets to None — confirmation timer restarts. Acceptable. | INFO |

---

## 11. Test Scenarios

All tests in `tests/test_decision_logic.py` using `pytest`.  
The `decide()` function signature:

```python
def decide(
    outdoor_temp_c: float | None,
    solar_surplus_w: float | None,
    solar_confirmed: bool,
    forecast_solar_w: float | None,
    forecast_temps: list[float],           # preheat horizon: forecast_horizon + thermal_lag
    forecast_recovery_temps: list[float],  # COP recovery horizon: cop_recovery_horizon_hours
    temp_ideal: float,
    temp_minimum: float,
    temp_solar_boost: float,
    preheat_delta: float,
    cop_threshold_temp: float,
    solar_surplus_threshold: float,
) -> tuple[float, str]:  # (target_temp, rule_name)
```

| # | Scenario | Key inputs | Expected rule | Expected target |
|---|---|---|---|---|
| T01 | Normal, no solar, mild outdoor | outdoor=8, surplus=0, confirmed=False, forecast_min=6, threshold=5 | `default` | 21.0 |
| T02 | Solar surplus confirmed | outdoor=12, surplus=700, confirmed=True | `solar_boost` | 22.5 |
| T03 | Solar surplus present but not yet confirmed | outdoor=12, surplus=700, confirmed=False, no forecast.solar | `default` | 21.0 |
| T04 | Solar predicted via Forecast.Solar | outdoor=8, surplus=0, confirmed=False, forecast_solar=600, threshold=500 | `solar_predicted` | 22.5 |
| T05 | Pre-heat: cold coming within horizon, COP still good now | outdoor=6, forecast_min=2, threshold=5 | `preheat` | 21.5 |
| T06 | COP poor now, no recovery coming | outdoor=2, forecast_max_recovery=1, threshold=5 | `conserve` | 20.5 |
| T07 | COP poor now, recovery coming within horizon | outdoor=2, forecast_max_recovery=7, threshold=5 | `conserve_await_recovery` | 20.5 |
| T08 | Solar wins over conserve_await_recovery | outdoor=2, surplus=700 confirmed, forecast_max_recovery=7, threshold=5 | `solar_boost` | 22.5 |
| T09 | Solar wins over pre-heat simultaneously | outdoor=6, surplus=700 confirmed, forecast_min=2, threshold=5 | `solar_boost` | 22.5 |
| T10 | Safety floor: computed target below minimum | Force any rule that would produce < temp_minimum | any | 20.5 |
| T11 | All sensors unavailable (None inputs) | outdoor=None, forecast=[], surplus=None | `default` (fallback) | 21.0 |
| T12 | Empty forecast list | outdoor=8, forecast=[] | `default` (no forecast) | 21.0 |
| T13 | Boundary: outdoor exactly equals cop_threshold | outdoor=5.0, threshold=5.0 | `conserve` (outdoor not >= threshold) | 20.5 |
| T14 | Recovery boundary: max_recovery exactly equals threshold | outdoor=2, forecast_max_recovery=5.0, threshold=5.0 | `conserve_await_recovery` (>= threshold) | 20.5 |

---

## 12. Non-Functional Requirements

- **Local only:** All logic runs in AppDaemon. No external HTTP calls except via HA integrations.
- **Performance:** Full evaluation cycle completes in under 5 seconds.
- **Safety:** `temp_minimum` is a hard floor enforced in code after all rule logic — not a soft guideline.
- **Resilience:** No single sensor failure may crash the controller or leave the thermostat uncontrolled.
- **Portability:** Zero hardcoded entity names. All names defined in `smart_heatpump.yaml`.
- **Testability:** `decide()` is a pure Python function importable without AppDaemon or HA.
- **Community-ready:** README in English. All code comments and docstrings in English.
- **HACS-compatible:** Valid `hacs.json` with `appdaemon: true`.
- **Versioning:** Both `hacs.json` and `VERSION` file contain `1.0.0`.
- **Code quality:** `ruff` linting should pass with no errors. Type hints on all function signatures.

---

## 13. Out of Scope (v1)

| Feature | Notes |
|---|---|
| Direct heat pump API | No API on reference hardware |
| Cooling / summer mode | Heating only |
| Multi-zone | Single thermostat only |
| Occupancy / presence setback | No PIR or phone presence |
| Dynamic electricity tariff optimization | v2 candidate — needs energy price sensor |
| Push notifications | User can add HA notify actions manually |
| Historical thermal learning (ML) | v2 — needs weeks of collected data first |
| Radiator-specific tuning | Floor heating only in v1 |

---

## 14. Open Questions

| # | Question | Impact | Status |
|---|---|---|---|
| OQ-1 | Exact HomeWizard P1 entity name in author's HA | Needed to remove PLACEHOLDER | Author checks Developer Tools |
| OQ-2 | Exact Honeywell thermostat entity name | Needed to remove PLACEHOLDER | Author checks Developer Tools |
| OQ-3 | Does Met.no hourly forecast use `temperature` or `templow` per entry? | Code uses `temperature` — verify | Check in HA Template editor |
| OQ-4 | Forecast.Solar entity name (varies per install) | Needed for FR-03 | Check after Forecast.Solar install |
| OQ-5 | Actual thermal cool-down rate of the house | Affects accuracy of `thermal_lag_hours` default | Measurable after 2 weeks of v1 running |
| OQ-6 | Re-read `evaluation_interval_minutes` every cycle or only on startup? | **Recommendation: re-read every cycle** — allows live changes without restart | Implement re-read |

---

## 15. Glossary

| Term | Definition |
|---|---|
| COP | Coefficient of Performance. Ratio of heat output to electrical input. COP 4.0 = 4 kWh heat per 1 kWh electricity. Higher outdoor temp = higher COP. |
| Thermal mass | The capacity of a building's materials (floor slab, walls, furniture) to absorb and store heat. Floor heating exploits this as a thermal battery. |
| Thermal lag | Delay between thermostat setpoint change and actual room temperature reaching that target. Floor heating: 2–4 hours. |
| Solar surplus | Solar electricity generated in excess of current household consumption. Exported to the grid. Measured as negative net power on the P1 meter. |
| Setpoint | Target temperature value sent to the thermostat. The thermostat then drives the heat pump to reach this target. |
| P1 meter | Dutch smart meter interface (DSMR standard) providing real-time grid import/export data. HomeWizard P1 is a Wi-Fi dongle exposing this to HA. |
| AppDaemon | Python execution environment running alongside Home Assistant. Provides access to HA state and services via a Python API. |
| HACS | Home Assistant Community Store. Package manager for custom HA integrations, cards, and AppDaemon apps. |
| Forecast.Solar | HACS integration providing hourly solar energy yield predictions based on panel configuration and weather. |
| Pre-heating | Raising the setpoint above normal before a predicted cold period, while COP is still favorable, to store thermal energy in the floor slab. |
| Conservation mode | Lowering the setpoint to `temp_minimum` when outdoor temperature is already low (poor COP). Two variants: `conserve` (no COP recovery expected) and `conserve_await_recovery` (better COP is forecast within `cop_recovery_horizon_hours` — system deliberately waits for the efficient window). |
| cop_recovery_horizon_hours | How many hours ahead the system looks for a COP recovery (outdoor temp rising above threshold) when already in conservation mode. |
| effective_horizon | `forecast_horizon_hours + thermal_lag_hours` — the total hours ahead the system looks when deciding to pre-heat. |

---

*End of PRD v1.0 — Ready for implementation*
