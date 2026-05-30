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
NOTIFY_SECONDS = 3600 if TEST_MODE else 80 * 24 * 3600  # 1 hour test OR 80 days

app = Flask(__name__)

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
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        );
    """)
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
        ]
    }

# ─────────────────────────── NOTIFY WORKER ───────────────────────────

def notify_worker():
    logger.info(f"Notify worker started. Mode: {'TEST (1 soat)' if TEST_MODE else '80 kun'}")
    while True:
        try:
            conn = get_db()
            now = datetime.now()
            patients = conn.execute(
                "SELECT p.*, d.telegram_id as doc_tg FROM patients p "
                "JOIN doctors d ON p.doctor_id = d.id "
                "WHERE p.notified=0 AND p.notify_at <= ?",
                (now.strftime("%Y-%m-%d %H:%M:%S"),)
            ).fetchall()
            for p in patients:
                msg = (
                    f"⏰ <b>Eslatma!</b>\n\n"
                    f"Bemor: <b>{p['full_name']}</b>\n"
                    f"Tug'ilgan yili: {p['birth_year']}\n"
                    f"Telefon: {p['phone']}\n"
                    f"Kasallik: {p['disease']}\n"
                    f"Manzil: {p['address']}\n"
                    f"Izoh: {p['notes'] or '-'}\n\n"
                    f"📅 Qo'shilgan: {p['created_at']}\n"
                    f"{('⚠️ TEST: 1 soat otdi!' if TEST_MODE else '⚠️ 80 kun otdi!')}"
                )
                send_message(p["doc_tg"], msg)
                conn.execute("UPDATE patients SET notified=1 WHERE id=?", (p["id"],))
                conn.commit()
                logger.info(f"Notified doctor {p['doc_tg']} about patient {p['full_name']}")
            conn.close()
        except Exception as e:
            logger.error(f"Notify worker error: {e}")
        time.sleep(60)  # check every minute


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
        status = "✅ Xabar berildi" if p["notified"] else f"⏳ {p['notify_at']} ga xabar"
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
    notified = conn.execute("SELECT COUNT(*) FROM patients WHERE notified=1").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM patients WHERE notified=0").fetchone()[0]
    conn.close()
    send_message(chat_id,
        f"📈 <b>Statistika:</b>\n\n"
        f"👨‍⚕️ Faol shifokorlar: {total_docs}\n"
        f"🧑‍🤝‍🧑 Jami bemorlar: {total_patients}\n"
        f"✅ Xabar berilganlar: {notified}\n"
        f"⏳ Kutayotganlar: {pending}\n"
        f"\n⚙️ Rejim: {'TEST (1 soat)' if TEST_MODE else '80 kun'}"
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
            status = "✅ Xabar berildi" if p["notified"] else f"⏳ Xabar vaqti: {p['notify_at']}"
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


# ─────────────────────────── MAIN ───────────────────────────

if __name__ == "__main__":
    bootstrap()

    PORT = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Flask on port {PORT}")
    logger.info(f"Mode: {'TEST (1 soat)' if TEST_MODE else 'PROD (80 kun)'}")
    app.run(host="0.0.0.0", port=PORT)
