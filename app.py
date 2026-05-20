import os
import sqlite3
from flask import Flask, request, jsonify, session, send_from_directory

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "lending-app-dev-key-change-in-prod")

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "lending.db"))


# ─── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            father_name   TEXT,
            items         TEXT,
            weight        REAL,
            metal         TEXT,
            money_lent    REAL,
            interest_rate REAL,
            loan_date     TEXT,
            loan_time     TEXT,
            notes         TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


# ─── Cases ─────────────────────────────────────────────────────────────────────

@app.route("/api/cases", methods=["POST"])
def add_case():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    if not data.get("name", "").strip():
        return jsonify({"error": "Borrower name is required"}), 400

    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO cases
            (name, father_name, items, weight, metal, money_lent, interest_rate, loan_date, loan_time, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("name", "").strip(),
            data.get("father_name", "").strip() or None,
            data.get("items", "").strip() or None,
            data.get("weight") or None,
            data.get("metal", "").strip() or None,
            data.get("money_lent") or None,
            data.get("interest_rate") or None,
            data.get("loan_date", "").strip() or None,
            data.get("loan_time", "").strip() or None,
            data.get("notes", "").strip() or None,
        ),
    )
    conn.commit()
    case_id = cur.lastrowid
    conn.close()
    return jsonify({"success": True, "id": case_id}), 201


@app.route("/api/cases", methods=["GET"])
def get_cases():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    q      = request.args.get("q", "").strip()
    metal  = request.args.get("metal", "").strip()
    d_from = request.args.get("from", "").strip()
    d_to   = request.args.get("to", "").strip()
    amt_min = request.args.get("amt_min", "").strip()
    amt_max = request.args.get("amt_max", "").strip()

    sql    = "SELECT * FROM cases WHERE 1=1"
    params = []

    if q:
        sql += " AND (name LIKE ? OR father_name LIKE ? OR items LIKE ? OR CAST(id AS TEXT) = ?)"
        like = f"%{q}%"
        params.extend([like, like, like, q])

    if metal:
        sql += " AND LOWER(metal) = LOWER(?)"
        params.append(metal)

    if d_from:
        sql += " AND loan_date >= ?"
        params.append(d_from)

    if d_to:
        sql += " AND loan_date <= ?"
        params.append(d_to)

    if amt_min:
        sql += " AND money_lent >= ?"
        params.append(float(amt_min))

    if amt_max:
        sql += " AND money_lent <= ?"
        params.append(float(amt_max))

    sql += " ORDER BY id DESC"

    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─── Serve SPA ─────────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    return send_from_directory("static", "index.html")


# ─── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("\n" + "=" * 52)
    print("  🏦  LoanTrack — Lending Management System")
    print("  👉  http://localhost:8080")
    print("  🔑  Login: admin / admin123")
    print("=" * 52 + "\n")
    app.run(debug=True, port=8080)
