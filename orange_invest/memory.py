"""Memory system for Orange Invest agent.

Persists preferences across sessions (area, earliest time, preferred technician).
Enforces:
1. Every fact is marked as stated by user or inferred by agent.
2. Inferred facts are never used without user confirmation.
3. Users can view, correct, and forget facts.
4. Forgotten facts are tombstoned and never reused or resurrected.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional
from orange_invest.models import FactSource, FactStatus, MemoryFact


class MemoryStore:
    """Persistent preference store with strict provenance and tombstoning."""

    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = storage_path
        # user_id -> key -> MemoryFact
        self._store: Dict[str, Dict[str, MemoryFact]] = {}
        if self.storage_path and os.path.exists(self.storage_path):
            self.load()

    def set_stated(self, user_id: str, key: str, value: Any) -> MemoryFact:
        """Record or update a fact explicitly stated by the user."""
        user_facts = self._store.setdefault(user_id, {})
        fact = MemoryFact(
            key=key,
            value=value,
            source=FactSource.STATED,
            status=FactStatus.ACTIVE,
            confidence=1.0,
            created_at=time.time(),
            updated_at=time.time(),
            tombstoned_at=None,
        )
        user_facts[key] = fact
        self._persist()
        return fact

    def set_inferred(self, user_id: str, key: str, value: Any, confidence: float = 0.8) -> Optional[MemoryFact]:
        """
        Record a fact inferred by the agent.
        Rules:
        - If the fact was forgotten (tombstoned), NEVER reuse or re-infer it.
        - If a stated fact exists, do not overwrite with an inferred one.
        """
        user_facts = self._store.setdefault(user_id, {})
        existing = user_facts.get(key)
        if existing:
            if existing.status == FactStatus.FORGOTTEN:
                # Do not resurrect forgotten facts
                return None
            if existing.source == FactSource.STATED and existing.status == FactStatus.ACTIVE:
                # User's explicit statement takes precedence
                return existing

        fact = MemoryFact(
            key=key,
            value=value,
            source=FactSource.INFERRED,
            status=FactStatus.ACTIVE,
            confidence=confidence,
            created_at=time.time(),
            updated_at=time.time(),
            tombstoned_at=None,
        )
        user_facts[key] = fact
        self._persist()
        return fact

    def get_fact(self, user_id: str, key: str) -> Optional[MemoryFact]:
        """Retrieve a fact if it exists and is active."""
        user_facts = self._store.get(user_id, {})
        fact = user_facts.get(key)
        if fact and fact.status == FactStatus.ACTIVE:
            return fact
        return None

    def list_active_facts(self, user_id: str) -> List[MemoryFact]:
        """List all active facts for the user, showing stated vs inferred status."""
        user_facts = self._store.get(user_id, {})
        return [f for f in user_facts.values() if f.status == FactStatus.ACTIVE]

    def correct_fact(self, user_id: str, key: str, new_value: Any) -> MemoryFact:
        """User corrects a fact: value is updated and marked as explicitly stated."""
        return self.set_stated(user_id, key, new_value)

    def forget_fact(self, user_id: str, key: str) -> bool:
        """
        Forget a fact. It is tombstoned so it is never reused or inferred again.
        Returns True if a fact was found and tombstoned.
        """
        user_facts = self._store.setdefault(user_id, {})
        fact = user_facts.get(key)
        if fact:
            fact.status = FactStatus.FORGOTTEN
            fact.tombstoned_at = time.time()
            fact.updated_at = time.time()
            self._persist()
            return True
        else:
            # Create a tombstone even if it wasn't recorded before
            user_facts[key] = MemoryFact(
                key=key,
                value=None,
                source=FactSource.STATED,
                status=FactStatus.FORGOTTEN,
                confidence=1.0,
                created_at=time.time(),
                updated_at=time.time(),
                tombstoned_at=time.time(),
            )
            self._persist()
            return True

    def is_fact_usable_automatically(self, user_id: str, key: str) -> bool:
        """
        Check if a fact can be used automatically without asking the user.
        Invariant: ONLY active facts STATED by the user may be used without confirmation.
        Inferred facts MUST be confirmed by the user.
        Forgotten facts can never be used.
        """
        fact = self.get_fact(user_id, key)
        if not fact:
            return False
        return fact.status == FactStatus.ACTIVE and fact.source == FactSource.STATED

    def requires_confirmation(self, user_id: str, key: str) -> bool:
        """Returns True if the fact is inferred and needs user confirmation."""
        fact = self.get_fact(user_id, key)
        if not fact:
            return False
        return fact.status == FactStatus.ACTIVE and fact.source == FactSource.INFERRED

    def _persist(self):
        if not self.storage_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.storage_path)), exist_ok=True)
        data = {}
        for uid, facts in self._store.items():
            data[uid] = {
                k: {
                    "key": f.key,
                    "value": f.value,
                    "source": f.source.value,
                    "status": f.status.value,
                    "confidence": f.confidence,
                    "created_at": f.created_at,
                    "updated_at": f.updated_at,
                    "tombstoned_at": f.tombstoned_at,
                }
                for k, f in facts.items()
            }
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load(self):
        if not self.storage_path or not os.path.exists(self.storage_path):
            return
        with open(self.storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for uid, facts in data.items():
            self._store[uid] = {}
            for k, d in facts.items():
                self._store[uid][k] = MemoryFact(
                    key=d["key"],
                    value=d["value"],
                    source=FactSource(d["source"]),
                    status=FactStatus(d["status"]),
                    confidence=d["confidence"],
                    created_at=d["created_at"],
                    updated_at=d["updated_at"],
                    tombstoned_at=d.get("tombstoned_at"),
                )

