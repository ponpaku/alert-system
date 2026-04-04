from __future__ import annotations

import unittest
from unittest.mock import patch

from config import HttpConfig
from dispatcher import Dispatcher
from models import DispatchResult, build_alert_event
from transport import HttpTransport


class FakeResponse:
    def __init__(self, body: bytes):
        self.status_code = 500
        self.headers = {"Content-Type": "text/plain"}
        self.encoding = "utf-8"
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=1, decode_unicode=False):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index : index + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []
        self.closed = False

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def close(self) -> None:
        self.closed = True


class HttpTransportTests(unittest.TestCase):
    def test_response_body_is_truncated(self) -> None:
        response = FakeResponse(b"x" * 32)
        session = FakeSession(response)
        transport = HttpTransport(
            HttpConfig(
                user_agent="ReceptionAlert/1.0",
                request_timeout_seconds=5,
                verify_tls=True,
                ca_bundle_path="",
                response_body_limit_bytes=8,
            ),
            session=session,
        )

        http_response = transport.request(method="POST", url="https://example.com", event=make_event())

        self.assertEqual(http_response.text, "xxxxxxxx")
        self.assertTrue(http_response.truncated)
        self.assertTrue(response.closed)
        self.assertTrue(session.calls[0]["stream"])

    def test_transport_reuses_thread_local_session(self) -> None:
        sessions: list[FakeSession] = []

        def session_factory():
            session = FakeSession(FakeResponse(b"ok"))
            sessions.append(session)
            return session

        transport = HttpTransport(
            HttpConfig(
                user_agent="ReceptionAlert/1.0",
                request_timeout_seconds=5,
                verify_tls=True,
                ca_bundle_path="",
                response_body_limit_bytes=8,
            )
        )

        with patch("transport.requests.Session", side_effect=session_factory):
            transport.request(method="POST", url="https://example.com/1", event=make_event())
            transport.request(method="POST", url="https://example.com/2", event=make_event())
            transport.close()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].calls), 2)
        self.assertTrue(sessions[0].closed)

    def test_close_does_not_close_injected_session_by_default(self) -> None:
        response = FakeResponse(b"ok")
        session = FakeSession(response)
        transport = HttpTransport(
            HttpConfig(
                user_agent="ReceptionAlert/1.0",
                request_timeout_seconds=5,
                verify_tls=True,
                ca_bundle_path="",
                response_body_limit_bytes=8,
            ),
            session=session,
        )

        transport.close()

        self.assertFalse(session.closed)

    def test_dispatcher_reuses_bounded_sessions_across_multiple_dispatches(self) -> None:
        sessions: list[FakeSession] = []

        def session_factory():
            session = FakeSession(FakeResponse(b"ok"))
            sessions.append(session)
            return session

        class Destination:
            def __init__(self, name: str, transport: HttpTransport):
                self.name = name
                self.enabled = True
                self._transport = transport

            def send(self, event, *, stop_event=None, deadline_monotonic=None):
                self._transport.request(method="POST", url="https://example.com", event=event, deadline_monotonic=deadline_monotonic)
                return DispatchResult.success(self.name, 200)

        transport = HttpTransport(
            HttpConfig(
                user_agent="ReceptionAlert/1.0",
                request_timeout_seconds=5,
                verify_tls=True,
                ca_bundle_path="",
                response_body_limit_bytes=8,
            )
        )
        destinations = [Destination("a", transport), Destination("b", transport)]
        dispatcher = Dispatcher(destinations, retry_delays_seconds=(0,), max_parallel_destinations=2, transport=transport)

        with patch("transport.requests.Session", side_effect=session_factory):
            for _ in range(4):
                dispatcher.dispatch(make_event())
            dispatcher.close()

        self.assertLessEqual(len(sessions), 2)

    def test_dispatcher_close_does_not_close_external_transport_by_default(self) -> None:
        class Destination:
            name = "a"
            enabled = True

            def send(self, event, *, stop_event=None, deadline_monotonic=None):
                return DispatchResult.success(self.name, 200)

        transport = HttpTransport(
            HttpConfig(
                user_agent="ReceptionAlert/1.0",
                request_timeout_seconds=5,
                verify_tls=True,
                ca_bundle_path="",
                response_body_limit_bytes=8,
            )
        )
        with patch.object(transport, "close") as close_transport:
            dispatcher = Dispatcher([Destination()], retry_delays_seconds=(0,), transport=transport)
            dispatcher.close()
        close_transport.assert_not_called()


def make_event():
    return build_alert_event(
        button_name="staff",
        kind="alert",
        prefix="prefix",
        message="message",
        location_name="frontdesk",
    )
