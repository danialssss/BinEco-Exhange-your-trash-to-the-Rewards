import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from classifier import classify_image

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEVICE_KEY = os.getenv("DEVICE_KEY", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in the .env file")
if not DEVICE_KEY:
    raise RuntimeError("DEVICE_KEY is missing in the .env file")

DB_FILE = "bineco.db"
SESSION_SECONDS = 60
CATEGORY_POINTS = {"glass": 5, "plastic": 4, "paper": 3}

app = FastAPI(title="BinEco")


# ---------- database ----------
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username    TEXT,
            points      INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bins (
            bin_id    TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            status    TEXT NOT NULL DEFAULT 'active',
            lat       REAL,
            lon       REAL
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO bins (bin_id, device_id, status) VALUES ('BIN_001', 'ESP32_001', 'active')"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            telegram_id INTEGER NOT NULL,
            bin_id      TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
    """)
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN category TEXT")
    except sqlite3.OperationalError:
        pass  # the column already exists
        conn.execute("""
        CREATE TABLE IF NOT EXISTS disposal_events (
            event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     TEXT,
            telegram_id    INTEGER,
            bin_id         TEXT NOT NULL,
            device_id      TEXT NOT NULL,
            timestamp      TEXT NOT NULL,
            verified       INTEGER NOT NULL DEFAULT 0,
            points_awarded INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS classifications (
            telegram_id INTEGER PRIMARY KEY,
            category    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ---------- Telegram check ----------
def verify_init_data(init_data: str):
    """Returns the Telegram user (dict) if the data is genuine, otherwise None."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        return None
    if time.time() - int(pairs.get("auth_date", "0")) > 86400:
        return None
    try:
        return json.loads(pairs["user"])
    except Exception:
        return None


# ---------- request shapes ----------
class Body(BaseModel):
    init_data: str = ""
    tg_id: int = 0
    username: Optional[str] = None
    name: Optional[str] = None


class SessionBody(BaseModel):
    init_data: str = ""
    bin_id: str = ""
    category: str = ""


class DeviceEvent(BaseModel):
    device_id: str
    bin_id: str
    event: str = "trash_detected"


# ---------- pages and app endpoints ----------
@app.get("/")
def health():
    return {"ok": True, "message": "BinEco backend works"}


@app.get("/webapp")
def webapp():
    return FileResponse("index.html", headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/api/register")
def register(body: Body):
    return {"ok": True}


@app.post("/api/profile")
def profile(body: Body):
    tg_user = verify_init_data(body.init_data)
    if not tg_user:
        return {"ok": False, "reason": "bad auth"}
    tg_id = tg_user["id"]
    username = tg_user.get("username")

    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (telegram_id, username, points, created_at) VALUES (?, ?, 0, ?)",
        (tg_id, username, datetime.utcnow().isoformat()),
    )
    conn.execute("UPDATE users SET username = ? WHERE telegram_id = ?", (username, tg_id))
    conn.commit()
    conn.close()
    return {"ok": True}


CATEGORY_NAMES_RU = {"glass": "Стекло", "plastic": "Пластик", "paper": "Бумага"}


@app.post("/api/balance")
def balance(body: Body):
    tg_user = verify_init_data(body.init_data)
    if not tg_user:
        return {"ok": False, "reason": "bad auth"}

    conn = db()
    row = conn.execute(
        "SELECT points FROM users WHERE telegram_id = ?", (tg_user["id"],)
    ).fetchone()
    events = conn.execute(
        "SELECT e.timestamp, e.points_awarded, s.category "
        "FROM disposal_events e LEFT JOIN sessions s ON s.session_id = e.session_id "
        "WHERE e.telegram_id = ? AND e.verified = 1 ORDER BY e.event_id DESC LIMIT 20",
        (tg_user["id"],),
    ).fetchall()
    conn.close()

    ops = [
        {
            "trash_type": CATEGORY_NAMES_RU.get(e["category"], "Сдача мусора"),
            "timestamp": e["timestamp"] + "Z",
            "points": e["points_awarded"],
        }
        for e in events
    ]
    return {"ok": True, "balance": row["points"] if row else 0, "ops": ops}
@app.get("/api/list_bins")
def list_bins():
    return {"ok": True, "bins": []}


@app.get("/api/bin/{bin_id}")
def get_bin(bin_id: str):
    conn = db()
    row = conn.execute(
        "SELECT bin_id, status FROM bins WHERE bin_id = ?", (bin_id,)
    ).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "reason": "bin not found"}
    return {"ok": True, "bin_id": row["bin_id"], "status": row["status"]}


# ---------- sessions ----------
@app.post("/api/session/start")
def session_start(body: SessionBody):
    tg_user = verify_init_data(body.init_data)
    if not tg_user:
        return {"ok": False, "reason": "bad auth"}
    tg_id = tg_user["id"]

    conn = db()
    now = datetime.utcnow()
    now_s = now.isoformat()

    # the category comes from the AI's answer saved on the server
    photo = conn.execute(
        "SELECT category, created_at FROM classifications WHERE telegram_id = ?", (tg_id,)
    ).fetchone()
    if (not photo
            or photo["created_at"] < (now - timedelta(minutes=5)).isoformat()
            or photo["category"] not in CATEGORY_POINTS):
        conn.close()
        return {"ok": False, "reason": "take a photo of the trash first"}
    category = photo["category"]

    bin_row = conn.execute(
        "SELECT status FROM bins WHERE bin_id = ?", (body.bin_id,)
    ).fetchone()
    if not bin_row or bin_row["status"] != "active":
        conn.close()
        return {"ok": False, "reason": "bin not available"}

    conn.execute(
        "UPDATE sessions SET status = 'expired' WHERE status = 'active' AND expires_at < ?",
        (now_s,),
    )

    busy = conn.execute(
        "SELECT 1 FROM sessions WHERE bin_id = ? AND status = 'active' AND telegram_id != ?",
        (body.bin_id, tg_id),
    ).fetchone()
    if busy:
        conn.commit()
        conn.close()
        return {"ok": False, "reason": "bin is busy, try again in a minute"}

    conn.execute(
        "UPDATE sessions SET status = 'cancelled' WHERE telegram_id = ? AND status = 'active'",
        (tg_id,),
    )

    session_id = uuid.uuid4().hex
    expires_s = (now + timedelta(seconds=SESSION_SECONDS)).isoformat()

    conn.execute(
        "INSERT INTO sessions (session_id, telegram_id, bin_id, status, created_at, expires_at, category) "
        "VALUES (?, ?, ?, 'active', ?, ?, ?)",
        (session_id, tg_id, body.bin_id, now_s, expires_s, category),
    )
    conn.execute("DELETE FROM classifications WHERE telegram_id = ?", (tg_id,))   # one photo = one session
    conn.commit()
    conn.close()
    return {"ok": True, "session_id": session_id, "expires_in": SESSION_SECONDS, "category": category}

# ---------- the ESP32 reports a disposal ----------
@app.post("/api/device/event")
def device_event(body: DeviceEvent, x_device_key: str = Header(default="")):
    if not hmac.compare_digest(x_device_key, DEVICE_KEY):
        raise HTTPException(status_code=401, detail="bad device key")

    conn = db()
    now_s = datetime.utcnow().isoformat()

    bin_row = conn.execute(
        "SELECT 1 FROM bins WHERE bin_id = ? AND device_id = ? AND status = 'active'",
        (body.bin_id, body.device_id),
    ).fetchone()
    if not bin_row:
        conn.close()
        return {"ok": False, "led": "red", "reason": "unknown bin or device"}

    session = conn.execute(
        "SELECT session_id, telegram_id, category FROM sessions "
        "WHERE bin_id = ? AND status = 'active' AND expires_at >= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (body.bin_id, now_s),
    ).fetchone()

    if not session:
        conn.execute(
            "INSERT INTO disposal_events (bin_id, device_id, timestamp, verified, points_awarded) "
            "VALUES (?, ?, ?, 0, 0)",
            (body.bin_id, body.device_id, now_s),
        )
        conn.commit()
        conn.close()
        return {"ok": False, "led": "red", "reason": "no active session"}

    points = CATEGORY_POINTS.get(session["category"] or "", 0)
    if points == 0:
        conn.close()
        return {"ok": False, "led": "red", "reason": "session has no valid category"}

    closed = conn.execute(
        "UPDATE sessions SET status = 'completed' WHERE session_id = ? AND status = 'active'",
        (session["session_id"],),
    )
    if closed.rowcount != 1:
        conn.close()
        return {"ok": False, "led": "red", "reason": "session already used"}

    conn.execute(
        "UPDATE users SET points = points + ? WHERE telegram_id = ?",
        (points, session["telegram_id"]),
    )
    conn.execute(
        "INSERT INTO disposal_events "
        "(session_id, telegram_id, bin_id, device_id, timestamp, verified, points_awarded) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (session["session_id"], session["telegram_id"], body.bin_id,
         body.device_id, now_s, points),
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS classifications (
            telegram_id INTEGER PRIMARY KEY,
            category    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    return {"ok": True, "led": "green", "points": points, "category": session["category"]}
class ClassifyBody(BaseModel):
    init_data: str = ""
    image: str = ""          # the photo as base64 text


@app.post("/api/classify")
def classify(body: ClassifyBody):
    tg_user = verify_init_data(body.init_data)
    if not tg_user:
        return {"ok": False, "reason": "bad auth"}
    if not body.image or len(body.image) > 3_000_000:
        return {"ok": False, "reason": "no photo, or the photo is too large"}

    try:
        category, confidence = classify_image(body.image)
    except Exception:
        return {"ok": False, "reason": "could not read the photo"}

    if category not in CATEGORY_POINTS:
        return {"ok": False, "reason": "trash not recognized, try a closer photo", "category": "none"}

    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO classifications (telegram_id, category, created_at) VALUES (?, ?, ?)",
        (tg_user["id"], category, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "category": category, "confidence": round(confidence, 2)}