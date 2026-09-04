"""
generate_dataset.py
--------------------
Generates a realistic SYNTHETIC dataset of failed subscription payment
records for the Revenue Recovery Agent's decision engine.

Why "realistic" matters here:
    A pure `random.choice()` over every column produces IID noise that no
    classifier (rule-based or ML) can meaningfully learn from. Real payment
    failure data has structure: certain failure reasons cluster around
    certain customer behaviors, certain days of the month, and certain
    retry histories. This script deliberately injects those correlations
    so that downstream components (FailureClassifier, RetryEngine, and any
    future ML model) are trained/tested against believable dynamics.

Embedded patterns (by design):
    1. "insufficient funds" is the single most common failure reason, and
       is biased toward customers with "patchy" or "bad" payment history.
    2. "expired card" failures cluster around the 1st-5th of the month
       (when many recurring billing cycles fire and cards issued a month
       prior may have just rolled over/expired).
    3. "network timeout" is a transient, infrastructure-side failure and
       therefore ALWAYS has `prior_attempts == 0` -- it's not something
       that compounds with retries, it just happens.
    4. "bank decline" and "issuer unavailable" are seeded with more
       uniform, less-correlated behavior to simulate genuine ambiguity
       that a classifier has to reason carefully about.
    5. `opt_out_flag` is rare overall, but more likely for customers with
       "bad" payment history and 3 prior attempts (i.e. customers who are
       fatigued by repeated failed charges).
    6. `prior_attempts` generally trends upward with worse payment history,
       except where overridden by reason-specific logic (e.g. network
       timeout, expired card).

Output:
    data/failed_payments.csv  (created relative to this script's location)

Usage:
    python data/generate_dataset.py
    python data/generate_dataset.py --rows 250 --seed 7
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Configuration constants
# --------------------------------------------------------------------------

SUBSCRIPTION_PLANS = ["Basic Monthly", "Pro Monthly", "Pro Annual", "Team Plan", "Enterprise Lite"]

PLAN_PRICE_RANGES = {
    "Basic Monthly": (199, 499),
    "Pro Monthly": (599, 1499),
    "Pro Annual": (4999, 14999),
    "Team Plan": (2999, 7999),
    "Enterprise Lite": (9999, 24999),
}

FAILURE_REASONS = [
    "insufficient funds",
    "expired card",
    "bank decline",
    "network timeout",
    "issuer unavailable",
]

# Relative likelihood of each failure reason occurring (sums don't need to
# be 100, random.choices() normalizes weights internally).
FAILURE_REASON_WEIGHTS = {
    "insufficient funds": 38,
    "bank decline": 22,
    "expired card": 18,
    "issuer unavailable": 12,
    "network timeout": 10,
}

PAYMENT_HISTORIES = ["good", "patchy", "bad"]

# Base likelihood of each history bucket in the overall customer population.
PAYMENT_HISTORY_WEIGHTS = {"good": 45, "patchy": 35, "bad": 20}

# A 2024-2026 window gives us enough month-boundary variety to make the
# "expired card clusters near month start" pattern visible and testable.
DATE_RANGE_START = datetime(2025, 1, 1)
DATE_RANGE_END = datetime(2026, 8, 1)


@dataclass
class GenerationConfig:
    """Simple container for run parameters, keeps generate_row() pure-ish."""
    rows: int
    seed: int | None


def _weighted_choice(weights: dict) -> str:
    """Pick a single key from a {label: weight} dict using its weights."""
    labels = list(weights.keys())
    values = list(weights.values())
    return random.choices(labels, weights=values, k=1)[0]


def _pick_payment_history() -> str:
    """
    Draws a customer's payment history bucket.

    This is drawn independently, then used downstream to *bias* (not
    dictate) the failure reason and prior_attempts -- mirroring how real
    customer risk profiles influence, but don't fully determine, any
    single payment event.
    """
    return _weighted_choice(PAYMENT_HISTORY_WEIGHTS)


def _pick_failure_reason(payment_history: str) -> str:
    """
    Draws a failure reason, nudged by the customer's payment history.

    Pattern embedded: customers with "patchy" or "bad" history are
    meaningfully more likely to fail on "insufficient funds" than
    customers with "good" history, who more often see infrastructure or
    issuer-side reasons (bank decline / network timeout / issuer
    unavailable) since their own funds situation is typically stable.
    """
    weights = dict(FAILURE_REASON_WEIGHTS)  # copy so we don't mutate global

    if payment_history == "bad":
        weights["insufficient funds"] *= 2.2
        weights["expired card"] *= 0.7
    elif payment_history == "patchy":
        weights["insufficient funds"] *= 1.5
    else:  # "good"
        weights["insufficient funds"] *= 0.4
        weights["network timeout"] *= 1.3
        weights["issuer unavailable"] *= 1.2

    return _weighted_choice(weights)


def _pick_timestamp(failure_reason: str) -> datetime:
    """
    Draws a failure timestamp, clustering "expired card" failures near the
    start of the month (days 1-5), since that's when most recurring
    billing cycles fire and a card that quietly expired the prior month
    first gets caught.

    All other reasons are spread uniformly across the full date range,
    with a uniformly random time-of-day component for every reason.
    """
    total_days = (DATE_RANGE_END - DATE_RANGE_START).days

    if failure_reason == "expired card":
        # Pick a random month within range, then bias the day to 1-5.
        random_offset_days = random.randint(0, total_days)
        anchor = DATE_RANGE_START + timedelta(days=random_offset_days)
        month_start = anchor.replace(day=1)
        day_of_month = random.randint(1, 5)
        # Guard against months where day 5 would still be valid (always is).
        base_date = month_start.replace(day=day_of_month)
    else:
        random_offset_days = random.randint(0, total_days)
        base_date = DATE_RANGE_START + timedelta(days=random_offset_days)

    random_seconds = random.randint(0, 86_399)
    return base_date + timedelta(seconds=random_seconds)


def _pick_prior_attempts(failure_reason: str, payment_history: str) -> int:
    """
    Draws prior_attempts (0-3), with two hard rules layered on top of a
    history-driven baseline distribution:

      * "network timeout" is always 0 -- it's a one-off transient glitch,
        not a symptom of a repeatedly-failing payment method, so it never
        appears as a retried payment in this synthetic world.
      * Worse payment history trends toward higher prior_attempts, since
        those customers are more likely to already be mid-way through a
        dunning/retry cycle.
    """
    if failure_reason == "network timeout":
        return 0

    if payment_history == "good":
        weights = {0: 55, 1: 30, 2: 10, 3: 5}
    elif payment_history == "patchy":
        weights = {0: 30, 1: 35, 2: 25, 3: 10}
    else:  # "bad"
        weights = {0: 15, 1: 25, 2: 30, 3: 30}

    return int(_weighted_choice({str(k): v for k, v in weights.items()}))


def _pick_opt_out_flag(payment_history: str, prior_attempts: int) -> bool:
    """
    Draws opt_out_flag, biased upward for customers with "bad" history AND
    who are already at the maximum of 3 prior attempts -- these are
    customers who have been charged (and failed) repeatedly and are
    realistically more likely to have opted out of further retry attempts.
    """
    base_probability = 0.04  # ambient opt-out rate across the population

    if payment_history == "bad" and prior_attempts >= 3:
        base_probability = 0.35
    elif payment_history == "bad" and prior_attempts == 2:
        base_probability = 0.18
    elif payment_history == "patchy" and prior_attempts >= 3:
        base_probability = 0.15

    return random.random() < base_probability


def _pick_amount(plan: str) -> float:
    """Draws a plausible charge amount (INR) for the given plan tier."""
    low, high = PLAN_PRICE_RANGES[plan]
    # round to 2 decimals like a real currency amount
    return round(random.uniform(low, high), 2)


def generate_row(customer_id: str) -> dict:
    """
    Builds a single, internally-consistent failed payment record by
    drawing correlated fields in dependency order:

        payment_history -> failure_reason -> timestamp -> prior_attempts
                                                         -> opt_out_flag
        plan (independent) -> amount (depends on plan)
    """
    payment_history = _pick_payment_history()
    failure_reason = _pick_failure_reason(payment_history)
    timestamp = _pick_timestamp(failure_reason)
    prior_attempts = _pick_prior_attempts(failure_reason, payment_history)
    opt_out_flag = _pick_opt_out_flag(payment_history, prior_attempts)
    plan = random.choice(SUBSCRIPTION_PLANS)
    amount = _pick_amount(plan)

    return {
        "customer_id": customer_id,
        "amount": amount,
        "subscription_plan": plan,
        "failure_reason": failure_reason,
        "timestamp_of_failure": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "prior_attempts": prior_attempts,
        "customer_payment_history": payment_history,
        "opt_out_flag": opt_out_flag,
    }


def generate_dataset(config: GenerationConfig) -> pd.DataFrame:
    """Generates `config.rows` records and returns them as a DataFrame."""
    if config.seed is not None:
        random.seed(config.seed)

    records = []
    for i in range(config.rows):
        # Customer IDs are zero-padded and prefixed to look like real
        # internal identifiers (e.g. CUST-000042).
        customer_id = f"CUST-{i + 1:06d}"
        records.append(generate_row(customer_id))

    return pd.DataFrame.from_records(records)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a synthetic failed-payments dataset for the Revenue Recovery Agent."
    )
    parser.add_argument(
        "--rows", type=int, default=120,
        help="Number of failed payment records to generate (default: 120, satisfies the 100+ requirement).",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducibility (default: 42). Pass a different value for a fresh sample.",
    )
    args = parser.parse_args()

    config = GenerationConfig(rows=args.rows, seed=args.seed)
    df = generate_dataset(config)

    output_path = Path(__file__).resolve().parent / "failed_payments.csv"
    df.to_csv(output_path, index=False)

    print(f"Generated {len(df)} synthetic failed payment records.")
    print(f"Saved to: {output_path}")
    print("\nFailure reason distribution:")
    print(df["failure_reason"].value_counts())
    print("\nPayment history distribution:")
    print(df["customer_payment_history"].value_counts())


if __name__ == "__main__":
    main()
