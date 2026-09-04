"""
failure_classifier.py
----------------------
Defines `FailureClassifier`: a rule-based engine that takes a single raw
failed-payment record and produces a STANDARDIZED classification of why
the payment failed, along with a confidence score.

Design philosophy:
    Raw failure reasons coming from a payment gateway (e.g. "bank decline")
    are often ambiguous -- the same raw label can mean very different
    things depending on the customer's history. A "bank decline" for a
    customer with a spotless payment record is probably a one-off blip;
    the same raw reason for a customer with a "bad" history is more
    likely a genuine, recurring funding problem.

    Rather than treating the raw `failure_reason` as ground truth, this
    classifier applies a small set of interpretable heuristics that
    combine the raw reason with contextual signals (`customer_payment_history`,
    `prior_attempts`) to produce a `classified_reason` that better reflects
    the *underlying* situation, plus a `confidence_score` that reflects how
    certain the rule engine is about that interpretation.

    This is intentionally rule-based (no ML) so that:
      1. Every decision is fully explainable to the business/compliance team.
      2. It can run with zero training data, on day one.
      3. It serves as a deterministic baseline that a future ML model can
         be benchmarked against.
"""

from __future__ import annotations

from typing import Mapping, Union
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import pandas as pd

# A record can come in as a dict or a pandas Series; both support the same
# `record[key]` / `record.get(key)` access pattern we rely on below.
Record = Union[Mapping, "pd.Series"]  # noqa: F821  (pandas is an optional runtime dep, not imported here)


class FailureClassifier:
    """
    Rule-based classifier that maps a raw failed-payment record to a
    standardized `classified_reason` + `confidence_score`.

    Usage:
        classifier = FailureClassifier()
        result = classifier.classify(record)
        # result == {
        #     "classified_reason": "insufficient_funds",
        #     "confidence_score": 0.95,
        #     "raw_reason": "insufficient funds",
        # }

    Standardized `classified_reason` values produced by this class:
        - "insufficient_funds"        : genuine lack of funds
        - "temporary_anomaly"         : likely a one-off blip, not systemic
        - "card_expired"              : the card itself is no longer valid
        - "issuer_side_issue"         : problem sits with the card issuer/bank
        - "infrastructure_timeout"    : transient network/infra failure
        - "chronic_payment_risk"      : repeated failures + poor history,
                                         signals a high-risk customer
        - "unclassified"              : fallback when no rule confidently applies

    Rule branches (evaluated in priority order, first match wins):
        1. Raw reason == "network timeout"
           -> "infrastructure_timeout", confidence 0.97
           This is the clearest, least ambiguous signal available: a
           timeout is a plumbing problem, not a customer problem.

        2. Raw reason == "expired card"
           -> "card_expired", confidence 0.98
           Card expiry is a deterministic, unambiguous fact -- there is
           essentially no interpretation needed, hence the very high
           confidence.

        3. Raw reason == "insufficient funds"
           -> "insufficient_funds", confidence 0.95 (default), but:
             * If payment_history == "bad" AND prior_attempts >= 2:
               -> escalated to "chronic_payment_risk", confidence 0.9
               (repeated insufficient-funds failures on a bad-history
               customer is no longer just "insufficient funds", it is a
               pattern worth flagging distinctly).

        4. Raw reason == "bank decline":
             * If payment_history == "good":
               -> "temporary_anomaly", confidence 0.7
               (a good-standing customer getting declined is most likely
               a fluke -- fraud-rule false positive, temporary card
               freeze, etc. -- rather than a real funding issue).
             * If payment_history == "patchy":
               -> "issuer_side_issue", confidence 0.55
               (genuinely ambiguous: could be the customer, could be the
               bank; we lean toward issuer-side but with modest
               confidence).
             * If payment_history == "bad":
               -> "chronic_payment_risk", confidence 0.75
               (a bad-history customer being declined is more likely a
               real, recurring problem than a fluke).

        5. Raw reason == "issuer unavailable"
           -> "issuer_side_issue", confidence 0.85
           The raw label itself already names the issuer as the cause, so
           confidence is high regardless of customer history.

        6. Fallback (raw reason not recognized)
           -> "unclassified", confidence 0.3
           Low confidence signals downstream systems should treat this
           record cautiously (e.g. route to manual review) rather than
           silently defaulting to a "safe" reason.
    """

    # Recognized raw reasons, used for lightweight input validation/logging.
    KNOWN_RAW_REASONS = frozenset(
        {
            "insufficient funds",
            "expired card",
            "bank decline",
            "network timeout",
            "issuer unavailable",
        }
    )

    def classify(self, record: Record) -> dict:
        """
        Classifies a single failed payment record.

        Args:
            record: A dict-like or pandas.Series object that must contain
                (at minimum) the keys:
                    - "failure_reason": str
                    - "customer_payment_history": str ("good" | "patchy" | "bad")
                    - "prior_attempts": int (0-3)

        Returns:
            A dict with keys:
                - "classified_reason": str, one of the standardized labels above
                - "confidence_score": float in [0.0, 1.0]
                - "raw_reason": str, the original unmodified failure_reason
                  (kept for traceability/auditing)
        """
        raw_reason = str(record.get("failure_reason", "")).strip().lower()
        payment_history = str(record.get("customer_payment_history", "")).strip().lower()
        prior_attempts = int(record.get("prior_attempts", 0))

        classified_reason, confidence_score = self._apply_rules(
            raw_reason=raw_reason,
            payment_history=payment_history,
            prior_attempts=prior_attempts,
        )

        return {
            "classified_reason": classified_reason,
            "confidence_score": round(confidence_score, 3),
            "raw_reason": raw_reason,
        }

    def _apply_rules(
        self,
        raw_reason: str,
        payment_history: str,
        prior_attempts: int,
    ) -> tuple[str, float]:
        """
        Core heuristic decision tree. Returns (classified_reason, confidence).
        Kept as a private method so `classify()` stays focused on
        input/output shape, while this method stays focused purely on logic.
        """

        # --- Rule 1: network timeout -> infrastructure issue -----------
        if raw_reason == "network timeout":
            return "infrastructure_timeout", 0.97

        # --- Rule 2: expired card -> unambiguous, deterministic fact ----
        if raw_reason == "expired card":
            return "card_expired", 0.98

        # --- Rule 3: insufficient funds ---------------------------------
        if raw_reason == "insufficient funds":
            if payment_history == "bad" and prior_attempts >= 2:
                return "chronic_payment_risk", 0.90
            return "insufficient_funds", 0.95

        # --- Rule 4: bank decline -- history-dependent ambiguity --------
        if raw_reason == "bank decline":
            if payment_history == "good":
                return "temporary_anomaly", 0.70
            if payment_history == "patchy":
                return "issuer_side_issue", 0.55
            if payment_history == "bad":
                return "chronic_payment_risk", 0.75
            # payment_history missing/unknown: stay cautious
            return "issuer_side_issue", 0.45

        # --- Rule 5: issuer unavailable -> issuer-side by definition ----
        if raw_reason == "issuer unavailable":
            return "issuer_side_issue", 0.85

        # --- Rule 6: fallback for unrecognized raw reasons --------------
        return "unclassified", 0.30
