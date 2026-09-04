# 🚀 Revenue Recovery Agent

An intelligent, secure, and localized AI engine designed to autonomously recover failed payments. Built for the Razorpay AI Buildathon.

**Author:** [The-CodeTaker](https://github.com/The-CodeTaker)

---

## 📌 Overview
The Revenue Recovery Agent intercepts failed payment webhooks and determines the optimal recovery strategy. Instead of relying on static, rules-based retries, this engine uses a multi-stage pipeline to classify failures, propose business decisions, enforce strict compliance guardrails, and generate localized, hyper-personalized outreach (Text and Voice) to win back the customer.

## 🏗️ Architecture Pipeline

1. **Failure Classifier:** Analyzes error codes and customer history to categorize the failure (e.g., Insufficient Funds, Hard Decline, Technical Error).
2. **Retry Engine:** Calculates recovery probability and proposes an action (Retry, Customer Outreach, or Do Not Retry).
3. **Stopping Rules (Guardrails):** A strict idempotency and compliance layer. Blocks duplicate charges, respects active disputes, and enforces blackout windows.
4. **Hinglish AI Agent:** Uses a locally-hosted LLM (Llama 3.1 via Ollama) to generate hyper-personalized, context-aware recovery messages in Hinglish.
5. **Voice Agent (Piper TTS):** Synthesizes the generated text into a localized audio file (`.wav`) for IVR/Voice-callback integration.
6. **Audit Trail:** Appends the full journey to `results/audit_trail.jsonl` and exposes it via an operator dashboard panel.

## 🛡️ Security & Compliance First
Enterprise-grade security was built into this architecture from Day 1. Please see [SECURITY.md](SECURITY.md) for a detailed breakdown of our controls.
- **Idempotency & Duplicate Prevention:** Hard limits on retry attempts and cooldown windows.
- **Prompt Injection Defense:** Strict sanitization of customer-controlled data before LLM interpolation.
- **Command Injection Defense:** Safe subprocess array execution for TTS rendering; no shell parsing.
- **Dashboard Security:** Flask-Limiter for rate-limiting, Flask-WTF for CSRF protection, and strict `textContent` output escaping for XSS prevention.
- **Zero-Vulnerability Dependency Stack:** Verified via `pip-audit` (August 25, 2026).

## 💻 Tech Stack
* **Backend:** Python 3, Flask, Pandas
* **AI / LLM:** Ollama (Llama 3.1) - *Running 100% locally for data privacy*
* **Voice TTS:** Piper TTS
* **Frontend:** HTML5, CSS3, Vanilla JS, Chart.js

## 🚀 How to Run Locally

### 1. Clone and set up the environment
```bash
git clone https://github.com/The-CodeTaker/revenue-recovery-agent.git
cd revenue-recovery-agent
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables
```bash
copy .env.example .env
```
Open `.env` and set `FLASK_SECRET_KEY` to any long random string (the app
refuses to start without one — see `SECURITY.md` §1.6). You can generate one
with:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
The other variables (`OLLAMA_ENDPOINT`, `OLLAMA_MODEL`, `PORT`,
`RATE_LIMIT_STORAGE_URI`) already have working defaults for a local demo.

### 3. Install the local AI dependencies

**Ollama (Hinglish message polishing)**
1. Install Ollama from [ollama.com](https://ollama.com).
2. Pull the model this project uses:
   ```bash
   ollama pull llama3.1
   ```
3. Make sure `ollama serve` is running in the background before you start
   the dashboard. If Ollama isn't reachable, `HinglishAgent` automatically
   falls back to the hardcoded templates in `messaging/templates.py` — the
   app still works, you just won't see the LLM-polished messages.

**Piper (Hindi voice synthesis)**
1. Download the Windows Piper binary from the
   [official releases page](https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip)
   and extract it into `bin/piper/` (so `bin/piper/piper.exe` exists).
2. Download the voice model files from
   [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/hi/hi_IN)
   and place both the `.onnx` and matching `.onnx.json` into `models/`.
   *(Note: `voice_agent.py` currently defaults to loading
   `hi_IN-priyamvada-medium.onnx`, so grab that voice — not `rohan` — unless
   you update `DEFAULT_PIPER_MODEL` to match.)*
3. If either file is missing, `VoiceAgent` logs a warning and simply skips
   voice synthesis — text messages still generate normally.

### 4. Generate the dataset and run the batch simulation
```bash
python data/generate_dataset.py
python core/simulator.py
```
This produces `results/batch_run_results.json` and
`results/unresolved_log.json`, which the dashboard's metrics panel and
"Unresolved / Not Retried" table read from. Re-running `core/simulator.py`
appends fresh entries to `results/audit_trail.jsonl` rather than overwriting it.

### 5. Start the dashboard
```bash
python dashboard/app.py
```
Then open **http://127.0.0.1:5000** in your browser.

### 6. (Optional) Run the test suite
```bash
pytest tests/ -v
```
See `tests/README_TESTS.md` for what each test proves and why.

