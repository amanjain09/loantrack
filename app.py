"""
LoanTrack — Prabhu Ventures SaaS backend.

Architecture:
  - Roles: admin (platform owner), user (lender / tenant)
  - Multi-tenant: every case scoped to its owning lender (user_id)
  - Subscriptions: trial (7 d) → active → grace → expired
  - Payments: provider-agnostic (ManualProvider works; Razorpay/Stripe/PayU stubbed)
  - SMS / OTP: provider-agnostic (ConsoleProvider works; Twilio/MSG91 stubbed)
  - Audit log: every sensitive action is recorded

Existing prod data is preserved: a "founding lender" user is seeded
and all pre-existing cases are migrated under that account.
"""

import os
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, date as date_type
from functools import wraps

from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

# ──────────────────────────────────────────────────────────────────────────────
# Database connection
# ──────────────────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG       = bool(DATABASE_URL)

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "lending-app-dev-key-change-in-prod")
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024     # 64 MB for base64 uploads

DB_PATH = os.path.join(os.path.dirname(__file__), "lending.db")

# Founding lender (you) and platform super-admin seeded on first init.
FOUNDER_PHONE         = os.environ.get("FOUNDER_PHONE", "9479913772")
FOUNDER_NAME          = os.environ.get("FOUNDER_NAME",  "Aman")
FOUNDER_OTP           = os.environ.get("FOUNDER_OTP", "947200")  # bypass for FOUNDER_PHONE only
ADMIN_USERNAME        = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD        = os.environ.get("ADMIN_PASSWORD", "admin123")


def get_db():
    if USE_PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_execute(conn, sql, params=()):
    """Run a query — handles ? vs %s between SQLite and PostgreSQL."""
    if USE_PG:
        sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


# ──────────────────────────────────────────────────────────────────────────────
# SMS provider abstraction (gateway-agnostic OTP delivery)
# ──────────────────────────────────────────────────────────────────────────────
class SMSProvider:
    """Implement send(phone, message) → bool to plug in any SMS gateway."""
    def send(self, phone, message): raise NotImplementedError


class ConsoleSMSProvider(SMSProvider):
    """Dev / fallback: prints the SMS to server logs. Always succeeds."""
    def send(self, phone, message):
        print(f"\n[SMS → {phone}]  {message}\n", flush=True)
        return True


class TwilioSMSProvider(SMSProvider):
    """Stub. Add TWILIO_SID / TWILIO_TOKEN / TWILIO_FROM env vars and `pip install twilio`."""
    def send(self, phone, message):
        raise NotImplementedError("Twilio not configured. Set TWILIO_* env vars and install twilio package.")


class MSG91SMSProvider(SMSProvider):
    """Stub. Add MSG91_AUTH_KEY / MSG91_TEMPLATE_ID and call their HTTP API."""
    def send(self, phone, message):
        raise NotImplementedError("MSG91 not configured. Set MSG91_* env vars.")


def get_sms_provider():
    name = os.environ.get("SMS_PROVIDER", "console").lower()
    return {
        "console": ConsoleSMSProvider,
        "twilio":  TwilioSMSProvider,
        "msg91":   MSG91SMSProvider,
    }.get(name, ConsoleSMSProvider)()


# ──────────────────────────────────────────────────────────────────────────────
# Payment provider abstraction (gateway-agnostic billing)
# ──────────────────────────────────────────────────────────────────────────────
class PaymentProvider:
    """Implement these 3 to plug in any payment gateway."""
    name = "abstract"
    def create_order(self, amount_inr, currency, metadata):
        """Return {'provider_order_id': str, 'raw': dict, 'requires_redirect': bool}"""
        raise NotImplementedError
    def verify_payment(self, payload):
        """Return {'ok': bool, 'provider_payment_id': str, 'raw': dict}"""
        raise NotImplementedError
    def refund(self, provider_payment_id, amount_inr=None):
        raise NotImplementedError


class ManualPaymentProvider(PaymentProvider):
    """
    Works out-of-the-box. Admin records cash / UPI / bank transfer manually.
    Orders are auto-marked succeeded. Use this until you wire a real gateway.
    """
    name = "manual"
    def create_order(self, amount_inr, currency, metadata):
        return {
            "provider_order_id": f"MANUAL-{secrets.token_hex(6).upper()}",
            "raw":               {"note": "Manual payment — admin will mark paid."},
            "requires_redirect": False,
        }
    def verify_payment(self, payload):
        # Admin POSTs {provider_order_id, method, note}. We trust admin actions.
        return {
            "ok":                  True,
            "provider_payment_id": f"MANUAL-PAY-{secrets.token_hex(6).upper()}",
            "raw":                 payload,
        }
    def refund(self, provider_payment_id, amount_inr=None):
        return {"ok": True, "refund_id": f"MANUAL-RFND-{secrets.token_hex(6).upper()}"}


class RazorpayProvider(PaymentProvider):
    name = "razorpay"
    def create_order(self, amount_inr, currency, metadata):
        raise NotImplementedError("Razorpay not configured. Set RAZORPAY_KEY_ID / KEY_SECRET and install razorpay package.")
    def verify_payment(self, payload):  raise NotImplementedError
    def refund(self, *a, **kw):         raise NotImplementedError


class StripeProvider(PaymentProvider):
    name = "stripe"
    def create_order(self, amount_inr, currency, metadata):
        raise NotImplementedError("Stripe not configured. Set STRIPE_SECRET_KEY and install stripe package.")
    def verify_payment(self, payload):  raise NotImplementedError
    def refund(self, *a, **kw):         raise NotImplementedError


class PayUProvider(PaymentProvider):
    name = "payu"
    def create_order(self, amount_inr, currency, metadata):
        raise NotImplementedError("PayU not configured. Set PAYU_KEY / PAYU_SALT.")
    def verify_payment(self, payload):  raise NotImplementedError
    def refund(self, *a, **kw):         raise NotImplementedError


def get_payment_provider():
    name = os.environ.get("PAYMENT_PROVIDER", "manual").lower()
    return {
        "manual":   ManualPaymentProvider,
        "razorpay": RazorpayProvider,
        "stripe":   StripeProvider,
        "payu":     PayUProvider,
    }.get(name, ManualPaymentProvider)()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers — calc, current user, audit
# ──────────────────────────────────────────────────────────────────────────────
def _calc_months(loan_date_str, end_date=None):
    """Calendar-based months. Incomplete month counts as full. Minimum 1."""
    if not loan_date_str:
        return 1
    try:
        loan = date_type.fromisoformat(str(loan_date_str)[:10])
        end  = end_date or date_type.today()
        months = (end.year - loan.year) * 12 + (end.month - loan.month)
        if end.day > loan.day:
            months += 1
        return max(1, months)
    except Exception:
        return 1


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_db()
    cur  = db_execute(conn, "SELECT id, name, phone, username, role, status FROM users WHERE id = ?", (uid,))
    row  = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def require_auth(roles=None):
    """Decorator: require login (and optionally a specific role)."""
    if roles and isinstance(roles, str):
        roles = [roles]
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            u = current_user()
            if not u:
                return jsonify({"error": "Unauthorized"}), 401
            if u.get("status") != "active":
                return jsonify({"error": "Account suspended"}), 403
            if roles and u["role"] not in roles:
                return jsonify({"error": "Forbidden"}), 403
            return fn(*args, **kwargs, _user=u)
        return wrapped
    return deco


def audit(action, target_type=None, target_id=None, meta=None, actor_id=None):
    """Append a row to audit_log. Safe to call without an active user."""
    actor = actor_id if actor_id is not None else session.get("user_id")
    try:
        conn = get_db()
        db_execute(conn,
            "INSERT INTO audit_log (actor_id, action, target_type, target_id, meta) VALUES (?, ?, ?, ?, ?)",
            (actor, action, target_type, str(target_id) if target_id is not None else None,
             json.dumps(meta) if meta else None))
        conn.commit()
        conn.close()
    except Exception as e:
        # Never let audit break the request
        print(f"[audit] failed: {e}", flush=True)


def record_case_history(case_id, action, changes=None, actor_id=None):
    """Append a per-case history entry. `changes` is the diff dict or None."""
    actor = actor_id if actor_id is not None else session.get("user_id")
    try:
        conn = get_db()
        db_execute(conn,
            "INSERT INTO case_history (case_id, actor_id, action, changes) VALUES (?, ?, ?, ?)",
            (case_id, actor, action, json.dumps(changes) if changes else None))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[case_history] failed: {e}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# Lean column list for case list endpoint (excludes base64 blobs)
# ──────────────────────────────────────────────────────────────────────────────
_LIST_COLS = (
    "id, user_id, name, father_name, address, mobile, items, weight, metal, "
    "money_lent, interest_rate, loan_date, loan_time, notes, status, "
    "closed_at, amount_received, probable_close_date, hard_deadline, created_at"
)


# ──────────────────────────────────────────────────────────────────────────────
# Schema + seed
# ──────────────────────────────────────────────────────────────────────────────
USERS_DDL_PG = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT UNIQUE,
    username      TEXT UNIQUE,
    email         TEXT,
    password_hash TEXT,
    role          TEXT NOT NULL CHECK (role IN ('super_admin','admin','user')),
    status        TEXT DEFAULT 'active',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
USERS_DDL_SQ = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    phone         TEXT UNIQUE,
    username      TEXT UNIQUE,
    email         TEXT,
    password_hash TEXT,
    role          TEXT NOT NULL CHECK (role IN ('super_admin','admin','user')),
    status        TEXT DEFAULT 'active',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

PLANS_DDL_PG = """
CREATE TABLE IF NOT EXISTS plans (
    id            SERIAL PRIMARY KEY,
    code          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    price_inr     REAL NOT NULL,
    duration_days INTEGER NOT NULL,
    is_trial      BOOLEAN DEFAULT FALSE,
    active        BOOLEAN DEFAULT TRUE
)
"""
PLANS_DDL_SQ = """
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    price_inr     REAL NOT NULL,
    duration_days INTEGER NOT NULL,
    is_trial      INTEGER DEFAULT 0,
    active        INTEGER DEFAULT 1
)
"""

SUBS_DDL_PG = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    plan_code   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    started_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP NOT NULL,
    auto_renew  BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
SUBS_DDL_SQ = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    plan_code   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    started_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP NOT NULL,
    auto_renew  INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

PAYMENTS_DDL_PG = """
CREATE TABLE IF NOT EXISTS payments (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    subscription_id     INTEGER,
    plan_code           TEXT,
    amount_inr          REAL NOT NULL,
    currency            TEXT DEFAULT 'INR',
    provider            TEXT NOT NULL,
    provider_order_id   TEXT,
    provider_payment_id TEXT,
    status              TEXT NOT NULL,
    method              TEXT,
    note                TEXT,
    raw_payload         TEXT,
    paid_at             TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
PAYMENTS_DDL_SQ = """
CREATE TABLE IF NOT EXISTS payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    subscription_id     INTEGER,
    plan_code           TEXT,
    amount_inr          REAL NOT NULL,
    currency            TEXT DEFAULT 'INR',
    provider            TEXT NOT NULL,
    provider_order_id   TEXT,
    provider_payment_id TEXT,
    status              TEXT NOT NULL,
    method              TEXT,
    note                TEXT,
    raw_payload         TEXT,
    paid_at             TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

OTP_DDL_PG = """
CREATE TABLE IF NOT EXISTS otp_codes (
    id          SERIAL PRIMARY KEY,
    phone       TEXT NOT NULL,
    code        TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    used        BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
OTP_DDL_SQ = """
CREATE TABLE IF NOT EXISTS otp_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL,
    code        TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    used        INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

AUDIT_DDL_PG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    actor_id    INTEGER,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    meta        TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CASE_HISTORY_DDL_PG = """
CREATE TABLE IF NOT EXISTS case_history (
    id         SERIAL PRIMARY KEY,
    case_id    INTEGER NOT NULL,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    changes    TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
CASE_HISTORY_DDL_SQ = """
CREATE TABLE IF NOT EXISTS case_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    INTEGER NOT NULL,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    changes    TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
AUDIT_DDL_SQ = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    meta        TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# (CASE_HISTORY_DDL_SQ defined above)


def init_db():
    conn = get_db()
    if USE_PG:
        cur = conn.cursor()

        # ── cases (preserved from earlier deploy) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                id                  SERIAL PRIMARY KEY,
                user_id             INTEGER,
                name                TEXT    NOT NULL,
                father_name         TEXT,
                address             TEXT,
                mobile              TEXT,
                items               TEXT,
                weight              REAL,
                metal               TEXT,
                money_lent          REAL,
                interest_rate       REAL,
                loan_date           TEXT,
                loan_time           TEXT,
                notes               TEXT,
                status              TEXT DEFAULT 'open',
                closed_at           TEXT,
                amount_received     REAL,
                probable_close_date TEXT,
                hard_deadline       TEXT,
                address_proof       TEXT,
                lending_video       TEXT,
                closing_video       TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col_def in [
            "ADD COLUMN IF NOT EXISTS user_id INTEGER",
            "ADD COLUMN IF NOT EXISTS address TEXT",
            "ADD COLUMN IF NOT EXISTS mobile TEXT",
            "ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'open'",
            "ADD COLUMN IF NOT EXISTS closed_at TEXT",
            "ADD COLUMN IF NOT EXISTS amount_received REAL",
            "ADD COLUMN IF NOT EXISTS probable_close_date TEXT",
            "ADD COLUMN IF NOT EXISTS hard_deadline TEXT",
            "ADD COLUMN IF NOT EXISTS address_proof TEXT",
            "ADD COLUMN IF NOT EXISTS lending_video TEXT",
            "ADD COLUMN IF NOT EXISTS closing_video TEXT",
        ]:
            cur.execute(f"ALTER TABLE cases {col_def}")

        # ── leads (from earlier deploy) ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                email      TEXT,
                phone      TEXT,
                company    TEXT,
                message    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── new tables ──
        cur.execute(USERS_DDL_PG)
        cur.execute(PLANS_DDL_PG)
        cur.execute(SUBS_DDL_PG)
        cur.execute(PAYMENTS_DDL_PG)
        cur.execute(OTP_DDL_PG)
        cur.execute(AUDIT_DDL_PG)
        cur.execute(CASE_HISTORY_DDL_PG)
        # KYC + profile columns on users (idempotent migration)
        for col_def in [
            "ADD COLUMN IF NOT EXISTS aadhaar TEXT",
            "ADD COLUMN IF NOT EXISTS pan TEXT",
            "ADD COLUMN IF NOT EXISTS dob TEXT",
            "ADD COLUMN IF NOT EXISTS address TEXT",
            "ADD COLUMN IF NOT EXISTS business_name TEXT",
            "ADD COLUMN IF NOT EXISTS kyc_status TEXT DEFAULT 'pending'",
        ]:
            cur.execute(f"ALTER TABLE users {col_def}")

        cur.execute("UPDATE cases SET status = 'open' WHERE status IS NULL")
        conn.commit()
        cur.close()
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER,
                name                TEXT    NOT NULL,
                father_name         TEXT,
                address             TEXT,
                mobile              TEXT,
                items               TEXT,
                weight              REAL,
                metal               TEXT,
                money_lent          REAL,
                interest_rate       REAL,
                loan_date           TEXT,
                loan_time           TEXT,
                notes               TEXT,
                status              TEXT DEFAULT 'open',
                closed_at           TEXT,
                amount_received     REAL,
                probable_close_date TEXT,
                hard_deadline       TEXT,
                address_proof       TEXT,
                lending_video       TEXT,
                closing_video       TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col_def in [
            "user_id INTEGER", "address TEXT", "mobile TEXT",
            "status TEXT DEFAULT 'open'", "closed_at TEXT", "amount_received REAL",
            "probable_close_date TEXT", "hard_deadline TEXT",
            "address_proof TEXT", "lending_video TEXT", "closing_video TEXT",
        ]:
            try:    conn.execute(f"ALTER TABLE cases ADD COLUMN {col_def}")
            except: pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                email      TEXT,
                phone      TEXT,
                company    TEXT,
                message    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(USERS_DDL_SQ)
        conn.execute(PLANS_DDL_SQ)
        conn.execute(SUBS_DDL_SQ)
        conn.execute(PAYMENTS_DDL_SQ)
        conn.execute(OTP_DDL_SQ)
        conn.execute(AUDIT_DDL_SQ)
        conn.execute(CASE_HISTORY_DDL_SQ)
        # KYC + profile columns on users (idempotent — SQLite has no IF NOT EXISTS for ALTER)
        for col_def in [
            "aadhaar TEXT", "pan TEXT", "dob TEXT", "address TEXT",
            "business_name TEXT", "kyc_status TEXT DEFAULT 'pending'",
        ]:
            try:    conn.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
            except: pass
        conn.execute("UPDATE cases SET status = 'open' WHERE status IS NULL")
        conn.commit()
    conn.close()
    seed_data()


def seed_data():
    """Seed plans, super-admin, demo admin, founding lender + migrate existing cases."""
    conn = get_db()

    # 1) Plans (placeholder prices — super-admin can edit later)
    cur = db_execute(conn, "SELECT COUNT(*) AS n FROM plans")
    if cur.fetchone()["n"] == 0:
        TRUE_VAL  = True  if USE_PG else 1
        FALSE_VAL = False if USE_PG else 0
        for code, name, price, days, is_trial in [
            ("trial",       "7-Day Free Trial",     0,    7,   TRUE_VAL),
            ("monthly",     "Monthly",              1,    30,  FALSE_VAL),
            ("half_yearly", "Half-Yearly (6 mo)",   1,    180, FALSE_VAL),
            ("yearly",      "Yearly (12 mo)",       1,    365, FALSE_VAL),
        ]:
            db_execute(conn,
                "INSERT INTO plans (code, name, price_inr, duration_days, is_trial, active) VALUES (?, ?, ?, ?, ?, ?)",
                (code, name, price, days, is_trial, TRUE_VAL))

    # 2) Sole admin account (admin/admin123 by default)
    # Migrate any pre-existing super_admin → admin so they keep working
    db_execute(conn, "UPDATE users SET role = 'admin' WHERE role = 'super_admin'")
    # Remove legacy seeded "manager" admin (no-op if already deleted or never created)
    db_execute(conn, "DELETE FROM users WHERE username = 'manager' AND role = 'admin'")

    cur = db_execute(conn, "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,))
    if not cur.fetchone():
        db_execute(conn,
            "INSERT INTO users (name, username, password_hash, role, status) VALUES (?, ?, ?, ?, ?)",
            ("Admin", ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD, method='pbkdf2:sha256'), "admin", "active"))

    # 4) Founding lender (tenant) — and migrate orphan cases under them
    # If an old founder row exists with the legacy placeholder phone, update it.
    db_execute(conn, "UPDATE users SET phone = ? WHERE phone = '9999999999' AND role = 'user'", (FOUNDER_PHONE,))

    cur = db_execute(conn, "SELECT id FROM users WHERE phone = ?", (FOUNDER_PHONE,))
    founder = cur.fetchone()
    if not founder:
        cur = db_execute(conn,
            "INSERT INTO users (name, phone, role, status) VALUES (?, ?, ?, ?)" +
            (" RETURNING id" if USE_PG else ""),
            (FOUNDER_NAME, FOUNDER_PHONE, "user", "active"))
        founder_id = cur.fetchone()["id"] if USE_PG else cur.lastrowid

        # Generous founding plan: 1 year free
        expires = datetime.utcnow() + timedelta(days=365)
        db_execute(conn,
            "INSERT INTO subscriptions (user_id, plan_code, status, expires_at) VALUES (?, ?, ?, ?)",
            (founder_id, "yearly", "active", expires))
    else:
        founder_id = founder["id"]

    # Migrate any orphan cases (user_id IS NULL) to the founder
    db_execute(conn, "UPDATE cases SET user_id = ? WHERE user_id IS NULL", (founder_id,))

    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Subscription helpers
# ──────────────────────────────────────────────────────────────────────────────
def get_active_subscription(user_id):
    """Returns the latest sub (any status). Caller decides if it's usable."""
    conn = get_db()
    cur  = db_execute(conn,
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,))
    row  = cur.fetchone()
    conn.close()
    if not row:
        return None
    sub = dict(row)
    try:
        exp = sub["expires_at"]
        if isinstance(exp, str):
            exp = datetime.fromisoformat(exp.replace("Z", "").replace(" ", "T")[:19])
        sub["expires_at_iso"] = exp.isoformat()
        days_left = (exp - datetime.utcnow()).days
        sub["days_left"] = days_left
        # Status normalisation
        if days_left < 0:
            sub["effective_status"] = "expired"
        elif days_left <= 7 and sub["status"] != "trial":
            sub["effective_status"] = sub["status"]
        else:
            sub["effective_status"] = sub["status"]
        sub["is_trial"] = (sub["plan_code"] == "trial")
        sub["active"] = days_left >= 0 and sub["status"] in ("active", "trial")
    except Exception:
        sub["days_left"] = 0
        sub["active"]    = False
        sub["effective_status"] = "expired"
    return sub


def require_active_subscription(fn):
    """Decorator: tenant (role=user) must have an active subscription."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        u = kwargs.get("_user") or current_user()
        if not u:
            return jsonify({"error": "Unauthorized"}), 401
        # Admins & super admins bypass
        if u["role"] == "admin":
            return fn(*args, **kwargs)
        sub = get_active_subscription(u["id"])
        if not sub or not sub["active"]:
            return jsonify({"error": "Subscription expired", "code": "SUB_EXPIRED"}), 402
        return fn(*args, **kwargs)
    return wrapped


# ──────────────────────────────────────────────────────────────────────────────
# Auth & OTP
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/check-auth")
def check_auth():
    u = current_user()
    if not u:
        return jsonify({"authenticated": False})
    out = {"authenticated": True, "user": u}
    if u["role"] == "user":
        out["subscription"] = get_active_subscription(u["id"])
    return out


@app.route("/api/auth/me")
def auth_me():
    return check_auth()


def _mask_id(id_int, prefix="PV-"):
    return f"{prefix}{int(id_int):06d}" if id_int else None


@app.route("/api/users/me/profile", methods=["GET"])
@require_auth(roles=["user", "admin"])
def get_my_profile(_user):
    """Full profile for the currently logged-in user including KYC + sub + payments."""
    conn = get_db()
    cur  = db_execute(conn,
        "SELECT id, name, phone, email, role, status, "
        "aadhaar, pan, dob, address, business_name, kyc_status, created_at "
        "FROM users WHERE id = ?", (_user["id"],))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    profile = dict(row)
    profile["customer_id"] = _mask_id(profile["id"])

    sub = None
    payments = []
    if _user["role"] == "user":
        sub = get_active_subscription(_user["id"])
        cur = db_execute(conn, "SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 20", (_user["id"],))
        payments = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"profile": profile, "subscription": sub, "payments": payments})


_PROFILE_EDITABLE = ("name", "email", "address", "business_name", "aadhaar", "pan", "dob")


@app.route("/api/users/me", methods=["PATCH"])
@require_auth(roles=["user", "admin"])
def edit_my_profile(_user):
    """Edit own profile (non-sensitive fields). Phone and role cannot be changed here."""
    data = request.get_json() or {}
    fields, params = [], []
    for k in _PROFILE_EDITABLE:
        if k not in data:
            continue
        v = data.get(k)
        if isinstance(v, str):
            v = v.strip() or None
        fields.append(f"{k} = ?")
        params.append(v)
    if not fields:
        return jsonify({"error": "Nothing to update"}), 400
    params.append(_user["id"])
    conn = get_db()
    try:
        db_execute(conn, f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    audit("profile_self_edit", "user", _user["id"], {"fields": list(data.keys())})
    return jsonify({"success": True})


@app.route("/api/auth/login", methods=["POST"])
def login():
    """Username + password login (for admins & super-admins, and legacy lenders)."""
    data = request.get_json() or {}
    uname = (data.get("username") or "").strip()
    pwd   = data.get("password") or ""
    if not uname or not pwd:
        return jsonify({"success": False, "message": "Username and password required"}), 400

    conn = get_db()
    cur  = db_execute(conn,
        "SELECT * FROM users WHERE username = ? OR phone = ?", (uname, uname))
    row  = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"success": False, "message": "Invalid credentials"}), 401
    row  = dict(row)   # normalize sqlite3.Row → dict
    if not row.get("password_hash") or not check_password_hash(row["password_hash"], pwd):
        return jsonify({"success": False, "message": "Invalid credentials"}), 401
    if row.get("status") != "active":
        return jsonify({"success": False, "message": "Account suspended"}), 403

    session["user_id"]    = row["id"]
    session["role"]       = row["role"]
    session["logged_in"]  = True   # legacy flag retained for safety
    audit("login", "user", row["id"], {"method": "password"})
    return jsonify({"success": True, "user": {"id": row["id"], "name": row["name"], "role": row["role"]}})


@app.route("/api/auth/request-otp", methods=["POST"])
def request_otp():
    """Send OTP for signup or login (purpose: 'signup' or 'login')."""
    data    = request.get_json() or {}
    phone   = (data.get("phone")   or "").strip()
    purpose = (data.get("purpose") or "login").strip()
    if not phone or len(phone) < 10:
        return jsonify({"error": "Valid phone number required"}), 400
    if purpose not in ("signup", "login"):
        return jsonify({"error": "Invalid purpose"}), 400

    conn = get_db()
    # Block signup OTP if phone already registered
    cur = db_execute(conn, "SELECT id FROM users WHERE phone = ?", (phone,))
    exists = cur.fetchone()
    if purpose == "signup" and exists:
        conn.close()
        return jsonify({"error": "This number is already registered. Please sign in instead."}), 409
    if purpose == "login" and not exists:
        conn.close()
        return jsonify({"error": "No account found for this number. Please sign up first."}), 404

    code = f"{secrets.randbelow(900000) + 100000}"
    exp  = datetime.utcnow() + timedelta(minutes=10)
    db_execute(conn,
        "INSERT INTO otp_codes (phone, code, purpose, expires_at) VALUES (?, ?, ?, ?)",
        (phone, code, purpose, exp))
    conn.commit()
    conn.close()

    get_sms_provider().send(phone, f"Your Prabhu Ventures OTP is {code}. Valid 10 minutes.")
    return jsonify({"success": True, "message": "OTP sent. Check your phone."})


@app.route("/api/auth/verify-otp", methods=["POST"])
def verify_otp():
    """
    Verify OTP. If purpose=signup → create user (role=user) + start 7-day trial.
    If purpose=login → log existing lender in.
    """
    data    = request.get_json() or {}
    phone   = (data.get("phone")   or "").strip()
    code    = (data.get("code")    or "").strip()
    name    = (data.get("name")    or "").strip()
    purpose = (data.get("purpose") or "login").strip()

    if not phone or not code:
        return jsonify({"error": "Phone and code required"}), 400

    conn = get_db()

    # ── Founder OTP backdoor: ONLY for the founding member's phone ──
    if FOUNDER_OTP and phone == FOUNDER_PHONE and code == FOUNDER_OTP:
        print(f"[OTP] founder bypass used for {phone} (purpose={purpose})", flush=True)
    else:
        cur = db_execute(conn,
            "SELECT * FROM otp_codes WHERE phone = ? AND purpose = ? AND used = ? "
            "ORDER BY id DESC LIMIT 1",
            (phone, purpose, False if USE_PG else 0))
        otp = cur.fetchone()
        if not otp:
            conn.close()
            return jsonify({"error": "No OTP requested. Please request a new code."}), 400
        exp = otp["expires_at"]
        if isinstance(exp, str):
            exp = datetime.fromisoformat(exp.replace("Z", "").replace(" ", "T")[:19])
        if datetime.utcnow() > exp:
            conn.close()
            return jsonify({"error": "OTP expired. Please request a new code."}), 400
        if otp["code"] != code:
            conn.close()
            return jsonify({"error": "Invalid code."}), 400
        db_execute(conn, "UPDATE otp_codes SET used = ? WHERE id = ?", (True if USE_PG else 1, otp["id"]))

    if purpose == "signup":
        if not name:
            conn.close()
            return jsonify({"error": "Name required for signup"}), 400
        cur = db_execute(conn,
            "INSERT INTO users (name, phone, role, status) VALUES (?, ?, ?, ?)" +
            (" RETURNING id" if USE_PG else ""),
            (name, phone, "user", "active"))
        user_id = cur.fetchone()["id"] if USE_PG else cur.lastrowid
        # Start trial
        expires = datetime.utcnow() + timedelta(days=7)
        db_execute(conn,
            "INSERT INTO subscriptions (user_id, plan_code, status, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, "trial", "trial", expires))
        conn.commit()
        conn.close()

        session["user_id"]   = user_id
        session["role"]      = "user"
        session["logged_in"] = True
        audit("signup", "user", user_id, {"method": "otp"})
        return jsonify({"success": True, "user": {"id": user_id, "name": name, "role": "user"}}), 201

    # purpose == login
    cur = db_execute(conn, "SELECT * FROM users WHERE phone = ?", (phone,))
    row = cur.fetchone()
    conn.commit()
    conn.close()
    if not row or row["status"] != "active":
        return jsonify({"error": "Account not active"}), 403

    session["user_id"]   = row["id"]
    session["role"]      = row["role"]
    session["logged_in"] = True
    audit("login", "user", row["id"], {"method": "otp"})
    return jsonify({"success": True, "user": {"id": row["id"], "name": row["name"], "role": row["role"]}})


@app.route("/api/logout", methods=["POST"])
@app.route("/api/auth/logout", methods=["POST"])
def logout():
    uid = session.get("user_id")
    if uid:
        audit("logout", "user", uid)
    session.clear()
    return jsonify({"success": True})


# ──────────────────────────────────────────────────────────────────────────────
# Public — request-access (landing page lead capture)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/request-access", methods=["POST"])
def request_access():
    data    = request.get_json() or {}
    name    = (data.get("name")    or "").strip()
    email   = (data.get("email")   or "").strip()
    phone   = (data.get("phone")   or "").strip()
    company = (data.get("company") or "").strip()
    message = (data.get("message") or "").strip()

    if not name:
        return jsonify({"error": "Please enter your name."}), 400
    if not email and not phone:
        return jsonify({"error": "Please provide an email or phone number."}), 400

    conn = get_db()
    db_execute(conn,
        "INSERT INTO leads (name, email, phone, company, message) VALUES (?, ?, ?, ?, ?)",
        (name, email or None, phone or None, company or None, message or None))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 201


@app.route("/api/leads", methods=["GET"])
@require_auth(roles=["admin"])
def get_leads(_user):
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM leads ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ──────────────────────────────────────────────────────────────────────────────
# Cases (tenant-scoped)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/cases", methods=["POST"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def add_case(_user):
    data = request.get_json() or {}
    if not data.get("name", "").strip():
        return jsonify({"error": "Borrower name is required"}), 400

    values = (
        _user["id"],
        data.get("name", "").strip(),
        data.get("father_name", "").strip() or None,
        data.get("address", "").strip() or None,
        data.get("mobile", "").strip() or None,
        data.get("items", "").strip() or None,
        data.get("weight") or None,
        data.get("metal", "").strip() or None,
        data.get("money_lent") or None,
        data.get("interest_rate") or None,
        data.get("loan_date", "").strip() or None,
        data.get("loan_time", "").strip() or None,
        data.get("notes", "").strip() or None,
        data.get("probable_close_date", "").strip() or None,
        data.get("hard_deadline", "").strip() or None,
        data.get("address_proof") or None,
        data.get("lending_video") or None,
    )

    conn = get_db()
    if USE_PG:
        cur = db_execute(conn,
            """
            INSERT INTO cases
                (user_id, name, father_name, address, mobile, items, weight, metal,
                 money_lent, interest_rate, loan_date, loan_time, notes,
                 probable_close_date, hard_deadline, address_proof, lending_video)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """, values)
        case_id = cur.fetchone()["id"]
    else:
        cur = db_execute(conn,
            """
            INSERT INTO cases
                (user_id, name, father_name, address, mobile, items, weight, metal,
                 money_lent, interest_rate, loan_date, loan_time, notes,
                 probable_close_date, hard_deadline, address_proof, lending_video)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
        case_id = cur.lastrowid
    conn.commit()
    conn.close()
    audit("case_create", "case", case_id, {"name": data.get("name")})
    record_case_history(case_id, "created", {"name": data.get("name")})
    return jsonify({"success": True, "id": case_id}), 201


@app.route("/api/cases", methods=["GET"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def get_cases(_user):
    q         = request.args.get("q",         "").strip()
    metal     = request.args.get("metal",     "").strip()
    d_from    = request.args.get("from",      "").strip()
    d_to      = request.args.get("to",        "").strip()
    amt_min   = request.args.get("amt_min",   "").strip()
    amt_max   = request.args.get("amt_max",   "").strip()
    amt_exact = request.args.get("amt_exact", "").strip()
    status    = request.args.get("status",    "").strip()

    sql    = f"SELECT {_LIST_COLS} FROM cases WHERE 1=1"
    params = []
    # Tenant scoping (admin can pass ?tenant=ID; defaults to all)
    if _user["role"] == "user":
        sql += " AND user_id = ?"; params.append(_user["id"])
    else:
        t = request.args.get("tenant", "").strip()
        if t:
            sql += " AND user_id = ?"; params.append(int(t))

    if q:
        like_op = "ILIKE" if USE_PG else "LIKE"
        sql += f" AND (name {like_op} ? OR father_name {like_op} ? OR items {like_op} ? OR address {like_op} ? OR mobile {like_op} ? OR CAST(id AS TEXT) = ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like, like, q])
    if metal:     sql += " AND LOWER(metal) = LOWER(?)"; params.append(metal)
    if d_from:    sql += " AND loan_date >= ?";          params.append(d_from)
    if d_to:      sql += " AND loan_date <= ?";          params.append(d_to)
    if amt_exact: sql += " AND money_lent = ?";          params.append(float(amt_exact))
    else:
        if amt_min: sql += " AND money_lent >= ?"; params.append(float(amt_min))
        if amt_max: sql += " AND money_lent <= ?"; params.append(float(amt_max))
    if status: sql += " AND status = ?"; params.append(status)
    sql += " ORDER BY id DESC"

    conn = get_db()
    cur  = db_execute(conn, sql, params)
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/cases/<int:case_id>", methods=["GET"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def get_case(_user, case_id):
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))
    row  = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Case not found"}), 404
    row = dict(row)
    if _user["role"] == "user" and row.get("user_id") != _user["id"]:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(row)


@app.route("/api/cases/<int:case_id>/close", methods=["POST"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def close_case(_user, case_id):
    data            = request.get_json() or {}
    amount_received = data.get("amount_received")
    closing_video   = data.get("closing_video") or None
    closed_at       = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_db()
    # Authorise
    cur = db_execute(conn, "SELECT user_id FROM cases WHERE id = ?", (case_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Case not found"}), 404
    if _user["role"] == "user" and row["user_id"] != _user["id"]:
        conn.close()
        return jsonify({"error": "Forbidden"}), 403

    db_execute(conn,
        "UPDATE cases SET status = 'closed', closed_at = ?, amount_received = ?, closing_video = ? WHERE id = ?",
        (closed_at, amount_received, closing_video, case_id))
    conn.commit()
    conn.close()
    audit("case_close", "case", case_id, {"amount": amount_received})
    record_case_history(case_id, "closed", {"amount_received": amount_received, "closed_at": closed_at})
    return jsonify({"success": True})


@app.route("/api/cases/<int:case_id>/bad-debt", methods=["POST"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def mark_bad_debt(_user, case_id):
    conn = get_db()
    cur = db_execute(conn, "SELECT user_id, status FROM cases WHERE id = ?", (case_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Case not found"}), 404
    if _user["role"] == "user" and row["user_id"] != _user["id"]:
        conn.close()
        return jsonify({"error": "Forbidden"}), 403
    if row["status"] != "open":
        conn.close()
        return jsonify({"error": "Only open cases can be marked bad debt"}), 400

    db_execute(conn, "UPDATE cases SET status = 'bad_debt' WHERE id = ?", (case_id,))
    conn.commit()
    conn.close()
    audit("case_bad_debt", "case", case_id)
    record_case_history(case_id, "marked_bad_debt", None)
    return jsonify({"success": True})


# ── Editable fields for PATCH (excludes status/closed_at/amount_received and media) ──
_EDITABLE_FIELDS = (
    "name", "father_name", "address", "mobile",
    "items", "weight", "metal",
    "money_lent", "interest_rate",
    "loan_date", "loan_time", "notes",
    "probable_close_date", "hard_deadline",
)


@app.route("/api/cases/<int:case_id>", methods=["PATCH"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def edit_case(_user, case_id):
    """Edit a case. Diffs each field, writes a history entry with the diff."""
    data = request.get_json() or {}
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))
    row  = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Case not found"}), 404
    row = dict(row)
    if _user["role"] == "user" and row.get("user_id") != _user["id"]:
        conn.close()
        return jsonify({"error": "Forbidden"}), 403

    fields, params, changes = [], [], {}
    for k in _EDITABLE_FIELDS:
        if k not in data:
            continue
        new_v = data.get(k)
        if isinstance(new_v, str):
            new_v = new_v.strip() or None
        if new_v == "":
            new_v = None
        old_v = row.get(k)
        # Normalise numeric strings vs floats for comparison
        try:
            if old_v is not None and new_v is not None and k in ("weight", "money_lent", "interest_rate"):
                if float(old_v) == float(new_v):
                    continue
        except Exception:
            pass
        if (old_v or None) == (new_v or None):
            continue
        fields.append(f"{k} = ?")
        params.append(new_v)
        changes[k] = {"old": old_v, "new": new_v}

    if not fields:
        conn.close()
        return jsonify({"success": True, "changed": False, "message": "No changes detected"})

    params.append(case_id)
    db_execute(conn, f"UPDATE cases SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    audit("case_edit", "case", case_id, {"fields": list(changes.keys())})
    record_case_history(case_id, "updated", changes)
    return jsonify({"success": True, "changed": True, "changes": changes})


@app.route("/api/cases/<int:case_id>", methods=["DELETE"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def delete_case(_user, case_id):
    """Hard delete a case. Audit log retains the action; case_history is removed."""
    conn = get_db()
    cur  = db_execute(conn, "SELECT user_id, name FROM cases WHERE id = ?", (case_id,))
    row  = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Case not found"}), 404
    row = dict(row)
    if _user["role"] == "user" and row.get("user_id") != _user["id"]:
        conn.close()
        return jsonify({"error": "Forbidden"}), 403

    # Delete the per-case history and the case itself.
    db_execute(conn, "DELETE FROM case_history WHERE case_id = ?", (case_id,))
    db_execute(conn, "DELETE FROM cases WHERE id = ?", (case_id,))
    conn.commit()
    conn.close()
    audit("case_delete", "case", case_id, {"name": row.get("name")})
    return jsonify({"success": True})


@app.route("/api/cases/<int:case_id>/history", methods=["GET"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def case_history(_user, case_id):
    """Return chronological history for a case (newest first)."""
    conn = get_db()
    cur  = db_execute(conn, "SELECT user_id FROM cases WHERE id = ?", (case_id,))
    row  = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Case not found"}), 404
    row = dict(row)
    if _user["role"] == "user" and row.get("user_id") != _user["id"]:
        conn.close()
        return jsonify({"error": "Forbidden"}), 403

    cur = db_execute(conn,
        "SELECT h.id, h.case_id, h.actor_id, h.action, h.changes, h.created_at, u.name AS actor_name "
        "FROM case_history h LEFT JOIN users u ON u.id = h.actor_id "
        "WHERE h.case_id = ? ORDER BY h.id DESC",
        (case_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard (tenant-scoped)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/dashboard", methods=["GET"])
@require_auth(roles=["user", "admin"])
@require_active_subscription
def get_dashboard(_user):
    today = date_type.today()
    conn  = get_db()
    if _user["role"] == "user":
        cur = db_execute(conn, f"SELECT {_LIST_COLS} FROM cases WHERE user_id = ?", (_user["id"],))
    else:
        cur = db_execute(conn, f"SELECT {_LIST_COLS} FROM cases")
    all_cases = [dict(r) for r in cur.fetchall()]
    conn.close()

    open_cases     = [c for c in all_cases if c.get("status") == "open"]
    closed_cases   = [c for c in all_cases if c.get("status") == "closed"]
    bad_debt_cases = [c for c in all_cases if c.get("status") == "bad_debt"]

    total_principal = sum(c.get("money_lent") or 0 for c in all_cases)
    total_interest_generated = 0.0
    for c in closed_cases:
        if c.get("money_lent") and c.get("interest_rate") and c.get("loan_date"):
            try:
                end    = date_type.fromisoformat(str(c["closed_at"])[:10]) if c.get("closed_at") else today
                months = _calc_months(c["loan_date"], end)
                total_interest_generated += c["money_lent"] * (c["interest_rate"] / 100) * months
            except: pass
    total_amount_received = sum(c.get("amount_received") or 0 for c in closed_cases)
    bad_debt_amount       = sum(c.get("money_lent") or 0 for c in bad_debt_cases)
    outstanding_receivable = 0.0
    projected_interest     = 0.0
    for c in open_cases:
        p = c.get("money_lent") or 0
        outstanding_receivable += p
        if p and c.get("interest_rate") and c.get("loan_date"):
            try:
                months   = _calc_months(c["loan_date"])
                interest = p * (c["interest_rate"] / 100) * months
                outstanding_receivable += interest
                projected_interest     += interest
            except: pass

    recovered_fully = 0
    for c in closed_cases:
        if c.get("amount_received") and c.get("money_lent") and c.get("interest_rate") and c.get("loan_date"):
            try:
                end       = date_type.fromisoformat(str(c["closed_at"])[:10]) if c.get("closed_at") else today
                months    = _calc_months(c["loan_date"], end)
                total_due = c["money_lent"] * (1 + c["interest_rate"] / 100 * months)
                if c["amount_received"] >= total_due * 0.99: recovered_fully += 1
            except: pass
    recovery_rate = round(recovered_fully / len(closed_cases) * 100, 1) if closed_cases else 0

    durations = []
    for c in closed_cases:
        if c.get("loan_date") and c.get("closed_at"):
            try:
                d1 = date_type.fromisoformat(str(c["loan_date"])[:10])
                d2 = date_type.fromisoformat(str(c["closed_at"])[:10])
                durations.append((d2 - d1).days)
            except: pass
    avg_duration_days = round(sum(durations) / len(durations), 1) if durations else 0

    case_interests = []
    for c in closed_cases:
        if c.get("money_lent") and c.get("interest_rate") and c.get("loan_date"):
            try:
                end    = date_type.fromisoformat(str(c["closed_at"])[:10]) if c.get("closed_at") else today
                months = _calc_months(c["loan_date"], end)
                case_interests.append(c["money_lent"] * (c["interest_rate"] / 100) * months)
            except: pass
    avg_interest_per_case = round(sum(case_interests) / len(case_interests), 2) if case_interests else 0

    principal_closed = sum(c.get("money_lent") or 0 for c in closed_cases)
    effective_yield  = round(total_interest_generated / principal_closed * 100, 2) if principal_closed else 0
    avg_loan_amount = round(total_principal / len(all_cases), 2) if all_cases else 0
    denom_coll = total_amount_received + outstanding_receivable
    collection_efficiency = round(total_amount_received / denom_coll * 100, 1) if denom_coll else 0

    cases_with_amount = [c for c in all_cases if c.get("money_lent")]
    highest_case = lowest_case = None
    if cases_with_amount:
        hc = max(cases_with_amount, key=lambda c: c["money_lent"])
        lc = min(cases_with_amount, key=lambda c: c["money_lent"])
        highest_case = {"id": hc["id"], "name": hc["name"], "money_lent": hc["money_lent"], "status": hc.get("status")}
        lowest_case  = {"id": lc["id"], "name": lc["name"], "money_lent": lc["money_lent"], "status": lc.get("status")}

    name_counts = {}
    for c in all_cases:
        nm = (c.get("name") or "").strip().lower()
        if nm: name_counts[nm] = name_counts.get(nm, 0) + 1
    repeat_borrowers = sum(1 for v in name_counts.values() if v > 1)

    metal_map, wt_open = {}, {}
    for c in all_cases:
        m = c.get("metal") or "Unknown"
        metal_map.setdefault(m, {"metal": m, "cases": 0, "amount": 0})
        metal_map[m]["cases"]  += 1
        metal_map[m]["amount"] += c.get("money_lent") or 0
        if c.get("status") == "open":
            wt_open[m] = wt_open.get(m, 0) + (c.get("weight") or 0)
    metal_breakdown = sorted(metal_map.values(), key=lambda x: x["amount"], reverse=True)
    total_weight_open = round(sum(wt_open.values()), 2)
    weight_by_metal = [{"metal": k, "weight": round(v, 2)} for k, v in sorted(wt_open.items(), key=lambda x: x[1], reverse=True)]

    year_map = {}
    for c in all_cases:
        if c.get("loan_date"):
            yr = str(c["loan_date"])[:4]
            year_map.setdefault(yr, {"year": yr, "amount": 0, "cases": 0})
            year_map[yr]["amount"] += c.get("money_lent") or 0
            year_map[yr]["cases"]  += 1
    lending_by_year = sorted(year_map.values(), key=lambda x: x["year"])

    monthly = {}
    for i in range(11, -1, -1):
        m = today.month - i; y = today.year
        while m <= 0: m += 12; y -= 1
        monthly[f"{y}-{m:02d}"] = {"month": f"{y}-{m:02d}", "lent": 0, "recovered": 0, "opened": 0, "closed_count": 0}
    for c in all_cases:
        if c.get("loan_date"):
            k = str(c["loan_date"])[:7]
            if k in monthly: monthly[k]["lent"] += c.get("money_lent") or 0; monthly[k]["opened"] += 1
        if c.get("closed_at") and c.get("status") == "closed":
            k = str(c["closed_at"])[:7]
            if k in monthly: monthly[k]["recovered"] += c.get("amount_received") or 0; monthly[k]["closed_count"] += 1
    monthly_trend = sorted(monthly.values(), key=lambda x: x["month"])

    age_buckets = {"0_3": 0, "3_6": 0, "6_12": 0, "12_plus": 0}
    for c in open_cases:
        if c.get("loan_date"):
            try:
                days = (today - date_type.fromisoformat(str(c["loan_date"])[:10])).days
                mo = days / 30
                if   mo <= 3:  age_buckets["0_3"]    += 1
                elif mo <= 6:  age_buckets["3_6"]    += 1
                elif mo <= 12: age_buckets["6_12"]   += 1
                else:          age_buckets["12_plus"] += 1
            except: pass

    borrower_map = {}
    for c in all_cases:
        nm = (c.get("name") or "Unknown").strip()
        borrower_map.setdefault(nm, {"name": nm, "total_lent": 0, "cases": 0})
        borrower_map[nm]["total_lent"] += c.get("money_lent") or 0
        borrower_map[nm]["cases"]      += 1
    top_borrowers = sorted(borrower_map.values(), key=lambda x: x["total_lent"], reverse=True)[:5]

    open_dated = sorted([c for c in open_cases if c.get("loan_date")], key=lambda c: c["loan_date"])
    oldest_open = []
    for c in open_dated[:5]:
        try:
            loan_d = date_type.fromisoformat(str(c["loan_date"])[:10])
            days   = (today - loan_d).days
            months = _calc_months(c["loan_date"])
            accrued = (c.get("money_lent") or 0) * ((c.get("interest_rate") or 0) / 100) * months
            oldest_open.append({
                "id": c["id"], "name": c["name"], "loan_date": c["loan_date"],
                "days_open": days, "money_lent": c.get("money_lent"),
                "interest_rate": c.get("interest_rate"),
                "accrued_interest": round(accrued),
                "total_due": round((c.get("money_lent") or 0) + accrued),
            })
        except: pass

    overdue_count = 0
    for c in open_cases:
        if c.get("hard_deadline"):
            try:
                if date_type.fromisoformat(str(c["hard_deadline"])[:10]) < today:
                    overdue_count += 1
            except: pass

    return jsonify({
        "total_cases": len(all_cases), "open_cases": len(open_cases),
        "closed_cases": len(closed_cases), "bad_debt_cases": len(bad_debt_cases),
        "repeat_borrowers": repeat_borrowers, "overdue_cases": overdue_count,
        "total_principal": round(total_principal, 2),
        "total_interest_generated": round(total_interest_generated, 2),
        "total_amount_received": round(total_amount_received, 2),
        "outstanding_receivable": round(outstanding_receivable, 2),
        "projected_interest": round(projected_interest, 2),
        "bad_debt_amount": round(bad_debt_amount, 2),
        "avg_loan_amount": avg_loan_amount,
        "recovery_rate": recovery_rate, "effective_yield": effective_yield,
        "collection_efficiency": collection_efficiency,
        "bad_debt_rate_cases": round(len(bad_debt_cases) / len(all_cases) * 100, 1) if all_cases else 0,
        "bad_debt_rate_amount": round(bad_debt_amount / total_principal * 100, 1) if total_principal else 0,
        "avg_duration_days": avg_duration_days,
        "avg_interest_per_case": avg_interest_per_case,
        "highest_case": highest_case, "lowest_case": lowest_case,
        "total_weight_open": total_weight_open, "weight_by_metal": weight_by_metal,
        "metal_breakdown": metal_breakdown, "lending_by_year": lending_by_year,
        "monthly_trend": monthly_trend, "case_age_buckets": age_buckets,
        "top_borrowers": top_borrowers, "oldest_open_cases": oldest_open,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Billing (plans, orders, payments)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/billing/plans", methods=["GET"])
def list_plans():
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM plans WHERE active = ? ORDER BY price_inr ASC", (True if USE_PG else 1,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/billing/me", methods=["GET"])
@require_auth(roles=["user"])
def billing_me(_user):
    sub = get_active_subscription(_user["id"])
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC", (_user["id"],))
    payments = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"subscription": sub, "payments": payments, "provider": get_payment_provider().name})


@app.route("/api/billing/create-order", methods=["POST"])
@require_auth(roles=["user"])
def create_order(_user):
    data = request.get_json() or {}
    plan_code = (data.get("plan_code") or "").strip()
    if not plan_code:
        return jsonify({"error": "plan_code required"}), 400

    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM plans WHERE code = ? AND active = ?", (plan_code, True if USE_PG else 1))
    plan = cur.fetchone()
    if not plan:
        conn.close()
        return jsonify({"error": "Plan not found"}), 404
    plan = dict(plan)

    provider = get_payment_provider()
    order = provider.create_order(plan["price_inr"], "INR", {"user_id": _user["id"], "plan": plan_code})

    db_execute(conn,
        "INSERT INTO payments (user_id, plan_code, amount_inr, provider, provider_order_id, status, raw_payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_user["id"], plan_code, plan["price_inr"], provider.name,
         order["provider_order_id"], "created", json.dumps(order.get("raw") or {})))
    conn.commit()
    conn.close()

    audit("billing_create_order", "payment", order["provider_order_id"],
          {"plan": plan_code, "amount": plan["price_inr"], "provider": provider.name})
    return jsonify({
        "provider":          provider.name,
        "provider_order_id": order["provider_order_id"],
        "amount_inr":        plan["price_inr"],
        "plan":              plan,
        "requires_redirect": order.get("requires_redirect", False),
    }), 201


@app.route("/api/billing/verify", methods=["POST"])
@require_auth(roles=["user"])
def verify_payment_route(_user):
    """Verify a payment with the provider, mark subscription active."""
    data = request.get_json() or {}
    order_id = data.get("provider_order_id")
    if not order_id:
        return jsonify({"error": "provider_order_id required"}), 400

    conn = get_db()
    cur  = db_execute(conn,
        "SELECT * FROM payments WHERE provider_order_id = ? AND user_id = ?",
        (order_id, _user["id"]))
    payment = cur.fetchone()
    if not payment:
        conn.close()
        return jsonify({"error": "Order not found"}), 404
    payment = dict(payment)

    provider = get_payment_provider()
    result   = provider.verify_payment(data)
    if not result.get("ok"):
        db_execute(conn, "UPDATE payments SET status = ? WHERE id = ?", ("failed", payment["id"]))
        conn.commit(); conn.close()
        audit("billing_verify_failed", "payment", payment["id"])
        return jsonify({"error": "Payment verification failed"}), 400

    # Find plan + extend subscription
    cur  = db_execute(conn, "SELECT * FROM plans WHERE code = ?", (payment["plan_code"],))
    plan = dict(cur.fetchone())
    cur  = db_execute(conn,
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (_user["id"],))
    current = cur.fetchone()
    base = datetime.utcnow()
    if current:
        cur_exp = current["expires_at"]
        if isinstance(cur_exp, str):
            try: cur_exp = datetime.fromisoformat(cur_exp.replace("Z", "").replace(" ", "T")[:19])
            except: cur_exp = base
        if cur_exp and cur_exp > base and current["status"] == "active":
            base = cur_exp
    new_expires = base + timedelta(days=plan["duration_days"])

    cur = db_execute(conn,
        "INSERT INTO subscriptions (user_id, plan_code, status, expires_at) VALUES (?, ?, ?, ?)" +
        (" RETURNING id" if USE_PG else ""),
        (_user["id"], plan["code"], "active", new_expires))
    sub_id = cur.fetchone()["id"] if USE_PG else cur.lastrowid

    db_execute(conn,
        "UPDATE payments SET status = ?, provider_payment_id = ?, subscription_id = ?, paid_at = ?, raw_payload = ? WHERE id = ?",
        ("success", result["provider_payment_id"], sub_id, datetime.utcnow(),
         json.dumps(result.get("raw") or {}), payment["id"]))
    conn.commit()
    conn.close()
    audit("billing_payment_success", "payment", payment["id"],
          {"plan": plan["code"], "amount": payment["amount_inr"]})
    return jsonify({"success": True, "subscription_id": sub_id, "expires_at": new_expires.isoformat()})


@app.route("/api/billing/manual-payment", methods=["POST"])
@require_auth(roles=["admin"])
def manual_payment(_user):
    """Admin records an offline payment for a user."""
    data = request.get_json() or {}
    user_id   = data.get("user_id")
    plan_code = (data.get("plan_code") or "").strip()
    method    = (data.get("method") or "manual").strip()
    note      = (data.get("note") or "").strip()
    if not user_id or not plan_code:
        return jsonify({"error": "user_id and plan_code required"}), 400

    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM plans WHERE code = ?", (plan_code,))
    plan = cur.fetchone()
    if not plan:
        conn.close()
        return jsonify({"error": "Plan not found"}), 404
    plan = dict(plan)

    cur = db_execute(conn,
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
    current = cur.fetchone()
    base = datetime.utcnow()
    if current:
        cur_exp = current["expires_at"]
        if isinstance(cur_exp, str):
            try: cur_exp = datetime.fromisoformat(cur_exp.replace("Z", "").replace(" ", "T")[:19])
            except: cur_exp = base
        if cur_exp and cur_exp > base and current["status"] == "active":
            base = cur_exp
    new_expires = base + timedelta(days=plan["duration_days"])

    cur = db_execute(conn,
        "INSERT INTO subscriptions (user_id, plan_code, status, expires_at) VALUES (?, ?, ?, ?)" +
        (" RETURNING id" if USE_PG else ""),
        (user_id, plan_code, "active", new_expires))
    sub_id = cur.fetchone()["id"] if USE_PG else cur.lastrowid

    db_execute(conn,
        "INSERT INTO payments (user_id, subscription_id, plan_code, amount_inr, provider, provider_payment_id, status, method, note, paid_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, sub_id, plan_code, plan["price_inr"], "manual",
         f"MANUAL-{secrets.token_hex(6).upper()}", "success", method, note, datetime.utcnow()))
    conn.commit()
    conn.close()
    audit("billing_manual_payment", "user", user_id,
          {"plan": plan_code, "amount": plan["price_inr"], "method": method, "note": note})
    return jsonify({"success": True, "subscription_id": sub_id, "expires_at": new_expires.isoformat()})


# ──────────────────────────────────────────────────────────────────────────────
# Admin endpoints
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@require_auth(roles=["admin"])
def admin_list_users(_user):
    role = request.args.get("role", "").strip()
    conn = get_db()
    sql = "SELECT id, name, phone, username, email, role, status, created_at FROM users WHERE 1=1"
    params = []
    if role:
        sql += " AND role = ?"; params.append(role)
    sql += " ORDER BY id DESC"
    cur = db_execute(conn, sql, params)
    users = [dict(r) for r in cur.fetchall()]
    # Attach latest sub for tenant users
    for u in users:
        if u["role"] == "user":
            cur2 = db_execute(conn, "SELECT plan_code, status, expires_at FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (u["id"],))
            r = cur2.fetchone()
            u["subscription"] = dict(r) if r else None
    conn.close()
    return jsonify(users)


@app.route("/api/admin/users/<int:uid>/suspend", methods=["POST"])
@require_auth(roles=["admin"])
def admin_suspend_user(_user, uid):
    conn = get_db()
    db_execute(conn, "UPDATE users SET status = 'suspended' WHERE id = ? AND role = 'user'", (uid,))
    conn.commit(); conn.close()
    audit("user_suspend", "user", uid)
    return jsonify({"success": True})


@app.route("/api/admin/users/<int:uid>/activate", methods=["POST"])
@require_auth(roles=["admin"])
def admin_activate_user(_user, uid):
    conn = get_db()
    db_execute(conn, "UPDATE users SET status = 'active' WHERE id = ? AND role = 'user'", (uid,))
    conn.commit(); conn.close()
    audit("user_activate", "user", uid)
    return jsonify({"success": True})


@app.route("/api/admin/create-admin", methods=["POST"])
@require_auth(roles=["admin"])
def super_create_admin(_user):
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    uname = (data.get("username") or "").strip()
    pwd  = data.get("password") or ""
    if not (name and uname and pwd):
        return jsonify({"error": "name, username, password required"}), 400
    conn = get_db()
    cur = db_execute(conn, "SELECT id FROM users WHERE username = ?", (uname,))
    if cur.fetchone():
        conn.close()
        return jsonify({"error": "Username already in use"}), 409
    db_execute(conn,
        "INSERT INTO users (name, username, password_hash, role, status) VALUES (?, ?, ?, ?, ?)",
        (name, uname, generate_password_hash(pwd, method='pbkdf2:sha256'), "admin", "active"))
    conn.commit(); conn.close()
    audit("admin_create", "user", uname)
    return jsonify({"success": True}), 201


@app.route("/api/admin/audit", methods=["GET"])
@require_auth(roles=["admin"])
def admin_audit(_user):
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 500")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/admin/payments", methods=["GET"])
@require_auth(roles=["admin"])
def admin_payments(_user):
    conn = get_db()
    cur  = db_execute(conn,
        "SELECT p.*, u.name AS user_name, u.phone AS user_phone "
        "FROM payments p LEFT JOIN users u ON u.id = p.user_id ORDER BY p.id DESC LIMIT 500")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/admin/plans", methods=["GET", "POST"])
@require_auth(roles=["admin"])
def admin_plans(_user):
    conn = get_db()
    if request.method == "POST":
        data = request.get_json() or {}
        code = data.get("code"); name = data.get("name")
        price = data.get("price_inr"); days = data.get("duration_days")
        TRUE_VAL  = True  if USE_PG else 1
        FALSE_VAL = False if USE_PG else 0
        active = TRUE_VAL if data.get("active", True) else FALSE_VAL
        if not (code and name and price is not None and days):
            conn.close()
            return jsonify({"error": "code, name, price_inr, duration_days required"}), 400
        cur = db_execute(conn, "SELECT id FROM plans WHERE code = ?", (code,))
        if cur.fetchone():
            db_execute(conn,
                "UPDATE plans SET name = ?, price_inr = ?, duration_days = ?, active = ? WHERE code = ?",
                (name, price, days, active, code))
        else:
            db_execute(conn,
                "INSERT INTO plans (code, name, price_inr, duration_days, is_trial, active) VALUES (?, ?, ?, ?, ?, ?)",
                (code, name, price, days, FALSE_VAL, active))
        conn.commit()
        audit("plan_upsert", "plan", code, {"price": price, "days": days})

    cur = db_execute(conn, "SELECT * FROM plans ORDER BY price_inr ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/admin/users/<int:uid>", methods=["PUT"])
@require_auth(roles=["admin"])
def admin_update_user(_user, uid):
    """Edit a user's name / phone / email / username. Admin can only edit role=user."""
    data = request.get_json() or {}
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM users WHERE id = ?", (uid,))
    row  = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "User not found"}), 404
    row = dict(row)
    if _user["role"] == "admin" and row["role"] != "user":
        conn.close()
        return jsonify({"error": "Admins can only edit lenders"}), 403

    fields = []
    params = []
    for k in ("name", "phone", "email", "username"):
        if k in data:
            v = (data.get(k) or "").strip() or None
            fields.append(f"{k} = ?")
            params.append(v)
    if not fields:
        conn.close()
        return jsonify({"error": "Nothing to update"}), 400
    params.append(uid)

    try:
        db_execute(conn, f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    audit("user_update", "user", uid, {k: data.get(k) for k in ("name", "phone", "email", "username") if k in data})
    return jsonify({"success": True})


@app.route("/api/admin/subscription/<int:user_id>/extend", methods=["POST"])
@require_auth(roles=["admin"])
def admin_extend_sub(_user, user_id):
    """Manually extend / adjust a tenant's subscription expiry by N days."""
    data = request.get_json() or {}
    days = int(data.get("days") or 0)
    if days == 0:
        return jsonify({"error": "days required (positive to extend, negative to shorten)"}), 400
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
    sub  = cur.fetchone()
    if not sub:
        conn.close()
        return jsonify({"error": "No subscription found"}), 404
    sub = dict(sub)
    exp = sub["expires_at"]
    if isinstance(exp, str):
        try: exp = datetime.fromisoformat(exp.replace("Z", "").replace(" ", "T")[:19])
        except: exp = datetime.utcnow()
    new_exp = exp + timedelta(days=days)
    db_execute(conn,
        "UPDATE subscriptions SET expires_at = ?, status = ? WHERE id = ?",
        (new_exp, "active", sub["id"]))
    conn.commit(); conn.close()
    audit("sub_extend", "subscription", sub["id"], {"days": days, "user_id": user_id})
    return jsonify({"success": True, "expires_at": new_exp.isoformat()})


@app.route("/api/admin/admins", methods=["GET"])
@require_auth(roles=["admin"])
def admin_list_admins(_user):
    conn = get_db()
    cur  = db_execute(conn,
        "SELECT id, name, username, email, role, status, created_at FROM users "
        "WHERE role = 'admin' ORDER BY id ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/admin/charts", methods=["GET"])
@require_auth(roles=["admin"])
def admin_charts(_user):
    """Aggregated analytics for admin/super-admin overview dashboard."""
    today = date_type.today()
    conn  = get_db()

    cur = db_execute(conn, "SELECT * FROM payments WHERE status = 'success'")
    payments = [dict(r) for r in cur.fetchall()]
    cur = db_execute(conn, "SELECT created_at FROM users WHERE role = 'user'")
    user_rows = [dict(r) for r in cur.fetchall()]
    cur = db_execute(conn, "SELECT plan_code, status, expires_at FROM subscriptions")
    subs = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Last 12 months window
    months = {}
    for i in range(11, -1, -1):
        m, y = today.month - i, today.year
        while m <= 0: m += 12; y -= 1
        key = f"{y}-{m:02d}"
        months[key] = {"month": key, "revenue": 0, "signups": 0}

    for p in payments:
        ts = p.get("paid_at") or p.get("created_at")
        if not ts: continue
        key = str(ts)[:7]
        if key in months:
            months[key]["revenue"] += float(p.get("amount_inr") or 0)

    for u in user_rows:
        ts = u.get("created_at")
        if not ts: continue
        key = str(ts)[:7]
        if key in months:
            months[key]["signups"] += 1

    plan_counts = {}
    status_counts = {"trial": 0, "active": 0, "expired": 0}
    now = datetime.utcnow()
    for s in subs:
        plan_counts[s["plan_code"]] = plan_counts.get(s["plan_code"], 0) + 1
        exp = s["expires_at"]
        if isinstance(exp, str):
            try: exp = datetime.fromisoformat(exp.replace("Z", "").replace(" ", "T")[:19])
            except: exp = now
        if exp < now:
            status_counts["expired"] += 1
        elif s["status"] == "trial":
            status_counts["trial"] += 1
        else:
            status_counts["active"] += 1

    revenue_by_plan = {}
    for p in payments:
        c = p.get("plan_code") or "unknown"
        revenue_by_plan[c] = revenue_by_plan.get(c, 0) + float(p.get("amount_inr") or 0)

    return jsonify({
        "monthly":           sorted(months.values(), key=lambda x: x["month"]),
        "plan_counts":       plan_counts,
        "status_counts":     status_counts,
        "revenue_by_plan":   revenue_by_plan,
        "total_revenue":     round(sum(float(p.get("amount_inr") or 0) for p in payments), 2),
        "total_signups":     len(user_rows),
    })


@app.route("/api/admin/stats", methods=["GET"])
@require_auth(roles=["admin"])
def admin_stats(_user):
    conn = get_db()
    def n(sql, params=()):
        cur = db_execute(conn, sql, params); r = cur.fetchone()
        if not r: return 0
        d = dict(r)
        return list(d.values())[0]
    out = {
        "total_users":     n("SELECT COUNT(*) AS n FROM users WHERE role = 'user'"),
        "total_admins":    n("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"),
        "active_subs":     n("SELECT COUNT(*) AS n FROM subscriptions WHERE status = 'active'"),
        "trial_subs":      n("SELECT COUNT(*) AS n FROM subscriptions WHERE status = 'trial'"),
        "total_cases":     n("SELECT COUNT(*) AS n FROM cases"),
        "total_revenue":   n("SELECT COALESCE(SUM(amount_inr),0) AS n FROM payments WHERE status = 'success'"),
        "pending_leads":   n("SELECT COUNT(*) AS n FROM leads"),
    }
    conn.close()
    return jsonify(out)


# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap & SPA
# ──────────────────────────────────────────────────────────────────────────────
init_db()


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  ◆  Prabhu Ventures — LoanTrack SaaS")
    print("  👉  http://localhost:8080")
    print(f"  🔑  Admin: {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    print(f"  📱  Founder phone: {FOUNDER_PHONE}  (use OTP login)")
    print(f"  💳  Payment provider: {get_payment_provider().name}")
    print(f"  📩  SMS provider:     {get_sms_provider().__class__.__name__}")
    print(f"  🗄   DB: " + ("PostgreSQL" if USE_PG else "SQLite (local)"))
    print("=" * 60 + "\n")
    app.run(debug=True, port=8080)
