# Security Overview — Revenue Recovery Agent

This document describes the security controls implemented in this hackathon
build, and honestly scopes the gap between this demo and an enterprise-grade,
production deployment handling real payment and PII data.

---

## Section 1: Implemented Controls

### 1.1 Idempotency & Duplicate-Charge Defense (`StoppingRules`)
`StoppingRules` is the final veto layer that runs after `RetryEngine`
proposes a business decision. It provides the system's primary
defense-in-depth against duplicate side effects:

- **Minimum cooldown enforcement**: `retry_now` is only valid on a payment's
  first touch (`prior_attempts == 0`). If anything upstream — a bug, a race
  condition, a redelivered webhook — proposes an immediate retry on a
  payment that has already been attempted, it is blocked outright.
- **Hard retry cap**: `RetryEngine` refuses to propose any retry once
  `prior_attempts >= 3`, bounding the total number of gateway charge
  attempts per payment regardless of how many times upstream logic is
  (correctly or incorrectly) invoked.
- **Active-dispute veto**: any payment with `dispute_flag = True` is blocked
  from all automated retry action, independent of classification or
  confidence — disputes are a compliance matter, not a revenue decision.
- **Overnight blackout window**: no retries are permitted between 10:00 PM
  and 8:00 AM, preventing customer-facing charge attempts and notifications
  during low-traffic hours associated with elevated bank fraud-system
  sensitivity.

Together, these mean the number of real gateway charge attempts — and
therefore customer-facing messages — for a single payment is strictly
bounded and idempotent, even under duplicate/at-least-once delivery from
upstream systems.

### 1.2 Prompt Injection Defense (`HinglishAgent`)
`HinglishAgent` enriches a message via a locally-hosted Ollama LLM, but the
`name` and `subscription_plan` fields it interpolates originate from
customer-controlled billing data. Defenses:

- **Character-level sanitization**: `_sanitize_input()` strips characters
  commonly used to fake role/markup delimiters or escape the prompt's own
  structure (`< > { } [ ] \ /`) before any customer-controlled value is
  interpolated into the LLM prompt.
- **Explicit non-authority instructions**: the prompt itself contains a
  STRICT RULES section instructing the model to treat name/amount/plan
  strictly as literal data, never as commands, regardless of their content.
- **Zero elevated trust in model output**: the LLM's response is never
  executed, evaluated, or treated as anything other than plain display
  text — it is rendered as a message string, nothing more.
- **Fail-closed to a deterministic template**: any failure mode (Ollama
  down, timeout, malformed response, empty response) falls back to the
  hardcoded, legally-reviewed template in `messaging/templates.py` rather
  than raising or blocking the pipeline.

### 1.3 Command Injection Defense (`VoiceAgent`)
Text passed to the local `piper` TTS binary is customer/LLM-derived. Rather
than building a shell string (`echo "{text}" | piper ...`), `VoiceAgent`
invokes `piper` as a list-form subprocess argv (`shell=False`) and feeds
text via `stdin` using `input=`. No shell ever parses the message text, so
shell-metacharacter injection is not a viable attack surface. `customer_id`
is separately reduced to an alphanumeric/hyphen/underscore token before
being used to build an output file path, preventing path traversal.

### 1.4 Rate Limiting (`Flask-Limiter`)
The `/api/simulate` endpoint is rate-limited to **5 requests per minute per
client IP**. This protects the demo from spam/abuse that would otherwise
hammer the local Ollama and piper processes, both of which are
CPU/latency-sensitive and not designed for high concurrency.

### 1.5 CSRF Protection (`Flask-WTF`)
The application is wrapped in `CSRFProtect`, and a token is issued both via
an `/api/csrf-token` endpoint and a same-site cookie, to be echoed back via
the `X-CSRFToken` header on state-changing POST requests — preventing a
malicious third-party page from triggering simulations on a logged-in
operator's session.

### 1.6 Configuration Hygiene
All runtime configuration (Flask secret key, Ollama endpoint/model, rate
limiter storage backend, port) is loaded from a local `.env` file via
`python-dotenv`. The app refuses to start if `FLASK_SECRET_KEY` is unset,
rather than silently falling back to an insecure default.

### 1.7 Explicit Debug Mode
`app.run()` explicitly sets `debug=False`. Flask's debug mode exposes the
Werkzeug interactive debugger, which permits arbitrary remote code
execution if reachable — this is never acceptable outside a developer's own
local machine, and is never toggled by environment or accident here.

### 1.8 Output Escaping (XSS Defense)
Every field returned by `/api/simulate` that may carry LLM-generated or
customer-controlled text (notably `hinglish_message`) is documented as
requiring strict text-escaping on render (e.g. `textContent`, or Jinja's
default auto-escaping — never `|safe`). This closes the path from
customer-controlled billing data, through the LLM, to a dashboard
operator's browser.

---

## Section 2: Path to Production

This build is a hackathon demonstration of the decision-engine logic and
its safety layers. The following gaps would need to be closed before this
system could responsibly touch real customer payment data:

### 2.1 Authentication & Authorization (AuthN/AuthZ)
There is currently no login, session, or role model — anyone who can reach
the dashboard can trigger simulations and view customer data. Production
requires: SSO/OIDC-based operator authentication, role-based access control
(e.g. support agents vs. finance vs. admin), and per-endpoint authorization
checks rather than a flat, open surface.

### 2.2 Secrets Management (KMS)
Secrets (Flask secret key, database credentials, payment-gateway API keys)
currently live in a local `.env` file. Production requires a managed KMS
or secrets manager (e.g. AWS KMS/Secrets Manager, HashiCorp Vault, GCP
Secret Manager) with automated rotation, audit-logged access, and no
secret material ever committed to disk or version control.

### 2.3 Encryption at Rest for PII & Payment Data
The synthetic dataset here is plaintext CSV/JSON on local disk. Production
requires field-level or full-disk encryption at rest for any customer PII
(names, contact info) and payment metadata, with encryption keys managed
separately from the data (envelope encryption via the KMS above), plus
encryption in transit (TLS) for every internal and external hop.

### 2.4 Immutable Audit Logging
**Partially implemented.** Every processed record's full decision journey —
classification, proposed action, stopping-rules verdict, final action, and
outbound message — is persisted to `results/audit_trail.jsonl`, an
append-only log written one line per entry (never rewritten or truncated
on subsequent runs), queryable by `customer_id` via `core/audit_trail.py`
and the dashboard's Audit Trail panel. This satisfies the append-only and
"who/what/when/why" requirements above for a single-instance deployment.

What's still missing for production: true tamper-evidence (a write-once
log store or hash-chained ledger, so a compromised process couldn't edit
history after the fact — a JSONL file on local disk has no such
guarantee), retention-policy enforcement per regulatory requirement, and
centralized/durable storage (this currently lives on local disk, not a
managed log store with its own backup and access-control guarantees).

### 2.5 India DPDP Act Compliance
Handling Indian customers' financial and personal data means aligning with
the Digital Personal Data Protection Act, 2023, including: documented
lawful basis and purpose limitation for processing payment-failure data,
a consent/notice mechanism (distinct from the existing `opt_out_flag`,
which only covers retry messaging, not data processing broadly), data
localization/residency review, data-principal rights handling (access,
correction, erasure requests), breach-notification procedures, and a
Data Protection Impact Assessment for the automated decision-making this
system performs — since RetryEngine's decisions materially affect how and
when customers are contacted and charged.

### 2.6 Additional Hardening Not Yet Addressed
- Network-level isolation between the Flask app, the Ollama LLM sidecar,
  and any real payment-gateway integration (no such integration exists yet
  — this demo only models expected recovery, it never calls a real gateway).
- Structured input validation (e.g. `pydantic`/`marshmallow` schemas) on
  all API request bodies, beyond the current ad-hoc `.get()` checks.
- A production-grade WSGI server (gunicorn/uWSGI behind nginx) instead of
  Flask's built-in development server, which this demo does not yet run
  behind.
- Dependency and container vulnerability scanning as part of CI/CD.
