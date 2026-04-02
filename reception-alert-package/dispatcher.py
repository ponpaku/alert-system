from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from destinations.base import Destination
from models import AlertEvent, DispatchResult


class Dispatcher:
    def __init__(self, destinations: list[Destination], retry_delays_seconds: tuple[float, ...]):
        self._destinations_by_name = {destination.name: destination for destination in destinations}
        self._enabled_destination_names = [destination.name for destination in destinations if destination.enabled]
        self._retry_delays_seconds = retry_delays_seconds

    def dispatch(
        self,
        event: AlertEvent,
        target_names: list[str] | None = None,
        *,
        stop_event: threading.Event | None = None,
        deadline_monotonic: float | None = None,
        deadline_supplier: Callable[[], float | None] | None = None,
    ) -> list[DispatchResult]:
        requested_names = target_names or list(self._enabled_destination_names)
        results: list[DispatchResult] = []
        resolved_names: list[str] = []
        for destination_name in requested_names:
            destination = self._destinations_by_name.get(destination_name)
            if destination is None:
                logging.warning("Skipping unknown destination=%s event_id=%s", destination_name, event.event_id)
                results.append(
                    DispatchResult.not_attempted(
                        destination_name,
                        error_summary="unknown destination",
                    )
                )
                continue
            if not destination.enabled:
                logging.warning("Skipping disabled destination=%s event_id=%s", destination_name, event.event_id)
                results.append(
                    DispatchResult.not_attempted(
                        destination_name,
                        error_summary="destination is disabled",
                    )
                )
                continue
            resolved_names.append(destination_name)
        for destination_name in resolved_names:
            destination = self._destinations_by_name[destination_name]
            results.append(
                self._dispatch_single_destination(
                    destination,
                    event,
                    stop_event=stop_event,
                    deadline_monotonic=deadline_monotonic,
                    deadline_supplier=deadline_supplier,
                )
            )
        return results

    def _dispatch_single_destination(
        self,
        destination: Destination,
        event: AlertEvent,
        *,
        stop_event: threading.Event | None,
        deadline_monotonic: float | None,
        deadline_supplier: Callable[[], float | None] | None,
    ) -> DispatchResult:
        attempts = max(1, len(self._retry_delays_seconds))
        latest_result: DispatchResult | None = None
        for attempt_index in range(attempts):
            current_deadline = deadline_supplier() if deadline_supplier is not None else deadline_monotonic
            if stop_event is not None and stop_event.is_set():
                return latest_result or DispatchResult.not_attempted(destination.name, error_summary="stopped before request start")
            if current_deadline is not None and time.monotonic() >= current_deadline:
                return latest_result or DispatchResult.not_attempted(destination.name, error_summary="deadline exceeded before request start")
            latest_result = destination.send(event, stop_event=stop_event, deadline_monotonic=current_deadline)
            logging.info(
                "dispatch destination=%s outcome=%s status=%s event_id=%s",
                destination.name,
                latest_result.outcome,
                latest_result.status_code,
                event.event_id,
            )
            if latest_result.outcome != "failed" or not latest_result.retryable:
                return latest_result
            if attempt_index == attempts - 1:
                return latest_result
            wait_seconds = latest_result.retry_after_seconds
            if wait_seconds is None:
                wait_seconds = self._retry_delays_seconds[attempt_index + 1]
            current_deadline = deadline_supplier() if deadline_supplier is not None else deadline_monotonic
            if current_deadline is not None:
                remaining = current_deadline - time.monotonic()
                if remaining <= 0:
                    return latest_result
                wait_seconds = min(wait_seconds, remaining)
            if wait_seconds > 0:
                if stop_event is not None:
                    if stop_event.wait(wait_seconds):
                        return latest_result
                else:
                    time.sleep(wait_seconds)
            current_deadline = deadline_supplier() if deadline_supplier is not None else deadline_monotonic
            if current_deadline is not None and time.monotonic() >= current_deadline:
                return latest_result
        assert latest_result is not None
        return latest_result
