import os
import re
import time
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI

# ======================
# ENV VARS
# ======================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()  # e.g. -1001234567890

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not ADMIN_CHAT_ID_RAW:
    raise RuntimeError("Missing ADMIN_CHAT_ID (e.g. -100...)")

try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
except ValueError as e:
    raise RuntimeError("ADMIN_CHAT_ID must be an integer (e.g. -100123...)") from e

# ======================
# INIT
# ======================
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
client = OpenAI(api_key=OPENAI_API_KEY)

# ======================
# STATE (in-memory)
# ======================
# user_id -> {"step": int, "data": dict}
states = {}

# admin_message_id -> {"user_chat_id": int, "user_display": str}
# (serve per notificare l'utente quando lo staff clicca)
admin_ticket_map = {}

# ======================
# HELPERS
# ======================
def clean_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:600]

def has_sensitive_request(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ["link", "m3u", "playlist", "username", "password", "accesso", "attivazione"])

def user_display_from_tg(user) -> str:
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else ""
    return (name + (" " + username if username else "")).strip() or "Utente"

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
Sei un assistente helpdesk per richieste VOD.
Il tuo compito è SOLO raccogliere e formattare richieste per lo staff.

Regole:
- Non fornire link, accessi, attivazioni o istruzioni per ottenere contenuti.
- Non fare promozioni o prezzi.
- Se l’utente chiede link/accesso/attivazione, rispondi educatamente che puoi solo registrare la richiesta (titolo/dettagli) e inoltrarla allo staff.
- Tono: educato, neutro, chiaro.

Output: crea una scheda richiesta in italiano, ordinata e breve.
"""

INTRO = (
    "Ciao! 👋 Posso registrare una richiesta VOD e inoltrarla allo staff.\n"
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

def kb_staff_status() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🟡 Presa in carico", callback_data="staff:in_progress"),
        InlineKeyboardButton("🟢 Completata", callback_data="staff:done"),
        InlineKeyboardButton("🔴 Non Disponibile", callback_data="staff:na"),
    )
    return kb

# ======================
# COMMANDS
# ======================
@bot.message_handler(commands=["start", "help"])
def start(m):
    bot.send_message(m.chat.id, INTRO)

@bot.message_handler(commands=["cancel"])
def cancel_cmd(m):
    states.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "Richiesta annullata. Se vuoi riprovare: /request")

@bot.message_handler(commands=["request"])
def request(m):
    init_state(m.from_user.id)
    bot.send_message(m.chat.id, "Ok! Dimmi il *titolo* (film o serie).", reply_markup=kb_cancel())

# ======================
# CALLBACK ROUTER
# ======================
@bot.callback_query_handler(func=lambda c: True)
def callback_router(call):
    # always ack callback (removes spinner)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data_cb = call.data or ""

    # --- CANCEL (works everywhere) ---
    if data_cb == "cancel":
        states.pop(user_id, None)
        # remove buttons if possible
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        bot.send_message(chat_id, "Richiesta annullata. Se vuoi riprovare: /request")
        return

    # --- STAFF BUTTONS (only in admin chat) ---
    if data_cb.startswith("staff:"):
        if chat_id != ADMIN_CHAT_ID:
            return

        action = data_cb.split(":", 1)[1]
        status_text = {
            "in_progress": "🟡 Presa in carico",
            "done": "🟢 Completata",
            "na": "🔴 Non Disponibile",
        }.get(action, "📌 Aggiornato")

        staff_name = user_display_from_tg(call.from_user)

        # Update admin message and REMOVE BUTTONS (disable after first click)
        try:
            bot.edit_message_text(
                call.message.text + f"\n\n📌 Stato: {status_text} (da {staff_name})",
                chat_id,
                msg_id,
                reply_markup=None,  # <- disattiva pulsanti dopo 1 click
            )
        except Exception:
            # fallback: at least remove keyboard
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            except Exception:
                pass

        # Notify user if we still have mapping
        info = admin_ticket_map.get(msg_id)
        if info:
            user_chat = info["user_chat_id"]
            try:
                if action == "in_progress":
                    bot.send_message(user_chat, "🟡 La tua richiesta è stata presa in carico dallo staff.")
                elif action == "done":
                    bot.send_message(user_chat, "🟢 La tua richiesta è stata completata. Grazie!")
                elif action == "na":
                    bot.send_message(user_chat, "🔴 La tua richiesta al momento non è disponibile.")
                else:
                    bot.send_message(user_chat, f"📌 Aggiornamento richiesta: {status_text}")
            except Exception:
                pass

            if action in ("done", "na"):
                admin_ticket_map.pop(msg_id, None)
        return

    # --- USER FLOW BUTTONS ---
    if user_id not in states:
        bot.send_message(chat_id, "Per iniziare una richiesta: /request")
        return

    st = states[user_id]
    step = st["step"]
    req = st["data"]

    # TYPE
    if data_cb.startswith("type:"):
        if step != 2:
            return
        chosen = data_cb.split(":", 1)[1]
        req["type"] = "Film" if chosen == "film" else "Serie"
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        st["step"] = 3
        bot.send_message(chat_id, "Seleziona l’anno (oppure “Non so” / “Scrivo io”).", reply_markup=kb_year())
        return

    # YEAR
    if data_cb.startswith("year:"):
        if step != 3:
            return
        chosen = data_cb.split(":", 1)[1]
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
    if data_cb.startswith("series:"):
        if step != 4:
            return
        chosen = data_cb.split(":", 1)[1]
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
    if data_cb.startswith("lang:"):
        if step != 5:
            return
        chosen = data_cb.split(":", 1)[1]
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
    if data_cb.startswith("confirm:"):
        if step != 7:
            return
        action = data_cb.split(":", 1)[1]
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass

        if action == "editnotes":
            st["step"] = 6
            bot.send_message(chat_id, "Ok! Riscrivi le note (se nulla “-”).", reply_markup=kb_cancel())
            return

        # SEND to staff
        user_info = {
            "user_id": call.from_user.id,
            "username": f"@{call.from_user.username}" if call.from_user.username else "(no username)",
            "name": f"{call.from_user.first_name or ''} {call.from_user.last_name or ''}".strip(),
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
            formatted = "📌 NUOVA RICHIESTA VOD\n" + payload

        # Send to admin with staff buttons
        msg_admin = bot.send_message(ADMIN_CHAT_ID, formatted, reply_markup=kb_staff_status())

        # Save mapping so staff click can notify user
        admin_ticket_map[msg_admin.message_id] = {
            "user_chat_id": chat_id,  # user private chat with bot
            "user_display": user_display_from_tg(call.from_user),
        }

        bot.send_message(chat_id, "✅ Inviato allo staff. Grazie!")
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
            bot.send_message(chat_id, "Posso solo registrare la richiesta (titolo/dettagli) e inoltrarla allo staff. Usa /request per iniziare.")
        return

    st = states[user_id]
    step = st["step"]
    req = st["data"]

    if has_sensitive_request(text):
        bot.send_message(chat_id, "Non posso aiutare con link o accessi. Posso però registrare la richiesta. Prosegui rispondendo alle domande 🙂")
        return

    text = clean_text(text)

    # 1) TITLE
    if step == 1:
        req["title"] = text
        st["step"] = 2
        bot.send_message(chat_id, "Perfetto. È un film o una serie?", reply_markup=kb_type())
        return

    # 31) YEAR MANUAL
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

    # 41) SERIES SEASON/EPISODE
    if step == 41:
        req["season_episode"] = text
        st["step"] = 5
        bot.send_message(chat_id, "Lingua richiesta?", reply_markup=kb_lang())
        return

    # 51) LANGUAGE MANUAL
    if step == 51:
        req["language"] = text
        st["step"] = 6
        bot.send_message(chat_id, "Note extra? (se nulla scrivi “-”)", reply_markup=kb_cancel())
        return

    # 6) NOTES -> CONFIRM
    if step == 6:
        req["notes"] = text
        summary = format_summary(req)
        st["step"] = 7
        bot.send_message(chat_id, summary + "\nConfermi l’invio allo staff?", reply_markup=kb_confirm())
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
