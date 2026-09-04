


"""
dashboard/app.py
-----------------
Flask dashboard for the Revenue Recovery Agent hackathon demo.

Exposes:
    GET  /                -> renders the dashboard UI
    GET  /api/metrics      -> serves results/batch_run_results.json
    GET  /api/csrf-token   -> issues a CSRF token for the SPA frontend
    POST /api/simulate     -> runs a single customer_id through the full
                              pipeline (Classifier -> RetryEngine ->
                              StoppingRules -> HinglishAgent -> VoiceAgent)
                              and returns the entire decision "journey".

Security posture (see SECURITY.md for the full writeup):
    - Flask-Limiter rate-limits /api/simulate to prevent demo spam / DoS
      against the local Ollama + piper integrations.
    - Flask-WTF provides CSRF protection on state-changing POST requests.
    - All configuration is loaded from a local .env via python-dotenv --
      no secrets are hardcoded.
    - debug=False is explicit and non-negotiable outside a developer's
      own machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf

from core.audit_trail import append_audit_entry, build_audit_entry, search_audit_trail
from core.failure_classifier import FailureClassifier
from core.retry_engine import RetryEngine
from core.stopping_rules import StoppingRules
from messaging.hinglish_agent import HinglishAgent
from messaging.templates import escalation_stage_for_attempts, render_message
from messaging.transliteration import to_devanagari_for_voice
from messaging.voice_agent import VoiceAgent

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_CSV_PATH = PROJECT_ROOT / "data" / "failed_payments.csv"
BATCH_RESULTS_PATH = PROJECT_ROOT / "results" / "batch_run_results.json"
UNRESOLVED_LOG_PATH = PROJECT_ROOT / "results" / "unresolved_log.json"

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY")
if not app.config["SECRET_KEY"]:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. Define it in your .env file before "
        "starting the app -- a missing/hardcoded secret key would break "
        "both session security and CSRF token integrity."
    )
app.config["WTF_CSRF_TIME_LIMIT"] = None

csrf = CSRFProtect(app)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://"),
)

_classifier = FailureClassifier()
_retry_engine = RetryEngine()
_stopping_rules = StoppingRules()
_hinglish_agent = HinglishAgent(
    endpoint=os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434/api/generate"),
    model=os.environ.get("OLLAMA_MODEL", "llama3.1"),
)
_voice_agent = VoiceAgent()

_dataset_cache: "pd.DataFrame | None" = None


@app.after_request
def _attach_csrf_cookie(response):
    """
    Mirrors the current CSRF token into a readable cookie on every
    response so the frontend SPA can pick it up and echo it back via the
    `X-CSRFToken` header on state-changing requests (the standard
    double-submit pattern for JSON APIs protected by Flask-WTF).
    """
    response.set_cookie("csrf_token", generate_csrf(), samesite="Strict")
    return response


def _load_dataset() -> pd.DataFrame:
    """Lazily loads and caches the synthetic failed-payments dataset."""
    global _dataset_cache
    if _dataset_cache is None:
        if not DATA_CSV_PATH.exists():
            raise FileNotFoundError(
                f"Dataset not found at {DATA_CSV_PATH}. Run "
                "data/generate_dataset.py first."
            )
        _dataset_cache = pd.read_csv(DATA_CSV_PATH, dtype={"customer_id": str})
    return _dataset_cache


from flask import send_from_directory

@app.route("/audio/generated/<path:filename>")
def serve_audio(filename):
    return send_from_directory(PROJECT_ROOT / "audio" / "generated", filename)

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/csrf-token", methods=["GET"])
def api_csrf_token():
    """Issues a fresh CSRF token for clients that prefer a JSON fetch
    over reading the cookie set by `_attach_csrf_cookie`."""
    return jsonify({"csrf_token": generate_csrf()})


@app.route("/api/metrics", methods=["GET"])
def api_metrics():
    """Serves the precomputed batch-run portfolio metrics."""
    if not BATCH_RESULTS_PATH.exists():
        return jsonify({"error": "Batch results not found. Run core/simulator.py first."}), 404
    with open(BATCH_RESULTS_PATH, "r", encoding="utf-8") as f:
        metrics = json.load(f)
    return jsonify(metrics)
@app.route("/api/unresolved", methods=["GET"])
def api_unresolved():
    """Serves the unresolved/blocked records log for the dashboard table."""
    if not UNRESOLVED_LOG_PATH.exists():
        return jsonify({"error": "Unresolved log not found. Run core/simulator.py first."}), 404
    with open(UNRESOLVED_LOG_PATH, "r", encoding="utf-8") as f:
        unresolved_records = json.load(f)
    return jsonify(unresolved_records)
@app.route("/api/simulate", methods=["POST"])
@limiter.limit("5 per minute")
def api_simulate():
    """
    Runs one customer's failed-payment record through the full decision
    pipeline and returns every stage's output as a single JSON "journey".

    SECURITY NOTE: every field in this response that carries LLM-
    generated or customer-controlled text (notably `hinglish_message`)
    MUST be rendered on the frontend using strict text-escaping -- e.g.
    `element.textContent = ...` in JS, or Jinja's default auto-escaping
    with the `|safe` filter NEVER applied to it. Injecting this content
    as raw HTML would open a stored/reflected XSS path running from
    customer-controlled billing data straight into the dashboard
    operator's browser.
    """
    payload = request.get_json(silent=True) or {}
    customer_id = str(payload.get("customer_id", "")).strip()

    if not customer_id:
        return jsonify({"error": "customer_id is required."}), 400

    try:
        dataset = _load_dataset()
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 500

    matches = dataset[dataset["customer_id"] == customer_id]
    if matches.empty:
        return jsonify({"error": f"No record found for customer_id={customer_id!r}."}), 404

    record = matches.iloc[0].to_dict()
    record.setdefault("dispute_flag", False)

    classification = _classifier.classify(record)

    decision = _retry_engine.decide(
        classification=classification,
        payment_history=record.get("customer_payment_history", ""),
        prior_attempts=record.get("prior_attempts", 0),
        opt_out_flag=bool(record.get("opt_out_flag", False)),
    )

    verdict = _stopping_rules.evaluate(record=record, proposed_decision=decision)

    is_allowed = verdict.get("is_allowed")
    effective_action = decision["action"] if is_allowed else "blocked_by_stopping_rules"

    opted_out = bool(record.get("opt_out_flag", False))
    stage = None
    hinglish_message = None
    message_source = "not_generated"
    voice_filepath = None

    # Respect opt-out here too, same as the batch simulator -- an
    # operator manually re-running a customer through the dashboard must
    # never be able to message someone who has opted out.
    if not opted_out:
        stage = escalation_stage_for_attempts(record.get("prior_attempts", 0))

        raw_name = str(record.get("name") or record.get("customer_id") or "Customer")
        try:
            fallback_amount = f"{float(record.get('amount', 0)):,.2f}"
        except (TypeError, ValueError):
            fallback_amount = str(record.get("amount", 0))
        fallback_plan = str(record.get("subscription_plan", "your subscription"))
        fallback_text = render_message(
            stage=stage, name=raw_name, amount=fallback_amount, plan=fallback_plan
        )

        hinglish_message = _hinglish_agent.generate_message(record, stage=stage)
        message_source = "template_fallback" if hinglish_message == fallback_text else "ollama_llm"

        if is_allowed:
            voice_text = to_devanagari_for_voice(hinglish_message)
            voice_filepath = _voice_agent.generate_voice_message(
                text=voice_text, customer_id=customer_id
            )

    audit_entry = build_audit_entry(
        source="live_dashboard",
        record=record,
        classification=classification,
        decision=decision,
        verdict=verdict,
        effective_action=effective_action,
        escalation_stage=stage,
        message_sent=hinglish_message,
        message_source=message_source,
        voice_file=voice_filepath,
    )
    append_audit_entry(audit_entry)

    return jsonify(
        {
            "customer_id": customer_id,
            "classification": classification,
            "retry_decision": decision,
            "stopping_rules_verdict": verdict,
            "escalation_stage": stage,
            "hinglish_message": hinglish_message,
            "voice_file": voice_filepath,
        }
    )


@app.route("/api/audit-trail", methods=["GET"])
@limiter.limit("20 per minute")
def api_audit_trail():
    """
    Searches the persistent audit trail. Supports an optional
    ?customer_id= query filter (case-insensitive exact match) and an
    optional ?limit= cap on the number of results returned (most recent
    first). With no query params, returns the most recent entries across
    every customer.
    """
    customer_id = request.args.get("customer_id", "").strip() or None
    limit_param = request.args.get("limit", "").strip()
    limit = int(limit_param) if limit_param.isdigit() else 50

    entries = search_audit_trail(customer_id=customer_id, limit=limit)
    return jsonify({"count": len(entries), "entries": entries})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)




