"""Coordinator for the Smart Heatpump Controller.

Runs the evaluation loop on a configurable interval, reads sensors,
calls the solar incremental decision logic, and applies the result.
Thermal model ML learning continues independently.

With a home battery, decisions use the grid power as it would be without
the battery, and a battery that discharges during a boost triggers an
evaluation straight away instead of waiting for the next interval.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .battery import DischargeWatch, effective_net_w, time_weighted_mean, to_discharge_w
from .const import (
    CONF_ACTIVE_MONTHS,
    CONF_BATTERY_POSITIVE,
    CONF_BATTERY_POWER,
    CONF_FORECAST_SOLAR,
    CONF_HEATPUMP_HEAT,
    CONF_HEATPUMP_POWER,
    CONF_NOTIFY_TARGETS,
    CONF_P1_POWER,
    CONF_TEMP_SENSOR,
    CONF_THERMOSTAT,
    CONF_WEATHER,
    DEFAULT_ACTIVE_MONTHS,
    DEFAULT_BOOST_DRAW_W,
    DEFAULTS,
    DOMAIN,
    RULE_DESCRIPTIONS,
)
from .decision import decide_solar
from .notifications import format_notification
from .solar_energy import SolarEnergyTracker
from .thermal_model import predict_hours_until_below
from .thermal_store import ThermalStore

_LOGGER = logging.getLogger(__name__)

try:
    with open(Path(__file__).with_name("manifest.json"), encoding="utf-8") as _f:
        _VERSION: str | None = json.load(_f).get("version")
except (OSError, ValueError):
    _VERSION = None

# How many minutes of P1 history to query for the rolling average
_AVG_WINDOW_MINUTES = 5

# Minimum number of P1 readings in the window before trusting the average.
# The P1 meter reports every ~10s, so 5 minutes should yield ~30 readings.
# Require at least 3 to avoid acting on stale/sparse data after HA restart.
_MIN_READINGS_FOR_AVG = 3

# How often solar energy is integrated when no sensor reports a change, and
# how often the energy sensors refresh their state.
_ENERGY_HEARTBEAT = timedelta(minutes=1)
_ENERGY_STORAGE_KEY = "smart_heatpump_solar_energy"
_ENERGY_SAVE_DELAY_S = 60


class SmartHeatpumpCoordinator:
    """Manages the evaluation loop and state for the Smart Heatpump Controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        # Mutable config values — number entities push updates here
        self.config_values: dict[str, float] = dict(DEFAULTS)

        # Active months — read from options (set via Configure dialog)

        # Switch entity pushes updates here
        self.notifications_enabled: bool = True

        # Thermal learning (continues independently — not used for thermostat control)
        self.thermal_store = ThermalStore(hass, entry.entry_id)

        # Solar-powered heat pump energy (only fed when a heat pump power sensor is set)
        self.solar_energy = SolarEnergyTracker()
        self._energy_store: Store = Store(
            hass, 1, f"{_ENERGY_STORAGE_KEY}_{entry.entry_id}"
        )
        self._energy_unsubs: list[CALLBACK_TYPE] = []

        # Evaluation state
        self.active_rule: str = "initialising"
        self.last_target: float | None = None  # Track computed setpoint
        self.hours_until_below_ideal: float | None = None
        self._solar_boost_active: bool = False
        self._boost_activated_at: float | None = None  # monotonic timestamp
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._listeners: list[callback] = []

        # One evaluation at a time — the battery watch can start one between timer runs
        self._eval_lock = asyncio.Lock()

        # Home battery — the priority switch pushes updates here
        self.heatpump_first: bool = False
        self._battery_watch = DischargeWatch()
        self._battery_check: CALLBACK_TYPE | None = None
        self._battery_eval_pending: bool = False
        self._battery_reset_at: float | None = None  # UTC timestamp

        # Dry run — switch entity pushes updates here
        self.dry_run_enabled: bool = not self._opt(CONF_THERMOSTAT)
        self.virtual_thermostat_entity = None  # Set by number.py on setup

        # Snapshot values for notifications / sensor attributes
        self._last_avg_import_5min: float = 0.0
        self._last_net_power: float | None = None
        self._last_outdoor_temp: float | None = None
        self._last_indoor_temp: float | None = None
        self._last_avg_forecast_temp: float | None = None

    def _opt(self, key: str, default: str = "") -> str:
        """Read a config value from options (primary) or data (migration fallback)."""
        return self.entry.options.get(key, self.entry.data.get(key, default))

    @property
    def dry_run(self) -> bool:
        """True when dry run is enabled or no thermostat configured."""
        return self.dry_run_enabled or not self._opt(CONF_THERMOSTAT)

    # ------------------------------------------------------------------
    # Listener management — entities register to get notified on changes
    # ------------------------------------------------------------------

    @callback
    def async_add_listener(self, update_callback: callback) -> callback:
        """Register a listener. Returns a callable to unregister."""
        self._listeners.append(update_callback)

        @callback
        def remove_listener() -> None:
            self._listeners.remove(update_callback)

        return remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Notify all registered listeners that state has changed."""
        for cb in self._listeners:
            cb()

    # ------------------------------------------------------------------
    # Config value management — number entities call this
    # ------------------------------------------------------------------

    def set_config_value(self, key: str, value: float) -> None:
        """Update a config value (called by number entities)."""
        self.config_values[key] = value

    # ------------------------------------------------------------------
    # Options helpers
    # ------------------------------------------------------------------

    @property
    def active_months(self) -> set[int]:
        """Get active months from options."""
        raw = self._opt(CONF_ACTIVE_MONTHS)
        if not raw:
            return {int(m) for m in DEFAULT_ACTIVE_MONTHS}
        if isinstance(raw, list):
            return {int(m) for m in raw}
        return {int(m) for m in DEFAULT_ACTIVE_MONTHS}

    @property
    def forecast_solar_entity(self) -> str | None:
        """Get the forecast solar entity from options."""
        entity = self._opt(CONF_FORECAST_SOLAR)
        return entity if entity else None

    @property
    def heatpump_power_entity(self) -> str | None:
        """Get the heat pump power entity from options."""
        entity = self._opt(CONF_HEATPUMP_POWER)
        return entity if entity else None

    @property
    def heatpump_heat_entity(self) -> str | None:
        """Get the heat pump heat output entity from options."""
        entity = self._opt(CONF_HEATPUMP_HEAT)
        return entity if entity else None

    @property
    def battery_power_entity(self) -> str | None:
        """Get the home battery power entity from options."""
        entity = self._opt(CONF_BATTERY_POWER)
        return entity if entity else None

    @property
    def battery_positive_discharge(self) -> bool:
        """Whether the battery sensor reads positive while discharging."""
        return self._opt(CONF_BATTERY_POSITIVE, "discharging") != "charging"

    @property
    def boost_draw_w(self) -> float:
        """Heat pump draw expected during a boost: set by hand, learned, or a default."""
        manual = self.config_values.get("heatpump_boost_draw", 0.0)
        if manual > 0:
            return manual
        learned = self.solar_energy.boost_draw_w
        return learned if learned is not None else DEFAULT_BOOST_DRAW_W

    @property
    def battery_cooldown_until(self) -> datetime | None:
        """End of the cooldown after a battery reset, or None when not cooling down."""
        if self._battery_reset_at is None:
            return None
        until = self._battery_reset_at + self.config_values.get("battery_cooldown_minutes", 0.0) * 60
        if until <= dt_util.utcnow().timestamp():
            return None
        return datetime.fromtimestamp(until, timezone.utc)

    def read_battery_discharge_w(self) -> float | None:
        """Battery power now, discharge positive (W), or None without a usable reading."""
        value = self._read_power_w(self.battery_power_entity or "")
        if value is None:
            return None
        return to_discharge_w(value, self.battery_positive_discharge)

    @property
    def notify_targets(self) -> list[str]:
        """Get notification targets from options."""
        raw = self._opt(CONF_NOTIFY_TARGETS)
        return [t.strip() for t in raw.split(",") if t.strip()]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start the evaluation loop (called after entities are set up)."""
        # Recover boost state after HA restart — if the thermostat setpoint
        # is above ideal, a boost was likely active before shutdown.
        self._recover_boost_state()

        self._start_energy_tracking()
        self._start_battery_watch()

        # First evaluation after a short delay to let entities restore
        self._cancel_timer = async_call_later(
            self.hass, 10, self._async_evaluate_callback
        )

    def _recover_boost_state(self) -> None:
        """Recover solar boost state after HA restart.

        If the thermostat setpoint is above temp_ideal, infer that a boost
        was active before shutdown and resume it.  This prevents the first
        evaluation from "re-activating" at room_temp + delta and lowering
        the setpoint.
        """
        import time as _time

        thermostat_sp = self._read_current_setpoint()
        ideal = self.config_values.get("temp_ideal", 21.0)
        if thermostat_sp is not None and thermostat_sp > ideal:
            self._solar_boost_active = True
            self.last_target = thermostat_sp
            self._boost_activated_at = _time.monotonic()
            _LOGGER.info(
                "Recovered boost state: thermostat at %.1f°C (above ideal %.1f°C) — resuming boost",
                thermostat_sp,
                ideal,
            )

    @callback
    def async_stop(self) -> None:
        """Stop the evaluation loop."""
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None
        if self._battery_check:
            self._battery_check()
            self._battery_check = None
        for unsub in self._energy_unsubs:
            unsub()
        self._energy_unsubs.clear()
        self._energy_store.async_delay_save(self.solar_energy.to_dict, 0)

    async def _async_evaluate_callback(self, _now: Any = None) -> None:
        """Timer callback — wraps evaluate with error recovery."""
        try:
            async with self._eval_lock:
                await self.async_evaluate()
        except Exception:
            _LOGGER.exception("Unhandled exception in evaluation")
            await self._async_safe_fallback()
        finally:
            self._schedule_next()

    @callback
    def _schedule_next(self) -> None:
        """Schedule the next evaluation based on current interval setting."""
        interval_min = self.config_values["evaluation_interval_min"]
        self._cancel_timer = async_call_later(
            self.hass, int(interval_min * 60), self._async_evaluate_callback
        )

    # ------------------------------------------------------------------
    # Core evaluation — Solar Incremental Flow
    # ------------------------------------------------------------------

    async def async_evaluate(self) -> None:
        """Read sensors, run solar incremental decision, apply result."""
        cfg = self.config_values

        # Read sensors
        outdoor_temp = self._read_outdoor_temp()
        indoor_temp = self._read_indoor_temp()
        net_power = self._read_net_power()
        solar_export = max(0.0, -net_power) if net_power is not None else None
        grid_import = max(0.0, net_power) if net_power is not None else None
        forecast_solar = self._read_forecast_solar()

        # Home battery: discharge positive, None when unavailable
        has_battery = bool(self.battery_power_entity)
        battery_now = self.read_battery_discharge_w() if has_battery else None
        now_ts = dt_util.utcnow().timestamp()
        battery_discharging = False
        if has_battery:
            self._battery_watch.update(now_ts, battery_now, cfg.get("battery_discharge_limit", 50.0))
            battery_discharging = self._solar_boost_active and self._battery_watch.sustained(
                now_ts, cfg.get("battery_reaction_minutes", 2.0) * 60
            )

        # Store snapshots for notification / sensor attributes
        self._last_net_power = net_power
        self._last_outdoor_temp = outdoor_temp
        self._last_indoor_temp = indoor_temp

        # ---- Thermal model learning (continues independently) ----
        now_utc = datetime.now(timezone.utc)
        if indoor_temp is not None and outdoor_temp is not None:
            heating_active = self._is_heating_active()
            solar_gain = self._is_solar_gain_likely(
                solar_surplus=solar_export,
                forecast_solar=forecast_solar,
            )
            self.thermal_store.add_observation(
                timestamp=now_utc.timestamp(),
                indoor_temp_c=indoor_temp,
                outdoor_temp_c=outdoor_temp,
                heating_active=heating_active,
                solar_gain_likely=solar_gain,
            )
            await self.thermal_store.async_save()

        # Thermal model prediction (informational — not used for control)
        forecast_temps = await self._async_read_forecast_temps(24)
        if (
            self.thermal_store.loss_coefficient is not None
            and indoor_temp is not None
            and forecast_temps
        ):
            self.hours_until_below_ideal = predict_hours_until_below(
                indoor_temp_c=indoor_temp,
                outdoor_temps=forecast_temps,
                threshold_temp_c=cfg["temp_ideal"],
                loss_coefficient_k=self.thermal_store.loss_coefficient,
            )
        else:
            self.hours_until_below_ideal = None

        # ---- Forecast temperature gate ----
        forecast_skip_threshold = cfg.get("forecast_temp_skip_threshold", 15.0)
        if forecast_temps:
            avg_forecast_temp = sum(forecast_temps) / len(forecast_temps)
            self._last_avg_forecast_temp = round(avg_forecast_temp, 1)
            # A boost running on the battery is still reset on a warm forecast
            if avg_forecast_temp >= forecast_skip_threshold and not battery_discharging:
                _LOGGER.info(
                    "Skipping solar boost — 24h forecast avg %.1f°C >= threshold %.1f°C",
                    avg_forecast_temp,
                    forecast_skip_threshold,
                )
                self.active_rule = "forecast_warm"
                self._notify_listeners()
                return

        # ---- 5-minute rolling averages from P1 history ----
        # Query HA's recorder for all P1 readings in the last N minutes.
        # The P1 meter reports every ~10s, giving ~30 readings per 5-min window.
        p1_averages = await self._async_read_p1_averages()
        if p1_averages is None:
            # Not enough P1 readings — skip solar decision entirely.
            # This prevents wrong boost activation/step-up when we have no data
            # (e.g. just after HA restart, recorder not ready yet).
            _LOGGER.info(
                "Skipping solar decision — not enough P1 data in %d-min window",
                _AVG_WINDOW_MINUTES,
            )
            self.active_rule = "waiting_for_data"
            self._notify_listeners()
            return
        avg_export_5min, avg_import_5min, avg_net_5min = p1_averages
        current_export = solar_export or 0.0

        # ---- Home battery: decide on the grid power without the battery ----
        boost_start_export = 0.0
        battery_cooldown = False
        if has_battery:
            battery_avgs = await self._async_read_battery_averages()
            if battery_avgs is None or battery_now is None:
                if not self._solar_boost_active:
                    # Can't tell whether a boost would run on the battery — don't start one
                    _LOGGER.info("Battery sensor unavailable — no new boost")
                    self.active_rule = "battery_unavailable"
                    self._notify_listeners()
                    return
                battery_avgs = (0.0, 0.0)
            avg_discharge, avg_charge = battery_avgs
            eff_avg = effective_net_w(avg_net_5min, avg_discharge, avg_charge, self.heatpump_first)
            avg_export_5min, avg_import_5min = max(0.0, -eff_avg), max(0.0, eff_avg)
            if net_power is not None and battery_now is not None:
                eff_now = effective_net_w(
                    net_power, max(0.0, battery_now), max(0.0, -battery_now), self.heatpump_first
                )
                current_export = max(0.0, -eff_now)
            boost_start_export = self.boost_draw_w
            battery_cooldown = self.battery_cooldown_until is not None
            _LOGGER.debug(
                "Battery 5-min avg: discharge=%.0fW charge=%.0fW | effective export=%.0fW import=%.0fW",
                avg_discharge, avg_charge, avg_export_5min, avg_import_5min,
            )
        self._last_avg_import_5min = avg_import_5min

        # ---- Read current setpoint ----
        # Use our last commanded target (self.last_target) when boost is active,
        # because the thermostat may not have updated its reported setpoint yet
        # from the previous set_temperature call (poll lag).  Fall back to the
        # thermostat-reported value when we haven't commanded anything yet.
        thermostat_setpoint = self._read_current_setpoint()
        if self.dry_run:
            current_setpoint = self.last_target
        elif self._solar_boost_active and self.last_target is not None:
            current_setpoint = self.last_target
        else:
            current_setpoint = thermostat_setpoint

        # ---- Solar incremental decision ----
        now_local = dt_util.now()
        current_month = now_local.month

        import time as _time
        boost_active_seconds = 0.0
        if self._solar_boost_active and self._boost_activated_at is not None:
            boost_active_seconds = _time.monotonic() - self._boost_activated_at

        target, rule, new_boost_active = decide_solar(
            current_month=current_month,
            solar_boost_active=self._solar_boost_active,
            avg_export_5min_w=avg_export_5min,
            avg_import_5min_w=avg_import_5min,
            current_temperature=indoor_temp,
            current_setpoint=current_setpoint,
            temp_ideal=cfg["temp_ideal"],
            solar_surplus_threshold=cfg["solar_surplus_threshold"],
            solar_release_threshold_high=cfg["solar_release_threshold_high"],
            solar_release_threshold_low=cfg["solar_release_threshold_low"],
            solar_step_delta=cfg["solar_step_delta"],
            active_months=self.active_months,
            boost_active_seconds=boost_active_seconds,
            min_boost_minutes=cfg.get("solar_min_boost_minutes", 0.0),
            max_boost_temp=cfg.get("solar_max_boost_temp", 25.0),
            current_export_w=current_export,
            boost_start_export_w=boost_start_export,
            battery_discharging=battery_discharging,
            battery_cooldown=battery_cooldown,
        )

        prev_boost_active = self._solar_boost_active
        self._solar_boost_active = new_boost_active
        if new_boost_active != prev_boost_active:
            # Close the interval under the old flag, start the next under the new one
            self._record_energy()
        if not self.dry_run:
            today = now_local.date().isoformat()
            if new_boost_active and not prev_boost_active:
                self.solar_energy.count(today, "boosts")
            if rule == "solar_reset":
                self.solar_energy.count(today, "resets")
            if rule == "solar_reset_battery":
                self.solar_energy.count(today, "battery_resets")
        if rule == "solar_reset_battery":
            self._battery_reset_at = now_ts

        # Track when boost was activated
        if new_boost_active and not prev_boost_active:
            self._boost_activated_at = _time.monotonic()
        elif not new_boost_active:
            self._boost_activated_at = None

        # Determine if we should apply a change
        # Only act when the rule actually changes the thermostat
        actionable_rules = {
            "solar_incremental",
            "solar_step_down",
            "solar_reset",
            "solar_reset_battery",
            "solar_boost_deactivated",
            "solar_min_run",
        }
        is_actionable = rule in actionable_rules

        if self.dry_run:
            previous = self.last_target
            setpoint_changed = is_actionable and (
                previous is None or abs(target - previous) >= 0.1
            )
            _LOGGER.warning(
                "DRY RUN eval: rule=%s | target=%.1f | previous=%s | actionable=%s | changed=%s | export=%s | avg_import=%.0f",
                rule, target, previous, is_actionable, setpoint_changed, solar_export, avg_import_5min,
            )
            if setpoint_changed:
                # Update the virtual thermostat on the dashboard
                if self.virtual_thermostat_entity is not None:
                    self.virtual_thermostat_entity.set_value_from_coordinator(target)
                _LOGGER.warning("DRY RUN — calling _async_send_notification for rule=%s", rule)
                await self._async_send_notification(
                    old_setpoint=previous,
                    new_setpoint=target,
                    rule=rule,
                    outdoor_temp=outdoor_temp,
                    indoor_temp=indoor_temp,
                    net_power=net_power,
                    avg_import_5min=avg_net_5min,
                    battery_w=battery_now,
                )
        else:
            setpoint_changed = is_actionable and (
                current_setpoint is None or abs(target - current_setpoint) >= 0.1
            )
            if setpoint_changed:
                await self._async_set_thermostat(target)
                _LOGGER.info(
                    "Setpoint change: %s -> %.1f°C | rule=%s | export=%sW | avg_import_5min=%.0fW",
                    current_setpoint,
                    target,
                    rule,
                    solar_export,
                    avg_import_5min,
                )
                await self._async_send_notification(
                    old_setpoint=current_setpoint,
                    new_setpoint=target,
                    rule=rule,
                    outdoor_temp=outdoor_temp,
                    indoor_temp=indoor_temp,
                    net_power=net_power,
                    avg_import_5min=avg_net_5min,
                    battery_w=battery_now,
                )
            else:
                _LOGGER.debug(
                    "No change: setpoint=%.1f°C | rule=%s | export=%sW",
                    target,
                    rule,
                    solar_export,
                )

        self.last_target = target
        self.active_rule = rule
        self._notify_listeners()

    # ------------------------------------------------------------------
    # Solar-powered heat pump energy
    # ------------------------------------------------------------------

    async def async_load_energy(self) -> None:
        """Load persisted solar energy totals."""
        data = await self._energy_store.async_load()
        if data:
            self.solar_energy.load_dict(data)

    @callback
    def _start_energy_tracking(self) -> None:
        """Integrate on every heat pump / P1 reading, plus a 1-minute heartbeat.

        Runs without a heat pump meter too: boost time, boost counts and
        unused export only need the P1 sensor.
        """
        p1_entity = self._opt(CONF_P1_POWER)
        if not p1_entity:
            return

        entities = [p1_entity]
        if self.heatpump_power_entity:
            entities.append(self.heatpump_power_entity)
        if self.heatpump_heat_entity:
            entities.append(self.heatpump_heat_entity)
        if self.battery_power_entity:
            entities.append(self.battery_power_entity)

        @callback
        def _on_state_change(_event: Any) -> None:
            self._record_energy()

        @callback
        def _on_heartbeat(_now: Any) -> None:
            self._record_energy()
            self._notify_listeners()

        self._energy_unsubs.append(
            async_track_state_change_event(self.hass, entities, _on_state_change)
        )
        self._energy_unsubs.append(
            async_track_time_interval(self.hass, _on_heartbeat, _ENERGY_HEARTBEAT)
        )
        self._record_energy()

    @callback
    def _record_energy(self) -> None:
        """Feed the current readings to the solar energy tracker.

        Counts as "boost" only when the controller is really steering the
        thermostat — in dry run the heat pump is not being boosted.  Without
        a heat pump meter its draw counts as 0 W (no solar, no savings).
        """
        boost = self._solar_boost_active and not self.dry_run
        cfg = self.config_values
        added = self.solar_energy.update(
            ts=dt_util.utcnow().timestamp(),
            day=dt_util.now().date().isoformat(),
            heatpump_w=(
                self._read_power_w(self.heatpump_power_entity)
                if self.heatpump_power_entity
                else 0.0
            ),
            net_w=self._read_power_w(self._opt(CONF_P1_POWER)),
            boost_active=boost,
            value_eur_per_kwh=cfg.get("price_import", 0.0) - cfg.get("price_export", 0.0),
            heat_w=self._read_power_w(self.heatpump_heat_entity or ""),
            discharge_w=self.read_battery_discharge_w() if self.battery_power_entity else None,
        )
        if added:
            self._energy_store.async_delay_save(
                self.solar_energy.to_dict, _ENERGY_SAVE_DELAY_S
            )

    # ------------------------------------------------------------------
    # Home battery watch — a boost must never run on battery power
    # ------------------------------------------------------------------

    @callback
    def _start_battery_watch(self) -> None:
        """Check the battery on every reading instead of waiting for the next evaluation."""
        if not self.battery_power_entity:
            return

        @callback
        def _on_state_change(_event: Any) -> None:
            self._check_battery()

        self._energy_unsubs.append(
            async_track_state_change_event(
                self.hass, [self.battery_power_entity], _on_state_change
            )
        )

    @callback
    def _check_battery(self, _now: Any = None) -> None:
        """Evaluate at once when the battery has discharged during a boost for the reaction time."""
        if _now is not None:
            self._battery_check = None  # the scheduled check is firing
        cfg = self.config_values
        now = dt_util.utcnow().timestamp()
        reaction_s = cfg.get("battery_reaction_minutes", 2.0) * 60
        self._battery_watch.update(
            now, self.read_battery_discharge_w(), cfg.get("battery_discharge_limit", 50.0)
        )
        due = self._battery_watch.due_at(reaction_s)
        if not self._solar_boost_active or due is None:
            if self._battery_check:
                self._battery_check()
                self._battery_check = None
            return
        if due > now:
            # The sensor may not report again while its value holds — check when due
            if self._battery_check is None:
                self._battery_check = async_call_later(
                    self.hass, due - now + 1, self._check_battery
                )
            return
        if not self._battery_eval_pending:
            self._battery_eval_pending = True
            self.hass.async_create_task(self._async_battery_evaluate())

    async def _async_battery_evaluate(self) -> None:
        """Extra evaluation between timer runs; the timer keeps its own schedule."""
        try:
            async with self._eval_lock:
                # The timer run may have ended the boost while we waited
                if self._solar_boost_active:
                    _LOGGER.info("Battery discharging during boost — evaluating now")
                    await self.async_evaluate()
        except Exception:
            _LOGGER.exception("Unhandled exception in battery-triggered evaluation")
            await self._async_safe_fallback()
        finally:
            self._battery_eval_pending = False

    async def _async_read_battery_averages(self) -> tuple[float, float] | None:
        """Time-weighted 5-min averages of battery (discharge, charge) in W, or None without data."""
        entity_id = self.battery_power_entity
        if not entity_id:
            return None
        now = dt_util.utcnow()
        start_time = now - timedelta(minutes=_AVG_WINDOW_MINUTES)
        states = await self._async_history(entity_id, start_time, now, include_start=True)
        if states is None:
            return None
        current = self.hass.states.get(entity_id)
        scale = 1000.0 if current and current.attributes.get("unit_of_measurement") == "kW" else 1.0
        samples: list[tuple[float, float]] = []
        for state in states:
            try:
                value = float(state.state) * scale
            except (TypeError, ValueError):
                continue
            samples.append(
                (state.last_changed.timestamp(), to_discharge_w(value, self.battery_positive_discharge))
            )
        start, end = start_time.timestamp(), now.timestamp()
        discharge = time_weighted_mean(samples, start, end, lambda v: max(0.0, v))
        charge = time_weighted_mean(samples, start, end, lambda v: max(0.0, -v))
        if discharge is None or charge is None:
            return None
        return discharge, charge

    def _read_power_w(self, entity_id: str) -> float | None:
        """Read a power sensor in watts (converts kW), without logging noise."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        if state.attributes.get("unit_of_measurement") == "kW":
            value *= 1000.0
        return value

    # ------------------------------------------------------------------
    # Sensor readers
    # ------------------------------------------------------------------

    def _is_heating_active(self) -> bool:
        """Check if the thermostat is currently calling for heat."""
        entity_id = self._opt(CONF_THERMOSTAT)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        action = state.attributes.get("hvac_action", "")
        return action == "heating"

    def _is_solar_gain_likely(
        self,
        solar_surplus: float | None,
        forecast_solar: float | None,
    ) -> bool:
        """Detect whether passive solar gain may be warming the house."""
        if solar_surplus is not None and solar_surplus > 0:
            return True
        if forecast_solar is not None and forecast_solar > 50:
            return True
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is not None:
            elevation = sun_state.attributes.get("elevation", -90)
            try:
                if float(elevation) > 5:
                    return True
            except (TypeError, ValueError):
                pass
        return False

    async def _async_read_p1_averages(self) -> tuple[float, float, float] | None:
        """Query HA recorder for P1 power readings over the last N minutes.

        Returns (avg_export_watts, avg_import_watts), or None if there is
        not enough data to compute a reliable average.  Returning None
        signals the caller to skip the solar decision entirely — this
        prevents wrong boost activation when data is missing (e.g. right
        after HA restart or when the recorder is not ready).

        Negative P1 values = export, positive = import.
        """
        entity_id = self._opt(CONF_P1_POWER)
        if not entity_id:
            return None

        now = dt_util.utcnow()
        start_time = now - timedelta(minutes=_AVG_WINDOW_MINUTES)

        states = await self._async_history(entity_id, start_time, now, include_start=False)
        if states is None:
            return None
        readings: list[float] = []
        for state in states:
            if state.state in ("unavailable", "unknown", ""):
                continue
            try:
                readings.append(float(state.state))
            except (TypeError, ValueError):
                continue

        if len(readings) < _MIN_READINGS_FOR_AVG:
            _LOGGER.debug(
                "P1 history: only %d readings in %d-min window (need %d)",
                len(readings), _AVG_WINDOW_MINUTES, _MIN_READINGS_FOR_AVG,
            )
            return None

        # Compute average net power, then split into export and import
        avg_net = sum(readings) / len(readings)
        avg_export = max(0.0, -avg_net)
        avg_import = max(0.0, avg_net)

        _LOGGER.debug(
            "P1 5-min avg: %d readings | net=%.0fW | export=%.0fW | import=%.0fW",
            len(readings), avg_net, avg_export, avg_import,
        )
        return avg_export, avg_import, avg_net

    async def _async_history(
        self, entity_id: str, start_time: datetime, end_time: datetime, include_start: bool
    ) -> list[Any] | None:
        """Recorder states of one entity (unavailable ones dropped), or None if the query fails."""
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import state_changes_during_period

            history = await get_instance(self.hass).async_add_executor_job(
                state_changes_during_period,
                self.hass,
                start_time,
                end_time,
                entity_id,
                True,   # no_attributes — we only need the state value
                False,  # descending
                None,   # limit
                include_start,  # include_start_time_state
            )
        except Exception:
            _LOGGER.warning("Failed to query history for '%s'", entity_id)
            return None
        return [
            s for s in history.get(entity_id, [])
            if s.state not in ("unavailable", "unknown", "")
        ]

    def _read_outdoor_temp(self) -> float | None:
        """Read outdoor temperature from the weather entity."""
        entity_id = self._opt(CONF_WEATHER)
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.warning("Weather entity '%s' unavailable", entity_id)
            return None
        temp = state.attributes.get("temperature")
        if temp is None:
            return None
        try:
            return float(temp)
        except (TypeError, ValueError):
            return None

    def _read_net_power(self) -> float | None:
        """Read net grid power in watts (positive=import, negative=export)."""
        entity_id = self._opt(CONF_P1_POWER)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.warning("P1 sensor '%s' unavailable", entity_id)
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def _async_read_forecast_temps(self, horizon_hours: float) -> list[float]:
        """Read hourly forecast temperatures using weather.get_forecasts service."""
        entity_id = self._opt(CONF_WEATHER)
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except Exception:
            _LOGGER.warning("Failed to call weather.get_forecasts for '%s'", entity_id)
            return []

        if not response:
            return []

        forecasts = response.get(entity_id, {}).get("forecast", [])
        if not forecasts:
            _LOGGER.warning("No forecast data from '%s'", entity_id)
            return []

        now = dt_util.utcnow()
        cutoff = now + timedelta(hours=horizon_hours)
        temps: list[float] = []

        for entry in forecasts:
            try:
                dt_str = entry.get("datetime", "")
                entry_dt = dt_util.parse_datetime(dt_str)
                if entry_dt and entry_dt <= cutoff:
                    temps.append(float(entry["temperature"]))
            except (TypeError, ValueError, KeyError):
                continue

        return temps

    def _read_forecast_solar(self) -> float | None:
        """Read predicted solar yield from Forecast.Solar entity."""
        entity_id = self.forecast_solar_entity
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_current_setpoint(self) -> float | None:
        """Read current setpoint from the thermostat."""
        entity_id = self._opt(CONF_THERMOSTAT)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        temp = state.attributes.get("temperature")
        if temp is None:
            return None
        try:
            return float(temp)
        except (TypeError, ValueError):
            return None

    def _read_indoor_temp(self) -> float | None:
        """Read indoor temperature from optional sensor or thermostat fallback."""
        temp_entity = self._opt(CONF_TEMP_SENSOR)
        if temp_entity:
            state = self.hass.states.get(temp_entity)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    return float(state.state)
                except (TypeError, ValueError):
                    pass
            _LOGGER.warning("Temperature sensor '%s' unavailable", temp_entity)

        therm_entity = self._opt(CONF_THERMOSTAT)
        if therm_entity:
            state = self.hass.states.get(therm_entity)
            if state:
                temp = state.attributes.get("current_temperature")
                if temp is not None:
                    try:
                        return float(temp)
                    except (TypeError, ValueError):
                        pass

        return None

    # ------------------------------------------------------------------
    # Actuators
    # ------------------------------------------------------------------

    async def _async_set_thermostat(self, target: float) -> None:
        """Set the thermostat to the target temperature."""
        entity_id = self._opt(CONF_THERMOSTAT)
        if not entity_id:
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": target},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("Failed to set thermostat '%s' to %.1f°C", entity_id, target)

    async def _async_send_notification(
        self,
        old_setpoint: float | None,
        new_setpoint: float,
        rule: str,
        outdoor_temp: float | None,
        indoor_temp: float | None,
        net_power: float | None,
        avg_import_5min: float,
        battery_w: float | None = None,
    ) -> None:
        """Send enriched notification on setpoint change."""
        _LOGGER.warning(
            "NOTIFY entry: rule=%s | enabled=%s | targets=%s",
            rule, self.notifications_enabled, self.notify_targets,
        )
        if not self.notifications_enabled:
            _LOGGER.warning("Notification skipped — notifications disabled")
            return

        targets = self.notify_targets
        if not targets:
            _LOGGER.warning("Notification skipped — no targets configured")
            return

        cfg = self.config_values
        description = RULE_DESCRIPTIONS.get(rule, rule)
        title, message = format_notification(
            rule=rule,
            description=description,
            old_setpoint=old_setpoint,
            new_setpoint=new_setpoint,
            outdoor_temp=outdoor_temp,
            indoor_temp=indoor_temp,
            net_power=net_power,
            avg_import_5min=avg_import_5min,
            dry_run=self.dry_run,
            config=cfg,
            forecast_avg_24h=self._last_avg_forecast_temp,
            version=_VERSION,
            battery_w=battery_w,
        )

        _LOGGER.warning("Sending notification to targets: %s | message_len=%d", targets, len(message))
        for target_name in targets:
            try:
                await self.hass.services.async_call(
                    "notify",
                    target_name,
                    {"title": title, "message": message},
                    blocking=True,
                )
                _LOGGER.warning("Notification SENT OK to '%s' for rule=%s", target_name, rule)
            except Exception:
                _LOGGER.exception("Notification FAILED to '%s' for rule=%s", target_name, rule)

    async def _async_safe_fallback(self) -> None:
        """Safe fallback on unhandled exception."""
        if not self.dry_run:
            try:
                await self._async_set_thermostat(DEFAULTS["temp_ideal"])
            except Exception:
                pass
        self.active_rule = "error_fallback"
        self._notify_listeners()
