# Smart Heatpump Controller for Home Assistant

An intelligent thermostat controller that optimises heat pump operation based on real-time COP (Coefficient of Performance), solar surplus, and weather forecast. It adjusts your thermostat setpoint automatically — no direct heat pump API required. All logic runs locally with no cloud dependency.

---

## What it does

The controller watches your outdoor temperature, solar production, and weather forecast, then adjusts your thermostat setpoint to minimise grid energy use for heating:

- **Solar boost** — when your solar panels export excess electricity, it raises the setpoint to store that free energy as heat in your floor.
- **COP-aware pre-heating** — it checks the forecast and pre-heats your home while the heat pump is still efficient, before a cold period arrives.
- **Conservation mode** — when COP is poor (cold outside), it reduces heating to the minimum and waits for the next efficient window.
- **Floor heating lag** — it accounts for the 2–4 hour delay of floor heating systems by looking further ahead in the forecast.

Your temperature stays within a comfort band you define, without manual intervention.

---

## Installation

### Step 1 — Add the repository in HACS

1. Open **HACS** in Home Assistant.
2. Click the **three dots** menu (top right) → **Custom repositories**.
3. Paste `https://github.com/antongitnow/ha-smart-heatpump` and select type **Integration**.
4. Click **Add**, then find **Smart Heatpump Controller** and click **Download**.
5. **Restart Home Assistant.**

### Step 2 — Add the integration

1. Go to **Settings → Devices & services**.
2. Click **+ Add integration** (bottom right).
3. Search for **Smart Heatpump Controller**.
4. Select your **thermostat**, **power sensor**, and **weather entity** from the dropdowns.
5. Click **Submit**. Done.

That's it. The controller is running. All parameters have sensible defaults and can be adjusted from the dashboard.

### Step 3 — Add the dashboard card (optional)

1. Open your dashboard → **Edit** (pencil icon) → **+ Add Card** → **Manual**.
2. Paste the contents of [`lovelace/dashboard_card.yaml`](lovelace/dashboard_card.yaml).
3. Click **Save**.

This gives you sliders for all parameters (ideal temperature, COP threshold, solar surplus threshold, etc.) and a status display showing the active rule.

---

## Configuration

All parameters appear as slider entities under the **Smart Heatpump Controller** device in **Settings → Devices & services**. They are also usable on any dashboard card.

| Parameter | Default | Range | Unit | Description |
|---|---|---|---|---|
| Ideal temperature | 21.0 | 16–26 | °C | Default comfort setpoint |
| Minimum temperature | 20.5 | 14–24 | °C | Hard floor — setpoint never goes below this |
| Solar boost temperature | 22.5 | 18–26 | °C | Setpoint during solar surplus |
| Pre-heat delta | 0.5 | 0–2 | °C | Extra °C above ideal when pre-heating |
| COP threshold temperature | 5.0 | -10–15 | °C | Outdoor temp below which COP is considered poor |
| COP recovery horizon | 6 | 1–24 | h | Hours ahead to look for COP recovery |
| Solar surplus threshold | 500 | 0–5000 | W | Minimum export to trigger solar boost |
| Solar confirmation delay | 10 | 0–60 | min | Export must be sustained this long before boost |
| Forecast horizon | 24 | 1–48 | h | How far ahead to check for cold periods |
| Floor heating thermal lag | 3 | 0–6 | h | Floor heating warm-up lag |
| Evaluation interval | 15 | 5–60 | min | How often the controller re-evaluates |

---

## Notifications

The controller can send a push notification every time it changes the thermostat setpoint.

1. Go to **Settings → Devices & services → Smart Heatpump Controller → Configure**.
2. In the **Notification targets** field, enter your notify service names (comma-separated, without the `notify.` prefix):
   ```
   mobile_app_my_phone, telegram
   ```
3. Click **Submit**.

Use the **Notifications** switch on the device or dashboard to mute/unmute without reconfiguring.

**Example notification:**

> **Smart Heatpump**
>
> Cold period coming — pre-heating while COP is still efficient
>
> Setpoint: 21.0°C → 21.5°C
> Outdoor: 6.2°C
> Solar export: 0W
> Rule: preheat

---

## Forecast.Solar (optional)

Forecast.Solar predicts solar production based on your panel configuration. When configured, the controller pre-heats *before* solar surplus starts.

1. Install **Forecast.Solar** via HACS or the built-in integration.
2. Go to **Settings → Devices & services → Smart Heatpump Controller → Configure**.
3. Select your Forecast.Solar entity (typically `sensor.energy_production_next_hour`).
4. Click **Submit**.

---

## Active rules

The **Active rule** sensor shows the controller's current decision:

| Rule | Meaning |
|---|---|
| `solar_boost` | Confirmed solar export — storing free energy as heat |
| `solar_predicted` | Predicted solar export — pre-heating before surplus starts |
| `preheat` | Cold period coming — pre-heating while COP is still efficient |
| `conserve` | COP poor, no recovery expected — holding minimum temperature |
| `conserve_await_recovery` | COP poor, but recovery coming — waiting for efficient window |
| `default` | Normal operation — maintaining ideal temperature |
| `error_fallback` | Error occurred — using safe fallback temperature |
| `initialising` | Controller starting up |

---

## Upgrading from v1 (AppDaemon)

v2 is a native Home Assistant integration — AppDaemon is no longer required.

1. Remove the old AppDaemon app files from `/config/appdaemon/apps/smart_heatpump/`.
2. Remove `/config/packages/smart_heatpump.yaml` (helpers are now created automatically).
3. Remove the old `input_number.shp_*` and `input_text.shp_active_rule` helpers from the UI if desired.
4. Install v2 via HACS (see Installation above).

---

## Troubleshooting

### Setpoint not changing

- Check the **Active rule** sensor — it should update every evaluation cycle.
- If it shows `error_fallback`, check the Home Assistant log for errors.
- Verify the thermostat entity works: **Settings → Developer tools → Services** → `climate.set_temperature`.

### Solar boost not triggering

- Your power sensor must return **negative values** when exporting. Check in **Settings → Developer tools → States**.
- Export must exceed the **Solar surplus threshold** for the **Solar confirmation delay** continuously.

### Pre-heat not triggering

- Outdoor temperature must be **above** the COP threshold for pre-heat to activate.
- The forecast must show temperatures dropping **below** the COP threshold within the effective horizon.

---

## Roadmap

**v2 candidates:**

- **ML thermal learning** — learn the building's thermal cool-down rate and adjust thermal lag automatically.
- **Dynamic electricity tariff optimisation** — integrate with real-time electricity prices (ENTSO-E, Tibber) to shift heating to low-price windows.
- **Multi-zone support** — control multiple thermostats with per-zone configuration.
- **Occupancy / presence setback** — reduce setpoint when no one is home.
- **Energy savings estimate in notifications** — include estimated kWh/€ saved.

---

## Contributing

Pull requests and issues are welcome. Please open an issue first to discuss significant changes.

When contributing code:
- Keep `decide()` in `decision.py` as a pure function with no HA dependency.
- Add a test case in `tests/test_decision_logic.py` for any new decision logic.
- Run `pytest tests/` before submitting.

---

## License

MIT — see [LICENSE](LICENSE).
