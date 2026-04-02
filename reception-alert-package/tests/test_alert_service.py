from __future__ import annotations

import threading
import time
import unittest

from alert_service import AlertService
from config import ConfigError, parse_config
from message_constants import TEST_PREFIX
from models import AlertEvent, DispatchResult

from tests.test_config import make_raw_config


class BlockingDispatcher:
    def __init__(self):
        self.calls: list[tuple[AlertEvent, tuple[str, ...] | None]] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def dispatch(self, event, target_names=None, *, stop_event=None, deadline_monotonic=None, deadline_supplier=None):
        self.calls.append((event, tuple(target_names) if target_names is not None else None))
        self.started.set()
        self.release.wait(1.0)
        return [DispatchResult.success("talk-main", 201)]


class NeverFinishingDispatcher:
    def dispatch(self, event, target_names=None, *, stop_event=None, deadline_monotonic=None, deadline_supplier=None):
        while True:
            time.sleep(0.05)


class FailingDispatcher:
    def __init__(self):
        self.call_times: list[float] = []
        self.started = threading.Event()

    def dispatch(self, event, target_names=None, *, stop_event=None, deadline_monotonic=None, deadline_supplier=None):
        self.call_times.append(time.monotonic())
        self.started.set()
        return [DispatchResult.failed("talk-main", status_code=500, retryable=False, error_summary="boom")]


class FakeThread:
    def __init__(self):
        self.join_timeout: float | None = None

    def join(self, timeout: float | None = None) -> None:
        self.join_timeout = timeout

    def is_alive(self) -> bool:
        return False


class AlertServiceTests(unittest.TestCase):
    def test_cooldown_is_applied_on_press(self) -> None:
        config = parse_config(make_raw_config())
        dispatcher = BlockingDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            self.assertFalse(service.handle_button_press("staff"))
        finally:
            dispatcher.release.set()
            service.shutdown()

    def test_cooldown_check_is_atomic_for_concurrent_presses(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 3
        config = parse_config(raw)
        dispatcher = BlockingDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False)
        results: list[bool] = []
        threads = [threading.Thread(target=lambda: results.append(service.handle_button_press("staff"))) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(1 for result in results if result), 1)
        finally:
            dispatcher.release.set()
            service.shutdown()

    def test_queue_overflow_drops_new_event(self) -> None:
        raw = make_raw_config()
        raw["delivery"]["queue_capacity"] = 1
        raw["timing"]["cooldown_seconds"] = 0
        config = parse_config(raw)
        dispatcher = BlockingDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            dispatcher.started.wait(1.0)
            self.assertTrue(service.handle_button_press("staff"))
            self.assertFalse(service.handle_button_press("staff"))
        finally:
            dispatcher.release.set()
            service.shutdown()

    def test_shutdown_does_not_start_unhandled_queue_item(self) -> None:
        raw = make_raw_config()
        raw["delivery"]["queue_capacity"] = 2
        raw["timing"]["cooldown_seconds"] = 0
        config = parse_config(raw)
        dispatcher = BlockingDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            dispatcher.started.wait(1.0)
            self.assertTrue(service.handle_button_press("staff"))
            service.shutdown()
            time.sleep(0.1)
            self.assertEqual(len(dispatcher.calls), 1)
        finally:
            dispatcher.release.set()

    def test_test_dispatch_uses_dispatcher_targets(self) -> None:
        config = parse_config(make_raw_config())
        dispatcher = BlockingDispatcher()
        dispatcher.release.set()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            service.dispatch_test_button("staff")
            self.assertEqual(dispatcher.calls[0][0].button_name, "staff")
            self.assertEqual(dispatcher.calls[0][1], ("talk-main", "hook-main"))
        finally:
            service.shutdown()

    def test_test_dispatch_uses_test_prefix(self) -> None:
        config = parse_config(make_raw_config())
        dispatcher = BlockingDispatcher()
        dispatcher.release.set()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            event = service._build_event(config.button_by_name("staff"), kind="test")
            self.assertEqual(event.prefix, TEST_PREFIX)
        finally:
            service.shutdown()

    def test_press_time_event_is_preserved_until_dispatch(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        config = parse_config(raw)
        dispatcher = BlockingDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            before_press = time.time()
            self.assertTrue(service.handle_button_press("staff"))
            dispatcher.started.wait(1.0)
            dispatched_event = dispatcher.calls[0][0]
            after_dispatch = time.time()
            occurred_at = dispatched_event.occurred_at.timestamp()
            self.assertGreaterEqual(occurred_at, before_press - 0.1)
            self.assertLessEqual(occurred_at, after_dispatch + 0.1)
        finally:
            dispatcher.release.set()
            service.shutdown()

    def test_failure_blink_does_not_block_next_dispatch(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        raw["timing"]["failure_blink_seconds"] = 2
        config = parse_config(raw)
        dispatcher = FailingDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            dispatcher.started.wait(1.0)
            self.assertTrue(service.handle_button_press("staff"))
            deadline = time.monotonic() + 1.0
            while len(dispatcher.call_times) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(dispatcher.call_times), 2)
            self.assertLess(dispatcher.call_times[1] - dispatcher.call_times[0], 1.0)
        finally:
            service.shutdown()

    def test_gpio_support_is_required_for_normal_service_startup(self) -> None:
        config = parse_config(make_raw_config())
        with self.assertRaises(ConfigError):
            AlertService(config, BlockingDispatcher(), use_gpio=True)

    def test_shutdown_wait_uses_request_timeout_when_longer_than_grace(self) -> None:
        raw = make_raw_config()
        raw["http"]["request_timeout_seconds"] = 12
        raw["delivery"]["shutdown_grace_seconds"] = 3
        config = parse_config(raw)
        service = AlertService(config, BlockingDispatcher(), use_gpio=False)
        fake_thread = FakeThread()
        service._worker_thread = fake_thread
        try:
            service.shutdown()
            self.assertEqual(fake_thread.join_timeout, 13.0)
        finally:
            service._stop_event.set()

    def test_shutdown_marks_leds_closed_even_if_worker_is_still_running(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        raw["delivery"]["shutdown_grace_seconds"] = 0.05
        config = parse_config(raw)
        service = AlertService(config, NeverFinishingDispatcher(), use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            time.sleep(0.1)
            service.shutdown()
            self.assertTrue(service.send_led_controller.is_closed)
            self.assertTrue(service._worker_thread.is_alive())
        finally:
            service._stop_event.set()
