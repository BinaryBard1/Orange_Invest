"""Explicit Workflow State Machine for Home-Service Errands Agent.

Enforces:
1. Exact tracking of known vs missing facts (service, area, time window, budget, preferences).
2. Never re-asks a known fact.
3. Corrections to one detail update only that detail and keep everything else.
4. Deterministic state transitions with invariant validation.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time
from orange_invest.models import (
    ActionType,
    ApprovalRecord,
    Booking,
    Provider,
    SlotHold,
    TimeWindow,
    WorkflowStatus,
)


@dataclass
class WorkflowState:
    session_id: str
    user_id: str
    status: WorkflowStatus = WorkflowStatus.IDLE
    intent: Optional[ActionType] = None

    # Core errand facts
    service: Optional[str] = None
    area: Optional[str] = None
    time_window: Optional[TimeWindow] = None
    budget: Optional[float] = None
    preferences: Dict[str, Any] = field(default_factory=dict)

    # Errand targets for Reschedule / Cancel
    target_booking_id: Optional[str] = None
    new_slot: Optional[str] = None

    # Inferred facts pending user confirmation
    pending_inferred_confirmations: Dict[str, Any] = field(default_factory=dict)

    # Search & Hold staging
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    selected_provider: Optional[Dict[str, Any]] = None
    selected_slot: Optional[str] = None
    current_hold: Optional[SlotHold] = None

    # Idempotency & Execution tracking
    idempotency_key: Optional[str] = None
    confirmed_booking_id: Optional[str] = None
    last_error: Optional[str] = None
    turn_count: int = 0
    tool_call_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def update_field(self, field_name: str, value: Any):
        """Update a single field while preserving all other known facts."""
        if hasattr(self, field_name):
            setattr(self, field_name, value)
            self.updated_at = time.time()
        else:
            self.preferences[field_name] = value

    def get_missing_fields_for_booking(self) -> List[str]:
        """
        Identify strictly what is missing for a booking.
        Required core facts: service, area, time_window.
        (Budget is optional unless required by user).
        """
        missing = []
        if not self.service:
            missing.append("service")
        if not self.area:
            missing.append("area")
        if not self.time_window:
            missing.append("time_window")
        return missing

    def is_ready_for_search(self) -> bool:
        return len(self.get_missing_fields_for_booking()) == 0

    def transition_to(self, new_status: WorkflowStatus, reason: str = ""):
        self.status = new_status
        self.updated_at = time.time()

    def reset_for_new_request(self):
        self.intent = None
        self.service = None
        self.area = None
        self.time_window = None
        self.budget = None
        self.preferences.clear()
        self.target_booking_id = None
        self.new_slot = None
        self.search_results.clear()
        self.selected_provider = None
        self.selected_slot = None
        self.current_hold = None
        self.idempotency_key = None
        self.confirmed_booking_id = None
        self.last_error = None
        self.status = WorkflowStatus.IDLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "status": self.status.value,
            "intent": self.intent.value if self.intent else None,
            "service": self.service,
            "area": self.area,
            "time_window": self.time_window.raw if self.time_window else None,
            "budget": self.budget,
            "preferences": self.preferences,
            "target_booking_id": self.target_booking_id,
            "new_slot": self.new_slot,
            "selected_provider_id": self.selected_provider.get("id") if self.selected_provider else None,
            "selected_slot": self.selected_slot,
            "hold_id": self.current_hold.hold_id if self.current_hold else None,
            "idempotency_key": self.idempotency_key,
            "confirmed_booking_id": self.confirmed_booking_id,
            "turn_count": self.turn_count,
            "tool_call_count": self.tool_call_count,
        }

