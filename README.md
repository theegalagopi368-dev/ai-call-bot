# Maya — AI Voice Collections Agent for Kapture Finance

A state-controlled AI voice collections agent: **Vapi (STT/LLM/TTS)** → tool calls →
**FastAPI mock backend (SQLite)** → **Streamlit control panel**. Built so that the
one non-negotiable rule — *never disclose debt information before the customer is
verified* — is enforced in backend code, not just requested in the system prompt.

For the full design writeup (state machine, tool contracts, compliance mapping,
production recommendations) see **[`docs/HLD.md`](docs/HLD.md)** /
**[`docs/Kapture_Collections_Voicebot_HLD.pdf`](docs/Kapture_Collections_Voicebot_HLD.pdf)**.
For going from this repo to an actual phone call, see
**[`docs/VAPI_DEPLOYMENT.md`](docs/VAPI_DEPLOYMENT.md)**.

---

## Project Overview

Kapture Finance needs outbound calls made about overdue EMIs at a scale a human team
can't reach alone, without violating collections-compliance norms. **Maya** is the AI
voice agent that does this: it identifies itself, verifies the caller, discloses the
overdue EMI only after verification succeeds, determines intent, calls the matching
backend tool, and ends every call with exactly one recorded disposition.

The **primary demo account** (per the assignment) is:

| Field | Value |
|---|---|
| Customer | Rahul Sharma |
| Loan type | Personal Loan |
| Overdue EMI | ₹8,499 |
| Days past due | 12 |

## Architecture

```
Customer -> Phone/Telephony -> Vapi (STT -> LLM -> TTS) -> Maya
   -> FastAPI Backend (backend/main.py)
        ├── verify_customer        (hard verification gate)
        ├── log_promise_to_pay     (alias: log_ptp)
        ├── send_payment_link
        ├── edge_case              (dispute/hardship/wrong_person/hostile/
        │                           request_human/cease_contact/voicemail/
        │                           already_paid/callback_requested)
        └── mark_disposition       (final outcome — every call)
   -> SQLite (backend/collections.db)
   -> Streamlit Dashboard (dashboard/app.py) — Live Monitor + Call Simulator
```

See `docs/architecture.png` for the rendered diagram and `docs/HLD.md` §2–§4 for the
full breakdown of what's implemented vs. configured vs. mocked.

```
collections-agent/
├── backend/main.py                     FastAPI mock backend — source of truth
├── dashboard/app.py                    Streamlit monitor + call simulator
├── vapi_config/assistant_config.json   Vapi assistant template + tool schemas + prompt
├── scripts/render_vapi_config.py       Substitutes env vars into the Vapi template
├── docs/HLD.md, *.pdf, architecture.png, VAPI_DEPLOYMENT.md
├── .env.example
└── requirements.txt
```

## Setup

```bash
pip install -r requirements.txt

# terminal 1 — backend
cd backend && uvicorn main:app --reload --port 8000

# terminal 2 — dashboard
cd dashboard && BACKEND_URL=http://localhost:8000 streamlit run app.py
```

Open the Streamlit URL it prints. Use the **Call Simulator** tab to run a full call —
verification, EMI disclosure, negotiation, PTP, payment link, every edge case,
disposition — without touching Vapi. The **Live Monitor** tab shows exactly what a
real Vapi call would produce, because it reads the same backend through the same
`/api/*` routes.

## Environment Variables

See `.env.example` (copy to `.env`; never commit it — no secret is hardcoded
anywhere in source).

| Variable | Purpose |
|---|---|
| `BACKEND_URL` | Dashboard → backend, and used to render tool URLs into the Vapi config |
| `VAPI_VOICE_ID` | ElevenLabs voice ID for Maya (must be supplied — see below) |
| `VAPI_API_KEY` | Only needed to deploy the assistant via Vapi's API directly |
| `VAPI_ASSISTANT_ID` | Set after first deploy so re-deploys update, not duplicate |
| `VAPI_PHONE_NUMBER_ID` | Reference only — attach the number in the Vapi dashboard |

## Vapi Configuration

`vapi_config/assistant_config.json` is a **template** (`{{BACKEND_URL}}`,
`{{VOICE_ID}}` placeholders) — render it before using:

```bash
export BACKEND_URL=https://your-deployed-backend.example.com
export VAPI_VOICE_ID=your-elevenlabs-voice-id
python scripts/render_vapi_config.py
# -> vapi_config/assistant_config.rendered.json (gitignored)
```

**Testing against a real call without deploying anywhere:** use an ngrok tunnel
instead of a cloud deploy — `uvicorn main:app --reload --port 8000` in one terminal,
`ngrok http 8000` in another, then use the printed `https://....ngrok-free.app` as
`BACKEND_URL` above. Full walkthrough (including the "free-tier URLs rotate on
restart" gotcha) in `docs/VAPI_DEPLOYMENT.md` §1.

**Model / voice / transcriber choices, and why:**

| Component | Choice | Why |
|---|---|---|
| LLM | `claude-sonnet-4-6` (Anthropic), `temperature: 0.3` | Reliable tool-calling with a 5-tool schema, strong instruction-following for the compliance rules (never disclose pre-verification, never fabricate a tool result), low enough temperature to keep the EMI figure and dates precise instead of creatively paraphrased |
| Transcriber | Deepgram `nova-2`, `language: en`, `smartFormat: true` | Low-latency streaming STT with good accuracy on spoken names/dates/digits (DOB, last-4, amounts) over phone-quality audio; `smartFormat` normalizes spoken numbers, which matters for `verify_customer`'s exact-match DOB/last-4 check |
| Voice (TTS) | ElevenLabs, `voiceId` placeholder | Clear, natural speech for a debt-collection context where tone matters (calm, non-threatening); the actual voice ID must be chosen from your own ElevenLabs account/voice library — cannot be fabricated here |

**Manual values still required** (cannot be supplied by this repo — see
`docs/VAPI_DEPLOYMENT.md` for the full list): a deployed public `BACKEND_URL`, a real
`VAPI_VOICE_ID`, a `VAPI_API_KEY`, and a phone number assigned in the Vapi dashboard.

## Tools

| Tool | Purpose | Gate |
|---|---|---|
| `verify_customer` | Match caller's stated name/DOB/last-4 against the account on file | 2-attempt cap per call |
| `log_promise_to_pay` (alias `log_ptp`) | Record an agreed amount + due date | Requires `status='verified'`; rejects offers below the account's floor |
| `send_payment_link` | Generate a payment link | Requires `status='verified'` |
| `edge_case` | Log dispute / hardship / wrong_person / hostile / request_human / cease_contact / voicemail / already_paid / callback_requested | `callback_requested` requires both `callback_date` and `callback_time` |
| `mark_disposition` | Record the final outcome (12-value enum) — required on every call | Debt-related dispositions require the call to have completed verification |

Full request/response schemas: `docs/HLD.md` §6.

**Adding a customer.** The five tools above are what Maya calls mid-call — none of
them create accounts. To add a new mock loan account:
- **Dashboard:** Customers tab → "➕ Add a customer" form (name, DOB, last 4, phone,
  loan type, overdue EMI, days past due, settlement floor). There's a matching
  "🗑️ Remove a customer" expander below it.
- **Directly:** `POST {BACKEND_URL}/api/customers` with the same fields (see
  `CreateCustomerRequest` in `backend/main.py`), e.g.:
  ```bash
  curl -X POST $BACKEND_URL/api/customers -H "Content-Type: application/json" -d '{
    "full_name": "Sunita Iyer", "dob": "1992-03-10", "last4": "7788",
    "phone": "+919800011122", "loan_type": "Personal Loan",
    "overdue_amount_cents": 500000, "days_past_due": 8, "min_settlement_pct": 100
  }'
  ```
  Phone number must be unique (returns `409` otherwise) — it's the lookup key
  `verify_customer` matches against. This is an ops/admin action (`/api/customers`),
  not a Vapi tool — Maya never creates accounts mid-call.

## Authentication and State Enforcement

**The rule:** debt information is never disclosed before successful verification —
enforced in `backend/main.py`, not only in the prompt.

- `verify_customer`'s response only contains `loan_type` / `overdue_amount_cents` /
  `days_past_due` in the branch where the match succeeded — structurally, a failed or
  unverified call gets a response shape with no debt fields at all.
- `require_verified()` gates `log_promise_to_pay` and `send_payment_link`: **HTTP
  403** if `calls.status != 'verified'`.
- `mark_disposition` independently refuses to record `PROMISE_TO_PAY`,
  `PAYMENT_LINK_SENT`, `ALREADY_PAID`, `DISPUTE`, `HARDSHIP`, or `CALLBACK_REQUESTED`
  unless the call's `customer_id` shows verification actually happened — so even if
  the model hallucinated its way through the conversation, the backend won't let it
  log a debt-related outcome on a call that never verified.
- 2 failed verification attempts locks the call to `failed_verification`.
- A `cease_contact` flag persists on the customer record across future calls;
  `verify_customer` checks it and returns `verified_but_do_not_contact` instead of
  debt fields.

## Supported Intents

`WILL_PAY`, `HARDSHIP`, `DISPUTE`, `ALREADY_PAID`, `WRONG_PERSON`,
`CALLBACK_REQUESTED`, `DO_NOT_CALL`, `HOSTILE`, `REQUEST_HUMAN`, `NO_RESPONSE` — see
`docs/HLD.md` §5 for the full intent/entity table.

## Edge Cases

Already paid, dispute, hardship, wrong person, callback, DNC, hostile customer,
human request, voicemail, silence/no input, repeated verification failure — all
documented with exact handling in `docs/HLD.md` §9.

## Demo Scenarios

**1. Successful PTP (Rahul Sharma, the assignment's primary scenario):**
In the Call Simulator, pick "Rahul Sharma" → `verify_customer` → see the EMI
disclosed (₹8,499, Personal Loan, 12 days past due) → offer ₹8,499 due tomorrow →
`log_promise_to_pay` logs it → optionally `send_payment_link` → `mark_disposition`
→ `PROMISE_TO_PAY`.

**2. Recommended edge case — Already Paid:** Verify Rahul Sharma, see the EMI
disclosed, click "Already paid" → `edge_case(kind='already_paid')` → call flagged
`escalated` for payment verification, not confirmed outright → `mark_disposition` →
`ALREADY_PAID`.

**3. Also demoable in the simulator:** dispute, wrong person (use the "custom / wrong
answers" option in the caller picker), DNC (Devon Blake, pre-seeded with
`cease_contact=1`), hardship, hostile, callback (with date+time), request human, and
a below-floor negotiation offer (any non-Rahul account, which has a `<100%` floor).

## What Broke and How I Debugged It

**Problem:** Could not `pip install fastapi`/`pydantic` in this dev environment to
run the backend live and hit it with real HTTP requests.
**Root cause:** No outbound network access in this sandboxed environment (`pip
install` fails with "No matching distribution found").
**Fix:** Verified the schema, seeding, verification matching, floor calculation, and
the `mark_disposition` verification-gating logic with a standalone script that
exercises the same SQL directly (no FastAPI/pydantic import needed for that part of
the logic), plus `python -m py_compile` on both `backend/main.py` and
`dashboard/app.py` to catch syntax errors.
**Result:** Core business logic (verification matching, settlement floor math, the
verification-required-before-debt-disposition rule) is confirmed correct by direct
execution. The HTTP/FastAPI layer itself (request validation, status codes, route
wiring) is verified by careful code review rather than a live server — flagged
explicitly here rather than glossed over, and called out again in
`docs/VAPI_DEPLOYMENT.md` as something to re-check on first real deploy.

**Problem:** `pandoc ... --pdf-engine=pdflatex` failed with `! LaTeX Error: File
'lmodern.sty' not found.`
**Root cause:** The installed TeX Live is minimal and missing the `lmodern` font
package, with no network access to install it.
**Fix:** Switched the PDF pipeline to `pandoc → styled HTML → wkhtmltopdf`
(`wkhtmltopdf --enable-local-file-access`, styled with a small CSS file), which
doesn't need LaTeX at all and was already installed.
**Result:** `docs/Kapture_Collections_Voicebot_HLD.pdf` generates cleanly.

**Problem:** The first PDF render showed the document title as `Kapture Finance ???
Maya Collections Voicebot ??? High-Level Design` — the em dashes were mangled.
**Root cause:** The shell's locale was `POSIX` (no UTF-8), which corrupted the
multi-byte em-dash characters passed as a `--metadata title=...` command-line
argument specifically (body content from the `.md` file, read as a UTF-8 file, was
unaffected — only the CLI-argument path broke).
**Fix:** Used a plain hyphen in the CLI-passed title and forced `LANG=en_US.UTF-8
LC_ALL=C.UTF-8` on the pandoc/wkhtmltopdf invocations.
**Result:** Clean title rendering, confirmed by re-rendering and visually inspecting
the output page.

**Problem:** The first Graphviz architecture diagram rendered upside down relative to
the spec — Customer ended up at the *bottom* of the image instead of the top.
**Root cause:** An edge drawn from TTS back to Customer (to show "synthesized speech
returns to the caller") was influencing Graphviz's automatic rank ordering, since by
default every edge affects layout rank, including ones meant only to annotate a
return path.
**Fix:** Added `constraint=false` to that edge (and the `verify_customer -> LLM` tool
result annotation edge), so they render but don't affect the top-to-bottom rank
order.
**Result:** Diagram now reads top-to-bottom exactly as specified: Customer →
Telephony → Vapi → Maya → Backend tools → SQLite → Dashboard.

## Design Decisions

- **State-controlled, not prompt-controlled.** Every consequential action (disclosing
  the EMI, logging a PTP, sending a link, recording a disposition) is gated by
  backend code, not model judgement — a confused or adversarially-prompted model
  cannot talk its way past `require_verified()` or the settlement floor.
- **One `edge_case` tool with a `kind` enum**, rather than nine separate tools — keeps
  the tool surface small for the LLM to choose correctly between, while every kind is
  still independently logged and (where relevant) independently validated
  (`callback_requested` requires both a date and a time, checked server-side).
- **`mark_disposition` re-checks verification independently** of the tool-level gate,
  specifically because it's the *last* thing called on every path — it shouldn't
  trust that everything upstream went correctly.
- **Kept `log_ptp` working as an alias** for the newly-primary `log_promise_to_pay`
  name, rather than a breaking rename, per the "don't remove working functionality"
  constraint.
- **Amounts stored as integer paise**, not floats or rupee decimals, to avoid
  floating-point drift in a financial system; converted to `₹` display only in the UI
  layer (`dashboard/app.py`'s `inr()` helper).
- **SQLite, not an external DB:** keeps the whole stack runnable with zero external
  dependencies while still giving the dashboard and backend one shared source of
  truth, instead of in-memory state that would reset per process and break a
  conversation spanning multiple tool calls.

## Limitations

- No real telephony, STT/LLM/TTS execution, or payment gateway — all mocked or
  configured-but-unexecuted (see `docs/HLD.md`'s Implemented/Mocked labels
  throughout).
- `already_paid` can only ever escalate for human verification — this mock backend
  has no ledger to check against.
- Compliance rules like "no threats," "no fabricated legal consequences," and
  permitted calling hours are enforced by the system prompt only, not by backend
  code (unlike the verification gate, which is code-enforced) — see `docs/HLD.md` §8
  for the explicit list of which rules are backend-enforced vs. prompt-only.
- No latency, call-duration, or drop-rate instrumentation — those need Vapi's own
  call-lifecycle webhooks, not wired up here.
- No live phone call has been placed or verified from this environment (see
  `docs/VAPI_DEPLOYMENT.md` §10 for the specific things a real call would need to
  confirm, e.g. Vapi's actual caller-ID phone format matching the mock DB).

## What I Would Improve With More Time

- Wire up Vapi's call-lifecycle webhooks (`call.started`/`call.ended`) to populate
  duration, latency, and drop-rate metrics in the dashboard.
- Add phone-number normalization to `verify_customer` (currently an exact string
  match against `customers.phone`).
- Log tool *failures* (403/422s) as their own event kind, not just successful
  mutations, for a real tool-failure-rate metric.
- Add signed-webhook verification on `/tools/*` so the backend only accepts calls
  actually coming from Vapi.
- A small aggregation endpoint (or scheduled job) computing the observability metrics
  in `docs/HLD.md` §11 from the existing tables, instead of leaving them as
  "derivable but not pre-computed."

## Production Considerations

Real customer/account lookups instead of a 4-row mock table; a real payment gateway
(e.g., Razorpay/UPI) behind `send_payment_link` with webhook-confirmed payments (which
would also let `already_paid` be auto-verified instead of always escalating); signed
Vapi webhook verification; PII encryption at rest; authentication on the `/api/*`
read endpoints (currently open); regulatory guardrails (permitted calling hours,
consent/recording disclosure, RBI Fair Practices Code-equivalent rules) encoded as
backend checks rather than prompt text; tamper-evident audit logging. Full detail in
`docs/HLD.md` §13.
