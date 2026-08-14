"""
Mock collections backend for Kapture Finance's "Maya" voice agent.

This is the "FastAPI Backend" box in the architecture diagram. It exposes:

  Tool endpoints (called BY Vapi, via function/tool calling from the agent):
    POST /tools/verify_customer
    POST /tools/log_promise_to_pay   (alias: /tools/log_ptp, kept for compatibility)
    POST /tools/send_payment_link
    POST /tools/edge_case            (dispute, hardship, wrong_person, hostile,
                                       request_human, cease_contact/DNC, voicemail,
                                       already_paid, callback_requested)
    POST /tools/mark_disposition     (final call outcome — every call path ends here)

  Read endpoints (called BY the Streamlit dashboard, to render state):
    GET /api/customers
    GET /api/calls
    GET /api/ptps
    GET /api/payment_links
    GET /api/events?call_id=...

Design notes (see docs/HLD.md for the full writeup):
- Every tool call is logged as an "event" tied to a call_id, so the dashboard
  can reconstruct exactly what the agent did and when — this is the
  "state-controlled" part: the LLM proposes actions, but this service is the
  only thing that can actually mutate account state, and it enforces limits
  the LLM cannot talk its way around (verification gate, settlement floor,
  verification-attempt cap, disposition rules).
- THE MOST IMPORTANT RULE: verification is required before any tool that
  would reveal or act on debt information will do anything for a given
  call_id. This is enforced HERE, in code, not just in the system prompt —
  see `require_verified()` below and its use in every debt-related tool.
- All amounts are stored as integer cents/paise (INR) to avoid float issues.
  1 INR = 100 paise, so amount_cents=849900 means ₹8,499.00.
"""

import sqlite3
import uuid
import datetime
import os
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.path.join(os.path.dirname(__file__), "collections.db")

app = FastAPI(title="Kapture Finance — Maya Collections Mock Backend")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Final dispositions every call must eventually resolve to (Task 3.C).
DISPOSITIONS = [
    "PROMISE_TO_PAY",
    "PAYMENT_LINK_SENT",
    "ALREADY_PAID",
    "DISPUTE",
    "HARDSHIP",
    "CALLBACK_REQUESTED",
    "DO_NOT_CALL",
    "WRONG_PERSON",
    "ESCALATED",
    "NO_RESPONSE",
    "FAILED_VERIFICATION",
    "OTHER",
]

# Dispositions that describe an outcome ABOUT THE DEBT. Recording one of
# these is only legitimate if this call actually completed verification at
# some point (call.customer_id is set) — you cannot log "already paid" or a
# "promise to pay" against an account nobody confirmed the identity of.
# This is a second, independent enforcement point beyond the tool-level
# `require_verified()` gate, because mark_disposition is called at the very
# end of the call and must not trust the model's account of what happened.
DISPOSITIONS_REQUIRING_PRIOR_VERIFICATION = {
    "PROMISE_TO_PAY",
    "PAYMENT_LINK_SENT",
    "ALREADY_PAID",
    "DISPUTE",
    "HARDSHIP",
    "CALLBACK_REQUESTED",
}

EDGE_CASE_KINDS = [
    "dispute_debt",
    "hardship",
    "wrong_person",
    "hostile",
    "request_human",
    "cease_contact",       # Do-Not-Call
    "voicemail",
    "already_paid",
    "callback_requested",
]

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            full_name   TEXT NOT NULL,
            dob         TEXT NOT NULL,          -- YYYY-MM-DD, verification factor
            last4       TEXT NOT NULL,          -- last 4 of loan account, second verification factor
            phone       TEXT NOT NULL,
            loan_type   TEXT NOT NULL DEFAULT 'Personal Loan',
            overdue_amount_cents INTEGER NOT NULL,   -- paise; 100 = Rupee 1.00
            days_past_due INTEGER NOT NULL DEFAULT 0,
            min_settlement_pct INTEGER NOT NULL DEFAULT 100, -- floor for a PTP, % of overdue amount
            cease_contact INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS calls (
            call_id TEXT PRIMARY KEY,
            customer_id TEXT,
            status TEXT NOT NULL DEFAULT 'in_progress', -- in_progress | verified | failed_verification | escalated | closed
            verification_attempts INTEGER NOT NULL DEFAULT 0,
            disposition TEXT,                 -- final outcome, set by mark_disposition
            callback_date TEXT,
            callback_time TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ptps (
            ptp_id TEXT PRIMARY KEY,
            call_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            due_date TEXT NOT NULL,
            method TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payment_links (
            link_id TEXT PRIMARY KEY,
            call_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            call_id TEXT NOT NULL,
            kind TEXT NOT NULL,      -- tool name or 'edge_case'
            detail TEXT NOT NULL,    -- human-readable summary
            created_at TEXT NOT NULL
        );
        """
    )

    # --- lightweight migration for DBs created before disposition/callback
    # columns existed, so upgrading an existing demo DB doesn't crash. ---
    existing_cols = {row["name"] for row in c.execute("PRAGMA table_info(calls)").fetchall()}
    for col, ddl in [
        ("disposition", "ALTER TABLE calls ADD COLUMN disposition TEXT"),
        ("callback_date", "ALTER TABLE calls ADD COLUMN callback_date TEXT"),
        ("callback_time", "ALTER TABLE calls ADD COLUMN callback_time TEXT"),
    ]:
        if col not in existing_cols:
            c.execute(ddl)

    cust_cols = {row["name"] for row in c.execute("PRAGMA table_info(customers)").fetchall()}
    for col, ddl in [
        ("loan_type", "ALTER TABLE customers ADD COLUMN loan_type TEXT NOT NULL DEFAULT 'Personal Loan'"),
        ("days_past_due", "ALTER TABLE customers ADD COLUMN days_past_due INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in cust_cols:
            c.execute(ddl)
    # rename legacy balance_cents -> overdue_amount_cents if this DB predates the rename
    if "balance_cents" in cust_cols and "overdue_amount_cents" not in cust_cols:
        c.execute("ALTER TABLE customers ADD COLUMN overdue_amount_cents INTEGER")
        c.execute("UPDATE customers SET overdue_amount_cents = balance_cents")

    # Seed demo accounts if empty. Rahul Sharma is the PRIMARY assignment
    # demo account (Task 4). A couple of secondary accounts are kept so the
    # settlement-floor / cease-contact edge cases still have data to run
    # against without disturbing the primary scenario.
    row = c.execute("SELECT COUNT(*) AS n FROM customers").fetchone()
    if row["n"] == 0:
        c.executemany(
            """INSERT INTO customers
               (customer_id, full_name, dob, last4, phone, loan_type,
                overdue_amount_cents, days_past_due, min_settlement_pct, cease_contact)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                # Primary assignment demo account — Task 4.
                ("cust_rahul", "Rahul Sharma", "1990-06-15", "4321", "+919876543210",
                 "Personal Loan", 849900, 12, 100, 0),
                # Secondary accounts kept for other demo scenarios.
                ("cust_002", "Priya Nair", "1988-11-02", "9310", "+919810000002",
                 "Personal Loan", 1250000, 25, 70, 0),
                ("cust_003", "Michael Fernandes", "1985-07-23", "1123", "+919810000003",
                 "Two-Wheeler Loan", 2100000, 45, 80, 0),
                ("cust_004", "Devon Blake", "1995-01-30", "5567", "+919810000004",
                 "Personal Loan", 760000, 30, 70, 1),  # already opted out (DNC)
            ],
        )
    conn.commit()
    conn.close()


def now():
    return datetime.datetime.utcnow().isoformat()


def log_event(conn, call_id: str, kind: str, detail: str):
    conn.execute(
        "INSERT INTO events (event_id, call_id, kind, detail, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), call_id, kind, detail, now()),
    )


def ensure_call(conn, call_id: str):
    row = conn.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO calls (call_id, customer_id, status, verification_attempts, created_at) VALUES (?,?,?,?,?)",
            (call_id, None, "in_progress", 0, now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
    return row


def require_verified(conn, call_id: str):
    """The single choke point every debt-disclosing/debt-acting tool must
    call before doing anything. This is the technical (not just prompted)
    enforcement of: 'debt information MUST NEVER be disclosed before
    successful customer verification.'  Raises HTTP 403 otherwise."""
    call = conn.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
    if call is None or call["status"] != "verified":
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="Blocked: this call has not completed customer verification. "
                   "No debt or account information may be disclosed or acted on.",
        )
    return call


@app.on_event("startup")
def startup():
    init_db()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class VerifyCustomerRequest(BaseModel):
    call_id: str = Field(..., description="Vapi call id, stable per phone call")
    phone: str = Field(..., description="Caller phone number, e.g. from Vapi's call.customer.number")
    full_name: str
    dob: str = Field(..., description="YYYY-MM-DD as stated by caller")
    last4: str = Field(..., description="Last 4 digits of the loan account as stated by caller")


class LogPromiseToPayRequest(BaseModel):
    call_id: str
    amount_cents: int = Field(..., gt=0, description="Agreed payment amount in paise")
    due_date: str = Field(..., description="YYYY-MM-DD the caller agreed to pay by")
    method: Literal["upi", "debit_card", "bank_transfer", "netbanking", "other"] = "other"


class SendPaymentLinkRequest(BaseModel):
    call_id: str
    amount_cents: int = Field(..., gt=0)


class EdgeCaseRequest(BaseModel):
    call_id: str
    kind: Literal[
        "dispute_debt",
        "hardship",
        "wrong_person",
        "hostile",
        "request_human",
        "cease_contact",
        "voicemail",
        "already_paid",
        "callback_requested",
    ]
    note: Optional[str] = Field(None, description="Free-text context, e.g. dispute reason or claimed payment date")
    callback_date: Optional[str] = Field(None, description="YYYY-MM-DD — required when kind='callback_requested'")
    callback_time: Optional[str] = Field(None, description="HH:MM (24h) — required when kind='callback_requested'")


class MarkDispositionRequest(BaseModel):
    call_id: str
    disposition: Literal[
        "PROMISE_TO_PAY",
        "PAYMENT_LINK_SENT",
        "ALREADY_PAID",
        "DISPUTE",
        "HARDSHIP",
        "CALLBACK_REQUESTED",
        "DO_NOT_CALL",
        "WRONG_PERSON",
        "ESCALATED",
        "NO_RESPONSE",
        "FAILED_VERIFICATION",
        "OTHER",
    ]
    note: Optional[str] = None


class CreateCustomerRequest(BaseModel):
    """Admin/ops input for adding a new mock loan account — not a Vapi tool.
    Used by the dashboard's 'Add Customer' form (and usable directly via curl)."""
    full_name: str
    dob: str = Field(..., description="YYYY-MM-DD")
    last4: str = Field(..., description="Last 4 digits of the loan account")
    phone: str = Field(..., description="E.164-ish, e.g. +919876543210 — must be unique")
    loan_type: str = "Personal Loan"
    overdue_amount_cents: int = Field(..., gt=0, description="Overdue EMI in paise (INR * 100)")
    days_past_due: int = Field(..., ge=0)
    min_settlement_pct: int = Field(100, ge=1, le=100, description="Floor for a PTP, % of overdue amount")


# ---------------------------------------------------------------------------
# Tool endpoints — these are what Vapi's function-calling invokes
# ---------------------------------------------------------------------------

@app.post("/tools/verify_customer")
def verify_customer(req: VerifyCustomerRequest):
    conn = get_conn()
    call = ensure_call(conn, req.call_id)

    if call["status"] == "escalated":
        conn.close()
        return {"result": "blocked", "reason": "call already escalated to a human agent"}

    cust = conn.execute(
        "SELECT * FROM customers WHERE phone=?", (req.phone,)
    ).fetchone()

    attempts = call["verification_attempts"] + 1
    matched = (
        cust is not None
        and cust["full_name"].strip().lower() == req.full_name.strip().lower()
        and cust["dob"] == req.dob
        and cust["last4"] == req.last4
    )

    if matched:
        if cust["cease_contact"]:
            conn.execute(
                "UPDATE calls SET status=?, customer_id=?, verification_attempts=? WHERE call_id=?",
                ("closed", cust["customer_id"], attempts, req.call_id),
            )
            log_event(conn, req.call_id, "verify_customer",
                      f"Verified {cust['full_name']} but account is Do-Not-Call — call must end")
            conn.commit()
            conn.close()
            return {
                "result": "verified_but_do_not_contact",
                "customer_id": cust["customer_id"],
                "instruction": "This customer previously requested no further contact (DNC). "
                                "Apologize, do not discuss the debt, end the call, and record "
                                "disposition DO_NOT_CALL.",
            }

        conn.execute(
            "UPDATE calls SET status=?, customer_id=?, verification_attempts=? WHERE call_id=?",
            ("verified", cust["customer_id"], attempts, req.call_id),
        )
        log_event(conn, req.call_id, "verify_customer", f"Verified {cust['full_name']} ({cust['customer_id']})")
        conn.commit()
        result = {
            "result": "verified",
            "customer_id": cust["customer_id"],
            "loan_type": cust["loan_type"],
            "overdue_amount_cents": cust["overdue_amount_cents"],
            "days_past_due": cust["days_past_due"],
            "min_settlement_cents": int(cust["overdue_amount_cents"] * cust["min_settlement_pct"] / 100),
        }
        conn.close()
        return result

    # Not matched
    conn.execute(
        "UPDATE calls SET verification_attempts=? WHERE call_id=?", (attempts, req.call_id)
    )
    log_event(conn, req.call_id, "verify_customer", f"Verification failed (attempt {attempts})")

    if attempts >= 2:
        conn.execute("UPDATE calls SET status=? WHERE call_id=?", ("failed_verification", req.call_id))
        conn.commit()
        conn.close()
        return {
            "result": "failed_max_attempts",
            "instruction": "Do not disclose any account or debt information. Politely end the call, "
                            "offer a verified callback path, and record disposition FAILED_VERIFICATION.",
        }

    conn.commit()
    conn.close()
    return {"result": "failed", "attempts_remaining": 2 - attempts}


@app.post("/tools/log_promise_to_pay")
def log_promise_to_pay(req: LogPromiseToPayRequest):
    conn = get_conn()
    call = require_verified(conn, req.call_id)  # raises 403 + closes conn if not verified

    cust = conn.execute("SELECT * FROM customers WHERE customer_id=?", (call["customer_id"],)).fetchone()
    floor = int(cust["overdue_amount_cents"] * cust["min_settlement_pct"] / 100)
    if req.amount_cents < floor:
        conn.close()
        return {
            "result": "rejected",
            "reason": f"Offer is below the approved minimum of {floor} paise for this account.",
            "min_settlement_cents": floor,
        }

    ptp_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ptps (ptp_id, call_id, customer_id, amount_cents, due_date, method, created_at) VALUES (?,?,?,?,?,?,?)",
        (ptp_id, req.call_id, cust["customer_id"], req.amount_cents, req.due_date, req.method, now()),
    )
    log_event(conn, req.call_id, "log_promise_to_pay",
              f"PTP logged: paise {req.amount_cents} due {req.due_date} via {req.method}")
    conn.commit()
    conn.close()
    return {"result": "logged", "ptp_id": ptp_id}


@app.post("/tools/log_ptp", include_in_schema=False)
def log_ptp_alias(req: LogPromiseToPayRequest):
    """Backward-compatible alias for log_promise_to_pay (same handler,
    kept so the original tool name still works and nothing that already
    points at /tools/log_ptp breaks)."""
    return log_promise_to_pay(req)


@app.post("/tools/send_payment_link")
def send_payment_link(req: SendPaymentLinkRequest):
    conn = get_conn()
    call = require_verified(conn, req.call_id)

    link_id = str(uuid.uuid4())[:8]
    url = f"https://pay.kapturefinance-mock.com/l/{link_id}"
    conn.execute(
        "INSERT INTO payment_links (link_id, call_id, customer_id, amount_cents, url, created_at) VALUES (?,?,?,?,?,?)",
        (link_id, req.call_id, call["customer_id"], req.amount_cents, url, now()),
    )
    log_event(conn, req.call_id, "send_payment_link", f"Link sent for paise {req.amount_cents}: {url}")
    conn.commit()
    conn.close()
    return {"result": "sent", "url": url}


@app.post("/tools/edge_case")
def edge_case(req: EdgeCaseRequest):
    """Covers every non-happy-path intent (Task 3.A / 3.B and the original
    edge cases): dispute, hardship, wrong_person, hostile, request_human,
    cease_contact (DNC), voicemail, already_paid, callback_requested.

    NOTE: already_paid and callback_requested do NOT require prior
    verification at the HTTP layer, because in the real conversation they
    only ever occur *after* Maya has disclosed the EMI (which itself
    requires verification) — but see mark_disposition, which independently
    refuses to record a debt-related disposition on a call that was never
    verified, so an unverified call can't be walked to those outcomes
    either way.
    """
    conn = get_conn()
    call = ensure_call(conn, req.call_id)

    if req.kind == "callback_requested" and not (req.callback_date and req.callback_time):
        conn.close()
        raise HTTPException(
            status_code=422,
            detail="callback_requested requires both callback_date (YYYY-MM-DD) and callback_time (HH:MM).",
        )

    new_status = call["status"]
    if req.kind in ("hostile", "request_human", "dispute_debt", "already_paid"):
        new_status = "escalated"  # flagged for human/ops follow-up (e.g. payment verification)
    if req.kind == "cease_contact" and call["customer_id"]:
        conn.execute("UPDATE customers SET cease_contact=1 WHERE customer_id=?", (call["customer_id"],))
        new_status = "closed"

    if req.kind == "callback_requested":
        conn.execute(
            "UPDATE calls SET status=?, callback_date=?, callback_time=? WHERE call_id=?",
            (new_status, req.callback_date, req.callback_time, req.call_id),
        )
    else:
        conn.execute("UPDATE calls SET status=? WHERE call_id=?", (new_status, req.call_id))

    detail = req.kind
    if req.kind == "callback_requested":
        detail += f": {req.callback_date} {req.callback_time}"
    if req.note:
        detail += f" — {req.note}"
    log_event(conn, req.call_id, "edge_case", detail)
    conn.commit()
    conn.close()
    return {"result": "logged", "kind": req.kind, "call_status": new_status}


@app.post("/tools/mark_disposition")
def mark_disposition(req: MarkDispositionRequest):
    """Records the FINAL outcome of the call. Every normal call path must
    call this exactly once before ending (Task 3.C). Business rule enforced
    here, independent of the LLM: a debt-related disposition cannot be
    recorded on a call that never completed verification."""
    conn = get_conn()
    call = ensure_call(conn, req.call_id)

    if req.disposition in DISPOSITIONS_REQUIRING_PRIOR_VERIFICATION and not call["customer_id"]:
        conn.close()
        raise HTTPException(
            status_code=403,
            detail=f"Cannot record disposition {req.disposition}: this call never completed "
                   f"customer verification.",
        )

    new_status = "escalated" if req.disposition == "ESCALATED" else "closed"
    conn.execute(
        "UPDATE calls SET disposition=?, status=? WHERE call_id=?",
        (req.disposition, new_status, req.call_id),
    )
    log_event(conn, req.call_id, "mark_disposition", f"{req.disposition}" + (f" — {req.note}" if req.note else ""))
    conn.commit()
    conn.close()
    return {"result": "recorded", "disposition": req.disposition, "call_status": new_status}


# ---------------------------------------------------------------------------
# Read endpoints — for the Streamlit dashboard
# (api_customers has a companion POST/DELETE below it — these are ops/admin
# actions on the mock account list, not Vapi tools, so they live here rather
# than under /tools/*.)
# ---------------------------------------------------------------------------

@app.get("/api/customers")
def api_customers():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM customers").fetchall()]
    conn.close()
    return rows


@app.post("/api/customers")
def create_customer(req: CreateCustomerRequest):
    """Add a new mock loan account. Ops/admin action (used by the dashboard's
    Add Customer form), not something Maya calls mid-call."""
    conn = get_conn()
    existing = conn.execute("SELECT customer_id FROM customers WHERE phone=?", (req.phone,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail=f"A customer with phone {req.phone} already exists.")

    customer_id = f"cust_{uuid.uuid4().hex[:8]}"
    conn.execute(
        """INSERT INTO customers
           (customer_id, full_name, dob, last4, phone, loan_type,
            overdue_amount_cents, days_past_due, min_settlement_pct, cease_contact)
           VALUES (?,?,?,?,?,?,?,?,?,0)""",
        (customer_id, req.full_name, req.dob, req.last4, req.phone, req.loan_type,
         req.overdue_amount_cents, req.days_past_due, req.min_settlement_pct),
    )
    conn.commit()
    conn.close()
    return {"result": "created", "customer_id": customer_id}


@app.delete("/api/customers/{customer_id}")
def delete_customer(customer_id: str):
    """Remove a mock account — ops/admin cleanup, e.g. for a customer you
    added by mistake. Does not touch any calls/ptps/events already logged
    against that customer_id, so historical records stay intact."""
    conn = get_conn()
    row = conn.execute("SELECT customer_id FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"No customer with id {customer_id}.")
    conn.execute("DELETE FROM customers WHERE customer_id=?", (customer_id,))
    conn.commit()
    conn.close()
    return {"result": "deleted", "customer_id": customer_id}


@app.get("/api/calls")
def api_calls():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY created_at DESC").fetchall()]
    conn.close()
    return rows


@app.get("/api/ptps")
def api_ptps():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM ptps ORDER BY created_at DESC").fetchall()]
    conn.close()
    return rows


@app.get("/api/payment_links")
def api_payment_links():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM payment_links ORDER BY created_at DESC").fetchall()]
    conn.close()
    return rows


@app.get("/api/events")
def api_events(call_id: Optional[str] = None):
    conn = get_conn()
    if call_id:
        rows = conn.execute(
            "SELECT * FROM events WHERE call_id=? ORDER BY created_at ASC", (call_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/health")
def health():
    return {"status": "ok"}
