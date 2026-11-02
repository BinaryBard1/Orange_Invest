"""Structured tracing system for home-service errands agent.

Captures:
- State transitions
- Model calls (messages, responses, tokens, latency, cost)
- Tool calls (name, args, sanitised results, latency, errors)
- Invariant checks
- Sanitization of secrets and sensitive data
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from orange_invest.models import TraceEvent, TraceRecord


SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|password|auth[_-]?token|bearer)\s*[:=]\s*['\"]?([^'\"\s&]+)"), r"\1: [REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-\._~+/]+=*", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"), "[REDACTED_CC]"),
]


def sanitize_data(data: Any) -> Any:
    """Recursively sanitize data structures to strip secrets and sensitive credentials."""
    if isinstance(data, str):
        sanitized = data
        for pattern, replacement in SENSITIVE_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized
    elif isinstance(data, dict):
        sanitized_dict = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("secret", "password", "token", "auth_key", "api_key")):
                sanitized_dict[k] = "[REDACTED]"
            else:
                sanitized_dict[k] = sanitize_data(v)
        return sanitized_dict
    elif isinstance(data, (list, tuple, set)):
        return [sanitize_data(item) for item in data]
    return data


class Tracer:
    """Captures structured, auditable traces for every run."""

    def __init__(self, trace_dir: Optional[str] = "traces"):
        self.trace_dir = trace_dir
        if self.trace_dir:
            os.makedirs(self.trace_dir, exist_ok=True)
        self.current_record: Optional[TraceRecord] = None

    def start_run(self, user_id: str, run_id: Optional[str] = None) -> TraceRecord:
        record = TraceRecord(user_id=user_id)
        if run_id:
            record.run_id = run_id
        record.timestamp_start = time.time()
        self.current_record = record
        return record

    def log_state_transition(self, from_state: str, to_state: str, reason: str = ""):
        if not self.current_record:
            return
        event = TraceEvent(
            timestamp=time.time(),
            event_type="state_transition",
            name=f"{from_state} -> {to_state}",
            data={"from_state": from_state, "to_state": to_state, "reason": sanitize_data(reason)},
        )
        self.current_record.events.append(event)

    def log_model_call(
        self,
        messages: List[Dict[str, Any]],
        response: str,
        latency_ms: float = 0.0,
        tokens_prompt: int = 0,
        tokens_completion: int = 0,
        cost_usd: float = 0.0,
    ):
        if not self.current_record:
            return
        total_tokens = tokens_prompt + tokens_completion
        self.current_record.total_tokens += total_tokens
        self.current_record.estimated_cost_usd += cost_usd

        event = TraceEvent(
            timestamp=time.time(),
            event_type="model_call",
            name="llm_inference",
            data={
                "messages": sanitize_data(messages),
                "response": sanitize_data(response),
                "tokens_prompt": tokens_prompt,
                "tokens_completion": tokens_completion,
                "total_tokens": total_tokens,
            },
            latency_ms=latency_ms,
            tokens=total_tokens,
            cost_usd=cost_usd,
        )
        self.current_record.events.append(event)

    def log_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Any,
        latency_ms: float = 0.0,
        error: Optional[str] = None,
    ):
        if not self.current_record:
            return
        event = TraceEvent(
            timestamp=time.time(),
            event_type="tool_call",
            name=tool_name,
            data={
                "args": sanitize_data(args),
                "result": sanitize_data(result),
                "error": sanitize_data(error),
            },
            latency_ms=latency_ms,
        )
        self.current_record.events.append(event)

    def log_invariant_check(self, invariant_name: str, passed: bool, details: Dict[str, Any]):
        if not self.current_record:
            return
        event = TraceEvent(
            timestamp=time.time(),
            event_type="invariant_check",
            name=invariant_name,
            data={"passed": passed, "details": sanitize_data(details)},
        )
        self.current_record.events.append(event)

    def log_event(self, event_type: str, name: str, data: Dict[str, Any]):
        if not self.current_record:
            return
        event = TraceEvent(
            timestamp=time.time(),
            event_type=event_type,
            name=name,
            data=sanitize_data(data),
        )
        self.current_record.events.append(event)

    def end_run(self, success: bool, terminal_message: str) -> TraceRecord:
        if not self.current_record:
            raise RuntimeError("No active trace record to end.")

        rec = self.current_record
        rec.timestamp_end = time.time()
        rec.total_latency_ms = (rec.timestamp_end - rec.timestamp_start) * 1000.0
        rec.success = success
        rec.terminal_message = sanitize_data(terminal_message)

        if self.trace_dir:
            file_path = os.path.join(self.trace_dir, f"{rec.run_id}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(rec), f, indent=2)

        return rec

    def to_dict(self, record: Optional[TraceRecord] = None) -> Dict[str, Any]:
        rec = record or self.current_record
        if not rec:
            return {}
        return {
            "run_id": rec.run_id,
            "user_id": rec.user_id,
            "timestamp_start": rec.timestamp_start,
            "timestamp_end": rec.timestamp_end,
            "total_latency_ms": round(rec.total_latency_ms, 2),
            "total_tokens": rec.total_tokens,
            "estimated_cost_usd": round(rec.estimated_cost_usd, 6),
            "success": rec.success,
            "terminal_message": rec.terminal_message,
            "events_count": len(rec.events),
            "events": [
                {
                    "timestamp": e.timestamp,
                    "event_type": e.event_type,
                    "name": e.name,
                    "data": e.data,
                    "latency_ms": round(e.latency_ms, 2),
                    "tokens": e.tokens,
                    "cost_usd": round(e.cost_usd, 6),
                }
                for e in rec.events
            ],
        }

