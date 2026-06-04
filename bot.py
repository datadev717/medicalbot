import os
import sqlite3
import threading
import time
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN env topilmadi. Render (Environment) bo‘limida BOT_TOKEN ni qo‘ying.")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # Set your Telegram user ID here
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# Test mode: 1 hour = 3600 seconds instead of 80 days
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
NOTIFY_SECONDS = 3600 if TEST_MODE else  1 * 3600  # 1 hour test OR 80 days

app = Flask(__name__)

import hashlib
import secrets as _secrets

# ─────────────────────────── PANEL TOKEN ───────────────────────────

def make_panel_token(telegram_id):
    """Shifokor uchun xavfsiz token yaratadi (BOT_TOKEN + telegram_id asosida)."""
    secret = TOKEN or "fallback"
    raw = f"{secret}:{telegram_id}:panel"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_panel_url(telegram_id):
    token = make_panel_token(telegram_id)
    base = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    return f"{base}/panel/{telegram_id}/{token}"


def get_admin_panel_url():
    token = make_panel_token(ADMIN_ID)
    base = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    return f"{base}/admin/panel/{token}"

# ─────────────────────────── DATABASE ───────────────────────────

DB_PATH = os.environ.get("DB_PATH", "medical_bot.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS doctors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            name TEXT NOT NULL,
            username TEXT,
            approved INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            birth_year INTEGER,
            phone TEXT,
            disease TEXT,
            address TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            notified INTEGER DEFAULT 0,
            notify_at TEXT,
            status TEXT DEFAULT 'pending',
            reminder_count INTEGER DEFAULT 0,
            next_remind_at TEXT,
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        );

    """)
    # Safe migrations for existing DBs
    for col, definition in [
        ("status", "TEXT DEFAULT 'pending'"),
        ("reminder_count", "INTEGER DEFAULT 0"),
        ("next_remind_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE patients ADD COLUMN {col} {definition}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    logger.info("DB initialized.")

# ─────────────────────────── TELEGRAM API ───────────────────────────

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        logger.error(f"sendMessage error: {e}")
        return {}

def answer_callback(callback_query_id, text=""):
    try:
        requests.post(f"{BASE_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": text
        }, timeout=10)
    except Exception as e:
        logger.error(f"answerCallbackQuery error: {e}")

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{BASE_URL}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"editMessageText error: {e}")

def set_webhook(url):
    r = requests.post(f"{BASE_URL}/setWebhook", json={"url": url, "drop_pending_updates": True})
    logger.info(f"setWebhook response: {r.json()}")

# ─────────────────────────── SESSION (in-memory) ───────────────────────────
# user_state[chat_id] = {"step": ..., "data": {...}}
user_state = {}
state_lock = threading.Lock()

def get_state(chat_id):
    with state_lock:
        return user_state.get(chat_id, {})

def set_state(chat_id, state):
    with state_lock:
        user_state[chat_id] = state

def clear_state(chat_id):
    with state_lock:
        user_state.pop(chat_id, None)

# ─────────────────────────── HELPERS ───────────────────────────

def is_admin(telegram_id):
    return ADMIN_ID and telegram_id == ADMIN_ID

def get_doctor(telegram_id):
    conn = get_db()
    doc = conn.execute("SELECT * FROM doctors WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return doc

def is_approved_doctor(telegram_id):
    doc = get_doctor(telegram_id)
    return doc and doc["approved"] == 1

def main_menu_kb():
    return {
        "inline_keyboard": [
            [{"text": "➕ Bemor qo'shish", "callback_data": "add_patient"}],
            [{"text": "📋 Bemorlar ro'yxati", "callback_data": "list_patients"}],
            [{"text": "🔍 Bemor statusi", "callback_data": "patient_status"}],
            [{"text": "🌐 Mening paneliم", "callback_data": "open_panel"}],
        ]
    }

def admin_menu_kb():
    return {
        "inline_keyboard": [
            [{"text": "👨‍⚕️ Shifokorlar ro'yxati", "callback_data": "admin_doctors"}],
            [{"text": "✅ Shifokor tasdiqlash", "callback_data": "admin_approve"}],
            [{"text": "❌ Shifokor bloklash", "callback_data": "admin_block"}],
            [{"text": "📊 Barcha bemorlar", "callback_data": "admin_all_patients"}],
            [{"text": "📈 Statistika", "callback_data": "admin_stats"}],
            [{"text": "🛡️ Admin Panel (Web)", "callback_data": "open_panel"}],
        ]
    }

# ─────────────────────────── NOTIFY WORKER ───────────────────────────

RETRY_SECONDS = 7 * 24 * 3600   # 7 kun (real); testda 5 daqiqa
RETRY_SECONDS_TEST = 5 * 60


def get_retry_seconds():
    return RETRY_SECONDS_TEST if TEST_MODE else RETRY_SECONDS


def status_label(status):
    return {
        "pending":      "⏳ Kutilmoqda",
        "retrying":     "🔄 Qayta eslatiladi",
        "contacted":    "✅ Bog'lanildi",
        "unreachable":  "❌ Aloqaga chiqilmadi",
    }.get(status, "⏳ Kutilmoqda")


def send_reminder(doc_tg, patient_id, full_name, birth_year, phone, disease, address, notes, created_at, reminder_count):
    attempt_text = ["1-eslatma", "2-eslatma", "3-eslatma"][min(reminder_count, 2)]
    msg = (
        f"⏰ <b>Eslatma! ({attempt_text})</b>\n\n"
        f"Bemor: <b>{full_name}</b>\n"
        f"Tug'ilgan yili: {birth_year}\n"
        f"Telefon: {phone}\n"
        f"Kasallik: {disease}\n"
        f"Manzil: {address}\n"
        f"Izoh: {notes or '-'}\n\n"
        f"📅 Qo'shilgan: {created_at}\n"
        f"{'⚠️ TEST rejimi' if TEST_MODE else '⚠️ 80 kun otdi!'}\n\n"
        f"Bemor bilan bog'landingizmi?"
    )
    kb = {
        "inline_keyboard": [[
            {"text": "✅ Bog'lanildi", "callback_data": f"rem_yes_{patient_id}"},
            {"text": "❌ Bog'lanilmadi", "callback_data": f"rem_no_{patient_id}"},
        ]]
    }
    send_message(doc_tg, msg, reply_markup=kb)


def notify_worker():
    logger.info(f"Notify worker started. Mode: {'TEST (1 soat)' if TEST_MODE else '80 kun'}")
    while True:
        try:
            conn = get_db()
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")

            # 1) Birinchi eslatma (80 kun o'tib, hali pending)
            first_patients = conn.execute(
                "SELECT p.*, d.telegram_id as doc_tg FROM patients p "
                "JOIN doctors d ON p.doctor_id = d.id "
                "WHERE p.status='pending' AND p.notify_at <= ?",
                (now_str,)
            ).fetchall()
            for p in first_patients:
                send_reminder(p["doc_tg"], p["id"], p["full_name"], p["birth_year"],
                              p["phone"], p["disease"], p["address"], p["notes"], p["created_at"], 0)
                conn.execute(
                    "UPDATE patients SET notified=1, status='retrying', reminder_count=1 WHERE id=?",
                    (p["id"],)
                )
                conn.commit()
                logger.info(f"1-eslatma -> bemor {p['full_name']} (doc {p['doc_tg']})")

            # 2) Qayta eslatmalar (status='retrying', next_remind_at vaqti keldi)
            retry_patients = conn.execute(
                "SELECT p.*, d.telegram_id as doc_tg FROM patients p "
                "JOIN doctors d ON p.doctor_id = d.id "
                "WHERE p.status='retrying' AND p.next_remind_at IS NOT NULL AND p.next_remind_at <= ?",
                (now_str,)
            ).fetchall()
            for p in retry_patients:
                rc = p["reminder_count"] or 1
                if rc >= 3:
                    conn.execute(
                        "UPDATE patients SET status='unreachable', next_remind_at=NULL WHERE id=?",
                        (p["id"],)
                    )
                    conn.commit()
                    logger.info(f"Bemor {p['full_name']} -> unreachable (max urinish)")
                else:
                    send_reminder(p["doc_tg"], p["id"], p["full_name"], p["birth_year"],
                                  p["phone"], p["disease"], p["address"], p["notes"], p["created_at"], rc)
                    conn.execute(
                        "UPDATE patients SET reminder_count=?, next_remind_at=NULL WHERE id=?",
                        (rc + 1, p["id"])
                    )
                    conn.commit()
                    logger.info(f"Qayta eslatma #{rc+1} -> bemor {p['full_name']}")

            conn.close()
        except Exception as e:
            logger.error(f"Notify worker error: {e}")
        time.sleep(60)  # har daqiqa tekshir



# ─────────────────────────── BOOTSTRAP (Gunicorn/Render uchun) ───────────────────────────

_BOOTSTRAPPED = False

def bootstrap():
    """
    Render/Gunicorn ishga tushganda __main__ ishlamaydi.
    Shu sabab DB init va background worker import paytida ishga tushishi kerak.
    Eslatma: gunicorn workers>1 bo‘lsa, worker’lar ko‘payib ketadi (duplikat eslatmalar).
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    init_db()

    if os.environ.get("DISABLE_WORKER", "false").lower() != "true":
        t = threading.Thread(target=notify_worker, daemon=True)
        t.start()

    # Ixtiyoriy: server ishga tushganda webhook’ni avtomatik o‘rnatish
    if os.environ.get("AUTO_SET_WEBHOOK", "false").lower() == "true":
        public_url = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
        if public_url:
            try:
                set_webhook(f"{public_url}/webhook")
            except Exception as e:
                logger.error(f"AUTO_SET_WEBHOOK error: {e}")

    _BOOTSTRAPPED = True


bootstrap()

# ─────────────────────────── HANDLERS ───────────────────────────

def handle_start(chat_id, telegram_id, user):
    full_name = f"{user.get('first_name','')} {user.get('last_name','')}".strip()
    username = user.get("username", "")

    if is_admin(telegram_id):
        send_message(chat_id,
            f"👋 Xush kelibsiz, Admin <b>{full_name}</b>!\n\nAdmin paneli:",
            reply_markup=admin_menu_kb())
        return

    doc = get_doctor(telegram_id)
    if not doc:
        # Register new doctor
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO doctors (telegram_id, name, username) VALUES (?,?,?)",
            (telegram_id, full_name, username)
        )
        conn.commit()
        conn.close()
        send_message(chat_id,
            f"👋 Salom, Dr. <b>{full_name}</b>!\n\n"
            "✅ Ro'yxatdan o'tdingiz. Admin tasdiqlashini kuting.\n"
            "Tasdiqlanganingizda xabar beramiz.")
        # Notify admin
        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"🆕 Yangi shifokor ro'yxatdan o'tdi:\n"
                f"Ism: <b>{full_name}</b>\n"
                f"Username: @{username}\n"
                f"ID: <code>{telegram_id}</code>\n\n"
                f"Tasdiqlash uchun /approve_{telegram_id}")
        return

    if doc["approved"] == 0:
        send_message(chat_id, "⏳ Sizning so'rovingiz hali tasdiqlanmagan. Kuting.")
        return

    send_message(chat_id,
        f"👋 Xush kelibsiz, Dr. <b>{full_name}</b>!\n\nNimа qilmoqchisiz?",
        reply_markup=main_menu_kb())


def handle_add_patient_start(chat_id):
    set_state(chat_id, {"step": "add_name", "data": {}})
    send_message(chat_id, "📝 Bemorning <b>ismi va familiyasini</b> kiriting:")


def handle_list_patients(chat_id, telegram_id):
    conn = get_db()
    doc = conn.execute("SELECT id FROM doctors WHERE telegram_id=?", (telegram_id,)).fetchone()
    if not doc:
        conn.close()
        send_message(chat_id, "❌ Siz shifokor sifatida topilmadingiz.")
        return
    patients = conn.execute(
        "SELECT * FROM patients WHERE doctor_id=? ORDER BY created_at DESC LIMIT 20",
        (doc["id"],)
    ).fetchall()
    conn.close()

    if not patients:
        send_message(chat_id, "📭 Sizda hozircha bemorlar yo'q.")
        return

    text = "📋 <b>Bemorlaringiz ro'yxati:</b>\n\n"
    for i, p in enumerate(patients, 1):
        st = p["status"] if p["status"] else ("contacted" if p["notified"] else "pending")
        status = status_label(st)
        if st == "pending":
            status += f" ({p['notify_at'][:16]} ga eslatma)"
        elif st == "retrying":
            nra = p["next_remind_at"] or "?"
            status += f" (keyingi: {nra[:16]})"
        text += (
            f"{i}. <b>{p['full_name']}</b>\n"
            f"   📅 {p['birth_year']} | 📞 {p['phone']}\n"
            f"   🏥 {p['disease']}\n"
            f"   {status}\n\n"
        )
    send_message(chat_id, text)


def handle_patient_status(chat_id, telegram_id):
    set_state(chat_id, {"step": "status_search", "data": {}})
    send_message(chat_id, "🔍 Bemor ismini kiriting (qidirish uchun):")


def handle_admin_doctors(chat_id):
    conn = get_db()
    docs = conn.execute("SELECT * FROM doctors ORDER BY created_at DESC").fetchall()
    conn.close()
    if not docs:
        send_message(chat_id, "Hozircha hech kim ro'yxatdan o'tmagan.")
        return
    text = "👨‍⚕️ <b>Shifokorlar ro'yxati:</b>\n\n"
    for d in docs:
        status = "✅ Tasdiqlangan" if d["approved"] else "⏳ Kutmoqda"
        text += f"• <b>{d['name']}</b> (@{d['username']})\n  ID: <code>{d['telegram_id']}</code> | {status}\n\n"
    send_message(chat_id, text)


def handle_admin_approve_list(chat_id):
    conn = get_db()
    docs = conn.execute("SELECT * FROM doctors WHERE approved=0").fetchall()
    conn.close()
    if not docs:
        send_message(chat_id, "✅ Tasdiqlanmagan shifokorlar yo'q.")
        return
    kb = {"inline_keyboard": [
        [{"text": f"✅ {d['name']}", "callback_data": f"approve_{d['telegram_id']}"}]
        for d in docs
    ]}
    send_message(chat_id, "Qaysi shifokorni tasdiqlaysiz?", reply_markup=kb)


def handle_admin_block_list(chat_id):
    conn = get_db()
    docs = conn.execute("SELECT * FROM doctors WHERE approved=1").fetchall()
    conn.close()
    if not docs:
        send_message(chat_id, "Faol shifokorlar yo'q.")
        return
    kb = {"inline_keyboard": [
        [{"text": f"❌ {d['name']}", "callback_data": f"block_{d['telegram_id']}"}]
        for d in docs
    ]}
    send_message(chat_id, "Qaysi shifokorni bloklaysiz?", reply_markup=kb)


def handle_approve_doctor(chat_id, doc_tg_id):
    conn = get_db()
    conn.execute("UPDATE doctors SET approved=1 WHERE telegram_id=?", (doc_tg_id,))
    conn.commit()
    doc = conn.execute("SELECT * FROM doctors WHERE telegram_id=?", (doc_tg_id,)).fetchone()
    conn.close()
    send_message(chat_id, f"✅ Dr. <b>{doc['name']}</b> tasdiqlandi!")
    send_message(doc_tg_id,
        "🎉 Siz tasdiqlandi! Endi botdan foydalanishingiz mumkin.\n/start",
    )


def handle_block_doctor(chat_id, doc_tg_id):
    conn = get_db()
    conn.execute("UPDATE doctors SET approved=0 WHERE telegram_id=?", (doc_tg_id,))
    conn.commit()
    doc = conn.execute("SELECT * FROM doctors WHERE telegram_id=?", (doc_tg_id,)).fetchone()
    conn.close()
    send_message(chat_id, f"❌ Dr. <b>{doc['name']}</b> bloklandi.")
    send_message(doc_tg_id, "⛔ Sizning kirishingiz vaqtincha to'xtatildi.")


def handle_admin_all_patients(chat_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT p.*, d.name as doc_name FROM patients p "
        "JOIN doctors d ON p.doctor_id=d.id ORDER BY p.created_at DESC LIMIT 30"
    ).fetchall()
    conn.close()
    if not rows:
        send_message(chat_id, "📭 Hozircha bemorlar yo'q.")
        return
    text = "📊 <b>Barcha bemorlar (oxirgi 30):</b>\n\n"
    for p in rows:
        status = "✅" if p["notified"] else "⏳"
        text += f"{status} <b>{p['full_name']}</b> — Dr. {p['doc_name']}\n   🏥 {p['disease']} | {p['created_at'][:10]}\n\n"
    send_message(chat_id, text)


def handle_admin_stats(chat_id):
    conn = get_db()
    total_docs = conn.execute("SELECT COUNT(*) FROM doctors WHERE approved=1").fetchone()[0]
    total_patients = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    contacted = conn.execute("SELECT COUNT(*) FROM patients WHERE status='contacted'").fetchone()[0]
    retrying = conn.execute("SELECT COUNT(*) FROM patients WHERE status='retrying'").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM patients WHERE status='pending'").fetchone()[0]
    unreachable = conn.execute("SELECT COUNT(*) FROM patients WHERE status='unreachable'").fetchone()[0]
    conn.close()
    send_message(chat_id,
        f"📈 <b>Statistika:</b>\n\n"
        f"👨‍⚕️ Faol shifokorlar: {total_docs}\n"
        f"🧑‍🤝‍🧑 Jami bemorlar: {total_patients}\n\n"
        f"⏳ Kutilmoqda: {pending}\n"
        f"🔄 Qayta eslatiladi: {retrying}\n"
        f"✅ Bog'lanildi: {contacted}\n"
        f"❌ Aloqaga chiqilmadi: {unreachable}\n"
        f"\n⚙️ Rejim: {'TEST (5 daqiqa retry)' if TEST_MODE else '80 kun / 7 kun retry'}"
    )


# ─────────────────────────── TEXT MESSAGE STEPS ───────────────────────────

def handle_text_steps(chat_id, telegram_id, text):
    state = get_state(chat_id)
    step = state.get("step")
    data = state.get("data", {})

    # ── Add patient flow ──
    if step == "add_name":
        data["full_name"] = text
        set_state(chat_id, {"step": "add_birth_year", "data": data})
        send_message(chat_id, "📅 Tug'ilgan yilini kiriting (masalan: 1985):")

    elif step == "add_birth_year":
        if not text.isdigit() or not (1900 < int(text) < 2025):
            send_message(chat_id, "❌ Noto'g'ri yil. Iltimos, to'g'ri yil kiriting:")
            return
        data["birth_year"] = int(text)
        set_state(chat_id, {"step": "add_phone", "data": data})
        send_message(chat_id, "📞 Telefon raqamini kiriting:")

    elif step == "add_phone":
        data["phone"] = text
        set_state(chat_id, {"step": "add_disease", "data": data})
        send_message(chat_id, "🏥 Kasallik turini kiriting:")

    elif step == "add_disease":
        data["disease"] = text
        set_state(chat_id, {"step": "add_address", "data": data})
        send_message(chat_id, "🏠 Yashash manzilini kiriting:")

    elif step == "add_address":
        data["address"] = text
        set_state(chat_id, {"step": "add_notes", "data": data})
        send_message(chat_id, "📝 Qo'shimcha izoh kiriting (yoki 'yo'q' deb yozing):")

    elif step == "add_notes":
        data["notes"] = "" if text.lower() in ("yo'q", "yoq", "-", "no") else text
        # Save to DB
        conn = get_db()
        doc = conn.execute("SELECT id FROM doctors WHERE telegram_id=?", (telegram_id,)).fetchone()
        if not doc:
            conn.close()
            send_message(chat_id, "❌ Xato: Shifokor topilmadi.")
            clear_state(chat_id)
            return

        notify_at = (datetime.now() + timedelta(seconds=NOTIFY_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO patients (doctor_id, full_name, birth_year, phone, disease, address, notes, notify_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (doc["id"], data["full_name"], data["birth_year"], data["phone"],
             data["disease"], data["address"], data["notes"], notify_at)
        )
        conn.commit()
        conn.close()
        clear_state(chat_id)
        send_message(chat_id,
            f"✅ <b>Bemor muvaffaqiyatli qo'shildi!</b>\n\n"
            f"👤 Ism: {data['full_name']}\n"
            f"📅 Tug'ilgan yil: {data['birth_year']}\n"
            f"📞 Telefon: {data['phone']}\n"
            f"🏥 Kasallik: {data['disease']}\n"
            f"🏠 Manzil: {data['address']}\n"
            f"📝 Izoh: {data['notes'] or '-'}\n\n"
            f"⏰ Eslatma: {notify_at}\n"
            f"{'(TEST: 1 soatdan keyin)' if TEST_MODE else '(80 kundan keyin)'}",
            reply_markup=main_menu_kb()
        )

    # ── Patient status search ──
    elif step == "status_search":
        conn = get_db()
        doc = conn.execute("SELECT id FROM doctors WHERE telegram_id=?", (telegram_id,)).fetchone()
        if not doc:
            conn.close()
            clear_state(chat_id)
            return
        patients = conn.execute(
            "SELECT * FROM patients WHERE doctor_id=? AND full_name LIKE ?",
            (doc["id"], f"%{text}%")
        ).fetchall()
        conn.close()
        clear_state(chat_id)

        if not patients:
            send_message(chat_id, f"❌ '{text}' nomli bemor topilmadi.", reply_markup=main_menu_kb())
            return

        result = "🔍 <b>Topilgan bemorlar:</b>\n\n"
        for p in patients:
            st = p["status"] if p["status"] else ("contacted" if p["notified"] else "pending")
            status = status_label(st)
            if st == "pending":
                status += f" ({p['notify_at'][:16]} ga eslatma)"
            elif st == "retrying":
                nra = p["next_remind_at"] or "?"
                status += f" (keyingi: {nra[:16]})"
            result += (
                f"👤 <b>{p['full_name']}</b>\n"
                f"   📅 {p['birth_year']} | 📞 {p['phone']}\n"
                f"   🏥 {p['disease']}\n"
                f"   🏠 {p['address']}\n"
                f"   📝 {p['notes'] or '-'}\n"
                f"   {status}\n"
                f"   🗓 Qo'shilgan: {p['created_at'][:16]}\n\n"
            )
        send_message(chat_id, result, reply_markup=main_menu_kb())

    else:
        # No active state — show menu
        if is_admin(telegram_id):
            send_message(chat_id, "Admin panel:", reply_markup=admin_menu_kb())
        elif is_approved_doctor(telegram_id):
            send_message(chat_id, "Menyu:", reply_markup=main_menu_kb())




def handle_reminder_yes(chat_id, patient_id, message_id):
    """Shifokor bemor bilan bog'landi deb bildirdi."""
    conn = get_db()
    p = conn.execute("SELECT full_name FROM patients WHERE id=?", (patient_id,)).fetchone()
    if p:
        conn.execute(
            "UPDATE patients SET status='contacted', next_remind_at=NULL WHERE id=?",
            (patient_id,)
        )
        conn.commit()
        edit_message(chat_id, message_id,
            f"✅ <b>{p['full_name']}</b> bilan bog'landi deb belgilandi.\n"
            f"Status: ✅ Bog'lanildi"
        )
        logger.info(f"Patient {patient_id} marked as contacted")
    conn.close()


def handle_reminder_no(chat_id, patient_id, message_id):
    """Shifokor bemor bilan bog'lana olmadi."""
    conn = get_db()
    p = conn.execute("SELECT full_name, reminder_count FROM patients WHERE id=?", (patient_id,)).fetchone()
    if p:
        rc = p["reminder_count"] or 1
        if rc >= 3:
            # 3 marta ham urinib bo'ldi
            conn.execute(
                "UPDATE patients SET status='unreachable', next_remind_at=NULL WHERE id=?",
                (patient_id,)
            )
            conn.commit()
            edit_message(chat_id, message_id,
                f"❌ <b>{p['full_name']}</b> bilan 3 marta ham bog'lanib bo'lmadi.\n"
                f"Status: ❌ Aloqaga chiqilmadi\n\n"
                f"Keyingi eslatmalar to'xtatildi."
            )
            logger.info(f"Patient {patient_id} marked as unreachable after 3 attempts")
        else:
            # 7 kundan keyin qayta eslatish
            retry_at = (datetime.now() + timedelta(seconds=get_retry_seconds())).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE patients SET status='retrying', reminder_count=?, next_remind_at=? WHERE id=?",
                (rc, retry_at, patient_id)
            )
            conn.commit()
            days_text = "5 daqiqadan" if TEST_MODE else "7 kundan"
            edit_message(chat_id, message_id,
                f"🔄 <b>{p['full_name']}</b> — {days_text} keyin qayta eslatiladi.\n"
                f"Status: 🔄 Qayta eslatiladi\n"
                f"Urinish: {rc}/3"
            )
            logger.info(f"Patient {patient_id} scheduled retry #{rc} at {retry_at}")
    conn.close()

# ─────────────────────────── UPDATE DISPATCHER ───────────────────────────

def process_update(update):
    try:
        # Callback query
        if "callback_query" in update:
            cq = update["callback_query"]
            cq_id = cq["id"]
            data = cq.get("data", "")
            chat_id = cq["message"]["chat"]["id"]
            telegram_id = cq["from"]["id"]
            answer_callback(cq_id)

            if not is_admin(telegram_id) and not is_approved_doctor(telegram_id):
                send_message(chat_id, "⛔ Sizda ruxsat yo'q.")
                return

            if data == "add_patient":
                handle_add_patient_start(chat_id)
            elif data == "list_patients":
                handle_list_patients(chat_id, telegram_id)
            elif data == "patient_status":
                handle_patient_status(chat_id, telegram_id)
            elif data == "admin_doctors":
                handle_admin_doctors(chat_id)
            elif data == "admin_approve":
                handle_admin_approve_list(chat_id)
            elif data == "admin_block":
                handle_admin_block_list(chat_id)
            elif data == "admin_all_patients":
                handle_admin_all_patients(chat_id)
            elif data == "admin_stats":
                handle_admin_stats(chat_id)
            elif data.startswith("approve_"):
                doc_tg = int(data.split("_")[1])
                handle_approve_doctor(chat_id, doc_tg)
            elif data.startswith("block_"):
                doc_tg = int(data.split("_")[1])
                handle_block_doctor(chat_id, doc_tg)
            elif data.startswith("rem_yes_"):
                patient_id = int(data.split("_")[2])
                handle_reminder_yes(chat_id, patient_id, cq["message"]["message_id"])
            elif data.startswith("rem_no_"):
                patient_id = int(data.split("_")[2])
                handle_reminder_no(chat_id, patient_id, cq["message"]["message_id"])
            elif data == "open_panel":
                pub = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
                if not pub:
                    send_message(chat_id, "❌ PUBLIC_URL env o'rnatilmagan.")
                else:
                    if is_admin(telegram_id):
                        url = get_admin_panel_url()
                        send_message(chat_id,
                            f"🛡️ <b>Superadmin Panel:</b>\n\n"
                            f"<a href=\"{url}\">👆 Bu yerga bosing</a>\n\n"
                            f"Barcha shifokorlar va bemorlar ko'rinadi.")
                    else:
                        url = get_panel_url(telegram_id)
                        send_message(chat_id,
                            f"🌐 <b>Sizning panelingiz:</b>\n\n"
                            f"<a href=\"{url}\">👆 Bu yerga bosing</a>\n\n"
                            f"Havola faqat siz uchun — boshqalarga bermang!")
            return

        # Regular message
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            telegram_id = msg["from"]["id"]
            user = msg["from"]
            text = msg.get("text", "")

            if text.startswith("/start"):
                clear_state(chat_id)
                handle_start(chat_id, telegram_id, user)

            elif text.startswith("/admin") and is_admin(telegram_id):
                send_message(chat_id, "Admin panel:", reply_markup=admin_menu_kb())

            elif text.startswith("/approve_") and is_admin(telegram_id):
                try:
                    doc_tg = int(text.split("_")[1])
                    handle_approve_doctor(chat_id, doc_tg)
                except Exception:
                    send_message(chat_id, "Noto'g'ri format.")

            elif text.startswith("/stats") and is_admin(telegram_id):
                handle_admin_stats(chat_id)

            elif text.startswith("/panel") and (is_admin(telegram_id) or is_approved_doctor(telegram_id)):
                pub = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
                if not pub:
                    send_message(chat_id, "❌ PUBLIC_URL env o'rnatilmagan. Render da PUBLIC_URL ni qo'ying.")
                else:
                    if is_admin(telegram_id):
                        url = get_admin_panel_url()
                        send_message(chat_id,
                            f"🛡️ <b>Superadmin Panel:</b>\n\n"
                            f"<a href=\"{url}\">👆 Bu yerga bosing</a>\n\n"
                            f"Barcha shifokorlar va bemorlar ko'rinadi.")
                    else:
                        url = get_panel_url(telegram_id)
                        send_message(chat_id,
                            f"🌐 <b>Sizning panelingiz:</b>\n\n"
                            f"<a href=\"{url}\">👆 Bu yerga bosing</a>\n\n"
                            f"Havola faqat siz uchun — boshqalarga bermang!")

            elif text.startswith("/testmode") and is_admin(telegram_id):
                mode = "TEST (1 soat)" if TEST_MODE else "REAL (80 kun)"
                send_message(chat_id, f"⚙️ Joriy rejim: {mode}\nO'zgartirish uchun TEST_MODE env o'zgartiring.")

            elif text:
                if is_admin(telegram_id):
                    # Admin can also use doctor functions if they want
                    state = get_state(chat_id)
                    if state.get("step"):
                        handle_text_steps(chat_id, telegram_id, text)
                    else:
                        send_message(chat_id, "Admin panel:", reply_markup=admin_menu_kb())
                elif is_approved_doctor(telegram_id):
                    handle_text_steps(chat_id, telegram_id, text)
                else:
                    doc = get_doctor(telegram_id)
                    if doc:
                        send_message(chat_id, "⏳ Sizning so'rovingiz hali tasdiqlanmagan.")
                    else:
                        send_message(chat_id, "Iltimos, /start buyrug'ini bosing.")
    except Exception as e:
        logger.error(f"process_update error: {e}", exc_info=True)


# ─────────────────────────── FLASK ROUTES ───────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "bot": "Medical Bot", "mode": "TEST (1h)" if TEST_MODE else "PROD (80d)"})

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if update:
        threading.Thread(target=process_update, args=(update,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/set_webhook", methods=["GET"])
def setup_webhook():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "url parameter required"}), 400
    set_webhook(f"{url}/webhook")
    return jsonify({"ok": True, "webhook": f"{url}/webhook"})

@app.route("/health", methods=["GET"])
def health():
    conn = get_db()
    docs = conn.execute("SELECT COUNT(*) FROM doctors").fetchone()[0]
    patients = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    conn.close()
    return jsonify({"status": "healthy", "doctors": docs, "patients": patients})


# ─────────────────────────── PANEL HTML ───────────────────────────

PANEL_HTML = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shifokor Paneli</title>
<style>
  :root {
    --primary: #2563eb;
    --primary-light: #eff6ff;
    --success: #16a34a;
    --success-light: #f0fdf4;
    --warning: #d97706;
    --warning-light: #fffbeb;
    --danger: #dc2626;
    --danger-light: #fef2f2;
    --gray: #6b7280;
    --bg: #f8fafc;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #1e293b;
    --text-light: #64748b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

  header { background: linear-gradient(135deg, #1e40af 0%, #2563eb 100%); color: white; padding: 20px 24px; }
  header h1 { font-size: 1.4rem; font-weight: 700; }
  header p { font-size: 0.85rem; opacity: 0.8; margin-top: 4px; }

  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; padding: 20px 20px 0; }
  .stat-card { background: var(--card); border-radius: 12px; padding: 16px; border: 1px solid var(--border); text-align: center; }
  .stat-card .number { font-size: 2rem; font-weight: 800; line-height: 1; }
  .stat-card .label { font-size: 0.75rem; color: var(--text-light); margin-top: 6px; }
  .stat-card.pending .number { color: var(--warning); }
  .stat-card.retrying .number { color: var(--primary); }
  .stat-card.contacted .number { color: var(--success); }
  .stat-card.unreachable .number { color: var(--danger); }

  .section { padding: 20px; }
  .section h2 { font-size: 1rem; font-weight: 600; margin-bottom: 12px; color: var(--text-light); text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.75rem; }

  .filter-bar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  .filter-btn { padding: 6px 14px; border-radius: 20px; border: 1.5px solid var(--border); background: white; cursor: pointer; font-size: 0.82rem; font-weight: 500; transition: all 0.15s; }
  .filter-btn:hover { border-color: var(--primary); color: var(--primary); }
  .filter-btn.active { background: var(--primary); color: white; border-color: var(--primary); }
  .filter-btn.f-pending.active { background: var(--warning); border-color: var(--warning); }
  .filter-btn.f-retrying.active { background: var(--primary); border-color: var(--primary); }
  .filter-btn.f-contacted.active { background: var(--success); border-color: var(--success); }
  .filter-btn.f-unreachable.active { background: var(--danger); border-color: var(--danger); }

  .search-box { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1.5px solid var(--border); font-size: 0.9rem; margin-bottom: 14px; outline: none; transition: border 0.15s; }
  .search-box:focus { border-color: var(--primary); }

  .patient-card { background: var(--card); border-radius: 12px; border: 1px solid var(--border); margin-bottom: 10px; overflow: hidden; transition: box-shadow 0.15s; }
  .patient-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.07); }
  .card-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px 10px; }
  .card-name { font-weight: 700; font-size: 1rem; }
  .badge { font-size: 0.72rem; font-weight: 600; padding: 3px 10px; border-radius: 20px; white-space: nowrap; }
  .badge-pending { background: var(--warning-light); color: var(--warning); }
  .badge-retrying { background: var(--primary-light); color: var(--primary); }
  .badge-contacted { background: var(--success-light); color: var(--success); }
  .badge-unreachable { background: var(--danger-light); color: var(--danger); }
  .card-body { padding: 0 16px 14px; display: grid; grid-template-columns: 1fr 1fr; gap: 6px 12px; }
  .card-field { font-size: 0.82rem; }
  .card-field span { color: var(--text-light); }
  .card-field b { color: var(--text); }
  .card-divider { border: none; border-top: 1px solid var(--border); margin: 0 16px; }
  .card-footer { padding: 8px 16px; font-size: 0.75rem; color: var(--text-light); display: flex; justify-content: space-between; }

  .empty { text-align: center; padding: 40px 20px; color: var(--text-light); }
  .empty .icon { font-size: 2.5rem; margin-bottom: 10px; }

  .chart-wrap { background: var(--card); border-radius: 12px; border: 1px solid var(--border); padding: 16px; margin-bottom: 20px; }
  .bar-chart { display: flex; align-items: flex-end; gap: 10px; height: 80px; }
  .bar-group { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 4px; }
  .bar { width: 100%; border-radius: 6px 6px 0 0; transition: height 0.3s; min-height: 4px; }
  .bar-label { font-size: 0.65rem; color: var(--text-light); text-align: center; }
  .bar-val { font-size: 0.7rem; font-weight: 700; }
</style>
</head>
<body>
<header>
  <h1>🏥 Shifokor Paneli</h1>
  <p id="doc-name">Yuklanmoqda...</p>
</header>

<div class="stats-grid">
  <div class="stat-card pending"><div class="number" id="cnt-pending">—</div><div class="label">⏳ Kutilmoqda</div></div>
  <div class="stat-card retrying"><div class="number" id="cnt-retrying">—</div><div class="label">🔄 Qayta eslatiladi</div></div>
  <div class="stat-card contacted"><div class="number" id="cnt-contacted">—</div><div class="label">✅ Bog'lanildi</div></div>
  <div class="stat-card unreachable"><div class="number" id="cnt-unreachable">—</div><div class="label">❌ Aloqaga chiqilmadi</div></div>
</div>

<div class="section">
  <h2>Bemorlar</h2>
  <input class="search-box" type="text" placeholder="🔍 Ism, telefon yoki kasallik bo'yicha qidirish..." id="search" oninput="render()">
  <div class="filter-bar">
    <button class="filter-btn active" data-f="all" onclick="setFilter('all',this)">Barchasi</button>
    <button class="filter-btn f-pending" data-f="pending" onclick="setFilter('pending',this)">⏳ Kutilmoqda</button>
    <button class="filter-btn f-retrying" data-f="retrying" onclick="setFilter('retrying',this)">🔄 Qayta</button>
    <button class="filter-btn f-contacted" data-f="contacted" onclick="setFilter('contacted',this)">✅ Bog'lanildi</button>
    <button class="filter-btn f-unreachable" data-f="unreachable" onclick="setFilter('unreachable',this)">❌ Aloqa yo'q</button>
  </div>
  <div id="list"></div>
</div>

<script>
let allPatients = [];
let activeFilter = 'all';

const STATUS_LABEL = {
  pending:     "⏳ Kutilmoqda",
  retrying:    "🔄 Qayta eslatiladi",
  contacted:   "✅ Bog'lanildi",
  unreachable: "❌ Aloqaga chiqilmadi",
};
const STATUS_BADGE = {
  pending:     "badge-pending",
  retrying:    "badge-retrying",
  contacted:   "badge-contacted",
  unreachable: "badge-unreachable",
};

async function load() {
  const res = await fetch(location.pathname + '/data');
  const d = await res.json();
  if (d.error) { document.getElementById('doc-name').textContent = d.error; return; }
  document.getElementById('doc-name').textContent = "Dr. " + d.doctor;
  allPatients = d.patients;
  updateStats();
  render();
}

function updateStats() {
  ['pending','retrying','contacted','unreachable'].forEach(s => {
    document.getElementById('cnt-' + s).textContent = allPatients.filter(p => p.status === s).length;
  });
}

function setFilter(f, btn) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  render();
}

function render() {
  const q = document.getElementById('search').value.toLowerCase();
  let list = allPatients;
  if (activeFilter !== 'all') list = list.filter(p => p.status === activeFilter);
  if (q) list = list.filter(p =>
    p.full_name.toLowerCase().includes(q) ||
    (p.phone||'').includes(q) ||
    (p.disease||'').toLowerCase().includes(q)
  );

  const el = document.getElementById('list');
  if (!list.length) {
    el.innerHTML = '<div class="empty"><div class="icon">🔍</div><div>Hech narsa topilmadi</div></div>';
    return;
  }

  el.innerHTML = list.map(p => {
    const st = p.status || 'pending';
    const badge = STATUS_BADGE[st] || 'badge-pending';
    const label = STATUS_LABEL[st] || st;
    const remind = p.next_remind_at ? p.next_remind_at.slice(0,16) : (p.notify_at ? p.notify_at.slice(0,16) : '—');
    const rc = p.reminder_count || 0;
    return `<div class="patient-card">
      <div class="card-header">
        <div class="card-name">👤 ${esc(p.full_name)}</div>
        <span class="badge ${badge}">${label}</span>
      </div>
      <div class="card-body">
        <div class="card-field"><span>📅 Yili: </span><b>${p.birth_year||'—'}</b></div>
        <div class="card-field"><span>📞 Tel: </span><b>${esc(p.phone||'—')}</b></div>
        <div class="card-field"><span>🏥 Kasallik: </span><b>${esc(p.disease||'—')}</b></div>
        <div class="card-field"><span>🏠 Manzil: </span><b>${esc(p.address||'—')}</b></div>
        ${p.notes ? `<div class="card-field" style="grid-column:1/-1"><span>📝 Izoh: </span><b>${esc(p.notes)}</b></div>` : ''}
      </div>
      <hr class="card-divider">
      <div class="card-footer">
        <span>🗓 Qo'shilgan: ${(p.created_at||'').slice(0,16)}</span>
        <span>${st==='retrying' ? '🔄 Keyingi: '+remind+' ('+rc+'/3)' : st==='pending' ? '⏰ Eslatma: '+remind : ''}</span>
      </div>
    </div>`;
  }).join('');
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

load();
</script>
</body>
</html>"""


ADMIN_PANEL_HTML = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Superadmin Panel</title>
<style>
  :root {
    --primary: #7c3aed;
    --primary-light: #f5f3ff;
    --success: #16a34a;
    --success-light: #f0fdf4;
    --warning: #d97706;
    --warning-light: #fffbeb;
    --danger: #dc2626;
    --danger-light: #fef2f2;
    --bg: #f8fafc;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #1e293b;
    --text-light: #64748b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
  header { background: linear-gradient(135deg, #4c1d95 0%, #7c3aed 100%); color: white; padding: 20px 24px; }
  header h1 { font-size: 1.4rem; font-weight: 700; }
  header p { font-size: 0.85rem; opacity: 0.8; margin-top: 4px; }
  .tabs { display: flex; background: white; border-bottom: 2px solid var(--border); padding: 0 20px; }
  .tab { padding: 14px 20px; cursor: pointer; font-weight: 600; font-size: 0.9rem; color: var(--text-light); border-bottom: 3px solid transparent; margin-bottom: -2px; transition: all 0.15s; }
  .tab.active { color: var(--primary); border-bottom-color: var(--primary); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; padding: 20px; }
  .stat-card { background: var(--card); border-radius: 12px; padding: 16px; border: 1px solid var(--border); text-align: center; }
  .stat-card .number { font-size: 2rem; font-weight: 800; }
  .stat-card .label { font-size: 0.72rem; color: var(--text-light); margin-top: 6px; }
  .stat-card.total .number { color: var(--primary); }
  .stat-card.pending .number { color: var(--warning); }
  .stat-card.contacted .number { color: var(--success); }
  .stat-card.unreachable .number { color: var(--danger); }
  .section { padding: 16px 20px; }
  .section h2 { font-size: 0.75rem; font-weight: 600; color: var(--text-light); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .search-box { width: 100%; padding: 10px 14px; border-radius: 10px; border: 1.5px solid var(--border); font-size: 0.9rem; margin-bottom: 14px; outline: none; }
  .search-box:focus { border-color: var(--primary); }
  .filter-bar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
  .filter-btn { padding: 6px 14px; border-radius: 20px; border: 1.5px solid var(--border); background: white; cursor: pointer; font-size: 0.82rem; font-weight: 500; }
  .filter-btn.active { background: var(--primary); color: white; border-color: var(--primary); }
  .patient-card { background: var(--card); border-radius: 12px; border: 1px solid var(--border); margin-bottom: 10px; overflow: hidden; }
  .patient-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.07); }
  .card-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px 8px; }
  .card-name { font-weight: 700; font-size: 0.95rem; }
  .doc-tag { font-size: 0.72rem; background: var(--primary-light); color: var(--primary); padding: 2px 8px; border-radius: 20px; font-weight: 600; }
  .badge { font-size: 0.72rem; font-weight: 600; padding: 3px 10px; border-radius: 20px; }
  .badge-pending { background: var(--warning-light); color: var(--warning); }
  .badge-retrying { background: var(--primary-light); color: var(--primary); }
  .badge-contacted { background: var(--success-light); color: var(--success); }
  .badge-unreachable { background: var(--danger-light); color: var(--danger); }
  .card-body { padding: 0 16px 12px; display: grid; grid-template-columns: 1fr 1fr; gap: 5px 12px; }
  .card-field { font-size: 0.81rem; }
  .card-field span { color: var(--text-light); }
  .card-field b { color: var(--text); }
  .card-divider { border: none; border-top: 1px solid var(--border); margin: 0 16px; }
  .card-footer { padding: 7px 16px; font-size: 0.73rem; color: var(--text-light); display: flex; justify-content: space-between; }
  .doctor-card { background: var(--card); border-radius: 12px; border: 1px solid var(--border); padding: 14px 16px; margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; }
  .doctor-info { display: flex; flex-direction: column; gap: 4px; }
  .doctor-name { font-weight: 700; font-size: 0.95rem; }
  .doctor-meta { font-size: 0.78rem; color: var(--text-light); }
  .doctor-badge { font-size: 0.72rem; font-weight: 600; padding: 4px 12px; border-radius: 20px; }
  .doc-approved { background: var(--success-light); color: var(--success); }
  .doc-pending { background: var(--warning-light); color: var(--warning); }
  .empty { text-align: center; padding: 40px 20px; color: var(--text-light); }
  .empty .icon { font-size: 2.5rem; margin-bottom: 10px; }
  .refresh-btn { position: fixed; bottom: 20px; right: 20px; background: var(--primary); color: white; border: none; border-radius: 50px; padding: 12px 20px; font-size: 0.85rem; font-weight: 600; cursor: pointer; box-shadow: 0 4px 12px rgba(124,58,237,0.4); }
</style>
</head>
<body>
<header>
  <h1>🛡️ Superadmin Panel</h1>
  <p id="header-stats">Yuklanmoqda...</p>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('patients', this)">👥 Bemorlar</div>
  <div class="tab" onclick="switchTab('doctors', this)">👨‍⚕️ Shifokorlar</div>
</div>

<div id="tab-patients" class="tab-content active">
  <div class="stats-grid">
    <div class="stat-card total"><div class="number" id="a-total">—</div><div class="label">Jami bemorlar</div></div>
    <div class="stat-card pending"><div class="number" id="a-pending">—</div><div class="label">⏳ Kutilmoqda</div></div>
    <div class="stat-card" style="--num-color:#7c3aed"><div class="number" id="a-retrying" style="color:#7c3aed">—</div><div class="label">🔄 Qayta eslatiladi</div></div>
    <div class="stat-card contacted"><div class="number" id="a-contacted">—</div><div class="label">✅ Bog'lanildi</div></div>
    <div class="stat-card unreachable"><div class="number" id="a-unreachable">—</div><div class="label">❌ Aloqa yo'q</div></div>
  </div>
  <div class="section">
    <h2>Barcha bemorlar</h2>
    <input class="search-box" type="text" placeholder="🔍 Ism, telefon, kasallik yoki shifokor..." id="p-search" oninput="renderPatients()">
    <div class="filter-bar">
      <button class="filter-btn active" data-f="all" onclick="setPFilter('all',this)">Barchasi</button>
      <button class="filter-btn" data-f="pending" onclick="setPFilter('pending',this)">⏳ Kutilmoqda</button>
      <button class="filter-btn" data-f="retrying" onclick="setPFilter('retrying',this)">🔄 Qayta</button>
      <button class="filter-btn" data-f="contacted" onclick="setPFilter('contacted',this)">✅ Bog'lanildi</button>
      <button class="filter-btn" data-f="unreachable" onclick="setPFilter('unreachable',this)">❌ Aloqa yo'q</button>
    </div>
    <div id="patients-list"></div>
  </div>
</div>

<div id="tab-doctors" class="tab-content">
  <div class="section" style="padding-top:20px">
    <h2>Barcha shifokorlar</h2>
    <input class="search-box" type="text" placeholder="🔍 Ism yoki username..." id="d-search" oninput="renderDoctors()">
    <div id="doctors-list"></div>
  </div>
</div>

<button class="refresh-btn" onclick="load()">🔄 Yangilash</button>

<script>
let allPatients = [];
let allDoctors = [];
let activePFilter = 'all';

const STATUS_LABEL = { pending:"⏳ Kutilmoqda", retrying:"🔄 Qayta eslatiladi", contacted:"✅ Bog'lanildi", unreachable:"❌ Aloqaga chiqilmadi" };
const STATUS_BADGE = { pending:"badge-pending", retrying:"badge-retrying", contacted:"badge-contacted", unreachable:"badge-unreachable" };

function switchTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}

async function load() {
  try {
    const res = await fetch(location.pathname + '/data');
    const d = await res.json();
    if (d.error) { document.getElementById('header-stats').textContent = d.error; return; }
    allPatients = d.patients;
    allDoctors = d.doctors;
    const approved = allDoctors.filter(x => x.approved).length;
    document.getElementById('header-stats').textContent = `${approved} faol shifokor | ${allPatients.length} bemor`;
    updateStats();
    renderPatients();
    renderDoctors();
  } catch(e) {
    document.getElementById('header-stats').textContent = 'Xatolik: ' + e.message;
  }
}

function updateStats() {
  document.getElementById('a-total').textContent = allPatients.length;
  ['pending','retrying','contacted','unreachable'].forEach(s => {
    document.getElementById('a-' + s).textContent = allPatients.filter(p => p.status === s).length;
  });
}

function setPFilter(f, btn) {
  activePFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderPatients();
}

function renderPatients() {
  const q = document.getElementById('p-search').value.toLowerCase();
  let list = allPatients;
  if (activePFilter !== 'all') list = list.filter(p => p.status === activePFilter);
  if (q) list = list.filter(p =>
    p.full_name.toLowerCase().includes(q) ||
    (p.phone||'').includes(q) ||
    (p.disease||'').toLowerCase().includes(q) ||
    (p.doc_name||'').toLowerCase().includes(q)
  );
  const el = document.getElementById('patients-list');
  if (!list.length) { el.innerHTML = '<div class="empty"><div class="icon">🔍</div><div>Hech narsa topilmadi</div></div>'; return; }
  el.innerHTML = list.map(p => {
    const st = p.status || 'pending';
    const badge = STATUS_BADGE[st] || 'badge-pending';
    const label = STATUS_LABEL[st] || st;
    const remind = p.next_remind_at ? p.next_remind_at.slice(0,16) : (p.notify_at ? p.notify_at.slice(0,16) : '—');
    return `<div class="patient-card">
      <div class="card-header">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="card-name">👤 ${esc(p.full_name)}</div>
          <span class="doc-tag">Dr. ${esc(p.doc_name||'?')}</span>
        </div>
        <span class="badge ${badge}">${label}</span>
      </div>
      <div class="card-body">
        <div class="card-field"><span>📅 Yili: </span><b>${p.birth_year||'—'}</b></div>
        <div class="card-field"><span>📞 Tel: </span><b>${esc(p.phone||'—')}</b></div>
        <div class="card-field"><span>🏥 Kasallik: </span><b>${esc(p.disease||'—')}</b></div>
        <div class="card-field"><span>🏠 Manzil: </span><b>${esc(p.address||'—')}</b></div>
        ${p.notes ? `<div class="card-field" style="grid-column:1/-1"><span>📝 Izoh: </span><b>${esc(p.notes)}</b></div>` : ''}
      </div>
      <hr class="card-divider">
      <div class="card-footer">
        <span>🗓 ${(p.created_at||'').slice(0,16)}</span>
        <span>${st==='retrying'||st==='pending' ? '⏰ '+remind : ''}</span>
      </div>
    </div>`;
  }).join('');
}

function renderDoctors() {
  const q = document.getElementById('d-search').value.toLowerCase();
  let list = allDoctors;
  if (q) list = list.filter(d => d.name.toLowerCase().includes(q) || (d.username||'').toLowerCase().includes(q));
  const el = document.getElementById('doctors-list');
  if (!list.length) { el.innerHTML = '<div class="empty"><div class="icon">👨‍⚕️</div><div>Shifokorlar topilmadi</div></div>'; return; }
  el.innerHTML = list.map(d => {
    const cnt = allPatients.filter(p => p.doctor_id === d.id).length;
    return `<div class="doctor-card">
      <div class="doctor-info">
        <div class="doctor-name">👨‍⚕️ ${esc(d.name)}</div>
        <div class="doctor-meta">@${esc(d.username||'—')} · ID: ${d.telegram_id} · ${cnt} bemor</div>
        <div class="doctor-meta">Qo'shilgan: ${(d.created_at||'').slice(0,16)}</div>
      </div>
      <span class="doctor-badge ${d.approved ? 'doc-approved' : 'doc-pending'}">${d.approved ? '✅ Tasdiqlangan' : '⏳ Kutmoqda'}</span>
    </div>`;
  }).join('');
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

load();
</script>
</body>
</html>"""


@app.route("/admin/panel/<token>", methods=["GET"])
def admin_panel(token):
    if not ADMIN_ID:
        return "ADMIN_ID sozlanmagan", 403
    expected = make_panel_token(ADMIN_ID)
    if token != expected:
        return "Ruxsat yo'q", 403
    return ADMIN_PANEL_HTML


@app.route("/admin/panel/<token>/data", methods=["GET"])
def admin_panel_data(token):
    if not ADMIN_ID:
        return jsonify({"error": "ADMIN_ID sozlanmagan"}), 403
    expected = make_panel_token(ADMIN_ID)
    if token != expected:
        return jsonify({"error": "Ruxsat yo'q"}), 403
    conn = get_db()
    doctors = conn.execute("SELECT * FROM doctors ORDER BY created_at DESC").fetchall()
    patients = conn.execute(
        "SELECT p.*, d.name as doc_name FROM patients p "
        "JOIN doctors d ON p.doctor_id=d.id ORDER BY p.created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify({
        "doctors": [dict(d) for d in doctors],
        "patients": [dict(p) for p in patients],
    })


@app.route("/panel/<int:telegram_id>/<token>", methods=["GET"])
def panel(telegram_id, token):
    expected = make_panel_token(telegram_id)
    if token != expected:
        return "Ruxsat yo'q", 403
    return PANEL_HTML


@app.route("/panel/<int:telegram_id>/<token>/data", methods=["GET"])
def panel_data(telegram_id, token):
    expected = make_panel_token(telegram_id)
    if token != expected:
        return jsonify({"error": "Ruxsat yo'q"}), 403
    conn = get_db()
    doc = conn.execute("SELECT * FROM doctors WHERE telegram_id=?", (telegram_id,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"error": "Shifokor topilmadi"})
    patients = conn.execute(
        "SELECT * FROM patients WHERE doctor_id=? ORDER BY created_at DESC",
        (doc["id"],)
    ).fetchall()
    conn.close()
    return jsonify({
        "doctor": doc["name"],
        "patients": [dict(p) for p in patients]
    })


# ─────────────────────────── MAIN ───────────────────────────

if __name__ == "__main__":
    bootstrap()

    PORT = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask on port {PORT}")
    logger.info(f"Mode: {'TEST (1 soat)' if TEST_MODE else 'PROD (80 kun)'}")
    app.run(host="0.0.0.0", port=PORT)
