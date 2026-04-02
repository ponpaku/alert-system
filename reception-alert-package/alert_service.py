from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any

from config import AppConfig, ButtonConfig, ConfigError
from dispatcher import Dispatcher
from message_constants import TEST_PREFIX
from models import AlertEvent, DispatchSummary, build_alert_event, summarize_dispatch_results

try:
    from gpiozero import Button as GpioButton
    from gpiozero import Device
    from gpiozero import LED as GpioLED
except ModuleNotFoundError:  # pragma: no cover
    GpioButton = None
    Device = None
    GpioLED = None


class NoopLED:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def on(self) -> None:
        pass

    def off(self) -> None:
        pass

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class QueuedAlert:
    event: AlertEvent
    target_names: tuple[str, ...] | None


class AlertService:
    def __init__(self, config: AppConfig, dispatcher: Dispatcher, *, use_gpio: bool = True):
        self.config = config
        self.dispatcher = dispatcher
        if use_gpio and (GpioLED is None or GpioButton is None):
            raise ConfigError("gpiozero support is required for normal service startup")
        self.use_gpio = use_gpio
        self._stop_event = threading.Event()
        self._queue: queue.Queue[QueuedAlert] = queue.Queue(maxsize=config.delivery.queue_capacity)
        self._worker_thread = threading.Thread(target=self._worker_loop, name="alert-worker", daemon=True)
        self._send_led_timer: threading.Timer | None = None
        self._send_led_timer_lock = threading.Lock()
        self._failure_blink_thread: threading.Thread | None = None
        self._failure_blink_stop = threading.Event()
        self._failure_blink_lock = threading.Lock()
        self._accept_lock = threading.Lock()
        self._led_lock = threading.Lock()
        self._shutdown_deadline_lock = threading.Lock()
        self._last_accepted_monotonic = 0.0
        self._shutdown_deadline_monotonic: float | None = None
        self._leds_closed = False
        led_factory = GpioLED if self.use_gpio else NoopLED
        self.alive_led = led_factory(config.gpio.alive_led_gpio)
        self.send_led = led_factory(config.gpio.send_led_gpio)
        self.buttons = []
        if self.use_gpio:
            for button in config.buttons:
                gpio_button = GpioButton(button.gpio, pull_up=True, bounce_time=config.timing.bounce_seconds)
                gpio_button.when_pressed = lambda button_name=button.name: self.handle_button_press(button_name)
                self.buttons.append(gpio_button)
        self._worker_thread.start()

    def handle_button_press(self, button_name: str) -> bool:
        if self._stop_event.is_set():
            logging.warning("Rejected button press during shutdown: %s", button_name)
            return False
        button = self.config.button_by_name(button_name)
        with self._accept_lock:
            now = time.monotonic()
            if now - self._last_accepted_monotonic < self.config.timing.cooldown_seconds:
                logging.warning("Ignored press due to cooldown: %s", button.name)
                return False
            queued_alert = QueuedAlert(
                event=self._build_event(button, kind="alert"),
                target_names=tuple(button.destinations) if button.destinations is not None else None,
            )
            try:
                self._queue.put_nowait(queued_alert)
            except queue.Full:
                logging.warning("Dropped button press due to queue overflow: %s", button.name)
                return False
            self._last_accepted_monotonic = now
        logging.info("Accepted button press: %s event_id=%s", button.name, queued_alert.event.event_id)
        return True

    def dispatch_test_button(self, button_name: str) -> DispatchSummary:
        button = self.config.button_by_name(button_name)
        event = self._build_event(button, kind="test")
        results = self.dispatcher.dispatch(event, target_names=list(button.destinations) if button.destinations is not None else None)
        summary = summarize_dispatch_results(results)
        logging.info("dispatch summary event_id=%s button_name=%s summary=%s", event.event_id, event.button_name, summary)
        return summary

    def run(self) -> None:
        if Device is not None:
            logging.info("Pin factory: %s", Device.pin_factory)
        logging.info("Ready with %d buttons", len(self.buttons))
        self.alive_led.on()
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._shutdown_deadline_lock:
            if self._shutdown_deadline_monotonic is None:
                self._shutdown_deadline_monotonic = time.monotonic() + self.config.delivery.shutdown_grace_seconds
        self._cancel_send_led_timer()
        self._cancel_failure_blink()
        self._worker_thread.join(timeout=self._shutdown_join_timeout_seconds())
        worker_alive = self._worker_thread.is_alive()
        for button in self.buttons:
            button.close()
        with self._led_lock:
            self._leds_closed = True
            if not worker_alive:
                self.send_led.close()
                self.alive_led.close()
        if worker_alive:
            logging.warning("Worker did not stop within shutdown grace; skipped LED close to avoid post-close GPIO access")
        logging.info("Shutdown complete")

    def _signal_handler(self, signum: int, frame: Any) -> None:
        logging.info("Received signal %s, stopping", signum)
        with self._shutdown_deadline_lock:
            if self._shutdown_deadline_monotonic is None:
                self._shutdown_deadline_monotonic = time.monotonic() + self.config.delivery.shutdown_grace_seconds
        self._stop_event.set()

    def _worker_loop(self) -> None:
        while True:
            if self._stop_event.is_set():
                self._drain_queue_without_processing()
                return
            try:
                queued_alert = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop_event.is_set():
                logging.warning("Discarded queued event during shutdown: %s", queued_alert.event.button_name)
                self._queue.task_done()
                self._drain_queue_without_processing()
                return
            self._cancel_send_led_timer()
            blink_stop = threading.Event()
            blink_thread = threading.Thread(target=self._blink_send_led_until, args=(blink_stop, 0.15, 0.15), daemon=True)
            blink_thread.start()
            try:
                results = self.dispatcher.dispatch(
                    queued_alert.event,
                    target_names=list(queued_alert.target_names) if queued_alert.target_names is not None else None,
                    stop_event=self._stop_event,
                    deadline_supplier=self._get_shutdown_deadline,
                )
            finally:
                blink_stop.set()
                blink_thread.join(timeout=1.0)
                self._queue.task_done()
            summary = summarize_dispatch_results(results)
            logging.info(
                "dispatch summary event_id=%s button_name=%s summary=%s",
                queued_alert.event.event_id,
                queued_alert.event.button_name,
                summary,
            )
            self._apply_led_summary(summary)

    def _build_event(self, button: ButtonConfig, *, kind: str) -> AlertEvent:
        prefix = TEST_PREFIX if kind == "test" else button.prefix
        return build_alert_event(
            button_name=button.name,
            kind=kind,
            prefix=prefix,
            message=button.message,
            location_name=self.config.location_name,
        )

    def _apply_led_summary(self, summary: DispatchSummary) -> None:
        if summary == "success":
            self._cancel_failure_blink()
            self._set_send_led_success_hold(self.config.timing.success_hold_seconds)
            return
        self._start_failure_blink(self.config.timing.failure_blink_seconds)

    def _cancel_send_led_timer(self) -> None:
        with self._send_led_timer_lock:
            if self._send_led_timer is not None:
                self._send_led_timer.cancel()
                self._send_led_timer = None

    def _cancel_failure_blink(self) -> None:
        with self._failure_blink_lock:
            self._failure_blink_stop.set()
            thread = self._failure_blink_thread
            self._failure_blink_thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        self._failure_blink_stop = threading.Event()

    def _start_failure_blink(self, seconds: float) -> None:
        self._cancel_send_led_timer()
        self._cancel_failure_blink()
        if seconds <= 0:
            self._safe_send_led_off()
            return
        stop_event = self._failure_blink_stop
        thread = threading.Thread(target=self._blink_failure_until, args=(stop_event, seconds), daemon=True)
        with self._failure_blink_lock:
            self._failure_blink_thread = thread
        thread.start()

    def _safe_send_led_on(self) -> None:
        with self._led_lock:
            if self._leds_closed:
                return
            self.send_led.on()

    def _safe_send_led_off(self) -> None:
        with self._led_lock:
            if self._leds_closed:
                return
            self.send_led.off()

    def _set_send_led_success_hold(self, seconds: float) -> None:
        self._cancel_send_led_timer()
        self._safe_send_led_on()
        timer = threading.Timer(seconds, self._safe_send_led_off)
        timer.daemon = True
        with self._send_led_timer_lock:
            self._send_led_timer = timer
        timer.start()

    def _blink_send_led_until(self, stop_event: threading.Event, on_sec: float, off_sec: float) -> None:
        while not stop_event.is_set() and not self._stop_event.is_set():
            self._safe_send_led_on()
            if stop_event.wait(on_sec) or self._stop_event.is_set():
                break
            self._safe_send_led_off()
            if stop_event.wait(off_sec) or self._stop_event.is_set():
                break
        self._safe_send_led_off()

    def _blink_failure_until(self, stop_event: threading.Event, seconds: float) -> None:
        end_at = time.monotonic() + seconds
        while time.monotonic() < end_at and not self._stop_event.is_set() and not stop_event.is_set():
            self._safe_send_led_on()
            if stop_event.wait(0.5) or self._stop_event.wait(0.0):
                break
            self._safe_send_led_off()
            if stop_event.wait(0.5) or self._stop_event.wait(0.0):
                break
        self._safe_send_led_off()

    def _drain_queue_without_processing(self) -> None:
        while True:
            try:
                queued_alert = self._queue.get_nowait()
            except queue.Empty:
                return
            logging.warning("Discarded queued event during shutdown: %s", queued_alert.event.button_name)
            self._queue.task_done()

    def _get_shutdown_deadline(self) -> float | None:
        with self._shutdown_deadline_lock:
            return self._shutdown_deadline_monotonic

    def _shutdown_join_timeout_seconds(self) -> float:
        return max(self.config.delivery.shutdown_grace_seconds, self.config.http.request_timeout_seconds) + 1.0
