import os
import sqlite3
from datetime import datetime, date as date_type
from flask import Flask, request, jsonify, session, send_from_directory

# ─── PostgreSQL if DATABASE_URL is set, else SQLite locally ────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "lending-app-dev-key-change-in-prod")
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024   # 64 MB — for base64 file uploads

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
DB_PATH = os.path.join(os.path.dirname(__file__), "lending.db")


# ─── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if USE_PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_execute(conn, sql, params=()):
    """Run a query — handles ? vs %s difference between SQLite and PostgreSQL."""
    if USE_PG:
        sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def _calc_months(loan_date_str, end_date=None):
    """
    Mirror of frontend calcMonths().
    Incomplete / current month always counts as a full month. Minimum 1.
    """
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


# ─── Lean column list for list endpoint (excludes heavy binary fields) ─────────
_LIST_COLS = (
    "id, name, father_name, address, mobile, items, weight, metal, "
    "money_lent, interest_rate, loan_date, loan_time, notes, status, "
    "closed_at, amount_received, probable_close_date, hard_deadline, created_at"
)


def init_db():
    conn = get_db()
    if USE_PG:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS cases (
                id                  SERIAL PRIMARY KEY,
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
        cur.execute("UPDATE cases SET status = 'open' WHERE status IS NULL")
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
        conn.commit()
        cur.close()
    else:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS cases (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
            "address TEXT", "mobile TEXT", "status TEXT DEFAULT 'open'",
            "closed_at TEXT", "amount_received REAL",
            "probable_close_date TEXT", "hard_deadline TEXT",
            "address_proof TEXT", "lending_video TEXT", "closing_video TEXT",
        ]:
            try:
                conn.execute(f"ALTER TABLE cases ADD COLUMN {col_def}")
            except Exception:
                pass
        conn.execute("UPDATE cases SET status = 'open' WHERE status IS NULL")
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
        conn.commit()
    conn.close()


# ─── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    if data.get("username") == ADMIN_USERNAME and data.get("password") == ADMIN_PASSWORD:
        session["logged_in"] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Invalid username or password"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/check-auth")
def check_auth():
    return jsonify({"authenticated": bool(session.get("logged_in"))})


# ─── Public: request access (landing-page lead capture) ────────────────────────

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
def get_leads():
    """Admin-only — view captured access requests."""
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM leads ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─── Cases ─────────────────────────────────────────────────────────────────────

@app.route("/api/cases", methods=["POST"])
def add_case():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    if not data.get("name", "").strip():
        return jsonify({"error": "Borrower name is required"}), 400

    values = (
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
                (name, father_name, address, mobile, items, weight, metal,
                 money_lent, interest_rate, loan_date, loan_time, notes,
                 probable_close_date, hard_deadline, address_proof, lending_video)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """, values)
        case_id = cur.fetchone()["id"]
    else:
        cur = db_execute(conn,
            """
            INSERT INTO cases
                (name, father_name, address, mobile, items, weight, metal,
                 money_lent, interest_rate, loan_date, loan_time, notes,
                 probable_close_date, hard_deadline, address_proof, lending_video)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
        case_id = cur.lastrowid

    conn.commit()
    conn.close()
    return jsonify({"success": True, "id": case_id}), 201


@app.route("/api/cases", methods=["GET"])
def get_cases():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    q         = request.args.get("q",         "").strip()
    metal     = request.args.get("metal",     "").strip()
    d_from    = request.args.get("from",      "").strip()
    d_to      = request.args.get("to",        "").strip()
    amt_min   = request.args.get("amt_min",   "").strip()
    amt_max   = request.args.get("amt_max",   "").strip()
    amt_exact = request.args.get("amt_exact", "").strip()
    status    = request.args.get("status",    "").strip()

    # Exclude heavy base64 columns from list view
    sql    = f"SELECT {_LIST_COLS} FROM cases WHERE 1=1"
    params = []

    if q:
        if USE_PG:
            sql += " AND (name ILIKE ? OR father_name ILIKE ? OR items ILIKE ? OR address ILIKE ? OR mobile ILIKE ? OR CAST(id AS TEXT) = ?)"
        else:
            sql += " AND (name LIKE ? OR father_name LIKE ? OR items LIKE ? OR address LIKE ? OR mobile LIKE ? OR CAST(id AS TEXT) = ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like, like, q])

    if metal:
        sql += " AND LOWER(metal) = LOWER(?)"
        params.append(metal)

    if d_from:
        sql += " AND loan_date >= ?"
        params.append(d_from)

    if d_to:
        sql += " AND loan_date <= ?"
        params.append(d_to)

    if amt_exact:
        sql += " AND money_lent = ?"
        params.append(float(amt_exact))
    else:
        if amt_min:
            sql += " AND money_lent >= ?"
            params.append(float(amt_min))
        if amt_max:
            sql += " AND money_lent <= ?"
            params.append(float(amt_max))

    if status:
        sql += " AND status = ?"
        params.append(status)

    sql += " ORDER BY id DESC"

    conn = get_db()
    cur  = db_execute(conn, sql, params)
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/cases/<int:case_id>", methods=["GET"])
def get_case(case_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur  = db_execute(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))
    row  = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Case not found"}), 404
    return jsonify(dict(row))


@app.route("/api/cases/<int:case_id>/close", methods=["POST"])
def close_case(case_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    data            = request.get_json() or {}
    amount_received = data.get("amount_received")
    closing_video   = data.get("closing_video") or None
    closed_at       = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_db()
    cur  = db_execute(conn,
        "UPDATE cases SET status = 'closed', closed_at = ?, amount_received = ?, closing_video = ? WHERE id = ?",
        (closed_at, amount_received, closing_video, case_id))

    if cur.rowcount == 0:
        conn.close()
        return jsonify({"error": "Case not found"}), 404

    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/cases/<int:case_id>/bad-debt", methods=["POST"])
def mark_bad_debt(case_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db()
    cur  = db_execute(conn,
        "UPDATE cases SET status = 'bad_debt' WHERE id = ? AND status = 'open'",
        (case_id,))

    if cur.rowcount == 0:
        conn.close()
        return jsonify({"error": "Case not found or not currently open"}), 404

    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ─── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/api/dashboard", methods=["GET"])
def get_dashboard():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    today = date_type.today()

    conn      = get_db()
    # Fetch only the columns needed for analytics (skip base64 blobs)
    cur       = db_execute(conn, f"SELECT {_LIST_COLS} FROM cases")
    all_cases = [dict(r) for r in cur.fetchall()]
    conn.close()

    open_cases     = [c for c in all_cases if c.get("status") == "open"]
    closed_cases   = [c for c in all_cases if c.get("status") == "closed"]
    bad_debt_cases = [c for c in all_cases if c.get("status") == "bad_debt"]

    # ── Money totals ──────────────────────────────────────────────────────────
    total_principal = sum(c.get("money_lent") or 0 for c in all_cases)

    total_interest_generated = 0.0
    for c in closed_cases:
        if c.get("money_lent") and c.get("interest_rate") and c.get("loan_date"):
            try:
                end     = date_type.fromisoformat(str(c["closed_at"])[:10]) if c.get("closed_at") else today
                months  = _calc_months(c["loan_date"], end)
                total_interest_generated += c["money_lent"] * (c["interest_rate"] / 100) * months
            except Exception:
                pass

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
            except Exception:
                pass

    # ── Rates & averages ──────────────────────────────────────────────────────
    recovered_fully = 0
    for c in closed_cases:
        if c.get("amount_received") and c.get("money_lent") and c.get("interest_rate") and c.get("loan_date"):
            try:
                end       = date_type.fromisoformat(str(c["closed_at"])[:10]) if c.get("closed_at") else today
                months    = _calc_months(c["loan_date"], end)
                total_due = c["money_lent"] * (1 + c["interest_rate"] / 100 * months)
                if c["amount_received"] >= total_due * 0.99:
                    recovered_fully += 1
            except Exception:
                pass
    recovery_rate = round(recovered_fully / len(closed_cases) * 100, 1) if closed_cases else 0

    durations = []
    for c in closed_cases:
        if c.get("loan_date") and c.get("closed_at"):
            try:
                d1 = date_type.fromisoformat(str(c["loan_date"])[:10])
                d2 = date_type.fromisoformat(str(c["closed_at"])[:10])
                durations.append((d2 - d1).days)
            except Exception:
                pass
    avg_duration_days = round(sum(durations) / len(durations), 1) if durations else 0

    case_interests = []
    for c in closed_cases:
        if c.get("money_lent") and c.get("interest_rate") and c.get("loan_date"):
            try:
                end    = date_type.fromisoformat(str(c["closed_at"])[:10]) if c.get("closed_at") else today
                months = _calc_months(c["loan_date"], end)
                case_interests.append(c["money_lent"] * (c["interest_rate"] / 100) * months)
            except Exception:
                pass
    avg_interest_per_case = round(sum(case_interests) / len(case_interests), 2) if case_interests else 0

    principal_closed = sum(c.get("money_lent") or 0 for c in closed_cases)
    effective_yield  = round(total_interest_generated / principal_closed * 100, 2) if principal_closed else 0

    avg_loan_amount = round(total_principal / len(all_cases), 2) if all_cases else 0

    denom_coll = total_amount_received + outstanding_receivable
    collection_efficiency = round(total_amount_received / denom_coll * 100, 1) if denom_coll else 0

    # ── Highest & lowest case by amount lent ──────────────────────────────────
    cases_with_amount = [c for c in all_cases if c.get("money_lent")]
    highest_case = lowest_case = None
    if cases_with_amount:
        hc = max(cases_with_amount, key=lambda c: c["money_lent"])
        lc = min(cases_with_amount, key=lambda c: c["money_lent"])
        highest_case = {"id": hc["id"], "name": hc["name"], "money_lent": hc["money_lent"], "status": hc.get("status")}
        lowest_case  = {"id": lc["id"], "name": lc["name"], "money_lent": lc["money_lent"], "status": lc.get("status")}

    # ── Repeat borrowers ──────────────────────────────────────────────────────
    name_counts = {}
    for c in all_cases:
        nm = (c.get("name") or "").strip().lower()
        if nm:
            name_counts[nm] = name_counts.get(nm, 0) + 1
    repeat_borrowers = sum(1 for v in name_counts.values() if v > 1)

    # ── Metal breakdown ───────────────────────────────────────────────────────
    metal_map  = {}
    wt_open    = {}
    for c in all_cases:
        metal = c.get("metal") or "Unknown"
        if metal not in metal_map:
            metal_map[metal] = {"metal": metal, "cases": 0, "amount": 0}
        metal_map[metal]["cases"]  += 1
        metal_map[metal]["amount"] += c.get("money_lent") or 0
        if c.get("status") == "open":
            wt_open[metal] = wt_open.get(metal, 0) + (c.get("weight") or 0)

    metal_breakdown = sorted(metal_map.values(), key=lambda x: x["amount"], reverse=True)
    total_weight_open = round(sum(wt_open.values()), 2)
    weight_by_metal   = [{"metal": k, "weight": round(v, 2)}
                         for k, v in sorted(wt_open.items(), key=lambda x: x[1], reverse=True)]

    # ── Lending by year ───────────────────────────────────────────────────────
    year_map = {}
    for c in all_cases:
        if c.get("loan_date"):
            try:
                yr = str(c["loan_date"])[:4]
                if yr not in year_map:
                    year_map[yr] = {"year": yr, "amount": 0, "cases": 0}
                year_map[yr]["amount"] += c.get("money_lent") or 0
                year_map[yr]["cases"]  += 1
            except Exception:
                pass
    lending_by_year = sorted(year_map.values(), key=lambda x: x["year"])

    # ── Monthly trend (last 12 months) ────────────────────────────────────────
    monthly = {}
    for i in range(11, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        key = f"{y}-{m:02d}"
        monthly[key] = {"month": key, "lent": 0, "recovered": 0, "opened": 0, "closed_count": 0}

    for c in all_cases:
        if c.get("loan_date"):
            key = str(c["loan_date"])[:7]
            if key in monthly:
                monthly[key]["lent"]   += c.get("money_lent") or 0
                monthly[key]["opened"] += 1
        if c.get("closed_at") and c.get("status") == "closed":
            key = str(c["closed_at"])[:7]
            if key in monthly:
                monthly[key]["recovered"]    += c.get("amount_received") or 0
                monthly[key]["closed_count"] += 1
    monthly_trend = sorted(monthly.values(), key=lambda x: x["month"])

    # ── Case age buckets (open cases) ─────────────────────────────────────────
    age_buckets = {"0_3": 0, "3_6": 0, "6_12": 0, "12_plus": 0}
    for c in open_cases:
        if c.get("loan_date"):
            try:
                days = (today - date_type.fromisoformat(str(c["loan_date"])[:10])).days
                mo   = days / 30
                if mo <= 3:
                    age_buckets["0_3"]    += 1
                elif mo <= 6:
                    age_buckets["3_6"]    += 1
                elif mo <= 12:
                    age_buckets["6_12"]   += 1
                else:
                    age_buckets["12_plus"] += 1
            except Exception:
                pass

    # ── Top 5 borrowers by total amount lent ─────────────────────────────────
    borrower_map = {}
    for c in all_cases:
        nm = (c.get("name") or "Unknown").strip()
        if nm not in borrower_map:
            borrower_map[nm] = {"name": nm, "total_lent": 0, "cases": 0}
        borrower_map[nm]["total_lent"] += c.get("money_lent") or 0
        borrower_map[nm]["cases"]      += 1
    top_borrowers = sorted(borrower_map.values(), key=lambda x: x["total_lent"], reverse=True)[:5]

    # ── Oldest 5 open cases ───────────────────────────────────────────────────
    open_dated = sorted([c for c in open_cases if c.get("loan_date")], key=lambda c: c["loan_date"])
    oldest_open = []
    for c in open_dated[:5]:
        try:
            loan_d  = date_type.fromisoformat(str(c["loan_date"])[:10])
            days    = (today - loan_d).days
            months  = _calc_months(c["loan_date"])
            accrued = 0.0
            if c.get("money_lent") and c.get("interest_rate"):
                accrued = c["money_lent"] * (c["interest_rate"] / 100) * months
            oldest_open.append({
                "id":               c["id"],
                "name":             c["name"],
                "loan_date":        c["loan_date"],
                "days_open":        days,
                "money_lent":       c.get("money_lent"),
                "interest_rate":    c.get("interest_rate"),
                "accrued_interest": round(accrued),
                "total_due":        round((c.get("money_lent") or 0) + accrued),
            })
        except Exception:
            pass

    # ── Overdue cases (hard_deadline passed, still open) ─────────────────────
    overdue_count = 0
    for c in open_cases:
        if c.get("hard_deadline"):
            try:
                if date_type.fromisoformat(str(c["hard_deadline"])[:10]) < today:
                    overdue_count += 1
            except Exception:
                pass

    return jsonify({
        # Counts
        "total_cases":       len(all_cases),
        "open_cases":        len(open_cases),
        "closed_cases":      len(closed_cases),
        "bad_debt_cases":    len(bad_debt_cases),
        "repeat_borrowers":  repeat_borrowers,
        "overdue_cases":     overdue_count,

        # Money
        "total_principal":           round(total_principal, 2),
        "total_interest_generated":  round(total_interest_generated, 2),
        "total_amount_received":     round(total_amount_received, 2),
        "outstanding_receivable":    round(outstanding_receivable, 2),
        "projected_interest":        round(projected_interest, 2),
        "bad_debt_amount":           round(bad_debt_amount, 2),
        "avg_loan_amount":           avg_loan_amount,

        # Rates
        "recovery_rate":          recovery_rate,
        "effective_yield":        effective_yield,
        "collection_efficiency":  collection_efficiency,
        "bad_debt_rate_cases":    round(len(bad_debt_cases) / len(all_cases) * 100, 1) if all_cases else 0,
        "bad_debt_rate_amount":   round(bad_debt_amount / total_principal * 100, 1) if total_principal else 0,
        "avg_duration_days":      avg_duration_days,
        "avg_interest_per_case":  avg_interest_per_case,

        # Notable
        "highest_case":      highest_case,
        "lowest_case":       lowest_case,

        # Weight
        "total_weight_open": total_weight_open,
        "weight_by_metal":   weight_by_metal,

        # Chart data
        "metal_breakdown":   metal_breakdown,
        "lending_by_year":   lending_by_year,
        "monthly_trend":     monthly_trend,
        "case_age_buckets":  age_buckets,
        "top_borrowers":     top_borrowers,
        "oldest_open_cases": oldest_open,
    })


# ─── Init DB on startup (works with both gunicorn and python3 app.py) ──────────
init_db()

# ─── Serve SPA ─────────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    return send_from_directory("static", "index.html")


# ─── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 52)
    print("  🏦  LoanTrack — Lending Management System")
    print("  👉  http://localhost:8080")
    print("  🔑  Login: admin / admin123")
    print("  🗄️   DB: " + ("PostgreSQL" if USE_PG else "SQLite (local)"))
    print("=" * 52 + "\n")
    app.run(debug=True, port=8080)
