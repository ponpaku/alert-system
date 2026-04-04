from __future__ import annotations

import tempfile
import unittest

from models import build_alert_event
from persistent_queue import PersistentQueue


class PersistentQueueTests(unittest.TestCase):
    def test_processing_row_is_recovered_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/queue.sqlite3"
            queue = PersistentQueue(path, capacity=4, retry_base_seconds=1, retry_max_seconds=10)
            record_id = queue.enqueue(make_event(), ("talk-main", "hook-main"))
            claimed = queue.claim_next_ready()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.record_id, record_id)
            queue.close()

            reopened = PersistentQueue(path, capacity=4, retry_base_seconds=1, retry_max_seconds=10)
            recovered = reopened.claim_next_ready()
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.record_id, record_id)
            self.assertEqual(recovered.target_names, ("talk-main", "hook-main"))
            reopened.close()

    def test_requeue_updates_remaining_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/queue.sqlite3"
            queue = PersistentQueue(path, capacity=4, retry_base_seconds=1, retry_max_seconds=10)
            record_id = queue.enqueue(make_event(), ("talk-main", "hook-main"))
            claimed = queue.claim_next_ready()
            self.assertIsNotNone(claimed)
            queue.requeue(record_id, target_names=("hook-main",), error_summary="failed", delay_seconds=0)

            retried = queue.claim_next_ready()
            self.assertIsNotNone(retried)
            self.assertEqual(retried.target_names, ("hook-main",))
            queue.close()

    def test_processed_destination_is_not_retried_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/queue.sqlite3"
            queue = PersistentQueue(path, capacity=4, retry_base_seconds=1, retry_max_seconds=10)
            record_id = queue.enqueue(make_event(), ("talk-main", "hook-main"))
            claimed = queue.claim_next_ready()
            self.assertIsNotNone(claimed)
            remaining = queue.mark_processed_destination(
                record_id,
                destination_name="talk-main",
                keep_for_retry=False,
                error_summary=None,
            )
            self.assertEqual(remaining, ("hook-main",))
            queue.close()

            reopened = PersistentQueue(path, capacity=4, retry_base_seconds=1, retry_max_seconds=10)
            recovered = reopened.claim_next_ready()
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.target_names, ("hook-main",))
            reopened.close()

    def test_open_without_recovery_leaves_processing_row_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/queue.sqlite3"
            queue = PersistentQueue(path, capacity=4, retry_base_seconds=1, retry_max_seconds=10)
            record_id = queue.enqueue(make_event(), ("talk-main",))
            claimed = queue.claim_next_ready()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.record_id, record_id)
            queue.close()

            reopened = PersistentQueue(
                path,
                capacity=4,
                retry_base_seconds=1,
                retry_max_seconds=10,
                recover_processing_rows=False,
            )
            try:
                self.assertEqual(reopened.pending_count(), 1)
                self.assertIsNone(reopened.claim_next_ready())
            finally:
                reopened.close()


def make_event():
    return build_alert_event(
        button_name="staff",
        kind="alert",
        prefix="prefix",
        message="message",
        location_name="frontdesk",
    )
