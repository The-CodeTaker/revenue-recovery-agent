


"""
simulator.py
-------------
The master batch-processing script for the Revenue Recovery Agent's
decision engine. This ties together the full pipeline built so far:

    failed_payments.csv
          |
          v
    FailureClassifier   -> "what actually happened?"
          |
          v
    RetryEngine          -> "given that, what should we do?"
          |
          v
    StoppingRules         -> "are we actually allowed to do that?"
          |
          v
    Expected recovery modeling + reporting

It processes every record in the synthetic dataset through that pipeline,
then produces two reporting artifacts:

    1. results/batch_run_results.json
       High-level portfolio metrics: total failed revenue, a naive
       baseline recovery estimate, our engine's modeled recovery estimate,
       and the improvement over the naive baseline.

    2. results/unresolved_log.json
       A detailed log of every record that will NOT be retried -- either
       because RetryEngine itself decided "do_not_retry", or because
       StoppingRules vetoed an otherwise-approved retry. This is the list
       ops/support teams would triage manually.

On the "expected recovery" modeling:
    Neither the naive baseline nor our engine's estimate reflect real
    observed outcomes (we have no ground truth for whether a retry
    actually succeeded) -- this is a synthetic dataset. Instead, both
    numbers are *expected value* estimates: amount x probability of
    recovery, where the probability comes from either a flat industry-rule-
    of-thumb (naive) or from a lookup table keyed by our pipeline's own
    output (our engine). This makes the estimates fully deterministic,
    explainable, and reproducible -- exactly what you want before you have
    real outcome data to calibrate against.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

# --------------------------------------------------------------------------
# Make sure `core.*` imports resolve regardless of the working directory
# this script is launched from (e.g. `python core/simulator.py` from the
# project root, or run from inside `core/`).
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.audit_trail import AUDIT_TRAIL_PATH, append_audit_entries, build_audit_entry  # noqa: E402
from core.failure_classifier import FailureClassifier  # noqa: E402
from core.retry_engine import RetryEngine  # noqa: E402
from core.stopping_rules import StoppingRules  # noqa: E402
from messaging.hinglish_agent import HinglishAgent  # noqa: E402
from messaging.templates import escalation_stage_for_attempts  # noqa: E402

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

INPUT_CSV_PATH = PROJECT_ROOT / "data" / "failed_payments.csv"
RESULTS_DIR = PROJECT_ROOT / "results"
BATCH_RESULTS_PATH = RESULTS_DIR / "batch_run_results.json"
UNRESOLVED_LOG_PATH = RESULTS_DIR / "unresolved_log.json"

# Naive baseline assumption: a naive system retries every single failed
# payment instantly and just keeps hammering for 3 days, with no
# segmentation by reason or customer history. Industry rule-of-thumb for
# this kind of "spray and pray" approach is a flat ~15% recovery rate.
NAIVE_FLAT_RECOVERY_RATE = 0.15

# The synthetic dataset (data/failed_payments.csv) does not yet include a
# `dispute_flag` column. To actually exercise StoppingRules' dispute-flag
# safeguard in this simulation, we deterministically synthesize the flag
# for a small, fixed subset of records (seeded, so results are
# reproducible run-to-run). This is clearly logged/labeled below and is
# purely a simulation aid -- once the real pipeline captures genuine
# dispute flags from the payment gateway, this synthesis step should be
# deleted and the real column used directly.
SYNTHETIC_DISPUTE_RATE = 0.04
SYNTHETIC_DISPUTE_SEED = 2026

# --------------------------------------------------------------------------
# Expected recovery probability model
# --------------------------------------------------------------------------
#
# Maps (classified_reason, action) -> probability that the payment is
# ultimately recovered, given our engine took that action. These are
# hand-set, explainable estimates (not fit from data, since we have no
# ground-truth outcomes yet) that encode the same intuition RetryEngine's
# docstring already lays out: fast, low-friction paths (e.g. an
# infrastructure timeout retried immediately) recover at a high rate,
# while chronic-risk / long-cooldown paths recover at a lower rate.
#
# Any (classified_reason, action) combination not explicitly listed here
# falls back to DEFAULT_RECOVERY_PROBABILITY.
RECOVERY_PROBABILITY_TABLE = {
    ("infrastructure_timeout", "retry_now"): 0.85,
    ("temporary_anomaly", "retry_in_24h"): 0.75,
    ("issuer_side_issue", "retry_in_24h"): 0.65,
    ("insufficient_funds", "retry_in_24h"): 0.55,
    ("insufficient_funds", "retry_in_3_days"): 0.60,
    ("chronic_payment_risk", "retry_in_3_days"): 0.35,
    ("unclassified", "retry_in_24h"): 0.40,
    ("card_expired", "do_not_retry"): 0.10,  # occasional manual card update
    ("chronic_payment_risk", "do_not_retry"): 0.05,
}
DEFAULT_RECOVERY_PROBABILITY = 0.20  # conservative fallback for any unlisted combo

# The exhaustive set of raw dataset columns the pipeline actually reads
# from a record (audited against failure_classifier.py, retry_engine.py,
# stopping_rules.py, and this module's own record.get(...)/row[...]
# accesses). Columns the pipeline treats as genuinely optional -- i.e.
# every access already has a safe default or is synthesized when absent,
# such as `dispute_flag` (see _synthesize_dispute_flags above),
# `opt_out_flag`, and `timestamp_of_failure` -- are deliberately excluded
# here, since requiring them would break inputs the pipeline is already
# designed to handle gracefully.
REQUIRED_COLUMNS = [
    "customer_id",
    "amount",
    "subscription_plan",
    "failure_reason",
    "customer_payment_history",
    "prior_attempts",
]

# A record that RetryEngine approved but StoppingRules then blocked never
# has a charge attempt fire, so its expected recovery is always exactly 0.
BLOCKED_RECOVERY_PROBABILITY = 0.0


def _synthesize_dispute_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds a synthetic `dispute_flag` column to the dataframe so that
    StoppingRules' active-dispute safeguard has something to actually
    trigger on during this simulation (see module docstring above for why
    this is necessary and temporary).

    Uses a fixed seed so the same rows are flagged on every run, keeping
    batch_run_results.json reproducible.
    """
    df = df.copy()
    flagged_sample = df.sample(
        frac=SYNTHETIC_DISPUTE_RATE, random_state=SYNTHETIC_DISPUTE_SEED
    ).index
    df["dispute_flag"] = df.index.isin(flagged_sample)
    return df


def _expected_recovery_probability(
    record: dict, classified_reason: str, action: str, is_allowed: bool
) -> float:
    """
    Looks up the modeled probability of recovering a given payment, based
    on our engine's own classification + action, and whether StoppingRules
    actually allowed that action to proceed.

    When `is_allowed` is False, we do NOT collapse every blocked record to
    a flat probability -- a block is not a single homogenous outcome, so
    we differentiate by WHY it was blocked:

        1. Active dispute (`dispute_flag`): this is a legal/compliance
           hold, not a timing issue. We are not going to touch this
           payment at all in the foreseeable future, so there is no
           organic recovery signal left to model -- probability is a
           hard 0.0.

        2. Customer opt-out (`opt_out_flag`): we will never message or
           charge this customer again through this flow, but customers
           occasionally resolve the underlying issue on their own
           initiative (e.g. manually updating their card in-app) even
           without any nudge from us. We model that as a small "organic
           win-back" probability rather than a hard zero.

        3. Any other block (blackout window / minimum cooldown): these
           are TEMPORARY safeguards, not cancellations -- the exact same
           action RetryEngine proposed will still fire once the window
           clears or the cooldown elapses. So rather than zeroing this
           out, we reuse the probability that action would have had if
           it had been allowed to proceed right now, discounted by 10%
           to reflect the modest extra risk introduced by the delay
           (e.g. a card expiring in the interim, customer attention
           drifting to a different notification).
    """
    if is_allowed:
        return RECOVERY_PROBABILITY_TABLE.get(
            (classified_reason, action), DEFAULT_RECOVERY_PROBABILITY
        )

    # --- Blocked: active dispute -> no recoverable signal --------------
    if bool(record.get("dispute_flag", False)):
        return 0.0

    # --- Blocked: customer opted out -> small organic win-back chance --
    if bool(record.get("opt_out_flag", False)):
        return 0.05

    # --- Blocked: temporary safeguard (blackout / cooldown) ------------
    # The retry isn't cancelled, only delayed -- so we carry forward the
    # underlying action's probability, lightly discounted for the delay.
    underlying_probability = RECOVERY_PROBABILITY_TABLE.get(
        (classified_reason, action), DEFAULT_RECOVERY_PROBABILITY
    )
    return round(underlying_probability * 0.9, 4)


def _generate_batch_message(record: dict, hinglish_agent: HinglishAgent) -> tuple[Optional[str], str, Optional[str]]:
    """
    Generates the outbound message for one record during the batch run,
    for audit-trail purposes. Every record gets a message attempt EXCEPT
    customers who have opted out -- respecting opt_out_flag here (not
    just in RetryEngine) is what makes this "compliant escalation": we
    never contact a customer who has told us to stop, regardless of what
    the retry/stopping-rules layers decided about the payment itself.

    Returns:
        (message_sent, message_source, escalation_stage) where
        message_source is "ollama_llm", "template_fallback", or
        "not_generated" (opted-out customers only). Voice synthesis is
        deliberately NOT attempted here -- for a 120-record batch run,
        generating 120 .wav files is unnecessary demo weight; voice is
        exercised via the live single-customer dashboard flow instead.
    """
    if bool(record.get("opt_out_flag", False)):
        return None, "not_generated", None

    stage = escalation_stage_for_attempts(record.get("prior_attempts", 0))

    # HinglishAgent.generate_message() never raises -- it falls back to
    # the hardcoded template internally on any LLM failure. We only need
    # to figure out, after the fact, which of the two actually produced
    # the returned text, so the audit trail can be honest about it.
    fallback_text = render_message_fallback(record, stage)
    message = hinglish_agent.generate_message(record, stage=stage)
    message_source = "template_fallback" if message == fallback_text else "ollama_llm"

    return message, message_source, stage


def render_message_fallback(record: dict, stage: str) -> str:
    """
    Renders exactly what HinglishAgent's own internal fallback would
    produce for this record/stage, so `_generate_batch_message` can tell
    whether the LLM actually ran or the fallback silently kicked in --
    without needing HinglishAgent to expose that as a separate return
    value.
    """
    from messaging.templates import render_message

    raw_name = str(record.get("name") or record.get("customer_id") or "Customer")
    try:
        amount = f"{float(record.get('amount', 0)):,.2f}"
    except (TypeError, ValueError):
        amount = str(record.get("amount", 0))
    plan = str(record.get("subscription_plan", "your subscription"))
    return render_message(stage=stage, name=raw_name, amount=amount, plan=plan)


def _process_record(
    record: dict,
    classifier: FailureClassifier,
    retry_engine: RetryEngine,
    stopping_rules: StoppingRules,
    hinglish_agent: HinglishAgent,
) -> tuple[dict, dict]:
    """
    Runs a single failed-payment record through the full pipeline and
    returns (processed_record, audit_entry):
        - processed_record: the consolidated dict that metrics and the
          unresolved log are built from (unchanged shape from before).
        - audit_entry: the full decision-journey entry for the audit
          trail, including the message that was (or wasn't) sent.
    """
    classification = classifier.classify(record)

    decision = retry_engine.decide(
        classification=classification,
        payment_history=record.get("customer_payment_history", ""),
        prior_attempts=record.get("prior_attempts", 0),
        opt_out_flag=bool(record.get("opt_out_flag", False)),
    )

    stopping_verdict = stopping_rules.evaluate(record=record, proposed_decision=decision)

    # The "effective" action is what actually happens after StoppingRules
    # has had the final say -- if it blocked an otherwise-approved retry,
    # the effective outcome is functionally equivalent to "do_not_retry"
    # for this cycle, even though RetryEngine's *proposed* action differs.
    is_allowed = stopping_verdict["is_allowed"]
    effective_action = decision["action"] if is_allowed else "blocked_by_stopping_rules"

    recovery_probability = _expected_recovery_probability(
        record=record,
        classified_reason=classification["classified_reason"],
        action=decision["action"],
        is_allowed=is_allowed,
    )

    amount = float(record.get("amount", 0.0))
    expected_recovery_amount = round(amount * recovery_probability, 2)

    message_sent, message_source, escalation_stage = _generate_batch_message(
        record, hinglish_agent
    )

    processed_record = {
        "customer_id": record.get("customer_id"),
        "amount": amount,
        "subscription_plan": record.get("subscription_plan"),
        "failure_reason": record.get("failure_reason"),
        "customer_payment_history": record.get("customer_payment_history"),
        "prior_attempts": record.get("prior_attempts"),
        "dispute_flag": bool(record.get("dispute_flag", False)),
        "opt_out_flag": bool(record.get("opt_out_flag", False)),
        "classified_reason": classification["classified_reason"],
        "confidence_score": classification["confidence_score"],
        "proposed_action": decision["action"],
        "retry_reasoning": decision["reasoning"],
        "is_allowed": is_allowed,
        "block_reason": stopping_verdict["block_reason"],
        "effective_action": effective_action,
        "recovery_probability": recovery_probability,
        "expected_recovery_amount": expected_recovery_amount,
    }

    audit_entry = build_audit_entry(
        source="batch_run",
        record=record,
        classification=classification,
        decision=decision,
        verdict=stopping_verdict,
        effective_action=effective_action,
        escalation_stage=escalation_stage,
        message_sent=message_sent,
        message_source=message_source,
        voice_file=None,
    )

    return processed_record, audit_entry


def run_simulation(input_csv_path: Optional[Path] = None) -> tuple[dict, list]:
    """
    Runs the full batch simulation end-to-end.

    Args:
        input_csv_path: Optional override for the input dataset path.
            Defaults to data/failed_payments.csv under the project root.

    Returns:
        A tuple of (metrics_dict, unresolved_records_list, audit_entries_list),
        where metrics/unresolved_records match exactly what gets written
        to batch_run_results.json and unresolved_log.json respectively,
        and audit_entries_list is the full per-record decision journey
        for every record in this run (persisted to
        results/audit_trail.jsonl by `main()`).
    """
    csv_path = input_csv_path or INPUT_CSV_PATH
    df = pd.read_csv(csv_path)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Input CSV is missing required columns: {missing}. "
            f"Expected columns: {REQUIRED_COLUMNS}"
        )

    if "dispute_flag" not in df.columns:
        df = _synthesize_dispute_flags(df)

    classifier = FailureClassifier()
    retry_engine = RetryEngine()
    stopping_rules = StoppingRules()
    hinglish_agent = HinglishAgent()

    print("Warming up Ollama...")
    try:
        hinglish_agent._call_ollama("Say hello.")
    except Exception:
        pass

    processed_records = []
    audit_entries = []
    for _, row in df.iterrows():
        processed_record, audit_entry = _process_record(
            row.to_dict(), classifier, retry_engine, stopping_rules, hinglish_agent
        )
        processed_records.append(processed_record)
        audit_entries.append(audit_entry)

    metrics = _compute_metrics(processed_records)
    unresolved_records = _build_unresolved_log(processed_records)

    return metrics, unresolved_records, audit_entries


def _compute_metrics(processed_records: list) -> dict:
    """
    Aggregates the full portfolio of processed records into the top-level
    metrics we report: total failed revenue, naive baseline recovery,
    our engine's expected recovery, and the improvement over baseline.
    """
    total_failed_amount = round(sum(r["amount"] for r in processed_records), 2)

    naive_recovery_amount = round(total_failed_amount * NAIVE_FLAT_RECOVERY_RATE, 2)

    engine_recovery_amount = round(
        sum(r["expected_recovery_amount"] for r in processed_records), 2
    )

    if naive_recovery_amount > 0:
        improvement_pct = round(
            ((engine_recovery_amount - naive_recovery_amount) / naive_recovery_amount) * 100,
            2,
        )
    else:
        improvement_pct = 0.0

    action_breakdown = pd.Series(
        [r["effective_action"] for r in processed_records]
    ).value_counts().to_dict()

    blocked_count = sum(1 for r in processed_records if not r["is_allowed"])
    do_not_retry_count = sum(
        1 for r in processed_records if r["proposed_action"] == "do_not_retry"
    )

    return {
        "total_records_processed": len(processed_records),
        "total_failed_revenue_inr": total_failed_amount,
        "naive_baseline": {
            "assumed_flat_recovery_rate": NAIVE_FLAT_RECOVERY_RATE,
            "recovered_revenue_inr": naive_recovery_amount,
        },
        "engine_recovery": {
            "recovered_revenue_inr": engine_recovery_amount,
        },
        "improvement_over_naive_baseline_pct": improvement_pct,
        "effective_action_breakdown": action_breakdown,
        "blocked_by_stopping_rules_count": blocked_count,
        "do_not_retry_count": do_not_retry_count,
    }


def _build_unresolved_log(processed_records: list) -> list:
    """
    Extracts every record that will NOT be retried this cycle -- i.e.
    RetryEngine proposed "do_not_retry", OR StoppingRules blocked an
    otherwise-approved retry action. This is the operational hand-off list
    for a human/ops team to review (e.g. reach out manually, escalate a
    dispute, or prompt the customer to update an expired card).
    """
    return [
        record
        for record in processed_records
        if record["proposed_action"] == "do_not_retry" or not record["is_allowed"]
    ]


def main(input_csv_path: Optional[Path] = None) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    metrics, unresolved_records, audit_entries = run_simulation(input_csv_path)

    with open(BATCH_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)

    with open(UNRESOLVED_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(unresolved_records, f, indent=2, ensure_ascii=False)

    # Audit trail is append-only across runs (see core/audit_trail.py
    # docstring for why) -- re-running the simulator adds a fresh batch
    # of 120 entries rather than overwriting prior history.
    append_audit_entries(audit_entries)

    print(f"Processed {metrics['total_records_processed']} records.")
    print(f"Total failed revenue:     ₹{metrics['total_failed_revenue_inr']:,.2f}")
    print(f"Naive baseline recovery:  ₹{metrics['naive_baseline']['recovered_revenue_inr']:,.2f}")
    print(f"Engine expected recovery: ₹{metrics['engine_recovery']['recovered_revenue_inr']:,.2f}")
    print(f"Improvement over naive:   {metrics['improvement_over_naive_baseline_pct']}%")
    print(f"Unresolved / blocked records logged: {len(unresolved_records)}")
    print(f"Audit trail entries appended: {len(audit_entries)}")
    print(
        f"\nResults written to:\n  {BATCH_RESULTS_PATH}\n  {UNRESOLVED_LOG_PATH}\n"
        f"  {AUDIT_TRAIL_PATH}"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the Revenue Recovery Agent batch simulation."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            "Optional path to an alternate input CSV, for testing "
            "(e.g. a deliberately broken copy to verify schema "
            "validation) without touching the real "
            "data/failed_payments.csv."
        ),
    )
    args = parser.parse_args()
    main(input_csv_path=args.input)




