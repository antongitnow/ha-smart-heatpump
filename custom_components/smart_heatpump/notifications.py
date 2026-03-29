"""Notification message formatting for the Smart Heatpump Controller.

Pure functions — no Home Assistant dependency.
Can be unit tested with plain pytest.
"""

from __future__ import annotations


def format_notification(
    rule: str,
    description: str,
    old_setpoint: float | None,
    new_setpoint: float,
    outdoor_temp: float | None,
    indoor_temp: float | None,
    net_power: float | None,
    avg_import_5min: float,
    dry_run: bool,
    config: dict[str, float],
) -> tuple[str, str]:
    """Build notification title and message for a setpoint change.

    Returns:
        (title, message) — plain-text strings safe for Telegram and other
        notification services. Must not contain characters that Telegram
        interprets as HTML/Markdown markup (e.g. ``<``, ``>``, ``&``).
    """
    # Format values
    outdoor_str = f"{outdoor_temp:.1f}C" if outdoor_temp is not None else "N/A"
    indoor_str = f"{indoor_temp:.1f}C" if indoor_temp is not None else "N/A"
    old_str = f"{old_setpoint:.1f}C" if old_setpoint is not None else "N/A"

    # Current power: show as export or import
    if net_power is not None:
        if net_power < 0:
            current_power_str = f"Export {abs(net_power):.0f}W"
        else:
            current_power_str = f"Import {net_power:.0f}W"
    else:
        current_power_str = "N/A"

    # 5-min average: show as import
    avg_str = f"Import {avg_import_5min:.0f}W"

    title = "Smart Heatpump"
    dry_run_tag = " [DRY RUN]" if dry_run else ""
    message = (
        f"{description}\n"
        f"\n"
        f"Rule: {rule}{dry_run_tag}\n"
        f"Room: {indoor_str}\n"
        f"Outdoor: {outdoor_str}\n"
        f"Setpoint: {old_str} to {new_setpoint:.1f}C\n"
        f"\n"
        f"Current power: {current_power_str}\n"
        f"5-min avg: {avg_str}\n"
        f"\n"
        f"Thresholds:\n"
        f"  Surplus activate: {config['solar_surplus_threshold']:.0f}W export\n"
        f"  Release high: {config['solar_release_threshold_high']:.0f}W import\n"
        f"  Release low: {config['solar_release_threshold_low']:.0f}W import\n"
        f"  Step delta: {config['solar_step_delta']:.1f}C"
    )

    return title, message
