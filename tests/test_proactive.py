"""Tests for Proactive Follow-Up on Provider Cancellations."""

import unittest
from orange_invest.agent import HomeServiceAgent
from orange_invest.mock_api import MockProviderAPI
from orange_invest.models import ApprovalStatus, WorkflowStatus


class TestProactiveFollowUp(unittest.TestCase):

    def setUp(self):
        self.api = MockProviderAPI()
        self.agent = HomeServiceAgent(api=self.api)
        self.agent.start_session("user_123")

    def test_proactive_follow_up_finds_replacement_and_requires_approval(self):
        # Existing booking in API is b_ac_active_002 with CoolBreeze AC Care
        b_id = "b_ac_active_002"

        # Provider cancels externally
        result = self.agent.proactive_handler.handle_cancellation_event(
            booking_id=b_id,
            state=self.agent.state,
            reason="Technician fell sick",
        )

        self.assertTrue(result["success"])
        self.assertIn("was cancelled by the provider due to 'Technician fell sick'", result["message"])
        self.assertIn("Would you like me to confirm this replacement booking?", result["message"])

        # Invariant 1: State must be AWAITING_REPLACEMENT_APPROVAL
        self.assertEqual(self.agent.state.status, WorkflowStatus.AWAITING_REPLACEMENT_APPROVAL)

        # Invariant 2: Hold acquired on replacement
        self.assertIsNotNone(self.agent.state.current_hold)
        replacement_prov = result["replacement"]
        self.assertNotEqual(replacement_prov["id"], "p_ac_301")  # Must not be the cancelled provider

        # Invariant 3: Code approval record is PENDING, NOT auto-approved
        appr = self.agent.approval_guard.active_record
        self.assertEqual(appr.status, ApprovalStatus.PENDING)
        self.assertIsNone(self.agent.state.confirmed_booking_id)

        # 4. User approves ("yes")
        reply = self.agent.handle_message("yes, please confirm the replacement")
        self.assertIn("Success! Your booking with", reply)
        self.assertEqual(self.agent.state.status, WorkflowStatus.CONFIRMED)
        self.assertIsNotNone(self.agent.state.confirmed_booking_id)

    def test_proactive_follow_up_user_rejection(self):
        b_id = "b_ac_active_002"
        self.agent.proactive_handler.handle_cancellation_event(
            booking_id=b_id,
            state=self.agent.state,
            reason="Emergency",
        )
        self.assertEqual(self.agent.state.status, WorkflowStatus.AWAITING_REPLACEMENT_APPROVAL)

        # User rejects replacement
        reply = self.agent.handle_message("no, don't rebook")
        self.assertIn("cancelled and no changes were made", reply)
        self.assertEqual(self.agent.state.status, WorkflowStatus.CANCELLED_BY_USER)
        self.assertIsNone(self.agent.state.confirmed_booking_id)


if __name__ == "__main__":
    unittest.main()

