"""Core data models and type definitions for Orange Invest home-service errands agent."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import time
import uuid


class ActionType(str, Enum):
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    SEARCH = "search"
    HOLD = "hold"
    MANAGE_MEMORY = "manage_memory"


class WorkflowStatus(str, Enum):
    IDLE = "IDLE"
    COLLECTING_INFO = "COLLECTING_INFO"
    SEARCHING = "SEARCHING"
    PROPOSED = "PROPOSED"
    HOLD_ACQUIRED = "HOLD_ACQUIRED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    AWAITING_REPLACEMENT_APPROVAL = "AWAITING_REPLACEMENT_APPROVAL"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    VOIDED = "VOIDED"
    CONSUMED = "CONSUMED"


@dataclass
class ApprovalRecord:
    id: str = field(default_factory=lambda: f"appr_{uuid.uuid4().hex[:8]}")
    action: ActionType = ActionType.BOOK
    provider_id: str = ""
    provider_name: str = ""
    slot: str = ""
    price: float = 0.0
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    approved_at: Optional[float] = None
    void_reason: Optional[str] = None
    consumed_at: Optional[float] = None

    def matches(self, action: ActionType, provider_id: str, slot: str, price: float) -> bool:
        """Code-enforced equality check: exact action, provider, slot, and price."""
        return (
            self.action == action
            and self.provider_id == provider_id
            and self.slot == slot
            and abs(self.price - price) < 1e-4
        )

    def is_valid_and_approved(self) -> bool:
        return self.status == ApprovalStatus.APPROVED


@dataclass
class Provider:
    id: str
    name: str
    service: str
    area: str
    price: float
    rating: float
    free_slots: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    reviews: List[Dict[str, Any]] = field(default_factory=list)

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "service": self.service,
            "area": self.area,
            "price": self.price,
            "rating": self.rating,
            "free_slots": self.free_slots,
            "attributes": self.attributes,
        }


@dataclass
class SlotHold:
    hold_id: str
    provider_id: str
    slot: str
    quoted_price: float
    expires_at: float  # Epoch timestamp (default 10 min from creation)
    created_at: float = field(default_factory=time.time)

    def is_expired(self, current_time: Optional[float] = None) -> bool:
        now = current_time if current_time is not None else time.time()
        return now >= self.expires_at


@dataclass
class Booking:
    id: str
    user_id: str
    provider_id: str
    provider_name: str
    service: str
    slot: str
    price: float
    status: str = "confirmed"  # confirmed, completed, cancelled, cancelled_by_provider
    idempotency_key: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class FactSource(str, Enum):
    STATED = "stated"      # Explicitly stated by the user
    INFERRED = "inferred"  # Inferred by the agent (must NOT be used without confirmation)


class FactStatus(str, Enum):
    ACTIVE = "active"
    FORGOTTEN = "forgotten"  # Tombstoned, never reused


@dataclass
class MemoryFact:
    key: str
    value: Any
    source: FactSource
    status: FactStatus = FactStatus.ACTIVE
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tombstoned_at: Optional[float] = None


@dataclass
class TimeWindow:
    raw: str
    day: Optional[str] = None           # e.g., "Saturday", "2026-11-07"
    time_of_day: Optional[str] = None   # "morning", "afternoon", "evening"
    not_before_hour: Optional[int] = None # e.g. 10 (for "not before 10 a.m.")
    not_after_hour: Optional[int] = None  # e.g. 13 (for "mornings only")

    def matches_slot(self, slot_str: str) -> bool:
        """Check if a slot matches this time window constraint."""
        s_lower = slot_str.lower()
        if self.day and self.day.lower() not in ("this week", "any day", "upcoming"):
            day_lower = self.day.lower()
            if day_lower not in s_lower:
                return False

        # Parse hour if available (e.g., "2026-11-07 09:00" or "Saturday 11:00 AM")
        hour = None
        for part in slot_str.replace(":", " ").split():
            if part.isdigit():
                val = int(part)
                if 0 <= val <= 23:
                    hour = val
                    break
        if "pm" in s_lower and hour is not None and hour < 12:
            hour += 12
        if "am" in s_lower and hour == 12:
            hour = 0

        if hour is not None:
            if self.not_before_hour is not None and hour < self.not_before_hour:
                return False
            if self.not_after_hour is not None and hour > self.not_after_hour:
                return False
            if self.time_of_day == "morning" and not (6 <= hour < 12):
                return False
            if self.time_of_day == "afternoon" and not (12 <= hour < 17):
                return False
            if self.time_of_day == "evening" and not (17 <= hour < 22):
                return False

        return True


@dataclass
class TraceEvent:
    timestamp: float = field(default_factory=time.time)
    event_type: str = ""  # state_transition, model_call, tool_call, invariant_check, error, memory
    name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class TraceRecord:
    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:10]}")
    user_id: str = "default_user"
    timestamp_start: float = field(default_factory=time.time)
    timestamp_end: float = 0.0
    total_latency_ms: float = 0.0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    events: List[TraceEvent] = field(default_factory=list)
    success: bool = True
    terminal_message: str = ""
