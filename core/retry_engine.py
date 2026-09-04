"""
retry_engine.py
----------------
Defines `RetryEngine`: a rule-based decision engine that takes the output
of `FailureClassifier` (plus raw context about the customer) and decides
WHAT to do next -- i.e. whether, and when, to retry the failed charge.

Design philosophy:
    This engine deliberately sits *downstream* of FailureClassifier and
    does not re-interpret the raw failure_reason itself. It trusts the
    classifier's `classified_reason` + `confidence_score` as its primary
    input, and layers retry-timing business logic on top. This separation
    of concerns means:
      - FailureClassifier answers: "What actually happened?"
      - RetryEngine answers:       "Given that, what should we do about it?"

    Keeping these as two independent, composable classes (rather than one
    monolithic function) makes each one independently testable and lets
    either be swapped out later (e.g. FailureClassifier could eventually
    be replaced by an ML model) without touching the other.
"""

from __future__ import annotations

from typing import Mapping, Union
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import pandas as pd

Record = Union[Mapping, "pd.Series"]  # noqa: F821  (pandas is an optional runtime dep, not imported here)

# The only four actions this engine is allowed to output. Kept as a
# frozenset (rather than a free-form string) so callers/tests can validate
# against a single source of truth.
VALID_ACTIONS = frozenset(
    {"retry_now", "retry_in_24h", "retry_in_3_days", "do_not_retry"}
)


class RetryEngine:
    """
    Rule-based engine that decides the retry `action` + `reasoning` for a
    failed payment, based on its classification and customer context.

    Usage:
        classifier = FailureClassifier()
        engine = RetryEngine()

        classification = classifier.classify(record)
        decision = engine.decide(
            classification=classification,
            payment_history=record["customer_payment_history"],
            prior_attempts=record["prior_attempts"],
        )
        # decision == {
        #     "action": "retry_in_24h",
        #     "reasoning": "...",
        # }

    Business rules (evaluated in priority order, first confident match wins):

        1. Hard cap: prior_attempts >= 3
           -> "do_not_retry"
           We never retry a payment method that has already failed 3+
           times in this cycle -- further attempts risk issuer-side fraud
           flags and a poor customer experience, regardless of reason.

        2. classified_reason == "infrastructure_timeout"
           -> "retry_now"
           Since this is a transient, non-customer-caused failure (e.g.
           raw "network timeout"), the safest and fastest recovery is an
           immediate retry -- the underlying payment method is presumed
           fine.

        3. classified_reason == "card_expired"
           -> "do_not_retry"
           Retrying against a card that is factually expired will simply
           fail again. This requires a customer-initiated card update, not
           a scheduled retry, so we stop the automated retry loop here.

        4. classified_reason == "temporary_anomaly"
           -> "retry_in_24h"
           A likely one-off blip (e.g. bank decline on a good-history
           customer) usually self-resolves quickly, so a short 24h
           cooldown is enough before trying again.

        5. classified_reason == "insufficient_funds":
             * payment_history == "good"  -> "retry_in_24h"
               (good-history customers typically resolve short-term cash
               flow gaps quickly).
             * payment_history == "patchy" -> "retry_in_24h"
               (still worth a fast retry; patchy isn't necessarily a
               structural funding issue).
             * payment_history == "bad"   -> "retry_in_3_days"
               (bad-history customers with insufficient funds usually need
               a longer runway, e.g. until a payday cycle).

        6. classified_reason == "chronic_payment_risk"
           -> "retry_in_3_days"
           A customer already flagged as high-risk (repeated failures on
           bad history) shouldn't be retried aggressively. A longer cool
           down reduces the number of consecutive failed-charge
           notifications while still giving them a fair chance to recover.

        7. classified_reason == "issuer_side_issue"
           -> "retry_in_24h"
           Issuer-side problems (bank system hiccups, temporary holds)
           tend to resolve within a business day.

        8. classified_reason == "unclassified" (fallback from classifier)
           -> "retry_in_24h"
           We deliberately don't give up on an unclassified failure --
           but we also don't retry instantly, since we're not confident
           about the cause. A 24h buffer plus (implicitly) manual review
           is the safer default.

    Additional guardrail:
        Regardless of the reason-driven decision above, if `opt_out_flag`
        is present and True on the record passed to `decide()`, the
        engine always returns "do_not_retry" -- customer consent overrides
        every other business rule.
    """

    def decide(
        self,
        classification: dict,
        payment_history: str,
        prior_attempts: int,
        opt_out_flag: bool = False,
    ) -> dict:
        """
        Decides the retry action for a single failed payment.

        Args:
            classification: The dict returned by `FailureClassifier.classify()`,
                must contain "classified_reason" (str) and "confidence_score" (float).
            payment_history: One of "good" | "patchy" | "bad".
            prior_attempts: Integer count (0-3) of prior retry attempts
                already made for this payment.
            opt_out_flag: Whether the customer has opted out of automated
                retries. Defaults to False if not provided. When True, this
                always overrides every other rule.

        Returns:
            A dict with keys:
                - "action": one of VALID_ACTIONS
                - "reasoning": a short human-readable explanation of why
                  this action was chosen (useful for logs, dashboards, and
                  any human-in-the-loop review).
        """
        classified_reason = classification.get("classified_reason", "unclassified")
        payment_history = str(payment_history).strip().lower()
        prior_attempts = int(prior_attempts)

        # --- Guardrail: customer consent always wins --------------------
        if opt_out_flag:
            return self._result(
                "do_not_retry",
                "Customer has opted out of automated retry attempts; "
                "consent overrides all other retry logic.",
            )

        # --- Rule 1: hard cap on prior attempts --------------------------
        if prior_attempts >= 3:
            return self._result(
                "do_not_retry",
                f"Payment has already failed {prior_attempts} times in this "
                "cycle, which meets the maximum retry cap. Further automated "
                "retries risk issuer fraud flags and customer frustration; "
                "this record should move to manual/human follow-up.",
            )

        # --- Rule 2: infrastructure timeout -> retry immediately --------
        if classified_reason == "infrastructure_timeout":
            return self._result(
                "retry_now",
                "Classified as an infrastructure-side timeout, not a "
                "customer funding issue. The payment method is presumed "
                "healthy, so an immediate retry is the fastest safe path "
                "to recovery.",
            )

        # --- Rule 3: expired card -> stop automated retries --------------
        if classified_reason == "card_expired":
            return self._result(
                "do_not_retry",
                "The card on file is expired. Retrying the same card will "
                "deterministically fail again; recovery requires the "
                "customer to update their payment method before any retry "
                "is scheduled.",
            )

        # --- Rule 4: temporary anomaly -> short cooldown -----------------
        if classified_reason == "temporary_anomaly":
            return self._result(
                "retry_in_24h",
                "Classified as a likely one-off anomaly (e.g. a bank "
                "decline on an otherwise good-standing customer). These "
                "typically self-resolve quickly, so a 24-hour cooldown "
                "before retrying is sufficient.",
            )

        # --- Rule 5: insufficient funds -> depends on payment history ---
        if classified_reason == "insufficient_funds":
            if payment_history == "bad":
                return self._result(
                    "retry_in_3_days",
                    "Insufficient funds combined with a 'bad' payment "
                    "history suggests a structural cash-flow gap rather "
                    "than a momentary shortfall. Waiting ~3 days gives the "
                    "customer a realistic window (e.g. a payday cycle) to "
                    "have funds available.",
                )
            return self._result(
                "retry_in_24h",
                f"Insufficient funds with a '{payment_history}' payment "
                "history is more likely a short-term gap. A 24-hour "
                "cooldown balances giving the customer time to top up "
                "funds against recovering revenue quickly.",
            )

        # --- Rule 6: chronic payment risk -> longer cooldown -------------
        if classified_reason == "chronic_payment_risk":
            return self._result(
                "retry_in_3_days",
                "Customer shows a pattern of repeated failures on a poor "
                "payment history, indicating elevated ongoing risk. A "
                "longer 3-day cooldown avoids stacking failed-charge "
                "notifications while still allowing recovery.",
            )

        # --- Rule 7: issuer-side issue -> moderate cooldown ---------------
        if classified_reason == "issuer_side_issue":
            return self._result(
                "retry_in_24h",
                "Classified as an issuer-side problem (e.g. bank system "
                "issue or temporary hold), which typically clears within "
                "one business day. A 24-hour retry balances promptness "
                "with giving the issuer time to resolve the underlying "
                "issue.",
            )

        # --- Rule 8: fallback for anything unclassified -------------------
        return self._result(
            "retry_in_24h",
            "The failure reason could not be confidently classified. "
            "Rather than retrying instantly or giving up, a 24-hour "
            "cooldown is used as a safe default while the record is "
            "flagged for review.",
        )

    @staticmethod
    def _result(action: str, reasoning: str) -> dict:
        """
        Small internal helper that builds the return dict and asserts the
        action is one of the four contractually allowed values -- this
        guards against a future rule branch accidentally returning a typo'd
        or invalid action string.
        """
        assert action in VALID_ACTIONS, f"Invalid action produced: {action!r}"
        return {"action": action, "reasoning": reasoning}
