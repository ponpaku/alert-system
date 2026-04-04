from __future__ import annotations

import unittest

from config import ConfigError, parse_config
from message_constants import DEFAULT_ALERT_MESSAGE, DEFAULT_ALERT_PREFIX, DEFAULT_LOCATION_NAME


def make_raw_config() -> dict:
    return {
        "location_name": DEFAULT_LOCATION_NAME,
        "http": {
            "user_agent": "ReceptionAlert/1.0",
            "request_timeout_seconds": 5,
            "verify_tls": True,
            "ca_bundle_path": "",
            "response_body_limit_bytes": 4096,
        },
        "gpio": {"alive_led_gpio": 5, "send_led_gpio": 27},
        "timing": {
            "bounce_seconds": 0.08,
            "cooldown_seconds": 3,
            "success_hold_seconds": 30,
            "failure_blink_seconds": 30,
        },
        "delivery": {
            "retry_delays_seconds": [0, 1, 3],
            "queue_capacity": 8,
            "shutdown_grace_seconds": 6,
            "max_retry_after_seconds": 30,
            "max_event_delivery_seconds": 15,
            "running_cutoff_grace_seconds": 5.5,
            "max_parallel_destinations": 4,
            "persistent_queue_path": ":memory:",
            "persistent_retry_base_seconds": 15,
            "persistent_retry_max_seconds": 300,
        },
        "destinations": [
            {
                "type": "nextcloud_talk",
                "name": "talk-main",
                "enabled": True,
                "base_url": "https://cloud.example.com",
                "username": "alertbot",
                "app_password": "secret",
                "room_token": "room1",
            },
            {
                "type": "generic_webhook",
                "name": "hook-main",
                "enabled": True,
                "url": "https://hooks.example.com/reception",
                "method": "POST",
                "content_type": "json",
                "payload": {"text": "{{ text }}", "event_id": "{{ event_id }}"},
            },
        ],
        "buttons": [
            {
                "name": "staff",
                "gpio": 17,
                "prefix": DEFAULT_ALERT_PREFIX,
                "message": DEFAULT_ALERT_MESSAGE,
                "destinations": ["talk-main", "hook-main"],
            }
        ],
    }


class ConfigTests(unittest.TestCase):
    def test_parse_config_accepts_new_format(self) -> None:
        config = parse_config(make_raw_config())
        self.assertEqual(config.location_name, DEFAULT_LOCATION_NAME)
        self.assertEqual(config.buttons[0].destinations, ("talk-main", "hook-main"))

    def test_duplicate_destination_name_fails_fast(self) -> None:
        raw = make_raw_config()
        raw["destinations"].append(dict(raw["destinations"][0]))
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_unknown_destination_reference_fails_fast(self) -> None:
        raw = make_raw_config()
        raw["buttons"][0]["destinations"] = ["missing"]
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_button_with_only_disabled_destinations_fails_fast(self) -> None:
        raw = make_raw_config()
        raw["destinations"][0]["enabled"] = False
        raw["destinations"][1]["enabled"] = False
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_button_with_mixed_enabled_and_disabled_destinations_fails_fast(self) -> None:
        raw = make_raw_config()
        raw["destinations"][1]["enabled"] = False
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_invalid_generic_webhook_content_type_fails_fast(self) -> None:
        raw = make_raw_config()
        raw["destinations"][1]["content_type"] = "xml"
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_queue_capacity_zero_fails_fast(self) -> None:
        raw = make_raw_config()
        raw["delivery"]["queue_capacity"] = 0
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_queue_capacity_negative_fails_fast(self) -> None:
        raw = make_raw_config()
        raw["delivery"]["queue_capacity"] = -1
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_request_timeout_must_be_positive(self) -> None:
        raw = make_raw_config()
        raw["http"]["request_timeout_seconds"] = 0
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_response_body_limit_must_be_positive(self) -> None:
        raw = make_raw_config()
        raw["http"]["response_body_limit_bytes"] = 0
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_retry_delay_must_be_non_negative(self) -> None:
        raw = make_raw_config()
        raw["delivery"]["retry_delays_seconds"] = [0, -1]
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_persistent_retry_max_must_not_be_smaller_than_base(self) -> None:
        raw = make_raw_config()
        raw["delivery"]["persistent_retry_base_seconds"] = 30
        raw["delivery"]["persistent_retry_max_seconds"] = 29
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_failure_blink_seconds_must_be_non_negative(self) -> None:
        raw = make_raw_config()
        raw["timing"]["failure_blink_seconds"] = -1
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_running_cutoff_grace_seconds_must_be_non_negative(self) -> None:
        raw = make_raw_config()
        raw["delivery"]["running_cutoff_grace_seconds"] = -0.1
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_generic_webhook_bearer_auth_requires_token(self) -> None:
        raw = make_raw_config()
        raw["destinations"][1]["auth"] = {"type": "bearer"}
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_generic_webhook_basic_auth_requires_username_and_password(self) -> None:
        raw = make_raw_config()
        raw["destinations"][1]["auth"] = {"type": "basic", "username": "user"}
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_generic_webhook_header_auth_requires_header_name(self) -> None:
        raw = make_raw_config()
        raw["destinations"][1]["auth"] = {"type": "header", "header_value": "secret"}
        with self.assertRaises(ConfigError):
            parse_config(raw)
