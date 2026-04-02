from __future__ import annotations

import unittest
from unittest.mock import patch

import app


class FakeService:
    def __init__(self, summary: str):
        self.summary = summary
        self.shutdown_called = False

    def dispatch_test_button(self, button_name: str) -> str:
        return self.summary

    def shutdown(self) -> None:
        self.shutdown_called = True


class AppTests(unittest.TestCase):
    def test_test_mode_returns_zero_for_success(self) -> None:
        service = FakeService("success")
        with patch("app.load_config", return_value=object()), patch("app.build_dispatcher", return_value=object()), patch(
            "app.AlertService", return_value=service
        ), patch("sys.argv", ["app.py", "config.toml", "--test", "staff"]):
            with self.assertRaises(SystemExit) as exc:
                app.main()
        self.assertEqual(exc.exception.code, 0)
        self.assertTrue(service.shutdown_called)

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
