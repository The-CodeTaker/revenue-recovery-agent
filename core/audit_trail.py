
"""
audit_trail.py
---------------
Persistent, append-only audit log for the Revenue Recovery Agent.

This directly answers the buildathon track's grading bar: "Show measured
money recovered across a batch, with compliant escalation, stopping
rules, and an audit trail." Every record processed by the pipeline --
whether via the batch simulator (`core/simulator.py`) or a live
single-customer run from the dashboard ("Simulate Journey" button) --
gets one line written here capturing its full decision journey:

    classification -> proposed action -> stopping-rules check
    -> final (effective) action -> message sent (or why not)

Why JSONL (one JSON object per line), not a single JSON array:
    1. Append-only by construction. Writing a new entry is a single
       `open(..., "a")` + one line -- no read-modify-write of the whole
       file, so a batch run and a live dashboard click can both append
       safely without one clobbering the other's data.
    2. An audit trail should never be silently overwritten. A JSON array
       written with `json.dump()` requires rewriting the entire file on
       every update, which is exactly the failure mode an audit log must
       avoid (a crash mid-write can corrupt/truncate prior history).
    3. Trivially greppable/streamable even outside Python, which matters
       for a "prove this is a real audit trail" hackathon demo.

Every entry is immutable once written -- this module never edits or
deletes existing lines, only appends new ones.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "results"
AUDIT_TRAIL_PATH = RESULTS_DIR / "audit_trail.jsonl"


def build_audit_entry(
    *,
    source: str,
    record: dict,
    classification: dict,
    decision: dict,
    verdict: dict,
    effective_action: str,
    escalation_stage: Optional[str] = None,
    message_sent: Optional[str] = None,
    message_source: str = "not_generated",
    voice_file: Optional[str] = None,
) -> dict:
    """
    Assembles one audit-trail entry from the outputs of every pipeline
    stage. This is the single source of truth for the audit entry shape
    -- both `core/simulator.py` (batch, 120 records) and
    `dashboard/app.py` (live, single customer) call this so the schema
    never drifts between the two call sites.

    Args:
        source: Where this journey was run from -- "batch_run" (the
            120-record simulator pass) or "live_dashboard" (an operator
            clicking "Simulate Journey" for one customer_id).
        record: The raw failed-payment record (dict or pandas.Series
            already converted to dict).
        classification: Output of FailureClassifier.classify().
        decision: Output of RetryEngine.decide().
        verdict: Output of StoppingRules.evaluate().
        effective_action: The action that actually took effect after
            StoppingRules had the final say (see simulator.py for how
            this is derived).
        escalation_stage: The messaging escalation stage used, if a
            message was generated (e.g. "soft_reminder").
        message_sent: The final outbound message text, or None if no
            message was sent for this record (e.g. customer opted out).
        message_source: "ollama_llm" if the local LLM generated the
            message, "template_fallback" if it fell back to the
            hardcoded template, or "not_generated" if no message was
            attempted for this record at all.
        voice_file: Relative path to a synthesized .wav file, or None if
            voice synthesis was not attempted/unavailable/failed.
    """
    return {
        "audit_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "customer_id": record.get("customer_id"),
        "amount": record.get("amount"),
        "subscription_plan": record.get("subscription_plan"),
        "failure_reason": record.get("failure_reason"),
        "prior_attempts": record.get("prior_attempts"),
        "dispute_flag": bool(record.get("dispute_flag", False)),
        "opt_out_flag": bool(record.get("opt_out_flag", False)),
        "classification": {
            "classified_reason": classification.get("classified_reason"),
            "confidence_score": classification.get("confidence_score"),
            "raw_reason": classification.get("raw_reason"),
        },
        "proposed_action": decision.get("action"),
        "retry_reasoning": decision.get("reasoning"),
        "stopping_rules_verdict": {
            "is_allowed": verdict.get("is_allowed"),
            "block_reason": verdict.get("block_reason"),
        },
        "effective_action": effective_action,
        "escalation_stage": escalation_stage,
        "message_sent": message_sent,
        "message_source": message_source,
        "voice_file": voice_file,
    }


def append_audit_entry(entry: dict) -> None:
    """
    Appends a single audit entry as one JSON line to results/audit_trail.jsonl.
    Creates the results/ directory and the file itself if they don't
    exist yet. Never truncates or rewrites existing content.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_TRAIL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_audit_entries(entries: list[dict]) -> None:
    """
    Appends multiple audit entries in a single file open/close cycle --
    used by the batch simulator so writing all 120 records' entries
    doesn't open/close the file 120 times.
    """
    if not entries:
        return
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_TRAIL_PATH, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_audit_trail() -> list[dict]:
    """
    Reads and returns every entry ever written to the audit trail, in
    file order (oldest first). Returns an empty list if the file doesn't
    exist yet (e.g. the simulator hasn't been run since this feature
    shipped). Malformed lines are skipped rather than raising, so one
    corrupted line can never take down the whole audit view.
    """
    if not AUDIT_TRAIL_PATH.exists():
        return []

    entries = []
    with open(AUDIT_TRAIL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def search_audit_trail(
    customer_id: Optional[str] = None,
    effective_action: Optional[str] = None,
    source: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """
    Filters the full audit trail by any combination of customer_id,
    effective_action, and source. Results are returned most-recent-first
    (the natural order for a human scanning "what happened with this
    customer"), optionally capped to `limit` entries.

    All filters are exact-match except customer_id, which is
    case-insensitive to be forgiving of how an operator types it into
    the dashboard search box.
    """
    entries = load_audit_trail()

    if customer_id:
        needle = customer_id.strip().lower()
        entries = [
            e for e in entries
            if str(e.get("customer_id", "")).lower() == needle
        ]

    if effective_action:
        entries = [e for e in entries if e.get("effective_action") == effective_action]

    if source:
        entries = [e for e in entries if e.get("source") == source]

    entries = list(reversed(entries))

    if limit is not None:
        entries = entries[:limit]

    return entries
