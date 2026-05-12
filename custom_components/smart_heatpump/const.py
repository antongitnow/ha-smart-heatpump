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
    "evaluation_interval_min": 5.0,
    # Solar incremental flow
    "solar_surplus_threshold": 300.0,
    "solar_release_threshold_high": 700.0,
    "solar_release_threshold_low": 300.0,
    "solar_step_delta": 0.5,
    "solar_min_boost_minutes": 20.0,
    "solar_max_boost_temp": 25.0,
}

# Default active months — Sep through Apr (heating season)
DEFAULT_ACTIVE_MONTHS: set[int] = {1, 2, 3, 4, 9, 10, 11, 12}

MONTH_NAMES: list[tuple[int, str]] = [
    (1, "January"),
    (2, "February"),
    (3, "March"),
    (4, "April"),
    (5, "May"),
    (6, "June"),
    (7, "July"),
    (8, "August"),
    (9, "September"),
    (10, "October"),
    (11, "November"),
    (12, "December"),
]

# ---------------------------------------------------------------------------
# Number entity definitions: (key, name, min, max, step, unit, icon)
# ---------------------------------------------------------------------------
NUMBER_DEFINITIONS: list[tuple[str, str, float, float, float, str, str]] = [
    ("temp_ideal", "Ideal temperature", 16, 26, 0.5, "°C", "mdi:thermometer"),
    ("temp_minimum", "Minimum temperature", 14, 24, 0.5, "°C", "mdi:thermometer-low"),
    ("evaluation_interval_min", "Evaluation interval", 1, 60, 1, "min", "mdi:refresh"),
    ("solar_surplus_threshold", "Solar surplus threshold", 0, 5000, 50, "W", "mdi:solar-panel"),
    ("solar_release_threshold_high", "Solar release threshold high", 0, 5000, 50, "W", "mdi:transmission-tower-import"),
    ("solar_release_threshold_low", "Solar release threshold low", 0, 5000, 50, "W", "mdi:transmission-tower-import"),
    ("solar_step_delta", "Solar step delta", 0.1, 3.0, 0.1, "°C", "mdi:thermometer-plus"),
    ("solar_min_boost_minutes", "Minimum boost duration", 0, 60, 5, "min", "mdi:timer-outline"),
    ("solar_max_boost_temp", "Maximum boost temperature", 10, 30, 0.5, "°C", "mdi:thermometer-high"),
]

# ---------------------------------------------------------------------------
# Human-readable rule descriptions for notifications
# ---------------------------------------------------------------------------
RULE_DESCRIPTIONS: dict[str, str] = {
    "solar_incremental": "Solar surplus - boosting setpoint",
    "solar_step_down": "Moderate import - stepping setpoint down",
    "solar_reset": "High import - resetting setpoint to ideal",
    "solar_boost_deactivated": "Setpoint reached ideal - boost deactivated",
    "no_solar_action": "No solar action",
    "solar_boost_holding": "Solar boost active - holding setpoint",
    "solar_min_run": "Minimum run time - keeping boost active",
    "default": "Normal operation",
    "error_fallback": "Error - using safe fallback temperature",
    "initialising": "Controller starting up",
}
