from __future__ import annotations

import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from alert_service import AlertService
from config import ConfigError, parse_config
from dispatcher import Dispatcher
from message_constants import TEST_PREFIX
from models import AlertEvent, DispatchResult

from tests.test_config import make_raw_config


class BlockingDispatcher:
    def __init__(self):
        self.calls: list[tuple[AlertEvent, tuple[str, ...] | None]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def resolve_target_names(self, target_names=None):
        return list(target_names or ["talk-main", "hook-main"])

    def close(self) -> None:
        self.closed = True

    def dispatch(
        self,
        event,
        target_names=None,
        *,
        stop_event=None,
        deadline_monotonic=None,
        deadline_supplier=None,
        result_handler=None,
    ):
        self.calls.append((event, tuple(target_names) if target_names is not None else None))
        self.started.set()
        self.release.wait(1.0)
        results = [DispatchResult.success("talk-main", 201)]
        if result_handler is not None:
            for result in results:
                result_handler(result)
        return results


class NeverFinishingDispatcher:
    def resolve_target_names(self, target_names=None):
        return list(target_names or ["talk-main", "hook-main"])

    def dispatch(
        self,
        event,
        target_names=None,
        *,
        stop_event=None,
        deadline_monotonic=None,
        deadline_supplier=None,
        result_handler=None,
    ):
        while True:
            time.sleep(0.05)


class FailingDispatcher:
    def __init__(self):
        self.call_times: list[float] = []
        self.started = threading.Event()

    def resolve_target_names(self, target_names=None):
        return list(target_names or ["talk-main", "hook-main"])

    def dispatch(
        self,
        event,
        target_names=None,
        *,
        stop_event=None,
        deadline_monotonic=None,
        deadline_supplier=None,
        result_handler=None,
    ):
        self.call_times.append(time.monotonic())
        self.started.set()
        results = [DispatchResult.failed("talk-main", status_code=500, retryable=False, error_summary="boom")]
        if result_handler is not None:
            for result in results:
                result_handler(result)
        return results


class CrashingDispatcher:
    def __init__(self):
        self.started = threading.Event()

    def resolve_target_names(self, target_names=None):
        return list(target_names or ["talk-main", "hook-main"])

    def dispatch(
        self,
        event,
        target_names=None,
        *,
        stop_event=None,
        deadline_monotonic=None,
        deadline_supplier=None,
        result_handler=None,
    ):
        self.started.set()
        raise RuntimeError("boom")


class NonRetryableDispatcher:
    def __init__(self):
        self.started = threading.Event()

    def resolve_target_names(self, target_names=None):
        return list(target_names or ["talk-main", "hook-main"])

    def dispatch(
        self,
        event,
        target_names=None,
        *,
        stop_event=None,
        deadline_monotonic=None,
        deadline_supplier=None,
        result_handler=None,
    ):
        self.started.set()
        results = [
            DispatchResult.failed("talk-main", status_code=400, retryable=False, error_summary="bad request"),
            DispatchResult.not_attempted("hook-main", error_summary="unknown destination"),
        ]
        if result_handler is not None:
            for result in results:
                result_handler(result)
        return results

    def close(self) -> None:
        pass


class LegacyDispatcher:
    def __init__(self):
        self.started = threading.Event()
        self.calls: list[tuple[AlertEvent, tuple[str, ...] | None]] = []

    def resolve_target_names(self, target_names=None):
        return list(target_names or ["talk-main"])

    def dispatch(self, event, target_names=None, *, stop_event=None, deadline_monotonic=None, deadline_supplier=None):
        self.calls.append((event, tuple(target_names) if target_names is not None else None))
        self.started.set()
        return [DispatchResult.success("talk-main", 201)]

    def close(self) -> None:
        pass


class ExplodingDestination:
    def __init__(self, name: str):
        self.name = name
        self.enabled = True
        self.calls = 0

    def send(self, event, *, stop_event=None, deadline_monotonic=None):
        self.calls += 1
        raise RuntimeError("boom")


class SlowSuccessDestination:
    def __init__(self, name: str, *, sleep_seconds: float):
        self.name = name
        self.enabled = True
        self.calls = 0
        self.sleep_seconds = sleep_seconds

    def send(self, event, *, stop_event=None, deadline_monotonic=None):
        self.calls += 1
        time.sleep(self.sleep_seconds)
        return DispatchResult.success(self.name, 200)


class SlowIgnoringStopDestination(SlowSuccessDestination):
    def __init__(self, name: str, results: list[DispatchResult], *, sleep_seconds: float):
        super().__init__(name, sleep_seconds=sleep_seconds)
        self._results = list(results)

    def send(self, event, *, stop_event=None, deadline_monotonic=None):
        self.calls += 1
        time.sleep(self.sleep_seconds)
        return self._results[min(self.calls - 1, len(self._results) - 1)]


class FakeThread:
    def __init__(self):
        self.join_timeout: float | None = None

    def join(self, timeout: float | None = None) -> None:
        self.join_timeout = timeout

    def is_alive(self) -> bool:
        return False


class RecordingLed:
    instances: list["RecordingLed"] = []

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.on_calls = 0
        self.off_calls = 0
        RecordingLed.instances.append(self)

    def on(self) -> None:
        self.on_calls += 1

    def off(self) -> None:
        self.off_calls += 1

    def close(self) -> None:
        self.closed = True


class TrackingQueueStore:
    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True


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
        raw["delivery"]["persistent_retry_base_seconds"] = 10
        raw["delivery"]["persistent_retry_max_seconds"] = 10
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

    def test_worker_crash_sets_fatal_error_and_rejects_new_presses(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        config = parse_config(raw)
        dispatcher = CrashingDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            dispatcher.started.wait(1.0)
            deadline = time.monotonic() + 1.0
            while service._worker_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(service._worker_thread.is_alive())
            self.assertIsNotNone(service._get_fatal_error())
            self.assertFalse(service.handle_button_press("staff"))
        finally:
            service.shutdown()

    def test_non_retryable_results_are_not_requeued(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        config = parse_config(raw)
        dispatcher = NonRetryableDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            dispatcher.started.wait(1.0)
            deadline = time.monotonic() + 1.0
            while service._queue_store.pending_count() != 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(service._queue_store.pending_count(), 0)
        finally:
            service.shutdown()

    def test_test_mode_does_not_require_persistent_queue(self) -> None:
        raw = make_raw_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            raw["delivery"]["persistent_queue_path"] = tmpdir
            config = parse_config(raw)
            service = AlertService(config, BlockingDispatcher(), use_gpio=False, enable_queue_worker=False)
            try:
                self.assertFalse(service.enable_queue_worker)
                self.assertIsNone(service._queue_store)
            finally:
                service.shutdown()

    def test_legacy_dispatcher_is_rejected_in_persistent_queue_mode(self) -> None:
        raw = make_raw_config()
        config = parse_config(raw)
        dispatcher = LegacyDispatcher()
        with self.assertRaises(ConfigError):
            AlertService(config, dispatcher, use_gpio=False)

    def test_legacy_dispatcher_can_still_be_used_without_queue_worker(self) -> None:
        config = parse_config(make_raw_config())
        dispatcher = LegacyDispatcher()
        service = AlertService(config, dispatcher, use_gpio=False, enable_queue_worker=False)
        try:
            summary = service.dispatch_test_button("staff")
            self.assertEqual(summary, "success")
            self.assertEqual(len(dispatcher.calls), 1)
        finally:
            service.shutdown()

    def test_parallel_destination_exception_keeps_success_progress_and_worker_alive(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        raw["delivery"]["persistent_retry_base_seconds"] = 10
        raw["delivery"]["persistent_retry_max_seconds"] = 10
        config = parse_config(raw)
        dispatcher = Dispatcher(
            [
                ExplodingDestination("talk-main"),
                SlowSuccessDestination("hook-main", sleep_seconds=0.05),
            ],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
        )
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            deadline = time.monotonic() + 2.0
            while service._queue_store.pending_count() != 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(service._queue_store.pending_count(), 0)
            self.assertTrue(service._worker_thread.is_alive())
            self.assertIsNone(service._get_fatal_error())
        finally:
            service.shutdown()

    def test_inflight_destination_completion_within_cutoff_grace_drains_queue_without_fatal(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        raw["delivery"]["max_event_delivery_seconds"] = 0.02
        raw["delivery"]["running_cutoff_grace_seconds"] = 0.2
        raw["delivery"]["persistent_retry_base_seconds"] = 10
        raw["delivery"]["persistent_retry_max_seconds"] = 10
        config = parse_config(raw)
        dispatcher = Dispatcher(
            [
                SlowSuccessDestination("talk-main", sleep_seconds=0.08),
                SlowSuccessDestination("hook-main", sleep_seconds=0.01),
            ],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=config.delivery.running_cutoff_grace_seconds,
        )
        service = AlertService(config, dispatcher, use_gpio=False)
        try:
            self.assertTrue(service.handle_button_press("staff"))
            deadline = time.monotonic() + 2.0
            while service._queue_store.pending_count() != 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(service._queue_store.pending_count(), 0)
            self.assertTrue(service._worker_thread.is_alive())
            self.assertIsNone(service._get_fatal_error())
        finally:
            service.shutdown()

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

    def test_abandoned_inflight_completion_persists_progress_after_shutdown(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        raw["delivery"]["max_event_delivery_seconds"] = 0.02
        raw["delivery"]["running_cutoff_grace_seconds"] = 0.01
        raw["delivery"]["persistent_retry_base_seconds"] = 10
        raw["delivery"]["persistent_retry_max_seconds"] = 10
        config = parse_config(raw)
        dispatcher = Dispatcher(
            [
                SlowSuccessDestination("talk-main", sleep_seconds=0.12),
                SlowSuccessDestination("hook-main", sleep_seconds=0.01),
            ],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=config.delivery.running_cutoff_grace_seconds,
        )
        service = AlertService(config, dispatcher, use_gpio=False)
        queue_store = service._queue_store
        try:
            self.assertTrue(service.handle_button_press("staff"))
            fatal_deadline = time.monotonic() + 1.0
            while service._get_fatal_error() is None and time.monotonic() < fatal_deadline:
                time.sleep(0.01)
            self.assertIsNotNone(service._get_fatal_error())
            service.shutdown()
            pending_deadline = time.monotonic() + 1.0
            while queue_store is not None and not queue_store._closed and queue_store.pending_count() != 0 and time.monotonic() < pending_deadline:
                time.sleep(0.02)
            self.assertIsNotNone(queue_store)
            self.assertTrue(queue_store._closed or queue_store.pending_count() == 0)
        finally:
            if service._queue_store is not None:
                service._queue_store.close()

    def test_shutdown_eventually_closes_queue_store_after_detached_completion(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        raw["delivery"]["max_event_delivery_seconds"] = 0.02
        raw["delivery"]["running_cutoff_grace_seconds"] = 0.01
        config = parse_config(raw)
        dispatcher = Dispatcher(
            [
                SlowSuccessDestination("talk-main", sleep_seconds=0.12),
                SlowSuccessDestination("hook-main", sleep_seconds=0.01),
            ],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=config.delivery.running_cutoff_grace_seconds,
            detached_cleanup_timeout_seconds=0.5,
        )
        service = AlertService(config, dispatcher, use_gpio=False)
        queue_store = service._queue_store
        try:
            self.assertTrue(service.handle_button_press("staff"))
            fatal_deadline = time.monotonic() + 1.0
            while service._get_fatal_error() is None and time.monotonic() < fatal_deadline:
                time.sleep(0.01)
            self.assertIsNotNone(service._get_fatal_error())
            service.shutdown()
            close_deadline = time.monotonic() + 1.0
            while service._queue_store is not None and time.monotonic() < close_deadline:
                time.sleep(0.02)
            self.assertIsNone(service._queue_store)
            self.assertIsNotNone(queue_store)
            self.assertTrue(queue_store._closed)
        finally:
            if service._queue_store is not None:
                service._queue_store.close()

    def test_shutdown_closes_queue_store_after_detached_cleanup_timeout(self) -> None:
        raw = make_raw_config()
        raw["timing"]["cooldown_seconds"] = 0
        raw["delivery"]["max_event_delivery_seconds"] = 0.02
        raw["delivery"]["running_cutoff_grace_seconds"] = 0.01
        config = parse_config(raw)
        dispatcher = Dispatcher(
            [
                SlowIgnoringStopDestination("talk-main", [DispatchResult.success("talk-main", 200)], sleep_seconds=1.0),
                SlowSuccessDestination("hook-main", sleep_seconds=0.01),
            ],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=config.delivery.running_cutoff_grace_seconds,
            detached_cleanup_timeout_seconds=0.05,
        )
        service = AlertService(config, dispatcher, use_gpio=False)
        queue_store = service._queue_store
        try:
            self.assertTrue(service.handle_button_press("staff"))
            fatal_deadline = time.monotonic() + 1.0
            while service._get_fatal_error() is None and time.monotonic() < fatal_deadline:
                time.sleep(0.01)
            self.assertIsNotNone(service._get_fatal_error())
            service.shutdown()
            close_deadline = time.monotonic() + 0.5
            while service._queue_store is not None and time.monotonic() < close_deadline:
                time.sleep(0.02)
            self.assertIsNone(service._queue_store)
            self.assertIsNotNone(queue_store)
            self.assertTrue(queue_store._closed)
        finally:
            if service._queue_store is not None:
                service._queue_store.close()

    def test_startup_failure_closes_allocated_leds(self) -> None:
        RecordingLed.instances.clear()
        config = parse_config(make_raw_config())

        with patch("alert_service.GpioLED", RecordingLed), patch("alert_service.GpioButton", RecordingLed), patch(
            "alert_service.SendLedController", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                AlertService(config, BlockingDispatcher(), use_gpio=True, enable_queue_worker=False)

        self.assertGreaterEqual(len(RecordingLed.instances), 2)
        self.assertTrue(all(led.closed for led in RecordingLed.instances[:2]))

    def test_startup_failure_closes_queue_store_and_dispatcher_in_worker_mode(self) -> None:
        config = parse_config(make_raw_config())
        dispatcher = BlockingDispatcher()
        fake_queue = TrackingQueueStore()

        with patch("alert_service.PersistentQueue", return_value=fake_queue), patch(
            "alert_service.SendLedController", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                AlertService(config, dispatcher, use_gpio=False, enable_queue_worker=True)

        self.assertTrue(fake_queue.closed)
        self.assertTrue(dispatcher.closed)
