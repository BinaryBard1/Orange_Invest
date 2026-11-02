"""Tests for Booking Correctness, Idempotency, and Timeout Recovery."""

import unittest
from orange_invest.agent import HomeServiceAgent
from orange_invest.mock_api import MockProviderAPI
from orange_invest.models import WorkflowStatus


class TestBookingIdempotencyAndRecovery(unittest.TestCase):

    def setUp(self):
        self.api = MockProviderAPI(seed=999)
        self.agent = HomeServiceAgent(api=self.api)
        self.agent.start_session("idem_user")

    def test_no_duplicate_booking_across_repeated_yes(self):
        # 1. Initiate and approve booking
        self.api.force_book_outcome = "success"
        self.agent.handle_message("Find a plumber in Indiranagar mornings this week under 1500")
        self.assertEqual(self.agent.state.status, WorkflowStatus.AWAITING_APPROVAL)

        reply1 = self.agent.handle_message("yes")
        self.assertIn("Success", reply1)
        booking_id = self.agent.state.confirmed_booking_id
        self.assertIsNotNone(booking_id)
        self.assertEqual(self.agent.state.status, WorkflowStatus.CONFIRMED)

        # Count bookings in API for this user
        user_bookings = [b for b in self.api.bookings.values() if b.get("idempotency_key") == self.agent.state.idempotency_key]
        self.assertEqual(len(user_bookings), 1)

        # 2. User sends repeated "yes"
        reply2 = self.agent.handle_message("yes, confirm please")
        self.assertIn("already confirmed", reply2.lower())
        self.assertIn("No duplicate", reply2)

        # Total bookings in backend must strictly remain 1!
        user_bookings_after = [b for b in self.api.bookings.values() if b.get("idempotency_key") == self.agent.state.idempotency_key]
        self.assertEqual(len(user_bookings_after), 1)

    def test_timeout_after_success_recovery(self):
        """
        Critical invariant:
        book() times out, but backend committed booking.
        Agent MUST check get_booking_status before retrying or reporting,
        verify success, and claim success without double-booking.
        """
        self.api.force_book_outcome = "timeout_booked"
        self.agent.handle_message("Find a plumber in Indiranagar mornings this week under 1500")
        self.assertEqual(self.agent.state.status, WorkflowStatus.AWAITING_APPROVAL)

        # User confirms
        reply = self.agent.handle_message("yes")
        self.assertIn("Success", reply)
        self.assertEqual(self.agent.state.status, WorkflowStatus.CONFIRMED)
        self.assertIsNotNone(self.agent.state.confirmed_booking_id)

        # Verify backend has exactly 1 booking
        status = self.api.get_booking_status(self.agent.state.idempotency_key)
        self.assertEqual(status["status"], "booked")
        self.assertEqual(status["booking_id"], self.agent.state.confirmed_booking_id)

    def test_timeout_after_failure_recovery(self):
        """
        book() times out and backend failed to book.
        Agent MUST verify via get_booking_status and NOT claim false success!
        """
        self.api.force_book_outcome = "timeout_unbooked"
        self.agent.handle_message("Find a plumber in Indiranagar mornings this week under 1500")
        self.assertEqual(self.agent.state.status, WorkflowStatus.AWAITING_APPROVAL)

        reply = self.agent.handle_message("yes")
        # Invariant: Never claim success until verified!
        self.assertNotIn("Success", reply)
        self.assertIn("timed out and verification confirmed", reply)
        self.assertEqual(self.agent.state.status, WorkflowStatus.FAILED)
        self.assertIsNone(self.agent.state.confirmed_booking_id)

    def test_restart_mid_booking_reconciliation(self):
        """
        Agent state is saved. Process restarts mid-flight.
        Reconciles with authoritative backend using idempotency key.
        """
        self.api.force_book_outcome = "timeout_booked"
        self.agent.handle_message("Find a plumber in Indiranagar mornings this week under 1500")
        saved_idem_key = f"idem_restart_test_{self.agent.state.selected_provider['id']}"
        self.agent.state.idempotency_key = saved_idem_key

        # Approve and trigger timeout
        self.agent.handle_message("yes")
        b_id = self.agent.state.confirmed_booking_id

        # Simulate agent crash and fresh agent restart with saved state
        new_agent = HomeServiceAgent(api=self.api)
        new_agent.start_session("idem_user")
        new_agent.state.idempotency_key = saved_idem_key

        # Reconcile via get_booking_status
        status = self.api.get_booking_status(saved_idem_key)
        self.assertEqual(status["status"], "booked")
        new_agent.state.confirmed_booking_id = status["booking_id"]
        new_agent.state.status = WorkflowStatus.CONFIRMED

        self.assertEqual(new_agent.state.confirmed_booking_id, b_id)


if __name__ == "__main__":
    unittest.main()

