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
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()          # staff channel/group id: -100...
MEMBER_GROUP_ID_RAW = os.getenv("MEMBER_GROUP_ID", "").strip()      # main group id allowed: -100...

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not ADMIN_CHAT_ID_RAW:
    raise RuntimeError("Missing ADMIN_CHAT_ID (e.g. -100...)")
if not MEMBER_GROUP_ID_RAW:
    raise RuntimeError("Missing MEMBER_GROUP_ID (e.g. -100...)")

try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
except ValueError as e:
    raise RuntimeError("ADMIN_CHAT_ID must be an integer like -100...") from e

try:
    MEMBER_GROUP_ID = int(MEMBER_GROUP_ID_RAW)
except ValueError as e:
    raise RuntimeError("MEMBER_GROUP_ID must be an integer like -100...") from e

# ======================
# INIT
# ======================
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
client = OpenAI(api_key=OPENAI_API_KEY)

# ======================
# CONFIG (limits)
# ======================
MAX_REQUESTS_24H = 3

SPAM_STREAK_LIMIT = 3
SPAM_WINDOW_MINUTES = 10
BLOCK_HOURS = 24

# ======================
# ORARI RICHIESTE
# ======================
REQUEST_START_HOUR = 10  # 10:00
REQUEST_END_HOUR = 21    # 21:00 (stop alle 21:00)
TIMEZONE_NAME = "Europe/Rome"

CLOSED_MESSAGE = (
    "⏰ Le richieste sono attive dalle **10:00 alle 21:00**.\n"
    "Riprova più tardi 🙏"
)

# ======================
# STATE (in-memory)
# ======================
states = {}        # user_id -> {"step": int, "data": dict}
tickets = {}       # admin_message_id -> ticket info dict
user_limits = {}   # user_id -> {"req_times":[ts], "streak":int, "last_req_ts":ts|None, "blocked_until":ts|None}
daily_counts = {}  # "YYYY-MM-DD" -> int
user_history = {}  # user_id -> [ticket_summary_dict] (last 20)

# ======================
# HELPERS
# ======================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts() -> float:
    return time.time()

def clean_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:700]

def has_sensitive_request(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ["link", "m3u", "playlist", "username", "password", "accesso", "attivazione"])

def user_display_from_tg(user) -> str:
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else ""
    return (name + (" " + username if username else "")).strip() or "Utente"

def is_allowed_user(user_id: int) -> bool:
    """
    E1: only users who are members of MEMBER_GROUP_ID can use the bot.
    Bot must be inside that group and preferably admin to reliably call get_chat_member.
    """
    try:
        cm = bot.get_chat_member(MEMBER_GROUP_ID, user_id)
        return cm.status in ("creator", "administrator", "member")
    except Exception:
        return False

def is_request_time_allowed() -> bool:
    tz = pytz.timezone(TIMEZONE_NAME)
    now_local = datetime.now(tz)
    start = now_local.replace(hour=REQUEST_START_HOUR, minute=0, second=0, microsecond=0)
    end = now_local.replace(hour=REQUEST_END_HOUR, minute=0, second=0, microsecond=0)
    return start <= now_local < end

def get_user_limit_state(user_id: int):
    if user_id not in user_limits:
        user_limits[user_id] = {"req_times": [], "streak": 0, "last_req_ts": None, "blocked_until": None}
    return user_limits[user_id]

def prune_24h(times_list):
    cutoff = ts() - 24 * 3600
    return [t for t in times_list if t >= cutoff]

def can_submit_request(user_id: int):
    st = get_user_limit_state(user_id)

    bu = st.get("blocked_until")
    if bu and ts() < bu:
        remaining_min = int((bu - ts()) // 60)
        return False, f"⛔ Sei temporaneamente bloccato per spam. Riprova tra circa {remaining_min} minuti."

    st["req_times"] = prune_24h(st["req_times"])
    if len(st["req_times"]) >= MAX_REQUESTS_24H:
        return False, f"⛔ Hai raggiunto il limite: massimo {MAX_REQUESTS_24H} richieste ogni 24 ore."

    return True, None

def register_request_submission(user_id: int):
    st = get_user_limit_state(user_id)

    st["req_times"] = prune_24h(st["req_times"])
    st["req_times"].append(ts())

    last = st.get("last_req_ts")
    if last and (ts() - last) <= SPAM_WINDOW_MINUTES * 60:
        st["streak"] = st.get("streak", 0) + 1
    else:
        st["streak"] = 1

    st["last_req_ts"] = ts()

    if st["streak"] >= SPAM_STREAK_LIMIT:
        st["blocked_until"] = ts() + BLOCK_HOURS * 3600

def inc_daily_counter():
    key = now_utc().strftime("%Y-%m-%d")
    daily_counts[key] = daily_counts.get(key, 0) + 1

def add_history(user_id: int, ticket_summary: dict):
    arr = user_history.get(user_id, [])
    arr.append(ticket_summary)
    user_history[user_id] = arr[-20:]

def init_state(user_id: int):
    states[user_id] = {
        # steps:
        # 1=title(text)
        # 2=type(button)
        # 3=year(button)
        # 31=year_manual(text)
        # 4=series_mode(button) (only if serie)
        # 41=season_episode(text) (only if serie specific)
        # 5=lang(button)
        # 51=lang_manual(text) (if Altro)
        # 6=notes(text)
        # 7=confirm(button)
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

def format_summary(data: dict) -> str:
    return (
        "📌 Riepilogo richiesta\n"
        f"Titolo: {data.get('title','')}\n"
        f"Tipo: {data.get('type','')}\n"
        f"Anno: {data.get('year','')}\n"
        f"Stagione/Episodio: {data.get('season_episode','')}\n"
        f"Lingua: {data.get('language','')}\n"
        f"Note: {data.get('notes','')}\n"
    )

SYSTEM_PROMPT = """
Sei un assistente helpdesk per richieste contenuti.
Il tuo compito è SOLO raccogliere e formattare richieste per lo staff.

Regole:
- Non fornire link, accessi o credenziali.
- Non fare promozioni o prezzi.
- Se l’utente chiede link/accesso, rispondi che puoi solo registrare la richiesta e inoltrarla allo staff.
- Tono: educato, neutro, chiaro.

Output: crea una scheda richiesta in italiano, ordinata e breve.
"""

INTRO = (
    "Ciao! 👋 Posso registrare una richiesta e inoltrarla allo staff.\n"
    "Scrivi /request per iniziare.\n"
    "Nota: non posso fornire link o accessi, solo raccogliere la richiesta."
)

# ======================
# KEYBOARDS
# ======================
def kb_cancel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_type() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🎬 Film", callback_data="type:film"),
        InlineKeyboardButton("📺 Serie", callback_data="type:serie"),
    )
    kb.row(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_year() -> InlineKeyboardMarkup:
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

def kb_series_mode() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✅ Completa", callback_data="series:complete"),
        InlineKeyboardButton("🎯 Specifico S/E", callback_data="series:specific"),
    )
    kb.row(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_lang() -> InlineKeyboardMarkup:
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

def kb_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✅ Conferma invio", callback_data="confirm:send"),
        InlineKeyboardButton("✏️ Modifica note", callback_data="confirm:editnotes"),
    )
    kb.row(InlineKeyboardButton("❌ Annulla", callback_data="cancel"))
    return kb

def kb_staff_initial() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("👤 Assegnata a me", callback_data="staff:assign"))
    kb.row(
        InlineKeyboardButton("🟡 Presa in carico", callback_data="staff:in_progress"),
        InlineKeyboardButton("🟢 Completata", callback_data="staff:done"),
    )
    kb.row(
        InlineKeyboardButton("🔴 Non Disponibile", callback_data="staff:na"),
        InlineKeyboardButton("🟠 Già presente (controlla bene)", callback_data="staff:already"),
    )
    return kb

def kb_staff_after_assign_or_progress() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🟢 Completata", callback_data="staff:done"),
        InlineKeyboardButton("🔴 Non Disponibile", callback_data="staff:na"),
    )
    kb.row(InlineKeyboardButton("🟠 Già presente (controlla bene)", callback_data="staff:already"))
    return kb

# ======================
# COMMANDS
# ======================
@bot.message_handler(commands=["start", "help"])
def start(m):
    bot.send_message(m.chat.id, INTRO)

@bot.message_handler(commands=["request"])
def request(m):
    # Orari
    if not is_request_time_allowed():
        bot.send_message(m.chat.id, CLOSED_MESSAGE)
        return

    # E1
    if not is_allowed_user(m.from_user.id):
        bot.send_message(m.chat.id, "⛔ Questo bot è riservato agli utenti del gruppo. Se pensi sia un errore, contatta un admin.")
        return

    # A1/A2
    ok, reason = can_submit_request(m.from_user.id)
    if not ok:
        bot.send_message(m.chat.id, reason)
        return

    init_state(m.from_user.id)
    bot.send_message(m.chat.id, "Ok! Dimmi il *titolo* (film o serie).", reply_markup=kb_cancel())

@bot.message_handler(commands=["cancel"])
def cancel_cmd(m):
    states.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "Richiesta annullata. Se vuoi riprovare: /request")

# ======================
# CALLBACK ROUTER
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

    # CANCEL anywhere
    if cb == "cancel":
        states.pop(user_id, None)
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        bot.send_message(chat_id, "Richiesta annullata. Se vuoi riprovare: /request")
        return

    # ======================
    # STAFF actions (only in ADMIN_CHAT_ID)
    # ======================
    if cb.startswith("staff:"):
        if chat_id != ADMIN_CHAT_ID:
            return

        action = cb.split(":", 1)[1]
        staff_name = user_display_from_tg(call.from_user)
        t = tickets.get(msg_id, {})

        def append_line(text: str) -> str:
            return (call.message.text or "") + f"\n\n{text}"

        # ASSIGN (non-terminal)
        if action == "assign":
            t["assignee"] = staff_name
            tickets[msg_id] = t
            try:
                bot.edit_message_text(
                    append_line(f"👤 Assegnata a: {staff_name}"),
                    ADMIN_CHAT_ID,
                    msg_id,
                    reply_markup=kb_staff_after_assign_or_progress(),
                )
            except Exception:
                try:
                    bot.edit_message_reply_markup(ADMIN_CHAT_ID, msg_id, reply_markup=kb_staff_after_assign_or_progress())
                except Exception:
                    pass
            return

        # IN PROGRESS (non-terminal)
        if action == "in_progress":
            t["status"] = "🟡 Presa in carico"
            t["assignee"] = t.get("assignee") or staff_name
            tickets[msg_id] = t

            if t.get("user_chat_id"):
                try:
                    bot.send_message(int(t["user_chat_id"]), "🟡 La tua richiesta è stata presa in carico dallo staff.")
                except Exception:
                    pass

            try:
                bot.edit_message_text(
                    append_line(f"📌 Stato: 🟡 Presa in carico (da {t.get('assignee')})"),
                    ADMIN_CHAT_ID,
                    msg_id,
                    reply_markup=kb_staff_after_assign_or_progress(),
                )
            except Exception:
                try:
                    bot.edit_message_reply_markup(ADMIN_CHAT_ID, msg_id, reply_markup=kb_staff_after_assign_or_progress())
                except Exception:
                    pass
            return

        # Terminal actions: DONE / NA / ALREADY -> disable buttons
        status_map = {
            "done": "🟢 Completata",
            "na": "🔴 Non Disponibile",
            "already": "🟠 Già presente (controlla bene)",
        }
        if action in status_map:
            status_text = status_map[action]
            t["status"] = status_text
            t["closed_by"] = staff_name
            tickets[msg_id] = t

            user_chat_id = t.get("user_chat_id")
            if user_chat_id:
                try:
                    if action == "done":
                        bot.send_message(int(user_chat_id), "🟢 La tua richiesta è stata completata. Grazie!")
                    elif action == "na":
                        bot.send_message(int(user_chat_id), "🔴 La tua richiesta al momento non è disponibile.")
                    elif action == "already":
                        bot.send_message(int(user_chat_id), "🟠 Questa richiesta risulta già presente. Controlla bene e, se serve, specifica meglio titolo/anno.")
                except Exception:
                    pass

            assignee = t.get("assignee") or staff_name
            try:
                bot.edit_message_text(
                    append_line(f"📌 Stato: {status_text} (da {staff_name})\n👤 Assegnata a: {assignee}"),
                    ADMIN_CHAT_ID,
                    msg_id,
                    reply_markup=None,  # disable after first final click
                )
            except Exception:
                try:
                    bot.edit_message_reply_markup(ADMIN_CHAT_ID, msg_id, reply_markup=None)
                except Exception:
                    pass

            uid = t.get("user_id")
            if uid in user_history:
                for item in reversed(user_history[uid]):
                    if item.get("admin_msg_id") == msg_id:
                        item["status"] = status_text
                        item["updated_at"] = now_utc().isoformat()
                        break
            return

        return

    # ======================
    # USER flow buttons
    # ======================
    if user_id not in states:
        bot.send_message(chat_id, "Per iniziare una richiesta: /request")
        return

    st = states[user_id]
    step = st["step"]
    req = st["data"]

    # TYPE
    if cb.startswith("type:"):
        if step != 2:
            return
        chosen = cb.split(":", 1)[1]
        req["type"] = "Film" if chosen == "film" else "Serie"
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        st["step"] = 3
        bot.send_message(chat_id, "Seleziona l’anno (oppure “Non so” / “Scrivo io”).", reply_markup=kb_year())
        return

    # YEAR
    if cb.startswith("year:"):
        if step != 3:
            return
        chosen = cb.split(":", 1)[1]
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass

        if chosen == "manual":
            st["step"] = 31
            bot.send_message(chat_id, "Scrivi l’anno (es. 2019) oppure “non so”.", reply_markup=kb_cancel())
            return

        req["year"] = "Non so" if chosen == "unknown" else chosen

        if req["type"] == "Serie":
            st["step"] = 4
            bot.send_message(chat_id, "La vuoi *completa* o vuoi specificare stagione/episodio?", reply_markup=kb_series_mode())
        else:
            req["season_episode"] = "-"
            st["step"] = 5
            bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        return

    # SERIES MODE
    if cb.startswith("series:"):
        if step != 4:
            return
        chosen = cb.split(":", 1)[1]
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass

        if chosen == "complete":
            req["season_episode"] = "Completa"
            st["step"] = 5
            bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        else:
            st["step"] = 41
            bot.send_message(chat_id, "Scrivi stagione/episodio (es. S2 E5) oppure “S2 completa”.", reply_markup=kb_cancel())
        return

    # LANGUAGE
    if cb.startswith("lang:"):
        if step != 5:
            return
        chosen = cb.split(":", 1)[1]
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass

        if chosen == "ALTRO":
            st["step"] = 51
            bot.send_message(chat_id, "Scrivi la lingua richiesta (es. ES, FR, ITA+SUB ENG, ecc.).", reply_markup=kb_cancel())
            return

        req["language"] = chosen
        st["step"] = 6
        bot.send_message(chat_id, "Note extra? (se nulla scrivi “-”)", reply_markup=kb_cancel())
        return

    # CONFIRM
    if cb.startswith("confirm:"):
        if step != 7:
            return
        action = cb.split(":", 1)[1]
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass

        if action == "editnotes":
            st["step"] = 6
            bot.send_message(chat_id, "Ok! Riscrivi le note (se nulla “-”).", reply_markup=kb_cancel())
            return

        # Re-check time + limits
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
        user_info = {
            "user_id": u.id,
            "username": f"@{u.username}" if u.username else "(no username)",
            "name": f"{u.first_name or ''} {u.last_name or ''}".strip(),
        }

        payload = (
            f"Utente: {user_info['name']} | {user_info['username']} | id:{user_info['user_id']}\n"
            f"Titolo: {req['title']}\n"
            f"Tipo: {req['type']}\n"
            f"Anno: {req['year']}\n"
            f"Stagione/Episodio: {req['season_episode']}\n"
            f"Lingua: {req['language']}\n"
            f"Note: {req['notes']}\n"
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

        ticket = {
            "admin_msg_id": msg_admin.message_id,
            "user_id": user_id,
            "user_chat_id": chat_id,
            "user_display": user_display_from_tg(u),
            "created_at": now_utc().isoformat(),
            "status": "Nuova",
            "assignee": None,
            "title": req["title"],
            "type": req["type"],
            "year": req["year"],
        }
        tickets[msg_admin.message_id] = ticket

        add_history(user_id, {
            "admin_msg_id": msg_admin.message_id,
            "created_at": ticket["created_at"],
            "title": ticket["title"],
            "type": ticket["type"],
            "year": ticket["year"],
            "status": "Nuova",
        })

        register_request_submission(user_id)
        inc_daily_counter()

        today_key = now_utc().strftime("%Y-%m-%d")
        bot.send_message(chat_id, f"✅ Inviato allo staff. (Richieste oggi: {daily_counts.get(today_key, 0)}) Grazie!")
        states.pop(user_id, None)
        return

# ======================
# TEXT HANDLER (steps)
# ======================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(m):
    user_id = m.from_user.id
    chat_id = m.chat.id
    text = (m.text or "").strip()

    if user_id not in states:
        if has_sensitive_request(text):
            bot.send_message(chat_id, "Posso solo registrare la richiesta e inoltrarla allo staff. Usa /request per iniziare.")
        return

    # Membership check even during flow
    if not is_allowed_user(user_id):
        states.pop(user_id, None)
        bot.send_message(chat_id, "⛔ Questo bot è riservato agli utenti del gruppo.")
        return

    st = states[user_id]
    step = st["step"]
    req = st["data"]

    if has_sensitive_request(text):
        bot.send_message(chat_id, "Non posso aiutare con link o accessi. Posso però registrare la richiesta. Prosegui rispondendo alle domande 🙂")
        return

    text = clean_text(text)

    # TITLE
    if step == 1:
        req["title"] = text
        st["step"] = 2
        bot.send_message(chat_id, "Perfetto. È un film o una serie?", reply_markup=kb_type())
        return

    # YEAR MANUAL
    if step == 31:
        req["year"] = text
        if req["type"] == "Serie":
            st["step"] = 4
            bot.send_message(chat_id, "La vuoi *completa* o vuoi specificare stagione/episodio?", reply_markup=kb_series_mode())
        else:
            req["season_episode"] = "-"
            st["step"] = 5
            bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        return

    # SERIES S/E TEXT
    if step == 41:
        req["season_episode"] = text
        st["step"] = 5
        bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        return

    # LANGUAGE MANUAL
    if step == 51:
        req["language"] = text
        st["step"] = 6
        bot.send_message(chat_id, "Note extra? (se nulla scrivi “-”)", reply_markup=kb_cancel())
        return

    # NOTES -> CONFIRM
    if step == 6:
        req["notes"] = text
        st["step"] = 7
        bot.send_message(chat_id, format_summary(req) + "\nConfermi l’invio allo staff?", reply_markup=kb_confirm())
        return

    bot.send_message(chat_id, "Se vuoi iniziare una nuova richiesta: /request (oppure /cancel per annullare).")

# ======================
# RUN FOREVER
# ======================
while True:
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    except Exception:
        time.sleep(3)
