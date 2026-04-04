from __future__ import annotations

import logging
import inspect
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any

from config import AppConfig, ButtonConfig, ConfigError
from dispatcher import Dispatcher
from message_constants import TEST_PREFIX
from models import AlertEvent, DispatchSummary, build_alert_event, summarize_dispatch_results
from persistent_queue import PersistedAlert, PersistentQueue, QueueFullError
from send_led_controller import SendLedController

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
    target_names: tuple[str, ...]


class AlertService:
    def __init__(
        self,
        config: AppConfig,
        dispatcher: Dispatcher,
        *,
        use_gpio: bool = True,
        enable_queue_worker: bool = True,
    ):
        self.config = config
        self.dispatcher = dispatcher
        self._dispatcher_supports_result_handler = _supports_result_handler(dispatcher.dispatch)
        if enable_queue_worker and not self._dispatcher_supports_result_handler:
            raise ConfigError("persistent queue mode requires a dispatcher that supports result_handler")
        if use_gpio and (GpioLED is None or GpioButton is None):
            raise ConfigError("gpiozero support is required for normal service startup")
        self.use_gpio = use_gpio
        self.enable_queue_worker = enable_queue_worker
        self._stop_event = threading.Event()
        self._work_available = threading.Event()
        self._fatal_error_lock = threading.Lock()
        self._fatal_error: BaseException | None = None
        self._queue_store_close_lock = threading.Lock()
        self._queue_store: PersistentQueue | None = None
        self._worker_thread: threading.Thread | None = None
        if self.enable_queue_worker:
            self._queue_store = PersistentQueue(
                config.delivery.persistent_queue_path,
                capacity=config.delivery.queue_capacity,
                retry_base_seconds=config.delivery.persistent_retry_base_seconds,
                retry_max_seconds=config.delivery.persistent_retry_max_seconds,
            )
            self._worker_thread = threading.Thread(target=self._worker_entrypoint, name="alert-worker", daemon=True)
            self._register_detached_cleanup_callback()
        self._accept_lock = threading.Lock()
        self._shutdown_deadline_lock = threading.Lock()
        self._last_accepted_monotonic = 0.0
        self._shutdown_deadline_monotonic: float | None = None
        self.buttons = []
        self.alive_led = None
        self.send_led = None
        self.send_led_controller = None
        try:
            led_factory = GpioLED if self.use_gpio else NoopLED
            self.alive_led = led_factory(config.gpio.alive_led_gpio)
            self.send_led = led_factory(config.gpio.send_led_gpio)
            self.send_led_controller = SendLedController(self.send_led, stop_event=self._stop_event)
            if self.use_gpio:
                for button in config.buttons:
                    gpio_button = GpioButton(button.gpio, pull_up=True, bounce_time=config.timing.bounce_seconds)
                    gpio_button.when_pressed = lambda button_name=button.name: self.handle_button_press(button_name)
                    self.buttons.append(gpio_button)
            if self._worker_thread is not None:
                self._worker_thread.start()
        except Exception:
            self._cleanup_startup_failure()
            raise

    def handle_button_press(self, button_name: str) -> bool:
        if self._stop_event.is_set():
            logging.warning("Rejected button press during shutdown: %s", button_name)
            return False
        if not self.enable_queue_worker:
            logging.error("Rejected button press because queue worker is disabled: %s", button_name)
            return False
        if not self._worker_is_alive() or self._get_fatal_error() is not None:
            logging.error("Rejected button press because alert worker is unavailable: %s", button_name)
            return False
        button = self.config.button_by_name(button_name)
        with self._accept_lock:
            if not self._worker_is_alive() or self._get_fatal_error() is not None:
                logging.error("Rejected button press because alert worker is unavailable: %s", button_name)
                return False
            now = time.monotonic()
            if now - self._last_accepted_monotonic < self.config.timing.cooldown_seconds:
                logging.warning("Ignored press due to cooldown: %s", button.name)
                return False
            queued_alert = QueuedAlert(
                event=self._build_event(button, kind="alert"),
                target_names=tuple(self.dispatcher.resolve_target_names(button.destinations)),
            )
            try:
                self._require_queue_store().enqueue(queued_alert.event, queued_alert.target_names)
            except QueueFullError:
                logging.warning("Dropped button press due to queue overflow: %s", button.name)
                return False
            except Exception as exc:
                logging.exception("Failed to persist alert event event_id=%s", queued_alert.event.event_id)
                self._set_fatal_error(exc)
                return False
            self._last_accepted_monotonic = now
        self._work_available.set()
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
                self._raise_if_fatal_error()
                time.sleep(1)
        finally:
            self.shutdown()
        self._raise_if_fatal_error()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._work_available.set()
        with self._shutdown_deadline_lock:
            if self._shutdown_deadline_monotonic is None:
                self._shutdown_deadline_monotonic = time.monotonic() + self.config.delivery.shutdown_grace_seconds
        worker_thread = self._worker_thread
        if worker_thread is not None:
            worker_thread.join(timeout=self._shutdown_join_timeout_seconds())
        worker_alive = self._worker_is_alive()
        keep_queue_store_open = self._dispatcher_has_detached_inflight_work()
        for button in self.buttons:
            button.close()
        if self.send_led_controller is not None:
            self.send_led_controller.shutdown(close_led=not worker_alive)
        if not worker_alive:
            if self.alive_led is not None:
                self.alive_led.off()
                self.alive_led.close()
            dispatcher_close = getattr(self.dispatcher, "close", None)
            if callable(dispatcher_close):
                dispatcher_close()
            keep_queue_store_open = keep_queue_store_open or self._dispatcher_has_detached_inflight_work()
            if self._queue_store is not None and not keep_queue_store_open:
                self._close_queue_store_once()
        if worker_alive:
            logging.warning("Worker did not stop within shutdown grace; skipped LED close to avoid post-close GPIO access")
        elif keep_queue_store_open:
            logging.warning("Skipped queue-store close because detached in-flight dispatch work is still finishing")
        logging.info("Shutdown complete")

    def _signal_handler(self, signum: int, frame: Any) -> None:
        logging.info("Received signal %s, stopping", signum)
        with self._shutdown_deadline_lock:
            if self._shutdown_deadline_monotonic is None:
                self._shutdown_deadline_monotonic = time.monotonic() + self.config.delivery.shutdown_grace_seconds
        self._stop_event.set()
        self._work_available.set()

    def _worker_entrypoint(self) -> None:
        try:
            self._worker_loop()
        except BaseException as exc:
            logging.exception("Alert worker crashed")
            self._set_fatal_error(exc)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            queued_alert = self._require_queue_store().claim_next_ready()
            if queued_alert is None:
                self._work_available.wait(timeout=0.2)
                self._work_available.clear()
                continue
            dispatch_deadline = time.monotonic() + self.config.delivery.max_event_delivery_seconds
            self.send_led_controller.start_activity_blink()
            try:
                dispatch_kwargs: dict[str, Any] = {
                    "stop_event": self._stop_event,
                    "deadline_supplier": lambda dispatch_deadline=dispatch_deadline: self._combined_deadline(dispatch_deadline),
                }
                if self._dispatcher_supports_result_handler:
                    dispatch_kwargs["result_handler"] = (
                        lambda result, record_id=queued_alert.record_id: self._persist_destination_progress(record_id, result)
                    )
                results = self.dispatcher.dispatch(
                    queued_alert.event,
                    target_names=list(queued_alert.target_names),
                    **dispatch_kwargs,
                )
            finally:
                self.send_led_controller.stop_activity_blink()
            self._finalize_record(queued_alert, results)
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
            self.send_led_controller.show_success_hold(self.config.timing.success_hold_seconds)
            return
        self.send_led_controller.show_failure_blink(self.config.timing.failure_blink_seconds)

    def _get_shutdown_deadline(self) -> float | None:
        with self._shutdown_deadline_lock:
            return self._shutdown_deadline_monotonic

    def _shutdown_join_timeout_seconds(self) -> float:
        return max(self.config.delivery.shutdown_grace_seconds, self.config.http.request_timeout_seconds) + 1.0

    def _combined_deadline(self, dispatch_deadline: float) -> float:
        shutdown_deadline = self._get_shutdown_deadline()
        if shutdown_deadline is None:
            return dispatch_deadline
        return min(dispatch_deadline, shutdown_deadline)

    def _finalize_record(self, queued_alert: PersistedAlert, results: list[Any]) -> None:
        remaining_targets = self._require_queue_store().current_targets(queued_alert.record_id)
        if not remaining_targets:
            self._require_queue_store().complete_success(queued_alert.record_id)
            return
        delay_seconds = (
            0.0
            if self._stop_event.is_set()
            else self._require_queue_store().compute_retry_delay_seconds(queued_alert.attempt_count + 1)
        )
        error_summary = "; ".join(
            filter(
                None,
                [
                    f"{result.destination_name}: {result.error_summary}"
                    for result in results
                    if result.outcome != "success" and result.error_summary
                ],
            )
        )
        self._require_queue_store().requeue(
            queued_alert.record_id,
            target_names=remaining_targets,
            error_summary=error_summary or None,
            delay_seconds=delay_seconds,
        )
        if delay_seconds <= 0:
            self._work_available.set()

    def _set_fatal_error(self, exc: BaseException) -> None:
        with self._fatal_error_lock:
            if self._fatal_error is None:
                self._fatal_error = exc
        self._stop_event.set()
        self._work_available.set()

    def _get_fatal_error(self) -> BaseException | None:
        with self._fatal_error_lock:
            return self._fatal_error

    def _raise_if_fatal_error(self) -> None:
        fatal_error = self._get_fatal_error()
        if fatal_error is not None:
            raise RuntimeError("alert worker failed") from fatal_error

    def _worker_is_alive(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    def _require_queue_store(self) -> PersistentQueue:
        if self._queue_store is None:
            raise RuntimeError("persistent queue is not enabled")
        return self._queue_store

    def _should_requeue_result(self, result: Any) -> bool:
        if result.outcome == "failed":
            return bool(result.retryable)
        if result.outcome != "not_attempted":
            return False
        return result.error_summary in {
            "stopped before request start",
            "deadline exceeded before request start",
        }

    def _persist_destination_progress(self, record_id: int, result: Any) -> None:
        self._require_queue_store().mark_processed_destination(
            record_id,
            destination_name=result.destination_name,
            keep_for_retry=self._should_requeue_result(result),
            error_summary=result.error_summary,
        )

    def _dispatcher_has_detached_inflight_work(self) -> bool:
        dispatcher_check = getattr(self.dispatcher, "has_detached_inflight_work", None)
        if not callable(dispatcher_check):
            return False
        try:
            return bool(dispatcher_check())
        except Exception:
            logging.exception("Failed to query dispatcher detached in-flight work state")
            return False

    def _register_detached_cleanup_callback(self) -> None:
        register_callback = getattr(self.dispatcher, "register_detached_cleanup_callback", None)
        if not callable(register_callback):
            return
        try:
            register_callback(self._on_detached_cleanup_finished)
        except Exception:
            logging.exception("Failed to register detached cleanup callback")

    def _on_detached_cleanup_finished(self, timed_out: bool) -> None:
        if timed_out:
            logging.error("Detached dispatch cleanup timed out; closing queue store without waiting for late completions")
        self._close_queue_store_once()

    def _close_queue_store_once(self) -> None:
        with self._queue_store_close_lock:
            queue_store = self._queue_store
            if queue_store is None:
                return
            self._queue_store = None
        queue_store.close()

    def _cleanup_startup_failure(self) -> None:
        self._stop_event.set()
        self._work_available.set()
        for button in self.buttons:
            try:
                button.close()
            except Exception:
                logging.exception("Failed to close button during startup cleanup")
        if self.send_led_controller is not None:
            try:
                self.send_led_controller.shutdown(close_led=True)
            except Exception:
                logging.exception("Failed to close send LED controller during startup cleanup")
        elif self.send_led is not None:
            try:
                self.send_led.close()
            except Exception:
                logging.exception("Failed to close send LED during startup cleanup")
        if self.alive_led is not None:
            try:
                self.alive_led.off()
                self.alive_led.close()
            except Exception:
                logging.exception("Failed to close alive LED during startup cleanup")
        try:
            self._close_queue_store_once()
        except Exception:
            logging.exception("Failed to close queue store during startup cleanup")
        dispatcher_close = getattr(self.dispatcher, "close", None)
        if callable(dispatcher_close):
            try:
                dispatcher_close()
            except Exception:
                logging.exception("Failed to close dispatcher during startup cleanup")


def _supports_result_handler(dispatch_callable: Any) -> bool:
    try:
        signature = inspect.signature(dispatch_callable)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters.values()
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return True
    return "result_handler" in signature.parameters
