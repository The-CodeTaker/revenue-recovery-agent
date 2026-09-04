"""
test_stopping_rules.py
------------------------
Isolated, sharp assertions on StoppingRules' hard veto layer: active
disputes, the overnight blackout window, and the minimum cooldown that
guarantees retry_now can only ever fire on a payment's first touch.

current_time is always passed explicitly so these tests are fully
deterministic and never depend on the real wall clock.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime

import pytest
from core.stopping_rules import StoppingRules

DAYTIME = datetime(2026, 6, 15, 14, 0, 0)   # 2:00 PM -- outside blackout
NIGHTTIME = datetime(2026, 6, 15, 23, 0, 0)  # 11:00 PM -- inside blackout


@pytest.fixture
def stopping_rules():
    return StoppingRules()


def test_active_dispute_always_blocks_retry(stopping_rules):
    """dispute_flag=True must ALWAYS result in is_allowed=False, even for
    an otherwise perfectly-timed, first-attempt retry_now."""
    record = {"dispute_flag": True, "prior_attempts": 0, "timestamp_of_failure": None}
    decision = {"action": "retry_now"}
    verdict = stopping_rules.evaluate(record, decision, current_time=DAYTIME)
    assert verdict["is_allowed"] is False


def test_blackout_window_blocks_any_retry(stopping_rules):
    """Any retry proposed inside the 10 PM - 8 AM window must be blocked,
    regardless of dispute status or prior attempts."""
    record = {"dispute_flag": False, "prior_attempts": 0, "timestamp_of_failure": None}
    decision = {"action": "retry_in_24h"}
    verdict = stopping_rules.evaluate(record, decision, current_time=NIGHTTIME)
    assert verdict["is_allowed"] is False
    assert "blackout" in verdict["block_reason"].lower()


def test_daytime_first_attempt_retry_now_is_allowed(stopping_rules):
    """Counter-case: a clean, daytime, first-touch retry_now with no
    dispute must pass through with no block."""
    record = {"dispute_flag": False, "prior_attempts": 0, "timestamp_of_failure": None}
    decision = {"action": "retry_now"}
    verdict = stopping_rules.evaluate(record, decision, current_time=DAYTIME)
    assert verdict["is_allowed"] is True
    assert verdict["block_reason"] == ""


def test_minimum_cooldown_blocks_retry_now_after_prior_attempt(stopping_rules):
    """Any retry_now proposed with prior_attempts > 0 is blocked by the
    minimum cooldown rule, even during a safe daytime window."""
    record = {"dispute_flag": False, "prior_attempts": 1, "timestamp_of_failure": None}
    decision = {"action": "retry_now"}
    verdict = stopping_rules.evaluate(record, decision, current_time=DAYTIME)
    assert verdict["is_allowed"] is False
    assert "cooldown" in verdict["block_reason"].lower()


def test_retry_now_only_works_when_prior_attempts_is_zero(stopping_rules):
    """Directly proves the idempotency contract: retry_now is valid ONLY
    on a payment's first touch (prior_attempts == 0)."""
    record_first_touch = {"dispute_flag": False, "prior_attempts": 0, "timestamp_of_failure": None}
    record_repeat = {"dispute_flag": False, "prior_attempts": 1, "timestamp_of_failure": None}
    decision = {"action": "retry_now"}

    verdict_first = stopping_rules.evaluate(record_first_touch, decision, current_time=DAYTIME)
    verdict_repeat = stopping_rules.evaluate(record_repeat, decision, current_time=DAYTIME)

    assert verdict_first["is_allowed"] is True
    assert verdict_repeat["is_allowed"] is False


def test_do_not_retry_short_circuits_before_any_other_rule(stopping_rules):
    """Rule 0: if RetryEngine already decided 'do_not_retry', StoppingRules
    has nothing to block -- this holds even if dispute_flag is True,
    proving the short-circuit runs before the dispute check."""
    record = {"dispute_flag": True, "prior_attempts": 5, "timestamp_of_failure": None}
    decision = {"action": "do_not_retry"}
    verdict = stopping_rules.evaluate(record, decision, current_time=NIGHTTIME)
    assert verdict["is_allowed"] is True
    assert verdict["block_reason"] == ""