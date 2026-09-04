"""
test_retry_engine.py
---------------------
Isolated, sharp assertions on RetryEngine's non-negotiable safety rules:
customer consent (opt-out), the hard retry cap, and the expired-card veto.

Each test constructs the minimum classification dict needed and calls
RetryEngine.decide() directly -- no dataset, no StoppingRules, no Flask.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from core.retry_engine import RetryEngine, VALID_ACTIONS


@pytest.fixture
def engine():
    return RetryEngine()


def _classification(reason: str, confidence: float = 0.9) -> dict:
    return {"classified_reason": reason, "confidence_score": confidence}


@pytest.mark.parametrize(
    "classified_reason,prior_attempts",
    [
        ("infrastructure_timeout", 0),
        ("insufficient_funds", 1),
        ("chronic_payment_risk", 2),
    ],
)
def test_opt_out_always_results_in_do_not_retry(engine, classified_reason, prior_attempts):
    """opt_out_flag=True must ALWAYS win, regardless of how favorable the
    underlying classification or retry timing would otherwise be."""
    decision = engine.decide(
        classification=_classification(classified_reason),
        payment_history="good",
        prior_attempts=prior_attempts,
        opt_out_flag=True,
    )
    assert decision["action"] == "do_not_retry"


@pytest.mark.parametrize(
    "classified_reason",
    ["insufficient_funds", "issuer_side_issue", "chronic_payment_risk", "temporary_anomaly"],
)
def test_hard_cap_blocks_retries_regardless_of_reason(engine, classified_reason):
    """prior_attempts >= 3 must ALWAYS block further retries, independent
    of what the classifier decided about the underlying cause."""
    decision = engine.decide(
        classification=_classification(classified_reason),
        payment_history="bad",
        prior_attempts=3,
        opt_out_flag=False,
    )
    assert decision["action"] == "do_not_retry"


def test_card_expired_always_blocks_retry(engine):
    """A factually expired card can never be usefully retried, so this
    classification must deterministically stop the automated loop."""
    decision = engine.decide(
        classification=_classification("card_expired"),
        payment_history="good",
        prior_attempts=0,
        opt_out_flag=False,
    )
    assert decision["action"] == "do_not_retry"


def test_infrastructure_timeout_proposes_retry_now_on_first_touch(engine):
    """Sanity check: the ONLY path that should propose 'retry_now' is a
    transient infra failure with no prior attempts and no opt-out/cap hit."""
    decision = engine.decide(
        classification=_classification("infrastructure_timeout"),
        payment_history="good",
        prior_attempts=0,
        opt_out_flag=False,
    )
    assert decision["action"] == "retry_now"


@pytest.mark.parametrize(
    "classified_reason,prior_attempts,opt_out",
    [
        ("insufficient_funds", 0, False),
        ("chronic_payment_risk", 2, False),
        ("unclassified", 1, False),
        ("temporary_anomaly", 3, True),
    ],
)
def test_decision_action_is_always_from_valid_action_set(engine, classified_reason, prior_attempts, opt_out):
    """Every decision RetryEngine produces, under any combination of
    inputs, must be one of the four contractually allowed actions."""
    decision = engine.decide(
        classification=_classification(classified_reason),
        payment_history="patchy",
        prior_attempts=prior_attempts,
        opt_out_flag=opt_out,
    )
    assert decision["action"] in VALID_ACTIONS