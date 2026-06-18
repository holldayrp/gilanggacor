import imaplib
import email
import random
import re
import asyncio
import threading
import time
import socket
import os
import signal
import sqlite3
import httpx
import random
import string
import secrets
from email.header import decode_header
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ============================================================
# KONFIGURASI
# ============================================================
BOT_TOKEN           = "8887721278:AAGDbiEssWugcuq2hNApqm0fuTbcCIbY5Io"
ADMIN_IDS           = [7980141797, 1630056409]

IMAP_SERVER         = "imap.gmail.com"
IMAP_PORT           = 993
GMAIL_ADDRESS       = "imamganteng@bahlil.cfd"
GMAIL_APP_PASSWORD  = "tmsbdmnfpfdchmyi"

SERVER_NAME         = "Server Bahlil"
IDLE_TIMEOUT        = 290
CACHE_MAX           = 5000
# FIX: DEDUP dihapus, diganti sistem timestamp
OTP_RESEND_COOLDOWN = 30   # detik — OTP yang SAMA bisa dikirim ulang setelah 30 detik
STARTER_PACK_SLOTS  = 10

# ── PAYMENT ──
QRIS_API_KEY        = "6Vws1VAWoTp3rnRNUZAYEVUB06VkhZi9w3bg0RMY"
QRIS_MERCHANT_ID    = "176952001778"
QRIS_BASE_URL       = "https://klikqris.com/api"
PRICE_PER_SLOT      = 100
TOPUP_MIN           = 2000
BONUS_SLOTS_PER_TOPUP = 10

# ── GROUP VERIFICATION ──
REQUIRED_GROUP_ID   = None
REQUIRED_GROUP_LINK = ""

PAYMENT_POLL_INTERVAL = 5

# ── SLOT EXPIRY ──
SLOT_EXPIRY_DAYS    = 0

# ── IMAP THROTTLE ──
POLL_INTERVAL       = 30
SCAN_BATCH_DELAY    = 0.1   # FIX: dipercepat dari 0.3 → 0.1
IDLE_BACKOFF_START  = 5
IDLE_BACKOFF_MAX    = 120

# ── BROADCAST ──
BROADCAST_DELAY     = 0.05
BROADCAST_BATCH_SIZE = 20

TZ_JAKARTA = ZoneInfo("Asia/Jakarta")
# ============================================================

# ============================================================
# DATABASE & TIME HELPERS
# ============================================================
DB_NAME = "bot_database.db"

def now_wib() -> datetime:
    return datetime.now(TZ_JAKARTA)

def now_wib_str() -> str:
    return now_wib().strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id      INTEGER PRIMARY KEY,
        slots        INTEGER DEFAULT 0,
        email_count  INTEGER DEFAULT 0,
        otp_count    INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_stats (
        key   TEXT PRIMARY KEY,
        value TEXT DEFAULT ''
    )''')
    for k, v in [
        ('total_otp', '0'),
        ('required_group_id', ''),
        ('required_group_link', ''),
        ('slot_expiry_days', '0'),
        ('bonus_slots_per_topup', str(BONUS_SLOTS_PER_TOPUP)),
        ('seeded_default_domains', '0'),
    ]:
        c.execute("INSERT OR IGNORE INTO bot_stats (key,value) VALUES (?,?)", (k, v))
    c.execute('''CREATE TABLE IF NOT EXISTS slot_batches (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        source      TEXT NOT NULL,
        total       INTEGER NOT NULL,
        remaining   INTEGER NOT NULL,
        expired_at  TEXT,
        created_at  TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS topup_orders (
        order_id   TEXT PRIMARY KEY,
        user_id    INTEGER,
        amount     INTEGER,
        slots      INTEGER,
        bonus      INTEGER DEFAULT 0,
        status     TEXT DEFAULT 'PENDING',
        signature  TEXT,
        created_at TEXT,
        paid_at    TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS domain_labels (
        domain TEXT PRIMARY KEY,
        label  TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS fb_checkpoint_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        email      TEXT,
        status     TEXT,
        checked_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS domains (
        domain     TEXT PRIMARY KEY,
        label      TEXT DEFAULT '',
        active     INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )''')

    for migration in [
        "ALTER TABLE users ADD COLUMN otp_count INTEGER DEFAULT 0",
        "ALTER TABLE topup_orders ADD COLUMN bonus INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except:
            pass

    try:
        r = conn.execute("SELECT value FROM bot_stats WHERE key='bonus_slots_per_topup'").fetchone()
        if r and r[0] == '0' and BONUS_SLOTS_PER_TOPUP > 0:
            conn.execute(
                "UPDATE bot_stats SET value=? WHERE key='bonus_slots_per_topup'",
                (str(BONUS_SLOTS_PER_TOPUP),)
            )
            conn.commit()
    except:
        pass

    seeded = conn.execute("SELECT value FROM bot_stats WHERE key='seeded_default_domains'").fetchone()
    if seeded and seeded[0] == '0':
        INITIAL_DOMAINS = [
           "ngegasterus.xyz", "giskaayufirnandalabs.my.id", "uyakuya.xyz"
        ]
        for i, d in enumerate(INITIAL_DOMAINS):
            c.execute(
                "INSERT OR IGNORE INTO domains (domain, label, active, sort_order, created_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (d, f"@{d}", i, now_wib_str())
            )
            c.execute(
                "INSERT OR IGNORE INTO domain_labels (domain, label) VALUES (?, ?)",
                (d, f"@{d}")
            )
        conn.execute("UPDATE bot_stats SET value='1' WHERE key='seeded_default_domains'")
        conn.commit()

    conn.commit()
    conn.close()

init_db()

def db():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

# ── USER ──

def _migrate_legacy_slots(user_id: int):
    with db() as conn:
        batch_cnt = conn.execute(
            "SELECT COUNT(*) FROM slot_batches WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        if batch_cnt > 0:
            return
        legacy = conn.execute(
            "SELECT slots FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not legacy or legacy[0] <= 0:
            return
        amount = legacy[0]
        now    = now_wib_str()
        conn.execute(
            "INSERT INTO slot_batches (user_id,source,total,remaining,expired_at,created_at) "
            "VALUES (?,?,?,?,NULL,?)",
            (user_id, "legacy", amount, amount, now)
        )
        conn.commit()

def get_valid_slots(user_id: int) -> int:
    current_wib_str = now_wib_str()
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(remaining),0) FROM slot_batches "
            "WHERE user_id=? AND remaining>0 AND (expired_at IS NULL OR expired_at > ?)",
            (user_id, current_wib_str)
        ).fetchone()
    return row[0] if row else 0

def repair_user_slots(user_id: int) -> int:
    """
    FIX SLOT HILANG: Sinkronkan users.slots dengan slot_batches.
    Kolom users.slots adalah cache — kalau drift karena crash/race condition,
    fungsi ini akan memperbaikinya secara otomatis.
    """
    current_wib_str = now_wib_str()
    with db() as conn:
        real_total = conn.execute(
            "SELECT COALESCE(SUM(remaining),0) FROM slot_batches "
            "WHERE user_id=? AND remaining>0 AND (expired_at IS NULL OR expired_at > ?)",
            (user_id, current_wib_str)
        ).fetchone()[0]
        cached = conn.execute(
            "SELECT slots FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if cached and cached[0] != real_total:
            print(f"🔧 repair_user_slots: user {user_id} slots {cached[0]} -> {real_total} (drift diperbaiki)")
            conn.execute("UPDATE users SET slots=? WHERE user_id=?", (real_total, user_id))
            conn.commit()
    return real_total

def get_user_data(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT slots, email_count, otp_count FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, slots, email_count, otp_count) VALUES (?,0,0,0)",
                (user_id,)
            )
            conn.commit()
            batch_cnt = conn.execute(
                "SELECT COUNT(*) FROM slot_batches WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            if batch_cnt == 0:
                add_slot_batch(user_id, STARTER_PACK_SLOTS, "starterpack")
            return {"slots": get_valid_slots(user_id), "email_count": 0, "otp_count": 0}
    _migrate_legacy_slots(user_id)
    # FIX SLOT HILANG: selalu sinkronkan dari slot_batches, bukan dari cache users.slots
    valid = repair_user_slots(user_id)
    return {"slots": valid, "email_count": row[1], "otp_count": row[2] or 0}

def _expiry_dt() -> str | None:
    if SLOT_EXPIRY_DAYS <= 0:
        return None
    future_wib = now_wib() + timedelta(days=SLOT_EXPIRY_DAYS)
    return future_wib.strftime("%Y-%m-%d %H:%M:%S")

def add_slot_batch(user_id: int, amount: int, source: str):
    exp = _expiry_dt()
    now = now_wib_str()
    current_wib_str = now_wib_str()
    with db() as conn:
        # Pastikan user row ada sebelum insert batch
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, slots, email_count, otp_count) VALUES (?,0,0,0)",
            (user_id,)
        )
        conn.execute(
            "INSERT INTO slot_batches (user_id,source,total,remaining,expired_at,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, source, amount, amount, exp, now)
        )
        # FIX: Sync users.slots dari slot_batches secara real-time
        # Pakai SUM dari slot_batches sebagai sumber kebenaran,
        # bukan += yang bisa drift jika ada crash/race condition
        real_total = conn.execute(
            "SELECT COALESCE(SUM(remaining),0) FROM slot_batches "
            "WHERE user_id=? AND remaining>0 AND (expired_at IS NULL OR expired_at > ?)",
            (user_id, current_wib_str)
        ).fetchone()[0]
        conn.execute("UPDATE users SET slots=? WHERE user_id=?", (real_total, user_id))
        conn.commit()

def consume_slot_batch(user_id: int, count: int = 1):
    _migrate_legacy_slots(user_id)
    current_wib_str = now_wib_str()
    with db() as conn:
        rows = conn.execute(
            "SELECT id, remaining FROM slot_batches "
            "WHERE user_id=? AND remaining>0 AND (expired_at IS NULL OR expired_at > ?) "
            "ORDER BY created_at ASC",
            (user_id, current_wib_str)
        ).fetchall()
        total_avail = sum(r[1] for r in rows)
        if total_avail < count:
            return False
        to_consume = count
        for batch_id, rem in rows:
            if to_consume <= 0:
                break
            take = min(rem, to_consume)
            conn.execute(
                "UPDATE slot_batches SET remaining=remaining-? WHERE id=?", (take, batch_id)
            )
            to_consume -= take
        # FIX: Sync users.slots dari slot_batches sebagai sumber kebenaran
        real_total = conn.execute(
            "SELECT COALESCE(SUM(remaining),0) FROM slot_batches "
            "WHERE user_id=? AND remaining>0 AND (expired_at IS NULL OR expired_at > ?)",
            (user_id, current_wib_str)
        ).fetchone()[0]
        conn.execute("UPDATE users SET slots=? WHERE user_id=?", (real_total, user_id))
        conn.commit()
    return True

def update_user_slots(user_id, delta):
    if delta > 0:
        add_slot_batch(user_id, delta, "topup")
    elif delta < 0:
        consume_slot_batch(user_id, abs(delta))

def increment_email_count(user_id):
    with db() as conn:
        conn.execute("UPDATE users SET email_count=email_count+1 WHERE user_id=?", (user_id,))
        conn.commit()

def increment_otp_count(user_id):
    try:
        with db() as conn:
            conn.execute("UPDATE users SET otp_count=otp_count+1 WHERE user_id=?", (user_id,))
            conn.execute("UPDATE bot_stats SET value=CAST(value AS INTEGER)+1 WHERE key='total_otp'")
            conn.commit()
    except:
        pass

def increment_otp_stat():
    try:
        with db() as conn:
            conn.execute("UPDATE bot_stats SET value=CAST(value AS INTEGER)+1 WHERE key='total_otp'")
            conn.commit()
    except:
        pass

def get_all_user_ids():
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
    return [r[0] for r in rows]

def get_top_otp_users(limit=10):
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, otp_count FROM users ORDER BY otp_count DESC LIMIT ?", (limit,)
        ).fetchall()
    return rows

def get_slot_batches_user(user_id: int):
    current_wib_str = now_wib_str()
    with db() as conn:
        rows = conn.execute(
            "SELECT source, total, remaining, expired_at, created_at FROM slot_batches "
            "WHERE user_id=? AND remaining>0 AND (expired_at IS NULL OR expired_at > ?) "
            "ORDER BY created_at ASC",
            (user_id, current_wib_str)
        ).fetchall()
    return rows

def expire_slots_now():
    current_wib_str = now_wib_str()
    affected = 0
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM slot_batches "
            "WHERE remaining>0 AND expired_at IS NOT NULL AND expired_at <= ?",
            (current_wib_str,)
        ).fetchall()
        for (uid,) in rows:
            conn.execute(
                "UPDATE slot_batches SET remaining=0 "
                "WHERE user_id=? AND remaining>0 AND expired_at IS NOT NULL AND expired_at <= ?",
                (uid, current_wib_str)
            )
            valid = conn.execute(
                "SELECT COALESCE(SUM(remaining),0) FROM slot_batches "
                "WHERE user_id=? AND remaining>0 AND (expired_at IS NULL OR expired_at > ?)",
                (uid, current_wib_str)
            ).fetchone()[0]
            conn.execute("UPDATE users SET slots=? WHERE user_id=?", (valid, uid))
            affected += 1
        conn.commit()
    return affected

def extend_user_slot_expiry(user_id: int) -> int:
    if SLOT_EXPIRY_DAYS <= 0:
        return 0
    new_expiry = _expiry_dt()
    if not new_expiry:
        return 0
    with db() as conn:
        cursor = conn.execute(
            "UPDATE slot_batches SET expired_at=? "
            "WHERE user_id=? AND remaining>0 AND expired_at IS NOT NULL",
            (new_expiry, user_id)
        )
        affected = cursor.rowcount
        conn.commit()
    return affected

def get_domain_label(domain: str) -> str:
    with db() as conn:
        row = conn.execute("SELECT label FROM domain_labels WHERE domain=?", (domain,)).fetchone()
    return row[0] if row else f"@{domain}"

def set_domain_label(domain: str, label: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO domain_labels (domain,label) VALUES (?,?)",
            (domain, label)
        )
        conn.commit()

def load_group_config():
    global REQUIRED_GROUP_ID, REQUIRED_GROUP_LINK
    with db() as conn:
        r = conn.execute("SELECT value FROM bot_stats WHERE key='required_group_id'").fetchone()
        try:
            REQUIRED_GROUP_ID = int(r[0]) if r and r[0] else None
        except:
            REQUIRED_GROUP_ID = None
        r = conn.execute("SELECT value FROM bot_stats WHERE key='required_group_link'").fetchone()
        REQUIRED_GROUP_LINK = r[0] if r and r[0] else ""

def save_group_config(group_id, group_link):
    with db() as conn:
        conn.execute("UPDATE bot_stats SET value=? WHERE key='required_group_id'", (str(group_id),))
        conn.execute("UPDATE bot_stats SET value=? WHERE key='required_group_link'", (str(group_link),))
        conn.commit()
    load_group_config()

def load_slot_expiry_config():
    global SLOT_EXPIRY_DAYS
    with db() as conn:
        r = conn.execute("SELECT value FROM bot_stats WHERE key='slot_expiry_days'").fetchone()
        try:
            SLOT_EXPIRY_DAYS = int(r[0]) if r and r[0] else 0
        except:
            SLOT_EXPIRY_DAYS = 0

def load_bonus_config():
    global BONUS_SLOTS_PER_TOPUP
    with db() as conn:
        r = conn.execute("SELECT value FROM bot_stats WHERE key='bonus_slots_per_topup'").fetchone()
        try:
            val = int(r[0]) if r and r[0] else 0
            if val == 0 and BONUS_SLOTS_PER_TOPUP > 0:
                conn.execute(
                    "UPDATE bot_stats SET value=? WHERE key='bonus_slots_per_topup'",
                    (str(BONUS_SLOTS_PER_TOPUP),)
                )
                conn.commit()
                val = BONUS_SLOTS_PER_TOPUP
            BONUS_SLOTS_PER_TOPUP = val
        except:
            BONUS_SLOTS_PER_TOPUP = 0

load_group_config()
load_slot_expiry_config()
load_bonus_config()

def create_order(order_id, user_id, amount, slots, bonus, signature):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO topup_orders "
            "(order_id,user_id,amount,slots,bonus,status,signature,created_at) VALUES (?,?,?,?,?,'PENDING',?,?)",
            (order_id, user_id, amount, slots, bonus, signature, now_wib_str())
        )
        conn.commit()

def complete_order(order_id):
    with db() as conn:
        cursor = conn.execute(
            "UPDATE topup_orders SET status='SUCCESS', paid_at=? "
            "WHERE order_id=? AND status='PENDING'",
            (now_wib_str(), order_id)
        )
        if cursor.rowcount == 0:
            conn.commit()
            return None, 0, 0
        row = conn.execute(
            "SELECT user_id, slots, bonus FROM topup_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        conn.commit()
    if not row:
        return None, 0, 0
    return row[0], row[1], row[2] or 0

def expire_order(order_id):
    with db() as conn:
        conn.execute(
            "UPDATE topup_orders SET status='EXPIRED' WHERE order_id=? AND status='PENDING'", (order_id,)
        )
        conn.commit()

def log_fb_check(em: str, status: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO fb_checkpoint_log (email,status,checked_at) VALUES (?,?,?)",
            (em, status, now_wib_str())
        )
        conn.commit()

# ============================================================
# DYNAMIC DOMAIN MANAGEMENT
# ============================================================

def get_domains(active_only: bool = True) -> list[str]:
    with db() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT domain FROM domains WHERE active=1 ORDER BY sort_order ASC, domain ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT domain FROM domains ORDER BY sort_order ASC, domain ASC"
            ).fetchall()
    return [r[0] for r in rows]

def get_domain_full_info() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT domain, label, active, sort_order, created_at FROM domains "
            "ORDER BY sort_order ASC, domain ASC"
        ).fetchall()
    return [
        {"domain": r[0], "label": r[1], "active": bool(r[2]),
         "sort_order": r[3], "created_at": r[4]}
        for r in rows
    ]

def add_domain_db(domain: str, label: str = "", sort_order: int = -1) -> bool:
    domain = domain.strip().lower()
    if not domain:
        return False
    label = label.strip() if label.strip() else f"@{domain}"
    if sort_order < 0:
        with db() as conn:
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order),-1) FROM domains"
            ).fetchone()[0]
            sort_order = max_order + 1
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO domains (domain, label, active, sort_order, created_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (domain, label, sort_order, now_wib_str())
            )
            conn.execute(
                "INSERT OR REPLACE INTO domain_labels (domain, label) VALUES (?, ?)",
                (domain, label)
            )
            conn.commit()
        return True
    except:
        return False

def del_domain_db(domain: str) -> bool:
    domain = domain.strip().lower()
    with db() as conn:
        cursor = conn.execute("DELETE FROM domains WHERE domain=?", (domain,))
        deleted = cursor.rowcount
        conn.execute("DELETE FROM domain_labels WHERE domain=?", (domain,))
        conn.commit()
    return deleted > 0

def toggle_domain_db(domain: str) -> bool | None:
    domain = domain.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT active FROM domains WHERE domain=?", (domain,)
        ).fetchone()
        if not row:
            return None
        new_state = 0 if row[0] else 1
        conn.execute("UPDATE domains SET active=? WHERE domain=?", (new_state, domain))
        conn.commit()
    return bool(new_state)

def update_domain_db(old_domain: str, new_domain: str) -> bool:
    old_domain = old_domain.strip().lower()
    new_domain = new_domain.strip().lower()
    if not old_domain or not new_domain:
        return False
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT domain FROM domains WHERE domain=?", (old_domain,)
            ).fetchone()
            if not row:
                return False
            exists = conn.execute(
                "SELECT domain FROM domains WHERE domain=?", (new_domain,)
            ).fetchone()
            if exists:
                return False

            conn.execute(
                "UPDATE domains SET domain=?, label=? WHERE domain=?",
                (new_domain, f"@{new_domain}", old_domain)
            )
            conn.execute(
                "UPDATE domain_labels SET domain=?, label=? WHERE domain=?",
                (new_domain, f"@{new_domain}", old_domain)
            )
            conn.commit()

        with otp_lock:
            old_suffix = f"@{old_domain}"
            new_suffix = f"@{new_domain}"

            keys_to_update = [em for em in list(email_owners.keys()) if em.endswith(old_suffix)]
            for old_em in keys_to_update:
                new_em = old_em[:-len(old_suffix)] + new_suffix
                email_owners[new_em] = email_owners.pop(old_em)

                if old_em in otp_history:
                    otp_history[new_em] = otp_history.pop(old_em)

                # FIX: Update timestamp dict juga
                keys_ts = [k for k in otp_sent_timestamps if k.startswith(f"{old_em}:")]
                for k in keys_ts:
                    otp_sent_timestamps[k.replace(f"{old_em}:", f"{new_em}:", 1)] = otp_sent_timestamps.pop(k)

            for uid in list(user_emails.keys()):
                user_emails[uid] = [
                    e[:-len(old_suffix)] + new_suffix if e.endswith(old_suffix) else e
                    for e in user_emails[uid]
                ]
        return True
    except Exception as e:
        print(f"update_domain_db error: {e}")
        return False

# ============================================================
# QRIS API
# ============================================================

def qris_create(order_id, amount, keterangan="Topup Slot"):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": QRIS_API_KEY,
        "id_merchant": QRIS_MERCHANT_ID,
    }
    try:
        r = httpx.post(
            f"{QRIS_BASE_URL}/qris/create",
            json={"order_id": order_id, "id_merchant": QRIS_MERCHANT_ID,
                  "amount": amount, "keterangan": keterangan},
            headers=headers, timeout=15
        )
        return r.json()
    except Exception as e:
        print(f"QRIS create error: {e}")
        return None

def qris_status(order_id):
    try:
        r = httpx.get(
            f"{QRIS_BASE_URL}/qris/status/{order_id}",
            headers={"x-api-key": QRIS_API_KEY, "id_merchant": QRIS_MERCHANT_ID},
            timeout=10
        )
        return r.json()
    except Exception as e:
        print(f"QRIS status error: {e}")
        return None

# ============================================================
# FACEBOOK CHECKPOINT CHECK
# ============================================================

async def check_fb_checkpoint(email: str) -> str:
    url = "https://www.facebook.com/ajax/register/validate_email.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 Chrome/112.0.0.0 Mobile Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Language": "id-ID,id;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.post(url, data={"email": email, "validate_only": "1", "__a": "1"}, headers=headers)
            text = resp.text.lower()
            if "checkpoint" in text or "suspicious" in text or "unusual" in text:
                return "checkpoint"
            if "already" in text or "registered" in text or "taken" in text:
                return "used"
            if resp.status_code == 200:
                return "ok"
            return "error"
    except Exception as e:
        print(f"FB check error [{email}]: {e}")
        return "error"

# ============================================================
# IMAP / OTP
# ============================================================

def auto_kill_existing():
    current_pid = os.getpid()
    script_name = os.path.basename(__file__)
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", script_name],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split() if p.strip().isdigit()]
        for pid in pids:
            if pid != current_pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    print(f"🔴 Killed duplicate PID: {pid}")
                except ProcessLookupError:
                    pass
                except PermissionError:
                    print(f"⚠️ No permission to kill PID: {pid}")
        if pids:
            time.sleep(1)
    except Exception as e:
        print(f"Auto-kill error: {e}")

user_emails  = {}
email_owners = {}
user_state   = {}
otp_lock     = threading.Lock()

otp_history          = {}

# ============================================================
# FIX #1: Ganti sent_otp_set dengan timestamp dict
# Tujuan: OTP kedua (atau OTP yang sama) bisa masuk setelah cooldown
# ============================================================
otp_sent_timestamps  = {}   # key: "email:otp" → timestamp (float) terakhir dikirim

def _is_otp_on_cooldown(em: str, otp: str) -> bool:
    """Cek apakah OTP ini masih dalam cooldown (belum boleh dikirim ulang)."""
    key = f"{em}:{otp}"
    last_sent = otp_sent_timestamps.get(key)
    if last_sent is None:
        return False
    return (time.time() - last_sent) < OTP_RESEND_COOLDOWN

def _mark_otp_sent(em: str, otp: str):
    """Tandai OTP ini sudah dikirim, simpan timestamp-nya."""
    key = f"{em}:{otp}"
    otp_sent_timestamps[key] = time.time()

def _cleanup_otp_timestamps():
    """Hapus entry yang sudah melewati cooldown (hemat memori)."""
    now_ts = time.time()
    expired_keys = [k for k, ts in otp_sent_timestamps.items()
                    if (now_ts - ts) > OTP_RESEND_COOLDOWN * 10]
    for k in expired_keys:
        otp_sent_timestamps.pop(k, None)
# ============================================================

_scan_lock = threading.Lock()
_last_scan  = 0.0

# ── IMAP Connection Pool ──
_imap_pool      = []
_imap_pool_lock = threading.Lock()
_POOL_SIZE      = 3

def get_imap_connection():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    return mail

def _borrow_imap():
    with _imap_pool_lock:
        if _imap_pool:
            return _imap_pool.pop()
    return get_imap_connection()

def _return_imap(conn):
    with _imap_pool_lock:
        if len(_imap_pool) < _POOL_SIZE:
            try:
                conn.check()
                _imap_pool.append(conn)
                return
            except:
                pass
    try:
        conn.logout()
    except:
        pass

def _warm_imap_pool():
    for _ in range(_POOL_SIZE):
        try:
            c = get_imap_connection()
            with _imap_pool_lock:
                _imap_pool.append(c)
        except Exception as e:
            print(f"Pool warm error: {e}")
    print(f"🔥 IMAP pool warmed ({len(_imap_pool)} koneksi)")

def generate_random_email(domain):
    alphanumeric = string.ascii_lowercase + string.digits
    styles = [
        lambda: ''.join(secrets.choice(alphanumeric) for _ in range(random.randint(12, 16))),
        lambda: ''.join(random.choices(string.ascii_lowercase, k=random.randint(6, 8))) + str(secrets.randbelow(900000) + 100000),
        lambda: str(secrets.randbelow(90000) + 10000) + ''.join(random.choices(string.ascii_lowercase, k=random.randint(7, 9))),
        lambda: ''.join(random.choices(string.ascii_lowercase, k=random.randint(11, 15))),
        lambda: ''.join(random.choices(string.ascii_lowercase, k=5)) + str(secrets.randbelow(9000) + 1000) + ''.join(random.choices(string.ascii_lowercase, k=4))
    ]
    username = random.choice(styles)()
    return f"{username}@{domain}"

def decode_str(s):
    if not s: return ""
    try:
        decoded = decode_header(s)
        result = ""
        for part, enc in decoded:
            if isinstance(part, bytes): result += part.decode(enc or "utf-8", errors="ignore")
            else: result += str(part)
        return result
    except: return str(s)

def extract_otp(text):
    if not text: return None
    patterns = [
        r'(?i)(?:otp|code|kode|verif|verification|token|pin)[^\d]*(\d{4,8})',
        r'(?i)(\d{4,8})\s+(?:is your|adalah)',
        r'\b(\d{6})\b', r'\b(\d{5})\b', r'\b(\d{4})\b',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            grp = m.groups()
            return grp[0] if grp else m.group()
    return None

def get_email_body(msg):
    body = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    try:
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
                    except: pass
                elif ctype == "text/html" and not body:
                    try:
                        html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        body = re.sub(r"<[^>]+>", " ", html)
                        body = re.sub(r"\s+", " ", body).strip()
                    except: pass
        else:
            try: body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            except: body = str(msg.get_payload())
    except: pass
    return body[:2000]

def auto_cleanup():
    with otp_lock:
        if len(otp_history) > CACHE_MAX:
            otp_history.clear()
        _cleanup_otp_timestamps()

def push_otp_to_cache(em: str, otp: str):
    em = em.lower()
    with otp_lock:
        if em not in otp_history:
            otp_history[em] = []
        if otp not in otp_history[em]:
            otp_history[em].append(otp)
        if len(otp_history[em]) > 5:
            otp_history[em] = otp_history[em][-5:]

def search_otp_with_conn(conn, target_email: str):
    date_str = now_wib().strftime("%d-%b-%Y")
    folders  = ['INBOX', '"[Gmail]/All Mail"', '"[Gmail]/Spam"']
    for folder in folders:
        try:
            status, _ = conn.select(folder, readonly=True)
            if status != "OK": continue
            _, data = conn.search(None, f'(TO "{target_email}" SINCE "{date_str}")')
            if not data or not data[0]: continue
            nums = data[0].split()
            if not nums: continue
            for num in reversed(nums[-10:]):
                try:
                    _, msg_data = conn.fetch(num, "(RFC822)")
                    if not msg_data or not msg_data[0]: continue
                    raw = msg_data[0]
                    if not isinstance(raw, tuple) or len(raw) < 2: continue
                    msg     = email.message_from_bytes(raw[1])
                    subject = decode_str(msg.get("Subject", ""))
                    body    = get_email_body(msg)
                    otp = extract_otp(body) or extract_otp(subject)
                    if otp: return otp
                except: continue
        except: continue
    return None

def search_otp_fast(target_email: str):
    date_str = now_wib().strftime("%d-%b-%Y")
    folders  = ['INBOX', '"[Gmail]/All Mail"', '"[Gmail]/Spam"']
    conn = None
    try:
        conn = _borrow_imap()
        for folder in folders:
            try:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    continue
                _, data = conn.search(None, f'(TO "{target_email}" SINCE "{date_str}")')
                if not data or not data[0]:
                    continue
                nums = data[0].split()
                if not nums:
                    continue
                for num in reversed(nums[-5:]):
                    try:
                        _, msg_data = conn.fetch(num, "(RFC822)")
                        if not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0]
                        if not isinstance(raw, tuple) or len(raw) < 2:
                            continue
                        msg     = email.message_from_bytes(raw[1])
                        subject = decode_str(msg.get("Subject", ""))
                        body    = get_email_body(msg)
                        otp = extract_otp(body) or extract_otp(subject)
                        if otp:
                            return otp
                    except:
                        continue
            except:
                continue
    except Exception as e:
        print(f"search_otp_fast error: {e}")
        if conn:
            try:
                conn.logout()
            except:
                pass
        conn = None
    finally:
        if conn is not None:
            _return_imap(conn)
    return None

def _do_scan_all():
    if not email_owners: return 0
    if not _scan_lock.acquire(blocking=False): return 0
    conn = None
    found = 0
    try:
        conn = get_imap_connection()
        with otp_lock:
            emails_snapshot = list(email_owners.keys())
        print(f"📬 Scanning {len(emails_snapshot)} emails...")
        for target_email in emails_snapshot:
            try:
                otp = search_otp_with_conn(conn, target_email)
                if otp:
                    push_otp_to_cache(target_email, otp)
                    increment_otp_stat()
                    print(f"💾 Cached: {otp} → {target_email}")
                    found += 1
                time.sleep(SCAN_BATCH_DELAY)
            except Exception as inner_e:
                print(f"Scan single email error ({target_email}): {inner_e}")
                try:
                    conn.noop()
                except:
                    print("🔄 IMAP disconnected mid-scan, reconnecting...")
                    try:
                        conn.logout()
                    except:
                        pass
                    try:
                        conn = get_imap_connection()
                    except Exception as reconnect_e:
                        print(f"Reconnect failed: {reconnect_e}")
                        break
        auto_cleanup()
    except Exception as e:
        print(f"Scan error: {e}")
    finally:
        _scan_lock.release()
        if conn:
            try: conn.logout()
            except: pass
    return found

def cache_all_emails_throttled():
    global _last_scan
    now_ts = time.time()
    if now_ts - _last_scan < POLL_INTERVAL: return
    _last_scan = time.time()
    _do_scan_all()

def imap_idle_thread():
    backoff = IDLE_BACKOFF_START
    while True:
        conn = None
        try:
            print("🔌 Connecting IMAP IDLE...")
            conn = get_imap_connection()
            conn.select("INBOX")
            backoff = IDLE_BACKOFF_START
            print("⚡ IMAP IDLE Active!")
            while True:
                tag = conn._new_tag()
                conn.send(tag + b' IDLE\r\n')
                conn.socket().settimeout(IDLE_TIMEOUT)
                try:
                    while True:
                        line = conn.readline()
                        if b"EXISTS" in line or b"RECENT" in line:
                            raise Exception("NEW_MAIL_DETECTED")
                        if b"BYE" in line:
                            raise Exception("BYE")
                except socket.timeout:
                    pass
                except Exception as e:
                    if "NEW_MAIL_DETECTED" not in str(e):
                        raise e
                conn.send(b'DONE\r\n')
                conn.readline()
                print("📨 Email baru via IDLE!")
                time.sleep(0.3)
                _do_scan_all()
        except Exception as e:
            print(f"IMAP IDLE Error: {e} — retry in {backoff}s")
            backoff = min(backoff * 2, IDLE_BACKOFF_MAX)
        finally:
            if conn:
                try: conn.logout()
                except: pass
            time.sleep(backoff)

def imap_poll_thread():
    time.sleep(30)
    while True:
        try: cache_all_emails_throttled()
        except Exception as e: print(f"Poll error: {e}")
        time.sleep(POLL_INTERVAL)

def slot_expiry_thread():
    while True:
        time.sleep(3600)
        try:
            affected = expire_slots_now()
            if affected > 0:
                print(f"⏰ [WIB {now_wib_str()}] Slot expiry: {affected} user terdampak")
        except sqlite3.DatabaseError as e:
            print(f"Expiry thread DB error: {e}")
        except Exception as e:
            print(f"Expiry thread error: {e}")

# ============================================================
# KEYBOARDS
# ============================================================

def keyboard_domain():
    buttons = []
    domains = get_domains(active_only=True)
    for d in domains:
        label = get_domain_label(d)
        buttons.append([InlineKeyboardButton(label, callback_data=f"domain:{d}")])
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)

def keyboard_ambil_otp(em):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔥 Ambil OTP", callback_data=f"otp:{em}")]])

def keyboard_coba_lagi(em):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Coba Lagi", callback_data=f"otp:{em}")]])

def keyboard_coba_lagi_manual(em):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Coba Lagi", callback_data=f"otpmanual:{em}")]])

def keyboard_join_group():
    if not REQUIRED_GROUP_LINK: return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join Group Sekarang", url=REQUIRED_GROUP_LINK)]])

def keyboard_broadcast_type():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Teks", callback_data="bc_type:text")],
        [InlineKeyboardButton("🖼️ Foto + Caption", callback_data="bc_type:photo")],
        [InlineKeyboardButton("🎬 Video + Caption", callback_data="bc_type:video")],
        [InlineKeyboardButton("❌ Batal", callback_data="bc_cancel")],
    ])

def keyboard_broadcast_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Kirim Sekarang", callback_data="bc_confirm:yes")],
        [InlineKeyboardButton("❌ Batal", callback_data="bc_confirm:no")],
    ])

# ============================================================
# HELPERS
# ============================================================

async def safe_edit(edit_func, text, reply_markup=None, parse_mode="Markdown"):
    try:
        kwargs = {"parse_mode": parse_mode}
        if reply_markup: kwargs["reply_markup"] = reply_markup
        await edit_func(text, **kwargs)
    except Exception as e:
        if "not modified" not in str(e).lower(): print(f"Edit error: {e}")

async def safe_delete(message):
    try: await message.delete()
    except: pass

async def check_group_membership(user_id, context):
    if not REQUIRED_GROUP_ID: return True
    if user_id in ADMIN_IDS: return True
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_GROUP_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except: return True

async def send_not_member_message(update, context):
    text   = "⛔ *Akses Ditolak*\n\nKamu harus bergabung dengan grup resmi kami sebelum dapat menggunakan bot ini."
    markup = keyboard_join_group()
    if update.callback_query:
        try: await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        except: await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    else: await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)

def _check_admin(user_id): return user_id in ADMIN_IDS

# ============================================================
# FIX #2: OTP SEARCH — Sistem baru dengan timestamp cooldown
# ============================================================

from concurrent.futures import ThreadPoolExecutor
_search_executor = ThreadPoolExecutor(max_workers=8)

async def do_search_otp(em: str, edit_func, reply_markup_fn, user_id=None):
    em = em.strip().lower()

    # ── Cek cache dulu ──
    # FIX: Tidak pakai sent_otp_set, pakai timestamp cooldown
    # Sehingga OTP kedua/baru BISA masuk setelah cooldown habis
    found_in_cache = None
    with otp_lock:
        cached_otps = list(reversed(otp_history.get(em, [])))
    
    for otp in cached_otps:
        if not _is_otp_on_cooldown(em, otp):
            found_in_cache = otp
            break

    if found_in_cache:
        _mark_otp_sent(em, found_in_cache)
        if user_id:
            increment_otp_count(user_id)
        await safe_edit(edit_func, f"✅ *OTP Ditemukan!*\n\n📧 Email: `{em}`\n🔥 OTP: `{found_in_cache}`")
        return

    await safe_edit(edit_func, f"⏳ *Mencari OTP...*\n\n📧 `{em}`")

    loop = asyncio.get_running_loop()

    def _search():
        # Cek cache lagi di thread (mungkin sudah ada OTP baru dari scan paralel)
        with otp_lock:
            cached_otps_now = list(reversed(otp_history.get(em, [])))
        
        for otp in cached_otps_now:
            if not _is_otp_on_cooldown(em, otp):
                _mark_otp_sent(em, otp)
                return otp
        
        # Langsung IMAP search
        otp = search_otp_fast(em)
        if otp:
            push_otp_to_cache(em, otp)
            _mark_otp_sent(em, otp)
        return otp

    otp = await loop.run_in_executor(_search_executor, _search)

    if otp:
        if user_id:
            increment_otp_count(user_id)
        await safe_edit(edit_func, f"✅ *OTP Ditemukan!*\n\n📧 Email: `{em}`\n🔥 OTP: `{otp}`")
    else:
        await safe_edit(
            edit_func,
            f"📭 *OTP Belum Ada*\n\n📧 `{em}`\n\nPastikan sudah mendaftar/menekan verifikasi,\nlalu tekan coba lagi.",
            reply_markup=reply_markup_fn(em)
        )

# ============================================================
# TOPUP LOGIC
# ============================================================

async def process_topup_payment(user_id, amount, update, context):
    slots     = amount // PRICE_PER_SLOT
    bonus     = BONUS_SLOTS_PER_TOPUP
    total_get = slots + bonus
    ts        = now_wib().strftime("%Y%m%d%H%M%S")
    order_id  = f"SLOT-{user_id}-{ts}"
    msg_wait  = await update.message.reply_text("⏳ *Membuat QRIS...*", parse_mode="Markdown")
    resp = await asyncio.get_running_loop().run_in_executor(
        None, lambda: qris_create(order_id, amount, f"Topup {total_get} slot @{user_id}")
    )
    if not resp or not resp.get("status"):
        err = resp.get("message", "Unknown") if resp else "Timeout"
        await msg_wait.edit_text(f"❌ *Gagal membuat QRIS:* `{err}`", parse_mode="Markdown")
        return
    data         = resp["data"]
    total_amount = int(float(data["total_amount"]))
    qris_url     = data.get("qris_url", "")
    expired_at   = data.get("expired_at", "-")
    signature    = data.get("signature", "")
    create_order(order_id, user_id, total_amount, slots, bonus, signature)
    bonus_line  = f"🎁 Bonus      : `+{bonus} slot`\n" if bonus > 0 else ""
    exp_slot    = f"⏰ Slot berlaku: *{SLOT_EXPIRY_DAYS} hari*\n" if SLOT_EXPIRY_DAYS > 0 else ""
    extend_note = "\n🔄 *Slot lama akan diperpanjang otomatis!*\n" if SLOT_EXPIRY_DAYS > 0 else ""
    caption = (
        "━━━━━━━━━━━━━━━━━\n   💳 *Tagihan Top Up*\n━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 Order ID   : `{order_id}`\n"
        f"💰 Bayar      : *Rp{total_amount:,}*\n"
        f"📦 Slot       : `+{slots} slot`\n"
        f"{bonus_line}"
        f"✨ Total Dapat : *`{total_get} slot`*\n"
        f"{exp_slot}{extend_note}"
        f"⏰ QRIS Exp   : `{expired_at}`\n\n"
        "Scan QRIS di bawah ini untuk membayar.\nStatus akan otomatis diperbarui."
    )
    try:
        await msg_wait.delete()
        await update.message.reply_photo(photo=qris_url, caption=caption, parse_mode="Markdown")
    except:
        await msg_wait.edit_text(caption + f"\n\n🖼️ [Lihat QRIS]({qris_url})", parse_mode="Markdown")
    asyncio.create_task(_poll_payment(update.effective_chat.id, user_id, order_id, slots, bonus, total_amount, context))

async def _poll_payment(chat_id, user_id, order_id, paid_slots, order_bonus, amount, context):
    print(f"🔄 Polling: {order_id} | user={user_id} | slots={paid_slots} | bonus={order_bonus}")
    for attempt in range(720):
        await asyncio.sleep(PAYMENT_POLL_INTERVAL)
        resp = await asyncio.get_running_loop().run_in_executor(None, lambda oid=order_id: qris_status(oid))
        if not resp or not resp.get("status"): continue
        data = resp.get("data")
        if not data: continue
        trx_status = (data.get("status") or "PENDING").strip().upper()
        print(f"📊 [{order_id}] attempt {attempt+1}: {trx_status}")
        if trx_status in ("SUCCESS", "PAID"):
            uid, processed_slots, stored_bonus = complete_order(order_id)
            # FIX: processed_slots dari DB adalah sumber kebenaran
            # Kalau 0 berarti sudah diproses sebelumnya (race condition) — skip
            if processed_slots > 0:
                # FIX: Pakai processed_slots dari DB, bukan paid_slots dari closure
                # Ini mencegah mismatch jika closure menangkap nilai yang salah
                actual_bonus = stored_bonus if stored_bonus > 0 else order_bonus
                total_to_add = processed_slots + actual_bonus

                # FIX: add_slot_batch DULU, baru extend_user_slot_expiry
                # Sebelumnya: extend dipanggil sebelum add → slot baru tidak ikut di-extend
                add_slot_batch(user_id, total_to_add, "topup")

                # Extend SETELAH slot baru ditambahkan — semua batch (termasuk yang baru) ikut
                extended_batches = extend_user_slot_expiry(user_id) if SLOT_EXPIRY_DAYS > 0 else 0

                # Baca saldo SETELAH semua operasi selesai
                udata = get_user_data(user_id)

                bonus_line = f"🎁 Bonus   : `+{actual_bonus}` slot\n" if actual_bonus > 0 else ""
                exp_line   = f"⏰ Berlaku : *{SLOT_EXPIRY_DAYS} hari*\n" if SLOT_EXPIRY_DAYS > 0 else ""
                extend_line = ""
                if SLOT_EXPIRY_DAYS > 0 and extended_batches > 0:
                    new_exp = _expiry_dt()
                    extend_line = (
                        f"🔄 Diperpanjang : `{extended_batches}` batch slot lama\n"
                        f"   → Expired baru: `{new_exp} WIB`\n"
                    )
                msg = (
                    "✅ *Pembayaran Diterima!*\n\n"
                    f"🆔 Order   : `{order_id}`\n"
                    f"💰 Nominal : Rp{amount:,}\n"
                    f"📦 Slot +  : `{processed_slots}` slot\n"
                    f"{bonus_line}"
                    f"➕ Total   : `+{total_to_add}` slot\n"
                    f"{exp_line}"
                    f"{extend_line}"
                    f"📦 Saldo   : `{udata['slots']}` slot\n\n"
                    "Gunakan /getemail untuk mulai!"
                )
                print(f"✅ Topup OK: {order_id} → +{total_to_add} slot ke user {user_id} | saldo={udata['slots']}")
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            else:
                print(f"⚠️ complete_order {order_id} returned 0 slots — sudah diproses sebelumnya, skip.")
            return
        if trx_status == "EXPIRED":
            expire_order(order_id)
            await context.bot.send_message(chat_id=chat_id, text=f"⏰ *Tagihan Kedaluwarsa*\n\nOrder `{order_id}` sudah expired.", parse_mode="Markdown")
            return
    expire_order(order_id)

# ============================================================
# BROADCAST V2
# ============================================================

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    user_state[update.effective_user.id] = {"state": "broadcast_choose_type"}
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━\n   📡 *Broadcast V2*\n━━━━━━━━━━━━━━━━━\n\n"
        "Pilih tipe konten yang ingin dikirim:\n\n"
        "📝 *Teks* — kirim pesan teks biasa\n"
        "🖼️ *Foto + Caption* — kirim foto dengan teks\n"
        "🎬 *Video + Caption* — kirim video dengan teks\n\n"
        "⚠️ Pesan akan dikirim ke *semua user* bot.",
        parse_mode="Markdown",
        reply_markup=keyboard_broadcast_type()
    )

async def _show_broadcast_preview(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    state = user_state.get(user_id)
    if not state: return
    bc_type    = state["bc_type"]
    bc_content = state["bc_content"]
    bc_caption = state.get("bc_caption", "")
    type_labels = {"text": "📝 Teks", "photo": "🖼️ Foto + Caption", "video": "🎬 Video + Caption"}
    total_users = len(get_all_user_ids())
    header = (
        "━━━━━━━━━━━━━━━━━\n   👁️ *Preview Broadcast*\n━━━━━━━━━━━━━━━━━\n\n"
        f"📋 Tipe   : {type_labels.get(bc_type, bc_type)}\n"
        f"👥 Target : `{total_users}` user\n\n"
        "👇 *Preview konten:*\n"
    )
    if bc_type == "text":
        preview_msg = await context.bot.send_message(chat_id=user_id, text=f"{header}{bc_content}", parse_mode="Markdown")
    elif bc_type == "photo":
        preview_msg = await context.bot.send_photo(chat_id=user_id, photo=bc_content, caption=f"{header}{bc_caption}" if bc_caption else header, parse_mode="Markdown")
    elif bc_type == "video":
        preview_msg = await context.bot.send_video(chat_id=user_id, video=bc_content, caption=f"{header}{bc_caption}" if bc_caption else header, parse_mode="Markdown")
    else:
        return
    await context.bot.send_message(
        chat_id=user_id,
        text="⚠️ *Pastikan preview di atas sudah benar!*\n\nTekan tombol di bawah untuk mengirim ke semua user.",
        parse_mode="Markdown",
        reply_markup=keyboard_broadcast_confirm()
    )
    state["bc_preview_msg_id"] = preview_msg.message_id

async def _execute_broadcast(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    state = user_state.get(user_id)
    if not state: return
    bc_type    = state["bc_type"]
    bc_content = state["bc_content"]
    bc_caption = state.get("bc_caption", "")
    all_ids = get_all_user_ids()
    total   = len(all_ids)

    user_state.pop(user_id, None)

    if total == 0:
        await context.bot.send_message(chat_id=user_id, text="📭 *Tidak ada user untuk di-broadcast.*", parse_mode="Markdown")
        return
    preview_id = state.get("bc_preview_msg_id")
    if preview_id:
        try: await context.bot.delete_message(chat_id=user_id, message_id=preview_id)
        except: pass
    type_labels = {"text": "📝 Teks", "photo": "🖼️ Foto", "video": "🎬 Video"}
    status_msg = await context.bot.send_message(
        chat_id=user_id,
        text=(
            "━━━━━━━━━━━━━━━━━\n   📡 *Broadcast Berjalan...*\n━━━━━━━━━━━━━━━━━\n\n"
            f"📋 Tipe  : {type_labels.get(bc_type, bc_type)}\n"
            f"👥 Total : `{total}` user\n\n"
            f"⏳ `[0/{total}]`\n"
            f"{'░' * 20} 0%"
        ),
        parse_mode="Markdown"
    )
    ok = 0; fail = 0; blocked = 0
    bar_width = 20
    for i, uid in enumerate(all_ids):
        try:
            if bc_type == "text":
                await context.bot.send_message(chat_id=uid, text=bc_content, parse_mode="Markdown")
            elif bc_type == "photo":
                await context.bot.send_photo(chat_id=uid, photo=bc_content, caption=bc_caption if bc_caption else None, parse_mode="Markdown" if bc_caption else None)
            elif bc_type == "video":
                await context.bot.send_video(chat_id=uid, video=bc_content, caption=bc_caption if bc_caption else None, parse_mode="Markdown" if bc_caption else None)
            ok += 1
        except Exception as e:
            err_str = str(e).lower()
            if "blocked" in err_str or "banned" in err_str:
                blocked += 1
            fail += 1
        if (i + 1) % BROADCAST_BATCH_SIZE == 0 or (i + 1) == total:
            done   = i + 1
            pct    = int((done / total) * 100)
            filled = int((done / total) * bar_width)
            bar    = "█" * filled + "░" * (bar_width - filled)
            try:
                await status_msg.edit_text(
                    "━━━━━━━━━━━━━━━━━\n   📡 *Broadcast Berjalan...*\n━━━━━━━━━━━━━━━━━\n\n"
                    f"📋 Tipe  : {type_labels.get(bc_type, bc_type)}\n"
                    f"👥 Total : `{total}` user\n\n"
                    f"⏳ `[{done}/{total}]`\n"
                    f"{bar} {pct}%\n\n"
                    f"✅ Terkirim : `{ok}`\n"
                    f"❌ Gagal    : `{fail}`"
                    + (f"\n🚫 Blocked  : `{blocked}`" if blocked > 0 else ""),
                    parse_mode="Markdown"
                )
            except: pass
        await asyncio.sleep(BROADCAST_DELAY)
    bar = "█" * bar_width
    await status_msg.edit_text(
        "━━━━━━━━━━━━━━━━━\n   ✅ *Broadcast Selesai!*\n━━━━━━━━━━━━━━━━━\n\n"
        f"📋 Tipe  : {type_labels.get(bc_type, bc_type)}\n"
        f"👥 Total : `{total}` user\n\n"
        f"⏳ `[{total}/{total}]`\n"
        f"{bar} 100%\n\n"
        f"✅ Terkirim : `{ok}`\n"
        f"❌ Gagal    : `{fail}`"
        + (f"\n🚫 Blocked  : `{blocked}`" if blocked > 0 else ""),
        parse_mode="Markdown"
    )

# ============================================================
# USER HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    udata = get_user_data(user_id)
    name  = update.effective_user.first_name or "kamu"
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━\n      📬 *GacorMail Bot*\n━━━━━━━━━━━━━━━━━\n\n"
        f"👋 Hai {name}!\n🎁 *Kamu punya {udata['slots']} Slot Email*\n\n"
        "📋 *Perintah:*\n├ /getemail  — Buat email baru\n├ /getotp    — Ambil OTP manual\n"
        "├ /myemails  — Email aktif sesi ini\n├ /topup     — Top up slot via QRIS\n"
        "├ /top       — Leaderboard OTP\n├ /myslots   — Detail slot & expiry\n"
        "└ /deleteall — Hapus email sesi ini\n\n⚡ *OTP realtime 1-2 detik!*\n\n━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )

async def getemail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    udata = get_user_data(user_id)
    if udata["slots"] <= 0:
        await update.message.reply_text("⛔ *Slot Habis!*\n\nGunakan /topup untuk beli slot.", parse_mode="Markdown")
        return
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━\n   🌐 *Pilih Domain Email*\n━━━━━━━━━━━━━━━━━\n\n"
        f"Sisa Slot: `{udata['slots']}`\nPilih domain yang ingin digunakan:",
        parse_mode="Markdown", reply_markup=keyboard_domain()
    )

async def getotp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    udata = get_user_data(user_id)
    if udata["slots"] <= 0:
        await update.message.reply_text(
            "⛔ *Slot Habis!*\n\nKamu perlu slot untuk menggunakan fitur ini.\nGunakan /topup untuk beli slot.",
            parse_mode="Markdown"
        )
        return
    if context.args:
        em = context.args[0].strip().lower()
        if "@" not in em:
            await update.message.reply_text("⚠️ *Format salah!*\nContoh: `/getotp abc@bahlil.cfd`", parse_mode="Markdown")
            return
        msg = await update.message.reply_text(f"⏳ *Mencari OTP...*\n\n📧 `{em}`", parse_mode="Markdown")
        await do_search_otp(em, msg.edit_text, keyboard_coba_lagi_manual, user_id)
        return
    user_state[user_id] = {"state": "waiting_email_otp"}
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━\n   🔍 *Ambil OTP Manual*\n━━━━━━━━━━━━━━━━━\n\n"
        "📩 Ketik alamat email yang ingin dicek:\n_(contoh: `abc123@bahlil.cfd`)_",
        parse_mode="Markdown"
    )

async def topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    get_user_data(user_id)
    user_state[user_id] = {"state": "waiting_topup_amount"}
    bonus_line  = f"🎁 Bonus  : +{BONUS_SLOTS_PER_TOPUP} slot / topup\n" if BONUS_SLOTS_PER_TOPUP > 0 else ""
    exp_line    = f"⏰ Slot berlaku {SLOT_EXPIRY_DAYS} hari\n" if SLOT_EXPIRY_DAYS > 0 else ""
    extend_info = "🔄 Top up = perpanjang slot lama\n" if SLOT_EXPIRY_DAYS > 0 else ""
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━\n   💳 *Top Up Slot*\n━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Harga  : Rp{PRICE_PER_SLOT:,} / slot\n📦 Min    : Rp{TOPUP_MIN:,}\n{bonus_line}{exp_line}{extend_info}"
        "Silakan ketik nominal top up kamu:\n_(Contoh: `5000`)_",
        parse_mode="Markdown"
    )

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_top_otp_users(10)
    if not rows:
        await update.message.reply_text("📭 Belum ada data OTP.", parse_mode="Markdown")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines  = ""
    for i, (uid, cnt) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        try:
            chat = await context.bot.get_chat(uid)
            name = chat.first_name or str(uid)
        except:
            name = str(uid)
        lines += f"{medal} *{name}* — `{cnt}` OTP\n"
    await update.message.reply_text(f"━━━━━━━━━━━━━━━━━\n   🏆 *Top OTP Leaderboard*\n━━━━━━━━━━━━━━━━━\n\n{lines}", parse_mode="Markdown")

async def cmd_fbcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    if not context.args:
        await update.message.reply_text("⚠️ *Format:* `/fbcheck <email>`", parse_mode="Markdown")
        return
    em = context.args[0].strip().lower()
    if "@" not in em:
        await update.message.reply_text("⚠️ Format email tidak valid.", parse_mode="Markdown")
        return
    msg    = await update.message.reply_text(f"🔍 *Mengecek email...*\n\n📧 `{em}`", parse_mode="Markdown")
    result = await check_fb_checkpoint(em)
    log_fb_check(em, result)
    texts = {
        "ok":         f"✅ *Email Aman!*\n\n📧 `{em}`\n\nTidak terdeteksi checkpoint oleh Facebook.",
        "checkpoint": f"⛔ *Kena Checkpoint!*\n\n📧 `{em}`\n\nEmail terdeteksi suspicious oleh Facebook.",
        "used":       f"⚠️ *Email Sudah Dipakai!*\n\n📧 `{em}`\n\nEmail ini sudah terdaftar di Facebook.",
        "error":      f"❓ *Gagal Cek*\n\n📧 `{em}`\n\nTidak bisa terhubung ke Facebook.",
    }
    await safe_edit(msg.edit_text, texts.get(result, texts["error"]))

async def cmd_fbcheck_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    emails = user_emails.get(user_id, [])
    if not emails:
        await update.message.reply_text("📭 *Tidak ada email aktif.*\n\nBuat email dulu dengan /getemail", parse_mode="Markdown")
        return
    msg = await update.message.reply_text(f"🔍 *Mengecek {len(emails)} email...*", parse_mode="Markdown")
    results = {"ok": [], "checkpoint": [], "used": [], "error": []}
    for i, em in enumerate(emails):
        try:
            await safe_edit(msg.edit_text, f"🔍 *Mengecek email {i+1}/{len(emails)}...*\n\n📧 `{em}`")
        except: pass
        result = await check_fb_checkpoint(em)
        log_fb_check(em, result)
        results[result].append(em)
        await asyncio.sleep(1)
    ok_list   = "\n".join([f"  ✅ `{e}`" for e in results["ok"]]) or "  _tidak ada_"
    cp_list   = "\n".join([f"  ⛔ `{e}`" for e in results["checkpoint"]]) or "  _tidak ada_"
    used_list = "\n".join([f"  ⚠️ `{e}`" for e in results["used"]]) or "  _tidak ada_"
    err_list  = "\n".join([f"  ❓ `{e}`" for e in results["error"]]) or "  _tidak ada_"
    await safe_edit(msg.edit_text,
        f"━━━━━━━━━━━━━━━━━\n   📊 *Hasil Cek FB Checkpoint*\n━━━━━━━━━━━━━━━━━\n\n"
        f"✅ *Aman* ({len(results['ok'])}):\n{ok_list}\n\n"
        f"⛔ *Checkpoint* ({len(results['checkpoint'])}):\n{cp_list}\n\n"
        f"⚠️ *Sudah Dipakai* ({len(results['used'])}):\n{used_list}\n\n"
        f"❓ *Error* ({len(results['error'])}):\n{err_list}"
    )

async def cmd_myslots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    udata   = get_user_data(user_id)
    batches = get_slot_batches_user(user_id)
    if not batches:
        await update.message.reply_text(f"📦 *Info Slot Kamu*\n\nSisa Slot: `{udata['slots']}`\n\n_Belum ada data batch. Gunakan /topup untuk beli slot._", parse_mode="Markdown")
        return
    source_map = {"starterpack": "🎁 Starterpack", "topup": "💳 Topup", "admin": "👑 Admin", "legacy": "📦 Legacy"}
    lines = []
    for src, total, remaining, expired_at, created_at in batches:
        label   = source_map.get(src, f"📦 {src}")
        exp_str = f"`{expired_at} WIB`" if expired_at else "♾️ Permanen"
        lines.append(f"{label} | +{total} → sisa `{remaining}` | exp: {exp_str}")
    exp_info = ""
    if SLOT_EXPIRY_DAYS > 0:
        exp_info = (
            f"\n⏰ Slot baru berlaku *{SLOT_EXPIRY_DAYS} hari* sejak diperoleh.\n"
            "🔄 *Top up lagi untuk perpanjang semua slot yang masih aktif!*"
        )
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━\n   📦 *Detail Slot Kamu*\n━━━━━━━━━━━━━━━━━\n\n"
        f"✨ *Total Aktif: {udata['slots']} slot*\n\n{chr(10).join(lines)}\n{exp_info}",
        parse_mode="Markdown"
    )

async def myemails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    udata  = get_user_data(user_id)
    emails = user_emails.get(user_id, [])
    if not emails:
        await update.message.reply_text(f"📭 *Belum ada email aktif*\n\n💳 Sisa Slot: `{udata['slots']}`", parse_mode="Markdown")
        return
    await update.message.reply_text(f"📦 *Email Aktif:* {len(emails)} | 💳 Sisa Slot: `{udata['slots']}`", parse_mode="Markdown")
    for i, em in enumerate(emails):
        await update.message.reply_text(
            f"🚀 *{SERVER_NAME} {i+1}/{len(emails)}:*\n`{em}`\n📋 Tap untuk copy.",
            parse_mode="Markdown",
            reply_markup=keyboard_ambil_otp(em)
        )

async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_group_membership(user_id, context):
        await send_not_member_message(update, context)
        return
    emails = user_emails.get(user_id, [])
    count  = len(emails)
    with otp_lock:
        for em in emails:
            em_lower = em.lower()
            otp_history.pop(em_lower, None)
            # FIX: Hapus timestamp entries untuk email ini
            ts_keys = [k for k in otp_sent_timestamps if k.startswith(f"{em_lower}:")]
            for k in ts_keys:
                otp_sent_timestamps.pop(k, None)
            email_owners.pop(em, None)
    user_emails[user_id] = []
    await update.message.reply_text(f"🗑️ *{count} email dihapus.*", parse_mode="Markdown")

# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    data    = query.data
    user_id = query.from_user.id

    try:
        await query.answer()
    except Exception:
        pass

    if not _check_admin(user_id):
        if not await check_group_membership(user_id, context):
            try:
                await query.edit_message_text(
                    "⛔ *Akses Ditolak*\n\nJoin grup dulu.",
                    parse_mode="Markdown",
                    reply_markup=keyboard_join_group()
                )
            except: pass
            return

    if data == "cancel":
        try:
            await query.edit_message_text("❌ *Dibatalkan.*", parse_mode="Markdown")
        except: pass
        user_state.pop(user_id, None)
        return

    if data.startswith("domain:"):
        domain = data[7:]
        udata  = get_user_data(user_id)
        user_state[user_id] = {"state": "waiting_count", "domain": domain}
        try:
            await query.edit_message_text(
                f"━━━━━━━━━━━━━━━━━\n   🌐 *Domain:* `@{domain}`\n━━━━━━━━━━━━━━━━━\n\n"
                f"💳 Sisa Slot: `{udata['slots']}`\n\n📩 Ketik jumlah email:\n_(maks 20)_",
                parse_mode="Markdown"
            )
        except: pass
        return

    if data.startswith("otp:"):
        em = data[4:]
        asyncio.create_task(do_search_otp(em, query.edit_message_text, keyboard_coba_lagi, user_id))
        return

    if data.startswith("otpmanual:"):
        em = data[10:]
        asyncio.create_task(do_search_otp(em, query.edit_message_text, keyboard_coba_lagi_manual, user_id))
        return

    if not _check_admin(user_id):
        return

    if data == "bc_cancel":
        user_state.pop(user_id, None)
        try:
            await query.edit_message_text("❌ *Broadcast dibatalkan.*", parse_mode="Markdown")
        except: pass
        return

    if data.startswith("bc_type:"):
        bc_type = data[8:]
        type_labels = {"text": "📝 Teks", "photo": "🖼️ Foto + Caption", "video": "🎬 Video + Caption"}
        user_state[user_id] = {"state": "broadcast_waiting_content", "bc_type": bc_type}
        hints = {
            "text":  "Kirim *pesan teks* yang ingin di-broadcast.\n\nGunakan format Markdown untuk styling.",
            "photo": "Kirim *foto* yang ingin di-broadcast.\n\n_Tambahkan caption di bagian teks foto._",
            "video": "Kirim *video* yang ingin di-broadcast.\n\n_Tambahkan caption di bagian teks video._",
        }
        try:
            await query.edit_message_text(
                f"━━━━━━━━━━━━━━━━━\n   📡 *Broadcast — {type_labels.get(bc_type, bc_type)}*\n"
                f"━━━━━━━━━━━━━━━━━\n\n{hints.get(bc_type, '')}\n\n❌ Ketik /cancel untuk batal.",
                parse_mode="Markdown"
            )
        except: pass
        return

    if data.startswith("bc_confirm:"):
        choice = data[11:]
        if choice == "no":
            user_state.pop(user_id, None)
            try:
                await query.message.reply_text("❌ *Broadcast dibatalkan.*", parse_mode="Markdown")
            except: pass
            return
        if choice == "yes":
            if user_id not in user_state:
                try:
                    await query.answer("⚠️ Broadcast sudah berjalan!", show_alert=True)
                except:
                    pass
                return
            try:
                await query.message.delete()
            except: pass
            asyncio.create_task(_execute_broadcast(user_id, context))
            return

# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state   = user_state.get(user_id)
    text    = update.message.text.strip()

    if state and state.get("state") == "broadcast_waiting_content" and state.get("bc_type") == "text":
        if _check_admin(user_id):
            state["bc_content"] = text
            state["bc_caption"] = ""
            state["state"] = "broadcast_waiting_confirm"
            await safe_delete(update.message)
            await _show_broadcast_preview(user_id, context)
            return

    if state and state.get("state") == "waiting_topup_amount":
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Masukkan angka saja!\nContoh: `5000`", parse_mode="Markdown")
            return
        if amount < TOPUP_MIN:
            await update.message.reply_text(f"⚠️ Minimal top up Rp{TOPUP_MIN:,}", parse_mode="Markdown")
            return
        user_state.pop(user_id, None)
        await process_topup_payment(user_id, amount, update, context)
        return

    if not state:
        return

    if not await check_group_membership(user_id, context):
        user_state.pop(user_id, None)
        await update.message.reply_text("⛔ *Akses Ditolak*\n\nJoin grup dulu ya!", parse_mode="Markdown", reply_markup=keyboard_join_group())
        return

    # ============================================================
    # FIX #3: Generate email PARALEL — semua email dikirim sekaligus
    # Masalah lama: await reply satu per satu → terasa lambat & kadang
    # pesan pertama saja yang muncul kalau ada error di tengah.
    # Solusi: generate semua email dulu, consume slot sekaligus,
    # lalu kirim semua reply secara paralel dengan asyncio.gather.
    # ============================================================
    if state.get("state") == "waiting_count":
        if not text.isdigit():
            await update.message.reply_text("⚠️ *Masukkan angka saja!*\nContoh: `1`", parse_mode="Markdown")
            return
        udata  = get_user_data(user_id)
        count  = max(1, min(int(text), 20, udata["slots"]))
        domain = state["domain"]
        user_state.pop(user_id, None)
        if count == 0 or udata["slots"] <= 0:
            await update.message.reply_text("⛔ *Slot habis!* Gunakan /topup", parse_mode="Markdown")
            return

        # Cek ulang slot yang tersedia setelah di-cap
        available_slots = get_valid_slots(user_id)
        count = min(count, available_slots)
        if count <= 0:
            await update.message.reply_text("⛔ *Slot habis!* Gunakan /topup", parse_mode="Markdown")
            return

        await update.message.reply_text(f"⚡ *Membuat {count} email @{domain}...*", parse_mode="Markdown")

        # Generate semua email sekaligus
        generated_emails = []
        for _ in range(count):
            em = generate_random_email(domain)
            generated_emails.append(em)

        # Consume slot sekaligus (atomic)
        ok = consume_slot_batch(user_id, count)
        if not ok:
            await update.message.reply_text("⛔ *Slot tidak cukup!* Gunakan /topup", parse_mode="Markdown")
            return

        # Daftarkan semua email ke memory
        if user_id not in user_emails:
            user_emails[user_id] = []
        with otp_lock:
            for em in generated_emails:
                user_emails[user_id].append(em)
                email_owners[em] = user_id
                increment_email_count(user_id)

        # Kirim semua pesan secara PARALEL
        total = len(generated_emails)
        async def _send_one(idx: int, em: str):
            await update.message.reply_text(
                f"🚀 *{SERVER_NAME} {idx+1}/{total}:*\n`{em}`\n📋 Tap untuk copy.",
                parse_mode="Markdown",
                reply_markup=keyboard_ambil_otp(em)
            )

        await asyncio.gather(*[_send_one(i, em) for i, em in enumerate(generated_emails)])

        # Trigger scan background
        threading.Thread(target=cache_all_emails_throttled, daemon=True).start()
        return

    if state.get("state") == "waiting_email_otp":
        em = text.lower()
        if "@" not in em:
            await update.message.reply_text("⚠️ *Format salah!*\nContoh: `abc123@bahlil.cfd`", parse_mode="Markdown")
            return
        user_state.pop(user_id, None)
        msg = await update.message.reply_text(f"⏳ *Mencari OTP...*\n\n📧 `{em}`", parse_mode="Markdown")
        await do_search_otp(em, msg.edit_text, keyboard_coba_lagi_manual, user_id)
        return

# ============================================================
# MEDIA HANDLER
# ============================================================

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state   = user_state.get(user_id)
    if not state or state.get("state") != "broadcast_waiting_content":
        return
    if not _check_admin(user_id):
        return
    bc_type = state.get("bc_type")
    caption = update.message.caption or ""
    if bc_type == "photo" and update.message.photo:
        file_id = update.message.photo[-1].file_id
        state["bc_content"] = file_id
        state["bc_caption"] = caption
        state["state"] = "broadcast_waiting_confirm"
        await safe_delete(update.message)
        await _show_broadcast_preview(user_id, context)
        return
    if bc_type == "video" and update.message.video:
        file_id = update.message.video.file_id
        state["bc_content"] = file_id
        state["bc_caption"] = caption
        state["state"] = "broadcast_waiting_confirm"
        await safe_delete(update.message)
        await _show_broadcast_preview(user_id, context)
        return

# ============================================================
# ADMIN HANDLERS
# ============================================================

async def admin_addslots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Format: `/addslots <user_id> <jumlah>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
        amount    = int(context.args[1])
        get_user_data(target_id)
        add_slot_batch(target_id, amount, "admin")
        udata    = get_user_data(target_id)
        exp_info = f" (berlaku {SLOT_EXPIRY_DAYS} hari)" if SLOT_EXPIRY_DAYS > 0 else " (permanen)"
        await update.message.reply_text(f"✅ Tambah `{amount}` slot ke `{target_id}`{exp_info}\n📦 Total: `{udata['slots']}`", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text=f"🎁 *Admin menambahkan {amount} slot!*{exp_info}\n📦 Total: `{udata['slots']}`", parse_mode="Markdown")
        except: pass
    except ValueError:
        await update.message.reply_text("⚠️ Input invalid.", parse_mode="Markdown")

async def admin_setslots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Format: `/setslots <user_id> <jumlah>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
        amount    = int(context.args[1])
        get_user_data(target_id)
        with db() as conn:
            conn.execute("UPDATE users SET slots=? WHERE user_id=?", (amount, target_id))
            conn.commit()
        await update.message.reply_text(f"✅ Slot `{target_id}` → `{amount}`.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("⚠️ Input invalid.", parse_mode="Markdown")

async def admin_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("⚠️ Format: `/setgroup <group_id> <link>`", parse_mode="Markdown")
        return
    try:
        gid  = int(context.args[0])
        link = context.args[1]
        if not link.startswith("http") and not link.startswith("@"):
            link = f"@{link}"
        save_group_config(gid, link)
        await update.message.reply_text(f"✅ Grup diperbarui!\n\nID: `{gid}`\nLink: `{link}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("⚠️ Group ID harus angka.", parse_mode="Markdown")

async def admin_deletegroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    save_group_config("", "")
    await update.message.reply_text("✅ *Syarat Grup Dihapus!*", parse_mode="Markdown")

async def admin_setdomainname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    if len(context.args) < 2:
        domains = get_domains(active_only=False)
        lines = "".join([f"• `{d}` → {get_domain_label(d)}\n" for d in domains])
        await update.message.reply_text(
            f"⚠️ *Format:* `/setdomainname <domain> <label>`\n\n*Domain:*\n{lines}"
            f"*Contoh:* `/setdomainname bahlil.cfd 🔥 Server Utama`",
            parse_mode="Markdown"
        )
        return
    domain = context.args[0].lower()
    label  = " ".join(context.args[1:])
    all_domains = get_domains(active_only=False)
    if domain not in all_domains:
        await update.message.reply_text(
            f"❌ Domain `{domain}` tidak ada.\n\nTersedia: `{'`, `'.join(all_domains)}`",
            parse_mode="Markdown"
        )
        return
    set_domain_label(domain, label)
    with db() as conn:
        conn.execute("UPDATE domains SET label=? WHERE domain=?", (label, domain))
        conn.commit()
    await update.message.reply_text(
        f"✅ *Button diperbarui!*\n\nDomain: `{domain}`\nButton: {label}",
        parse_mode="Markdown"
    )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    total_users   = len(get_all_user_ids())
    active_emails = len(email_owners)
    with db() as conn:
        total_slots = conn.execute("SELECT SUM(slots) FROM users").fetchone()[0] or 0
        total_otp   = conn.execute("SELECT CAST(value AS INTEGER) FROM bot_stats WHERE key='total_otp'").fetchone()[0] or 0
        topup_row   = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM topup_orders WHERE status='SUCCESS'").fetchone()
    bonus_status = f"`+{BONUS_SLOTS_PER_TOPUP} slot/topup`" if BONUS_SLOTS_PER_TOPUP > 0 else "`Nonaktif`"
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━\n   📊 *Admin Stats*\n━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Total User   : `{total_users}`\n📧 Email Aktif  : `{active_emails}`\n"
        f"🎫 Total Slot   : `{total_slots}`\n🔥 Total OTP    : `{total_otp}`\n"
        f"💰 Topup OK     : `{topup_row[0]}x` (Rp{int(topup_row[1]):,})\n\n"
        f"━━━ *Pengaturan Harga* ━━━\n"
        f"💲 Harga/Slot   : `Rp{PRICE_PER_SLOT:,}`\n📦 Min Topup    : `Rp{TOPUP_MIN:,}`\n"
        f"🎁 Bonus/Topup  : {bonus_status}\n\n"
        f"━━━ *Pengaturan Slot* ━━━\n"
        f"⏰ Expired Slot : `{SLOT_EXPIRY_DAYS} hari` (0=permanen)\n"
        f"📡 Scan Interval: `{POLL_INTERVAL}s`\n"
        f"🔌 IMAP Pool    : `{_POOL_SIZE} koneksi`\n\n"
        "━━━ *Perintah Kelola* ━━━\n"
        "/setprice · /setbonus · /setexpiry\n"
        "/adddomain · /deldomain · /changedomain · /listdomains\n"
        "/setdomainname · /setgroup · /broadcast\n"
        "/top · /runexpiry",
        parse_mode="Markdown"
    )

async def admin_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    global PRICE_PER_SLOT, TOPUP_MIN
    if len(context.args) < 1:
        await update.message.reply_text(
            f"Format:\n`/setprice <harga>` — ubah harga saja\n"
            f"`/setprice <harga> <min>` — ubah harga + min topup",
            parse_mode="Markdown"
        )
        return
    try:
        PRICE_PER_SLOT = int(context.args[0])
        if len(context.args) >= 2:
            TOPUP_MIN = int(context.args[1])
        await update.message.reply_text(
            f"✅ Harga: `Rp{PRICE_PER_SLOT:,}` | Min: `Rp{TOPUP_MIN:,}`",
            parse_mode="Markdown"
        )
    except:
        await update.message.reply_text("⚠️ Input invalid.", parse_mode="Markdown")

async def admin_setbonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    global BONUS_SLOTS_PER_TOPUP
    if not context.args:
        await update.message.reply_text(
            f"🎁 Bonus saat ini: `{BONUS_SLOTS_PER_TOPUP}`\nFormat: `/setbonus <jumlah>`",
            parse_mode="Markdown"
        )
        return
    try:
        bonus = int(context.args[0])
        if bonus < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Masukkan angka >= 0.", parse_mode="Markdown")
        return
    BONUS_SLOTS_PER_TOPUP = bonus
    with db() as conn:
        conn.execute("UPDATE bot_stats SET value=? WHERE key='bonus_slots_per_topup'", (str(bonus),))
        conn.commit()
    await update.message.reply_text(f"✅ Bonus: `+{bonus} slot/topup`", parse_mode="Markdown")

async def admin_setexpiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SLOT_EXPIRY_DAYS
    if not _check_admin(update.effective_user.id): return
    if not context.args:
        status = f"{SLOT_EXPIRY_DAYS} hari" if SLOT_EXPIRY_DAYS > 0 else "Permanen"
        await update.message.reply_text(
            f"⏰ Status: *{status}*\nFormat: `/setexpiry <hari>` | `0` = permanen",
            parse_mode="Markdown"
        )
        return
    try:
        days = int(context.args[0])
        if days < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Hari harus angka >= 0.", parse_mode="Markdown")
        return
    SLOT_EXPIRY_DAYS = days
    with db() as conn:
        conn.execute("UPDATE bot_stats SET value=? WHERE key='slot_expiry_days'", (str(days),))
        conn.commit()
    msg = f"✅ Slot expired setelah *{days} hari*" if days > 0 else "✅ Slot diset *Permanen*"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def admin_runexpiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    msg      = await update.message.reply_text("⏳ *Menjalankan expired slot...*", parse_mode="Markdown")
    affected = expire_slots_now()
    await msg.edit_text(
        f"✅ *Expired slot selesai!*\n\n👥 User terdampak: `{affected}`",
        parse_mode="Markdown"
    )

async def admin_fbstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    with db() as conn:
        total  = conn.execute("SELECT COUNT(*) FROM fb_checkpoint_log").fetchone()[0]
        ok_cnt = conn.execute("SELECT COUNT(*) FROM fb_checkpoint_log WHERE status='ok'").fetchone()[0]
        cp_cnt = conn.execute("SELECT COUNT(*) FROM fb_checkpoint_log WHERE status='checkpoint'").fetchone()[0]
        us_cnt = conn.execute("SELECT COUNT(*) FROM fb_checkpoint_log WHERE status='used'").fetchone()[0]
        er_cnt = conn.execute("SELECT COUNT(*) FROM fb_checkpoint_log WHERE status='error'").fetchone()[0]
    await update.message.reply_text(
        f"📊 *FB Checkpoint Stats*\n\nTotal: `{total}` | ✅`{ok_cnt}` ⛔`{cp_cnt}` ⚠️`{us_cnt}` ❓`{er_cnt}`",
        parse_mode="Markdown"
    )

async def admin_adddomain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text(
            "Format: `/adddomain <domain> [label]`\nContoh: `/adddomain surabaya.cfd 🔥 Server Surabaya`",
            parse_mode="Markdown"
        )
        return
    domain = context.args[0].strip().lower()
    label  = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
    if not domain or "." not in domain:
        await update.message.reply_text("⚠️ Domain tidak valid!", parse_mode="Markdown")
        return
    if domain in get_domains(active_only=False):
        await update.message.reply_text(f"⚠️ Domain `{domain}` sudah ada!", parse_mode="Markdown")
        return
    ok = add_domain_db(domain, label)
    if ok:
        await update.message.reply_text(f"✅ Domain `{domain}` ditambahkan!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Gagal menambahkan domain.", parse_mode="Markdown")

async def admin_deldomain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    if not context.args:
        domains = get_domains(active_only=False)
        lines = "\n".join([f"• `{d}`" for d in domains])
        await update.message.reply_text(f"Format: `/deldomain <domain>`\n\n{lines}", parse_mode="Markdown")
        return
    domain = context.args[0].strip().lower()
    if domain not in get_domains(active_only=False):
        await update.message.reply_text(f"❌ Domain `{domain}` tidak ditemukan.", parse_mode="Markdown")
        return
    ok = del_domain_db(domain)
    if ok:
        await update.message.reply_text(f"✅ Domain `{domain}` dihapus!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Gagal menghapus domain.", parse_mode="Markdown")

async def admin_changedomain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    if len(context.args) < 2:
        domains = get_domains(active_only=False)
        lines = "\n".join([f"• `{d}`" for d in domains])
        await update.message.reply_text(
            f"Format: `/changedomain <lama> <baru>`\n\n{lines}",
            parse_mode="Markdown"
        )
        return
    old_domain = context.args[0].strip().lower()
    new_domain = context.args[1].strip().lower()
    all_domains = get_domains(active_only=False)
    if old_domain not in all_domains:
        await update.message.reply_text(f"❌ Domain `{old_domain}` tidak ditemukan.", parse_mode="Markdown")
        return
    if new_domain in all_domains:
        await update.message.reply_text(f"❌ Domain `{new_domain}` sudah digunakan!", parse_mode="Markdown")
        return
    ok = update_domain_db(old_domain, new_domain)
    if ok:
        await update.message.reply_text(f"✅ `{old_domain}` → `{new_domain}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Gagal mengubah domain.", parse_mode="Markdown")

async def admin_listdomains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    info = get_domain_full_info()
    if not info:
        await update.message.reply_text("📭 Belum ada domain.", parse_mode="Markdown")
        return
    lines = []
    for i, d in enumerate(info):
        status = "🟢" if d["active"] else "🔴"
        label = get_domain_label(d["domain"])
        lines.append(f"{i+1}. {status} `{d['domain']}` — {label}")
    await update.message.reply_text(
        "🌐 *Domain List*\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

async def admin_toggledomain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Format: `/toggledomain <domain>`", parse_mode="Markdown")
        return
    domain = context.args[0].strip().lower()
    result = toggle_domain_db(domain)
    if result is None:
        await update.message.reply_text(f"❌ Domain `{domain}` tidak ditemukan.", parse_mode="Markdown")
        return
    status = "🟢 Aktif" if result else "🔴 Nonaktif"
    await update.message.reply_text(f"✅ Domain `{domain}` → {status}", parse_mode="Markdown")

# ============================================================
# ADMIN: REPAIR SLOTS
# ============================================================

async def admin_repairslots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_admin(update.effective_user.id): return
    msg = await update.message.reply_text("Memeriksa & memperbaiki slot...", parse_mode="Markdown")

    if context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await msg.edit_text("User ID harus angka.", parse_mode="Markdown")
            return
        old_val = get_valid_slots(target_id)
        new_val = repair_user_slots(target_id)
        diff = new_val - old_val
        sign = "+" if diff >= 0 else ""
        result = (
            "*Repair Slot Selesai!*\n\n"
            + "User ID  : `" + str(target_id) + "`\n"
            + "Sebelum  : `" + str(old_val) + "` slot\n"
            + "Sesudah  : `" + str(new_val) + "` slot\n"
            + "Selisih  : `" + sign + str(diff) + "` slot"
        )
        await msg.edit_text(result, parse_mode="Markdown")
        return

    all_ids = get_all_user_ids()
    fixed = 0
    total = len(all_ids)
    for uid in all_ids:
        old_val = get_valid_slots(uid)
        new_val = repair_user_slots(uid)
        if old_val != new_val:
            fixed += 1
    result_all = (
        "*Repair Semua User Selesai!*\n\n"
        + "Total user  : `" + str(total) + "`\n"
        + "Diperbaiki  : `" + str(fixed) + "` user\n"
        + "Sudah benar : `" + str(total - fixed) + "` user"
    )
    await msg.edit_text(result_all, parse_mode="Markdown")

# ============================================================
# MAIN
# ============================================================

def main():
    print(f"🚀 Starting GacorMail Bot... [WIB {now_wib_str()}]")
    print(f"   OTP Cooldown   : {OTP_RESEND_COOLDOWN}s")
    print(f"   IMAP Pool Size : {_POOL_SIZE} koneksi")
    print(f"   Scan delay     : {SCAN_BATCH_DELAY}s/email")
    print(f"   Domains        : {', '.join(get_domains(active_only=False))}")

    auto_kill_existing()

    try:
        mail = get_imap_connection()
        mail.logout()
        print("✅ IMAP Connected!")
    except Exception as e:
        print(f"❌ IMAP Error: {e}")
        return

    threading.Thread(target=_warm_imap_pool, daemon=True).start()
    threading.Thread(target=imap_idle_thread, daemon=True).start()
    print("⚡ IMAP IDLE thread started!")
    threading.Thread(target=imap_poll_thread, daemon=True).start()
    print("🔄 Backup poll thread started!")
    threading.Thread(target=slot_expiry_thread, daemon=True).start()
    print("⏰ Slot expiry thread started!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("getemail",     getemail))
    app.add_handler(CommandHandler("getotp",       getotp))
    app.add_handler(CommandHandler("myemails",     myemails))
    app.add_handler(CommandHandler("deleteall",    deleteall))
    app.add_handler(CommandHandler("topup",        topup))
    app.add_handler(CommandHandler("top",          cmd_top))
    app.add_handler(CommandHandler("fbcheck",      cmd_fbcheck))
    app.add_handler(CommandHandler("fbcheckall",   cmd_fbcheck_bulk))
    app.add_handler(CommandHandler("myslots",      cmd_myslots))

    app.add_handler(CommandHandler("broadcast",      admin_broadcast))
    app.add_handler(CommandHandler("addslots",       admin_addslots))
    app.add_handler(CommandHandler("setslots",       admin_setslots))
    app.add_handler(CommandHandler("stats",          admin_stats))
    app.add_handler(CommandHandler("setprice",       admin_setprice))
    app.add_handler(CommandHandler("setbonus",       admin_setbonus))
    app.add_handler(CommandHandler("setgroup",       admin_setgroup))
    app.add_handler(CommandHandler("deletegroup",    admin_deletegroup))
    app.add_handler(CommandHandler("setdomainname",  admin_setdomainname))
    app.add_handler(CommandHandler("adddomain",      admin_adddomain))
    app.add_handler(CommandHandler("deldomain",      admin_deldomain))
    app.add_handler(CommandHandler("changedomain",   admin_changedomain))
    app.add_handler(CommandHandler("listdomains",    admin_listdomains))
    app.add_handler(CommandHandler("toggledomain",   admin_toggledomain))
    app.add_handler(CommandHandler("fbstats",        admin_fbstats))
    app.add_handler(CommandHandler("setexpiry",      admin_setexpiry))
    app.add_handler(CommandHandler("runexpiry",      admin_runexpiry))
    app.add_handler(CommandHandler("repairslots",    admin_repairslots))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, media_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("✅ Bot jalan! ⚡")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
