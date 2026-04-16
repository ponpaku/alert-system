from __future__ import annotations

import time
import threading
import unittest
from unittest.mock import patch

from send_led_controller import SendLedController


class FakeLed:
    def __init__(self):
        self.actions: list[str] = []
        self.closed = False

    def on(self) -> None:
        self.actions.append("on")

    def off(self) -> None:
        self.actions.append("off")

    def close(self) -> None:
        self.closed = True
        self.actions.append("close")


class FakePwmLed(FakeLed):
    def __init__(self):
        super().__init__()
        self.value = 0.0


class ShutdownDuringStartThread:
    def __init__(self, controller: SendLedController, target=None, args=(), kwargs=None, **ignored):
        self._controller = controller
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.started = False

    def start(self) -> None:
        self.started = True
        self._controller.shutdown(close_led=False)

    def join(self, timeout=None) -> None:
        if not self.started:
            raise RuntimeError("cannot join thread before it is started")


class SendLedControllerTests(unittest.TestCase):
    def test_success_hold_turns_led_off_after_delay(self) -> None:
        led = FakeLed()
        controller = SendLedController(led, stop_event=threading.Event())
        controller.show_success_hold(0.05)
        time.sleep(0.12)
        self.assertIn("on", led.actions)
        self.assertIn("off", led.actions)

    def test_activity_blink_can_be_stopped(self) -> None:
        led = FakeLed()
        controller = SendLedController(led, stop_event=threading.Event())
        controller.start_activity_blink(on_sec=0.02, off_sec=0.02)
        time.sleep(0.08)
        controller.stop_activity_blink()
        time.sleep(0.05)
        self.assertGreaterEqual(led.actions.count("on"), 1)
        self.assertEqual(led.actions[-1], "off")

    def test_failure_blink_can_be_replaced_without_long_block(self) -> None:
        led = FakeLed()
        controller = SendLedController(led, stop_event=threading.Event())
        controller.show_failure_blink(1.5)
        start = time.monotonic()
        controller.show_success_hold(0.05)
        elapsed = time.monotonic() - start
        time.sleep(0.12)
        self.assertLess(elapsed, 0.2)
        self.assertIn("off", led.actions)

    def test_shutdown_without_close_prevents_future_operations(self) -> None:
        led = FakeLed()
        controller = SendLedController(led, stop_event=threading.Event())
        controller.show_success_hold(0.2)
        controller.shutdown(close_led=False)
        before = list(led.actions)
        controller.show_failure_blink(0.2)
        controller.start_activity_blink()
        self.assertFalse(led.closed)
        self.assertEqual(led.actions, before)
        self.assertTrue(controller.is_closed)

    def test_shutdown_with_close_closes_led(self) -> None:
        led = FakeLed()
        controller = SendLedController(led, stop_event=threading.Event())
        controller.shutdown(close_led=True)
        self.assertTrue(led.closed)

    def test_shutdown_during_activity_start_does_not_join_unstarted_thread(self) -> None:
        led = FakeLed()
        controller = SendLedController(led, stop_event=threading.Event())
        with patch(
            "send_led_controller.threading.Thread",
            side_effect=lambda *args, **kwargs: ShutdownDuringStartThread(controller, *args, **kwargs),
        ):
            controller.start_activity_blink()
        self.assertTrue(controller.is_closed)
        self.assertFalse(led.closed)

    def test_shutdown_during_failure_start_does_not_join_unstarted_thread(self) -> None:
        led = FakeLed()
        controller = SendLedController(led, stop_event=threading.Event())
        with patch(
            "send_led_controller.threading.Thread",
            side_effect=lambda *args, **kwargs: ShutdownDuringStartThread(controller, *args, **kwargs),
        ):
            controller.show_failure_blink(0.2)
        self.assertTrue(controller.is_closed)
        self.assertFalse(led.closed)

    def test_brightness_uses_pwm_value_when_supported(self) -> None:
        led = FakePwmLed()
        controller = SendLedController(led, stop_event=threading.Event(), brightness=0.35, use_pwm=True)
        controller.show_success_hold(0.01)
        time.sleep(0.03)
        self.assertEqual(led.value, 0.35)
        self.assertNotIn("on", led.actions)

    def test_value_attribute_without_pwm_mode_falls_back_to_on(self) -> None:
        led = FakePwmLed()
        controller = SendLedController(led, stop_event=threading.Event(), brightness=0.35, use_pwm=False)
        controller.show_success_hold(0.01)
        time.sleep(0.03)
        self.assertIn("on", led.actions)
