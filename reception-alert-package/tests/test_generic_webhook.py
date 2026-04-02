from __future__ import annotations

import unittest

from config import GenericWebhookAuthConfig, GenericWebhookConfig
from destinations.generic_webhook import GenericWebhookDestination
from message_constants import DEFAULT_ALERT_MESSAGE, DEFAULT_ALERT_PREFIX, DEFAULT_LOCATION_NAME, LOCATION_LABEL
from models import build_alert_event
from transport import HttpResponse


class FakeTransport:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.calls: list[dict] = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return HttpResponse(self.status_code, {}, "")


class GenericWebhookTests(unittest.TestCase):
    def test_json_payload_templates_are_rendered(self) -> None:
        transport = FakeTransport()
        config = GenericWebhookConfig(
            type="generic_webhook",
            name="hook",
            enabled=True,
            url="https://example.com/hook",
            method="POST",
            content_type="json",
            success_status_codes=None,
            headers={},
            auth=GenericWebhookAuthConfig(type="none"),
            payload={"event_id": "{{ event_id }}", "text": "{{ text }}"},
        )

        destination = GenericWebhookDestination(config, transport)  # type: ignore[arg-type]
        result = destination.send(make_event())

        self.assertTrue(result.ok)
        self.assertEqual(
            transport.calls[0]["json_body"]["text"],
            f"{DEFAULT_ALERT_PREFIX} {DEFAULT_ALERT_MESSAGE}\n{LOCATION_LABEL}{DEFAULT_LOCATION_NAME}",
        )

    def test_form_payload_and_bearer_auth_are_rendered(self) -> None:
        transport = FakeTransport()
        config = GenericWebhookConfig(
            type="generic_webhook",
            name="hook",
            enabled=True,
            url="https://example.com/hook",
            method="POST",
            content_type="form",
            success_status_codes=None,
            headers={"X-Test": "1"},
            auth=GenericWebhookAuthConfig(type="bearer", token="secret"),
            payload={"button": "{{ button_name }}", "text": "{{ text }}"},
        )

        destination = GenericWebhookDestination(config, transport)  # type: ignore[arg-type]
        destination.send(make_event())

        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(transport.calls[0]["data"]["button"], "staff")


def make_event():
    return build_alert_event(
        button_name="staff",
        kind="alert",
        prefix=DEFAULT_ALERT_PREFIX,
        message=DEFAULT_ALERT_MESSAGE,
        location_name=DEFAULT_LOCATION_NAME,
    )
