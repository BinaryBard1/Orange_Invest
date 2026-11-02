# Engineering Postmortem: Three Real Bugs Hit While Building

**Author:** Aniruddha Shrikhande  
**Context:** Orange Invest Founding AI Engineer Assignment  

During development, unit testing, and eval scenario creation, I hit three genuine architectural and edge-case bugs. None of these were typos; they were edge cases arising from the intersection of state persistence, idempotency semantics, and NLP date parsing.

Here is what broke, why it happened, how I fixed it, and the regression tests guarding them.

---

## Bug 1: Premature Hold Eviction Failed Idempotent Retry of `book()`

### 1. What Broke
When running my first idempotency test where `mock_api.book()` was called twice with the same idempotency key (simulating a network retry after a successful booking), the second call crashed with:
```
HoldExpiredError: Hold 'hold_12f0bcd3' not found or already released.
```
Instead of returning the previously committed `booking_id`, the API threw an error, breaking the contract of idempotent operations.

### 2. Root Cause Analysis
I had structured the `book()` implementation like this:
```python
# Initial buggy sequence in mock_api.py:
def book(self, hold_id, idempotency_key):
    hold = self.holds.get(hold_id)
    if not hold:
        raise HoldExpiredError(...)

    if time.time() > hold["expires_at"]:
        raise HoldExpiredError(...)

    # Idempotency check placed AFTER hold check!
    if idempotency_key in self.idempotency_records:
        return self.idempotency_records[idempotency_key]["booking_id"]
    ...
    # Upon success:
    self.holds.pop(hold_id, None)  # <-- Consumed!
```

Because the first call succeeded and cleaned up `self.holds.pop(hold_id, None)`, any retry arrived when the hold was already gone. By evaluating `self.holds.get(hold_id)` *before* checking `idempotency_key`, the method failed before it ever looked at the idempotency ledger!

### 3. The Fix
In distributed systems, idempotency keys take precedence over transient session objects. I moved the idempotency record lookup to line 1 of `book()`:
```python
def book(self, hold_id, idempotency_key):
    # Rule 1: Always check idempotency record first
    if idempotency_key in self.idempotency_records:
        rec = self.idempotency_records[idempotency_key]
        if rec["status"] == "booked":
            return rec["booking_id"]
        elif rec["status"] == "failed":
            raise SlotTakenError("Previous attempt failed: slot was taken.")

    # Rule 2: Only check transient hold if this is a first-time attempt
    hold = self.holds.get(hold_id)
    if not hold:
        raise HoldExpiredError(f"Hold '{hold_id}' not found or already released.")
```

### 4. Regression Test Guard
- **File:** [`tests/test_bugs_regression.py`](tests/test_bugs_regression.py)
- **Test:** `test_bug1_idempotent_book_retry_after_hold_consumed()`
- **Assertion:** Asserts that calling `book()` with the same idempotency key after the hold has been popped from memory still succeeds and returns the exact same `booking_id`.

---

## Bug 2: Relative Date Qualifier ("this week") Caused False Slot Rejections in `TimeWindow`

### 1. What Broke
When running one of the assignment's exact example prompts:
> *"Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."*

The agent returned:
```
FAILED: I searched for plumbing in Indiranagar matching your constraints, but found no available slots within your budget.
```
This was wrong because `p_plumb_101` had multiple morning slots available in Indiranagar (`2026-11-05 09:00 AM`, `2026-11-05 11:00 AM`) at ₹1,200 (well within the ₹1,500 budget).

### 2. Root Cause Analysis
In `llm_driver.py`, the intent parser extracted the phrase `"this week"` and stored it in `TimeWindow(day="this week", time_of_day="morning")`.

Then in `TimeWindow.matches_slot()`:
```python
# Initial buggy check in models.py:
if self.day:
    day_lower = self.day.lower()
    if day_lower not in slot_str.lower():
        return False
```

The method checked if `"this week"` was a substring of the slot string `"2026-11-05 09:00 AM"`. Since the slot string obviously doesn't contain the literal English characters `"this week"`, every single provider slot was rejected!

### 3. The Fix
`"this week"`, `"any day"`, and `"upcoming"` are relative date range qualifiers, not literal day names like `"Saturday"` or `"Sunday"`. I updated `matches_slot()` to only perform literal substring checks when a specific calendar day name is present:
```python
# Fixed check in models.py:
if self.day and self.day.lower() not in ("this week", "any day", "upcoming"):
    day_lower = self.day.lower()
    if day_lower not in slot_str.lower():
        return False
```

### 4. Regression Test Guard
- **File:** [`tests/test_bugs_regression.py`](tests/test_bugs_regression.py)
- **Test:** `test_bug2_relative_date_window_matching()`
- **Assertion:** Asserts that slots formatted as `"2026-11-05 09:00 AM"` correctly match a `TimeWindow` with `day="this week"` and `time_of_day="morning"`, while correctly rejecting an afternoon slot (`"03:00 PM"`).

---

## Bug 3: Stale Approval Record Allowed Inadvertent Execution on Parameter Conflict

### 1. What Broke
During a reschedule flow, a user approved moving an appointment to a slot that encountered a `SlotConflictError`. The user then suggested an alternative slot. Under certain sequences, the approval check could evaluate the previously approved record if it was not explicitly marked void upon conflict.

### 2. Root Cause Analysis
In my first iteration of `ApprovalGuard`, an approval record was only marked `CONSUMED` upon successful tool completion. If the tool threw an upstream error (`SlotConflictError` or `SlotUnavailableError`), the in-memory active approval record remained in status `APPROVED`.

If the agent then proceeded to process a new slot without creating a new staged record, the old approval record could accidentally satisfy the check if provider and action matched, creating a security gap.

### 3. The Fix
I enforced two code-level invariants:
1. In `ApprovalGuard.stage_approval()`, any parameter difference (action, provider, slot, or price) between the new request and an active approved record immediately marks the active record `VOIDED`:
   ```python
   if self.active_record and self.active_record.status == ApprovalStatus.APPROVED:
       if not self.active_record.matches(action, provider_id, slot, price):
           self.void_approval(
               f"Parameters changed from ({self.active_record.action}, {self.active_record.provider_id}, "
               f"{self.active_record.slot}, ₹{self.active_record.price}) to ({action}, {provider_id}, {slot}, ₹{price})"
           )
   ```
2. Any upstream conflict or user correction immediately sets `status = VOIDED`, ensuring that every slot shift demands fresh, explicit user confirmation.

### 4. Regression Test Guard
- **File:** [`tests/test_bugs_regression.py`](tests/test_bugs_regression.py)
- **Test:** `test_bug3_stale_approval_voided_on_conflict_and_parameter_change()`
- **Assertion:** Asserts that changing slot parameters after an approval was granted immediately resets the active status to `PENDING`, and attempting execution without new user approval raises `ApprovalNotGrantedError`.
