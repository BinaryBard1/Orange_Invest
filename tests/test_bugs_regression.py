"""Regression tests guarding against the three real bugs encountered during implementation."""

import unittest
from orange_invest.agent import HomeServiceAgent
from orange_invest.approval import ApprovalGuard, ApprovalNotGrantedError
from orange_invest.mock_api import MockProviderAPI, SlotConflictError
from orange_invest.models import ActionType, ApprovalStatus, TimeWindow, WorkflowStatus


class TestBugsRegression(unittest.TestCase):

    def test_bug1_idempotent_book_retry_after_hold_consumed(self):
        """
        Bug 1 Regression Guard:
        Verifies that book() retry succeeds even after the hold has been popped from memory.
        """
        api = MockProviderAPI()
        hold = api.hold_slot("p_plumb_101", "2026-11-05 09:00 AM")
        hold_id = hold["hold_id"]
        idem_key = "reg_test_bug1_idem_key"

        # First call succeeds and consumes the hold
        b_id1 = api.book(hold_id, idempotency_key=idem_key)
        self.assertTrue(b_id1.startswith("b_"))
        self.assertNotIn(hold_id, api.holds, "Hold must be consumed after first booking")

        # Second call with same idempotency key must NOT raise HoldExpiredError
        # It must return the exact same booking_id
        b_id2 = api.book(hold_id, idempotency_key=idem_key)
        self.assertEqual(b_id1, b_id2, "Idempotent retry must return the existing booking ID")

    def test_bug2_relative_date_window_matching(self):
        """
        Bug 2 Regression Guard:
        Verifies that 'this week' does not reject slots formatted as ISO date strings.
        """
        window = TimeWindow(
            raw="mornings this week",
            day="this week",
            time_of_day="morning",
            not_after_hour=12,
        )
        slot_candidate = "2026-11-05 09:00 AM"
        # Must match morning slot within this week
        self.assertTrue(
            window.matches_slot(slot_candidate),
            "Relative qualifier 'this week' must not fail slot matching",
        )

        # Slot outside morning hours must still be rejected
        afternoon_slot = "2026-11-05 03:00 PM"
        self.assertFalse(
            window.matches_slot(afternoon_slot),
            "Afternoon slot must still be rejected by morning constraint",
        )

    def test_bug3_stale_approval_voided_on_conflict_and_parameter_change(self):
        """
        Bug 3 Regression Guard:
        Verifies that when parameters change after an approval was granted,
        the approval is strictly voided and cannot be reused for a different slot.
        """
        guard = ApprovalGuard()
        # Stage and approve Slot A
        guard.stage_approval(
            action=ActionType.RESCHEDULE,
            provider_id="p_ac_301",
            provider_name="CoolBreeze",
            slot="2026-11-07 10:00 AM",
            price=1100.0,
        )
        guard.grant_approval()
        self.assertEqual(guard.active_record.status, ApprovalStatus.APPROVED)

        # Parameters change to Slot B
        guard.stage_approval(
            action=ActionType.RESCHEDULE,
            provider_id="p_ac_301",
            provider_name="CoolBreeze",
            slot="2026-11-07 02:00 PM",
            price=1100.0,
        )

        # Active record must be PENDING, not APPROVED
        self.assertEqual(guard.active_record.status, ApprovalStatus.PENDING)

        # Attempting execution of Slot B without new user approval must fail
        with self.assertRaises(ApprovalNotGrantedError):
            guard.verify_and_consume(
                action=ActionType.RESCHEDULE,
                provider_id="p_ac_301",
                slot="2026-11-07 02:00 PM",
                price=1100.0,
            )


if __name__ == "__main__":
    unittest.main()

