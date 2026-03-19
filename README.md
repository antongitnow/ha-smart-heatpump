# Smart Heatpump Controller for Home Assistant

An intelligent AppDaemon controller that optimises heat pump operation based on real-time COP (Coefficient of Performance), solar surplus, and weather forecast. It controls your heat pump by adjusting the thermostat setpoint — no direct heat pump API required. All logic runs locally in AppDaemon with no cloud dependency.

---

## What it does

The Smart Heatpump Controller watches your outdoor temperature, solar electricity production, and weather forecast, then automatically adjusts your thermostat setpoint to minimise grid energy use for heating. It pre-heats your home while your heat pump is most efficient, stores free solar energy as warmth in your floor, and holds back heating when COP is poor. Your indoor temperature stays within a comfort band you define, without any manual intervention.

---

## How it works

### COP-aware control

A heat pump's efficiency (COP) depends strongly on outdoor temperature — roughly 4× more efficient at 10°C than at -5°C. Most thermostats ignore this. The Smart Heatpump Controller checks the weather forecast up to 24 hours ahead and pre-heats your home while COP is still favourable, before a cold period arrives. Conversely, when outdoor temperature is already low (poor COP), it reduces the setpoint to the configured minimum and coasts on stored thermal energy in your floor slab — potentially waiting for the next efficient window before heating again.

### Solar surplus boost

When your solar panels generate more electricity than your household uses, the excess is exported to the grid. The controller detects this from your P1 smart meter (negative net power = export) and raises the thermostat setpoint to store that free energy as heat in your floor. A configurable confirmation delay (default: 10 minutes) prevents reacting to brief cloud-passing spikes. If you have Forecast.Solar installed, the controller can also pre-heat *before* the surplus starts, based on predicted solar yield for the next hour.

### Floor heating thermal lag

Floor heating systems respond slowly — typically 2–4 hours between a setpoint change and the room reaching the new temperature. The controller accounts for this by extending its forecast look-ahead window by a configurable `thermal_lag_hours` value (default: 3 hours). This means it starts pre-heating the floor slab hours before a cold period is forecast to arrive.

---

## Prerequisites

- [ ] **Home Assistant** — any recent version (2023.x or later recommended)
- [ ] **AppDaemon add-on** — available in the HA Add-on Store
- [ ] **HACS** (optional, for Forecast.Solar) — Home Assistant Community Store
- [ ] **A thermostat with a `climate` entity** — any thermostat that supports `climate.set_temperature` (e.g. Honeywell, Nest, ecobee, generic Z-Wave/Zigbee)
- [ ] **A smart meter / P1 sensor** — any sensor providing net grid power in Watts (positive = import, negative = export). HomeWizard P1, DSMR Reader, Shelly EM, etc.
- [ ] **Met.no integration** — built into Home Assistant, no API key needed. Provides hourly weather forecast.

---

## Installation

### Step 1 — Install the AppDaemon add-on

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Search for **AppDaemon** and install it.
3. Start the add-on and enable **Start on boot**.

### Step 2 — Install via HACS (recommended)

1. Open HACS in Home Assistant.
2. Click the **⋮** menu (top right) → **Custom repositories**.
3. Paste `https://github.com/antongitnow/ha-smart-heatpump` and select category **AppDaemon**.
4. Click **Add**, then find **Smart Heatpump Controller** in the HACS store and click **Download**.

HACS will place the files in `/config/appdaemon/apps/smart_heatpump/` automatically.

<details>
<summary><strong>Manual installation (alternative)</strong></summary>

Copy the contents of `apps/smart_heatpump/` to your AppDaemon apps directory:

```bash
mkdir -p /config/appdaemon/apps/smart_heatpump
cp smart_heatpump.py /config/appdaemon/apps/smart_heatpump/
cp smart_heatpump.yaml /config/appdaemon/apps/smart_heatpump/
```

</details>

### Step 3 — Edit `smart_heatpump.yaml` with your entity names

Open `/config/appdaemon/apps/smart_heatpump/smart_heatpump.yaml` and replace the PLACEHOLDER values:

```yaml
smart_heatpump:
  module: smart_heatpump
  class: SmartHeatpump

  thermostat_entity: "climate.YOUR_THERMOSTAT"       # <-- replace
  p1_net_power_entity: "sensor.YOUR_POWER_SENSOR"    # <-- replace

  weather_entity: "weather.home"                     # change if needed
  forecast_solar_entity: null                        # see Forecast.Solar section
```

### Step 4 — How to find your entity names

1. Open Home Assistant → **Developer Tools → States**.
2. In the search box, type part of the entity name (e.g. `climate` for your thermostat).
3. Copy the `entity_id` exactly as shown (e.g. `climate.living_room`).
4. Repeat for your power sensor (e.g. search `power` or `p1`).

### Step 5 — Add HA helper entities via packages

The controller reads its configuration from `input_number` helpers in HA. Add them using the packages mechanism:

1. Open `/config/configuration.yaml` and add (or extend):

```yaml
homeassistant:
  packages: !include_dir_named packages
```

2. Create the directory `/config/packages/` if it doesn't exist.

3. Copy `config/packages/smart_heatpump.yaml` from this repository to `/config/packages/smart_heatpump.yaml`.

### Step 6 — Restart Home Assistant

Restart HA to create the helper entities. Then restart AppDaemon (or the AppDaemon add-on) to load the controller.

### Step 7 — Add the Lovelace dashboard card

1. Open your Home Assistant dashboard → **Edit** (pencil icon).
2. Click **+ Add Card → Manual** (scroll to the bottom of the card picker).
3. Open `lovelace/dashboard_card.yaml` from this repository.
4. Paste the YAML content into the card editor.
5. Update the `climate.PLACEHOLDER_THERMOSTAT` reference to your actual thermostat entity.
6. Click **Save**.

---

## Forecast.Solar setup (optional but recommended)

Forecast.Solar provides per-hour solar production predictions based on your panel configuration and local weather. When configured, the controller can pre-heat *before* solar surplus starts (predictive boost).

1. In HACS, search for **Forecast.Solar** and install it.
2. Go to **Settings → Devices & Services → Add Integration → Forecast.Solar**.
3. Enter your panel details (azimuth, tilt, peak power in kWp).
4. After setup, find the entity name: **Developer Tools → States**, search `forecast_solar` or `energy_production`.
   - Typical entity: `sensor.energy_production_next_hour`
5. Edit `smart_heatpump.yaml` and set:

```yaml
forecast_solar_entity: "sensor.energy_production_next_hour"
```

6. Restart AppDaemon.

---

## Configuration reference

All parameters are adjustable from the Lovelace dashboard without restarting anything.

| Entity | Default | Range | Unit | Description |
|---|---|---|---|---|
| `input_number.shp_temp_ideal` | 21.0 | 16–26 | °C | Default comfort setpoint |
| `input_number.shp_temp_minimum` | 20.5 | 14–24 | °C | Hard floor — setpoint never goes below this |
| `input_number.shp_temp_solar_boost` | 22.5 | 18–26 | °C | Setpoint during solar surplus |
| `input_number.shp_preheat_delta` | 0.5 | 0–2.0 | °C | Extra °C above ideal when pre-heating |
| `input_number.shp_cop_threshold_temp` | 5.0 | -10–15 | °C | Outdoor temp at/below which COP is considered poor |
| `input_number.shp_cop_recovery_horizon_hours` | 6 | 1–24 | h | Hours ahead to look for COP recovery |
| `input_number.shp_solar_surplus_threshold` | 500 | 0–5000 | W | Minimum export to trigger solar boost |
| `input_number.shp_solar_confirm_minutes` | 10 | 0–60 | min | Export must be sustained this long before boost |
| `input_number.shp_forecast_horizon_hours` | 24 | 1–48 | h | How far ahead to check for cold periods |
| `input_number.shp_thermal_lag_hours` | 3 | 0–6 | h | Floor heating warm-up lag |
| `input_number.shp_evaluation_interval_min` | 15 | 5–60 | min | Controller re-evaluation frequency |

---

## Observability

### Logs

AppDaemon logs appear in the AppDaemon add-on log panel, or in `/config/appdaemon/appdaemon.log`.

- **INFO** — logged on every setpoint change, with rule name, old/new setpoint, outdoor temp, solar surplus, and minimum forecast temperature.
- **DEBUG** — logged every cycle when no change is made, with full sensor values.
- **WARNING** — logged when a sensor is unavailable (controller continues with fallback behaviour).
- **ERROR** — logged on thermostat write failures or unhandled exceptions.

### Active rule values

The `input_text.shp_active_rule` entity shows the current decision rule:

| Value | Meaning |
|---|---|
| `solar_boost` | Confirmed solar export — setpoint raised to solar boost temperature |
| `solar_predicted` | Predicted solar export (Forecast.Solar) — pre-heating before surplus starts |
| `preheat` | Cold period coming within forecast horizon, COP still good — pre-heating now |
| `conserve` | COP poor, no recovery expected — holding minimum setpoint |
| `conserve_await_recovery` | COP poor, but forecast shows COP recovery within the recovery horizon — waiting for efficient window |
| `default` | Normal operation — maintaining ideal setpoint |
| `error_fallback` | Unhandled exception — thermostat set to ideal temperature as safe fallback |
| `initialising` | Controller has just started, first evaluation not yet complete |

---

## Troubleshooting

### Entity not found / "State is unavailable"

- Check your entity names in `smart_heatpump.yaml` match exactly what appears in Developer Tools → States.
- Verify the integration providing the entity is working (check its own integration page for errors).

### AppDaemon not starting

- Check the AppDaemon add-on log for Python errors.
- Verify `smart_heatpump.py` and `smart_heatpump.yaml` are in the same directory under `appdaemon/apps/`.
- Ensure `smart_heatpump.yaml` has valid YAML syntax (no tabs, correct indentation).

### Setpoint not changing

- Check `input_text.shp_active_rule` — the rule should be updating every evaluation cycle.
- If `error_fallback` is shown, check the AppDaemon ERROR log for the exception details.
- Confirm AppDaemon has write access to the thermostat entity: test manually in Developer Tools → Services → `climate.set_temperature`.
- Check that `thermostat_entity` in `smart_heatpump.yaml` matches the climate entity exactly.

### Solar boost not triggering

- Verify `p1_net_power_entity` returns negative values when you are exporting. Check in Developer Tools → States.
- The export must exceed `shp_solar_surplus_threshold` (default 500 W) for `shp_solar_confirm_minutes` (default 10 min) continuously.
- If using Forecast.Solar, verify the entity name and that its state is a number (not `unavailable`).

### Pre-heat not triggering

- The outdoor temperature must be *above* the COP threshold for pre-heat to activate.
- The forecast minimum within the effective horizon (`forecast_horizon_hours + thermal_lag_hours`) must drop below the COP threshold.
- Verify `weather.home` has a non-empty `forecast` attribute: Developer Tools → Template → `{{ state_attr('weather.home', 'forecast') }}`.

---

## Roadmap

**v2 candidates:**

- **ML thermal learning** — learn the actual thermal cool-down rate of the building and adjust `thermal_lag_hours` automatically based on weeks of historical data.
- **Dynamic electricity tariff optimisation** — integrate with a real-time electricity price sensor (e.g. ENTSO-E, Tibber) to shift heating to low-price windows.
- **Multi-zone support** — control multiple thermostats with per-zone configuration.
- **Occupancy / presence setback** — reduce setpoint to minimum when no one is home, using phone presence or PIR sensors.
- **Push notifications** — alert the user when a rule change occurs, optionally with energy savings estimate.

---

## Contributing

Pull requests and issues are welcome. Please open an issue first to discuss significant changes.

When contributing code:
- Keep `decide()` a pure function with no AppDaemon dependency.
- Add a test case in `tests/test_decision_logic.py` for any new decision logic.
- Run `pytest tests/` and `ruff check .` before submitting.

---

## License

MIT — see [LICENSE](LICENSE).
