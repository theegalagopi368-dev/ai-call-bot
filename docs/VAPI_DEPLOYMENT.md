# Vapi Deployment Guide — Maya (Kapture Finance Collections)

This document is the honest account of what's needed to take this repo from "works
in the Streamlit simulator" to "an actual phone call to Maya works." It says exactly
which parts are done, which parts need a manual step, and which parts **cannot be
verified from this development environment** (no network egress, no Vapi/ElevenLabs/
Deepgram credentials available here).

**No call has been placed from this environment.** Nothing below claims otherwise —
see §6.

---

## 1. Backend deployment URL

`backend/main.py` is a plain FastAPI app with no cloud-specific code. Locally it runs
on `http://localhost:8000`, which is **not reachable by Vapi** — Vapi's servers call
your tool URLs over the public internet, so a real call requires a **public HTTPS
URL** pointing at your backend. There are two ways to get one; pick based on what
you're doing.

### Option A — ngrok tunnel (fastest way to test a real call)

Good for testing/demoing against a real Vapi call without deploying anywhere. Free
ngrok URLs are temporary — expect to re-run this each session.

```bash
# terminal 1 — the backend, as usual
cd backend && uvicorn main:app --reload --port 8000

# terminal 2 — tunnel it
ngrok http 8000
```

ngrok prints a forwarding line like:

```
Forwarding    https://abc123.ngrok-free.app -> http://localhost:8000
```

That `https://abc123.ngrok-free.app` is your `BACKEND_URL`. Your five tool URLs
become:

```
https://abc123.ngrok-free.app/tools/verify_customer
https://abc123.ngrok-free.app/tools/log_promise_to_pay
https://abc123.ngrok-free.app/tools/send_payment_link
https://abc123.ngrok-free.app/tools/edge_case
https://abc123.ngrok-free.app/tools/mark_disposition
```

Use the same base URL for Vapi's Webhook/Server URL setting too, if your assistant
config or org-level Vapi settings need one.

Then render and deploy the assistant against that URL (see §2):

```bash
export BACKEND_URL=https://abc123.ngrok-free.app
export VAPI_VOICE_ID=your-elevenlabs-voice-id
python scripts/render_vapi_config.py
```

**Caveats specific to ngrok:**
- On the free tier, the URL **changes every time you restart `ngrok`** — you'll need
  to re-run `render_vapi_config.py` (and re-deploy, or re-paste into the Vapi
  dashboard) with the new URL each session. A paid ngrok plan can give you a fixed
  subdomain if this gets tedious.
- Keep both terminals (`uvicorn` and `ngrok`) running for the whole test session —
  closing either one breaks the tunnel and every tool call will fail.
- ngrok's local inspector (`http://127.0.0.1:4040` while `ngrok` is running) is a
  genuinely useful debugging tool here — it shows every incoming request from Vapi in
  real time, including the exact JSON body for each tool call, which is the fastest
  way to see what Vapi actually sent if `verify_customer` isn't matching a caller.
- Don't leave a demo tunnel open longer than you need it — anyone with the URL can
  hit your `/tools/*` and `/api/*` endpoints (see §9's troubleshooting note and
  `docs/HLD.md` §13 on the lack of webhook signature verification in this prototype).

### Option B — real deployment (for anything beyond ad-hoc testing)

Deploy `backend/` (e.g., `uvicorn backend.main:app --host 0.0.0.0 --port 8000`) to a
standard Python host with a public HTTPS URL — Render, Fly.io, Railway, a VM behind
a reverse proxy, etc. Use that URL as `BACKEND_URL` the same way as above. This is
the right choice once you want a stable URL that doesn't change between sessions, or
anything closer to how this would actually run in production.

**Manual step required either way:** a public HTTPS URL for the backend — this repo
doesn't provide or manage one itself.

## 2. Vapi assistant configuration

`vapi_config/assistant_config.json` is a **template**: it has `{{BACKEND_URL}}` and
`{{VOICE_ID}}` placeholders and is not valid to POST to Vapi as-is (it also has a
`note` field explaining this, which the render script strips).

Render it:

```bash
export BACKEND_URL=https://your-deployed-backend.example.com
export VAPI_VOICE_ID=your-elevenlabs-voice-id
python scripts/render_vapi_config.py
# -> writes vapi_config/assistant_config.rendered.json
```

Then either:
- **Paste it manually** into the Vapi dashboard (Assistants → Create Assistant →
  switch to JSON/advanced mode), or
- **Deploy via API**, if you also set `VAPI_API_KEY`:
  ```bash
  export VAPI_API_KEY=sk_...
  python scripts/render_vapi_config.py --deploy
  ```
  This POSTs to `https://api.vapi.ai/assistant`. Save the returned assistant `id` as
  `VAPI_ASSISTANT_ID` so a re-run PATCHes the same assistant instead of creating a
  duplicate.

**Manual step required:** an actual ElevenLabs voice ID and a Vapi API key —
neither can be supplied for you; see `.env.example`.

## 3. Required environment variables

All defined in `.env.example` (copy to `.env`, never commit `.env`):

| Variable | Used by | Required for |
|---|---|---|
| `BACKEND_URL` | `dashboard/app.py`, `scripts/render_vapi_config.py` | Dashboard pointing at the right backend; rendering tool URLs into the Vapi config |
| `VAPI_VOICE_ID` | `scripts/render_vapi_config.py` | Selecting Maya's voice |
| `VAPI_API_KEY` | `scripts/render_vapi_config.py --deploy` | Creating/updating the assistant via Vapi's API |
| `VAPI_ASSISTANT_ID` | `scripts/render_vapi_config.py --deploy` | Updating the same assistant on repeat deploys instead of creating new ones |
| `VAPI_PHONE_NUMBER_ID` | Not consumed by any script; reference only | Attaching the assistant to a phone number in the Vapi dashboard (Phone Numbers tab → assign assistant) |

## 4. Tool URLs

After rendering, each tool's `server.url` in `assistant_config.rendered.json` will be:

```
{BACKEND_URL}/tools/verify_customer
{BACKEND_URL}/tools/log_promise_to_pay
{BACKEND_URL}/tools/send_payment_link
{BACKEND_URL}/tools/edge_case
{BACKEND_URL}/tools/mark_disposition
```

Sanity-check they're reachable before attaching a phone number:

```bash
curl -s $BACKEND_URL/health
# -> {"status":"ok"}
```

## 5. How to place the test call

1. Confirm the assistant exists in the Vapi dashboard (created via §2) and shows the
   five tools above.
2. In the Vapi dashboard, go to **Phone Numbers**, either buy/import one or use a
   trial number, and assign it to this assistant (or use `VAPI_ASSISTANT_ID` with
   Vapi's outbound-call API to call a number directly — see Vapi's docs for the
   current outbound-call endpoint, since call-initiation APIs change over time and
   this repo doesn't wrap one).
3. Call the number, or trigger an outbound call to your own phone.
4. Answer, and go through the PTP scenario in §5 of `README.md` / Task 5 of the
   assignment: confirm you're Rahul Sharma, verify with DOB `1990-06-15` and last 4
   `4321`, hear the EMI disclosed, then say "I can pay tomorrow" / "₹8,499".

## 6. Expected successful PTP flow

Matches Task 5 of the assignment exactly (see `README.md` → Demo Scenarios for the
full transcript). What should happen technically, in order:

1. `POST /tools/verify_customer` → `{"result": "verified", "loan_type": "Personal Loan", "overdue_amount_cents": 849900, "days_past_due": 12, ...}`
2. Maya states the EMI amount and days past due out loud.
3. `POST /tools/log_promise_to_pay` with `amount_cents: 849900` → `{"result": "logged", "ptp_id": "..."}`
4. (Optional) `POST /tools/send_payment_link` → `{"result": "sent", "url": "https://pay.kapturefinance-mock.com/l/..."}`
5. `POST /tools/mark_disposition` with `disposition: "PROMISE_TO_PAY"` (or
   `"PAYMENT_LINK_SENT"` if step 4 ran) → `{"result": "recorded", ...}`
6. All five of the above appear as rows in the Streamlit dashboard's Live Monitor tab
   within a few seconds (it reads the same backend).

**This exact sequence has been verified logically and via the Streamlit Call
Simulator (which fires the same HTTP calls), but not yet via an actual placed Vapi
phone call from this environment** — see the callout at the top of this document.

## 7. Expected edge-case flow (ALREADY_PAID, recommended demo)

1. `verify_customer` succeeds as above.
2. Maya discloses the EMI.
3. Caller says "I already paid that."
4. `POST /tools/edge_case` with `kind: "already_paid"` → `{"result": "logged", "kind": "already_paid", "call_status": "escalated"}`
5. Maya acknowledges, does **not** argue or keep pressuring, and does **not** claim
   the payment is confirmed (nothing in this mock backend can confirm it).
6. `POST /tools/mark_disposition` with `disposition: "ALREADY_PAID"`.
7. Call ends politely; the call shows `status: escalated`, `disposition:
   ALREADY_PAID` in the dashboard, flagged for a human/ops payment-verification
   follow-up.

## 8. How recordings are accessed

**Not implemented by this repo.** Vapi records calls by default
(`recordingEnabled: true` is set in `vapi_config/assistant_config.json`) and exposes
the recording URL via its own dashboard / `GET /call/{id}` API response — this repo
does not fetch, store, or display recordings anywhere. To access a recording after a
real test call: open the call in the Vapi dashboard's Call Logs, or call Vapi's
`GET https://api.vapi.ai/call/{call_id}` with your `VAPI_API_KEY` and read the
`recordingUrl` field it returns.

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Vapi assistant creation fails / 401 | Bad or missing `VAPI_API_KEY` | Re-check the key in the Vapi dashboard's API Keys section |
| Tool calls never reach the backend (nothing shows in the dashboard during a real call) | `BACKEND_URL` still pointing at `localhost`, or backend not publicly deployed | Redeploy backend somewhere public; re-render and re-deploy the assistant config |
| Maya discloses nothing about the EMI even after correct answers | `verify_customer`'s `phone` doesn't match any seeded customer — Vapi's caller-ID field name/format may differ from what's assumed here | Check what phone format Vapi actually sends (may need a country-code normalization step in `verify_customer` — not currently implemented) and match it against `customers.phone` in `backend/main.py` |
| `log_promise_to_pay` always returns `"rejected"` | Offer below the account's floor (`min_settlement_pct`) | For Rahul Sharma the floor is 100% of ₹8,499 — the demo script requires offering the full amount |
| `edge_case(kind='callback_requested')` returns HTTP 422 | Missing `callback_date` or `callback_time` | Both are required together — see the tool schema in `vapi_config/assistant_config.json` |
| Dashboard shows nothing | `BACKEND_URL` env var not set for `dashboard/app.py`, or backend not running | `export BACKEND_URL=...` before `streamlit run dashboard/app.py`, confirm `/health` responds |
| Voice sounds wrong / default voice used | `VAPI_VOICE_ID` not set before rendering, or an invalid ID | Confirm the voice ID exists in your ElevenLabs account and was set before running `render_vapi_config.py` |
| Tool calls worked earlier in the session but suddenly all fail | ngrok free-tier URL rotated (e.g. `uvicorn`/`ngrok` was restarted) | Get the new `https://....ngrok-free.app` URL, re-run `render_vapi_config.py` with it, and re-deploy/re-paste the assistant config — the old URL is dead the moment the tunnel restarts |
| Need to see exactly what Vapi is sending to a tool | — | With `ngrok` running, open `http://127.0.0.1:4040` locally — it logs every request/response, including the full JSON body, which is the fastest way to debug a mismatch (e.g. an unexpected phone number format) |

## 10. What still requires an actual real call

Everything in §5–§6 above describes the *expected* flow based on the tool contracts
and the system prompt — it has not been confirmed against a live Vapi call from this
environment (no outbound network access here to reach Vapi/Deepgram/ElevenLabs, and
no live credentials configured). Specifically unverified:

- Real STT accuracy on spoken DOB / last-4 digits (numbers are one of the harder STT
  cases — e.g. "one nine nine zero" vs. "nineteen ninety").
- Real end-to-end turn latency against the §12 budget in `docs/HLD.md`.
- Whether Vapi's actual caller-ID phone format matches `customers.phone` in the mock
  DB (`+919876543210`) without normalization.
- Silence/voicemail detection behavior against Vapi's real `silenceTimeoutSeconds`
  setting.
- Recording retrieval (§8 above).

Whoever runs the real test call should re-check each of these against the actual
transcript and correct `backend/main.py` or the system prompt if they don't match
(e.g., add phone-number normalization to `verify_customer` if needed).
