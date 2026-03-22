"""Constants for the Smart Heatpump Controller integration."""

from __future__ import annotations

DOMAIN = "smart_heatpump"

# Config entry keys (set during config flow)
CONF_THERMOSTAT = "thermostat_entity"
CONF_P1_POWER = "p1_net_power_entity"
CONF_WEATHER = "weather_entity"
CONF_TEMP_SENSOR = "temp_sensor_entity"

# Options entry keys (set via Configure button)
CONF_FORECAST_SOLAR = "forecast_solar_entity"
CONF_NOTIFY_TARGETS = "notify_targets"

# ---------------------------------------------------------------------------
# Defaults — used as initial values for number entities on first install
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, float] = {
    "temp_ideal": 21.0,
    "temp_minimum": 20.5,
    "temp_solar_boost": 22.5,
    "preheat_delta": 0.5,
    "cop_threshold_temp": 5.0,
    "cop_recovery_horizon_hours": 6.0,
    "solar_surplus_threshold": 500.0,
    "solar_confirm_minutes": 10.0,
    "forecast_horizon_hours": 24.0,
    "thermal_lag_hours": 3.0,
    "evaluation_interval_min": 15.0,
    "solar_boost_stop_import": 700.0,
}

# ---------------------------------------------------------------------------
# Number entity definitions: (key, name, min, max, step, unit, icon)
# ---------------------------------------------------------------------------
NUMBER_DEFINITIONS: list[tuple[str, str, float, float, float, str, str]] = [
    ("temp_ideal", "Ideal temperature", 16, 26, 0.5, "°C", "mdi:thermometer"),
    ("temp_minimum", "Minimum temperature", 14, 24, 0.5, "°C", "mdi:thermometer-low"),
    ("temp_solar_boost", "Solar boost temperature", 18, 26, 0.5, "°C", "mdi:solar-power"),
    ("preheat_delta", "Pre-heat delta", 0, 2.0, 0.5, "°C", "mdi:thermometer-plus"),
    ("cop_threshold_temp", "COP threshold temperature", -10, 15, 1.0, "°C", "mdi:heat-pump-outline"),
    ("cop_recovery_horizon_hours", "COP recovery horizon", 1, 24, 1, "h", "mdi:clock-fast"),
    ("solar_surplus_threshold", "Solar surplus threshold", 0, 5000, 100, "W", "mdi:solar-panel"),
    ("solar_confirm_minutes", "Solar confirmation delay", 0, 60, 5, "min", "mdi:timer-outline"),
    ("forecast_horizon_hours", "Forecast horizon", 1, 48, 1, "h", "mdi:weather-partly-cloudy"),
    ("thermal_lag_hours", "Floor heating thermal lag", 0, 6, 0.5, "h", "mdi:floor-plan"),
    ("evaluation_interval_min", "Evaluation interval", 5, 60, 5, "min", "mdi:refresh"),
    ("solar_boost_stop_import", "Solar boost stop import", 0, 3000, 100, "W", "mdi:transmission-tower-import"),
]

# ---------------------------------------------------------------------------
# Human-readable rule descriptions for notifications
# ---------------------------------------------------------------------------
RULE_DESCRIPTIONS: dict[str, str] = {
    "solar_boost": "Solar surplus detected — storing free energy as heat",
    "preheat": "Cold period coming — pre-heating while COP is still efficient",
    "conserve": "COP poor, no recovery expected — holding minimum temperature",
    "conserve_await_recovery": "COP poor but recovery coming — waiting for efficient window",
    "default": "Normal operation — maintaining ideal temperature",
    "indoor_buffer_ok": "Indoor temperature sufficient — skipping pre-heat",
    "error_fallback": "Error occurred — using safe fallback temperature",
    "initialising": "Controller starting up",
}
