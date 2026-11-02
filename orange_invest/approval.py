"""Code-enforced approval system for home-service errands agent.

Guarantees:
1. Book, cancel, and reschedule REQUIRE explicit user confirmation.
2. Approval is a concrete in-memory / persistent record checked by code.
3. It cannot be inferred from model output, prompt injection, or tool arguments.
4. Any change to provider, slot, or price immediately voids the approval.
5. Approvals are single-use (consumed upon execution).
"""

from typing import Optional
import time
from orange_invest.models import ActionType, ApprovalRecord, ApprovalStatus


class ApprovalError(Exception):
    """Base exception for approval enforcement failures."""
    pass


class ApprovalMissingError(ApprovalError):
    """Raised when an action is attempted without any approval record."""
    pass


class ApprovalNotGrantedError(ApprovalError):
    """Raised when an approval record exists but is not yet approved by the user."""
    pass


class ApprovalVoidedError(ApprovalError):
    """Raised when an approval record was voided due to parameter change or rejection."""
    pass


class ApprovalConsumedError(ApprovalError):
    """Raised when an approval record was already consumed."""
    pass


class ApprovalMismatchError(ApprovalError):
    """Raised when execution parameters do not exactly match the approved parameters."""
    pass


class ApprovalGuard:
    """Deterministic, code-level approval barrier protecting sensitive actions."""

    def __init__(self):
        self.active_record: Optional[ApprovalRecord] = None
        self.history: list[ApprovalRecord] = []

    def stage_approval(
        self,
        action: ActionType,
        provider_id: str,
        provider_name: str,
        slot: str,
        price: float,
    ) -> ApprovalRecord:
        """Create a new pending approval record. Voids any previous active record if parameters differ."""
        if self.active_record and self.active_record.status == ApprovalStatus.APPROVED:
            if not self.active_record.matches(action, provider_id, slot, price):
                self.void_approval(
                    f"Parameters changed from ({self.active_record.action}, {self.active_record.provider_id}, "
                    f"{self.active_record.slot}, ₹{self.active_record.price}) to ({action}, {provider_id}, {slot}, ₹{price})"
                )

        record = ApprovalRecord(
            action=action,
            provider_id=provider_id,
            provider_name=provider_name,
            slot=slot,
            price=price,
            status=ApprovalStatus.PENDING,
        )
        self.active_record = record
        self.history.append(record)
        return record

    def grant_approval(self) -> ApprovalRecord:
        """User explicitly granted approval for the currently staged action."""
        if not self.active_record:
            raise ApprovalMissingError("No pending action staged for approval.")

        if self.active_record.status == ApprovalStatus.VOIDED:
            raise ApprovalVoidedError(f"Cannot grant approval: record was voided ({self.active_record.void_reason}).")

        if self.active_record.status == ApprovalStatus.CONSUMED:
            raise ApprovalConsumedError("Approval was already used.")

        self.active_record.status = ApprovalStatus.APPROVED
        self.active_record.approved_at = time.time()
        return self.active_record

    def reject_approval(self, reason: str = "User declined") -> ApprovalRecord:
        """User rejected the staged action."""
        if not self.active_record:
            raise ApprovalMissingError("No pending action staged to reject.")

        self.active_record.status = ApprovalStatus.REJECTED
        self.active_record.void_reason = reason
        return self.active_record

    def void_approval(self, reason: str) -> Optional[ApprovalRecord]:
        """Invalidate the current approval because a constraint, price, or slot changed."""
        if self.active_record and self.active_record.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
            self.active_record.status = ApprovalStatus.VOIDED
            self.active_record.void_reason = reason
            return self.active_record
        return None

    def verify_and_consume(
        self,
        action: ActionType,
        provider_id: str,
        slot: str,
        price: float,
    ) -> ApprovalRecord:
        """
        Verify that an exact, approved record exists for the given action, provider, slot, and price.
        If valid, marks it CONSUMED and returns it.
        Otherwise raises an ApprovalError.
        """
        if not self.active_record:
            raise ApprovalMissingError(f"Execution of '{action.value}' blocked: no approval record exists.")

        rec = self.active_record

        if rec.status == ApprovalStatus.CONSUMED:
            raise ApprovalConsumedError(f"Approval for '{action.value}' was already consumed. Cannot double-execute.")

        if rec.status == ApprovalStatus.VOIDED:
            raise ApprovalVoidedError(f"Approval for '{action.value}' is voided: {rec.void_reason}")

        if rec.status == ApprovalStatus.REJECTED:
            raise ApprovalVoidedError(f"Approval for '{action.value}' was rejected by user.")

        if rec.status != ApprovalStatus.APPROVED:
            raise ApprovalNotGrantedError(
                f"Action '{action.value}' has not been approved by the user (current status: {rec.status.value})."
            )

        # Exact matching check enforced in code
        if rec.action != action:
            raise ApprovalMismatchError(
                f"Action mismatch: approved '{rec.action.value}', attempted '{action.value}'."
            )
        if rec.provider_id != provider_id:
            raise ApprovalMismatchError(
                f"Provider mismatch: approved '{rec.provider_id}', attempted '{provider_id}'."
            )
        if rec.slot != slot:
            raise ApprovalMismatchError(
                f"Slot mismatch: approved '{rec.slot}', attempted '{slot}'."
            )
        if abs(rec.price - price) >= 1e-4:
            raise ApprovalMismatchError(
                f"Price mismatch: approved ₹{rec.price}, attempted ₹{price}."
            )

        # Mark consumed
        rec.status = ApprovalStatus.CONSUMED
        rec.consumed_at = time.time()
        return rec

