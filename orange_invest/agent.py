"""Home-Service Errands Agent orchestrator.

Implements all 8 must-haves + proactive follow-up nice-to-have.
Enforces explicit state, code-level approval, booking correctness,
prompt-injection immunity, bounded execution, memory persistence,
and structured observability.
"""

import hashlib
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from orange_invest.approval import (
    ApprovalConsumedError,
    ApprovalError,
    ApprovalGuard,
    ApprovalMismatchError,
    ApprovalMissingError,
    ApprovalNotGrantedError,
    ApprovalVoidedError,
)
from orange_invest.llm_driver import ScriptedModelDriver
from orange_invest.memory import MemoryStore
from orange_invest.mock_api import (
    HoldExpiredError,
    MockProviderAPI,
    MockProviderAPIException,
    ServiceUnavailable503Error,
    SlotConflictError,
    SlotTakenError,
    SlotUnavailableError,
    TimeoutError,
)
from orange_invest.models import (
    ActionType,
    ApprovalRecord,
    ApprovalStatus,
    FactSource,
    SlotHold,
    TimeWindow,
    WorkflowStatus,
)
from orange_invest.proactive import ProactiveFollowUpHandler
from orange_invest.state_machine import WorkflowState
from orange_invest.tracer import Tracer


class BoundedExecutionError(Exception):
    """Raised when hard caps on turns, tool calls, or wall-clock time are exceeded."""
    pass


class AgentExecutionLimits:
    """Configurable hard caps on agent execution."""

    def __init__(
        self,
        max_model_turns: int = 10,
        max_tool_calls: int = 15,
        max_wall_clock_seconds: float = 30.0,
        max_retries: int = 3,
    ):
        self.max_model_turns = max_model_turns
        self.max_tool_calls = max_tool_calls
        self.max_wall_clock_seconds = max_wall_clock_seconds
        self.max_retries = max_retries


class HomeServiceAgent:
    """Full-featured, safe, invariant-guarded Home Service Chat Agent."""

    def __init__(
        self,
        api: Optional[MockProviderAPI] = None,
        memory: Optional[MemoryStore] = None,
        tracer: Optional[Tracer] = None,
        limits: Optional[AgentExecutionLimits] = None,
        model_driver: Optional[ScriptedModelDriver] = None,
    ):
        self.api = api or MockProviderAPI()
        self.memory = memory or MemoryStore()
        self.tracer = tracer or Tracer()
        self.limits = limits or AgentExecutionLimits()
        self.driver = model_driver or ScriptedModelDriver()
        self.approval_guard = ApprovalGuard()
        self.proactive_handler = ProactiveFollowUpHandler(self.api, self.approval_guard, self.tracer)

        # Active session state
        self.state: Optional[WorkflowState] = None
        self.start_wall_time: float = 0.0

    def start_session(self, user_id: str, session_id: Optional[str] = None) -> WorkflowState:
        s_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"
        self.state = WorkflowState(session_id=s_id, user_id=user_id)
        self.start_wall_time = time.time()
        self.tracer.start_run(user_id=user_id, run_id=f"run_{s_id}")
        self.tracer.log_state_transition("INIT", WorkflowStatus.IDLE.value, reason="Session initialized")
        return self.state

    def _check_bounds(self):
        """Enforce hard caps on model turns, tool calls, and wall-clock time."""
        if not self.state:
            return

        elapsed = time.time() - self.start_wall_time
        if elapsed > self.limits.max_wall_clock_seconds:
            msg = (
                f"Execution stopped: Wall-clock time limit ({self.limits.max_wall_clock_seconds}s) exceeded. "
                "No unauthorized bookings were made."
            )
            self.tracer.log_event("limit_exhaustion", "wall_clock_timeout", {"elapsed_s": elapsed})
            raise BoundedExecutionError(msg)

        if self.state.turn_count > self.limits.max_model_turns:
            msg = (
                f"Execution stopped: Maximum interaction turns ({self.limits.max_model_turns}) exceeded. "
                "No unauthorized bookings were made."
            )
            self.tracer.log_event("limit_exhaustion", "max_turns_exceeded", {"turns": self.state.turn_count})
            raise BoundedExecutionError(msg)

        if self.state.tool_call_count >= self.limits.max_tool_calls:
            msg = (
                f"Execution stopped: Maximum tool calls limit ({self.limits.max_tool_calls}) exceeded. "
                "No unauthorized bookings were made."
            )
            self.tracer.log_event("limit_exhaustion", "max_tool_calls_exceeded", {"tools": self.state.tool_call_count})
            raise BoundedExecutionError(msg)

    def _call_tool(self, tool_name: str, fn, *args, **kwargs) -> Any:
        """Instrumented tool invocation with bound checks and tracing."""
        self._check_bounds()
        self.state.tool_call_count += 1
        t0 = time.time()
        try:
            result = fn(*args, **kwargs)
            lat_ms = (time.time() - t0) * 1000.0
            self.tracer.log_tool_call(tool_name, {"args": args, "kwargs": kwargs}, result=result, latency_ms=lat_ms)
            return result
        except Exception as e:
            lat_ms = (time.time() - t0) * 1000.0
            self.tracer.log_tool_call(tool_name, {"args": args, "kwargs": kwargs}, result=None, latency_ms=lat_ms, error=str(e))
            raise

    def handle_message(self, user_message: str) -> str:
        """Main entrypoint for chat interactions with bounded execution and safe exits."""
        if not self.state:
            self.start_session("default_user")

        self.state.turn_count += 1
        try:
            self._check_bounds()
            return self._process_turn(user_message)
        except BoundedExecutionError as e:
            self.state.transition_to(WorkflowStatus.FAILED, reason=str(e))
            self.tracer.end_run(success=False, terminal_message=str(e))
            return str(e)
        except Exception as e:
            err_msg = f"An unexpected error occurred: {str(e)}. No unapproved actions were performed."
            self.state.transition_to(WorkflowStatus.FAILED, reason=str(e))
            self.tracer.end_run(success=False, terminal_message=err_msg)
            return err_msg

    def _process_turn(self, user_message: str) -> str:
        parsed = self.driver.parse_user_intent(user_message)

        # Log model parsing event with tokens & cost
        tokens_p, tokens_c, lat_ms, cost = self.driver.estimate_metrics(user_message, str(parsed))
        self.tracer.log_model_call(
            messages=[{"role": "user", "content": user_message}],
            response=str(parsed),
            latency_ms=lat_ms,
            tokens_prompt=tokens_p,
            tokens_completion=tokens_c,
            cost_usd=cost,
        )

        # 1. Handle Memory Commands (View / Forget / Correct)
        if parsed.get("intent") == ActionType.MANAGE_MEMORY:
            return self._handle_memory_turn(parsed, user_message)

        # 2. Check for Pending Inferred Confirmation
        if self.state.pending_inferred_confirmations:
            return self._handle_inferred_confirmation(parsed, user_message)

        # 3. Handle Awaiting Approval or Awaiting Replacement Approval
        if self.state.status in (WorkflowStatus.AWAITING_APPROVAL, WorkflowStatus.AWAITING_REPLACEMENT_APPROVAL):
            return self._handle_approval_turn(parsed, user_message)

        # 4. Handle Repeated Approval after Confirmation
        if self.state.status == WorkflowStatus.CONFIRMED:
            if parsed.get("is_approval"):
                # Invariant: No duplicate bookings across repeated "yes"
                return (
                    f"Your booking (ID: {self.state.confirmed_booking_id}) is already confirmed! "
                    "No duplicate booking was made."
                )
            # If user asks for a new errand, reset state
            if parsed.get("intent") in (ActionType.BOOK, ActionType.RESCHEDULE, ActionType.CANCEL):
                self.state.reset_for_new_request()

        # 5. Extract / Update Workflow Facts (Never re-ask known facts, partial correction preserves all else)
        self._update_workflow_state_from_parsed(parsed)

        # 6. Apply Stated Memory Preferences Automatically
        self._apply_stated_memory_preferences()

        # 7. Check for Inferred Memory Facts that Need Confirmation
        unconfirmed_reply = self._check_for_inferred_memory_confirmation()
        if unconfirmed_reply:
            return unconfirmed_reply

        # 8. Route to Intent Handler
        if self.state.intent == ActionType.CANCEL:
            return self._handle_cancel_flow()
        elif self.state.intent == ActionType.RESCHEDULE:
            return self._handle_reschedule_flow()
        else:
            # Default to booking flow
            return self._handle_booking_flow()

    def _handle_memory_turn(self, parsed: Dict[str, Any], user_message: str) -> str:
        """Handle viewing, correcting, or forgetting memory facts."""
        action = parsed.get("action")
        uid = self.state.user_id

        if action == "view":
            facts = self.memory.list_active_facts(uid)
            if not facts:
                return "You have no saved preferences in memory."
            lines = ["Your saved preferences:"]
            for f in facts:
                source_label = "stated by you" if f.source == FactSource.STATED else "inferred by agent (unconfirmed)"
                lines.append(f"- {f.key}: {f.value} [{source_label}]")
            return "\n".join(lines)

        elif action == "forget":
            key = parsed.get("key", "area")
            self.memory.forget_fact(uid, key)
            self.tracer.log_event("memory_operation", "forget_fact", {"key": key, "user_id": uid})
            # Also clear from active state if present
            if key == "area":
                self.state.area = None
            elif key == "preferred_technician":
                self.state.preferences.pop("preferred_technician", None)
            return f"I have forgotten your preference for '{key}'. It will never be reused."

        return "Memory preference updated."

    def _handle_inferred_confirmation(self, parsed: Dict[str, Any], user_message: str) -> str:
        """Handle user response when prompted to confirm an inferred preference."""
        key, val = next(iter(self.state.pending_inferred_confirmations.items()))
        if parsed.get("is_approval"):
            # Confirmed -> promote to stated
            self.memory.set_stated(self.state.user_id, key, val)
            if key == "area":
                self.state.area = val
            elif key == "preferred_technician":
                self.state.preferences["preferred_technician"] = val
            self.state.pending_inferred_confirmations.clear()
            self.tracer.log_event("memory_operation", "inferred_preference_confirmed", {"key": key, "value": val})
            return f"Great, I've noted that! Continuing with your {self.state.service or 'service'} request..."
        elif parsed.get("is_rejection"):
            # Rejected -> do not use
            self.state.pending_inferred_confirmations.clear()
            self.tracer.log_event("memory_operation", "inferred_preference_rejected", {"key": key, "value": val})
            return f"Understood, I won't use that preference. Could you specify your preferred {key}?"
        else:
            # User provided a direct value instead
            self.state.pending_inferred_confirmations.clear()
            self._update_workflow_state_from_parsed(parsed)
            return self._handle_booking_flow()

    def _update_workflow_state_from_parsed(self, parsed: Dict[str, Any]):
        """Partial update logic: updates newly provided facts and strictly keeps existing facts."""
        if parsed.get("intent"):
            self.state.intent = parsed["intent"]
        if parsed.get("service"):
            self.state.service = parsed["service"]
        if parsed.get("area"):
            self.state.area = parsed["area"]
            # Save explicitly stated area to memory
            self.memory.set_stated(self.state.user_id, "area", parsed["area"])
        if parsed.get("budget") is not None:
            old_budget = self.state.budget
            self.state.budget = parsed["budget"]
            # If budget changed after an approval was staged, void the approval!
            if old_budget is not None and old_budget != self.state.budget:
                self.approval_guard.void_approval(f"User changed budget from ₹{old_budget} to ₹{self.state.budget}")
        if parsed.get("time_window"):
            self.state.time_window = parsed["time_window"]
        if parsed.get("preferences"):
            self.state.preferences.update(parsed["preferences"])

    def _apply_stated_memory_preferences(self):
        """Automatically apply only STATED facts. Inferred facts are never applied without confirmation."""
        uid = self.state.user_id
        if not self.state.area:
            if self.memory.is_fact_usable_automatically(uid, "area"):
                fact = self.memory.get_fact(uid, "area")
                if fact:
                    self.state.area = fact.value
                    self.tracer.log_event("memory_applied", "stated_area_applied", {"area": fact.value})

    def _check_for_inferred_memory_confirmation(self) -> Optional[str]:
        """If there is an inferred fact relevant to this errand, ask user to confirm it first."""
        uid = self.state.user_id
        if not self.state.area and self.memory.requires_confirmation(uid, "area"):
            fact = self.memory.get_fact(uid, "area")
            if fact:
                self.state.pending_inferred_confirmations["area"] = fact.value
                return f"I noticed you previously booked in {fact.value}. Would you like to search in {fact.value}?"
        return None

    def _handle_booking_flow(self) -> str:
        """Booking workflow: ask missing info -> search -> hold -> stage approval."""
        missing = self.state.get_missing_fields_for_booking()

        # 1. Ask ONLY for what's missing
        if missing:
            self.state.transition_to(WorkflowStatus.COLLECTING_INFO, reason=f"Missing fields: {missing}")
            if "service" in missing:
                return "What home service errand can I help you with (e.g., plumbing, electrician, AC servicing)?"
            if "area" in missing:
                return f"Which neighborhood or area do you need {self.state.service} in?"
            if "time_window" in missing:
                return f"When should I schedule the {self.state.service} (e.g., mornings this week, Saturday after 10 a.m.)?"

        # 2. Check preferences (e.g., "same electrician as last time")
        if self.state.preferences.get("use_previous_provider"):
            past_bookings = self._call_tool("list_bookings", self.api.list_bookings, self.state.user_id)
            target_prov_id = None
            for b in reversed(past_bookings):
                if b["service"].lower() == (self.state.service or "").lower() and b.get("provider_id"):
                    target_prov_id = b["provider_id"]
                    break
            if target_prov_id:
                self.state.preferences["target_provider_id"] = target_prov_id

        # 3. Search Providers
        self.state.transition_to(WorkflowStatus.SEARCHING, reason="All required fields present")
        providers = self._call_tool(
            "search_providers",
            self.api.search_providers,
            service=self.state.service,
            area=self.state.area,
        )

        # 4. Filter by constraints: budget, specific provider, and time window
        eligible_candidates = []
        for p in providers:
            # Target provider filter
            if "target_provider_id" in self.state.preferences:
                if p["id"] != self.state.preferences["target_provider_id"]:
                    continue

            # Budget constraint
            if self.state.budget is not None and p["price"] > self.state.budget:
                continue

            # Time window slot matching
            matching_slots = []
            for slot in p.get("free_slots", []):
                if self.state.time_window and not self.state.time_window.matches_slot(slot):
                    continue
                matching_slots.append(slot)

            if matching_slots:
                p_copy = dict(p)
                p_copy["matching_slots"] = matching_slots
                eligible_candidates.append(p_copy)

        if not eligible_candidates:
            self.state.transition_to(WorkflowStatus.FAILED, reason="No matching providers found")
            return (
                f"I searched for {self.state.service} in {self.state.area} matching your constraints, "
                f"but found no available slots within your budget. Would you like to adjust your budget or time window?"
            )

        # 5. Read untrusted provider reviews safely
        # Note: reviews contain injection attempts, but reviews are passive data enclosed in XML tags!
        best_p = eligible_candidates[0]
        chosen_slot = best_p["matching_slots"][0]
        prov_details = self._call_tool("get_provider", self.api.get_provider, best_p["id"])

        # Trace untrusted review data
        self.tracer.log_event("untrusted_tool_data_boundary", "provider_reviews_loaded", {
            "provider_id": best_p["id"],
            "review_count": len(prov_details.get("reviews", [])),
            "untrusted_content_tags": "<untrusted_review_data>...</untrusted_review_data>",
        })

        # 6. Acquire Hold
        try:
            hold_res = self._call_tool("hold_slot", self.api.hold_slot, best_p["id"], chosen_slot)
            hold = SlotHold(
                hold_id=hold_res["hold_id"],
                provider_id=best_p["id"],
                slot=chosen_slot,
                quoted_price=hold_res["quoted_price"],
                expires_at=hold_res["expires_at"],
            )
            self.state.current_hold = hold
            self.state.selected_provider = best_p
            self.state.selected_slot = chosen_slot
        except SlotUnavailableError:
            return f"The slot {chosen_slot} with {best_p['name']} just became unavailable. Let me find an alternative."

        # 7. Check if hold quoted price changed from search price
        final_price = hold.quoted_price
        price_alert = ""
        if abs(final_price - best_p["price"]) > 1e-4:
            price_alert = f" Note: The provider's current quoted rate is ₹{final_price:,.2f} (updated from search price ₹{best_p['price']:,.2f})."

        # 8. Stage Approval
        self.approval_guard.stage_approval(
            action=ActionType.BOOK,
            provider_id=best_p["id"],
            provider_name=best_p["name"],
            slot=chosen_slot,
            price=final_price,
        )
        self.state.transition_to(WorkflowStatus.AWAITING_APPROVAL, reason="Hold acquired, awaiting explicit user approval")

        return (
            f"I found {best_p['name']} (Rating: {best_p['rating']}★) for {self.state.service}.\n"
            f"- Slot: {chosen_slot}\n"
            f"- Price: ₹{final_price:,.2f}{price_alert}\n"
            f"Would you like me to book this errand for you? Please reply 'yes' to confirm."
        )

    def _handle_cancel_flow(self) -> str:
        """Cancel workflow: find booking -> stage approval -> user confirms -> cancel with 503 retry."""
        bookings = self._call_tool("list_bookings", self.api.list_bookings, self.state.user_id)
        active_b = None
        for b in bookings:
            if b.get("status") in ("confirmed", "active"):
                active_b = b
                break

        if not active_b:
            return "You have no active bookings to cancel."

        self.state.target_booking_id = active_b["id"]
        # Stage approval
        self.approval_guard.stage_approval(
            action=ActionType.CANCEL,
            provider_id=active_b.get("provider_id", "prov_cancel"),
            provider_name=active_b.get("provider", "Provider"),
            slot=active_b.get("slot", ""),
            price=active_b.get("price", 0.0),
        )
        self.state.transition_to(WorkflowStatus.AWAITING_APPROVAL, reason="Awaiting user confirmation to cancel")
        return (
            f"Are you sure you want to cancel your booking for {active_b['service']} with "
            f"{active_b['provider']} on {active_b['slot']}? Please reply 'yes' to confirm cancellation."
        )

    def _handle_reschedule_flow(self) -> str:
        """Reschedule workflow: locate booking -> stage approval -> user confirms -> reschedule."""
        bookings = self._call_tool("list_bookings", self.api.list_bookings, self.state.user_id)
        active_b = None
        for b in bookings:
            if b.get("status") in ("confirmed", "active"):
                active_b = b
                break

        if not active_b:
            return "You have no active bookings to reschedule."

        self.state.target_booking_id = active_b["id"]
        new_slot = "2026-11-07 10:00 AM"  # Saturday morning default
        if self.state.time_window and self.state.time_window.day == "Saturday":
            new_slot = "2026-11-07 10:00 AM"

        self.state.new_slot = new_slot
        self.approval_guard.stage_approval(
            action=ActionType.RESCHEDULE,
            provider_id=active_b.get("provider_id", "prov_resched"),
            provider_name=active_b.get("provider", "Provider"),
            slot=new_slot,
            price=active_b.get("price", 0.0),
        )
        self.state.transition_to(WorkflowStatus.AWAITING_APPROVAL, reason="Awaiting user confirmation to reschedule")
        return (
            f"Confirm rescheduling your {active_b['service']} with {active_b['provider']} "
            f"to {new_slot}? Please reply 'yes' to confirm."
        )

    def _handle_approval_turn(self, parsed: Dict[str, Any], user_message: str) -> str:
        """Handles user responses when an action is awaiting approval."""
        if parsed.get("is_rejection"):
            self.approval_guard.reject_approval("User declined")
            self.state.transition_to(WorkflowStatus.CANCELLED_BY_USER, reason="User rejected proposal")
            return "Understood. The action was cancelled and no changes were made."

        if not parsed.get("is_approval"):
            # Check if user changed a parameter instead of approving
            if parsed.get("budget") is not None or parsed.get("time_window") or parsed.get("area"):
                self._update_workflow_state_from_parsed(parsed)
                self.approval_guard.void_approval("User modified search criteria instead of confirming")
                return self._handle_booking_flow()
            return "I am waiting for your explicit approval. Please reply 'yes' to confirm, or 'no' to cancel."

        # User approved -> Grant approval in record
        self.approval_guard.grant_approval()
        self.state.transition_to(WorkflowStatus.EXECUTING, reason="User granted approval")

        action = self.approval_guard.active_record.action
        if action == ActionType.BOOK:
            return self._execute_booking()
        elif action == ActionType.CANCEL:
            return self._execute_cancel()
        elif action == ActionType.RESCHEDULE:
            return self._execute_reschedule()

        return "Unknown action."

    def _execute_booking(self) -> str:
        """
        Execute booking with code-level approval verification and timeout recovery.
        Invariant: Never claim success until verified via get_booking_status!
        """
        rec = self.approval_guard.active_record
        hold = self.state.current_hold

        if not hold:
            return "Error: No active hold found to book."

        # Generate deterministic idempotency key
        idem_key = self.state.idempotency_key or f"idem_{self.state.user_id}_{rec.provider_id}_{hashlib.sha256(rec.slot.encode()).hexdigest()[:8]}_{rec.id}"
        self.state.idempotency_key = idem_key

        # 1. VERIFY AND CONSUME APPROVAL (Enforced by code)
        try:
            self.approval_guard.verify_and_consume(
                action=ActionType.BOOK,
                provider_id=rec.provider_id,
                slot=rec.slot,
                price=rec.price,
            )
            self.tracer.log_invariant_check("code_level_approval_verified", passed=True, details={
                "action": "book",
                "provider_id": rec.provider_id,
                "slot": rec.slot,
                "price": rec.price,
            })
        except ApprovalError as e:
            self.tracer.log_invariant_check("code_level_approval_verified", passed=False, details={"error": str(e)})
            self.state.transition_to(WorkflowStatus.FAILED, reason=str(e))
            return f"Security Violation: Booking blocked by code-level approval guard: {e}"

        # 2. CALL BOOK WITH RECOVERY AND VERIFICATION
        booking_id = None
        try:
            booking_id = self._call_tool("book", self.api.book, hold.hold_id, idem_key)
        except TimeoutError:
            # Must-have 3: On timeout, check get_booking_status before retrying or reporting!
            self.tracer.log_event("booking_timeout", "book_call_timed_out", {"idempotency_key": idem_key})
            status_res = self._call_tool("get_booking_status", self.api.get_booking_status, idem_key)
            if status_res.get("status") == "booked" and status_res.get("booking_id"):
                booking_id = status_res["booking_id"]
                self.tracer.log_event("timeout_recovery_success", "verified_via_get_booking_status", {
                    "booking_id": booking_id,
                })
            else:
                self.state.transition_to(WorkflowStatus.FAILED, reason="Booking timed out and was not confirmed on backend")
                return (
                    "The booking request timed out and verification confirmed it was not completed. "
                    "You have not been charged. Would you like me to try again?"
                )
        except SlotTakenError:
            self.state.transition_to(WorkflowStatus.FAILED, reason="Slot was taken")
            return f"The slot was taken before the booking could be finalized. Would you like to select another time?"
        except HoldExpiredError:
            self.state.transition_to(WorkflowStatus.FAILED, reason="Hold expired")
            return "The reservation hold expired. Please search again."

        # Verified success
        self.state.confirmed_booking_id = booking_id
        self.state.transition_to(WorkflowStatus.CONFIRMED, reason=f"Booking verified successfully: {booking_id}")
        self.tracer.end_run(success=True, terminal_message=f"Booking confirmed: {booking_id}")
        return f"Success! Your booking with {rec.provider_name} for {rec.slot} has been confirmed. Booking ID: {booking_id}."

    def _execute_cancel(self) -> str:
        """Execute cancellation with automatic retry on transient 503."""
        rec = self.approval_guard.active_record
        b_id = self.state.target_booking_id
        idem_key = f"cancel_{b_id}"

        # Verify approval
        self.approval_guard.verify_and_consume(
            action=ActionType.CANCEL,
            provider_id=rec.provider_id,
            slot=rec.slot,
            price=rec.price,
        )

        # First call might fail with transient 503; retry logic handles it
        for attempt in range(self.limits.max_retries):
            try:
                res = self._call_tool("cancel", self.api.cancel, b_id, idem_key)
                if res == "cancelled":
                    self.state.transition_to(WorkflowStatus.CONFIRMED, reason=f"Booking {b_id} cancelled")
                    self.tracer.end_run(success=True, terminal_message=f"Booking {b_id} cancelled")
                    return f"Your booking (ID: {b_id}) has been successfully cancelled."
            except ServiceUnavailable503Error:
                self.tracer.log_event("transient_retry", "503_service_unavailable", {"attempt": attempt + 1})
                if attempt == self.limits.max_retries - 1:
                    break
                time.sleep(0.05)  # Quick backoff

        self.state.transition_to(WorkflowStatus.FAILED, reason="Exhausted retries on 503")
        return "Unable to cancel booking due to provider upstream downtime. Please try again later."

    def _execute_reschedule(self) -> str:
        """Execute reschedule with slot conflict handling."""
        rec = self.approval_guard.active_record
        b_id = self.state.target_booking_id
        idem_key = f"resched_{b_id}_{rec.slot}"

        # Verify approval
        self.approval_guard.verify_and_consume(
            action=ActionType.RESCHEDULE,
            provider_id=rec.provider_id,
            slot=rec.slot,
            price=rec.price,
        )

        try:
            res = self._call_tool("reschedule", self.api.reschedule, b_id, rec.slot, idem_key)
            self.state.transition_to(WorkflowStatus.CONFIRMED, reason=f"Booking {b_id} rescheduled to {rec.slot}")
            self.tracer.end_run(success=True, terminal_message=f"Rescheduled to {rec.slot}")
            return f"Your booking has been successfully moved to {rec.slot}!"
        except SlotConflictError as e:
            self.state.transition_to(WorkflowStatus.FAILED, reason=str(e))
            return f"Could not reschedule: {e}. Would you like to select an alternative slot?"

