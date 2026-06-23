import os
import re
import time
from datetime import datetime, timezone
import pytz

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI

# ======================
# ENV VARS
# ======================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not ADMIN_CHAT_ID_RAW:
    raise RuntimeError("Missing ADMIN_CHAT_ID")

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)

# ======================
# INIT
# ======================
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
client = OpenAI(api_key=OPENAI_API_KEY)

# ======================
# CONFIG
# ======================
MAX_REQUESTS_24H = 3
SPAM_STREAK_LIMIT = 3
SPAM_WINDOW_MINUTES = 10
BLOCK_HOURS = 24

REQUEST_START_HOUR = 10
REQUEST_END_HOUR = 21
TIMEZONE_NAME = "Europe/Rome"

CLOSED_MESSAGE = (
    "⏰ Le richieste sono attive dalle 10:00 alle 21:00.\n"
    "Riprova più tardi 🙏"
)

states = {}
tickets = {}
user_limits = {}
daily_counts = {}
user_history = {}

# ======================
# HELPERS
# ======================
def now_utc():
    return datetime.now(timezone.utc)

def ts():
    return time.time()

def clean_text(s):
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:700]

def user_display_from_tg(user):
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else ""
    return (name + (" " + username if username else "")).strip() or "Utente"

def is_request_time_allowed():
    tz = pytz.timezone(TIMEZONE_NAME)
    now_local = datetime.now(tz)
    start = now_local.replace(hour=REQUEST_START_HOUR, minute=0, second=0, microsecond=0)
    end = now_local.replace(hour=REQUEST_END_HOUR, minute=0, second=0, microsecond=0)
    return start <= now_local < end

def get_user_limit_state(user_id):
    if user_id not in user_limits:
        user_limits[user_id] = {
            "req_times": [],
            "streak": 0,
            "last_req_ts": None,
            "blocked_until": None
        }
    return user_limits[user_id]

def prune_24h(times_list):
    cutoff = ts() - 24 * 3600
    return [t for t in times_list if t >= cutoff]

def can_submit_request(user_id):
    st = get_user_limit_state(user_id)

    if st["blocked_until"] and ts() < st["blocked_until"]:
        remaining = int((st["blocked_until"] - ts()) // 60)
        return False, f"⛔ Sei temporaneamente bloccato. Riprova tra circa {remaining} minuti."

    st["req_times"] = prune_24h(st["req_times"])

    if len(st["req_times"]) >= MAX_REQUESTS_24H:
        return False, "⛔ Hai raggiunto il limite massimo di 3 richieste ogni 24 ore."

    return True, None

def register_request_submission(user_id):
    st = get_user_limit_state(user_id)
    st["req_times"] = prune_24h(st["req_times"])
    st["req_times"].append(ts())

    last = st["last_req_ts"]
    if last and ts() - last <= SPAM_WINDOW_MINUTES * 60:
        st["streak"] += 1
    else:
        st["streak"] = 1

    st["last_req_ts"] = ts()

    if st["streak"] >= SPAM_STREAK_LIMIT:
        st["blocked_until"] = ts() + BLOCK_HOURS * 3600

def inc_daily_counter():
    key = now_utc().strftime("%Y-%m-%d")
    daily_counts[key] = daily_counts.get(key, 0) + 1

def add_history(user_id, ticket):
    arr = user_history.get(user_id, [])
    arr.append(ticket)
    user_history[user_id] = arr[-20:]

def init_state(user_id):
    states[user_id] = {
        "step": 1,
        "data": {
            "title": "",
            "type": "",
            "year": "",
            "season_episode": "",
            "language": "",
            "notes": "",
        },
    }

def format_summary(data):
    return (
        "📌 Riepilogo richiesta\n"
        f"Titolo: {data['title']}\n"
        f"Tipo: {data['type']}\n"
        f"Anno: {data['year']}\n"
        f"Stagione/Episodio: {data['season_episode']}\n"
        f"Lingua: {data['language']}\n"
        f"Note: {data['notes']}\n"
    )

SYSTEM_PROMPT = """
Sei un assistente helpdesk.
Il tuo compito è raccogliere e formattare richieste per lo staff.

Regole:
- Non fornire link o accessi.
- Non fare promozioni o prezzi.
- Tono educato, neutro e chiaro.
- Output in italiano, ordinato e breve.
"""

INTRO = (
    "Ciao! 👋 Posso registrare una richiesta e inoltrarla allo staff.\n"
    "Scrivi /request per iniziare."
)

# ======================
# KEYBOARDS
# ======================
def kb_cancel():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_type():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🎬 Film", callback_data="type:film"),
        InlineKeyboardButton("📺 Serie", callback_data="type:serie"),
    )
    kb.row(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_year():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("2026", callback_data="year:2026"),
        InlineKeyboardButton("2025", callback_data="year:2025"),
        InlineKeyboardButton("2024", callback_data="year:2024"),
    )
    kb.row(
        InlineKeyboardButton("2023", callback_data="year:2023"),
        InlineKeyboardButton("2022", callback_data="year:2022"),
        InlineKeyboardButton("2021", callback_data="year:2021"),
    )
    kb.row(
        InlineKeyboardButton("Non so", callback_data="year:unknown"),
        InlineKeyboardButton("Scrivo io", callback_data="year:manual"),
    )
    kb.row(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_series_mode():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✅ Completa", callback_data="series:complete"),
        InlineKeyboardButton("🎯 Specifico S/E", callback_data="series:specific"),
    )
    kb.row(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_lang():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🇮🇹 ITA", callback_data="lang:ITA"),
        InlineKeyboardButton("🇬🇧 ENG", callback_data="lang:ENG"),
    )
    kb.row(
        InlineKeyboardButton("🇮🇹+🇬🇧 ITA+ENG", callback_data="lang:ITA+ENG"),
        InlineKeyboardButton("Altro", callback_data="lang:ALTRO"),
    )
    kb.row(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_confirm():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✅ Conferma invio", callback_data="confirm:send"),
        InlineKeyboardButton("✏️ Modifica note", callback_data="confirm:editnotes"),
    )
    kb.row(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_staff_initial():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("👤 Assegnata a me", callback_data="staff:assign"))
    kb.row(
        InlineKeyboardButton("🟡 Presa in carico", callback_data="staff:in_progress"),
        InlineKeyboardButton("🟢 Completata", callback_data="staff:done"),
    )
    kb.row(
        InlineKeyboardButton("🔴 Non Disponibile", callback_data="staff:na"),
        InlineKeyboardButton("🟠 Già presente", callback_data="staff:already"),
    )
    return kb

def kb_staff_after_assign():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🟢 Completata", callback_data="staff:done"),
        InlineKeyboardButton("🔴 Non Disponibile", callback_data="staff:na"),
    )
    kb.row(InlineKeyboardButton("🟠 Già presente", callback_data="staff:already"))
    return kb

# ======================
# COMMANDS
# ======================
@bot.message_handler(commands=["start", "help"])
def start(m):
    bot.send_message(m.chat.id, INTRO)

@bot.message_handler(commands=["request"])
def request(m):
    if not is_request_time_allowed():
        bot.send_message(m.chat.id, CLOSED_MESSAGE)
        return

    ok, reason = can_submit_request(m.from_user.id)
    if not ok:
        bot.send_message(m.chat.id, reason)
        return

    init_state(m.from_user.id)
    bot.send_message(m.chat.id, "Ok! Dimmi il titolo.", reply_markup=kb_cancel())

@bot.message_handler(commands=["cancel"])
def cancel_cmd(m):
    states.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "Richiesta annullata. Se vuoi riprovare: /request")

# ======================
# CALLBACKS
# ======================
@bot.callback_query_handler(func=lambda c: True)
def callback_router(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    cb = call.data or ""

    if cb == "cancel":
        states.pop(user_id, None)
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        bot.send_message(chat_id, "Richiesta annullata.")
        return

    if cb.startswith("staff:"):
        if chat_id != ADMIN_CHAT_ID:
            return

        action = cb.split(":", 1)[1]
        staff_name = user_display_from_tg(call.from_user)
        ticket = tickets.get(msg_id, {})

        def append_line(text):
            return (call.message.text or "") + f"\n\n{text}"

        if action == "assign":
            ticket["assignee"] = staff_name
            tickets[msg_id] = ticket
            bot.edit_message_text(
                append_line(f"👤 Assegnata a: {staff_name}"),
                ADMIN_CHAT_ID,
                msg_id,
                reply_markup=kb_staff_after_assign(),
            )
            return

        if action == "in_progress":
            ticket["status"] = "🟡 Presa in carico"
            ticket["assignee"] = ticket.get("assignee") or staff_name
            tickets[msg_id] = ticket

            if ticket.get("user_chat_id"):
                bot.send_message(ticket["user_chat_id"], "🟡 La tua richiesta è stata presa in carico.")

            bot.edit_message_text(
                append_line(f"📌 Stato: 🟡 Presa in carico\n👤 Assegnata a: {ticket['assignee']}"),
                ADMIN_CHAT_ID,
                msg_id,
                reply_markup=kb_staff_after_assign(),
            )
            return

        status_map = {
            "done": "🟢 Completata",
            "na": "🔴 Non Disponibile",
            "already": "🟠 Già presente",
        }

        if action in status_map:
            status = status_map[action]

            if ticket.get("user_chat_id"):
                if action == "done":
                    bot.send_message(ticket["user_chat_id"], "🟢 La tua richiesta è stata completata. Grazie!")
                elif action == "na":
                    bot.send_message(ticket["user_chat_id"], "🔴 La tua richiesta al momento non è disponibile.")
                elif action == "already":
                    bot.send_message(ticket["user_chat_id"], "🟠 Questa richiesta risulta già presente. Controlla bene.")

            assignee = ticket.get("assignee") or staff_name

            bot.edit_message_text(
                append_line(f"📌 Stato: {status} (da {staff_name})\n👤 Assegnata a: {assignee}"),
                ADMIN_CHAT_ID,
                msg_id,
                reply_markup=None,
            )
            return

    if user_id not in states:
        bot.send_message(chat_id, "Per iniziare una richiesta: /request")
        return

    st = states[user_id]
    step = st["step"]
    data = st["data"]

    if cb.startswith("type:"):
        data["type"] = "Film" if cb.endswith("film") else "Serie"
        st["step"] = 3
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        bot.send_message(chat_id, "Seleziona l’anno.", reply_markup=kb_year())
        return

    if cb.startswith("year:"):
        chosen = cb.split(":", 1)[1]
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)

        if chosen == "manual":
            st["step"] = 31
            bot.send_message(chat_id, "Scrivi l’anno.", reply_markup=kb_cancel())
            return

        data["year"] = "Non so" if chosen == "unknown" else chosen

        if data["type"] == "Serie":
            st["step"] = 4
            bot.send_message(chat_id, "Completa o specifico episodio?", reply_markup=kb_series_mode())
        else:
            data["season_episode"] = "-"
            st["step"] = 5
            bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        return

    if cb.startswith("series:"):
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        if cb.endswith("complete"):
            data["season_episode"] = "Completa"
            st["step"] = 5
            bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        else:
            st["step"] = 41
            bot.send_message(chat_id, "Scrivi stagione/episodio.", reply_markup=kb_cancel())
        return

    if cb.startswith("lang:"):
        chosen = cb.split(":", 1)[1]
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)

        if chosen == "ALTRO":
            st["step"] = 51
            bot.send_message(chat_id, "Scrivi la lingua richiesta.", reply_markup=kb_cancel())
            return

        data["language"] = chosen
        st["step"] = 6
        bot.send_message(chat_id, "Note extra? Se nulla scrivi “-”.", reply_markup=kb_cancel())
        return

    if cb.startswith("confirm:"):
        action = cb.split(":", 1)[1]
        bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)

        if action == "editnotes":
            st["step"] = 6
            bot.send_message(chat_id, "Riscrivi le note.", reply_markup=kb_cancel())
            return

        if not is_request_time_allowed():
            bot.send_message(chat_id, CLOSED_MESSAGE)
            states.pop(user_id, None)
            return

        ok, reason = can_submit_request(user_id)
        if not ok:
            bot.send_message(chat_id, reason)
            states.pop(user_id, None)
            return

        u = call.from_user

        payload = (
            f"Utente: {user_display_from_tg(u)} | id:{u.id}\n"
            f"Titolo: {data['title']}\n"
            f"Tipo: {data['type']}\n"
            f"Anno: {data['year']}\n"
            f"Stagione/Episodio: {data['season_episode']}\n"
            f"Lingua: {data['language']}\n"
            f"Note: {data['notes']}\n"
        )

        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": payload},
                ],
            )
            formatted = resp.choices[0].message.content.strip()
        except Exception:
            formatted = "📌 NUOVA RICHIESTA\n" + payload

        msg_admin = bot.send_message(ADMIN_CHAT_ID, formatted, reply_markup=kb_staff_initial())

        tickets[msg_admin.message_id] = {
            "user_id": user_id,
            "user_chat_id": chat_id,
            "assignee": None,
            "status": "Nuova",
        }

        add_history(user_id, {
            "admin_msg_id": msg_admin.message_id,
            "created_at": now_utc().isoformat(),
            "title": data["title"],
            "status": "Nuova",
        })

        register_request_submission(user_id)
        inc_daily_counter()

        today_key = now_utc().strftime("%Y-%m-%d")
        bot.send_message(chat_id, f"✅ Inviato allo staff. (Richieste oggi: {daily_counts.get(today_key, 0)}) Grazie!")
        states.pop(user_id, None)
        return

# ======================
# TEXT HANDLER
# ======================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(m):
    user_id = m.from_user.id
    chat_id = m.chat.id
    text = clean_text(m.text or "")

    if user_id not in states:
        return

    st = states[user_id]
    step = st["step"]
    data = st["data"]

    if step == 1:
        data["title"] = text
        st["step"] = 2
        bot.send_message(chat_id, "Perfetto. È un film o una serie?", reply_markup=kb_type())
        return

    if step == 31:
        data["year"] = text
        if data["type"] == "Serie":
            st["step"] = 4
            bot.send_message(chat_id, "Completa o specifico episodio?", reply_markup=kb_series_mode())
        else:
            data["season_episode"] = "-"
            st["step"] = 5
            bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        return

    if step == 41:
        data["season_episode"] = text
        st["step"] = 5
        bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        return

    if step == 51:
        data["language"] = text
        st["step"] = 6
        bot.send_message(chat_id, "Note extra? Se nulla scrivi “-”.", reply_markup=kb_cancel())
        return

    if step == 6:
        data["notes"] = text
        st["step"] = 7
        bot.send_message(chat_id, format_summary(data) + "\nConfermi l’invio allo staff?", reply_markup=kb_confirm())
        return

# ======================
# RUN
# ======================
while True:
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    except Exception:
        time.sleep(3)
