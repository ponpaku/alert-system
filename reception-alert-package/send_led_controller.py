from __future__ import annotations

import threading
import time
from typing import Any


class SendLedController:
    def __init__(self, led: Any, *, stop_event: threading.Event):
        self._led = led
        self._stop_event = stop_event
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False
        self._success_timer: threading.Timer | None = None
        self._activity_thread: threading.Thread | None = None
        self._activity_stop: threading.Event | None = None
        self._failure_thread: threading.Thread | None = None
        self._failure_stop: threading.Event | None = None

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def start_activity_blink(self, on_sec: float = 0.15, off_sec: float = 0.15) -> None:
        with self._lock:
            if self._closed:
                return
            token = self._begin_mode_transition_locked()
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_activity_blink,
                args=(token, stop_event, on_sec, off_sec),
                name="send-led-activity",
                daemon=True,
            )
        thread.start()
        with self._lock:
            if self._closed or self._generation != token:
                stop_event.set()
                return
            self._activity_stop = stop_event
            self._activity_thread = thread

    def stop_activity_blink(self) -> None:
        with self._lock:
            self._stop_activity_locked()
            self._generation += 1
            self._safe_led_off_locked()

    def show_success_hold(self, seconds: float) -> None:
        with self._lock:
            if self._closed:
                return
            token = self._begin_mode_transition_locked()
            if seconds <= 0:
                self._safe_led_off_locked()
                return
            self._safe_led_on_locked()
            timer = threading.Timer(seconds, self._finish_success_hold, args=(token,))
            timer.daemon = True
            self._success_timer = timer
        timer.start()

    def show_failure_blink(self, seconds: float) -> None:
        with self._lock:
            if self._closed:
                return
            token = self._begin_mode_transition_locked()
            if seconds <= 0:
                self._safe_led_off_locked()
                return
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_failure_blink,
                args=(token, stop_event, seconds),
                name="send-led-failure",
                daemon=True,
            )
        thread.start()
        with self._lock:
            if self._closed or self._generation != token:
                stop_event.set()
                return
            self._failure_stop = stop_event
            self._failure_thread = thread

    def shutdown(self, *, close_led: bool) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._cancel_success_timer_locked()
            activity_thread = self._activity_thread
            failure_thread = self._failure_thread
            self._stop_activity_locked()
            self._stop_failure_locked()
            if close_led:
                self._led.close()
        for thread in (activity_thread, failure_thread):
            if thread is not None:
                thread.join(timeout=0.6)

    def _begin_mode_transition_locked(self) -> int:
        self._generation += 1
        self._cancel_success_timer_locked()
        self._stop_activity_locked()
        self._stop_failure_locked()
        return self._generation

    def _cancel_success_timer_locked(self) -> None:
        if self._success_timer is not None:
            self._success_timer.cancel()
            self._success_timer = None

    def _stop_activity_locked(self) -> None:
        if self._activity_stop is not None:
            self._activity_stop.set()
        self._activity_stop = None
        self._activity_thread = None

    def _stop_failure_locked(self) -> None:
        if self._failure_stop is not None:
            self._failure_stop.set()
        self._failure_stop = None
        self._failure_thread = None

    def _safe_led_on_locked(self) -> None:
        if self._closed:
            return
        self._led.on()

    def _safe_led_off_locked(self) -> None:
        if self._closed:
            return
        self._led.off()

    def _is_active_generation(self, token: int) -> bool:
        with self._lock:
            return not self._closed and self._generation == token

    def _finish_success_hold(self, token: int) -> None:
        with self._lock:
            if self._closed or self._generation != token:
                return
            self._success_timer = None
            self._safe_led_off_locked()

    def _run_activity_blink(self, token: int, stop_event: threading.Event, on_sec: float, off_sec: float) -> None:
        while self._is_active_generation(token) and not stop_event.is_set() and not self._stop_event.is_set():
            with self._lock:
                if self._closed or self._generation != token:
                    return
                self._safe_led_on_locked()
            if stop_event.wait(on_sec) or self._stop_event.is_set():
                break
            if not self._is_active_generation(token):
                return
            with self._lock:
                if self._closed or self._generation != token:
                    return
                self._safe_led_off_locked()
            if stop_event.wait(off_sec) or self._stop_event.is_set():
                break
        with self._lock:
            if self._closed or self._generation != token:
                return
            self._safe_led_off_locked()

    def _run_failure_blink(self, token: int, stop_event: threading.Event, seconds: float) -> None:
        end_at = time.monotonic() + seconds
        while time.monotonic() < end_at and self._is_active_generation(token) and not stop_event.is_set() and not self._stop_event.is_set():
            with self._lock:
                if self._closed or self._generation != token:
                    return
                self._safe_led_on_locked()
            if stop_event.wait(0.5) or self._stop_event.is_set():
                break
            if not self._is_active_generation(token):
                return
            with self._lock:
                if self._closed or self._generation != token:
                    return
                self._safe_led_off_locked()
            if stop_event.wait(0.5) or self._stop_event.is_set():
                break
        with self._lock:
            if self._closed or self._generation != token:
                return
            self._safe_led_off_locked()
