from __future__ import annotations

import unittest

from destinations.common import failure_result_from_response
from transport import HttpResponse


class DestinationCommonTests(unittest.TestCase):
    def test_http_500_is_retryable_by_default(self) -> None:
        result = failure_result_from_response(
            destination_name="hook",
            response=HttpResponse(500, {}, "server error"),
        )

        self.assertTrue(result.retryable)

    def test_http_429_is_retryable_by_default(self) -> None:
        result = failure_result_from_response(
            destination_name="hook",
            response=HttpResponse(429, {"Retry-After": "3"}, "rate limited"),
        )

        self.assertTrue(result.retryable)
        self.assertEqual(result.retry_after_seconds, 3.0)
