# Running the Tests

Install dependencies (`pytest` is already in `requirements.txt`), then from the project root run:

```bash
pytest tests/
```

For verbose per-assertion output during judging demos, use `pytest tests/ -v`.

---

## Why These Tests Matter

An AI agent that decides *whether and when to charge a customer's card again* is not a
recommendation engine — it's a system with real financial, legal, and reputational
consequences if it gets the guardrails wrong. These 10 tests exist to prove three specific
properties to a judge, a compliance reviewer, or a future engineer inheriting this code:

**1. Idempotency.** `test_minimum_cooldown_blocks_retry_now_after_prior_attempt` and
`test_retry_now_only_works_when_prior_attempts_is_zero` prove that a duplicate webhook,
a race condition, or a retried request can never cause two charge attempts to fire in
quick succession against the same payment method. Combined with the hard retry cap
(`test_hard_cap_blocks_retries_regardless_of_reason`), the total number of real gateway
attempts per payment is strictly bounded — no matter how many times upstream logic
misfires.

**2. Anti-Abuse.** The blackout-window test (`test_blackout_window_blocks_any_retry`)
proves the system will never fire a charge attempt or customer notification between
10 PM and 8 AM, protecting customers from being hit by automated financial actions
overnight and reducing exposure to elevated bank fraud-system sensitivity during
low-traffic hours.

**3. Legal Compliance.** `test_active_dispute_always_blocks_retry` and
`test_opt_out_always_results_in_do_not_retry` prove that an active payment dispute or
a customer's explicit opt-out **always** overrides every other business rule — including
a highly favorable classification or perfect timing. These aren't revenue decisions;
they're compliance and consent boundaries, and the tests demonstrate the engine treats
them as absolute, not as inputs to a scoring function.

Each test isolates a single rule and asserts it directly against `RetryEngine` or
`StoppingRules` — no dataset, no Flask app, no LLM — so a judge can read any one test
and see exactly which guarantee it proves, with nothing else in the way.