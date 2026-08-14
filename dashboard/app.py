"""
Streamlit control panel for Kapture Finance's Maya collections agent.

This app talks to the FastAPI backend over HTTP (BACKEND_URL below) — the
exact same endpoints Vapi's tool-calling hits. That means this dashboard
does two jobs with one codebase:

1. MONITOR — while real Vapi calls are happening, the "Live Monitor" tab
   polls /api/calls, /api/ptps, /api/payment_links, /api/events and shows
   you what the agent has actually done, in real time.

2. SIMULATOR — the "Call Simulator" tab lets you role-play the caller side
   of a conversation and fire the exact same tool calls Vapi would fire
   (verify_customer, log_promise_to_pay, send_payment_link, edge_case,
   mark_disposition), so you can demo a full call — including the
   already-paid and callback flows — and every edge case without dialing
   a phone.

Run with:  streamlit run app.py
Set BACKEND_URL as an env var if the backend isn't on localhost:8000.
"""

import os
import time
import datetime
import requests
import streamlit as st
import pandas as pd

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

st.set_page_config(page_title="Maya — Kapture Finance Collections", page_icon="📞", layout="wide")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def api_get(path, params=None):
    try:
        r = requests.get(f"{BACKEND_URL}{path}", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Backend GET {path} failed: {e}")
        return []


def api_post(path, payload):
    try:
        r = requests.post(f"{BACKEND_URL}{path}", json=payload, timeout=5)
        try:
            body = r.json()
        except Exception:
            body = {"error": r.text}
        return r.status_code, body
    except Exception as e:
        return 0, {"error": str(e)}


def api_post_delete(path):
    try:
        r = requests.delete(f"{BACKEND_URL}{path}", timeout=5)
        try:
            body = r.json()
        except Exception:
            body = {"error": r.text}
        return r.status_code, body
    except Exception as e:
        return 0, {"error": str(e)}


def inr(cents):
    """Format paise as INR, e.g. 849900 -> '₹8,499.00'."""
    if cents is None:
        return "—"
    return f"₹{cents/100:,.2f}"


STATE_ORDER = ["in_progress", "verified", "failed_verification", "escalated", "closed"]
STATE_COLOR = {
    "in_progress": "🟡",
    "verified": "🟢",
    "failed_verification": "🔴",
    "escalated": "🟠",
    "closed": "⚪",
}

DISPOSITIONS = [
    "PROMISE_TO_PAY", "PAYMENT_LINK_SENT", "ALREADY_PAID", "DISPUTE", "HARDSHIP",
    "CALLBACK_REQUESTED", "DO_NOT_CALL", "WRONG_PERSON", "ESCALATED",
    "NO_RESPONSE", "FAILED_VERIFICATION", "OTHER",
]


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("📞 Maya — Kapture Finance")
    st.caption("State-controlled AI voice collections agent — control panel")
    st.text_input("Backend URL", value=BACKEND_URL, key="backend_url_display", disabled=True)
    if st.button("🔄 Refresh now"):
        st.rerun()
    auto = st.checkbox("Auto-refresh every 4s (Live Monitor tab)", value=False)
    st.divider()
    st.markdown(
        "**Architecture**\n\n"
        "Customer ↔ Vapi (STT/LLM/TTS) → Maya → tool call →\n"
        "this FastAPI backend → SQLite\n\n"
        "This dashboard reads the same DB via the backend's `/api/*` "
        "routes, and can also *write* to it via `/tools/*` to simulate "
        "a call locally — including without Vapi."
    )

tab_monitor, tab_sim, tab_customers, tab_about = st.tabs(
    ["📊 Live Monitor", "🧪 Call Simulator", "👤 Customers", "ℹ️ Design Notes"]
)


# ---------------------------------------------------------------------------
# TAB 1 — Live monitor
# ---------------------------------------------------------------------------

with tab_monitor:
    st.subheader("Calls")
    calls = api_get("/api/calls")
    if calls:
        df_calls = pd.DataFrame(calls)
        df_calls["status"] = df_calls["status"].apply(lambda s: f"{STATE_COLOR.get(s,'')} {s}")
        cols = ["call_id", "customer_id", "status", "disposition", "verification_attempts",
                "callback_date", "callback_time", "created_at"]
        cols = [c for c in cols if c in df_calls.columns]
        st.dataframe(df_calls[cols], use_container_width=True, hide_index=True)
    else:
        st.info("No calls yet. Use the Call Simulator tab to generate one.")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Promises to Pay")
        ptps = api_get("/api/ptps")
        if ptps:
            df = pd.DataFrame(ptps)
            df["amount"] = df["amount_cents"].apply(inr)
            st.dataframe(df[["call_id", "customer_id", "amount", "due_date", "method", "created_at"]],
                         use_container_width=True, hide_index=True)
            st.metric("Total promised", inr(df["amount_cents"].sum()))
        else:
            st.caption("None logged yet.")

    with col2:
        st.subheader("Payment Links")
        links = api_get("/api/payment_links")
        if links:
            df = pd.DataFrame(links)
            df["amount"] = df["amount_cents"].apply(inr)
            st.dataframe(df[["call_id", "customer_id", "amount", "url", "created_at"]],
                         use_container_width=True, hide_index=True)
        else:
            st.caption("None sent yet.")

    st.subheader("Event log (most recent 200)")
    events = api_get("/api/events")
    if events:
        df = pd.DataFrame(events)
        st.dataframe(df[["created_at", "call_id", "kind", "detail"]], use_container_width=True, hide_index=True)
    else:
        st.caption("No events yet.")

    if auto:
        time.sleep(4)
        st.rerun()


# ---------------------------------------------------------------------------
# TAB 2 — Call simulator
# ---------------------------------------------------------------------------

with tab_sim:
    st.subheader("Simulate a call")
    st.caption(
        "This fires the exact tool endpoints Vapi calls during a real conversation, "
        "so you can demo the PTP flow, already-paid, callback, and every other edge "
        "case without dialing a phone."
    )

    def _fresh_sim_state():
        return {
            "verified": False, "customer_id": None, "loan_type": None,
            "overdue_amount_cents": None, "days_past_due": None, "floor_cents": None,
        }

    def _start_new_sim_call():
        # Runs as an on_click callback, which fires BEFORE the script body
        # re-executes and the text_input(key="sim_call_id") widget below is
        # instantiated for this run — so it's safe to write session_state
        # here. Doing this assignment inside the button's `if st.button(...)`
        # block instead (i.e. after the widget already rendered this run)
        # is what raises StreamlitAPIException: cannot modify session_state
        # for a key that already has an instantiated widget this run.
        st.session_state.sim_call_id = f"sim_{int(time.time())}"
        st.session_state.sim_state = _fresh_sim_state()

    if "sim_call_id" not in st.session_state:
        st.session_state.sim_call_id = f"sim_{int(time.time())}"
    if "sim_state" not in st.session_state:
        st.session_state.sim_state = _fresh_sim_state()

    colA, colB = st.columns([2, 1])
    with colA:
        st.text_input("Call ID (simulates Vapi's call.id)", key="sim_call_id")
    with colB:
        st.button("🆕 Start new simulated call", on_click=_start_new_sim_call)

    call_id = st.session_state.sim_call_id
    state = st.session_state.sim_state

    st.markdown("#### Step 1 — Verify identity")
    st.caption("No debt/account details are shown anywhere below until this step returns 'verified'.")
    customers = api_get("/api/customers")
    phone_options = {f"{c['full_name']} ({c['phone']})": c for c in customers} if customers else {}
    default_label = next((k for k in phone_options if "Rahul Sharma" in k), None)
    options = ["-- custom / wrong answers --"] + list(phone_options.keys())
    picked_label = st.selectbox(
        "Pretend caller is...", options=options,
        index=options.index(default_label) if default_label else 0,
    )

    if picked_label != "-- custom / wrong answers --":
        picked = phone_options[picked_label]
        default_phone, default_name, default_dob, default_last4 = picked["phone"], picked["full_name"], picked["dob"], picked["last4"]
    else:
        default_phone, default_name, default_dob, default_last4 = "+919800000099", "Someone Wrong", "2000-01-01", "0000"

    v1, v2, v3, v4 = st.columns(4)
    phone = v1.text_input("Phone (from caller ID)", value=default_phone)
    full_name = v2.text_input("Stated full name", value=default_name)
    dob = v3.text_input("Stated DOB (YYYY-MM-DD)", value=default_dob)
    last4 = v4.text_input("Stated last 4 (loan a/c)", value=default_last4)

    if st.button("▶️ Call verify_customer"):
        status, body = api_post("/tools/verify_customer", {
            "call_id": call_id, "phone": phone, "full_name": full_name, "dob": dob, "last4": last4,
        })
        st.json(body)
        if body.get("result") == "verified":
            state.update(
                verified=True, customer_id=body["customer_id"], loan_type=body["loan_type"],
                overdue_amount_cents=body["overdue_amount_cents"], days_past_due=body["days_past_due"],
                floor_cents=body["min_settlement_cents"],
            )
            st.session_state.sim_state = state
            st.success(
                f"Verified. {body['loan_type']} overdue EMI {inr(body['overdue_amount_cents'])}, "
                f"{body['days_past_due']} days past due. (Internal floor {inr(body['min_settlement_cents'])} — never shown to caller.)"
            )
        elif body.get("result") == "verified_but_do_not_contact":
            st.warning("Verified, but this account is Do-Not-Call. Agent must end the call without discussing debt, and record disposition DO_NOT_CALL.")
        elif body.get("result") == "failed_max_attempts":
            st.error("Max verification attempts reached — agent must not disclose any account info. Record disposition FAILED_VERIFICATION.")

    st.divider()
    st.markdown("#### Step 2 — Disclose EMI, negotiate & log a promise-to-pay")
    if not state["verified"]:
        st.info("Verify the caller above first — the backend will reject this otherwise (matches real tool behavior: HTTP 403).")
    else:
        st.write(f"**{state['loan_type']}** overdue EMI on file: **{inr(state['overdue_amount_cents'])}** "
                 f"({state['days_past_due']} days past due)")
        n1, n2, n3 = st.columns(3)
        offer = n1.number_input("Caller's offer (₹)", min_value=0.0, value=state["overdue_amount_cents"]/100, step=100.0)
        due = n2.date_input("Agreed due date", value=datetime.date.today() + datetime.timedelta(days=1))
        method = n3.selectbox("Method", ["upi", "debit_card", "bank_transfer", "netbanking", "other"])
        if st.button("▶️ Call log_promise_to_pay"):
            status, body = api_post("/tools/log_promise_to_pay", {
                "call_id": call_id, "amount_cents": int(offer * 100),
                "due_date": due.isoformat(), "method": method,
            })
            st.json(body)
            if body.get("result") == "rejected":
                st.error(f"Backend rejected the offer — below approved minimum of {inr(body['min_settlement_cents'])}. Agent should ask for more.")
            elif body.get("result") == "logged":
                st.success("PTP logged. Next: optionally send a payment link, then mark_disposition(PROMISE_TO_PAY).")

        st.markdown("#### Step 3 — Send payment link (optional)")
        link_amt = st.number_input("Amount to charge now (₹)", min_value=0.0, value=state["overdue_amount_cents"]/100, step=100.0, key="link_amt")
        if st.button("▶️ Call send_payment_link"):
            status, body = api_post("/tools/send_payment_link", {"call_id": call_id, "amount_cents": int(link_amt * 100)})
            st.json(body)
            if body.get("result") == "sent":
                st.success(f"Link: {body['url']}")

    st.divider()
    st.markdown("#### Edge cases")
    st.caption("Fire these at any point in the simulated call to see how the backend and agent instructions respond.")
    ec1, ec2, ec3, ec4 = st.columns(4)
    ec5, ec6, ec7, ec8 = st.columns(4)

    def edge_button(col, kind, label, note="", extra_payload=None):
        if col.button(label):
            payload = {"call_id": call_id, "kind": kind, "note": note}
            if extra_payload:
                payload.update(extra_payload)
            status, body = api_post("/tools/edge_case", payload)
            st.json(body)

    edge_button(ec1, "dispute_debt", "⚖️ Dispute debt", "Caller says debt isn't theirs")
    edge_button(ec2, "hardship", "💔 Hardship claim", "Caller cites job loss")
    edge_button(ec3, "wrong_person", "🙅 Wrong person", "Wrong number reached")
    edge_button(ec4, "hostile", "😠 Hostile caller", "Caller is abusive")
    edge_button(ec5, "request_human", "🧑‍💼 Request human", "Caller wants a person")
    edge_button(ec6, "cease_contact", "🛑 Do-Not-Call", "Caller invokes do-not-call")
    edge_button(ec7, "voicemail", "📼 Hit voicemail", "No answer, VM reached")
    if ec8.button("💳 Already paid"):
        status, body = api_post("/tools/edge_case", {
            "call_id": call_id, "kind": "already_paid",
            "note": "Caller claims the EMI was already paid",
        })
        st.json(body)
        st.info("Backend flags this for payment-verification follow-up (status→escalated). Do NOT tell the caller the payment is confirmed — only that the backend has confirmed it. Then call mark_disposition(ALREADY_PAID).")

    st.markdown("##### 📅 Callback requested")
    cb1, cb2, cb3 = st.columns(3)
    cb_date = cb1.date_input("Callback date", value=datetime.date.today() + datetime.timedelta(days=1), key="cb_date")
    cb_time = cb2.text_input("Callback time (HH:MM)", value="18:00", key="cb_time")
    if cb3.button("▶️ Call edge_case(callback_requested)"):
        status, body = api_post("/tools/edge_case", {
            "call_id": call_id, "kind": "callback_requested",
            "callback_date": cb_date.isoformat(), "callback_time": cb_time,
            "note": "Caller asked to be called back",
        })
        st.json(body)

    st.divider()
    st.markdown("#### Step 4 — mark_disposition (every call must end with exactly one)")
    d1, d2 = st.columns([2, 1])
    disposition = d1.selectbox("Final disposition", DISPOSITIONS)
    if d2.button("▶️ Call mark_disposition"):
        status, body = api_post("/tools/mark_disposition", {"call_id": call_id, "disposition": disposition})
        st.json(body)
        if status == 403:
            st.error("Blocked — this disposition requires the call to have completed verification first.")
        else:
            st.success(f"Disposition recorded: {disposition}. Call closed.")


# ---------------------------------------------------------------------------
# TAB 3 — Customers
# ---------------------------------------------------------------------------

with tab_customers:
    st.subheader("Mock loan accounts")
    customers = api_get("/api/customers")
    if customers:
        df = pd.DataFrame(customers)
        df["overdue_emi"] = df["overdue_amount_cents"].apply(inr)
        df["min_acceptable"] = df.apply(lambda r: inr(int(r["overdue_amount_cents"] * r["min_settlement_pct"] / 100)), axis=1)
        df["do_not_call"] = df["cease_contact"].map({0: "No", 1: "Yes"})
        st.dataframe(
            df[["customer_id", "full_name", "phone", "loan_type", "overdue_emi", "days_past_due",
                "min_settlement_pct", "min_acceptable", "do_not_call"]],
            use_container_width=True, hide_index=True,
        )
        st.caption(
            "`min_settlement_pct` is the lowest % of the overdue EMI the backend will accept on a "
            "promise-to-pay — the negotiation guardrail the LLM cannot override. Rahul Sharma is set "
            "to 100% (full EMI only, matching the assignment's PTP demo)."
        )
    else:
        st.info("No customers loaded — is the backend running?")

    st.divider()
    st.subheader("➕ Add a customer")
    st.caption(
        "Adds a new mock loan account to the backend (`POST /api/customers`). "
        "This is an ops/admin action, not something Maya does mid-call — use it to "
        "add test accounts beyond the seeded ones."
    )
    with st.form("add_customer_form", clear_on_submit=True):
        f1, f2 = st.columns(2)
        new_name = f1.text_input("Full name")
        new_phone = f2.text_input("Phone (unique, e.g. +919800011122)")
        f3, f4 = st.columns(2)
        new_dob = f3.date_input("Date of birth", value=datetime.date(1990, 1, 1), min_value=datetime.date(1930, 1, 1), max_value=datetime.date.today())
        new_last4 = f4.text_input("Last 4 digits of loan account", max_chars=4)
        f5, f6 = st.columns(2)
        new_loan_type = f5.selectbox("Loan type", ["Personal Loan", "Two-Wheeler Loan", "Home Loan", "Business Loan", "Credit Card"])
        new_overdue_inr = f6.number_input("Overdue EMI (₹)", min_value=1.0, value=5000.0, step=100.0)
        f7, f8 = st.columns(2)
        new_dpd = f7.number_input("Days past due", min_value=0, value=10, step=1)
        new_floor_pct = f8.slider("Minimum settlement % (PTP floor)", min_value=10, max_value=100, value=100)

        submitted = st.form_submit_button("Add customer")
        if submitted:
            if not (new_name and new_phone and new_last4):
                st.error("Full name, phone, and last 4 digits are required.")
            else:
                status, body = api_post("/api/customers", {
                    "full_name": new_name,
                    "dob": new_dob.isoformat(),
                    "last4": new_last4,
                    "phone": new_phone,
                    "loan_type": new_loan_type,
                    "overdue_amount_cents": int(new_overdue_inr * 100),
                    "days_past_due": int(new_dpd),
                    "min_settlement_pct": int(new_floor_pct),
                })
                if status == 200:
                    st.success(f"Added {new_name} ({body['customer_id']}). Refresh above to see them in the table.")
                elif status == 409:
                    st.error(body.get("error") or body.get("detail") or "A customer with that phone already exists.")
                else:
                    st.error(f"Failed to add customer: {body}")

    with st.expander("🗑️ Remove a customer"):
        if customers:
            del_options = {f"{c['full_name']} ({c['phone']}) — {c['customer_id']}": c["customer_id"] for c in customers}
            del_label = st.selectbox("Customer to remove", list(del_options.keys()), key="del_customer_select")
            if st.button("Delete this customer"):
                status, body = api_post_delete(f"/api/customers/{del_options[del_label]}")
                if status == 200:
                    st.success(f"Deleted {del_label}.")
                else:
                    st.error(f"Failed to delete: {body}")
        else:
            st.caption("No customers to remove.")


# ---------------------------------------------------------------------------
# TAB 4 — Design notes
# ---------------------------------------------------------------------------

with tab_about:
    st.subheader("Why it's built this way")
    st.markdown(
        """
**State-controlled, not prompt-controlled.** The system prompt tells Maya the happy-path
conversation flow, but every *consequential* action — reading the overdue EMI, logging a
promise-to-pay, sending a payment link, recording a disposition — is gated by the FastAPI
backend, not by the LLM's judgement. A jailbroken or confused model literally cannot disclose
the EMI pre-verification or log a PTP below the settlement floor, because the backend returns
a 403 or a `rejected` result regardless of what the model asked for.

**Verification is a hard gate, enforced twice.** `log_promise_to_pay` and `send_payment_link`
check the call's `status` in the database before doing anything (`require_verified()`). Two
failed verification attempts locks the call to `failed_verification` server-side. And
independently, `mark_disposition` refuses to record a debt-related outcome (PROMISE_TO_PAY,
ALREADY_PAID, DISPUTE, HARDSHIP, CALLBACK_REQUESTED, PAYMENT_LINK_SENT) unless the call's
`customer_id` shows verification actually happened — so even a confused agent that skipped
straight to "mark this as already paid" on an unverified call is blocked by the backend.

**Negotiation floor lives in data, not in the prompt.** Each mock account has a
`min_settlement_pct`. The backend computes the floor and rejects anything below it.

**already_paid and callback_requested are first-class, not afterthoughts.** `already_paid`
flags the call for human/ops payment verification (it does NOT let the agent confirm the
payment itself — the mock backend cannot check a core banking system). `callback_requested`
requires both a date and a time before the backend will log it, and stores them on the call
record for ops to action.

**Every call ends with exactly one disposition.** `mark_disposition` is the last tool call on
every path and is what the Live Monitor / dashboard reports as the outcome of the call.

**This dashboard is the same surface for demo and for ops.** It doesn't hit a different
mock — it hits the literal endpoints Vapi calls. What you see in the Call Simulator tab is
mechanically identical to what happens on a real phone call.
"""
    )
