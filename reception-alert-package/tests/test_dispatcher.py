from __future__ import annotations

import threading
import time
import unittest

from dispatcher import Dispatcher
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
