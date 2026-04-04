from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
import logging
from queue import Empty, Queue
import threading
import time
from typing import Callable

from config import ConfigError
from destinations.base import Destination
from models import AlertEvent, DispatchResult
from requests import RequestException
from transport import DeadlineExceededError, HttpTransport


class DispatchCutoffError(RuntimeError):
    def __init__(self, *, destination_names: tuple[str, ...], reason: str):
        names = ", ".join(destination_names)
        super().__init__(f"dispatch cutoff with active destinations ({reason}): {names}")
        self.destination_names = destination_names
        self.reason = reason


@dataclass(frozen=True)
class _QueuedTask:
    future: Future[DispatchResult]
    fn: Callable[..., DispatchResult]
    args: tuple[object, ...]
    kwargs: dict[str, object]
    started_event: threading.Event


@dataclass(frozen=True)
class _SubmittedTask:
    future: Future[DispatchResult]
    started_event: threading.Event


class _DaemonWorkerPool:
    def __init__(self, max_workers: int, *, thread_name_prefix: str):
        self._max_workers = max(1, max_workers)
        self._thread_name_prefix = thread_name_prefix
        self._work_queue: Queue[_QueuedTask | None] = Queue()
        self._threads: list[threading.Thread] = []
        self._closed = False
        self._close_lock = threading.Lock()
        for index in range(self._max_workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"{thread_name_prefix}-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def submit(self, fn: Callable[..., DispatchResult], *args: object, **kwargs: object) -> _SubmittedTask:
        future: Future[DispatchResult] = Future()
        started_event = threading.Event()
        with self._close_lock:
            if self._closed:
                raise RuntimeError("worker pool is closed")
            self._work_queue.put(_QueuedTask(future=future, fn=fn, args=args, kwargs=kwargs, started_event=started_event))
        return _SubmittedTask(future=future, started_event=started_event)

    def close(self, *, wait_for_running: bool) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_queued_tasks()
            for _ in self._threads:
                self._work_queue.put(None)
        if wait_for_running:
            for thread in self._threads:
                thread.join()

    def _cancel_queued_tasks(self) -> None:
        while True:
            try:
                item = self._work_queue.get_nowait()
            except Empty:
                return
            if item is None:
                self._work_queue.put(None)
                return
            item.future.cancel()

    def _worker_loop(self) -> None:
        while True:
            item = self._work_queue.get()
            if item is None:
                return
            if not item.future.set_running_or_notify_cancel():
                continue
            item.started_event.set()
            try:
                result = item.fn(*item.args, **item.kwargs)
            except BaseException as exc:
                item.future.set_exception(exc)
            else:
                item.future.set_result(result)


class Dispatcher:
    def __init__(
        self,
        destinations: list[Destination],
        retry_delays_seconds: tuple[float, ...],
        *,
        max_parallel_destinations: int = 4,
        max_retry_after_seconds: float = 30.0,
        running_cutoff_grace_seconds: float = 5.0,
        detached_cleanup_timeout_seconds: float = 30.0,
        transport: HttpTransport | None = None,
        owns_transport: bool = False,
    ):
        self._destinations_by_name = {destination.name: destination for destination in destinations}
        self._enabled_destination_names = [destination.name for destination in destinations if destination.enabled]
        self._retry_delays_seconds = retry_delays_seconds
        self._max_parallel_destinations = max_parallel_destinations
        self._max_retry_after_seconds = max_retry_after_seconds
        self._running_cutoff_grace_seconds = max(0.0, running_cutoff_grace_seconds)
        self._detached_cleanup_timeout_seconds = max(0.0, detached_cleanup_timeout_seconds)
        self._transport = transport
        self._owns_transport = owns_transport
        self._pool = _DaemonWorkerPool(
            max_workers=max_parallel_destinations,
            thread_name_prefix="dispatch-worker",
        )
        self._abandoned_running_dispatch = threading.Event()
        self._close_lock = threading.Lock()
        self._close_requested = False
        self._transport_closed = False
        self._detached_futures_lock = threading.Lock()
        self._detached_futures: set[Future[DispatchResult]] = set()
        self._detached_cleanup_callbacks: list[Callable[[bool], None]] = []
        self._detached_cleanup_callback_lock = threading.Lock()
        self._detached_cleanup_finalized = False
        self._detached_cleanup_timed_out = False
        self._detached_cleanup_monitor_started = False
        self._discard_detached_results = False
        self._active_detached_result_callbacks = 0
        self._detached_result_callbacks_drained = threading.Event()
        self._detached_result_callbacks_drained.set()

    def close(self) -> None:
        with self._close_lock:
            self._close_requested = True
        wait_for_running = not self._abandoned_running_dispatch.is_set()
        self._pool.close(wait_for_running=wait_for_running)
        if wait_for_running:
            self._close_owned_transport_once()
            self._finalize_detached_cleanup(timed_out=False)
            return
        if not self.has_detached_inflight_work():
            self._close_owned_transport_once()
            self._finalize_detached_cleanup(timed_out=False)
            return
        self._start_detached_cleanup_monitor()

    def register_detached_cleanup_callback(self, callback: Callable[[bool], None]) -> None:
        finalized_timed_out: bool | None = None
        with self._detached_cleanup_callback_lock:
            if self._detached_cleanup_finalized:
                finalized_timed_out = self._detached_cleanup_timed_out
            else:
                self._detached_cleanup_callbacks.append(callback)
        if finalized_timed_out is not None:
            callback(finalized_timed_out)

    def has_detached_cleanup_pending(self) -> bool:
        with self._detached_cleanup_callback_lock:
            return not self._detached_cleanup_finalized

    def has_detached_inflight_work(self) -> bool:
        with self._detached_futures_lock:
            return bool(self._detached_futures)

    def resolve_target_names(self, target_names: list[str] | tuple[str, ...] | None = None) -> list[str]:
        requested_names = list(self._enabled_destination_names) if target_names is None else list(target_names)
        resolved_names: list[str] = []
        for destination_name in requested_names:
            destination = self._destinations_by_name.get(destination_name)
            if destination is None or not destination.enabled:
                continue
            resolved_names.append(destination_name)
        return resolved_names

    def dispatch(
        self,
        event: AlertEvent,
        target_names: list[str] | None = None,
        *,
        stop_event: threading.Event | None = None,
        deadline_monotonic: float | None = None,
        deadline_supplier: Callable[[], float | None] | None = None,
        result_handler: Callable[[DispatchResult], None] | None = None,
    ) -> list[DispatchResult]:
        if self._abandoned_running_dispatch.is_set():
            raise RuntimeError("dispatcher has abandoned in-flight destinations and must be recreated")
        requested_names = list(self._enabled_destination_names) if target_names is None else list(target_names)
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
        if len(resolved_names) <= 1 or self._max_parallel_destinations <= 1:
            for destination_name in resolved_names:
                destination = self._destinations_by_name[destination_name]
                result = self._dispatch_single_destination(
                    destination,
                    event,
                    stop_event=stop_event,
                    deadline_monotonic=deadline_monotonic,
                    deadline_supplier=deadline_supplier,
                )
                if result_handler is not None:
                    result_handler(result)
                results.append(result)
            return results
        return results + self._dispatch_parallel(
            event,
            resolved_names,
            stop_event=stop_event,
            deadline_monotonic=deadline_monotonic,
            deadline_supplier=deadline_supplier,
            result_handler=result_handler,
        )

    def _dispatch_parallel(
        self,
        event: AlertEvent,
        resolved_names: list[str],
        *,
        stop_event: threading.Event | None,
        deadline_monotonic: float | None,
        deadline_supplier: Callable[[], float | None] | None,
        result_handler: Callable[[DispatchResult], None] | None,
    ) -> list[DispatchResult]:
        current_deadline = deadline_supplier() if deadline_supplier is not None else deadline_monotonic
        if stop_event is not None and stop_event.is_set():
            return self._build_parallel_not_attempted_results(
                resolved_names,
                error_summary="stopped before request start",
                result_handler=result_handler,
            )
        if current_deadline is not None and time.monotonic() >= current_deadline:
            return self._build_parallel_not_attempted_results(
                resolved_names,
                error_summary="deadline exceeded before request start",
                result_handler=result_handler,
            )
        tasks_by_future: dict[Future[DispatchResult], tuple[int, str, threading.Event]] = {}
        resolved_results: list[DispatchResult | None] = [None] * len(resolved_names)
        for index, destination_name in enumerate(resolved_names):
            destination = self._destinations_by_name[destination_name]
            submitted = self._pool.submit(
                self._dispatch_single_destination,
                destination,
                event,
                stop_event=stop_event,
                deadline_monotonic=deadline_monotonic,
                deadline_supplier=deadline_supplier,
            )
            tasks_by_future[submitted.future] = (index, destination_name, submitted.started_event)

        pending_futures: set[Future[DispatchResult]] = set(tasks_by_future)
        cutoff_reason: str | None = None
        while pending_futures:
            current_deadline = deadline_supplier() if deadline_supplier is not None else deadline_monotonic
            if stop_event is not None and stop_event.is_set():
                cutoff_reason = "stopped"
                break
            wait_timeout = None
            if current_deadline is not None:
                remaining = current_deadline - time.monotonic()
                if remaining <= 0:
                    cutoff_reason = "deadline"
                    break
                wait_timeout = min(0.05, remaining)
            elif stop_event is not None:
                wait_timeout = 0.05
            done_futures, pending_futures = wait(pending_futures, timeout=wait_timeout, return_when=FIRST_COMPLETED)
            self._collect_done_futures(
                event=event,
                done_futures=done_futures,
                tasks_by_future=tasks_by_future,
                resolved_results=resolved_results,
                result_handler=result_handler,
            )

        if cutoff_reason is not None:
            self._collect_done_futures(
                event=event,
                done_futures={future for future in pending_futures if future.done()},
                tasks_by_future=tasks_by_future,
                resolved_results=resolved_results,
                result_handler=result_handler,
            )
            pending_futures = {future for future in pending_futures if not future.done()}
            self._handle_cutoff(
                pending_futures=pending_futures,
                tasks_by_future=tasks_by_future,
                resolved_results=resolved_results,
                result_handler=result_handler,
                cutoff_reason=cutoff_reason,
                event=event,
            )

        return [result for result in resolved_results if result is not None]

    def _build_parallel_not_attempted_results(
        self,
        destination_names: list[str],
        *,
        error_summary: str,
        result_handler: Callable[[DispatchResult], None] | None,
    ) -> list[DispatchResult]:
        results = [DispatchResult.not_attempted(destination_name, error_summary=error_summary) for destination_name in destination_names]
        if result_handler is not None:
            for result in results:
                result_handler(result)
        return results

    def _collect_done_futures(
        self,
        *,
        event: AlertEvent,
        done_futures: set[Future[DispatchResult]],
        tasks_by_future: dict[Future[DispatchResult], tuple[int, str, threading.Event]],
        resolved_results: list[DispatchResult | None],
        result_handler: Callable[[DispatchResult], None] | None,
    ) -> None:
        for future in done_futures:
            index, destination_name, _ = tasks_by_future[future]
            result = self._result_from_future(
                future,
                destination_name=destination_name,
                event=event,
            )
            if result_handler is not None:
                result_handler(result)
            resolved_results[index] = result

    def _handle_cutoff(
        self,
        *,
        pending_futures: set[Future[DispatchResult]],
        tasks_by_future: dict[Future[DispatchResult], tuple[int, str, threading.Event]],
        resolved_results: list[DispatchResult | None],
        result_handler: Callable[[DispatchResult], None] | None,
        cutoff_reason: str,
        event: AlertEvent,
    ) -> None:
        not_started_error = (
            "stopped before request start"
            if cutoff_reason == "stopped"
            else "deadline exceeded before request start"
        )
        running_futures: set[Future[DispatchResult]] = set()
        for future in pending_futures:
            index, destination_name, started_event = tasks_by_future[future]
            if started_event.is_set():
                running_futures.add(future)
                continue
            future.cancel()
            result = DispatchResult.not_attempted(
                destination_name,
                error_summary=not_started_error,
            )
            if result_handler is not None:
                result_handler(result)
            resolved_results[index] = result
        if running_futures and self._running_cutoff_grace_seconds > 0:
            drain_deadline = time.monotonic() + self._running_cutoff_grace_seconds
            while running_futures:
                remaining = drain_deadline - time.monotonic()
                if remaining <= 0:
                    break
                done_futures, running_futures = wait(
                    running_futures,
                    timeout=min(0.05, remaining),
                    return_when=FIRST_COMPLETED,
                )
                self._collect_done_futures(
                    event=event,
                    done_futures=done_futures,
                    tasks_by_future=tasks_by_future,
                    resolved_results=resolved_results,
                    result_handler=result_handler,
                )
            done_after_timeout = {future for future in running_futures if future.done()}
            if done_after_timeout:
                self._collect_done_futures(
                    event=event,
                    done_futures=done_after_timeout,
                    tasks_by_future=tasks_by_future,
                    resolved_results=resolved_results,
                    result_handler=result_handler,
                )
                running_futures -= done_after_timeout
        if running_futures:
            for future in running_futures:
                index, destination_name, _ = tasks_by_future[future]
                self._detach_running_future(
                    future,
                    destination_name=destination_name,
                    event=event,
                    resolved_results=resolved_results,
                    result_handler=result_handler,
                    result_index=index,
                )
            running_destinations = [
                tasks_by_future[future][1]
                for future in running_futures
            ]
            self._abandoned_running_dispatch.set()
            raise DispatchCutoffError(
                destination_names=tuple(sorted(running_destinations)),
                reason=cutoff_reason,
            )

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
            try:
                latest_result = destination.send(event, stop_event=stop_event, deadline_monotonic=current_deadline)
            except Exception as exc:
                logging.exception(
                    "dispatch destination=%s raised unexpected exception event_id=%s",
                    destination.name,
                    event.event_id,
                )
                latest_result = DispatchResult.failed(
                    destination.name,
                    retryable=_is_retryable_exception(exc),
                    error_summary=f"unexpected destination error: {type(exc).__name__}",
                )
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
            wait_seconds = max(0.0, min(wait_seconds, self._max_retry_after_seconds))
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

    def _result_from_future(
        self,
        future: Future[DispatchResult],
        *,
        destination_name: str,
        event: AlertEvent,
    ) -> DispatchResult:
        try:
            return future.result()
        except Exception as exc:
            logging.exception(
                "dispatch worker future failed unexpectedly destination=%s event_id=%s",
                destination_name,
                event.event_id,
            )
            return DispatchResult.failed(
                destination_name,
                retryable=_is_retryable_exception(exc),
                error_summary=f"unexpected dispatcher worker error: {type(exc).__name__}",
            )

    def _detach_running_future(
        self,
        future: Future[DispatchResult],
        *,
        destination_name: str,
        event: AlertEvent,
        resolved_results: list[DispatchResult | None],
        result_handler: Callable[[DispatchResult], None] | None,
        result_index: int,
    ) -> None:
        with self._detached_futures_lock:
            self._detached_futures.add(future)

        def _on_done(completed_future: Future[DispatchResult]) -> None:
            should_record_result = self._begin_detached_result_recording()
            try:
                if should_record_result:
                    result = self._result_from_future(
                        completed_future,
                        destination_name=destination_name,
                        event=event,
                    )
                    resolved_results[result_index] = result
                    if result_handler is not None:
                        result_handler(result)
                else:
                    logging.error(
                        "discarded detached dispatch completion after cleanup timeout destination=%s event_id=%s",
                        destination_name,
                        event.event_id,
                    )
            except Exception:
                logging.exception(
                    "detached dispatch completion handling failed destination=%s event_id=%s",
                    destination_name,
                    event.event_id,
                )
            finally:
                if should_record_result:
                    self._finish_detached_result_recording()
                with self._detached_futures_lock:
                    self._detached_futures.discard(completed_future)
                    should_finalize_cleanup = self._close_requested and not self._detached_futures
                if should_finalize_cleanup:
                    self._close_owned_transport_once()
                    self._finalize_detached_cleanup(timed_out=False)

        future.add_done_callback(_on_done)

    def _begin_detached_result_recording(self) -> bool:
        with self._detached_cleanup_callback_lock:
            if self._discard_detached_results:
                return False
            self._active_detached_result_callbacks += 1
            self._detached_result_callbacks_drained.clear()
            return True

    def _finish_detached_result_recording(self) -> None:
        with self._detached_cleanup_callback_lock:
            self._active_detached_result_callbacks -= 1
            if self._active_detached_result_callbacks == 0:
                self._detached_result_callbacks_drained.set()

    def _start_detached_cleanup_monitor(self) -> None:
        with self._detached_cleanup_callback_lock:
            if self._detached_cleanup_monitor_started:
                return
            self._detached_cleanup_monitor_started = True
        thread = threading.Thread(
            target=self._monitor_detached_cleanup,
            name="dispatch-detached-cleanup",
            daemon=True,
        )
        thread.start()

    def _monitor_detached_cleanup(self) -> None:
        timeout_seconds = self._detached_cleanup_timeout_seconds
        deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
        while self.has_detached_inflight_work():
            if deadline is not None and time.monotonic() >= deadline:
                logging.error(
                    "detached dispatch cleanup timed out after %.3fs; forcing transport cleanup and discarding late completions",
                    timeout_seconds,
                )
                with self._detached_cleanup_callback_lock:
                    self._discard_detached_results = True
                    should_wait_for_callbacks = self._active_detached_result_callbacks > 0
                if should_wait_for_callbacks:
                    self._detached_result_callbacks_drained.wait()
                self._close_owned_transport_once()
                self._finalize_detached_cleanup(timed_out=True)
                return
            time.sleep(0.05)
        self._close_owned_transport_once()
        self._finalize_detached_cleanup(timed_out=False)

    def _finalize_detached_cleanup(self, *, timed_out: bool) -> None:
        callbacks: list[Callable[[bool], None]]
        with self._detached_cleanup_callback_lock:
            if self._detached_cleanup_finalized:
                return
            self._detached_cleanup_finalized = True
            self._detached_cleanup_timed_out = timed_out
            callbacks = list(self._detached_cleanup_callbacks)
            self._detached_cleanup_callbacks.clear()
        for callback in callbacks:
            try:
                callback(timed_out)
            except Exception:
                logging.exception("detached cleanup callback failed")

    def _close_owned_transport_once(self) -> None:
        with self._close_lock:
            if self._transport_closed:
                return
            if self._transport is None or not self._owns_transport:
                self._transport_closed = True
                return
            self._transport_closed = True
        self._transport.close()


def _is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (RequestException, DeadlineExceededError, TimeoutError)):
        return True
    if isinstance(exc, ConfigError):
        return False
    return False
