import os
import re
import time
import telebot
from openai import OpenAI

# ====== ENV VARS ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()  # e.g. -1001234567890

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not ADMIN_CHAT_ID:
    raise RuntimeError("Missing ADMIN_CHAT_ID (e.g. -100...)")

# ====== INIT ======
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
client = OpenAI(api_key=OPENAI_API_KEY)

# ====== SIMPLE STATE (in-memory) ======
# For production you'd use a DB, but this is fine to start.
states = {}  # user_id -> dict

def clean_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:500]

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

@bot.message_handler(commands=["start", "help"])
def start(m):
    bot.send_message(m.chat.id, INTRO)

@bot.message_handler(commands=["request"])
def request(m):
    user_id = m.from_user.id
    states[user_id] = {
        "step": 1,
        "data": {
            "title": "",
            "type": "",
            "year": "",
            "season_episode": "",
            "language": "",
            "notes": ""
        }
    }
    bot.send_message(m.chat.id, "Ok! Dimmi il *titolo* (film o serie).")

@bot.message_handler(commands=["cancel"])
def cancel(m):
    states.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "Richiesta annullata. Se vuoi riprovare: /request")

def ask_next(chat_id, step):
    prompts = {
        2: "È un *film* o una *serie*? (scrivi: film/serie)",
        3: "Anno (se lo sai) oppure scrivi “non so”.",
        4: "Se è una serie: stagione/episodio (es. S2 E5) oppure “completa”. Se è un film: scrivi “-”.",
        5: "Lingua richiesta? (ITA / ENG / ITA+ENG / altro)",
        6: "Note extra (es. versione extended, qualità preferita, sottotitoli). Se nulla, scrivi “-”."
    }
    bot.send_message(chat_id, prompts[step])

@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle(m):
    user_id = m.from_user.id
    text = (m.text or "").strip()

    # If user hasn't started a request, gently guide.
    if user_id not in states:
        # If they ask for links/access, refuse and guide.
        lowered = text.lower()
        if any(k in lowered for k in ["link", "m3u", "playlist", "username", "password", "accesso", "attivazione"]):
            bot.send_message(m.chat.id, "Posso solo registrare la richiesta (titolo/dettagli) e inoltrarla allo staff. Usa /request per iniziare.")
            return
        return

    st = states[user_id]
    step = st["step"]
    data = st["data"]

    # Hard-stop if they ask for links/access during flow
    lowered = text.lower()
    if any(k in lowered for k in ["link", "m3u", "playlist", "username", "password", "accesso", "attivazione"]):
        bot.send_message(m.chat.id, "Non posso aiutare con link o accessi. Posso però registrare la richiesta. Prosegui rispondendo alle domande 🙂")
        return

    text = clean_text(text)

    if step == 1:
        data["title"] = text
        st["step"] = 2
        ask_next(m.chat.id, 2)
        return

    if step == 2:
        t = text.lower()
        if "film" in t:
            data["type"] = "Film"
        elif "serie" in t:
            data["type"] = "Serie"
        else:
            bot.send_message(m.chat.id, "Per favore scrivi solo: film oppure serie.")
            return
        st["step"] = 3
        ask_next(m.chat.id, 3)
        return

    if step == 3:
        data["year"] = text
        st["step"] = 4
        ask_next(m.chat.id, 4)
        return

    if step == 4:
        data["season_episode"] = text
        st["step"] = 5
        ask_next(m.chat.id, 5)
        return

    if step == 5:
        data["language"] = text
        st["step"] = 6
        ask_next(m.chat.id, 6)
        return

    if step == 6:
        data["notes"] = text

        # Build a user summary and ask ChatGPT to format a neat admin ticket
        user_info = {
            "user_id": m.from_user.id,
            "username": f"@{m.from_user.username}" if m.from_user.username else "(no username)",
            "name": f"{m.from_user.first_name or ''} {m.from_user.last_name or ''}".strip()
        }

        user_payload = (
            f"Utente: {user_info['name']} | {user_info['username']} | id:{user_info['user_id']}\n"
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
                    {"role": "user", "content": user_payload},
                ],
            )
            formatted = resp.choices[0].message.content.strip()
        except Exception:
            formatted = "📌 NUOVA RICHIESTA VOD\n" + user_payload

        # Send to admin channel
        bot.send_message(int(ADMIN_CHAT_ID), formatted)

        # Confirm to user
        bot.send_message(m.chat.id, "✅ Perfetto! Ho inoltrato la richiesta allo staff. Grazie.")

        # Clear state
        states.pop(user_id, None)
        return

# Keep the bot running
while True:
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    except Exception:
        time.sleep(3)
