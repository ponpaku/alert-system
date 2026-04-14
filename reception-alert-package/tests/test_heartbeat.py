from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from heartbeat import HeartbeatSender, HeartbeatState
from tests.test_config import make_raw_config
from config import parse_config


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class RecordingTransport:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.closed = False
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def request(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
        return FakeResponse(status_code=self.status_code)

    def close(self) -> None:
        self.closed = True


class HeartbeatSenderTests(unittest.TestCase):
    def test_send_once_posts_expected_payload_and_headers(self) -> None:
        raw = make_raw_config()
        raw["heartbeat"] = {
            "enabled": True,
            "url": "https://script.google.com/macros/s/example/exec",
            "shared_secret": "super-secret",
            "instance_id": "raspi-frontdesk-01",
        }
        config = parse_config(raw)
        transport = RecordingTransport()
        sender = HeartbeatSender(
            config.heartbeat,
            config.http,
            location_name=config.location_name,
            state_supplier=lambda: HeartbeatState(queue_depth=2, worker_alive=True, worker_fatal=False),
            started_at=datetime.now(timezone.utc) - timedelta(seconds=90),
            transport=transport,
        )

        ok = sender.send_once("heartbeat")

        self.assertTrue(ok)
        self.assertEqual(len(transport.calls), 1)
        request = transport.calls[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], raw["heartbeat"]["url"])
        self.assertEqual(request["headers"]["X-Reception-Alert-Heartbeat-Secret"], "super-secret")
        self.assertEqual(request["json_body"]["event"], "heartbeat")
        self.assertEqual(request["json_body"]["status"], "alive")
        self.assertEqual(request["json_body"]["instance_id"], "raspi-frontdesk-01")
        self.assertEqual(request["json_body"]["queue_depth"], 2)
        self.assertTrue(request["json_body"]["worker_alive"])
        self.assertFalse(request["json_body"]["worker_fatal"])
        self.assertGreaterEqual(request["json_body"]["uptime_seconds"], 90)

    def test_start_and_shutdown_emit_lifecycle_events(self) -> None:
        raw = make_raw_config()
        raw["heartbeat"] = {
            "enabled": True,
            "url": "https://script.google.com/macros/s/example/exec",
            "interval_seconds": 0.05,
            "timeout_seconds": 0.05,
            "jitter_seconds": 0,
            "failure_backoff_seconds": 0.05,
            "stale_after_seconds": 1,
        }
        config = parse_config(raw)
        transport = RecordingTransport()
        sender = HeartbeatSender(
            config.heartbeat,
            config.http,
            location_name=config.location_name,
            state_supplier=lambda: HeartbeatState(queue_depth=0, worker_alive=True, worker_fatal=False),
            started_at=datetime.now(timezone.utc),
            transport=transport,
        )
        try:
            sender.start()
            deadline = time.monotonic() + 1.0
            while len(transport.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            sender.shutdown()

        events = [call["json_body"]["event"] for call in transport.calls]
        self.assertIn("startup", events)
        self.assertIn("heartbeat", events)
        self.assertEqual(events[-1], "shutdown")
        self.assertFalse(transport.closed)
