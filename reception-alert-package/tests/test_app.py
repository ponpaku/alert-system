from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from config import ConfigError, parse_config
from models import build_alert_event
from persistent_queue import PersistentQueue
from tests.test_config import make_raw_config


class FakeService:
    def __init__(self, summary: str):
        self.summary = summary
        self.shutdown_called = False

    def dispatch_test_button(self, button_name: str) -> str:
        return self.summary

    def shutdown(self) -> None:
        self.shutdown_called = True


class RecordingDispatcher:
    def __init__(self):
        self.closed = False
        self.dispatch_calls: list[tuple] = []

    def dispatch(self, *args, **kwargs):
        self.dispatch_calls.append((args, kwargs))
        raise AssertionError("validate helpers must not dispatch alerts")

    def close(self) -> None:
        self.closed = True


class FakeQueueStore:
    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True


class LegacyDispatcherLike:
    def dispatch(self, event, target_names=None, *, stop_event=None, deadline_monotonic=None, deadline_supplier=None):
        return []

    def close(self) -> None:
        pass


class AppTests(unittest.TestCase):
    def test_validation_help_mentions_queue_worker_not_dispatch_workers(self) -> None:
        help_text = app.build_argument_parser().format_help()

        self.assertIn("without sending alerts or starting the queue worker", help_text)
        self.assertNotIn("without starting dispatch workers", help_text)

    def test_test_mode_returns_zero_for_success(self) -> None:
        service = FakeService("success")
        with patch("app.load_config", return_value=object()), patch("app.build_dispatcher", return_value=object()), patch(
            "app.AlertService", return_value=service
        ) as alert_service, patch("sys.argv", ["app.py", "config.toml", "--test", "staff"]):
            with self.assertRaises(SystemExit) as exc:
                app.main()
        self.assertEqual(exc.exception.code, 0)
        self.assertTrue(service.shutdown_called)
        self.assertEqual(alert_service.call_args.kwargs["use_gpio"], False)
        self.assertEqual(alert_service.call_args.kwargs["enable_queue_worker"], False)
        self.assertEqual(alert_service.call_args.kwargs["enable_heartbeat"], False)

    def test_test_mode_returns_two_for_warning(self) -> None:
        service = FakeService("warning")
        with patch("app.load_config", return_value=object()), patch("app.build_dispatcher", return_value=object()), patch(
            "app.AlertService", return_value=service
        ), patch("sys.argv", ["app.py", "config.toml", "--test", "staff"]):
            with self.assertRaises(SystemExit) as exc:
                app.main()
        self.assertEqual(exc.exception.code, 2)
        self.assertTrue(service.shutdown_called)

    def test_test_mode_returns_one_for_failure(self) -> None:
        service = FakeService("failure")
        with patch("app.load_config", return_value=object()), patch("app.build_dispatcher", return_value=object()), patch(
            "app.AlertService", return_value=service
        ), patch("sys.argv", ["app.py", "config.toml", "--test", "staff"]):
            with self.assertRaises(SystemExit) as exc:
                app.main()
        self.assertEqual(exc.exception.code, 1)
        self.assertTrue(service.shutdown_called)

    def test_validate_runtime_calls_runtime_validator(self) -> None:
        config = object()
        with patch("app.load_config", return_value=config), patch("app.validate_runtime") as validate_runtime, patch(
            "sys.argv", ["app.py", "config.toml", "--validate-runtime"]
        ):
            app.main()
        validate_runtime.assert_called_once_with(config)

    def test_validate_gpio_calls_gpio_validator(self) -> None:
        config = object()
        with patch("app.load_config", return_value=config), patch("app.validate_gpio_runtime") as validate_gpio_runtime, patch(
            "sys.argv", ["app.py", "config.toml", "--validate-gpio"]
        ):
            app.main()
        validate_gpio_runtime.assert_called_once_with(config)

    def test_validate_runtime_does_not_dispatch_pending_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "queue.sqlite3"
            config = make_config_with_queue_path(str(path))
            queue = PersistentQueue(
                str(path),
                capacity=config.delivery.queue_capacity,
                retry_base_seconds=config.delivery.persistent_retry_base_seconds,
                retry_max_seconds=config.delivery.persistent_retry_max_seconds,
            )
            queue.enqueue(make_event(), ("talk-main",))
            queue.close()

            dispatcher = RecordingDispatcher()
            with patch("app.build_dispatcher", return_value=dispatcher):
                app.validate_runtime(config)

            reopened = PersistentQueue(
                str(path),
                capacity=config.delivery.queue_capacity,
                retry_base_seconds=config.delivery.persistent_retry_base_seconds,
                retry_max_seconds=config.delivery.persistent_retry_max_seconds,
            )
            try:
                self.assertEqual(reopened.pending_count(), 1)
            finally:
                reopened.close()
            self.assertEqual(dispatcher.dispatch_calls, [])
            self.assertTrue(dispatcher.closed)

    def test_validate_runtime_recovers_processing_rows_like_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "queue.sqlite3"
            config = make_config_with_queue_path(str(path))
            queue = PersistentQueue(
                str(path),
                capacity=config.delivery.queue_capacity,
                retry_base_seconds=config.delivery.persistent_retry_base_seconds,
                retry_max_seconds=config.delivery.persistent_retry_max_seconds,
            )
            queue.enqueue(make_event(), ("talk-main",))
            claimed = queue.claim_next_ready()
            self.assertIsNotNone(claimed)
            queue.close()

            dispatcher = RecordingDispatcher()
            with patch("app.build_dispatcher", return_value=dispatcher):
                app.validate_runtime(config)

            reopened = PersistentQueue(
                str(path),
                capacity=config.delivery.queue_capacity,
                retry_base_seconds=config.delivery.persistent_retry_base_seconds,
                retry_max_seconds=config.delivery.persistent_retry_max_seconds,
                recover_processing_rows=False,
            )
            try:
                recovered = reopened.claim_next_ready()
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered.event.event_id, claimed.event.event_id)
            finally:
                reopened.close()

    def test_validate_runtime_detects_unwritable_queue_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config_with_queue_path(tmpdir)
            dispatcher = RecordingDispatcher()
            with patch("app.build_dispatcher", return_value=dispatcher):
                with self.assertRaises(sqlite3.OperationalError):
                    app.validate_runtime(config)
            self.assertTrue(dispatcher.closed)

    def test_validate_runtime_detects_dispatcher_contract_mismatch(self) -> None:
        config = parse_config(make_raw_config())
        dispatcher = LegacyDispatcherLike()
        with patch("app.build_dispatcher", return_value=dispatcher):
            with self.assertRaisesRegex(ConfigError, "result_handler"):
                app.validate_runtime(config)

    def test_validate_gpio_runtime_uses_gpio_startup_without_worker(self) -> None:
        config = parse_config(make_raw_config())
        dispatcher = RecordingDispatcher()
        fake_queue = FakeQueueStore()
        fake_service = FakeService("success")
        with patch("app.build_dispatcher", return_value=dispatcher), patch(
            "app.open_validation_queue_store", return_value=fake_queue
        ) as open_queue_store, patch("app.AlertService", return_value=fake_service) as alert_service:
            app.validate_gpio_runtime(config)
        open_queue_store.assert_called_once_with(config)
        self.assertEqual(alert_service.call_args.kwargs["use_gpio"], True)
        self.assertEqual(alert_service.call_args.kwargs["enable_queue_worker"], False)
        self.assertEqual(alert_service.call_args.kwargs["enable_heartbeat"], False)
        self.assertTrue(fake_service.shutdown_called)
        self.assertTrue(fake_queue.closed)

    def test_validate_gpio_runtime_does_not_dispatch_pending_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "queue.sqlite3"
            config = make_config_with_queue_path(str(path))
            queue = PersistentQueue(
                str(path),
                capacity=config.delivery.queue_capacity,
                retry_base_seconds=config.delivery.persistent_retry_base_seconds,
                retry_max_seconds=config.delivery.persistent_retry_max_seconds,
            )
            queue.enqueue(make_event(), ("talk-main",))
            queue.close()

            dispatcher = RecordingDispatcher()
            fake_service = FakeService("success")
            with patch("app.build_dispatcher", return_value=dispatcher), patch("app.AlertService", return_value=fake_service):
                app.validate_gpio_runtime(config)

            reopened = PersistentQueue(
                str(path),
                capacity=config.delivery.queue_capacity,
                retry_base_seconds=config.delivery.persistent_retry_base_seconds,
                retry_max_seconds=config.delivery.persistent_retry_max_seconds,
            )
            try:
                self.assertEqual(reopened.pending_count(), 1)
            finally:
                reopened.close()
            self.assertEqual(dispatcher.dispatch_calls, [])
            self.assertTrue(fake_service.shutdown_called)

    def test_validate_gpio_detects_unwritable_queue_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config_with_queue_path(tmpdir)
            dispatcher = RecordingDispatcher()
            with patch("app.build_dispatcher", return_value=dispatcher):
                with self.assertRaises(sqlite3.OperationalError):
                    app.validate_gpio_runtime(config)
            self.assertTrue(dispatcher.closed)

    def test_validate_gpio_detects_dispatcher_contract_mismatch(self) -> None:
        config = parse_config(make_raw_config())
        dispatcher = LegacyDispatcherLike()
        with patch("app.build_dispatcher", return_value=dispatcher):
            with self.assertRaisesRegex(ConfigError, "result_handler"):
                app.validate_gpio_runtime(config)

    def test_main_prints_friendly_config_error_instead_of_traceback(self) -> None:
        stderr = io.StringIO()
        with patch("app.load_config", side_effect=ConfigError("button 'staff' references unknown destinations: discord-ops")), patch(
            "sys.argv", ["app.py", "config.toml", "--test", "staff"]
        ), patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as exc:
                app.main()
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "設定エラー: button 'staff' references unknown destinations: discord-ops",
        )


def make_config_with_queue_path(path: str):
    raw = make_raw_config()
    raw["delivery"]["persistent_queue_path"] = path
    return parse_config(raw)


def make_event():
    return build_alert_event(
        button_name="staff",
        kind="alert",
        prefix="prefix",
        message="message",
        location_name="frontdesk",
    )
