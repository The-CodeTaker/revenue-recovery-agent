
"""
test_audit_trail.py
---------------------
Isolated, sharp assertions on the audit trail's build/append/load/search
contract: entry shape, append-only durability across multiple writes,
tolerance of a corrupted line, and every search_audit_trail() filter
(including the case-insensitive customer_id match and most-recent-first
ordering).

Every test redirects core.audit_trail's module-level RESULTS_DIR /
AUDIT_TRAIL_PATH into a fresh tmp_path via monkeypatch (see the autouse
`isolated_audit_trail` fixture below) -- this suite never reads or writes
the project's real results/audit_trail.jsonl.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import json

import pytest
from core import audit_trail


@pytest.fixture(autouse=True)
def isolated_audit_trail(tmp_path, monkeypatch):
    """Redirects RESULTS_DIR/AUDIT_TRAIL_PATH to a fresh tmp_path for the
    duration of each test. Applied automatically (autouse) to every test
    in this file -- this isolation is a hard requirement, not opt-in per
    test, since real audit history must never be touched by the suite."""
    results_dir = tmp_path / "results"
    trail_path = results_dir / "audit_trail.jsonl"
    monkeypatch.setattr(audit_trail, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(audit_trail, "AUDIT_TRAIL_PATH", trail_path)
    return trail_path


EXPECTED_ENTRY_KEYS = {
    "audit_id",
    "timestamp",
    "source",
    "customer_id",
    "amount",
    "subscription_plan",
    "failure_reason",
    "prior_attempts",
    "dispute_flag",
    "opt_out_flag",
    "classification",
    "proposed_action",
    "retry_reasoning",
    "stopping_rules_verdict",
    "effective_action",
    "escalation_stage",
    "message_sent",
    "message_source",
    "voice_file",
}


def _record(
    customer_id: str = "CUST-000001",
    amount: float = 499.0,
    subscription_plan: str = "pro_monthly",
    failure_reason: str = "insufficient_funds",
    prior_attempts: int = 1,
    dispute_flag: bool = False,
    opt_out_flag: bool = False,
) -> dict:
    return {
        "customer_id": customer_id,
        "amount": amount,
        "subscription_plan": subscription_plan,
        "failure_reason": failure_reason,
        "prior_attempts": prior_attempts,
        "dispute_flag": dispute_flag,
        "opt_out_flag": opt_out_flag,
    }


def _classification(
    classified_reason: str = "insufficient_funds",
    confidence_score: float = 0.9,
    raw_reason: str = "insufficient funds",
) -> dict:
    return {
        "classified_reason": classified_reason,
        "confidence_score": confidence_score,
        "raw_reason": raw_reason,
    }


def _decision(action: str = "retry_in_24h", reasoning: str = "standard retry window") -> dict:
    return {"action": action, "reasoning": reasoning}


def _verdict(is_allowed: bool = True, block_reason: str = "") -> dict:
    return {"is_allowed": is_allowed, "block_reason": block_reason}


def _entry(
    customer_id: str = "CUST-000001",
    effective_action: str = "retry_in_24h",
    source: str = "batch_run",
    **build_kwargs,
) -> dict:
    """Builds one realistic audit entry via build_audit_entry(), with
    sensible defaults for everything the caller doesn't care about."""
    return audit_trail.build_audit_entry(
        source=source,
        record=_record(customer_id=customer_id),
        classification=_classification(),
        decision=_decision(),
        verdict=_verdict(),
        effective_action=effective_action,
        escalation_stage=build_kwargs.pop("escalation_stage", "soft_reminder"),
        message_sent=build_kwargs.pop("message_sent", "Hi, aapka payment fail ho gaya hai."),
        message_source=build_kwargs.pop("message_source", "template_fallback"),
        voice_file=build_kwargs.pop("voice_file", None),
        **build_kwargs,
    )


def test_build_audit_entry_contains_all_expected_keys():
    """build_audit_entry() must produce a dict with exactly the full,
    established audit-entry schema -- no missing or extra keys."""
    entry = audit_trail.build_audit_entry(
        source="batch_run",
        record=_record(),
        classification=_classification(),
        decision=_decision(),
        verdict=_verdict(),
        effective_action="retry_in_24h",
        escalation_stage="soft_reminder",
        message_sent="Hi Rohan, aapka payment fail ho gaya hai.",
        message_source="template_fallback",
        voice_file=None,
    )
    assert set(entry.keys()) == EXPECTED_ENTRY_KEYS


def test_append_and_load_single_entry_round_trips():
    """A single appended entry must come back from load_audit_trail()
    unchanged."""
    entry = _entry(customer_id="CUST-000001")
    audit_trail.append_audit_entry(entry)

    loaded = audit_trail.load_audit_trail()

    assert loaded == [entry]


def test_append_audit_entry_multiple_calls_is_additive():
    """Three separate append_audit_entry() calls must all survive --
    none overwritten -- and come back in the order they were written."""
    entry_1 = _entry(customer_id="CUST-000001")
    entry_2 = _entry(customer_id="CUST-000002")
    entry_3 = _entry(customer_id="CUST-000003")

    audit_trail.append_audit_entry(entry_1)
    audit_trail.append_audit_entry(entry_2)
    audit_trail.append_audit_entry(entry_3)

    loaded = audit_trail.load_audit_trail()

    assert len(loaded) == 3
    assert [e["customer_id"] for e in loaded] == [
        "CUST-000001",
        "CUST-000002",
        "CUST-000003",
    ]


def test_append_audit_entries_batch_writes_all():
    """append_audit_entries() (plural, batch form) must write every entry
    in the list in a single call."""
    entries = [
        _entry(customer_id="CUST-000010"),
        _entry(customer_id="CUST-000011"),
        _entry(customer_id="CUST-000012"),
    ]

    audit_trail.append_audit_entries(entries)

    loaded = audit_trail.load_audit_trail()

    assert len(loaded) == 3
    assert [e["customer_id"] for e in loaded] == [
        "CUST-000010",
        "CUST-000011",
        "CUST-000012",
    ]


def test_append_audit_entries_empty_list_is_noop(isolated_audit_trail):
    """append_audit_entries([]) must not create the audit trail file, and
    must not raise."""
    audit_trail.append_audit_entries([])

    assert not isolated_audit_trail.exists()


def test_load_audit_trail_missing_file_returns_empty_list():
    """load_audit_trail() on a path that has never been written to must
    return an empty list, not raise."""
    assert audit_trail.load_audit_trail() == []


def test_load_audit_trail_skips_malformed_line(isolated_audit_trail):
    """A single corrupted line must be skipped without raising, and must
    not take down the valid entries around it."""
    valid_entry_1 = _entry(customer_id="CUST-000020")
    valid_entry_2 = _entry(customer_id="CUST-000021")

    audit_trail.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(isolated_audit_trail, "w", encoding="utf-8") as f:
        f.write(json.dumps(valid_entry_1) + "\n")
        f.write("{this is not valid json,,,\n")
        f.write(json.dumps(valid_entry_2) + "\n")

    loaded = audit_trail.load_audit_trail()

    assert len(loaded) == 2
    assert [e["customer_id"] for e in loaded] == ["CUST-000020", "CUST-000021"]


def test_search_audit_trail_filters_by_customer_id_case_insensitive_and_most_recent_first():
    """customer_id filtering must be case-insensitive, and matches must
    come back most-recent-first (reverse of append order)."""
    entry_1 = _entry(customer_id="CUST-000001", effective_action="retry_now")
    entry_2 = _entry(customer_id="CUST-000002", effective_action="retry_now")
    entry_3 = _entry(customer_id="CUST-000001", effective_action="retry_in_24h")
    audit_trail.append_audit_entries([entry_1, entry_2, entry_3])

    results = audit_trail.search_audit_trail(customer_id="cust-000001")

    assert len(results) == 2
    # Most recent (entry_3) must come first.
    assert results[0]["effective_action"] == "retry_in_24h"
    assert results[1]["effective_action"] == "retry_now"


def test_search_audit_trail_filters_by_effective_action():
    """effective_action filtering must be an exact match."""
    entry_1 = _entry(customer_id="CUST-000001", effective_action="retry_now")
    entry_2 = _entry(customer_id="CUST-000002", effective_action="do_not_retry")
    entry_3 = _entry(customer_id="CUST-000003", effective_action="retry_now")
    audit_trail.append_audit_entries([entry_1, entry_2, entry_3])

    results = audit_trail.search_audit_trail(effective_action="retry_now")

    assert len(results) == 2
    assert all(e["effective_action"] == "retry_now" for e in results)


def test_search_audit_trail_filters_by_source():
    """source filtering must be an exact match between batch_run and
    live_dashboard."""
    entry_1 = _entry(customer_id="CUST-000001", source="batch_run")
    entry_2 = _entry(customer_id="CUST-000002", source="live_dashboard")
    entry_3 = _entry(customer_id="CUST-000003", source="batch_run")
    audit_trail.append_audit_entries([entry_1, entry_2, entry_3])

    results = audit_trail.search_audit_trail(source="live_dashboard")

    assert len(results) == 1
    assert results[0]["customer_id"] == "CUST-000002"


def test_search_audit_trail_limit_caps_results_to_most_recent():
    """limit=N must cap results to the N most recent entries, not just
    the first N in file order."""
    entries = [_entry(customer_id=f"CUST-00000{i}") for i in range(1, 6)]
    audit_trail.append_audit_entries(entries)

    results = audit_trail.search_audit_trail(limit=2)

    assert len(results) == 2
    assert [e["customer_id"] for e in results] == ["CUST-000005", "CUST-000004"]


def test_search_audit_trail_combined_filters_apply_all():
    """Combining customer_id and effective_action must require BOTH to
    match -- neither filter alone is sufficient."""
    entry_1 = _entry(customer_id="CUST-000001", effective_action="retry_now")
    entry_2 = _entry(customer_id="CUST-000001", effective_action="do_not_retry")
    entry_3 = _entry(customer_id="CUST-000002", effective_action="retry_now")
    audit_trail.append_audit_entries([entry_1, entry_2, entry_3])

    results = audit_trail.search_audit_trail(
        customer_id="CUST-000001", effective_action="retry_now"
    )

    assert len(results) == 1
    assert results[0]["customer_id"] == "CUST-000001"
    assert results[0]["effective_action"] == "retry_now"
