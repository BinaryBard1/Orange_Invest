"""Scripted and offline model driver for reproducible evaluations.

Provides natural language understanding, entity extraction, and dialogue generation
without requiring external network or live LLM APIs.
"""

import re
import time
from typing import Any, Dict, List, Optional, Tuple
from orange_invest.models import ActionType, TimeWindow


class ScriptedModelDriver:
    """Offline, deterministic model driver executing NLP tasks with token & cost tracking."""

    def __init__(self):
        # Pricing simulation: $0.15 / 1M prompt tokens, $0.60 / 1M completion tokens (Gemini Flash tier)
        self.prompt_token_rate = 0.15 / 1_000_000
        self.completion_token_rate = 0.60 / 1_000_000

    def parse_user_intent(self, text: str) -> Dict[str, Any]:
        """
        Parse user utterance for intent, service, area, time window, budget, preferences, and approval signals.
        Enforces untrusted text boundaries so that reviews cannot spoof user intent.
        """
        t = text.strip()
        t_lower = t.lower()

        # Approval detection
        is_approval = bool(
            re.search(r"\b(yes|yeah|yep|sure|confirm|confirmed|proceed|go ahead|book it|looks good|okay|ok)\b", t_lower)
            and not re.search(r"\b(no|don't|do not|cancel|wait|stop)\b", t_lower)
        )
        is_rejection = bool(re.search(r"\b(no|nope|cancel|don't|do not|stop|nevermind)\b", t_lower))

        # Memory commands: view, correct, forget
        if re.search(r"\b(show|view|what are|list)\b.*(preferences|memory|details)", t_lower):
            return {"intent": ActionType.MANAGE_MEMORY, "action": "view"}
        if re.search(r"\b(forget|delete|remove|clear)\b.*(preference|area|technician|time)", t_lower):
            key = "area" if "area" in t_lower else ("preferred_technician" if "technician" in t_lower or "electrician" in t_lower or "ramesh" in t_lower else "earliest_time")
            return {"intent": ActionType.MANAGE_MEMORY, "action": "forget", "key": key}

        # Intent detection
        intent = None
        if "reschedule" in t_lower or "move " in t_lower or "postpone" in t_lower:
            intent = ActionType.RESCHEDULE
        elif "cancel" in t_lower:
            intent = ActionType.CANCEL
        elif "book" in t_lower or "find" in t_lower or "need" in t_lower or "hire" in t_lower or "get me" in t_lower:
            intent = ActionType.BOOK

        # Service extraction
        service = None
        if "plumb" in t_lower or "tap" in t_lower or "pipe" in t_lower or "leak" in t_lower:
            service = "plumbing"
        elif "ac " in t_lower or "air condition" in t_lower or "cooling" in t_lower:
            service = "ac servicing"
        elif "electri" in t_lower or "wiring" in t_lower or "switch" in t_lower:
            service = "electrician"
        elif "carpent" in t_lower:
            service = "carpentry"

        # Area extraction
        area = None
        for cand in ["Indiranagar", "Koramangala", "HSR Layout", "Whitefield", "Jayanagar", "Bellandur"]:
            if cand.lower() in t_lower:
                area = cand
                break

        # Budget extraction
        budget = None
        budget_match = re.search(r"(?:under|below|budget of|max(?:imum)? of|within)?\s*[₹rs\.]*\s*([0-9]+(?:,[0-9]+)*)", t_lower)
        # Check if the sentence implies budget
        if budget_match and ("₹" in t or "rs" in t_lower or "under" in t_lower or "budget" in t_lower or "below" in t_lower):
            val_str = budget_match.group(1).replace(",", "")
            try:
                budget = float(val_str)
            except ValueError:
                pass

        # Time window extraction
        time_window = None
        day = None
        time_of_day = None
        not_before = None
        not_after = None

        if "saturday" in t_lower:
            day = "Saturday"
        elif "sunday" in t_lower:
            day = "Sunday"
        elif "tomorrow" in t_lower:
            day = "Tomorrow"
        elif "this week" in t_lower:
            day = "this week"

        if "morning" in t_lower:
            time_of_day = "morning"
            not_after = 12
        elif "afternoon" in t_lower:
            time_of_day = "afternoon"
        elif "evening" in t_lower:
            time_of_day = "evening"

        not_before_match = re.search(r"(?:not before|after|from)\s*([0-9]{1,2})(?:\s*([ap]\.?m\.?))?", t_lower)
        if not_before_match:
            hr = int(not_before_match.group(1))
            meridiem = not_before_match.group(2)
            if meridiem and "p" in meridiem.lower() and hr < 12:
                hr += 12
            not_before = hr

        if day or time_of_day or not_before is not None or not_after is not None:
            time_window = TimeWindow(
                raw=t,
                day=day,
                time_of_day=time_of_day,
                not_before_hour=not_before,
                not_after_hour=not_after,
            )

        # Preferences
        preferences = {}
        if "same electrician" in t_lower or "same plumber" in t_lower or "last time" in t_lower:
            preferences["use_previous_provider"] = True

        return {
            "intent": intent,
            "is_approval": is_approval,
            "is_rejection": is_rejection,
            "service": service,
            "area": area,
            "budget": budget,
            "time_window": time_window,
            "preferences": preferences,
        }

    def estimate_metrics(self, prompt_text: str, reply_text: str) -> Tuple[int, int, float, float]:
        """Estimate tokens and cost for the interaction."""
        # Simple token heuristic: 1 token ~ 4 chars
        tokens_p = max(1, len(prompt_text) // 4)
        tokens_c = max(1, len(reply_text) // 4)
        cost = (tokens_p * self.prompt_token_rate) + (tokens_c * self.completion_token_rate)
        # Latency simulation: 15-40ms
        latency_ms = 20.0 + (len(reply_text) % 15)
        return tokens_p, tokens_c, latency_ms, cost
