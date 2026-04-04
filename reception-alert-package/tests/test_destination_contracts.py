from __future__ import annotations

import threading
import time
import unittest

from config import (
    DiscordWebhookConfig,
    GenericWebhookAuthConfig,
    GenericWebhookConfig,
    LineBotConfig,
    NextcloudBotConfig,
    NextcloudTalkConfig,
    SlackWebhookConfig,
)
from destinations.discord_webhook import DiscordWebhookDestination
from destinations.generic_webhook import GenericWebhookDestination
from destinations.line_bot import LineBotDestination
from destinations.nextcloud_bot import NextcloudBotDestination
from destinations.nextcloud_talk import NextcloudTalkDestination
from destinations.slack_webhook import SlackWebhookDestination
from models import AlertEvent, build_alert_event
from transport import HttpResponse


class FakeTransport:
    def __init__(self, response: HttpResponse):
        self._response = response
        self.calls: list[dict] = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class DestinationContractTests(unittest.TestCase):
    def test_all_destinations_short_circuit_when_stop_event_is_set(self) -> None:
        stop_event = threading.Event()
        stop_event.set()

        for destination, transport in build_destinations():
            with self.subTest(destination=destination.name):
                result = destination.send(make_event(), stop_event=stop_event)
                self.assertEqual(result.outcome, "not_attempted")
                self.assertEqual(result.error_summary, "stopped before request start")
                self.assertEqual(transport.calls, [])

    def test_all_destinations_short_circuit_when_deadline_is_already_expired(self) -> None:
        expired_deadline = time.monotonic() - 1.0

        for destination, transport in build_destinations():
            with self.subTest(destination=destination.name):
                result = destination.send(make_event(), deadline_monotonic=expired_deadline)
                self.assertEqual(result.outcome, "not_attempted")
                self.assertEqual(result.error_summary, "deadline exceeded before request start")
                self.assertEqual(transport.calls, [])

    def test_all_destinations_forward_deadline_to_transport(self) -> None:
        deadline = time.monotonic() + 30.0

        for destination, transport in build_destinations():
            with self.subTest(destination=destination.name):
                result = destination.send(make_event(), deadline_monotonic=deadline)
                self.assertEqual(result.outcome, "success")
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.calls[0]["deadline_monotonic"], deadline)

    def test_destination_specific_success_status_contracts(self) -> None:
        for case in build_destination_cases():
            with self.subTest(destination=case["name"]):
                success_transport = FakeTransport(HttpResponse(case["success_status"], {}, ""))
                success_destination = case["factory"](success_transport)
                success_result = success_destination.send(make_event())
                self.assertEqual(success_result.outcome, "success")

                failure_transport = FakeTransport(HttpResponse(case["failure_status"], {}, "bad"))
                failure_destination = case["factory"](failure_transport)
                failure_result = failure_destination.send(make_event())
                self.assertEqual(failure_result.outcome, "failed")
                self.assertEqual(failure_result.status_code, case["failure_status"])

    def test_destination_failures_preserve_retryability_contract(self) -> None:
        retry_after_headers = {"Retry-After": "7"}

        for case in build_destination_cases():
            with self.subTest(destination=case["name"]):
                retry_transport = FakeTransport(HttpResponse(case["retryable_status"], retry_after_headers, "retry"))
                retry_destination = case["factory"](retry_transport)
                retry_result = retry_destination.send(make_event())
                self.assertEqual(retry_result.outcome, "failed")
                self.assertTrue(retry_result.retryable)
                self.assertEqual(retry_result.retry_after_seconds, 7.0)

                non_retry_transport = FakeTransport(HttpResponse(case["non_retryable_status"], {}, "bad"))
                non_retry_destination = case["factory"](non_retry_transport)
                non_retry_result = non_retry_destination.send(make_event())
                self.assertEqual(non_retry_result.outcome, "failed")
                self.assertFalse(non_retry_result.retryable)


def build_destinations() -> list[tuple[object, FakeTransport]]:
    return [(case["factory"](transport := FakeTransport(HttpResponse(case["success_status"], {}, ""))), transport) for case in build_destination_cases()]


def build_destination_cases() -> list[dict[str, object]]:
    destinations: list[tuple[object, FakeTransport]] = []
    return [
        {
            "name": "talk-main",
            "factory": lambda transport: NextcloudTalkDestination(
                NextcloudTalkConfig(
                    type="nextcloud_talk",
                    name="talk-main",
                    enabled=True,
                    base_url="https://cloud.example.com",
                    username="bot",
                    app_password="secret",
                    room_token="room",
                ),
                transport,
            ),
            "success_status": 201,
            "failure_status": 200,
            "retryable_status": 429,
            "non_retryable_status": 400,
        },
        {
            "name": "talk-bot",
            "factory": lambda transport: NextcloudBotDestination(
                NextcloudBotConfig(
                    type="nextcloud_bot",
                    name="talk-bot",
                    enabled=True,
                    base_url="https://cloud.example.com",
                    conversation_token="room",
                    shared_secret="secret",
                    silent=False,
                ),
                transport,
            ),
            "success_status": 201,
            "failure_status": 200,
            "retryable_status": 429,
            "non_retryable_status": 400,
        },
        {
            "name": "discord-main",
            "factory": lambda transport: DiscordWebhookDestination(
                DiscordWebhookConfig(
                    type="discord_webhook",
                    name="discord-main",
                    enabled=True,
                    webhook_url="https://discord.example.com",
                ),
                transport,
            ),
            "success_status": 204,
            "failure_status": 200,
            "retryable_status": 429,
            "non_retryable_status": 400,
        },
        {
            "name": "slack-main",
            "factory": lambda transport: SlackWebhookDestination(
                SlackWebhookConfig(
                    type="slack_webhook",
                    name="slack-main",
                    enabled=True,
                    webhook_url="https://slack.example.com",
                ),
                transport,
            ),
            "success_status": 200,
            "failure_status": 204,
            "retryable_status": 429,
            "non_retryable_status": 400,
        },
        {
            "name": "line-main",
            "factory": lambda transport: LineBotDestination(
                LineBotConfig(
                    type="line_bot",
                    name="line-main",
                    enabled=True,
                    channel_access_token="token",
                    to="user",
                ),
                transport,
            ),
            "success_status": 200,
            "failure_status": 204,
            "retryable_status": 429,
            "non_retryable_status": 400,
        },
        {
            "name": "generic-main",
            "factory": lambda transport: GenericWebhookDestination(
                GenericWebhookConfig(
                    type="generic_webhook",
                    name="generic-main",
                    enabled=True,
                    url="https://hooks.example.com",
                    method="POST",
                    content_type="json",
                    success_status_codes=(202,),
                    headers={"X-Test": "1"},
                    auth=GenericWebhookAuthConfig(type="none"),
                    payload={"text": "{{ text }}"},
                ),
                transport,
            ),
            "success_status": 202,
            "failure_status": 200,
            "retryable_status": 429,
            "non_retryable_status": 400,
        },
    ]


def make_event() -> AlertEvent:
    return build_alert_event(
        button_name="staff",
        kind="alert",
        prefix="prefix",
        message="message",
        location_name="frontdesk",
    )
