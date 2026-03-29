"""Unit tests for notification message formatting.

Ensures notification messages are safe for Telegram and other
notification services that parse HTML/Markdown by default.

Run with:
    pytest tests/test_notifications.py -v
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

# Import modules directly without triggering __init__.py (which needs homeassistant).
_base = Path(__file__).parent.parent / "custom_components" / "smart_heatpump"

_const_spec = importlib.util.spec_from_file_location("const", _base / "const.py")
_const_module = importlib.util.module_from_spec(_const_spec)
_const_spec.loader.exec_module(_const_module)
RULE_DESCRIPTIONS = _const_module.RULE_DESCRIPTIONS

_notif_spec = importlib.util.spec_from_file_location("notifications", _base / "notifications.py")
_notif_module = importlib.util.module_from_spec(_notif_spec)
_notif_spec.loader.exec_module(_notif_module)
format_notification = _notif_module.format_notification

# Characters that Telegram interprets as HTML markup and will cause
# "Can't parse entities" errors.
TELEGRAM_UNSAFE_CHARS = re.compile(r"[<>&]")

# Characters that may cause issues with various notification parsers.
PROBLEMATIC_UNICODE = re.compile(r"[→←↑↓°—–]")

# ---------------------------------------------------------------------------
# Shared test config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "solar_surplus_threshold": 300.0,
    "solar_release_threshold_high": 700.0,
    "solar_release_threshold_low": 300.0,
    "solar_step_delta": 0.5,
}


def _format(**overrides) -> tuple[str, str]:
    """Call format_notification with defaults, overriding specific kwargs."""
    rule = overrides.pop("rule", "solar_incremental")
    kwargs = {
        "rule": rule,
        "description": RULE_DESCRIPTIONS.get(rule, rule),
        "old_setpoint": 21.0,
        "new_setpoint": 21.5,
        "outdoor_temp": 8.0,
        "indoor_temp": 21.0,
        "net_power": -500.0,
        "avg_import_5min": 0.0,
        "dry_run": False,
        "config": DEFAULT_CONFIG,
        **overrides,
    }
    return format_notification(**kwargs)


# ===========================================================================
# Telegram safety — no HTML-breaking characters
# ===========================================================================

class TestTelegramSafety:
    """Ensure notification messages don't contain characters that break Telegram."""

    def test_no_html_chars_in_all_rules(self) -> None:
        """Every known rule must produce a message without < > &."""
        for rule in RULE_DESCRIPTIONS:
            title, message = _format(rule=rule)
            assert not TELEGRAM_UNSAFE_CHARS.search(title), f"Title for '{rule}' contains unsafe chars: {title}"
            assert not TELEGRAM_UNSAFE_CHARS.search(message), f"Message for '{rule}' contains unsafe chars: {message}"

    def test_no_html_chars_in_unknown_rule(self) -> None:
        """Unknown rule name used as description — still safe."""
        title, message = _format(rule="some_future_rule")
        assert not TELEGRAM_UNSAFE_CHARS.search(message), f"Message contains unsafe chars: {message}"

    def test_no_problematic_unicode_in_all_rules(self) -> None:
        """No arrows, degree symbols, or em dashes in any rule message."""
        for rule in list(RULE_DESCRIPTIONS.keys()) + ["unknown_rule"]:
            title, message = _format(rule=rule)
            assert not PROBLEMATIC_UNICODE.search(title), f"Title for '{rule}' has problematic unicode: {title}"
            assert not PROBLEMATIC_UNICODE.search(message), f"Message for '{rule}' has problematic unicode: {message}"

    def test_no_problematic_chars_in_rule_descriptions(self) -> None:
        """All RULE_DESCRIPTIONS values must be plain-text safe."""
        for rule, desc in RULE_DESCRIPTIONS.items():
            assert not TELEGRAM_UNSAFE_CHARS.search(desc), f"Description for '{rule}' contains unsafe chars: {desc}"
            assert not PROBLEMATIC_UNICODE.search(desc), f"Description for '{rule}' has problematic unicode: {desc}"

    def test_safe_with_none_values(self) -> None:
        """None values for optional fields must not produce unsafe chars."""
        title, message = _format(
            old_setpoint=None,
            outdoor_temp=None,
            indoor_temp=None,
            net_power=None,
        )
        assert not TELEGRAM_UNSAFE_CHARS.search(message), f"Message contains unsafe chars: {message}"
        assert not PROBLEMATIC_UNICODE.search(message), f"Message has problematic unicode: {message}"

    def test_safe_with_large_values(self) -> None:
        """Large power/temp values must not produce unsafe chars."""
        title, message = _format(
            net_power=-5000.0,
            avg_import_5min=3000.0,
            indoor_temp=35.5,
            outdoor_temp=-15.0,
            old_setpoint=25.0,
            new_setpoint=25.5,
        )
        assert not TELEGRAM_UNSAFE_CHARS.search(message), f"Message contains unsafe chars: {message}"


# ===========================================================================
# Message content
# ===========================================================================

class TestMessageContent:
    """Verify notification message content is correct."""

    def test_title_is_smart_heatpump(self) -> None:
        title, _ = _format()
        assert title == "Smart Heatpump"

    def test_dry_run_tag_present(self) -> None:
        _, message = _format(dry_run=True)
        assert "[DRY RUN]" in message

    def test_dry_run_tag_absent(self) -> None:
        _, message = _format(dry_run=False)
        assert "[DRY RUN]" not in message

    def test_rule_name_in_message(self) -> None:
        _, message = _format(rule="solar_reset")
        assert "solar_reset" in message

    def test_setpoint_change_in_message(self) -> None:
        _, message = _format(old_setpoint=21.0, new_setpoint=21.5)
        assert "21.0" in message
        assert "21.5" in message

    def test_none_old_setpoint_shows_na(self) -> None:
        _, message = _format(old_setpoint=None)
        assert "N/A" in message

    def test_export_power_format(self) -> None:
        _, message = _format(net_power=-800.0)
        assert "Export 800W" in message

    def test_import_power_format(self) -> None:
        _, message = _format(net_power=400.0)
        assert "Import 400W" in message

    def test_none_power_shows_na(self) -> None:
        _, message = _format(net_power=None)
        assert "N/A" in message

    def test_thresholds_in_message(self) -> None:
        _, message = _format()
        assert "300W export" in message
        assert "700W import" in message
        assert "0.5C" in message
