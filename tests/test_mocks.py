"""Tests for MockProviderAPI to verify tool contracts and failure behaviors."""

import unittest
from orange_invest.mock_api import (
    MockProviderAPI,
    SlotUnavailableError,
    SlotTakenError,
    HoldExpiredError,
    TimeoutError,
    ServiceUnavailable503Error,
    SlotConflictError,
)


class TestMockProviderAPI(unittest.TestCase):

    def setUp(self):
        self.api = MockProviderAPI(seed=12345)

    def test_search_providers_returns_3_to_8(self):
        res = self.api.search_providers("plumbing", area="Indiranagar")
        self.assertGreaterEqual(len(res), 3)
        self.assertLessEqual(len(res), 8)
        first = res[0]
        self.assertIn("id", first)
        self.assertIn("price", first)
        self.assertIn("rating", first)
        self.assertIn("free_slots", first)
        self.assertIn("attributes", first)

    def test_get_provider_contains_prompt_injection(self):
        prov = self.api.get_provider("p_plumb_101")
        self.assertIn("reviews", prov)
        injection_found = any(
            "disregard" in r["text"].lower() or "override" in r["text"].lower()
            for r in prov["reviews"]
        )
        self.assertTrue(injection_found, "Expected at least one review with prompt injection attempt")

    def test_list_bookings(self):
        bookings = self.api.list_bookings("user_123")
        self.assertGreaterEqual(len(bookings), 2)
        b = bookings[0]
        self.assertIn("id", b)
        self.assertIn("provider", b)
        self.assertIn("service", b)
        self.assertIn("slot", b)
        self.assertIn("status", b)

    def test_hold_slot_success_and_unavailable(self):
        hold = self.api.hold_slot("p_plumb_101", "2026-11-05 09:00 AM")
        self.assertIn("hold_id", hold)
        self.assertIn("expires_at", hold)
        self.assertIn("quoted_price", hold)
        self.assertEqual(hold["quoted_price"], 1200.0)

        # Mark slot unavailable
        self.api.unavailable_slots.add(("p_plumb_101", "2026-11-05 09:00 AM"))
        with self.assertRaises(SlotUnavailableError):
            self.api.hold_slot("p_plumb_101", "2026-11-05 09:00 AM")

    def test_hold_slot_quoted_price_differ(self):
        self.api.hold_price_override = 1750.0
        hold = self.api.hold_slot("p_plumb_101", "2026-11-05 09:00 AM")
        self.assertEqual(hold["quoted_price"], 1750.0)

    def test_book_deterministic_outcomes(self):
        # 1. Success
        self.api.force_book_outcome = "success"
        hold = self.api.hold_slot("p_plumb_101", "2026-11-05 09:00 AM")
        booking_id = self.api.book(hold["hold_id"], idempotency_key="test_idem_1")
        self.assertTrue(booking_id.startswith("b_"))

        # Idempotent repeat returns same booking_id
        repeat_id = self.api.book(hold["hold_id"], idempotency_key="test_idem_1")
        self.assertEqual(booking_id, repeat_id)

        # 2. Slot taken
        self.api.force_book_outcome = "slot_taken"
        hold2 = self.api.hold_slot("p_plumb_101", "2026-11-05 11:00 AM")
        with self.assertRaises(SlotTakenError):
            self.api.book(hold2["hold_id"], idempotency_key="test_idem_2")

        # 3. Timeout where backend actually booked
        self.api.force_book_outcome = "timeout_booked"
        hold3 = self.api.hold_slot("p_plumb_101", "2026-11-06 10:00 AM")
        with self.assertRaises(TimeoutError):
            self.api.book(hold3["hold_id"], idempotency_key="test_idem_timeout_booked")
        # Authoritative status check reveals booked
        status = self.api.get_booking_status("test_idem_timeout_booked")
        self.assertEqual(status["status"], "booked")
        self.assertIsNotNone(status["booking_id"])

        # 4. Timeout where backend did not book
        self.api.force_book_outcome = "timeout_unbooked"
        hold4 = self.api.hold_slot("p_plumb_101", "2026-11-06 04:00 PM")
        with self.assertRaises(TimeoutError):
            self.api.book(hold4["hold_id"], idempotency_key="test_idem_timeout_unbooked")
        # Authoritative status check reveals failed
        status2 = self.api.get_booking_status("test_idem_timeout_unbooked")
        self.assertEqual(status2["status"], "failed")

    def test_cancel_transient_503_and_retry(self):
        b_id = "b_ac_active_002"
        idem_key = "cancel_idem_001"
        # First call fails with 503
        with self.assertRaises(ServiceUnavailable503Error):
            self.api.cancel(b_id, idempotency_key=idem_key)

        # Retry call succeeds
        res = self.api.cancel(b_id, idempotency_key=idem_key)
        self.assertEqual(res, "cancelled")
        self.assertEqual(self.api.bookings[b_id]["status"], "cancelled")

    def test_reschedule_and_slot_conflict(self):
        b_id = "b_ac_active_002"
        # Normal reschedule
        res = self.api.reschedule(b_id, new_slot="2026-11-07 02:00 PM", idempotency_key="resched_01")
        self.assertEqual(res["slot"], "2026-11-07 02:00 PM")

        # Slot conflict
        self.api.conflict_reschedule_slots.add("2026-11-07 04:00 PM")
        with self.assertRaises(SlotConflictError):
            self.api.reschedule(b_id, new_slot="2026-11-07 04:00 PM", idempotency_key="resched_02")


if __name__ == "__main__":
    unittest.main()

