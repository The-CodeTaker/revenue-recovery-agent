"""
stopping_rules.py
------------------
Defines `StoppingRules`: the final global safety layer that runs *after*
`RetryEngine` has already produced a proposed action. Where `RetryEngine`
reasons about "what's the smart business decision for this specific
customer", `StoppingRules` reasons about "are there any hard, non-negotiable
constraints -- legal, operational, or anti-abuse -- that must override that
decision regardless of how smart it is?"

Design philosophy:
    This is intentionally the LAST checkpoint in the pipeline
    (Classifier -> RetryEngine -> StoppingRules) and it is intentionally
    dumb and rigid compared to the other two. Its job is not nuance, it's
    to be a hard veto layer. Every rule here is a global override that
    applies uniformly, regardless of classified_reason or confidence
    score, because these are constraints that exist independent of *why*
    the payment failed:
        - You never touch a payment under active dispute, no matter how
          "recoverable" it looks.
        - You never fire card charges at 3 AM, no matter how urgent the
          revenue recovery is.
        - You never let a bug or a race condition cause back-to-back
          charge attempts against the same payment method within minutes
          of each other.

    Keeping this logic in its own class (rather than folding it into
    RetryEngine) means these organization-wide safety constraints live in
    exactly one place and can be audited, unit-tested, and modified by a
    compliance/risk stakeholder without anyone needing to touch or
    understand the business heuristics inside RetryEngine.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Mapping, Optional, Union

Record = Union[Mapping, "pd.Series"]  # noqa: F821  (pandas is an optional runtime dep, not imported here)

# Failure record timestamps are stored as "YYYY-MM-DD HH:MM:SS" strings
# (see data/generate_dataset.py), so we parse against that exact format.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Blackout window: no retries fire between 10:00 PM and 8:00 AM (inclusive
# of 22:00:00, exclusive of 08:00:00). This protects customers from being
# charged / notified in the middle of the night, and reduces the chance of
# a declined charge triggering a bank's fraud system during low-traffic
# overnight hours.
BLACKOUT_START = time(22, 0, 0)  # 10:00 PM
BLACKOUT_END = time(8, 0, 0)     # 8:00 AM

# Minimum time that must elapse between two consecutive charge attempts
# against the same payment method, expressed in hours. This exists purely
# to prevent runaway/duplicate "retry_now" calls from hammering the
# payment gateway (e.g. due to a retry-loop bug or a duplicate webhook).
MINIMUM_COOLDOWN_HOURS = 2


class StoppingRules:
    """
    Final safeguard layer applied after RetryEngine has proposed a decision.

    Usage:
        stopping_rules = StoppingRules()
        verdict = stopping_rules.evaluate(record, proposed_decision)
        # verdict == {"is_allowed": True, "block_reason": ""}
        # or
        # verdict == {"is_allowed": False, "block_reason": "Active dispute on this payment."}

    Rules (evaluated in priority order, first violation blocks immediately):

        0. No-op short circuit:
           If the proposed action is already "do_not_retry", there is
           nothing to block -- RetryEngine has already decided not to
           touch this payment, so the record trivially passes through as
           allowed (is_allowed=True, block_reason="").

        1. Active Dispute:
           If `record["dispute_flag"]` is True, ANY retry action is
           blocked immediately, regardless of how favorable the
           classification or timing is. A payment under active dispute is
           a legal/compliance matter, not a revenue-recovery decision --
           charging it again could constitute processing an already
           contested transaction, which is never appropriate.

        2. Blackout Window (10:00 PM - 8:00 AM):
           Retries are never allowed to fire during this overnight window.
           Because RetryEngine's "retry_now" means "fire immediately", we
           evaluate the blackout window against the current evaluation
           time. For "retry_in_24h" / "retry_in_3_days" we still evaluate
           against the current time as a demonstrative proxy for "is the
           system currently inside a blackout window" -- in a live system,
           this same check would be re-run again at actual execution time
           right before the scheduled retry fires, so a delayed retry
           that happens to land in the daytime would pass even if it was
           *decided* during a nighttime blackout window.

        3. Minimum Cooldown (2 hours):
           If RetryEngine proposes "retry_now" but `prior_attempts > 0`,
           we block it. "retry_now" should only ever apply to a payment's
           FIRST attempt (prior_attempts == 0, e.g. a fresh infrastructure
           timeout). If a payment already has prior attempts and something
           upstream still proposes an immediate retry, that's a signal of
           a possible bug or race condition, and we defensively enforce a
           minimum cooldown rather than risk spamming the gateway with
           back-to-back charge attempts.

    Idempotency Guarantee & financial safety (rules 3, and the cap that
    rule 3 sits on top of):
        Rule 3 is not just a UX nicety -- it is this system's primary
        DEFENSE-IN-DEPTH mechanism against duplicate side effects. In any
        distributed system that talks to a payment gateway over a network,
        upstream failures are expected and routine: a webhook can fire
        twice for the same event (most providers document at-least-once
        delivery), a queue consumer can crash and redeliver a message
        after partial processing, a retried HTTP request can race against
        the original one that actually succeeded, or a scheduler can
        double-trigger a batch job. None of these are exotic edge cases --
        they are the normal operating conditions of any webhook- or
        queue-driven pipeline, and this system has no control over the
        upstream systems that can trigger them.

        Two properties work together here to make sure that class of bug
        can NEVER translate into a duplicate charge or a duplicate
        customer message, even if `RetryEngine` (or something further
        upstream) is fooled into proposing "retry_now" a second time for
        a payment that has already been attempted:

          - `MINIMUM_COOLDOWN_HOURS` establishes a hard floor on how soon
            two consecutive attempts against the SAME payment method are
            allowed to fire. Rule 3 checks `prior_attempts > 0` specifically
            because "retry_now" is only ever a legitimate decision on a
            payment's very first touch -- once `prior_attempts >= 1`, any
            request to fire immediately again is, by definition, arriving
            sooner than the cooldown permits, and is blocked.
          - The upstream `prior_attempts >= 3` hard cap (see Rule 1 above)
            provides the outer bound: even in a worst-case scenario where
            a bug causes repeated duplicate decisions to be proposed over
            time, the total number of gateway charge attempts for a single
            payment is strictly capped, and every attempt after that cap
            is blocked outright regardless of timing.

        Together, this means `StoppingRules` acts as an idempotent
        checkpoint: no matter how many times upstream logic is
        (correctly or incorrectly) invoked for the same failed payment
        record, the number of actual gateway charge attempts -- and
        therefore the number of customer-facing "your payment was
        retried" messages -- is bounded and rate-limited by this layer
        alone, independent of whether the callers above it are behaving
        correctly. This is the layer a compliance/risk reviewer should
        point to when asked "what stops us from double-charging a
        customer if a webhook fires twice?"
    """

    def evaluate(
        self,
        record: Record,
        proposed_decision: dict,
        current_time: Optional[datetime] = None,
    ) -> dict:
        """
        Evaluates whether the proposed RetryEngine decision is allowed to
        proceed, or must be blocked by a global safeguard.

        Args:
            record: The raw failed-payment record (dict or pandas.Series).
                Recognized keys used here:
                    - "dispute_flag": bool, optional (defaults to False if absent)
                    - "prior_attempts": int
                    - "timestamp_of_failure": str, "YYYY-MM-DD HH:MM:SS"
            proposed_decision: The dict returned by `RetryEngine.decide()`,
                must contain at least "action" (str).
            current_time: Optional datetime to evaluate the blackout window
                against. If omitted, we fall back to parsing the record's
                own `timestamp_of_failure` as a stand-in "current time" --
                this keeps the method fully deterministic and testable
                without requiring a real wall clock, which is exactly what
                we want for batch simulation (see core/simulator.py). In a
                live/production system, callers should pass the actual
                current wall-clock time (`datetime.now()`) instead.

        Returns:
            A dict with keys:
                - "is_allowed": bool
                - "block_reason": str (empty string if is_allowed is True)
        """
        proposed_action = proposed_decision.get("action", "do_not_retry")

        # --- Rule 0: nothing to block if we're already not retrying -----
        if proposed_action == "do_not_retry":
            return self._result(is_allowed=True, block_reason="")

        # --- Resolve the evaluation timestamp before checking rules ----
        evaluation_time = current_time or self._parse_timestamp(
            record.get("timestamp_of_failure")
        )

        # --- Rule 1: active dispute always wins --------------------------
        if bool(record.get("dispute_flag", False)):
            return self._result(
                is_allowed=False,
                block_reason=(
                    "Active dispute is open on this payment; automated "
                    "retries are blocked pending dispute resolution."
                ),
            )

        # --- Rule 2: overnight blackout window ---------------------------
        if evaluation_time is not None and self._is_in_blackout_window(evaluation_time):
            return self._result(
                is_allowed=False,
                block_reason=(
                    f"Blocked by blackout window: retries are not permitted "
                    f"between {BLACKOUT_START.strftime('%I:%M %p')} and "
                    f"{BLACKOUT_END.strftime('%I:%M %p')}; evaluation time "
                    f"was {evaluation_time.strftime('%I:%M %p')}."
                ),
            )

        # --- Rule 3: minimum cooldown between attempts --------------------
        prior_attempts = int(record.get("prior_attempts", 0))
        if proposed_action == "retry_now" and prior_attempts > 0:
            return self._result(
                is_allowed=False,
                block_reason=(
                    f"'retry_now' was proposed but this payment already has "
                    f"{prior_attempts} prior attempt(s); a minimum "
                    f"{MINIMUM_COOLDOWN_HOURS}-hour cooldown between attempts "
                    "is enforced to avoid spamming the payment gateway."
                ),
            )

        # --- No global safeguard triggered: allow the proposed action ---
        return self._result(is_allowed=True, block_reason="")

    @staticmethod
    def _parse_timestamp(raw_timestamp) -> Optional[datetime]:
        """
        Safely parses a "YYYY-MM-DD HH:MM:SS" string into a datetime.
        Returns None if the value is missing or malformed, in which case
        the blackout-window rule is simply skipped for that record (we
        never want a parsing failure to silently block a legitimate retry).
        """
        if not raw_timestamp:
            return None
        try:
            return datetime.strptime(str(raw_timestamp), TIMESTAMP_FORMAT)
        except ValueError:
            return None

    @staticmethod
    def _is_in_blackout_window(moment: datetime) -> bool:
        """
        Returns True if `moment`'s time-of-day falls inside the overnight
        blackout window. The window wraps past midnight (22:00 -> 08:00
        the next day), so this is a "wrap-around" comparison rather than a
        simple start <= t <= end check.
        """
        current_clock_time = moment.time()
        return current_clock_time >= BLACKOUT_START or current_clock_time < BLACKOUT_END

    @staticmethod
    def _result(is_allowed: bool, block_reason: str) -> dict:
        """Small internal helper to keep the return shape consistent."""
        return {"is_allowed": is_allowed, "block_reason": block_reason}
