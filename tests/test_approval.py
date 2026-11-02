"""Tests for Code-Enforced Approval System."""

import unittest
from orange_invest.approval import (
    ApprovalGuard,
    ApprovalMissingError,
    ApprovalNotGrantedError,
    ApprovalVoidedError,
    ApprovalConsumedError,
    ApprovalMismatchError,
)
from orange_invest.models import ActionType, ApprovalStatus


class TestApprovalEnforcement(unittest.TestCase):

    def setUp(self):
        self.guard = ApprovalGuard()

    def test_missing_approval_blocks_execution(self):
        # Attempting verify_and_consume with no record staged must fail
        with self.assertRaises(ApprovalMissingError):
            self.guard.verify_and_consume(
                action=ActionType.BOOK,
                provider_id="p_plumb_101",
                slot="2026-11-05 09:00 AM",
                price=1200.0,
            )

    def test_unapproved_record_blocks_execution(self):
        # Record is staged (PENDING) but user has not said 'yes'
        self.guard.stage_approval(
            action=ActionType.BOOK,
            provider_id="p_plumb_101",
            provider_name="Ramesh",
            slot="2026-11-05 09:00 AM",
            price=1200.0,
        )
        with self.assertRaises(ApprovalNotGrantedError):
            self.guard.verify_and_consume(
                action=ActionType.BOOK,
                provider_id="p_plumb_101",
                slot="2026-11-05 09:00 AM",
                price=1200.0,
            )

    def test_price_change_voids_approval(self):
        # Stage approval for ₹1200
        self.guard.stage_approval(
            action=ActionType.BOOK,
            provider_id="p_plumb_101",
            provider_name="Ramesh",
            slot="2026-11-05 09:00 AM",
            price=1200.0,
        )
        self.guard.grant_approval()
        self.assertEqual(self.guard.active_record.status, ApprovalStatus.APPROVED)

        # Quoted price increases to ₹1500 (e.g. surge pricing)
        self.guard.stage_approval(
            action=ActionType.BOOK,
            provider_id="p_plumb_101",
            provider_name="Ramesh",
            slot="2026-11-05 09:00 AM",
            price=1500.0,
        )
        # Previous approval was voided; new record is PENDING, not approved!
        self.assertEqual(self.guard.active_record.status, ApprovalStatus.PENDING)
        with self.assertRaises(ApprovalNotGrantedError):
            self.guard.verify_and_consume(
                action=ActionType.BOOK,
                provider_id="p_plumb_101",
                slot="2026-11-05 09:00 AM",
                price=1500.0,
            )

    def test_slot_change_voids_approval(self):
        self.guard.stage_approval(
            action=ActionType.BOOK,
            provider_id="p_plumb_101",
            provider_name="Ramesh",
            slot="2026-11-05 09:00 AM",
            price=1200.0,
        )
        self.guard.grant_approval()

        # Slot changes to 11:00 AM
        self.guard.stage_approval(
            action=ActionType.BOOK,
            provider_id="p_plumb_101",
            provider_name="Ramesh",
            slot="2026-11-05 11:00 AM",
            price=1200.0,
        )
        self.assertEqual(self.guard.active_record.status, ApprovalStatus.PENDING)
        with self.assertRaises(ApprovalNotGrantedError):
            self.guard.verify_and_consume(
                action=ActionType.BOOK,
                provider_id="p_plumb_101",
                slot="2026-11-05 11:00 AM",
                price=1200.0,
            )

    def test_approval_is_single_use_consumed(self):
        self.guard.stage_approval(
            action=ActionType.BOOK,
            provider_id="p_plumb_101",
            provider_name="Ramesh",
            slot="2026-11-05 09:00 AM",
            price=1200.0,
        )
        self.guard.grant_approval()

        # First consumption succeeds
        consumed = self.guard.verify_and_consume(
            action=ActionType.BOOK,
            provider_id="p_plumb_101",
            slot="2026-11-05 09:00 AM",
            price=1200.0,
        )
        self.assertEqual(consumed.status, ApprovalStatus.CONSUMED)

        # Second attempt must be rejected (cannot double-book on same approval)
        with self.assertRaises(ApprovalConsumedError):
            self.guard.verify_and_consume(
                action=ActionType.BOOK,
                provider_id="p_plumb_101",
                slot="2026-11-05 09:00 AM",
                price=1200.0,
            )

    def test_cancel_and_reschedule_require_approval(self):
        # Cancel requires approval
        self.guard.stage_approval(
            action=ActionType.CANCEL,
            provider_id="p_ac_301",
            provider_name="CoolBreeze",
            slot="2026-11-05 11:00 AM",
            price=1100.0,
        )
        with self.assertRaises(ApprovalNotGrantedError):
            self.guard.verify_and_consume(
                action=ActionType.CANCEL,
                provider_id="p_ac_301",
                slot="2026-11-05 11:00 AM",
                price=1100.0,
            )

        self.guard.grant_approval()
        rec = self.guard.verify_and_consume(
            action=ActionType.CANCEL,
            provider_id="p_ac_301",
            slot="2026-11-05 11:00 AM",
            price=1100.0,
        )
        self.assertEqual(rec.status, ApprovalStatus.CONSUMED)


if __name__ == "__main__":
    unittest.main()

