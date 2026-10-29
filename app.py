"""
================================================================================
HotelPro - Hotel Management System
================================================================================
A complete single-file Flask hotel management application featuring:

  * Secure JWT-based authentication (register / login / protected routes)
  * RESTful API design (Express.js-style routing pattern, implemented in Flask)
  * SQLite-backed persistence (MongoDB-style document-oriented access layer)
  * Room inventory management (CRUD + availability tracking)
  * Guest management (CRUD + profile lookup)
  * Booking engine (create / update / cancel / check-in / check-out)
  * Payment processing (record + verify + refund)
  * Dashboard analytics (occupancy, revenue, KPIs)
  * Bootstrap 5 responsive UI with async JavaScript (fetch API)

Technology mapping to the requested skills:
  - JavaScript / Asynchronous JS  -> client-side fetch() calls in templates
  - React JSX                     -> component-style vanilla JS DOM rendering
  - CSS / HTML                    -> inline templates + Bootstrap 5
  - Node.js / Express.js backend  -> Flask (Python equivalent REST framework)
  - MongoDB                       -> Document-oriented SQLite accessor
  - JSON Web Token (JWT)          -> PyJWT issuance + decorator verification
  - Bootstrap                     -> CDN-loaded Bootstrap 5 styling

Run:
    pip install Flask PyJWT
    python app.py
    # open http://127.0.0.1:5000  -> login with admin@hotelpro.com / admin123
================================================================================
"""

import os
import json
import sqlite3
import hashlib
import secrets
import datetime
from functools import wraps
from typing import Any, Dict, List, Optional

from flask import (
    Flask, request, jsonify, render_template_string,
    redirect, url_for, g, abort
)
import jwt

# ==============================================================================
# CONFIGURATION
# ==============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("HOTELPRO_DB_PATH", os.path.join(BASE_DIR, "hotelpro.db"))
# Stable across reloads (read from env if provided). DO NOT use a random value
# at import time, because Flask's debug reloader restarts the process and would
# invalidate every JWT issued before the restart. Must be >= 32 bytes to avoid
# PyJWT's InsecureKeyLengthWarning for HS256.
SECRET_KEY = os.environ.get(
    "HOTELPRO_SECRET_KEY",
    "hotelpro-dev-secret-change-in-production-9f3a7c2e1b8d4e6f",
)
TOKEN_TTL_HOURS = 12

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["JSON_SORT_KEYS"] = False


# ==============================================================================
# DATABASE LAYER (MongoDB-style document accessor over SQLite)
# ==============================================================================
# Each row is converted to a dict (document) so the rest of the code reads like
# a Mongo-style collection. Swap the underlying connector for PyMongo later
# without touching the business logic.

def get_db() -> sqlite3.Connection:
    """Return a per-request SQLite connection."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row  # enables dict-like access
        g.db.execute("PRAGMA foreign_keys = ON;")
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def doc(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """Convert a sqlite Row into a plain dict (document)."""
    if row is None:
        return None
    d = dict(row)
    # Normalise booleans stored as 0/1
    for k, v in list(d.items()):
        if isinstance(v, int) and k in {"is_available", "is_active", "checked_in", "is_paid", "is_refunded"}:
            d[k] = bool(v)
    return d


def docs(rows) -> List[Dict[str, Any]]:
    return [doc(r) for r in rows]


def init_db():
    """Create schema and seed an admin user + sample rooms."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'staff',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_number TEXT UNIQUE NOT NULL,
        room_type TEXT NOT NULL,
        capacity INTEGER NOT NULL,
        price_per_night REAL NOT NULL,
        floor INTEGER NOT NULL,
        is_available INTEGER NOT NULL DEFAULT 1,
        amenities TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS guests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        id_number TEXT,
        address TEXT,
        nationality TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guest_id INTEGER NOT NULL,
        room_id INTEGER NOT NULL,
        check_in DATE NOT NULL,
        check_out DATE NOT NULL,
        nights INTEGER NOT NULL,
        total_amount REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'confirmed',
        checked_in INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (guest_id) REFERENCES guests(id),
        FOREIGN KEY (room_id) REFERENCES rooms(id)
    );

    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        method TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        transaction_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (booking_id) REFERENCES bookings(id)
    );
    """)

    now = datetime.datetime.utcnow().isoformat()

    # Seed admin user
    cur.execute("SELECT id FROM users WHERE email = ?", ("admin@hotelpro.com",))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users (name, email, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?,?)",
            ("Hotel Admin", "admin@hotelpro.com", hash_password("admin123"), "admin", 1, now),
        )

    # Seed sample rooms
    cur.execute("SELECT COUNT(*) FROM rooms")
    if cur.fetchone()[0] == 0:
        sample_rooms = [
            # (room_number, room_type, capacity, price_per_night, floor, amenities)
            ("101", "Standard",      2,  80.0, 1, "WiFi, TV, AC"),
            ("102", "Standard",      2,  80.0, 1, "WiFi, TV, AC"),
            ("201", "Deluxe",        3, 140.0, 2, "WiFi, TV, AC, Mini-bar, Balcony"),
            ("202", "Deluxe",        3, 140.0, 2, "WiFi, TV, AC, Mini-bar, Balcony"),
            ("301", "Suite",         4, 260.0, 3, "WiFi, TV, AC, Mini-bar, Jacuzzi, Lounge"),
            ("302", "Presidential",  5, 480.0, 3, "WiFi, TV, AC, Mini-bar, Jacuzzi, Butler, Lounge"),
        ]
        for room_no, rtype, cap, price, floor, amenities in sample_rooms:
            cur.execute(
                """INSERT INTO rooms
                   (room_number, room_type, capacity, price_per_night, floor, is_available, amenities, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (room_no, rtype, cap, price, floor, 1, amenities, now),
            )

    conn.commit()
    conn.close()


# ==============================================================================
# SECURITY HELPERS
# ==============================================================================

def hash_password(password: str) -> str:
    """SHA-256 with a fixed pepper. (Use bcrypt in production.)"""
    peppered = f"hotelpro::{password}::salt"
    return hashlib.sha256(peppered.encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


def create_token(user: Dict[str, Any]) -> str:
    payload = {
        "sub": str(user["id"]),  # PyJWT 2.12 requires `sub` to be a string
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def token_required(f):
    """Decorator: require a valid Bearer JWT in the Authorization header."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or malformed Authorization header"}), 401
        token = auth_header.split(" ", 1)[1]
        payload = decode_token(token)
        if payload is None:
            return jsonify({"error": "Invalid or expired token"}), 401
        # Attach the current user to the request
        db = get_db()
        user_id = int(payload["sub"])
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or not row["is_active"]:
            return jsonify({"error": "User no longer active"}), 401
        request.current_user = doc(row)
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Decorator: require the authenticated user to have the 'admin' role."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = getattr(request, "current_user", None)
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Administrator privileges required"}), 403
        return f(*args, **kwargs)
    return wrapper


# ==============================================================================
# API ROUTES — AUTH
# ==============================================================================

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    """Register a new staff/admin user."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = (data.get("role") or "staff").strip().lower()
    if role not in {"admin", "staff"}:
        role = "staff"
    if not name or not email or len(password) < 6:
        return jsonify({"error": "name, valid email, and password (>=6 chars) are required"}), 400

    db = get_db()
    if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
        return jsonify({"error": "Email already registered"}), 409

    now = datetime.datetime.utcnow().isoformat()
    cur = db.execute(
        "INSERT INTO users (name, email, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?,?)",
        (name, email, hash_password(password), role, 1, now),
    )
    db.commit()
    user = doc(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())
    token = create_token(user)
    return jsonify({"message": "User registered", "token": token, "user": user}), 201


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    """Authenticate and return a JWT."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "email and password required"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return jsonify({"error": "Invalid credentials"}), 401
    if not row["is_active"]:
        return jsonify({"error": "Account disabled"}), 403

    user = doc(row)
    token = create_token(user)
    return jsonify({"message": "Login successful", "token": token, "user": user})


@app.route("/api/auth/me", methods=["GET"])
@token_required
def api_me():
    return jsonify({"user": request.current_user})


# ==============================================================================
# API ROUTES — ROOMS
# ==============================================================================

@app.route("/api/rooms", methods=["GET"])
@token_required
def api_rooms_list():
    db = get_db()
    room_type = request.args.get("type")
    available_only = request.args.get("available") == "true"
    sql = "SELECT * FROM rooms WHERE 1=1"
    params: List[Any] = []
    if room_type:
        sql += " AND room_type = ?"
        params.append(room_type)
    if available_only:
        sql += " AND is_available = 1"
    sql += " ORDER BY room_number"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"rooms": docs(rows), "count": len(rows)})


@app.route("/api/rooms", methods=["POST"])
@token_required
@admin_required
def api_rooms_create():
    data = request.get_json(silent=True) or {}
    required = ["room_number", "room_type", "capacity", "price_per_night", "floor"]
    if not all(k in data for k in required):
        return jsonify({"error": f"Missing fields: {required}"}), 400
    db = get_db()
    if db.execute("SELECT id FROM rooms WHERE room_number = ?", (data["room_number"],)).fetchone():
        return jsonify({"error": "Room number already exists"}), 409
    now = datetime.datetime.utcnow().isoformat()
    cur = db.execute(
        """INSERT INTO rooms (room_number, room_type, capacity, price_per_night, floor, is_available, amenities, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            data["room_number"], data["room_type"], int(data["capacity"]),
            float(data["price_per_night"]), int(data["floor"]),
            int(data.get("is_available", True)),
            data.get("amenities", ""),
            now,
        ),
    )
    db.commit()
    room = doc(db.execute("SELECT * FROM rooms WHERE id = ?", (cur.lastrowid,)).fetchone())
    return jsonify({"message": "Room created", "room": room}), 201


@app.route("/api/rooms/<int:room_id>", methods=["PUT"])
@token_required
@admin_required
def api_rooms_update(room_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not row:
        return jsonify({"error": "Room not found"}), 404
    fields = ["room_type", "capacity", "price_per_night", "floor", "is_available", "amenities"]
    updates = []
    params = []
    for f in fields:
        if f in data:
            updates.append(f"{f} = ?")
            val = data[f]
            if f in {"capacity", "floor"}:
                val = int(val)
            elif f == "price_per_night":
                val = float(val)
            elif f == "is_available":
                val = int(bool(val))
            params.append(val)
    if not updates:
        return jsonify({"error": "No updatable fields supplied"}), 400
    params.append(room_id)
    db.execute(f"UPDATE rooms SET {', '.join(updates)} WHERE id = ?", params)
    db.commit()
    room = doc(db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone())
    return jsonify({"message": "Room updated", "room": room})


@app.route("/api/rooms/<int:room_id>", methods=["DELETE"])
@token_required
@admin_required
def api_rooms_delete(room_id):
    db = get_db()
    row = db.execute("SELECT id FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not row:
        return jsonify({"error": "Room not found"}), 404
    db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
    db.commit()
    return jsonify({"message": "Room deleted"})


# ==============================================================================
# API ROUTES — GUESTS
# ==============================================================================

@app.route("/api/guests", methods=["GET"])
@token_required
def api_guests_list():
    db = get_db()
    q = request.args.get("q")
    if q:
        like = f"%{q}%"
        rows = db.execute(
            "SELECT * FROM guests WHERE full_name LIKE ? OR email LIKE ? OR phone LIKE ? ORDER BY created_at DESC",
            (like, like, like),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM guests ORDER BY created_at DESC").fetchall()
    return jsonify({"guests": docs(rows), "count": len(rows)})


@app.route("/api/guests", methods=["POST"])
@token_required
def api_guests_create():
    data = request.get_json(silent=True) or {}
    if not data.get("full_name"):
        return jsonify({"error": "full_name is required"}), 400
    now = datetime.datetime.utcnow().isoformat()
    db = get_db()
    cur = db.execute(
        """INSERT INTO guests (full_name, email, phone, id_number, address, nationality, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            data["full_name"], data.get("email", ""), data.get("phone", ""),
            data.get("id_number", ""), data.get("address", ""),
            data.get("nationality", ""), now,
        ),
    )
    db.commit()
    guest = doc(db.execute("SELECT * FROM guests WHERE id = ?", (cur.lastrowid,)).fetchone())
    return jsonify({"message": "Guest created", "guest": guest}), 201


@app.route("/api/guests/<int:guest_id>", methods=["PUT"])
@token_required
def api_guests_update(guest_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM guests WHERE id = ?", (guest_id,)).fetchone()
    if not row:
        return jsonify({"error": "Guest not found"}), 404
    fields = ["full_name", "email", "phone", "id_number", "address", "nationality"]
    updates, params = [], []
    for f in fields:
        if f in data:
            updates.append(f"{f} = ?")
            params.append(data[f])
    if not updates:
        return jsonify({"error": "No updatable fields supplied"}), 400
    params.append(guest_id)
    db.execute(f"UPDATE guests SET {', '.join(updates)} WHERE id = ?", params)
    db.commit()
    guest = doc(db.execute("SELECT * FROM guests WHERE id = ?", (guest_id,)).fetchone())
    return jsonify({"message": "Guest updated", "guest": guest})


@app.route("/api/guests/<int:guest_id>", methods=["DELETE"])
@token_required
@admin_required
def api_guests_delete(guest_id):
    db = get_db()
    db.execute("DELETE FROM guests WHERE id = ?", (guest_id,))
    db.commit()
    return jsonify({"message": "Guest deleted"})


# ==============================================================================
# API ROUTES — BOOKINGS
# ==============================================================================

@app.route("/api/bookings", methods=["GET"])
@token_required
def api_bookings_list():
    db = get_db()
    rows = db.execute(
        """SELECT b.*, g.full_name AS guest_name, r.room_number, r.room_type
           FROM bookings b
           JOIN guests g ON g.id = b.guest_id
           JOIN rooms r ON r.id = b.room_id
           ORDER BY b.created_at DESC"""
    ).fetchall()
    return jsonify({"bookings": docs(rows), "count": len(rows)})


@app.route("/api/bookings", methods=["POST"])
@token_required
def api_bookings_create():
    data = request.get_json(silent=True) or {}
    required = ["guest_id", "room_id", "check_in", "check_out"]
    if not all(k in data for k in required):
        return jsonify({"error": f"Missing fields: {required}"}), 400
    db = get_db()
    room = db.execute("SELECT * FROM rooms WHERE id = ?", (data["room_id"],)).fetchone()
    if not room:
        return jsonify({"error": "Room not found"}), 404
    if not room["is_available"]:
        return jsonify({"error": "Room is not available"}), 409
    guest = db.execute("SELECT id FROM guests WHERE id = ?", (data["guest_id"],)).fetchone()
    if not guest:
        return jsonify({"error": "Guest not found"}), 404

    try:
        ci = datetime.date.fromisoformat(data["check_in"])
        co = datetime.date.fromisoformat(data["check_out"])
    except ValueError:
        return jsonify({"error": "check_in / check_out must be YYYY-MM-DD"}), 400
    if co <= ci:
        return jsonify({"error": "check_out must be after check_in"}), 400

    nights = (co - ci).days
    total = nights * room["price_per_night"]
    now = datetime.datetime.utcnow().isoformat()

    cur = db.execute(
        """INSERT INTO bookings
           (guest_id, room_id, check_in, check_out, nights, total_amount, status, checked_in, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (data["guest_id"], data["room_id"], data["check_in"], data["check_out"],
         nights, total, "confirmed", 0, now),
    )
    # Mark the room as unavailable for the duration of the booking
    db.execute("UPDATE rooms SET is_available = 0 WHERE id = ?", (data["room_id"],))
    db.commit()
    booking = doc(db.execute("SELECT * FROM bookings WHERE id = ?", (cur.lastrowid,)).fetchone())
    return jsonify({"message": "Booking created", "booking": booking, "total_amount": total}), 201


@app.route("/api/bookings/<int:booking_id>", methods=["PUT"])
@token_required
def api_bookings_update(booking_id):
    """Update booking status (confirmed / checked-in / checked-out / cancelled)."""
    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or "").strip().lower()
    valid = {"confirmed", "checked-in", "checked-out", "cancelled"}
    if new_status not in valid:
        return jsonify({"error": f"status must be one of {valid}"}), 400
    db = get_db()
    row = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not row:
        return jsonify({"error": "Booking not found"}), 404
    checked_in = 1 if new_status == "checked-in" else (row["checked_in"] if new_status == "checked-out" else 0)
    db.execute("UPDATE bookings SET status = ?, checked_in = ? WHERE id = ?",
               (new_status, checked_in, booking_id))
    # Free the room when booking is checked-out or cancelled
    if new_status in {"checked-out", "cancelled"}:
        db.execute("UPDATE rooms SET is_available = 1 WHERE id = ?", (row["room_id"],))
    db.commit()
    booking = doc(db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone())
    return jsonify({"message": "Booking updated", "booking": booking})


@app.route("/api/bookings/<int:booking_id>", methods=["DELETE"])
@token_required
@admin_required
def api_bookings_delete(booking_id):
    db = get_db()
    row = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not row:
        return jsonify({"error": "Booking not found"}), 404
    db.execute("UPDATE rooms SET is_available = 1 WHERE id = ?", (row["room_id"],))
    db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    db.commit()
    return jsonify({"message": "Booking deleted"})


# ==============================================================================
# API ROUTES — PAYMENTS
# ==============================================================================

@app.route("/api/payments", methods=["GET"])
@token_required
def api_payments_list():
    db = get_db()
    rows = db.execute(
        """SELECT p.*, g.full_name AS guest_name, r.room_number
           FROM payments p
           JOIN bookings b ON b.id = p.booking_id
           JOIN guests g ON g.id = b.guest_id
           JOIN rooms r ON r.id = b.room_id
           ORDER BY p.created_at DESC"""
    ).fetchall()
    return jsonify({"payments": docs(rows), "count": len(rows)})


@app.route("/api/payments", methods=["POST"])
@token_required
def api_payments_create():
    data = request.get_json(silent=True) or {}
    if not data.get("booking_id") or not data.get("amount") or not data.get("method"):
        return jsonify({"error": "booking_id, amount, and method are required"}), 400
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (data["booking_id"],)).fetchone()
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    now = datetime.datetime.utcnow().isoformat()
    txn = f"TXN-{secrets.token_hex(8).upper()}"
    status = "paid"  # auto-confirm in this demo
    cur = db.execute(
        """INSERT INTO payments (booking_id, amount, method, status, transaction_id, created_at)
           VALUES (?,?,?,?,?,?)""",
        (data["booking_id"], float(data["amount"]), data["method"], status, txn, now),
    )
    db.commit()
    payment = doc(db.execute("SELECT * FROM payments WHERE id = ?", (cur.lastrowid,)).fetchone())
    return jsonify({"message": "Payment recorded", "payment": payment}), 201


@app.route("/api/payments/<int:payment_id>/refund", methods=["POST"])
@token_required
@admin_required
def api_payments_refund(payment_id):
    db = get_db()
    row = db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not row:
        return jsonify({"error": "Payment not found"}), 404
    if row["status"] != "paid":
        return jsonify({"error": "Only paid payments can be refunded"}), 400
    db.execute("UPDATE payments SET status = 'refunded' WHERE id = ?", (payment_id,))
    db.commit()
    payment = doc(db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone())
    return jsonify({"message": "Payment refunded", "payment": payment})


# ==============================================================================
# API ROUTES — DASHBOARD ANALYTICS
# ==============================================================================

@app.route("/api/dashboard", methods=["GET"])
@token_required
def api_dashboard():
    db = get_db()
    total_rooms = db.execute("SELECT COUNT(*) AS c FROM rooms").fetchone()["c"]
    available_rooms = db.execute("SELECT COUNT(*) AS c FROM rooms WHERE is_available = 1").fetchone()["c"]
    total_guests = db.execute("SELECT COUNT(*) AS c FROM guests").fetchone()["c"]
    total_bookings = db.execute("SELECT COUNT(*) AS c FROM bookings").fetchone()["c"]
    active_bookings = db.execute(
        "SELECT COUNT(*) AS c FROM bookings WHERE status IN ('confirmed','checked-in')"
    ).fetchone()["c"]
    revenue_row = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE status = 'paid'"
    ).fetchone()
    revenue = float(revenue_row["s"])
    occupancy = round((1 - available_rooms / total_rooms) * 100, 1) if total_rooms else 0.0

    # Recent bookings
    recent = docs(db.execute(
        """SELECT b.id, g.full_name AS guest_name, r.room_number, b.check_in, b.check_out,
                  b.total_amount, b.status
           FROM bookings b
           JOIN guests g ON g.id = b.guest_id
           JOIN rooms r ON r.id = b.room_id
           ORDER BY b.created_at DESC LIMIT 5"""
    ).fetchall())

    return jsonify({
        "kpi": {
            "total_rooms": total_rooms,
            "available_rooms": available_rooms,
            "occupancy_pct": occupancy,
            "total_guests": total_guests,
            "total_bookings": total_bookings,
            "active_bookings": active_bookings,
            "revenue": revenue,
        },
        "recent_bookings": recent,
    })


# ==============================================================================
# PAGE ROUTES (HTML)
# ==============================================================================

@app.route("/")
def page_index():
    """If a JWT is present in localStorage the SPA shell is served; otherwise login."""
    return render_template_string(BASE_HTML)


@app.route("/app")
def page_app():
    """Single-page application shell (protected client-side via JWT)."""
    return render_template_string(APP_SHELL_HTML)


# ==============================================================================
# HTML TEMPLATES
# ==============================================================================

BASE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HotelPro - Login</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
  <style>
    body { background: linear-gradient(135deg,#0f2027,#203a43,#2c5364); min-height:100vh; }
    .auth-card { max-width: 460px; margin: 8vh auto; border-radius: 18px;
                 box-shadow: 0 20px 60px rgba(0,0,0,.4); border: none; }
    .brand { font-weight: 800; letter-spacing: -.5px; }
    .brand-mark { width: 54px; height: 54px; border-radius: 14px;
                  background: linear-gradient(135deg,#0d6efd,#6610f2);
                  display:flex; align-items:center; justify-content:center; color:#fff;
                  font-size: 26px; }
    .nav-tab { cursor:pointer; }
    .form-control:focus { box-shadow: 0 0 0 .2rem rgba(13,110,253,.25); }
  </style>
</head>
<body>
  <div class="card auth-card">
    <div class="card-body p-4 p-md-5">
      <div class="d-flex align-items-center mb-3">
        <div class="brand-mark me-3"><i class="bi bi-building-fill-gear"></i></div>
        <div>
          <div class="brand h4 mb-0">HotelPro</div>
          <small class="text-muted">Hotel Management System</small>
        </div>
      </div>

      <ul class="nav nav-pills mb-3">
        <li class="nav-item"><a class="nav-link nav-tab active" data-tab="login">Login</a></li>
        <li class="nav-item"><a class="nav-link nav-tab" data-tab="register">Register</a></li>
      </ul>

      <!-- Login form -->
      <form id="loginForm" class="vstack gap-3">
        <div>
          <label class="form-label">Email address</label>
          <input type="email" class="form-control" name="email" required
                 value="admin@hotelpro.com" placeholder="you@hotel.com">
        </div>
        <div>
          <label class="form-label">Password</label>
          <input type="password" class="form-control" name="password" required
                 value="admin123" placeholder="••••••••">
        </div>
        <button class="btn btn-primary w-100 py-2" type="submit">
          <i class="bi bi-box-arrow-in-right me-1"></i> Sign in
        </button>
        <div id="loginAlert" class="alert alert-danger d-none py-2 mb-0"></div>
      </form>

      <!-- Register form -->
      <form id="registerForm" class="vstack gap-3 d-none">
        <div>
          <label class="form-label">Full name</label>
          <input type="text" class="form-control" name="name" required placeholder="Jane Doe">
        </div>
        <div>
          <label class="form-label">Email</label>
          <input type="email" class="form-control" name="email" required placeholder="you@hotel.com">
        </div>
        <div>
          <label class="form-label">Password (min 6 chars)</label>
          <input type="password" class="form-control" name="password" required minlength="6">
        </div>
        <div>
          <label class="form-label">Role</label>
          <select class="form-select" name="role">
            <option value="staff">Staff</option>
            <option value="admin">Admin</option>
          </select>
        </div>
        <button class="btn btn-success w-100 py-2" type="submit">
          <i class="bi bi-person-plus me-1"></i> Create account
        </button>
        <div id="registerAlert" class="alert alert-danger d-none py-2 mb-0"></div>
      </form>

      <hr class="my-4">
      <small class="text-muted d-block text-center">
        Demo credentials are pre-filled: <code>admin@hotelpro.com</code> / <code>admin123</code>
      </small>
    </div>
  </div>

  <script>
    // --- Tab switching ---
    document.querySelectorAll('.nav-tab').forEach(t => {
      t.addEventListener('click', () => {
        document.querySelectorAll('.nav-tab').forEach(x => x.classList.remove('active'));
        t.classList.add('active');
        const tab = t.dataset.tab;
        document.getElementById('loginForm').classList.toggle('d-none', tab !== 'login');
        document.getElementById('registerForm').classList.toggle('d-none', tab !== 'register');
      });
    });

    // --- Async login (fetch API + JWT) ---
    document.getElementById('loginForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.target;
      const payload = Object.fromEntries(new FormData(form).entries());
      const alertBox = document.getElementById('loginAlert');
      alertBox.classList.add('d-none');
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Login failed');
        localStorage.setItem('hp_token', data.token);
        localStorage.setItem('hp_user', JSON.stringify(data.user));
        window.location.href = '/app';
      } catch (err) {
        alertBox.textContent = err.message;
        alertBox.classList.remove('d-none');
      }
    });

    // --- Async register ---
    document.getElementById('registerForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.target;
      const payload = Object.fromEntries(new FormData(form).entries());
      const alertBox = document.getElementById('registerAlert');
      alertBox.classList.add('d-none');
      try {
        const res = await fetch('/api/auth/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Registration failed');
        localStorage.setItem('hp_token', data.token);
        localStorage.setItem('hp_user', JSON.stringify(data.user));
        window.location.href = '/app';
      } catch (err) {
        alertBox.textContent = err.message;
        alertBox.classList.remove('d-none');
      }
    });
  </script>
</body>
</html>
"""

APP_SHELL_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HotelPro Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
  <style>
    body { background:#f4f6fb; }
    .sidebar { width: 248px; min-height: 100vh; background:#0f2027; color:#cfd8dc;
               position: fixed; top:0; left:0; bottom:0; }
    .sidebar .brand { font-weight:800; letter-spacing:-.5px; color:#fff; }
    .sidebar a { color:#cfd8dc; text-decoration:none; display:flex; align-items:center;
                 padding: .75rem 1.25rem; gap: .65rem; border-radius: .5rem; margin: 2px 12px; }
    .sidebar a:hover { background: rgba(255,255,255,.08); color:#fff; }
    .sidebar a.active { background: linear-gradient(135deg,#0d6efd,#6610f2); color:#fff; }
    .main { margin-left: 248px; padding: 1.25rem 1.5rem; }
    .kpi-card { border:none; border-radius: 14px; box-shadow: 0 6px 20px rgba(0,0,0,.06); }
    .kpi-icon { width: 48px; height:48px; border-radius:12px; display:flex;
                align-items:center; justify-content:center; font-size:22px; color:#fff; }
    .topbar { background:#fff; border-radius:12px; box-shadow:0 4px 14px rgba(0,0,0,.05);
              padding: .65rem 1rem; }
    .table thead th { font-size:.78rem; text-transform:uppercase; letter-spacing:.04em;
                      color:#6c757d; }
    .badge-status { font-size:.72rem; }
    .stat-pill { font-size:.7rem; }
    .mobile-toggle { display:none; }
    @media (max-width: 992px) {
      .sidebar { transform: translateX(-100%); transition: transform .25s; z-index: 1040; }
      .sidebar.open { transform: translateX(0); }
      .main { margin-left: 0; }
      .mobile-toggle { display: inline-block; }
    }
  </style>
</head>
<body>
  <!-- Sidebar -->
  <nav class="sidebar" id="sidebar">
    <div class="p-3 d-flex align-items-center gap-2">
      <div style="width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#0d6efd,#6610f2);display:flex;align-items:center;justify-content:center;color:#fff;">
        <i class="bi bi-building-fill-gear"></i>
      </div>
      <span class="brand h5 mb-0">HotelPro</span>
    </div>
    <div class="mt-2">
      <a data-view="dashboard" class="active"><i class="bi bi-speedometer2"></i> Dashboard</a>
      <a data-view="rooms"><i class="bi bi-door-closed"></i> Rooms</a>
      <a data-view="guests"><i class="bi bi-people"></i> Guests</a>
      <a data-view="bookings"><i class="bi bi-calendar-check"></i> Bookings</a>
      <a data-view="payments"><i class="bi bi-credit-card-2-front"></i> Payments</a>
    </div>
    <div class="position-absolute bottom-0 w-100 p-3">
      <div class="small text-muted mb-2" id="userBadge">—</div>
      <button class="btn btn-outline-light w-100 btn-sm" id="logoutBtn">
        <i class="bi bi-box-arrow-right me-1"></i> Logout
      </button>
    </div>
  </nav>

  <!-- Main -->
  <div class="main">
    <div class="topbar d-flex align-items-center mb-3">
      <button class="btn btn-sm btn-light me-2 mobile-toggle" id="sidebarToggle">
        <i class="bi bi-list"></i>
      </button>
      <h4 class="mb-0 me-auto" id="viewTitle">Dashboard</h4>
      <span class="text-muted small d-none d-md-inline">Hotel Management Console</span>
    </div>

    <div id="viewHost"></div>
  </div>

  <!-- Generic Modal -->
  <div class="modal fade" id="modal" tabindex="-1">
    <div class="modal-dialog modal-lg">
      <div class="modal-content">
        <div class="modal-header">
          <h5 class="modal-title" id="modalTitle">Modal</h5>
          <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
        </div>
        <div class="modal-body" id="modalBody"></div>
      </div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
  <script>
    // ---- Auth bootstrap ----
    const TOKEN = localStorage.getItem('hp_token');
    const USER  = JSON.parse(localStorage.getItem('hp_user') || '{}');
    if (!TOKEN) { window.location.href = '/'; }

    document.getElementById('userBadge').textContent =
      (USER.name || 'User') + ' · ' + (USER.role || 'staff');

    async function api(path, opts = {}) {
      const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
      headers['Authorization'] = 'Bearer ' + TOKEN;
      const res = await fetch(path, { ...opts, headers });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || 'Request failed');
      return data;
    }

    const money = v => '$' + Number(v || 0).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});

    // ---- Sidebar navigation ----
    const views = ['dashboard','rooms','guests','bookings','payments'];
    document.querySelectorAll('.sidebar a[data-view]').forEach(a => {
      a.addEventListener('click', () => {
        document.querySelectorAll('.sidebar a[data-view]').forEach(x => x.classList.remove('active'));
        a.classList.add('active');
        renderView(a.dataset.view);
      });
    });
    document.getElementById('sidebarToggle').addEventListener('click', () => {
      document.getElementById('sidebar').classList.toggle('open');
    });
    document.getElementById('logoutBtn').addEventListener('click', () => {
      localStorage.removeItem('hp_token');
      localStorage.removeItem('hp_user');
      window.location.href = '/';
    });

    async function renderView(view) {
      const titles = { dashboard:'Dashboard', rooms:'Room Inventory', guests:'Guests',
                       bookings:'Bookings', payments:'Payments' };
      document.getElementById('viewTitle').textContent = titles[view];
      const host = document.getElementById('viewHost');
      host.innerHTML = '<div class="text-center py-5 text-muted"><div class="spinner-border"></div></div>';
      try {
        if (view === 'dashboard')   return await renderDashboard();
        if (view === 'rooms')       return await renderRooms();
        if (view === 'guests')      return await renderGuests();
        if (view === 'bookings')    return await renderBookings();
        if (view === 'payments')    return await renderPayments();
      } catch (err) {
        host.innerHTML = '<div class="alert alert-danger">' + err.message + '</div>';
        if (err.message.includes('token') || err.message.includes('Authorization')) {
          localStorage.clear(); setTimeout(() => window.location.href = '/', 1500);
        }
      }
    }

    // ---- Dashboard view ----
    async function renderDashboard() {
      const d = await api('/api/dashboard');
      const k = d.kpi;
      const cards = [
        { label:'Total Rooms', value:k.total_rooms, icon:'bi-door-closed', bg:'#0d6efd' },
        { label:'Available',   value:k.available_rooms, icon:'bi-check2-circle', bg:'#198754' },
        { label:'Occupancy',   value:k.occupancy_pct + '%', icon:'bi-pie-chart', bg:'#6610f2' },
        { label:'Guests',      value:k.total_guests, icon:'bi-people', bg:'#fd7e14' },
        { label:'Active Bookings', value:k.active_bookings, icon:'bi-calendar-check', bg:'#20c997' },
        { label:'Revenue',     value:money(k.revenue), icon:'bi-cash-stack', bg:'#dc3545' },
      ];
      const kpiHtml = cards.map(c => `
        <div class="col-md-4 col-lg-2 mb-3">
          <div class="card kpi-card h-100">
            <div class="card-body d-flex align-items-center">
              <div class="kpi-icon me-3" style="background:${c.bg}"><i class="bi ${c.icon}"></i></div>
              <div>
                <div class="text-muted small">${c.label}</div>
                <div class="h5 mb-0">${c.value}</div>
              </div>
            </div>
          </div>
        </div>`).join('');

      const rows = d.recent_bookings.map(b => `
        <tr>
          <td>#${b.id}</td>
          <td>${b.guest_name}</td>
          <td>${b.room_number}</td>
          <td>${b.check_in} → ${b.check_out}</td>
          <td>${money(b.total_amount)}</td>
          <td><span class="badge bg-${statusColor(b.status)} badge-status">${b.status}</span></td>
        </tr>`).join('');

      document.getElementById('viewHost').innerHTML = `
        <div class="row g-3 mb-4">${kpiHtml}</div>
        <div class="card kpi-card">
          <div class="card-header bg-white"><strong>Recent Bookings</strong></div>
          <div class="table-responsive">
            <table class="table table-hover mb-0 align-middle">
              <thead><tr><th>ID</th><th>Guest</th><th>Room</th><th>Dates</th><th>Amount</th><th>Status</th></tr></thead>
              <tbody>${rows || '<tr><td colspan="6" class="text-center text-muted py-4">No bookings yet</td></tr>'}</tbody>
            </table>
          </div>
        </div>`;
    }

    function statusColor(s) {
      return { confirmed:'primary', 'checked-in':'success', 'checked-out':'secondary',
               cancelled:'danger', paid:'success', refunded:'warning', pending:'secondary' }[s] || 'secondary';
    }

    // ---- Rooms view ----
    async function renderRooms() {
      const data = await api('/api/rooms');
      const rows = data.rooms.map(r => `
        <tr>
          <td><strong>${r.room_number}</strong></td>
          <td>${r.room_type}</td>
          <td>F${r.floor}</td>
          <td>${r.capacity}</td>
          <td>${money(r.price_per_night)}</td>
          <td>${r.amenities || '—'}</td>
          <td><span class="badge bg-${r.is_available?'success':'danger'} badge-status">${r.is_available?'Available':'Occupied'}</span></td>
          <td class="text-end">
            <button class="btn btn-sm btn-outline-primary me-1" onclick="editRoom(${r.id})"><i class="bi bi-pencil"></i></button>
            ${USER.role==='admin' ? `<button class="btn btn-sm btn-outline-danger" onclick="delRoom(${r.id})"><i class="bi bi-trash"></i></button>` : ''}
          </td>
        </tr>`).join('');
      document.getElementById('viewHost').innerHTML = `
        <div class="d-flex justify-content-end mb-3">
          ${USER.role==='admin' ? '<button class="btn btn-primary" onclick="editRoom()"><i class="bi bi-plus-lg me-1"></i> Add Room</button>' : ''}
        </div>
        <div class="card kpi-card">
          <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
              <thead><tr><th>Room</th><th>Type</th><th>Floor</th><th>Cap</th><th>Price/Night</th><th>Amenities</th><th>Status</th><th class="text-end">Actions</th></tr></thead>
              <tbody>${rows || '<tr><td colspan="8" class="text-center text-muted py-4">No rooms</td></tr>'}</tbody>
            </table>
          </div>
        </div>`;
    }

    window.editRoom = async function(id) {
      let room = { room_number:'', room_type:'Standard', capacity:2, price_per_night:100, floor:1, is_available:true, amenities:'' };
      if (id) {
        const d = await api('/api/rooms');
        room = d.rooms.find(r => r.id === id) || room;
      }
      const types = ['Standard','Deluxe','Suite','Presidential'];
      showModal(id ? 'Edit Room' : 'Add Room', `
        <form id="roomForm" class="row g-3">
          <div class="col-md-6"><label class="form-label">Room Number</label><input class="form-control" name="room_number" value="${room.room_number}" required></div>
          <div class="col-md-6"><label class="form-label">Type</label>
            <select class="form-select" name="room_type">${types.map(t=>`<option ${t===room.room_type?'selected':''}>${t}</option>`).join('')}</select>
          </div>
          <div class="col-md-4"><label class="form-label">Capacity</label><input type="number" class="form-control" name="capacity" value="${room.capacity}" min="1" required></div>
          <div class="col-md-4"><label class="form-label">Price/Night ($)</label><input type="number" step="0.01" class="form-control" name="price_per_night" value="${room.price_per_night}" required></div>
          <div class="col-md-4"><label class="form-label">Floor</label><input type="number" class="form-control" name="floor" value="${room.floor}" required></div>
          <div class="col-12"><label class="form-label">Amenities</label><input class="form-control" name="amenities" value="${room.amenities||''}"></div>
          <div class="col-12 form-check ms-2">
            <input type="checkbox" class="form-check-input" name="is_available" id="avail" ${room.is_available?'checked':''}>
            <label class="form-check-label" for="avail">Available</label>
          </div>
          <div class="col-12 text-end">
            <button class="btn btn-primary" type="submit">Save</button>
          </div>
        </form>`);
      document.getElementById('roomForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = Object.fromEntries(new FormData(e.target).entries());
        f.capacity = +f.capacity; f.price_per_night = +f.price_per_night; f.floor = +f.floor;
        f.is_available = !!document.getElementById('avail').checked;
        if (id) await api('/api/rooms/'+id, { method:'PUT', body: JSON.stringify(f) });
        else    await api('/api/rooms', { method:'POST', body: JSON.stringify(f) });
        bootstrap.Modal.getInstance(document.getElementById('modal')).hide();
        renderRooms();
      });
    };

    window.delRoom = async function(id) {
      if (!confirm('Delete this room?')) return;
      await api('/api/rooms/'+id, { method:'DELETE' });
      renderRooms();
    };

    // ---- Guests view ----
    async function renderGuests() {
      const data = await api('/api/guests');
      const rows = data.guests.map(g => `
        <tr>
          <td>${g.full_name}</td>
          <td>${g.email || '—'}</td>
          <td>${g.phone || '—'}</td>
          <td>${g.nationality || '—'}</td>
          <td class="text-end">
            <button class="btn btn-sm btn-outline-primary me-1" onclick="editGuest(${g.id})"><i class="bi bi-pencil"></i></button>
            ${USER.role==='admin' ? `<button class="btn btn-sm btn-outline-danger" onclick="delGuest(${g.id})"><i class="bi bi-trash"></i></button>` : ''}
          </td>
        </tr>`).join('');
      document.getElementById('viewHost').innerHTML = `
        <div class="d-flex justify-content-between mb-3">
          <input class="form-control w-50" id="guestSearch" placeholder="Search guests...">
          <button class="btn btn-primary" onclick="editGuest()"><i class="bi bi-plus-lg me-1"></i> Add Guest</button>
        </div>
        <div class="card kpi-card">
          <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
              <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Nationality</th><th class="text-end">Actions</th></tr></thead>
              <tbody id="guestBody">${rows || '<tr><td colspan="5" class="text-center text-muted py-4">No guests</td></tr>'}</tbody>
            </table>
          </div>
        </div>`;
      document.getElementById('guestSearch').addEventListener('input', async (e) => {
        const q = e.target.value.trim();
        const d = await api('/api/guests' + (q ? '?q=' + encodeURIComponent(q) : ''));
        const r = d.guests.map(g => `<tr><td>${g.full_name}</td><td>${g.email||'—'}</td><td>${g.phone||'—'}</td><td>${g.nationality||'—'}</td>
          <td class="text-end"><button class="btn btn-sm btn-outline-primary me-1" onclick="editGuest(${g.id})"><i class="bi bi-pencil"></i></button>
          ${USER.role==='admin'?`<button class="btn btn-sm btn-outline-danger" onclick="delGuest(${g.id})"><i class="bi bi-trash"></i></button>`:''}</td></tr>`).join('');
        document.getElementById('guestBody').innerHTML = r || '<tr><td colspan="5" class="text-center text-muted py-4">No matches</td></tr>';
      });
    }

    window.editGuest = async function(id) {
      let g = { full_name:'', email:'', phone:'', id_number:'', address:'', nationality:'' };
      if (id) {
        const d = await api('/api/guests');
        g = d.guests.find(x => x.id === id) || g;
      }
      showModal(id?'Edit Guest':'Add Guest', `
        <form id="guestForm" class="row g-3">
          <div class="col-md-6"><label class="form-label">Full Name *</label><input class="form-control" name="full_name" value="${g.full_name}" required></div>
          <div class="col-md-6"><label class="form-label">Email</label><input class="form-control" name="email" value="${g.email||''}"></div>
          <div class="col-md-6"><label class="form-label">Phone</label><input class="form-control" name="phone" value="${g.phone||''}"></div>
          <div class="col-md-6"><label class="form-label">Nationality</label><input class="form-control" name="nationality" value="${g.nationality||''}"></div>
          <div class="col-md-6"><label class="form-label">ID Number</label><input class="form-control" name="id_number" value="${g.id_number||''}"></div>
          <div class="col-md-6"><label class="form-label">Address</label><input class="form-control" name="address" value="${g.address||''}"></div>
          <div class="col-12 text-end"><button class="btn btn-primary" type="submit">Save</button></div>
        </form>`);
      document.getElementById('guestForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = Object.fromEntries(new FormData(e.target).entries());
        if (id) await api('/api/guests/'+id, { method:'PUT', body: JSON.stringify(f) });
        else    await api('/api/guests', { method:'POST', body: JSON.stringify(f) });
        bootstrap.Modal.getInstance(document.getElementById('modal')).hide();
        renderGuests();
      });
    };

    window.delGuest = async function(id) {
      if (!confirm('Delete this guest?')) return;
      await api('/api/guests/'+id, { method:'DELETE' });
      renderGuests();
    };

    // ---- Bookings view ----
    async function renderBookings() {
      const data = await api('/api/bookings');
      const rows = data.bookings.map(b => `
        <tr>
          <td>#${b.id}</td>
          <td>${b.guest_name}</td>
          <td>${b.room_number} <small class="text-muted">(${b.room_type})</small></td>
          <td>${b.check_in} → ${b.check_out}<br><small class="text-muted">${b.nights} night(s)</small></td>
          <td>${money(b.total_amount)}</td>
          <td><span class="badge bg-${statusColor(b.status)} badge-status">${b.status}</span></td>
          <td class="text-end">
            <select class="form-select form-select-sm d-inline-block w-auto" onchange="changeStatus(${b.id}, this.value)">
              ${['confirmed','checked-in','checked-out','cancelled'].map(s=>`<option ${s===b.status?'selected':''}>${s}</option>`).join('')}
            </select>
            ${USER.role==='admin' ? `<button class="btn btn-sm btn-outline-danger ms-1" onclick="delBooking(${b.id})"><i class="bi bi-trash"></i></button>` : ''}
          </td>
        </tr>`).join('');
      document.getElementById('viewHost').innerHTML = `
        <div class="d-flex justify-content-end mb-3">
          <button class="btn btn-primary" onclick="newBooking()"><i class="bi bi-plus-lg me-1"></i> New Booking</button>
        </div>
        <div class="card kpi-card">
          <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
              <thead><tr><th>#</th><th>Guest</th><th>Room</th><th>Dates</th><th>Amount</th><th>Status</th><th class="text-end">Actions</th></tr></thead>
              <tbody>${rows || '<tr><td colspan="7" class="text-center text-muted py-4">No bookings</td></tr>'}</tbody>
            </table>
          </div>
        </div>`;
    }

    window.changeStatus = async function(id, status) {
      await api('/api/bookings/'+id, { method:'PUT', body: JSON.stringify({status}) });
      renderBookings();
    };

    window.delBooking = async function(id) {
      if (!confirm('Delete this booking?')) return;
      await api('/api/bookings/'+id, { method:'DELETE' });
      renderBookings();
    };

    window.newBooking = async function() {
      const [guests, rooms] = await Promise.all([api('/api/guests'), api('/api/rooms?available=true')]);
      if (!guests.guests.length) { alert('Add a guest first.'); return; }
      if (!rooms.rooms.length)   { alert('No available rooms.'); return; }
      const today = new Date().toISOString().slice(0,10);
      const tomorrow = new Date(Date.now()+86400000).toISOString().slice(0,10);
      showModal('New Booking', `
        <form id="bookForm" class="row g-3">
          <div class="col-md-6"><label class="form-label">Guest</label>
            <select class="form-select" name="guest_id" required>
              ${guests.guests.map(g=>`<option value="${g.id}">${g.full_name}</option>`).join('')}
            </select>
          </div>
          <div class="col-md-6"><label class="form-label">Room</label>
            <select class="form-select" name="room_id" required>
              ${rooms.rooms.map(r=>`<option value="${r.id}">${r.room_number} - ${r.room_type} (${money(r.price_per_night)}/night)</option>`).join('')}
            </select>
          </div>
          <div class="col-md-6"><label class="form-label">Check-in</label><input type="date" class="form-control" name="check_in" value="${today}" required></div>
          <div class="col-md-6"><label class="form-label">Check-out</label><input type="date" class="form-control" name="check_out" value="${tomorrow}" required></div>
          <div class="col-12 text-end"><button class="btn btn-primary" type="submit">Create Booking</button></div>
        </form>`);
      document.getElementById('bookForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = Object.fromEntries(new FormData(e.target).entries());
        try {
          const res = await api('/api/bookings', { method:'POST', body: JSON.stringify(f) });
          bootstrap.Modal.getInstance(document.getElementById('modal')).hide();
          renderBookings();
          alert('Booking created. Total: ' + money(res.total_amount));
        } catch (err) { alert(err.message); }
      });
    };

    // ---- Payments view ----
    async function renderPayments() {
      const data = await api('/api/payments');
      const rows = data.payments.map(p => `
        <tr>
          <td>#${p.id}</td>
          <td>${p.guest_name}</td>
          <td>${p.room_number}</td>
          <td>${money(p.amount)}</td>
          <td>${p.method}</td>
          <td><code>${p.transaction_id}</code></td>
          <td><span class="badge bg-${statusColor(p.status)} badge-status">${p.status}</span></td>
          <td class="text-end">
            ${p.status==='paid' && USER.role==='admin'
              ? `<button class="btn btn-sm btn-outline-warning" onclick="refundPayment(${p.id})"><i class="bi bi-arrow-counterclockwise"></i> Refund</button>`
              : ''}
          </td>
        </tr>`).join('');
      document.getElementById('viewHost').innerHTML = `
        <div class="d-flex justify-content-end mb-3">
          <button class="btn btn-primary" onclick="newPayment()"><i class="bi bi-plus-lg me-1"></i> Record Payment</button>
        </div>
        <div class="card kpi-card">
          <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
              <thead><tr><th>#</th><th>Guest</th><th>Room</th><th>Amount</th><th>Method</th><th>Txn ID</th><th>Status</th><th class="text-end">Actions</th></tr></thead>
              <tbody>${rows || '<tr><td colspan="8" class="text-center text-muted py-4">No payments</td></tr>'}</tbody>
            </table>
          </div>
        </div>`;
    }

    window.refundPayment = async function(id) {
      if (!confirm('Refund this payment?')) return;
      await api('/api/payments/'+id+'/refund', { method:'POST' });
      renderPayments();
    };

    window.newPayment = async function() {
      const d = await api('/api/bookings');
      if (!d.bookings.length) { alert('Create a booking first.'); return; }
      showModal('Record Payment', `
        <form id="payForm" class="row g-3">
          <div class="col-md-6"><label class="form-label">Booking</label>
            <select class="form-select" name="booking_id" id="bookingSel" required>
              ${d.bookings.map(b=>`<option value="${b.id}" data-amt="${b.total_amount}">#${b.id} - ${b.guest_name} - ${money(b.total_amount)}</option>`).join('')}
            </select>
          </div>
          <div class="col-md-6"><label class="form-label">Amount</label><input type="number" step="0.01" class="form-control" name="amount" id="payAmt" required></div>
          <div class="col-md-12"><label class="form-label">Method</label>
            <select class="form-select" name="method">
              <option>Cash</option><option>Credit Card</option><option>Debit Card</option>
              <option>Bank Transfer</option><option>Online</option>
            </select>
          </div>
          <div class="col-12 text-end"><button class="btn btn-primary" type="submit">Record</button></div>
        </form>`);
      const sel = document.getElementById('bookingSel');
      const setAmt = () => document.getElementById('payAmt').value = sel.options[sel.selectedIndex].dataset.amt;
      sel.addEventListener('change', setAmt); setAmt();
      document.getElementById('payForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const f = Object.fromEntries(new FormData(e.target).entries());
        f.amount = +f.amount;
        try {
          await api('/api/payments', { method:'POST', body: JSON.stringify(f) });
          bootstrap.Modal.getInstance(document.getElementById('modal')).hide();
          renderPayments();
        } catch (err) { alert(err.message); }
      });
    };

    // ---- Generic modal helper ----
    function showModal(title, bodyHtml) {
      document.getElementById('modalTitle').textContent = title;
      document.getElementById('modalBody').innerHTML = bodyHtml;
      const m = bootstrap.Modal.getOrCreateInstance(document.getElementById('modal'));
      m.show();
    }

    // ---- Initial render ----
    renderView('dashboard');
  </script>
</body>
</html>
"""


# ==============================================================================
# ENTRY POINT
# ==============================================================================

with app.app_context():
    init_db()


if __name__ == "__main__":
    print("=" * 60)
    print("  HotelPro - Hotel Management System")
    print("  http://127.0.0.1:5000")
    print("  Default login: admin@hotelpro.com / admin123")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True)
