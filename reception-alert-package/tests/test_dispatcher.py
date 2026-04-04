from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock

from dispatcher import DispatchCutoffError, Dispatcher
from message_constants import DEFAULT_ALERT_MESSAGE, DEFAULT_ALERT_PREFIX, DEFAULT_LOCATION_NAME, LOCATION_LABEL
from models import AlertEvent, DispatchResult, build_alert_event, render_event_text


class FakeDestination:
    def __init__(self, name: str, results: list[DispatchResult], enabled: bool = True):
        self.name = name
        self.enabled = enabled
        self._results = list(results)
        self.calls = 0

    def send(self, event: AlertEvent, *, stop_event=None, deadline_monotonic=None) -> DispatchResult:
        self.calls += 1
        return self._results[min(self.calls - 1, len(self._results) - 1)]


class DeadlineAwareDestination(FakeDestination):
    def __init__(self, name: str, results: list[DispatchResult]):
        super().__init__(name, results)
        self.deadlines: list[float | None] = []

    def send(self, event: AlertEvent, *, stop_event=None, deadline_monotonic=None) -> DispatchResult:
        self.deadlines.append(deadline_monotonic)
        return super().send(event, stop_event=stop_event, deadline_monotonic=deadline_monotonic)


class SlowDestination(FakeDestination):
    def __init__(self, name: str, results: list[DispatchResult], sleep_seconds: float):
        super().__init__(name, results)
        self.sleep_seconds = sleep_seconds

    def send(self, event: AlertEvent, *, stop_event=None, deadline_monotonic=None) -> DispatchResult:
        time.sleep(self.sleep_seconds)
        return super().send(event, stop_event=stop_event, deadline_monotonic=deadline_monotonic)


class ExplodingDestination(FakeDestination):
    def send(self, event: AlertEvent, *, stop_event=None, deadline_monotonic=None) -> DispatchResult:
        self.calls += 1
        raise RuntimeError("boom")


class StoppableHangingDestination(FakeDestination):
    def send(self, event: AlertEvent, *, stop_event=None, deadline_monotonic=None) -> DispatchResult:
        self.calls += 1
        while stop_event is None or not stop_event.is_set():
            time.sleep(0.01)
        return DispatchResult.success(self.name, 200)


class SlowIgnoringStopDestination(FakeDestination):
    def __init__(self, name: str, results: list[DispatchResult], sleep_seconds: float):
        super().__init__(name, results)
        self.sleep_seconds = sleep_seconds
        self.started = threading.Event()

    def send(self, event: AlertEvent, *, stop_event=None, deadline_monotonic=None) -> DispatchResult:
        self.calls += 1
        self.started.set()
        time.sleep(self.sleep_seconds)
        return self._results[min(self.calls - 1, len(self._results) - 1)]


class DispatcherTests(unittest.TestCase):
    def test_retries_only_unsuccessful_destination(self) -> None:
        retryable_failure = DispatchResult.failed("hook", status_code=429, retryable=True, retry_after_seconds=0)
        hook = FakeDestination("hook", [retryable_failure, DispatchResult.success("hook", 200)])
        talk = FakeDestination("talk", [DispatchResult.success("talk", 201)])
        dispatcher = Dispatcher([hook, talk], retry_delays_seconds=(0, 1))

        results = dispatcher.dispatch(make_event())

        self.assertEqual([result.outcome for result in results], ["success", "success"])
        self.assertEqual(hook.calls, 2)
        self.assertEqual(talk.calls, 1)

    def test_stop_before_start_marks_not_attempted(self) -> None:
        destination = FakeDestination("hook", [DispatchResult.success("hook", 200)])
        dispatcher = Dispatcher([destination], retry_delays_seconds=(0,))
        stop_event = threading.Event()
        stop_event.set()

        results = dispatcher.dispatch(make_event(), stop_event=stop_event)

        self.assertEqual(results[0].outcome, "not_attempted")
        self.assertEqual(destination.calls, 0)

    def test_retry_wait_is_capped_by_deadline(self) -> None:
        retryable_failure = DispatchResult.failed("hook", status_code=429, retryable=True, retry_after_seconds=1.0)
        destination = FakeDestination("hook", [retryable_failure, DispatchResult.success("hook", 200)])
        dispatcher = Dispatcher([destination], retry_delays_seconds=(0, 1))

        started = time.monotonic()
        results = dispatcher.dispatch(make_event(), deadline_monotonic=time.monotonic() + 0.05)
        elapsed = time.monotonic() - started

        self.assertIn(results[0].outcome, {"failed", "success"})
        self.assertLessEqual(destination.calls, 2)
        self.assertLess(elapsed, 0.3)

    def test_explicit_target_does_not_send_to_disabled_destination(self) -> None:
        disabled = FakeDestination("hook", [DispatchResult.success("hook", 200)], enabled=False)
        dispatcher = Dispatcher([disabled], retry_delays_seconds=(0,))

        results = dispatcher.dispatch(make_event(), target_names=["hook"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "not_attempted")
        self.assertEqual(results[0].error_summary, "destination is disabled")
        self.assertEqual(disabled.calls, 0)

    def test_unknown_target_name_returns_not_attempted(self) -> None:
        dispatcher = Dispatcher([], retry_delays_seconds=(0,))

        results = dispatcher.dispatch(make_event(), target_names=["missing"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].destination_name, "missing")
        self.assertEqual(results[0].outcome, "not_attempted")
        self.assertEqual(results[0].error_summary, "unknown destination")

    def test_deadline_supplier_is_re_evaluated_between_attempts(self) -> None:
        retryable_failure = DispatchResult.failed("hook", status_code=429, retryable=True, retry_after_seconds=0)
        destination = DeadlineAwareDestination("hook", [retryable_failure, DispatchResult.success("hook", 200)])
        dispatcher = Dispatcher([destination], retry_delays_seconds=(0, 0))
        deadlines = [None, time.monotonic() + 1.0]

        def deadline_supplier() -> float | None:
            return deadlines.pop(0) if deadlines else time.monotonic() + 1.0

        dispatcher.dispatch(make_event(), deadline_supplier=deadline_supplier)

        self.assertEqual(destination.calls, 2)
        self.assertIsNone(destination.deadlines[0])
        self.assertIsNotNone(destination.deadlines[1])

    def test_retry_after_is_capped(self) -> None:
        retryable_failure = DispatchResult.failed("hook", status_code=429, retryable=True, retry_after_seconds=1.5)
        destination = FakeDestination("hook", [retryable_failure, DispatchResult.success("hook", 200)])
        dispatcher = Dispatcher([destination], retry_delays_seconds=(0, 0), max_retry_after_seconds=0.05)

        started = time.monotonic()
        dispatcher.dispatch(make_event())
        elapsed = time.monotonic() - started

        self.assertEqual(destination.calls, 2)
        self.assertLess(elapsed, 0.3)

    def test_multiple_destinations_are_dispatched_in_parallel(self) -> None:
        slow_a = SlowDestination("slow-a", [DispatchResult.success("slow-a", 200)], sleep_seconds=0.2)
        slow_b = SlowDestination("slow-b", [DispatchResult.success("slow-b", 200)], sleep_seconds=0.2)
        dispatcher = Dispatcher([slow_a, slow_b], retry_delays_seconds=(0,), max_parallel_destinations=2)

        started = time.monotonic()
        results = dispatcher.dispatch(make_event())
        elapsed = time.monotonic() - started

        self.assertEqual([result.outcome for result in results], ["success", "success"])
        self.assertLess(elapsed, 0.35)

    def test_parallel_destination_exception_becomes_non_retryable_failure(self) -> None:
        exploding = ExplodingDestination("hook", [DispatchResult.success("hook", 200)])
        slow_success = SlowDestination("talk", [DispatchResult.success("talk", 200)], sleep_seconds=0.05)
        dispatcher = Dispatcher([exploding, slow_success], retry_delays_seconds=(0,), max_parallel_destinations=2)

        results = dispatcher.dispatch(make_event(), deadline_monotonic=time.monotonic() + 1.0)

        outcomes = {result.destination_name: result for result in results}
        self.assertEqual(outcomes["talk"].outcome, "success")
        self.assertEqual(outcomes["hook"].outcome, "failed")
        self.assertFalse(outcomes["hook"].retryable)
        self.assertIn("unexpected destination error", outcomes["hook"].error_summary)

    def test_parallel_hanging_destination_raises_cutoff_after_persisting_finished_results(self) -> None:
        stop_event = threading.Event()
        hanging = StoppableHangingDestination("hook", [DispatchResult.success("hook", 200)])
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        dispatcher = Dispatcher(
            [hanging, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.05,
        )
        completed_results: list[DispatchResult] = []

        try:
            with self.assertRaises(DispatchCutoffError) as exc:
                dispatcher.dispatch(
                    make_event(),
                    stop_event=stop_event,
                    deadline_monotonic=time.monotonic() + 0.05,
                    result_handler=completed_results.append,
                )
            self.assertEqual(exc.exception.reason, "deadline")
            self.assertEqual(exc.exception.destination_names, ("hook",))
            self.assertEqual([(result.destination_name, result.outcome) for result in completed_results], [("talk", "success")])
        finally:
            stop_event.set()
            dispatcher.close()

    def test_parallel_inflight_completion_during_cutoff_grace_returns_success(self) -> None:
        slow = SlowDestination("hook", [DispatchResult.success("hook", 200)], sleep_seconds=0.08)
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        dispatcher = Dispatcher(
            [slow, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.2,
        )

        started = time.monotonic()
        try:
            results = dispatcher.dispatch(
                make_event(),
                deadline_monotonic=time.monotonic() + 0.02,
            )
        finally:
            dispatcher.close()

        elapsed = time.monotonic() - started
        self.assertEqual([(result.destination_name, result.outcome) for result in results], [("hook", "success"), ("talk", "success")])
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 0.35)

    def test_dispatch_refuses_new_work_after_abandoned_inflight_cutoff(self) -> None:
        stop_event = threading.Event()
        hanging = StoppableHangingDestination("hook", [DispatchResult.success("hook", 200)])
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        dispatcher = Dispatcher(
            [hanging, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.05,
        )

        try:
            with self.assertRaises(DispatchCutoffError):
                dispatcher.dispatch(
                    make_event(),
                    stop_event=stop_event,
                    deadline_monotonic=time.monotonic() + 0.05,
                )
            with self.assertRaisesRegex(RuntimeError, "abandoned in-flight"):
                dispatcher.dispatch(make_event())
        finally:
            stop_event.set()
            dispatcher.close()

    def test_close_waits_for_detached_completion_before_closing_owned_transport(self) -> None:
        stop_event = threading.Event()
        hanging = StoppableHangingDestination("hook", [DispatchResult.success("hook", 200)])
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        transport = Mock()
        dispatcher = Dispatcher(
            [hanging, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.05,
            transport=transport,
            owns_transport=True,
        )

        try:
            with self.assertRaises(DispatchCutoffError):
                dispatcher.dispatch(
                    make_event(),
                    stop_event=stop_event,
                    deadline_monotonic=time.monotonic() + 0.05,
                )
            self.assertFalse(transport.close.called)
        finally:
            stop_event.set()
            dispatcher.close()

        deadline = time.monotonic() + 0.3
        while not transport.close.called and time.monotonic() < deadline:
            time.sleep(0.01)
        transport.close.assert_called_once()

    def test_close_eventually_closes_owned_transport_after_detached_completion(self) -> None:
        slow = SlowIgnoringStopDestination("hook", [DispatchResult.success("hook", 200)], sleep_seconds=0.12)
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        transport = Mock()
        dispatcher = Dispatcher(
            [slow, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.05,
            transport=transport,
            owns_transport=True,
        )

        with self.assertRaises(DispatchCutoffError):
            dispatcher.dispatch(
                make_event(),
                deadline_monotonic=time.monotonic() + 0.02,
            )
        dispatcher.close()
        self.assertFalse(transport.close.called)
        time.sleep(0.2)
        transport.close.assert_called_once()

    def test_close_forces_transport_cleanup_after_detached_timeout(self) -> None:
        hanging = SlowIgnoringStopDestination("hook", [DispatchResult.success("hook", 200)], sleep_seconds=1.0)
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        transport = Mock()
        dispatcher = Dispatcher(
            [hanging, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.01,
            detached_cleanup_timeout_seconds=0.05,
            transport=transport,
            owns_transport=True,
        )

        with self.assertRaises(DispatchCutoffError):
            dispatcher.dispatch(
                make_event(),
                deadline_monotonic=time.monotonic() + 0.01,
            )
        dispatcher.close()
        time.sleep(0.15)

        transport.close.assert_called_once()

    def test_detached_cleanup_callback_runs_after_timeout(self) -> None:
        hanging = SlowIgnoringStopDestination("hook", [DispatchResult.success("hook", 200)], sleep_seconds=1.0)
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        dispatcher = Dispatcher(
            [hanging, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.01,
            detached_cleanup_timeout_seconds=0.05,
        )
        callback_states: list[bool] = []

        with self.assertRaises(DispatchCutoffError):
            dispatcher.dispatch(
                make_event(),
                deadline_monotonic=time.monotonic() + 0.01,
            )
        dispatcher.register_detached_cleanup_callback(callback_states.append)
        dispatcher.close()
        deadline = time.monotonic() + 0.3
        while not callback_states and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(callback_states, [True])

    def test_timeout_cleanup_discards_late_detached_completion_results(self) -> None:
        hanging = SlowIgnoringStopDestination("hook", [DispatchResult.success("hook", 200)], sleep_seconds=0.2)
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        dispatcher = Dispatcher(
            [hanging, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.01,
            detached_cleanup_timeout_seconds=0.05,
        )
        handled_destinations: list[str] = []

        with self.assertRaises(DispatchCutoffError):
            dispatcher.dispatch(
                make_event(),
                deadline_monotonic=time.monotonic() + 0.01,
                result_handler=lambda result: handled_destinations.append(result.destination_name),
            )
        dispatcher.close()
        time.sleep(0.3)

        self.assertEqual(handled_destinations, ["talk"])

    def test_timeout_cleanup_waits_for_active_detached_result_handler(self) -> None:
        slow = SlowIgnoringStopDestination("hook", [DispatchResult.success("hook", 200)], sleep_seconds=0.03)
        fast = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        dispatcher = Dispatcher(
            [slow, fast],
            retry_delays_seconds=(0,),
            max_parallel_destinations=2,
            running_cutoff_grace_seconds=0.0,
            detached_cleanup_timeout_seconds=0.01,
        )
        result_handler_started = threading.Event()
        completion_order: list[str] = []

        def result_handler(result: DispatchResult) -> None:
            if result.destination_name != "hook":
                completion_order.append("result-fast")
                return
            result_handler_started.set()
            time.sleep(0.05)
            completion_order.append("result-hook")

        with self.assertRaises(DispatchCutoffError):
            dispatcher.dispatch(
                make_event(),
                deadline_monotonic=time.monotonic() + 0.01,
                result_handler=result_handler,
            )
        dispatcher.register_detached_cleanup_callback(lambda timed_out: completion_order.append(f"cleanup-{timed_out}"))
        dispatcher.close()
        self.assertTrue(result_handler_started.wait(1.0))
        deadline = time.monotonic() + 1.0
        while len(completion_order) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(completion_order, ["result-fast", "result-hook", "cleanup-False"])

    def test_deadline_before_parallel_start_marks_all_not_attempted(self) -> None:
        first = FakeDestination("hook", [DispatchResult.success("hook", 200)])
        second = FakeDestination("talk", [DispatchResult.success("talk", 200)])
        dispatcher = Dispatcher([first, second], retry_delays_seconds=(0,), max_parallel_destinations=2)

        try:
            results = dispatcher.dispatch(
                make_event(),
                deadline_monotonic=time.monotonic() - 1.0,
            )
            self.assertEqual(
                [(result.destination_name, result.error_summary) for result in results],
                [
                    ("hook", "deadline exceeded before request start"),
                    ("talk", "deadline exceeded before request start"),
                ],
            )
            self.assertEqual(first.calls, 0)
            self.assertEqual(second.calls, 0)
        finally:
            dispatcher.close()

    def test_render_event_text_includes_location_label(self) -> None:
        event = make_event()

        text = render_event_text(event)

        self.assertIn(LOCATION_LABEL, text)
        self.assertEqual(text, f"{DEFAULT_ALERT_PREFIX} {DEFAULT_ALERT_MESSAGE}\n{LOCATION_LABEL}{DEFAULT_LOCATION_NAME}")


def make_event() -> AlertEvent:
    return build_alert_event(
        button_name="staff",
        kind="alert",
        prefix=DEFAULT_ALERT_PREFIX,
        message=DEFAULT_ALERT_MESSAGE,
        location_name=DEFAULT_LOCATION_NAME,
    )
