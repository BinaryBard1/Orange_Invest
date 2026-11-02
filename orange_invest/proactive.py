"""Proactive follow-up module for provider cancellations.

When a booked provider cancels:
1. Agent identifies eligible replacement providers matching the original booking requirements.
2. Acquires a hold on the replacement slot.
3. Stages an approval record for the new provider, slot, and price.
4. Contacts the user with details.
5. REQUIRES explicit user approval before executing the replacement booking.
"""

from typing import Any, Dict, Optional
from orange_invest.approval import ApprovalGuard
from orange_invest.mock_api import MockProviderAPI, SlotUnavailableError
from orange_invest.models import ActionType, SlotHold, WorkflowStatus
from orange_invest.state_machine import WorkflowState
from orange_invest.tracer import Tracer


class ProactiveFollowUpHandler:
    """Manages provider cancellation events and replacement orchestration."""

    def __init__(self, api: MockProviderAPI, approval_guard: ApprovalGuard, tracer: Tracer):
        self.api = api
        self.approval_guard = approval_guard
        self.tracer = tracer

    def handle_cancellation_event(
        self,
        booking_id: str,
        state: WorkflowState,
        reason: str = "Provider emergency",
    ) -> Dict[str, Any]:
        """
        Handle external provider cancellation event proactively.
        Returns a response payload with user message, replacement details, and next status.
        """
        self.tracer.log_event("proactive_event", "provider_cancellation_received", {
            "booking_id": booking_id,
            "reason": reason,
        })

        # Mark booking cancelled in API
        try:
            cancelled_booking = self.api.simulate_provider_cancellation(booking_id, reason=reason)
        except Exception as e:
            return {
                "success": False,
                "message": f"Could not find booking {booking_id} for cancellation event: {e}",
            }

        service = cancelled_booking.get("service", "service")
        old_provider_name = cancelled_booking.get("provider_name", "Provider")
        old_provider_id = cancelled_booking.get("provider_id")
        area = state.area or "Indiranagar"
        old_slot = cancelled_booking.get("slot")

        self.tracer.log_state_transition(
            state.status.value,
            WorkflowStatus.SEARCHING.value,
            reason=f"Proactive search for replacement following cancellation of booking {booking_id}",
        )
        state.status = WorkflowStatus.SEARCHING

        # Search replacements
        providers = self.api.search_providers(service=service, area=area)
        self.tracer.log_tool_call(
            "search_providers",
            {"service": service, "area": area},
            result=providers,
        )

        # Select a replacement other than the cancelled provider
        replacement = None
        for p in providers:
            if p["id"] != old_provider_id and p["free_slots"]:
                replacement = p
                break

        if not replacement:
            state.status = WorkflowStatus.FAILED
            msg = (
                f"Your booking with {old_provider_name} for {service} was cancelled ({reason}). "
                f"Unfortunately, no replacement providers are currently available in {area}."
            )
            return {"success": False, "message": msg}

        target_slot = replacement["free_slots"][0]
        # Acquire hold
        try:
            hold_res = self.api.hold_slot(replacement["id"], target_slot)
            self.tracer.log_tool_call(
                "hold_slot",
                {"provider_id": replacement["id"], "slot": target_slot},
                result=hold_res,
            )
            hold = SlotHold(
                hold_id=hold_res["hold_id"],
                provider_id=replacement["id"],
                slot=target_slot,
                quoted_price=hold_res["quoted_price"],
                expires_at=hold_res["expires_at"],
            )
            state.current_hold = hold
            state.selected_provider = replacement
            state.selected_slot = target_slot
        except SlotUnavailableError as e:
            state.status = WorkflowStatus.FAILED
            return {
                "success": False,
                "message": f"Replacement slot became unavailable while attempting to hold: {e}",
            }

        # Stage approval for user
        self.approval_guard.stage_approval(
            action=ActionType.BOOK,
            provider_id=replacement["id"],
            provider_name=replacement["name"],
            slot=target_slot,
            price=hold.quoted_price,
        )

        state.status = WorkflowStatus.AWAITING_REPLACEMENT_APPROVAL
        self.tracer.log_state_transition(
            WorkflowStatus.SEARCHING.value,
            WorkflowStatus.AWAITING_REPLACEMENT_APPROVAL.value,
            reason="Held replacement slot, awaiting user approval",
        )

        # Invariant check: ensure approval is still PENDING, not prematurely approved
        self.tracer.log_invariant_check(
            "replacement_requires_user_approval",
            passed=(self.approval_guard.active_record.status.value == "PENDING"),
            details={
                "replacement_provider": replacement["name"],
                "slot": target_slot,
                "price": hold.quoted_price,
            },
        )

        user_message = (
            f"Alert: Your booking with {old_provider_name} for {service} ({old_slot}) "
            f"was cancelled by the provider due to '{reason}'.\n"
            f"I have found and held a replacement for you:\n"
            f"- Provider: {replacement['name']} (Rating: {replacement['rating']}★)\n"
            f"- Slot: {target_slot}\n"
            f"- Price: ₹{hold.quoted_price:,.2f}\n"
            f"Would you like me to confirm this replacement booking?"
        )

        return {
            "success": True,
            "message": user_message,
            "replacement": replacement,
            "hold": hold,
        }

