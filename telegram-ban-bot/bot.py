import atexit
import datetime
import json
import logging
import os
import signal
import subprocess
from zoneinfo import ZoneInfo

BERLIN_TZ = ZoneInfo("Europe/Berlin")

def now_de():
    """Return current datetime in German timezone."""
    return datetime.datetime.now(BERLIN_TZ)
import requests as _requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ChatMemberHandler, ChatJoinRequestHandler, filters, ContextTypes
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
GROUPS_FILE = os.path.join(os.path.dirname(__file__), "groups.json")
LOCK_FILE = os.path.join(os.path.dirname(__file__), "bot.lock")

# --- Config helpers ---

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")

def normalize_data(data):
    data.setdefault("groups", [])
    data.setdefault("banned_users", {})
    data.setdefault("broadcasts", {})
    data.setdefault("scheduled", [])
    data.setdefault("open_close", {
        "open_sticker": None,
        "close_sticker": None,
        "notify_groups": [],
        "open_text": "Hey Freunde, wir haben geöffnet! 🎉\nKommt rein und gönnt euch!",
        "close_text": "Wir haben geschlossen. Bis zum nächsten Mal! 👋",
        "active_open_messages": {},
    })
    return data

def load_data():
    if not os.path.exists(DATA_FILE):
        return normalize_data({"groups": [], "banned_users": {}})
    with open(DATA_FILE, "r") as f:
        return normalize_data(json.load(f))

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def remember_group_ban(group_ids, user_id, name=None, username=None):
    data = load_data()
    banned_users = data.setdefault("banned_users", {})

    for group_id in group_ids:
        group_key = str(group_id)
        group_bans = banned_users.setdefault(group_key, {})
        group_bans[str(user_id)] = {
            "id": user_id,
            "name": name or str(user_id),
            "username": username,
        }

    save_data(data)

def forget_group_ban(group_ids, user_id):
    data = load_data()
    banned_users = data.setdefault("banned_users", {})

    for group_id in group_ids:
        group_bans = banned_users.get(str(group_id), {})
        group_bans.pop(str(user_id), None)

    save_data(data)

def is_banned_in_group(group_id, user_id):
    data = load_data()
    group_bans = data.get("banned_users", {}).get(str(group_id), {})
    return str(user_id) in group_bans

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r") as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

def track_user(user):
    """Track a user's username → ID mapping."""
    if not user or user.is_bot:
        return
    users = load_users()
    if user.username:
        users[user.username.lower()] = {
            "id": user.id,
            "name": user.full_name,
            "username": user.username,
        }
    # Also store by ID for reverse lookup
    users[str(user.id)] = {
        "id": user.id,
        "name": user.full_name,
        "username": user.username,
    }
    save_users(users)

def lookup_user(identifier: str):
    """Lookup user by username or ID from tracked users."""
    users = load_users()
    key = identifier.lower().lstrip("@")
    return users.get(key)

def is_owner(user_id: int) -> bool:
    cfg = load_config()
    return user_id in cfg.get("owner_ids", [])

def is_admin(user_id: int) -> bool:
    cfg = load_config()
    return user_id in cfg.get("admin_ids", []) or is_owner(user_id)

def is_authorized(user_id: int) -> bool:
    """Check if user is owner or admin (can use ban/unban)."""
    return is_admin(user_id)

async def log_action(context: ContextTypes.DEFAULT_TYPE, text: str):
    cfg = load_config()
    channel = cfg.get("log_channel_id")
    if channel:
        try:
            await context.bot.send_message(chat_id=channel, text=f"📋 {text}")
        except Exception as e:
            logger.error(f"Log channel error: {e}")

# --- Get bot's groups ---

async def get_bot_groups(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Returns list of groups stored in data.json. Groups are added via /registergroup."""
    data = load_data()
    groups = data.get("groups", [])
    result = []
    for g in groups:
        try:
            chat = await context.bot.get_chat(g["id"])
            result.append({"id": chat.id, "title": chat.title})
        except Exception:
            result.append({"id": g["id"], "title": g.get("title", str(g["id"]))})
    return result

# --- /start ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    keyboard = [
        [InlineKeyboardButton("🚫 BannALL", callback_data="menu_banall")],
        [InlineKeyboardButton("📨 Messenger", callback_data="menu_messenger")],
        [InlineKeyboardButton("🔁 Wiederholte Nachrichten", callback_data="menu_scheduled")],
        [InlineKeyboardButton("🔓 Open / Close", callback_data="menu_openclose")],
    ]

    # Owner-only settings
    if is_owner(user_id):
        keyboard.append([InlineKeyboardButton("⚙️ Einstellungen", callback_data="menu_settings")])

    role = "👑 Owner" if is_owner(user_id) else "🛡️ Admin"
    await update.message.reply_text(
        f"🤖 *Bot Menü* ({role})\nWähle eine Aktion:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

# --- Callback handler ---

# Conversation states
WAITING_BAN_INPUT, WAITING_UNBAN_INPUT = range(2)
WAITING_ADMIN_ADD, WAITING_ADMIN_REMOVE = range(2, 4)
WAITING_LOG_CHANNEL = 4
WAITING_GROUP_SELECT_BAN, WAITING_GROUP_SELECT_UNBAN = range(5, 7)
WAITING_MESSENGER_INPUT = 7
WAITING_SCHEDULED_TEXT = 8
WAITING_SCHEDULED_TIME = 9
WAITING_SCHEDULED_MEDIA = 10
WAITING_SCHED_STARTDATE = 11
WAITING_SCHED_ENDDATE = 12
WAITING_SCHED_TIMESPAN = 13
WAITING_OPEN_STICKER = 14
WAITING_CLOSE_STICKER = 15

# Store pending data
user_data_store = {}

def get_interval_label(minutes):
    labels = {
        1: "1 Min", 2: "2 Min", 3: "3 Min", 5: "5 Min",
        10: "10 Min", 15: "15 Min", 20: "20 Min", 30: "30 Min",
        60: "1 Stunde", 120: "2 Stunden", 180: "3 Stunden", 240: "4 Stunden",
        360: "6 Stunden", 480: "8 Stunden", 720: "12 Stunden", 1440: "24 Stunden",
    }
    return labels.get(minutes, f"{minutes} Min")

async def show_messenger_selection(query, context, user_id, groups):
    """Show group selection grid with checkboxes in 2-column layout."""
    selected = user_data_store.get(user_id, {}).get("selected", set())
    keyboard = []
    # 2-column layout for groups
    row = []
    for g in groups:
        check = "✅" if g["id"] in selected else "⬜"
        row.append(InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"msg_toggle_{g['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    # Select all / none
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data="msg_select_all"),
        InlineKeyboardButton("◻️ Keine", callback_data="msg_select_none"),
    ])
    keyboard.append([InlineKeyboardButton(f"📨 Senden ({len(selected)} gewählt)", callback_data="msg_confirm_selection")])
    # Show delete old broadcasts button if any exist
    bot_data = load_data()
    if bot_data.get("broadcasts"):
        keyboard.append([InlineKeyboardButton("🗑 Gesendete Nachrichten löschen", callback_data="show_broadcasts")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])
    await query.edit_message_text(
        "📨 *Messenger*\nWähle die Gruppen aus:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_authorized(user_id):
        await query.edit_message_text("⛔ Kein Zugriff.")
        return

    data = query.data

    # === BACK TO MAIN MENU ===
    if data == "back_main":
        keyboard = [
            [InlineKeyboardButton("🚫 BannALL", callback_data="menu_banall")],
            [InlineKeyboardButton("📨 Messenger", callback_data="menu_messenger")],
            [InlineKeyboardButton("🔁 Wiederholte Nachrichten", callback_data="menu_scheduled")],
            [InlineKeyboardButton("🔓 Open / Close", callback_data="menu_openclose")],
        ]
        if is_owner(user_id):
            keyboard.append([InlineKeyboardButton("⚙️ Einstellungen", callback_data="menu_settings")])
        role = "👑 Owner" if is_owner(user_id) else "🛡️ Admin"
        # Clear any pending state
        user_data_store.pop(user_id, None)
        await query.edit_message_text(
            f"🤖 *Bot Menü* ({role})\nWähle eine Aktion:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    # === MAIN MENU ===
    elif data == "menu_banall":
        keyboard = [
            [InlineKeyboardButton("🚫 Ban", callback_data="action_ban"),
             InlineKeyboardButton("✅ Unban", callback_data="action_unban")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            "🚫 *BannALL*\nWähle eine Aktion:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    elif data == "menu_messenger":
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.")
            return
        # Initialize selection state
        if user_id not in user_data_store or user_data_store[user_id].get("action") != "msg_select":
            user_data_store[user_id] = {"action": "msg_select", "selected": set()}
        await show_messenger_selection(query, context, user_id, groups)

    elif data.startswith("msg_toggle_"):
        gid = int(data.replace("msg_toggle_", ""))
        pending = user_data_store.get(user_id, {})
        selected = pending.get("selected", set())
        if gid in selected:
            selected.discard(gid)
        else:
            selected.add(gid)
        pending["selected"] = selected
        user_data_store[user_id] = pending
        groups = await get_bot_groups(context)
        await show_messenger_selection(query, context, user_id, groups)

    elif data == "msg_select_all":
        groups = await get_bot_groups(context)
        user_data_store[user_id] = {"action": "msg_select", "selected": {g["id"] for g in groups}}
        await show_messenger_selection(query, context, user_id, groups)

    elif data == "msg_select_none":
        user_data_store[user_id] = {"action": "msg_select", "selected": set()}
        groups = await get_bot_groups(context)
        await show_messenger_selection(query, context, user_id, groups)

    elif data == "msg_confirm_selection":
        pending = user_data_store.get(user_id, {})
        selected = pending.get("selected", set())
        if not selected:
            await query.answer("⚠️ Wähle mindestens eine Gruppe!", show_alert=True)
            return
        user_data_store[user_id] = {"action": "messenger", "groups": list(selected)}
        await query.edit_message_text(
            "📨 Sende mir jetzt die Nachricht.\n\n"
            "Tipp: Markiere deinen Text und nutze die Telegram-Formatierung (Fett, Kursiv, Link, Zitat usw.) – wird 1:1 übernommen.",
        )
        context.user_data["state"] = WAITING_MESSENGER_INPUT

    # === SHOW BROADCASTS FOR DELETION ===
    elif data == "show_broadcasts":
        bot_data = load_data()
        broadcasts = bot_data.get("broadcasts", {})
        if not broadcasts:
            await query.edit_message_text("Keine gesendeten Nachrichten vorhanden.")
            return
        keyboard = []
        for bid, info in list(broadcasts.items()):
            label = f"🗑 {info.get('date', '?')} – {info.get('count', '?')} Gruppen"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"del_broadcast_{bid}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_messenger")])
        await query.edit_message_text(
            "🗑 *Gesendete Nachrichten:*\nWähle eine zum Löschen:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    # === DELETE BROADCAST ===
    elif data.startswith("del_broadcast_"):
        broadcast_id = data.replace("del_broadcast_", "")
        bot_data = load_data()
        broadcasts = bot_data.get("broadcasts", {})
        msgs = broadcasts.pop(broadcast_id, {}).get("messages", [])
        save_data(bot_data)
        deleted = 0
        for entry in msgs:
            try:
                await context.bot.delete_message(chat_id=entry[0], message_id=entry[1])
                deleted += 1
            except Exception as e:
                logger.error(f"Delete broadcast msg failed in {entry[0]}: {e}")
        await query.edit_message_text(f"🗑 {deleted} Nachrichten gelöscht.")

    # === SCHEDULED MESSAGES ===
    elif data == "menu_scheduled":
        await show_scheduled_list(query, context, user_id)

    elif data.startswith("sched_page_"):
        page = int(data.replace("sched_page_", ""))
        await show_scheduled_list(query, context, user_id, page=page)

    elif data.startswith("sched_toggle_active_list_"):
        sched_id = data.replace("sched_toggle_active_list_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["active"] = not s["active"]
                save_data(bot_data)
                if s["active"]:
                    schedule_job(context, s)
                else:
                    remove_scheduled_job(context, sched_id)
                break
        await show_scheduled_list(query, context, user_id)

    elif data.startswith("sched_delete_confirm_"):
        sched_id = data.replace("sched_delete_confirm_", "")
        keyboard = [
            [InlineKeyboardButton("✅ Ja, löschen", callback_data=f"sched_delete_{sched_id}"),
             InlineKeyboardButton("❌ Abbrechen", callback_data="menu_scheduled")],
        ]
        await query.edit_message_text(
            "⚠️ Nachricht wirklich löschen?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "sched_new":
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.")
            return
        user_data_store[user_id] = {"action": "sched_select", "selected": set()}
        await show_sched_group_selection(query, context, user_id, groups)

    elif data.startswith("sched_toggle_") and not data.startswith("sched_toggle_active_"):
        gid = int(data.replace("sched_toggle_", ""))
        pending = user_data_store.get(user_id, {})
        selected = pending.get("selected", set())
        if gid in selected:
            selected.discard(gid)
        else:
            selected.add(gid)
        pending["selected"] = selected
        user_data_store[user_id] = pending
        groups = await get_bot_groups(context)
        await show_sched_group_selection(query, context, user_id, groups)

    elif data == "sched_select_all":
        groups = await get_bot_groups(context)
        user_data_store[user_id] = {"action": "sched_select", "selected": {g["id"] for g in groups}}
        await show_sched_group_selection(query, context, user_id, groups)

    elif data == "sched_select_none":
        user_data_store[user_id] = {"action": "sched_select", "selected": set()}
        groups = await get_bot_groups(context)
        await show_sched_group_selection(query, context, user_id, groups)

    elif data == "sched_confirm_groups":
        pending = user_data_store.get(user_id, {})
        selected = pending.get("selected", set())
        if not selected:
            await query.answer("⚠️ Wähle mindestens eine Gruppe!", show_alert=True)
            return
        # Create the scheduled message immediately with defaults
        import time as _time, datetime
        sched_id = str(int(_time.time() * 1000))
        bot_data = load_data()
        new_sched = {
            "id": sched_id,
            "groups": list(selected),
            "text": "",
            "text_html": "",
            "time": now_de().strftime("%H:%M"),
            "interval_minutes": 1440,
            "interval_label": "Alle 24 Stunden",
            "active": False,
            "created_by": user_id,
            "created_at": now_de().strftime("%d.%m.%Y %H:%M"),
            "last_sent": None,
            "last_sent_messages": [],
            "delete_previous": False,
            "pin_message": False,
        }
        bot_data.setdefault("scheduled", []).append(new_sched)
        save_data(bot_data)
        user_data_store.pop(user_id, None)
        context.user_data["state"] = None
        await show_scheduled_detail(query, context, user_id, sched_id)

    elif data == "sched_time_confirm":
        # After text is set, show hour picker
        await show_hour_picker(query, context, user_id)

    elif data.startswith("sched_hour_"):
        hour = int(data.replace("sched_hour_", ""))
        pending = user_data_store.get(user_id, {})
        pending["hour"] = hour
        user_data_store[user_id] = pending
        await show_minute_picker(query, context, user_id, hour)

    elif data.startswith("sched_minute_"):
        minute = int(data.replace("sched_minute_", ""))
        pending = user_data_store.get(user_id, {})
        hour = pending.get("hour", 0)
        pending["time"] = f"{hour:02d}:{minute:02d}"
        pending["action"] = "sched_set_time"
        user_data_store[user_id] = pending
        # Show interval picker
        await show_interval_picker(query, context, user_id)

    elif data == "noop":
        # Do nothing - used for section headers
        return

    elif data.startswith("sched_interval_"):
        minutes = int(data.replace("sched_interval_", ""))
        pending = user_data_store.get(user_id, {})
        if not pending:
            await query.edit_message_text("⚠️ Bitte starte nochmal.")
            return
        
        import time as _time, datetime
        sched_id = str(int(_time.time() * 1000))
        bot_data = load_data()
        
        new_sched = {
            "id": sched_id,
            "groups": pending["groups"],
            "text": pending["text"],
            "text_html": pending.get("text_html", pending["text"]),
            "time": pending.get("time", now_de().strftime("%H:%M")),
            "interval_minutes": minutes,
            "interval_label": f"Alle {get_interval_label(minutes)}",
            "active": True,
            "created_by": user_id,
            "created_at": now_de().strftime("%d.%m.%Y %H:%M"),
            "last_sent": None,
            "last_sent_messages": [],
            "delete_previous": True,
        }
        bot_data.setdefault("scheduled", []).append(new_sched)
        save_data(bot_data)
        
        schedule_job(context, new_sched)
        
        groups = await get_bot_groups(context)
        group_names = [g["title"] for g in groups if g["id"] in pending["groups"]]
        
        keyboard = [[InlineKeyboardButton("📋 Alle Nachrichten anzeigen", callback_data="menu_scheduled")]]
        await query.edit_message_text(
            f"✅ *Wiederholte Nachricht erstellt!*\n\n"
            f"⏰ Zeit: {new_sched['time']}\n"
            f"🔁 {new_sched['interval_label']}\n"
            f"📨 Gruppen: {', '.join(group_names)}\n"
            f"📝 Text: {pending['text'][:50]}...\n\n"
            f"🚀 Nächster Versand wird geplant",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        user_data_store.pop(user_id, None)
        context.user_data["state"] = None

    elif data.startswith("sched_view_"):
        sched_id = data.replace("sched_view_", "")
        await show_scheduled_detail(query, context, user_id, sched_id)

    elif data.startswith("sched_toggle_active_"):
        sched_id = data.replace("sched_toggle_active_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["active"] = not s["active"]
                save_data(bot_data)
                if s["active"]:
                    schedule_job(context, s)
                else:
                    remove_scheduled_job(context, sched_id)
                break
        await show_scheduled_detail(query, context, user_id, sched_id)

    elif data.startswith("sched_delete_"):
        sched_id = data.replace("sched_delete_", "")
        bot_data = load_data()
        bot_data["scheduled"] = [s for s in bot_data.get("scheduled", []) if s["id"] != sched_id]
        save_data(bot_data)
        remove_scheduled_job(context, sched_id)
        keyboard = [[InlineKeyboardButton("📋 Alle Nachrichten anzeigen", callback_data="menu_scheduled")],
                     [InlineKeyboardButton("🔙 Hauptmenü", callback_data="back_main")]]
        await query.edit_message_text(
            "🗑 Wiederholte Nachricht wurde gelöscht.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("sched_del_prev_"):
        sched_id = data.replace("sched_del_prev_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["delete_previous"] = not s.get("delete_previous", True)
                save_data(bot_data)
                break
        await show_scheduled_detail(query, context, user_id, sched_id)

    elif data.startswith("sched_pin_"):
        sched_id = data.replace("sched_pin_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["pin_message"] = not s.get("pin_message", False)
                save_data(bot_data)
                break
        await show_scheduled_detail(query, context, user_id, sched_id)

    # === WOCHENTAGE ===
    elif data.startswith("sched_weekdays_") and not data.startswith("sched_weekdays_toggle_"):
        sched_id = data.replace("sched_weekdays_", "")
        await show_weekdays_picker(query, context, sched_id)

    elif data.startswith("sched_weekdays_toggle_"):
        parts = data.replace("sched_weekdays_toggle_", "").rsplit("_", 1)
        sched_id, day = parts[0], int(parts[1])
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                days = set(s.get("weekdays", [0,1,2,3,4,5,6]))
                if day in days:
                    days.discard(day)
                else:
                    days.add(day)
                s["weekdays"] = sorted(days)
                save_data(bot_data)
                break
        await show_weekdays_picker(query, context, sched_id)

    # === TAGE DES MONATS ===
    elif data.startswith("sched_monthdays_") and not data.startswith("sched_monthdays_toggle_"):
        sched_id = data.replace("sched_monthdays_", "")
        await show_monthdays_picker(query, context, sched_id)

    elif data.startswith("sched_monthdays_toggle_"):
        parts = data.replace("sched_monthdays_toggle_", "").rsplit("_", 1)
        sched_id, day = parts[0], int(parts[1])
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                days = set(s.get("monthdays", []))
                if day in days:
                    days.discard(day)
                else:
                    days.add(day)
                s["monthdays"] = sorted(days)
                save_data(bot_data)
                break
        await show_monthdays_picker(query, context, sched_id)

    # === ANFANGSDATUM ===
    elif data.startswith("sched_startdate_"):
        sched_id = data.replace("sched_startdate_", "")
        user_data_store[user_id] = {"action": "sched_startdate", "sched_id": sched_id}
        now = now_de().strftime("%d/%m/%y %H:%M")
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data=f"sched_view_{sched_id}")]]
        await query.edit_message_text(
            f"🕐 <b>Wiederholte Mitteilungen</b>\n\n"
            f"👉 Jetzt das Datum und die Uhrzeit des Beginns der Nachrichtenwiederholung senden.\n\n"
            f"❓ Du mußt das Datum im Format tt/mm/jj hh:mm angeben\n"
            f"Beispiel: {now}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        context.user_data["state"] = WAITING_SCHED_STARTDATE

    # === ENDDATUM ===
    elif data.startswith("sched_enddate_"):
        sched_id = data.replace("sched_enddate_", "")
        user_data_store[user_id] = {"action": "sched_enddate", "sched_id": sched_id}
        now = now_de().strftime("%d/%m/%y %H:%M")
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data=f"sched_view_{sched_id}")]]
        await query.edit_message_text(
            f"🕐 <b>Wiederholte Mitteilungen</b>\n\n"
            f"👉 Sende jetzt das Ende der Nachrichtenwiederholung mit Datum und Uhrzeit.\n\n"
            f"❓ Du mußt das Datum im Format tt/mm/jj hh:mm angeben\n"
            f"Beispiel: {now}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        context.user_data["state"] = WAITING_SCHED_ENDDATE

    # === ZEITSPANNE ===
    elif data.startswith("sched_timespan_"):
        sched_id = data.replace("sched_timespan_", "")
        await show_timespan_picker(query, context, sched_id)

    elif data.startswith("sched_ts_set_"):
        parts = data.replace("sched_ts_set_", "").rsplit("_", 1)
        sched_id, minutes = parts[0], int(parts[1])
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["timespan_minutes"] = minutes
                save_data(bot_data)
                break
        await show_scheduled_detail(query, context, user_id, sched_id)

    # === AUTOMATISCH LÖSCHEN ===
    elif data.startswith("sched_autodelete_") and not data.startswith("sched_autodelete_set_") and not data.startswith("sched_autodelete_off_"):
        sched_id = data.replace("sched_autodelete_", "")
        await show_autodelete_picker(query, context, sched_id)

    elif data.startswith("sched_autodelete_set_"):
        parts = data.replace("sched_autodelete_set_", "").rsplit("_", 1)
        sched_id, minutes = parts[0], int(parts[1])
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["autodelete_minutes"] = minutes
                save_data(bot_data)
                break
        await show_scheduled_detail(query, context, user_id, sched_id)

    elif data.startswith("sched_autodelete_off_"):
        sched_id = data.replace("sched_autodelete_off_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s.pop("autodelete_minutes", None)
                save_data(bot_data)
                break
        await show_scheduled_detail(query, context, user_id, sched_id)

    elif data.startswith("sched_edit_text_"):
        sched_id = data.replace("sched_edit_text_", "")
        await show_sched_content_menu(query, context, user_id, sched_id)

    elif data.startswith("sched_set_text_"):
        sched_id = data.replace("sched_set_text_", "")
        user_data_store[user_id] = {"action": "sched_edit_text", "sched_id": sched_id}
        await query.edit_message_text(
            "✏️ Sende mir die neue Nachricht.\n\n"
            "Tipp: Nutze die Telegram-Formatierung (Fett, Kursiv, Link, Zitat).",
        )
        context.user_data["state"] = WAITING_SCHEDULED_TEXT

    elif data.startswith("sched_edit_time_"):
        sched_id = data.replace("sched_edit_time_", "")
        user_data_store[user_id] = {"action": "sched_edit_time", "sched_id": sched_id}
        await show_hour_picker(query, context, user_id, back_callback=f"sched_view_{sched_id}")

    elif data.startswith("sched_edit_hour_"):
        # Format: sched_edit_hour_SCHEDID_HOUR
        parts = data.replace("sched_edit_hour_", "").rsplit("_", 1)
        sched_id, hour = parts[0], int(parts[1])
        user_data_store[user_id] = {"action": "sched_edit_time", "sched_id": sched_id, "hour": hour}
        await show_minute_picker(query, context, user_id, hour, back_callback=f"sched_edit_time_{sched_id}", edit_sched_id=sched_id)

    elif data.startswith("sched_edit_min_"):
        # Format: sched_edit_min_SCHEDID_MINUTE
        parts = data.replace("sched_edit_min_", "").rsplit("_", 1)
        sched_id, minute = parts[0], int(parts[1])
        pending = user_data_store.get(user_id, {})
        hour = pending.get("hour", 0)
        time_str = f"{hour:02d}:{minute:02d}"
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["time"] = time_str
                save_data(bot_data)
                if s.get("active"):
                    schedule_job(context, s)
                break
        user_data_store.pop(user_id, None)
        await show_scheduled_detail(query, context, user_id, sched_id)

    elif data.startswith("sched_edit_interval_"):
        sched_id = data.replace("sched_edit_interval_", "")
        # Get current interval to show checkmark
        bot_data = load_data()
        current_minutes = 240
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                current_minutes = s.get("interval_minutes", 240)
                break
        await show_interval_picker(query, context, user_id, back_callback=f"sched_view_{sched_id}", edit_sched_id=sched_id, current_minutes=current_minutes)

    elif data.startswith("sched_view_text_"):
        sched_id = data.replace("sched_view_text_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                preview_html = s.get("text_html", s.get("text", "(leer)"))
                keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data=f"sched_edit_text_{sched_id}")]]
                await query.edit_message_text(
                    f"📄 <b>Nachrichtentext:</b>\n\n{preview_html}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
                return
        await query.edit_message_text("⚠️ Nicht gefunden.")

    elif data.startswith("sched_set_media_"):
        sched_id = data.replace("sched_set_media_", "")
        user_data_store[user_id] = {"action": "sched_set_media", "sched_id": sched_id}
        bot_data = load_data()
        sched = next((s for s in bot_data.get("scheduled", []) if s["id"] == sched_id), None)
        keyboard = []
        if sched and sched.get("media_file_id"):
            keyboard.append([InlineKeyboardButton("🚫 Mitteilung entfernen", callback_data=f"sched_remove_media_{sched_id}")])
        keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data=f"sched_edit_text_{sched_id}")])
        await query.edit_message_text(
            "👉 <b>Sende jetzt ein Medium</b> (Foto, Video, Sticker ... ), das Du einstellen möchtest.\n"
            "<i>Du kannst auch eine Bildunterschrift eingeben.</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        context.user_data["state"] = WAITING_SCHEDULED_MEDIA

    elif data.startswith("sched_remove_media_"):
        sched_id = data.replace("sched_remove_media_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s.pop("media_file_id", None)
                s.pop("media_type", None)
                save_data(bot_data)
                break
        await show_sched_content_menu(query, context, user_id, sched_id)

    elif data.startswith("sched_view_media_"):
        sched_id = data.replace("sched_view_media_", "")
        bot_data = load_data()
        sched = next((s for s in bot_data.get("scheduled", []) if s["id"] == sched_id), None)
        if not sched or not sched.get("media_file_id"):
            await query.answer("Kein Medium gesetzt.", show_alert=True)
            return
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data=f"sched_edit_text_{sched_id}")]]
        media_type = sched.get("media_type", "photo")
        try:
            if media_type == "photo":
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=sched["media_file_id"], reply_markup=InlineKeyboardMarkup(keyboard))
            elif media_type == "video":
                await context.bot.send_video(chat_id=query.message.chat_id, video=sched["media_file_id"], reply_markup=InlineKeyboardMarkup(keyboard))
            elif media_type == "animation":
                await context.bot.send_animation(chat_id=query.message.chat_id, animation=sched["media_file_id"], reply_markup=InlineKeyboardMarkup(keyboard))
            elif media_type == "sticker":
                await context.bot.send_sticker(chat_id=query.message.chat_id, sticker=sched["media_file_id"])
                await context.bot.send_message(chat_id=query.message.chat_id, text="⬆️ Aktueller Sticker", reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await context.bot.send_document(chat_id=query.message.chat_id, document=sched["media_file_id"], reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            await query.edit_message_text(f"⚠️ Fehler beim Anzeigen: {e}")

    elif data.startswith("sched_preview_"):
        sched_id = data.replace("sched_preview_", "")
        bot_data = load_data()
        sched = next((s for s in bot_data.get("scheduled", []) if s["id"] == sched_id), None)
        if not sched:
            await query.edit_message_text("⚠️ Nicht gefunden.")
            return
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data=f"sched_edit_text_{sched_id}")]]
        text_html = sched.get("text_html", sched.get("text", ""))
        media_fid = sched.get("media_file_id")
        media_type = sched.get("media_type", "photo")
        try:
            if media_fid and text_html:
                if media_type == "photo":
                    await context.bot.send_photo(chat_id=query.message.chat_id, photo=media_fid, caption=text_html, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
                elif media_type == "video":
                    await context.bot.send_video(chat_id=query.message.chat_id, video=media_fid, caption=text_html, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
                elif media_type == "animation":
                    await context.bot.send_animation(chat_id=query.message.chat_id, animation=media_fid, caption=text_html, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
                else:
                    await context.bot.send_document(chat_id=query.message.chat_id, document=media_fid, caption=text_html, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
            elif media_fid:
                if media_type == "photo":
                    await context.bot.send_photo(chat_id=query.message.chat_id, photo=media_fid, reply_markup=InlineKeyboardMarkup(keyboard))
                elif media_type == "video":
                    await context.bot.send_video(chat_id=query.message.chat_id, video=media_fid, reply_markup=InlineKeyboardMarkup(keyboard))
                else:
                    await context.bot.send_document(chat_id=query.message.chat_id, document=media_fid, reply_markup=InlineKeyboardMarkup(keyboard))
            elif text_html:
                await query.edit_message_text(f"👀 <b>Vorschau:</b>\n\n{text_html}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            else:
                await query.answer("Kein Inhalt gesetzt.", show_alert=True)
        except Exception as e:
            await query.edit_message_text(f"⚠️ Vorschau-Fehler: {e}")

    elif data.startswith("sched_set_int_"):
        parts = data.replace("sched_set_int_", "").rsplit("_", 1)
        sched_id, minutes = parts[0], int(parts[1])
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["interval_minutes"] = minutes
                s["interval_label"] = f"Alle {get_interval_label(minutes)}"
                save_data(bot_data)
                if s.get("active"):
                    schedule_job(context, s)
                break
        await show_scheduled_detail(query, context, user_id, sched_id)

    # === EDIT GROUPS ON EXISTING SCHEDULED MESSAGE ===
    elif data.startswith("sched_edit_groups_"):
        sched_id = data.replace("sched_edit_groups_", "")
        await show_sched_edit_groups(query, context, user_id, sched_id)

    elif data.startswith("sched_grp_toggle_"):
        rest = data.replace("sched_grp_toggle_", "")
        # Format: sched_grp_toggle_SCHEDID_GROUPID
        parts = rest.rsplit("_", 1)
        sched_id, gid = parts[0], int(parts[1])
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                groups_set = set(s.get("groups", []))
                if gid in groups_set:
                    groups_set.discard(gid)
                else:
                    groups_set.add(gid)
                s["groups"] = list(groups_set)
                save_data(bot_data)
                if s.get("active"):
                    schedule_job(context, s)
                break
        await show_sched_edit_groups(query, context, user_id, sched_id)

    elif data.startswith("sched_grp_all_"):
        sched_id = data.replace("sched_grp_all_", "")
        groups = await get_bot_groups(context)
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["groups"] = [g["id"] for g in groups]
                save_data(bot_data)
                if s.get("active"):
                    schedule_job(context, s)
                break
        await show_sched_edit_groups(query, context, user_id, sched_id)

    elif data.startswith("sched_grp_none_"):
        sched_id = data.replace("sched_grp_none_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["groups"] = []
                save_data(bot_data)
                if s.get("active"):
                    remove_scheduled_job(context, sched_id)
                    s["active"] = False
                    save_data(bot_data)
                break
        await show_sched_edit_groups(query, context, user_id, sched_id)

    # === BAN/UNBAN ===
    elif data == "action_ban":
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.")
            return
        keyboard = []
        keyboard.append([InlineKeyboardButton("🔴 ALLE GRUPPEN", callback_data="ban_all_groups")])
        for g in groups:
            keyboard.append([InlineKeyboardButton(g["title"], callback_data=f"ban_group_{g['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_banall")])
        await query.edit_message_text(
            "Wähle die Gruppe(n) für den Ban:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "action_unban":
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.")
            return
        keyboard = []
        keyboard.append([InlineKeyboardButton("🟢 ALLE GRUPPEN", callback_data="unban_all_groups")])
        for g in groups:
            keyboard.append([InlineKeyboardButton(g["title"], callback_data=f"unban_group_{g['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_banall")])
        await query.edit_message_text(
            "Wähle die Gruppe(n) für den Unban:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("ban_all_groups") or data.startswith("ban_group_"):
        if data == "ban_all_groups":
            groups = await get_bot_groups(context)
            user_data_store[user_id] = {"action": "ban", "groups": [g["id"] for g in groups]}
        else:
            gid = int(data.replace("ban_group_", ""))
            user_data_store[user_id] = {"action": "ban", "groups": [gid]}
        await query.edit_message_text("Sende mir jetzt die User-ID oder den @username zum Bannen:")
        context.user_data["state"] = WAITING_BAN_INPUT

    elif data.startswith("unban_all_groups") or data.startswith("unban_group_"):
        if data == "unban_all_groups":
            groups = await get_bot_groups(context)
            user_data_store[user_id] = {"action": "unban", "groups": [g["id"] for g in groups]}
        else:
            gid = int(data.replace("unban_group_", ""))
            user_data_store[user_id] = {"action": "unban", "groups": [gid]}
        await query.edit_message_text("Sende mir jetzt die User-ID oder den @username zum Entbannen:")
        context.user_data["state"] = WAITING_UNBAN_INPUT

    # === OPEN / CLOSE MENU ===
    elif data == "menu_openclose":
        await show_openclose_menu(query, context, user_id)

    elif data == "oc_set_open_sticker":
        user_data_store[user_id] = {"action": "set_open_sticker"}
        context.user_data["state"] = WAITING_OPEN_STICKER
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="menu_openclose")]]
        await query.edit_message_text(
            "🔓 Sende mir jetzt den **Open-Sticker**.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    elif data == "oc_set_close_sticker":
        user_data_store[user_id] = {"action": "set_close_sticker"}
        context.user_data["state"] = WAITING_CLOSE_STICKER
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="menu_openclose")]]
        await query.edit_message_text(
            "🔒 Sende mir jetzt den **Close-Sticker**.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    elif data == "oc_remove_open_sticker":
        bot_data = load_data()
        bot_data["open_close"]["open_sticker"] = None
        save_data(bot_data)
        await show_openclose_menu(query, context, user_id)

    elif data == "oc_remove_close_sticker":
        bot_data = load_data()
        bot_data["open_close"]["close_sticker"] = None
        save_data(bot_data)
        await show_openclose_menu(query, context, user_id)

    elif data == "oc_notify_groups":
        await show_openclose_group_selection(query, context, user_id)

    elif data.startswith("oc_grp_toggle_"):
        gid = int(data.replace("oc_grp_toggle_", ""))
        bot_data = load_data()
        notify = set(bot_data["open_close"].get("notify_groups", []))
        if gid in notify:
            notify.discard(gid)
        else:
            notify.add(gid)
        bot_data["open_close"]["notify_groups"] = list(notify)
        save_data(bot_data)
        await show_openclose_group_selection(query, context, user_id)

    elif data == "oc_grp_all":
        groups = await get_bot_groups(context)
        bot_data = load_data()
        bot_data["open_close"]["notify_groups"] = [g["id"] for g in groups]
        save_data(bot_data)
        await show_openclose_group_selection(query, context, user_id)

    elif data == "oc_grp_none":
        bot_data = load_data()
        bot_data["open_close"]["notify_groups"] = []
        save_data(bot_data)
        await show_openclose_group_selection(query, context, user_id)

    elif data == "oc_edit_open_text":
        user_data_store[user_id] = {"action": "oc_edit_open_text"}
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="menu_openclose")]]
        await query.edit_message_text(
            "✏️ Sende mir den neuen **Open-Text**.\n\n"
            "Nutze `{link}` als Platzhalter für den Gruppen-Link.\n"
            "Nutze `{name}` für den Gruppennamen.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        context.user_data["state"] = WAITING_MESSENGER_INPUT

    elif data == "oc_edit_close_text":
        user_data_store[user_id] = {"action": "oc_edit_close_text"}
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="menu_openclose")]]
        await query.edit_message_text(
            "✏️ Sende mir den neuen **Close-Text**.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        context.user_data["state"] = WAITING_MESSENGER_INPUT

    # === SETTINGS ===
    elif data == "menu_settings":
        if not is_owner(user_id):
            await query.edit_message_text("⛔ Nur für Owner.")
            return
        cfg = load_config()
        admins = cfg.get("admin_ids", [])
        log_ch = cfg.get("log_channel_id", "Nicht gesetzt")
        text = (
            f"⚙️ *Einstellungen*\n\n"
            f"👮 Admins: {len(admins)}\n"
            f"📋 Log-Kanal: `{log_ch}`"
        )
        keyboard = [
            [InlineKeyboardButton("➕ Admin hinzufügen", callback_data="add_admin"),
             InlineKeyboardButton("➖ Admin entfernen", callback_data="remove_admin")],
            [InlineKeyboardButton("📋 Log-Kanal setzen", callback_data="set_log")],
            [InlineKeyboardButton("👥 Gruppen anzeigen", callback_data="show_groups")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "add_admin":
        if not is_owner(user_id):
            await query.edit_message_text("⛔ Nur für Owner.")
            return
        await query.edit_message_text("Sende mir die User-ID des neuen Admins:")
        context.user_data["state"] = WAITING_ADMIN_ADD

    elif data == "remove_admin":
        if not is_owner(user_id):
            await query.edit_message_text("⛔ Nur für Owner.")
            return
        cfg = load_config()
        admins = cfg.get("admin_ids", [])
        text = "Aktuelle Admins:\n" + "\n".join(f"• `{a}`" for a in admins)
        text += "\n\nSende mir die User-ID zum Entfernen:"
        await query.edit_message_text(text, parse_mode="Markdown")
        context.user_data["state"] = WAITING_ADMIN_REMOVE

    elif data == "set_log":
        if not is_owner(user_id):
            await query.edit_message_text("⛔ Nur für Owner.")
            return
        await query.edit_message_text("Sende mir die Chat-ID des Log-Kanals\n(Bot muss dort Admin sein):")
        context.user_data["state"] = WAITING_LOG_CHANNEL

    elif data == "show_groups":
        if not is_owner(user_id):
            await query.edit_message_text("⛔ Nur für Owner.")
            return
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.\nNutze /registergroup in einer Gruppe.")
            return
        text = "👥 *Registrierte Gruppen:*\n\n"
        for g in groups:
            text += f"• {g['title']} (`{g['id']}`)\n"
        await query.edit_message_text(text, parse_mode="Markdown")

# --- Message handler for text input ---

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    state = context.user_data.get("state")
    text = update.message.text.strip()

    if state in (WAITING_BAN_INPUT, WAITING_UNBAN_INPUT):
        pending = user_data_store.get(user_id)
        if not pending:
            await update.message.reply_text("Bitte starte mit /start.")
            return

        # Resolve user
        target = text.lstrip("@")
        try:
            target_id = int(target)
        except ValueError:
            # It's a username, we can't resolve without knowing the user ID
            await update.message.reply_text(
                "⚠️ Bitte sende eine numerische User-ID.\n"
                "(Telegram erlaubt keinen Ban per @username ohne vorherige Interaktion)"
            )
            return

        action = pending["action"]
        groups = pending["groups"]
        tracked = lookup_user(str(target_id)) or lookup_user(target)
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None
        results = []

        for gid in groups:
            try:
                if action == "ban":
                    await context.bot.ban_chat_member(chat_id=gid, user_id=target_id, revoke_messages=True)
                    results.append(f"✅ Gebannt in `{gid}`")
                else:
                    await context.bot.unban_chat_member(chat_id=gid, user_id=target_id, only_if_banned=True)
                    results.append(f"✅ Entbannt in `{gid}`")
            except Exception as e:
                results.append(f"❌ Fehler in `{gid}`: {e}")

        if action == "ban":
            remember_group_ban(groups, target_id, target_name, target_username)
        else:
            forget_group_ban(groups, target_id)

        result_text = "\n".join(results)
        verb = "gebannt" if action == "ban" else "entbannt"
        await update.message.reply_text(f"Ergebnis für User `{target_id}`:\n\n{result_text}", parse_mode="Markdown")
        await log_action(context, f"User `{target_id}` {verb} von {update.effective_user.full_name} ({user_id})\n{result_text}")

        context.user_data["state"] = None
        del user_data_store[user_id]

    elif state == WAITING_ADMIN_ADD:
        try:
            new_admin = int(text)
            cfg = load_config()
            if new_admin not in cfg["admin_ids"]:
                cfg["admin_ids"].append(new_admin)
                save_config(cfg)
                await update.message.reply_text(f"✅ Admin `{new_admin}` hinzugefügt.", parse_mode="Markdown")
                await log_action(context, f"Admin hinzugefügt: {new_admin} von {user_id}")
            else:
                await update.message.reply_text("Ist bereits Admin.")
        except ValueError:
            await update.message.reply_text("⚠️ Bitte eine numerische User-ID senden.")
        context.user_data["state"] = None

    elif state == WAITING_ADMIN_REMOVE:
        try:
            rem_admin = int(text)
            cfg = load_config()
            if rem_admin in cfg["admin_ids"]:
                cfg["admin_ids"].remove(rem_admin)
                save_config(cfg)
                await update.message.reply_text(f"✅ Admin `{rem_admin}` entfernt.", parse_mode="Markdown")
                await log_action(context, f"Admin entfernt: {rem_admin} von {user_id}")
            else:
                await update.message.reply_text("Nicht in der Admin-Liste.")
        except ValueError:
            await update.message.reply_text("⚠️ Bitte eine numerische User-ID senden.")
        context.user_data["state"] = None

    elif state == WAITING_LOG_CHANNEL:
        try:
            channel_id = int(text)
            cfg = load_config()
            cfg["log_channel_id"] = channel_id
            save_config(cfg)
            await update.message.reply_text(f"✅ Log-Kanal auf `{channel_id}` gesetzt.", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("⚠️ Bitte eine numerische Chat-ID senden.")
        context.user_data["state"] = None

    elif state == WAITING_MESSENGER_INPUT:
        pending = user_data_store.get(user_id)
        if not pending:
            await update.message.reply_text("Bitte starte mit /start.")
            return

        # Handle open/close text editing
        if pending.get("action") == "oc_edit_open_text":
            bot_data = load_data()
            bot_data["open_close"]["open_text"] = update.message.text
            save_data(bot_data)
            context.user_data["state"] = None
            user_data_store.pop(user_id, None)
            keyboard = [[InlineKeyboardButton("🔙 Zurück zu Open/Close", callback_data="menu_openclose")]]
            await update.message.reply_text("✅ Open-Text aktualisiert!", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if pending.get("action") == "oc_edit_close_text":
            bot_data = load_data()
            bot_data["open_close"]["close_text"] = update.message.text
            save_data(bot_data)
            context.user_data["state"] = None
            user_data_store.pop(user_id, None)
            keyboard = [[InlineKeyboardButton("🔙 Zurück zu Open/Close", callback_data="menu_openclose")]]
            await update.message.reply_text("✅ Close-Text aktualisiert!", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        groups = pending["groups"]
        success = 0
        fail = 0
        import time, datetime
        broadcast_id = str(int(time.time() * 1000))
        sent_msgs = []
        for gid in groups:
            try:
                msg = await context.bot.send_message(
                    chat_id=gid,
                    text=update.message.text_html,
                    parse_mode="HTML",
                )
                sent_msgs.append((gid, msg.message_id))
                success += 1
            except Exception as e:
                fail += 1
                logger.error(f"Messenger send failed in {gid}: {e}")

        # Save broadcast persistently
        bot_data = load_data()
        bot_data.setdefault("broadcasts", {})[broadcast_id] = {
            "messages": sent_msgs,
            "date": now_de().strftime("%d.%m %H:%M"),
            "count": success,
            "preview": update.message.text[:50] if update.message.text else "...",
        }
        save_data(bot_data)

        keyboard = [[InlineKeyboardButton("🗑 Nachricht in allen Gruppen löschen", callback_data=f"del_broadcast_{broadcast_id}")]]
        await update.message.reply_text(
            f"📨 Nachricht gesendet!\n✅ {success} Gruppen erfolgreich"
            + (f"\n❌ {fail} Fehler" if fail else ""),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        await log_action(context, f"MESSENGER: {update.effective_user.full_name} ({user_id}) → {success} Gruppen\nText: {text[:100]}")
        context.user_data["state"] = None
        del user_data_store[user_id]

    elif state == WAITING_SCHEDULED_TEXT:
        pending = user_data_store.get(user_id)
        if not pending:
            await update.message.reply_text("Bitte starte mit /start.")
            return
        
        # Check if editing existing scheduled message
        if pending.get("action") == "sched_edit_text":
            sched_id = pending["sched_id"]
            bot_data = load_data()
            for s in bot_data.get("scheduled", []):
                if s["id"] == sched_id:
                    s["text"] = update.message.text
                    s["text_html"] = update.message.text_html
                    save_data(bot_data)
                    break
            keyboard = [[InlineKeyboardButton("🔙 Zurück zur Nachricht", callback_data=f"sched_view_{sched_id}")]]
            await update.message.reply_text(
                "✅ Nachricht aktualisiert.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            context.user_data["state"] = None
            user_data_store.pop(user_id, None)
        else:
            # New scheduled message flow - save text, then show hour picker
            pending["text"] = update.message.text
            pending["text_html"] = update.message.text_html
            pending["action"] = "sched_set_time"
            user_data_store[user_id] = pending
            # Build hour picker as inline reply
            keyboard = []
            for row_start in range(0, 24, 5):
                row = []
                for h in range(row_start, min(row_start + 5, 24)):
                    row.append(InlineKeyboardButton(str(h), callback_data=f"sched_hour_{h}"))
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_scheduled")])
            await update.message.reply_text(
                "🕐 *Wiederholte Mitteilungen*\n\n👉 Wähle die Startzeit.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            context.user_data["state"] = None

    elif state == WAITING_SCHED_STARTDATE:
        pending = user_data_store.get(user_id)
        if not pending:
            await update.message.reply_text("Bitte starte mit /start.")
            return
        sched_id = pending["sched_id"]
        try:
            dt = datetime.datetime.strptime(update.message.text.strip(), "%d/%m/%y %H:%M")
            bot_data = load_data()
            for s in bot_data.get("scheduled", []):
                if s["id"] == sched_id:
                    s["start_date"] = dt.strftime("%d.%m.%Y %H:%M")
                    save_data(bot_data)
                    break
            keyboard = [[InlineKeyboardButton("🔙 Zurück zur Nachricht", callback_data=f"sched_view_{sched_id}")]]
            await update.message.reply_text(
                f"✅ Anfangsdatum gesetzt: {dt.strftime('%d.%m.%Y %H:%M')}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except ValueError:
            await update.message.reply_text("⚠️ Falsches Format! Bitte tt/mm/jj hh:mm senden.\nBeispiel: 24/03/26 03:54")
            return
        context.user_data["state"] = None
        user_data_store.pop(user_id, None)

    elif state == WAITING_SCHED_ENDDATE:
        pending = user_data_store.get(user_id)
        if not pending:
            await update.message.reply_text("Bitte starte mit /start.")
            return
        sched_id = pending["sched_id"]
        try:
            dt = datetime.datetime.strptime(update.message.text.strip(), "%d/%m/%y %H:%M")
            bot_data = load_data()
            for s in bot_data.get("scheduled", []):
                if s["id"] == sched_id:
                    s["end_date"] = dt.strftime("%d.%m.%Y %H:%M")
                    save_data(bot_data)
                    break
            keyboard = [[InlineKeyboardButton("🔙 Zurück zur Nachricht", callback_data=f"sched_view_{sched_id}")]]
            await update.message.reply_text(
                f"✅ Enddatum gesetzt: {dt.strftime('%d.%m.%Y %H:%M')}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except ValueError:
            await update.message.reply_text("⚠️ Falsches Format! Bitte tt/mm/jj hh:mm senden.\nBeispiel: 24/03/26 03:54")
            return
        context.user_data["state"] = None
        user_data_store.pop(user_id, None)

# --- /registergroup - run in a group to add it ---

async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Nur Owner können Gruppen registrieren.")
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Dieser Befehl funktioniert nur in Gruppen.")
        return

    # Check if bot is admin in this group
    try:
        bot_member = await context.bot.get_chat_member(chat.id, (await context.bot.get_me()).id)
        if bot_member.status not in ("administrator", "creator"):
            await update.message.reply_text(
                "⚠️ Der Bot muss zuerst als *Admin* in dieser Gruppe hinzugefügt werden, "
                "bevor die Gruppe registriert werden kann.",
                parse_mode="Markdown",
            )
            return
    except Exception as e:
        await update.message.reply_text(f"⚠️ Konnte Admin-Status nicht prüfen: {e}")
        return

    data = load_data()
    groups = data.get("groups", [])
    
    if any(g["id"] == chat.id for g in groups):
        await update.message.reply_text(f"✅ Gruppe bereits registriert: {chat.title}")
        return

    groups.append({"id": chat.id, "title": chat.title})
    data["groups"] = groups
    save_data(data)
    await update.message.reply_text(f"✅ Gruppe registriert: *{chat.title}*", parse_mode="Markdown")
    await log_action(context, f"Gruppe registriert: {chat.title} ({chat.id})")

# --- /unregistergroup ---

async def unregister_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Dieser Befehl funktioniert nur in Gruppen.")
        return

    data = load_data()
    groups = data.get("groups", [])
    data["groups"] = [g for g in groups if g["id"] != chat.id]
    save_data(data)
    await update.message.reply_text(f"✅ Gruppe entfernt: *{chat.title}*", parse_mode="Markdown")

# --- Helper: resolve target user from reply or argument ---

async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve target user from reply, mention entity, tracked username, or numeric ID."""
    # Option 1: Reply to a message
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        user = update.message.reply_to_message.from_user
        track_user(user)
        return user.id, user.full_name

    # Option 2: Check for mention entities (text_mention has user object)
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "text_mention" and entity.user:
                track_user(entity.user)
                return entity.user.id, entity.user.full_name or str(entity.user.id)
            if entity.type == "mention":
                username = update.message.text[entity.offset + 1:entity.offset + entity.length]
                if username == (await context.bot.get_me()).username:
                    continue
                # Look up in our tracked users database
                tracked = lookup_user(username)
                if tracked:
                    return tracked["id"], tracked.get("name", username)
                else:
                    await update.message.reply_text(
                        f"⚠️ `@{username}` ist dem Bot noch nicht bekannt.\n"
                        "Der User muss erst eine Nachricht in einer Gruppe schreiben, "
                        "damit der Bot ihn tracken kann.\n\n"
                        "💡 Alternative: Antworte direkt auf eine Nachricht des Users.",
                        parse_mode="Markdown",
                    )
                    return None, None

    # Option 3: Argument after command (numeric ID or @username)
    if context.args and len(context.args) > 0:
        arg = context.args[0].lstrip("@")
        try:
            target_id = int(arg)
            tracked = lookup_user(arg)
            name = tracked["name"] if tracked else str(target_id)
            return target_id, name
        except ValueError:
            # Try lookup by username
            tracked = lookup_user(arg)
            if tracked:
                return tracked["id"], tracked.get("name", arg)
            await update.message.reply_text(
                f"⚠️ `@{arg}` ist dem Bot noch nicht bekannt.\n"
                "Der User muss erst eine Nachricht in einer Gruppe schreiben.",
                parse_mode="Markdown",
            )
            return None, None

    await update.message.reply_text(
        "⚠️ *Nutzung:*\n"
        "• Antworte auf eine Nachricht des Users\n"
        "• Markiere einen User: `/banall @User`\n"
        "• Oder: `/banall 123456789` (User-ID)",
        parse_mode="Markdown",
    )
    return None, None

# --- /banall ---

async def banall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    groups = await get_bot_groups(context)
    if not groups:
        await update.message.reply_text("Keine Gruppen registriert.")
        return

    tracked = lookup_user(str(target_id))
    target_username = tracked.get("username") if tracked else None
    success_count = 0
    fail_count = 0
    for g in groups:
        try:
            await context.bot.ban_chat_member(chat_id=g["id"], user_id=target_id, revoke_messages=True)
            success_count += 1
        except Exception as e:
            fail_count += 1
            logger.error(f"Ban failed for {target_id} in {g['id']}: {e}")

    remember_group_ban([g["id"] for g in groups], target_id, target_name, target_username)

    await update.message.reply_text(
        f"`{target_id}` wurde erfolgreich gebannt✅",
        parse_mode="Markdown",
    )
    await log_action(
        context,
        f"BANALL: {target_name} ({target_id}) von {update.effective_user.full_name} — {success_count} OK, {fail_count} Fehler",
    )

# --- /unbanall ---

async def unbanall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    groups = await get_bot_groups(context)
    if not groups:
        await update.message.reply_text("Keine Gruppen registriert.")
        return

    results = []
    for g in groups:
        try:
            await context.bot.unban_chat_member(chat_id=g["id"], user_id=target_id, only_if_banned=True)
            results.append(f"✅ {g['title']}")
        except Exception as e:
            results.append(f"❌ {g['title']}: {e}")

    forget_group_ban([g["id"] for g in groups], target_id)

    result_text = "\n".join(results)
    await update.message.reply_text(
        f"✅ *{target_name}* (`{target_id}`) entbannt:\n\n{result_text}",
        parse_mode="Markdown",
    )
    await log_action(
        context,
        f"UNBANALL: {target_name} ({target_id}) von {update.effective_user.full_name}\n{result_text}",
    )

# --- Delete service messages (pinned, joined, left, etc.) ---

async def delete_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically delete service/system messages in groups."""
    if update.message:
        try:
            await update.message.delete()
            logger.info(f"Deleted service message in {update.effective_chat.title} ({update.effective_chat.id})")
        except Exception as e:
            logger.error(f"Could not delete service message in {update.effective_chat.id}: {e}")

# --- User tracker + auto re-ban ---

async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track group activity, remove ban-related service messages, and immediately re-ban if needed."""
    if not update.message:
        return

    if update.message.from_user:
        track_user(update.message.from_user)

    if update.message.left_chat_member:
        left_member = update.message.left_chat_member
        if is_banned_in_group(update.effective_chat.id, left_member.id):
            try:
                await update.message.delete()
            except Exception as e:
                logger.error(f"Could not delete ban service message for {left_member.id} in {update.effective_chat.id}: {e}")

    for member in update.message.new_chat_members or []:
        track_user(member)
        if is_banned_in_group(update.effective_chat.id, member.id):
            try:
                await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=member.id, revoke_messages=True)
                try:
                    await update.message.delete()
                except Exception:
                    pass
                await log_action(context, f"AUTO-REBANNED: {member.full_name} ({member.id}) in {update.effective_chat.title}")
            except Exception as e:
                logger.error(f"Auto-reban via new_chat_members failed for {member.id} in {update.effective_chat.id}: {e}")

async def enforce_ban_on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_member:
        return

    member = update.chat_member.new_chat_member.user
    if not member or member.is_bot:
        return

    track_user(member)
    if is_banned_in_group(update.effective_chat.id, member.id):
        try:
            await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=member.id, revoke_messages=True)
            await log_action(context, f"AUTO-REBANNED: {member.full_name} ({member.id}) in {update.effective_chat.title}")
        except Exception as e:
            logger.error(f"Auto-reban via chat_member failed for {member.id} in {update.effective_chat.id}: {e}")

async def block_banned_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_join_request:
        return

    request = update.chat_join_request
    member = request.from_user
    if not member or member.is_bot:
        return

    track_user(member)
    if is_banned_in_group(request.chat.id, member.id):
        try:
            await context.bot.decline_chat_join_request(chat_id=request.chat.id, user_id=member.id)
            await log_action(context, f"JOIN-REQUEST ABGELEHNT: {member.full_name} ({member.id}) in {request.chat.title}")
        except Exception as e:
            logger.error(f"Decline join request failed for {member.id} in {request.chat.id}: {e}")

# --- Media handler (photos, videos, stickers for scheduled messages + JSON import) ---

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle media uploads for scheduled messages, sticker setting, or JSON file imports."""
    user_id = update.effective_user.id
    state = context.user_data.get("state")
    
    # If waiting for open/close sticker
    if state in (WAITING_OPEN_STICKER, WAITING_CLOSE_STICKER):
        msg = update.message
        if not msg.sticker:
            await msg.reply_text("⚠️ Bitte sende einen Sticker.")
            return
        bot_data = load_data()
        key = "open_sticker" if state == WAITING_OPEN_STICKER else "close_sticker"
        label = "Open" if state == WAITING_OPEN_STICKER else "Close"
        bot_data["open_close"][key] = msg.sticker.file_id
        save_data(bot_data)
        context.user_data["state"] = None
        user_data_store.pop(user_id, None)
        keyboard = [[InlineKeyboardButton("🔙 Zurück zu Open/Close", callback_data="menu_openclose")]]
        await msg.reply_text(
            f"✅ {label}-Sticker gespeichert!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # If waiting for scheduled media
    if state == WAITING_SCHEDULED_MEDIA:
        pending = user_data_store.get(user_id)
        if not pending or pending.get("action") != "sched_set_media":
            return
        sched_id = pending["sched_id"]
        
        # Determine media type and file_id
        media_file_id = None
        media_type = None
        msg = update.message
        
        if msg.photo:
            media_file_id = msg.photo[-1].file_id  # highest resolution
            media_type = "photo"
        elif msg.video:
            media_file_id = msg.video.file_id
            media_type = "video"
        elif msg.animation:
            media_file_id = msg.animation.file_id
            media_type = "animation"
        elif msg.sticker:
            media_file_id = msg.sticker.file_id
            media_type = "sticker"
        elif msg.document:
            # Check if it's a JSON import or a media document
            if msg.document.file_name and msg.document.file_name.endswith(".json"):
                # Redirect to document_handler for JSON import
                context.user_data["state"] = None
                user_data_store.pop(user_id, None)
                return await document_handler(update, context)
            media_file_id = msg.document.file_id
            media_type = "document"
        
        if not media_file_id:
            await msg.reply_text("⚠️ Konnte kein Medium erkennen. Bitte sende ein Foto, Video oder Sticker.")
            return
        
        # Save to scheduled message
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                s["media_file_id"] = media_file_id
                s["media_type"] = media_type
                save_data(bot_data)
                break
        
        context.user_data["state"] = None
        user_data_store.pop(user_id, None)
        
        keyboard = [[InlineKeyboardButton("🔙 Zurück zur Nachricht", callback_data=f"sched_edit_text_{sched_id}")]]
        await msg.reply_text(
            f"✅ Medium ({media_type}) gespeichert!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    
    # If it's a document and not in media state, try JSON import
    if update.message.document:
        return await document_handler(update, context)


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Import groups from a JSON file sent in private chat."""
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Nur Owner können Gruppen importieren.")
        return

    doc = update.message.document
    if not doc.file_name.endswith(".json"):
        await update.message.reply_text("⚠️ Bitte sende eine `.json` Datei.", parse_mode="Markdown")
        return

    try:
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()
        groups_dict = json.loads(file_bytes.decode("utf-8"))
    except Exception as e:
        await update.message.reply_text(f"⚠️ Fehler beim Lesen der Datei: {e}")
        return

    if not isinstance(groups_dict, dict):
        await update.message.reply_text(
            "⚠️ Format muss ein JSON-Objekt sein:\n"
            '`{"Gruppenname": -100xxx, ...}`',
            parse_mode="Markdown",
        )
        return

    data = load_data()
    existing_ids = {g["id"] for g in data.get("groups", [])}
    added = 0
    skipped = 0
    not_admin = 0

    for name, gid in groups_dict.items():
        try:
            gid = int(gid)
        except (ValueError, TypeError):
            skipped += 1
            continue

        if gid in existing_ids:
            skipped += 1
            continue

        # Check if bot is admin
        try:
            bot_me = await context.bot.get_me()
            bot_member = await context.bot.get_chat_member(gid, bot_me.id)
            if bot_member.status not in ("administrator", "creator"):
                not_admin += 1
                continue
        except Exception:
            not_admin += 1
            continue

        data.setdefault("groups", []).append({"id": gid, "title": name})
        existing_ids.add(gid)
        added += 1

    save_data(data)
    text = f"✅ {added} Gruppen importiert"
    if skipped:
        text += f"\n⏩ {skipped} übersprungen (bereits vorhanden/ungültig)"
    if not_admin:
        text += f"\n⚠️ {not_admin} übersprungen (Bot nicht Admin)"
    await update.message.reply_text(text)
    await log_action(context, f"GRUPPEN-IMPORT: {added} Gruppen von {update.effective_user.full_name} ({user_id})")

# --- Open / Close helpers ---

async def show_openclose_menu(query, context, user_id):
    """Show Open/Close configuration menu."""
    bot_data = load_data()
    oc = bot_data.get("open_close", {})
    
    has_open_sticker = "✅" if oc.get("open_sticker") else "❌"
    has_close_sticker = "✅" if oc.get("close_sticker") else "❌"
    notify_count = len(oc.get("notify_groups", []))
    
    # Check which groups are currently open
    active = oc.get("active_open_messages", {})
    all_groups = await get_bot_groups(context)
    open_groups = [g["title"] for g in all_groups if str(g["id"]) in active]
    open_str = ", ".join(open_groups) if open_groups else "Keine"
    
    text = (
        f"🔓 <b>Open / Close</b>\n\n"
        f"🎨 Open-Sticker: {has_open_sticker}\n"
        f"🎨 Close-Sticker: {has_close_sticker}\n"
        f"📢 Benachrichtigungs-Gruppen: {notify_count}\n"
        f"🟢 Aktuell geöffnet: {open_str}\n\n"
        f"<i>Nutze /open in einer Gruppe zum Öffnen.\n"
        f"Nutze /close zum Schließen – die Open-Nachrichten werden automatisch gelöscht.</i>"
    )
    
    keyboard = [
        [InlineKeyboardButton(f"🎨 Open-Sticker {has_open_sticker}", callback_data="oc_set_open_sticker")],
        [InlineKeyboardButton(f"🎨 Close-Sticker {has_close_sticker}", callback_data="oc_set_close_sticker")],
    ]
    if oc.get("open_sticker"):
        keyboard.append([InlineKeyboardButton("🚫 Open-Sticker entfernen", callback_data="oc_remove_open_sticker")])
    if oc.get("close_sticker"):
        keyboard.append([InlineKeyboardButton("🚫 Close-Sticker entfernen", callback_data="oc_remove_close_sticker")])
    keyboard.append([InlineKeyboardButton(f"📢 Benachrichtigungs-Gruppen ({notify_count})", callback_data="oc_notify_groups")])
    keyboard.append([InlineKeyboardButton("✏️ Open-Text ändern", callback_data="oc_edit_open_text")])
    keyboard.append([InlineKeyboardButton("✏️ Close-Text ändern", callback_data="oc_edit_close_text")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_openclose_group_selection(query, context, user_id):
    """Show group selection for open/close notifications."""
    bot_data = load_data()
    notify = set(bot_data.get("open_close", {}).get("notify_groups", []))
    all_groups = await get_bot_groups(context)
    
    keyboard = []
    row = []
    for g in all_groups:
        check = "✅" if g["id"] in notify else "⬜"
        row.append(InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"oc_grp_toggle_{g['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data="oc_grp_all"),
        InlineKeyboardButton("◻️ Keine", callback_data="oc_grp_none"),
    ])
    keyboard.append([InlineKeyboardButton(f"🔙 Zurück ({len(notify)} gewählt)", callback_data="menu_openclose")])
    
    await query.edit_message_text(
        "📢 <b>Benachrichtigungs-Gruppen</b>\n\n"
        "Wähle die Gruppen, die bei /open benachrichtigt werden sollen:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def handle_open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /open command in a group."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return
    
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Dieser Befehl funktioniert nur in Gruppen.")
        return
    
    bot_data = load_data()
    oc = bot_data.get("open_close", {})
    notify_groups = oc.get("notify_groups", [])
    
    if not notify_groups:
        await update.message.reply_text("⚠️ Keine Benachrichtigungs-Gruppen konfiguriert. Richte sie im Bot-Menü ein.")
        return
    
    # Get invite link for this group
    try:
        invite_link = await context.bot.export_chat_invite_link(chat.id)
    except Exception:
        invite_link = None
    
    # Build open text
    open_text = oc.get("open_text", "Hey Freunde, wir haben geöffnet! 🎉\nKommt rein und gönnt euch!")
    open_text = open_text.replace("{name}", chat.title or "")
    if invite_link:
        open_text = open_text.replace("{link}", invite_link)
        if "{link}" not in oc.get("open_text", ""):
            open_text += f"\n\n👉 {invite_link}"
    
    open_sticker = oc.get("open_sticker")
    
    # Send to source group first (sticker + confirmation)
    if open_sticker:
        try:
            await context.bot.send_sticker(chat_id=chat.id, sticker=open_sticker)
        except Exception as e:
            logger.error(f"Open sticker failed in source group: {e}")
    
    # Send notifications to all notify groups
    sent_messages = []
    for gid in notify_groups:
        if gid == chat.id:
            continue  # Don't notify the source group
        try:
            msgs = []
            if open_sticker:
                sticker_msg = await context.bot.send_sticker(chat_id=gid, sticker=open_sticker)
                msgs.append(sticker_msg.message_id)
            text_msg = await context.bot.send_message(chat_id=gid, text=open_text, parse_mode="HTML", disable_web_page_preview=False)
            msgs.append(text_msg.message_id)
            sent_messages.append({"group_id": gid, "message_ids": msgs})
        except Exception as e:
            logger.error(f"Open notification failed in {gid}: {e}")
    
    # Save active open messages so /close can delete them
    active = oc.get("active_open_messages", {})
    active[str(chat.id)] = {
        "source_group": chat.id,
        "source_title": chat.title,
        "sent_messages": sent_messages,
        "opened_at": now_de().strftime("%d.%m.%Y %H:%M"),
    }
    oc["active_open_messages"] = active
    bot_data["open_close"] = oc
    save_data(bot_data)
    
    await update.message.reply_text(
        f"🔓 *{chat.title}* ist jetzt OPEN!\n📢 {len(sent_messages)} Gruppen benachrichtigt.",
        parse_mode="Markdown",
    )
    await log_action(context, f"OPEN: {chat.title} von {update.effective_user.full_name} → {len(sent_messages)} Gruppen benachrichtigt")


async def handle_close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close command in a group - deletes the open notifications."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return
    
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Dieser Befehl funktioniert nur in Gruppen.")
        return
    
    bot_data = load_data()
    oc = bot_data.get("open_close", {})
    active = oc.get("active_open_messages", {})
    
    open_info = active.pop(str(chat.id), None)
    
    close_sticker = oc.get("close_sticker")
    close_text = oc.get("close_text", "Wir haben geschlossen. Bis zum nächsten Mal! 👋")
    close_text = close_text.replace("{name}", chat.title or "")
    
    # Send close sticker in source group
    if close_sticker:
        try:
            await context.bot.send_sticker(chat_id=chat.id, sticker=close_sticker)
        except Exception as e:
            logger.error(f"Close sticker failed: {e}")
    
    # Delete all open notification messages from other groups
    deleted_count = 0
    if open_info:
        for entry in open_info.get("sent_messages", []):
            gid = entry["group_id"]
            for mid in entry["message_ids"]:
                try:
                    await context.bot.delete_message(chat_id=gid, message_id=mid)
                    deleted_count += 1
                except Exception as e:
                    logger.error(f"Delete open msg failed in {gid}: {e}")
    
    # Save updated state
    oc["active_open_messages"] = active
    bot_data["open_close"] = oc
    save_data(bot_data)
    
    await update.message.reply_text(
        f"🔒 *{chat.title}* ist jetzt CLOSED!\n🗑 {deleted_count} Open-Nachrichten gelöscht.",
        parse_mode="Markdown",
    )
    await log_action(context, f"CLOSE: {chat.title} von {update.effective_user.full_name} → {deleted_count} Nachrichten gelöscht")


# --- Main ---

def ensure_single_instance():
    # Kill ALL other bot.py processes (not just lock file PID)
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["grep", "-rl", "bot.py", "/proc"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        pass

    # Simple approach: read /proc/*/cmdline and kill matching PIDs
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == my_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", errors="ignore")
            if "bot.py" in cmdline and "python" in cmdline:
                os.kill(pid, signal.SIGKILL)
                logger.info(f"Killed old bot process {pid}")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    def cleanup_lock():
        try:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except OSError:
            pass

    atexit.register(cleanup_lock)


# --- Scheduled messages helper functions ---

async def show_scheduled_list(query, context, user_id, page=0):
    """Show list of all scheduled messages - layout like the Worldskandi bot screenshot."""
    bot_data = load_data()
    scheduled = bot_data.get("scheduled", [])
    
    now = now_de().strftime("%d.%m.%Y, %H:%M")
    
    text = (
        "🕐 <b>Wiederholte Mitteilungen</b>\n"
        "In diesem Menü kann man Nachrichten erstellen, die nach einer festgelegten "
        "Zeitspanne (Minuten/Stunden) oder nach einer festgelegten Anzahl von Nachrichten "
        "in der Gruppe wiederholt versendet werden.\n\n"
        f"<b>Aktuelle Zeit:</b> {now}\n"
    )
    
    for i, s in enumerate(scheduled, 1):
        status = "Aktiv ✅" if s.get("active") else "Pausiert ⏸"
        preview = s.get("text", "")[:20]
        time_str = s.get("time", "?")
        interval = s.get("interval_label", "?")
        emoji = "🟢" if s.get("active") else "🔴"
        
        text += (
            f"\n💬{emoji} <b>{i}</b> · <b>{status}</b>\n"
            f"  ├ <i>Zeit: {time_str}</i>\n"
            f"  ├ <i>{interval}</i>\n"
            f"  └ {preview}..\n"
        )
    
    if not scheduled:
        text += "\nKeine wiederholten Nachrichten eingerichtet."
    
    # Buttons
    keyboard = []
    keyboard.append([InlineKeyboardButton("➕ Nachricht hinzufügen", callback_data="sched_new")])
    
    # 3-column grid per message: [🔥 number] [✅ Aktiv] [🗑]
    PER_PAGE = 5
    total_pages = max(1, (len(scheduled) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages - 1)
    start = page * PER_PAGE
    end = min(start + PER_PAGE, len(scheduled))
    
    for i in range(start, end):
        s = scheduled[i]
        num = i + 1
        active_label = "✅ Aktiv" if s.get("active") else "⏸ Pause"
        keyboard.append([
            InlineKeyboardButton(f"💬 {num}", callback_data=f"sched_view_{s['id']}"),
            InlineKeyboardButton(active_label, callback_data=f"sched_toggle_active_list_{s['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"sched_delete_confirm_{s['id']}"),
        ])
    
    # Pagination
    if total_pages > 1:
        page_row = []
        for p in range(total_pages):
            label = f"·{p+1}·" if p == page else str(p+1)
            page_row.append(InlineKeyboardButton(label, callback_data=f"sched_page_{p}"))
        keyboard.append(page_row)
    
    keyboard.append([InlineKeyboardButton("↩️ Zurück", callback_data="back_main")])
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_sched_group_selection(query, context, user_id, groups):
    """Show group selection grid for scheduled messages."""
    selected = user_data_store.get(user_id, {}).get("selected", set())
    keyboard = []
    row = []
    for g in groups:
        check = "✅" if g["id"] in selected else "⬜"
        row.append(InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"sched_toggle_{g['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data="sched_select_all"),
        InlineKeyboardButton("◻️ Keine", callback_data="sched_select_none"),
    ])
    keyboard.append([InlineKeyboardButton(f"✅ Weiter ({len(selected)} gewählt)", callback_data="sched_confirm_groups")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_scheduled")])
    await query.edit_message_text(
        "🔁 *Wiederholte Nachricht*\nWähle die Gruppen aus:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )



async def show_sched_edit_groups(query, context, user_id, sched_id):
    """Show group selection for editing an existing scheduled message."""
    bot_data = load_data()
    sched = next((s for s in bot_data.get("scheduled", []) if s["id"] == sched_id), None)
    if not sched:
        await query.edit_message_text("⚠️ Nicht gefunden.")
        return
    selected = set(sched.get("groups", []))
    all_groups = await get_bot_groups(context)
    keyboard = []
    row = []
    for g in all_groups:
        check = "✅" if g["id"] in selected else "⬜"
        row.append(InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"sched_grp_toggle_{sched_id}_{g['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data=f"sched_grp_all_{sched_id}"),
        InlineKeyboardButton("◻️ Keine", callback_data=f"sched_grp_none_{sched_id}"),
    ])
    keyboard.append([InlineKeyboardButton(f"↩️ Zurück ({len(selected)} gewählt)", callback_data=f"sched_view_{sched_id}")])
    await query.edit_message_text(
        f"👥 <b>Gruppen ändern</b>\n\nWähle die Gruppen für diese wiederholte Nachricht:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_sched_content_menu(query, context, user_id, sched_id):
    """Show content editing menu: Text, Medien, Buttons - like Worldskandi screenshot."""
    bot_data = load_data()
    sched = None
    for s in bot_data.get("scheduled", []):
        if s["id"] == sched_id:
            sched = s
            break
    if not sched:
        await query.edit_message_text("⚠️ Nachricht nicht gefunden.")
        return
    
    has_text = "✅" if sched.get("text") else "❌"
    has_media = "✅" if sched.get("media_file_id") else "❌"
    
    text = (
        f"🕐 <b>Wiederholte Mitteilungen</b>\n\n"
        f"📄 Text {has_text}\n"
        f"🖼 Medien {has_media}\n\n"
        f"👉 Mit den Schaltflächen hier kannst Du auswählen, was Du einstellen willst."
    )
    
    keyboard = [
        [InlineKeyboardButton("📄 Text", callback_data=f"sched_set_text_{sched_id}"),
         InlineKeyboardButton("👀 Sehen", callback_data=f"sched_view_text_{sched_id}")],
        [InlineKeyboardButton("🖼 Medien", callback_data=f"sched_set_media_{sched_id}"),
         InlineKeyboardButton("👀 Sehen", callback_data=f"sched_view_media_{sched_id}")],
        [InlineKeyboardButton("👀 Vollständige Vorschau", callback_data=f"sched_preview_{sched_id}")],
        [InlineKeyboardButton("↩️ Zurück", callback_data=f"sched_view_{sched_id}")],
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_scheduled_detail(query, context, user_id, sched_id):
    """Show detail view matching Worldskandi screenshot exactly."""
    bot_data = load_data()
    sched = None
    for s in bot_data.get("scheduled", []):
        if s["id"] == sched_id:
            sched = s
            break
    if not sched:
        await query.edit_message_text("⚠️ Nachricht nicht gefunden.")
        return
    
    status = "Aktiv" if sched.get("active") else "Inaktiv"
    time_str = sched.get("time", "—")
    interval = sched.get("interval_label", "—")
    pin = "✅" if sched.get("pin_message") else "✖"
    del_prev = "✅" if sched.get("delete_previous") else "✖"
    
    # Resolve group names
    all_groups = await get_bot_groups(context)
    sched_group_ids = set(sched.get("groups", []))
    group_names = [g["title"] for g in all_groups if g["id"] in sched_group_ids]
    groups_str = ", ".join(group_names) if group_names else "Keine"
    
    text = (
        f"🕐 <b>Wiederholte Mitteilungen</b>\n\n"
        f"💡 <b>Status</b>: {status}\n"
        f"🕐 <b>Zeit</b>: {time_str}\n"
        f"🔁 <b>Wiederholung</b>: {interval}\n"
        f"👥 <b>Gruppen</b>: {groups_str}\n"
        f"📌 <b>Mitteilung anheften:</b>  {pin}\n"
        f"♻️ <b>Letzte Nachricht löschen:</b>  {del_prev}"
    )
    
    keyboard = [
        [InlineKeyboardButton("✍️ Nachricht anpassen", callback_data=f"sched_edit_text_{sched_id}")],
        [InlineKeyboardButton("👥 Gruppen ändern", callback_data=f"sched_edit_groups_{sched_id}")],
        [InlineKeyboardButton("🕐 Zeit", callback_data=f"sched_edit_time_{sched_id}"),
         InlineKeyboardButton("🔁 Wiederholung", callback_data=f"sched_edit_interval_{sched_id}")],
        [InlineKeyboardButton("📅 Wochentage", callback_data=f"sched_weekdays_{sched_id}")],
        [InlineKeyboardButton("📅 Tage des Monats 🆕", callback_data=f"sched_monthdays_{sched_id}")],
        [InlineKeyboardButton("⏱ Zeitspanne einstellen", callback_data=f"sched_timespan_{sched_id}")],
        [InlineKeyboardButton("🔈 Anfangsdatum", callback_data=f"sched_startdate_{sched_id}"),
         InlineKeyboardButton("🔈 Enddatum", callback_data=f"sched_enddate_{sched_id}")],
        [InlineKeyboardButton(f"📌 Mitteilung anheften  {pin}", callback_data=f"sched_pin_{sched_id}")],
        [InlineKeyboardButton(f"♻️ Letzte Nachricht löschen  {del_prev}", callback_data=f"sched_del_prev_{sched_id}")],
        [InlineKeyboardButton("♻️ Automatisch löschen 🆕", callback_data=f"sched_autodelete_{sched_id}")],
        [InlineKeyboardButton("🗑 Löschen", callback_data=f"sched_delete_confirm_{sched_id}")],
        [InlineKeyboardButton("↩️ Zurück", callback_data="menu_scheduled")],
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_weekdays_picker(query, context, sched_id):
    """Show weekday selection grid like screenshot."""
    bot_data = load_data()
    sched = next((s for s in bot_data.get("scheduled", []) if s["id"] == sched_id), None)
    if not sched:
        await query.edit_message_text("⚠️ Nicht gefunden.")
        return
    days = set(sched.get("weekdays", [0,1,2,3,4,5,6]))
    day_names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    keyboard = []
    for i in range(0, 6, 2):
        row = []
        for j in [i, i+1]:
            check = "✅" if j in days else "⬜"
            row.append(InlineKeyboardButton(f"{day_names[j]} {check}", callback_data=f"sched_weekdays_toggle_{sched_id}_{j}"))
        keyboard.append(row)
    check6 = "✅" if 6 in days else "⬜"
    keyboard.append([InlineKeyboardButton(f"Sonntag {check6}", callback_data=f"sched_weekdays_toggle_{sched_id}_6")])
    keyboard.append([InlineKeyboardButton("↩️ Zurück", callback_data=f"sched_view_{sched_id}")])
    await query.edit_message_text(
        "🕐 <b>Wiederholte Mitteilungen</b>\n\n"
        "👉 Wähle aus, an welchen Wochentagen die Nachricht wiederholt werden soll.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_monthdays_picker(query, context, sched_id):
    """Show month days 1-31 grid like screenshot."""
    bot_data = load_data()
    sched = next((s for s in bot_data.get("scheduled", []) if s["id"] == sched_id), None)
    if not sched:
        await query.edit_message_text("⚠️ Nicht gefunden.")
        return
    days = set(sched.get("monthdays", []))
    keyboard = []
    for row_start in range(1, 32, 4):
        row = []
        for d in range(row_start, min(row_start + 4, 32)):
            check = "✅" if d in days else ""
            label = f"{check}{d}" if check else str(d)
            row.append(InlineKeyboardButton(label, callback_data=f"sched_monthdays_toggle_{sched_id}_{d}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("↩️ Zurück", callback_data=f"sched_view_{sched_id}")])
    await query.edit_message_text(
        "🕐 <b>Wiederholte Mitteilungen</b>\n\n"
        "👉 Wähle, an welchen Tagen des Monats die Nachricht wiederholt werden soll.\n\n"
        "<i>Wählst Du keinen Tag aus, wird die Nachricht an jedem Tag des Monats erneut gesendet.</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_autodelete_picker(query, context, sched_id):
    """Show auto-delete time picker like screenshot."""
    bot_data = load_data()
    sched = next((s for s in bot_data.get("scheduled", []) if s["id"] == sched_id), None)
    if not sched:
        await query.edit_message_text("⚠️ Nicht gefunden.")
        return
    current = sched.get("autodelete_minutes")
    current_label = "Nicht löschen"
    if current:
        if current >= 60:
            current_label = f"{current // 60} Stunden"
        else:
            current_label = f"{current} Minuten"

    keyboard = []
    keyboard.append([InlineKeyboardButton("• Stunden •", callback_data="noop")])
    hours = [1,2,3,4,6,8,10,12,15,24,36,48]
    for i in range(0, len(hours), 4):
        row = [InlineKeyboardButton(str(h), callback_data=f"sched_autodelete_set_{sched_id}_{h*60}") for h in hours[i:i+4]]
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("• Minuten •", callback_data="noop")])
    mins = [1,2,3,4,5,10,15,20,30,40,45,50]
    for i in range(0, len(mins), 4):
        row = [InlineKeyboardButton(str(m), callback_data=f"sched_autodelete_set_{sched_id}_{m}") for m in mins[i:i+4]]
        keyboard.append(row)
    no_del_check = "✅" if not current else ""
    keyboard.append([InlineKeyboardButton(f"✖ Nicht löschen {no_del_check}", callback_data=f"sched_autodelete_off_{sched_id}")])
    keyboard.append([InlineKeyboardButton("↩️ Zurück", callback_data=f"sched_view_{sched_id}")])

    await query.edit_message_text(
        f"🕐 <b>Wiederholte Mitteilungen</b>\n\n"
        f"♻️ <b>Automatisch löschen</b>\n"
        f"  └ Zeitspanne: {current_label}\n\n"
        f"OK! Sende nun die Anzahl der Minuten, nach denen jede in der Gruppe gesendete Nachricht automatisch gelöscht werden soll.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_timespan_picker(query, context, sched_id):
    """Show timespan picker - same layout as interval picker."""
    bot_data = load_data()
    sched = next((s for s in bot_data.get("scheduled", []) if s["id"] == sched_id), None)
    if not sched:
        await query.edit_message_text("⚠️ Nicht gefunden.")
        return
    keyboard = []
    keyboard.append([InlineKeyboardButton("• Stunden •", callback_data="noop")])
    hours = [1,2,3,4,6,8,12,24]
    for i in range(0, len(hours), 4):
        row = [InlineKeyboardButton(str(h), callback_data=f"sched_ts_set_{sched_id}_{h*60}") for h in hours[i:i+4]]
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("• Minuten •", callback_data="noop")])
    mins = [1,2,3,5,10,15,20,30]
    for i in range(0, len(mins), 4):
        row = [InlineKeyboardButton(str(m), callback_data=f"sched_ts_set_{sched_id}_{m}") for m in mins[i:i+4]]
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("↩️ Zurück", callback_data=f"sched_view_{sched_id}")])
    await query.edit_message_text(
        "🕐 <b>Wiederholte Mitteilungen</b>\n\n"
        "⏱ <b>Zeitspanne einstellen</b>\n\n"
        "Wähle die Zeitspanne für die Nachrichtenwiederholung.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_hour_picker(query, context, user_id, back_callback="menu_scheduled"):
    """Show hour picker grid 0-23 in rows of 5 like screenshot."""
    pending = user_data_store.get(user_id, {})
    edit_sched_id = pending.get("sched_id")
    
    keyboard = []
    for row_start in range(0, 24, 5):
        row = []
        for h in range(row_start, min(row_start + 5, 24)):
            if edit_sched_id:
                cb = f"sched_edit_hour_{edit_sched_id}_{h}"
            else:
                cb = f"sched_hour_{h}"
            row.append(InlineKeyboardButton(str(h), callback_data=cb))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data=back_callback)])
    
    await query.edit_message_text(
        "🕐 *Wiederholte Mitteilungen*\n\n👉 Wähle die Startzeit.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def show_minute_picker(query, context, user_id, hour, back_callback="menu_scheduled", edit_sched_id=None):
    """Show minute picker 0-59 in rows of 6."""
    keyboard = []
    for row_start in range(0, 60, 6):
        row = []
        for m in range(row_start, min(row_start + 6, 60)):
            label = f"{m:02d}"
            if edit_sched_id:
                cb = f"sched_edit_min_{edit_sched_id}_{m}"
            else:
                cb = f"sched_minute_{m}"
            row.append(InlineKeyboardButton(label, callback_data=cb))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data=back_callback)])
    
    await query.edit_message_text(
        f"🕐 *Stunde: {hour}*\n\n👉 Wähle die Minute.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


def get_interval_label(minutes):
    labels = {
        1: "1 Min", 2: "2 Min", 3: "3 Min", 5: "5 Min",
        10: "10 Min", 15: "15 Min", 20: "20 Min", 30: "30 Min",
        60: "1 Stunde", 120: "2 Stunden", 180: "3 Stunden", 240: "4 Stunden",
        360: "6 Stunden", 480: "8 Stunden", 720: "12 Stunden", 1440: "24 Stunden",
    }
    return labels.get(minutes, f"{minutes} Min")


async def show_interval_picker(query, context, user_id, back_callback="menu_scheduled", edit_sched_id=None, current_minutes=None):
    """Show interval picker with Stunden + Minuten grid like screenshot."""
    prefix = f"sched_set_int_{edit_sched_id}_" if edit_sched_id else "sched_interval_"
    
    def btn(val, label=None):
        check = " ✅" if current_minutes == val else ""
        return InlineKeyboardButton(f"{label or val}{check}", callback_data=f"{prefix}{val}")
    
    keyboard = [
        [InlineKeyboardButton("· Stunden ·", callback_data="noop")],
        [btn(60, "1"), btn(120, "2"), btn(180, "3"), btn(240, "4")],
        [btn(360, "6"), btn(480, "8"), btn(720, "12"), btn(1440, "24")],
        [InlineKeyboardButton("· Minuten ·", callback_data="noop")],
        [btn(1, "1"), btn(2, "2"), btn(3, "3"), btn(5, "5")],
        [btn(10, "10"), btn(15, "15"), btn(20, "20"), btn(30, "30")],
        [InlineKeyboardButton("🔙 Zurück", callback_data=back_callback)],
    ]
    
    current_label = get_interval_label(current_minutes) if current_minutes else "—"
    await query.edit_message_text(
        f"🕐 *Wiederholte Mitteilungen*\n\n"
        f"🔁 *Wiederholung:* Alle {current_label}\n\n"
        f"👉 Wähle aus, wie oft die Nachricht wiederholt werden soll.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


# --- Scheduled job execution ---

async def execute_scheduled_message(context: ContextTypes.DEFAULT_TYPE):
    """Execute a scheduled message job."""
    try:
        job = context.job
        sched_id = job.data
        logger.info(f"Executing scheduled message {sched_id}")
        
        bot_data = load_data()
        sched = None
        for s in bot_data.get("scheduled", []):
            if s["id"] == sched_id:
                sched = s
                break
        
        if not sched or not sched.get("active"):
            logger.info(f"Scheduled message {sched_id} not found or inactive")
            return
        
        text_html = sched.get("text_html", sched.get("text", ""))
        media_fid = sched.get("media_file_id")
        
        if not text_html and not media_fid:
            logger.warning(f"Scheduled message {sched_id} has no text and no media, skipping")
            return
    
        
        # Delete previous messages if enabled
        if sched.get("delete_previous") and sched.get("last_sent_messages"):
            for entry in sched["last_sent_messages"]:
                try:
                    await context.bot.delete_message(chat_id=entry[0], message_id=entry[1])
                except Exception as e:
                    logger.error(f"Scheduled delete failed in {entry[0]}: {e}")
        
        # Send new messages
        sent_msgs = []
        text_html = sched.get("text_html", sched.get("text", ""))
        media_fid = sched.get("media_file_id")
        media_type = sched.get("media_type", "photo")
        
        for gid in sched.get("groups", []):
            try:
                if media_fid:
                    if media_type == "photo":
                        msg = await context.bot.send_photo(chat_id=gid, photo=media_fid, caption=text_html or None, parse_mode="HTML" if text_html else None)
                    elif media_type == "video":
                        msg = await context.bot.send_video(chat_id=gid, video=media_fid, caption=text_html or None, parse_mode="HTML" if text_html else None)
                    elif media_type == "animation":
                        msg = await context.bot.send_animation(chat_id=gid, animation=media_fid, caption=text_html or None, parse_mode="HTML" if text_html else None)
                    elif media_type == "sticker":
                        msg = await context.bot.send_sticker(chat_id=gid, sticker=media_fid)
                    else:
                        msg = await context.bot.send_document(chat_id=gid, document=media_fid, caption=text_html or None, parse_mode="HTML" if text_html else None)
                else:
                    msg = await context.bot.send_message(chat_id=gid, text=text_html, parse_mode="HTML")
                sent_msgs.append([gid, msg.message_id])
                if sched.get("pin_message"):
                    try:
                        await context.bot.pin_chat_message(chat_id=gid, message_id=msg.message_id, disable_notification=True)
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Scheduled send failed in {gid}: {e}")
        
        # Update last sent info
        sched["last_sent"] = now_de().strftime("%d.%m.%Y %H:%M")
        sched["last_sent_messages"] = sent_msgs
        save_data(bot_data)
        
        logger.info(f"Scheduled message {sched_id} sent to {len(sent_msgs)} groups")
    except Exception as e:
        logger.error(f"execute_scheduled_message error: {e}", exc_info=True)


def _get_job_queue(context):
    """Get job_queue from either Application or CallbackContext."""
    # Application object
    if hasattr(context, 'job_queue') and context.job_queue is not None:
        return context.job_queue
    # CallbackContext
    if hasattr(context, 'application') and context.application.job_queue is not None:
        return context.application.job_queue
    return None


def schedule_job(context, sched):
    """Schedule a repeating job for a scheduled message."""
    
    jq = _get_job_queue(context)
    if not jq:
        logger.error("No job_queue available for scheduling")
        return
    
    sched_id = sched["id"]
    interval = sched.get("interval_minutes", 60) * 60  # convert to seconds
    
    # Remove existing job if any
    remove_scheduled_job(context, sched_id)
    
    # Calculate first run time
    now = now_de()
    time_str = sched.get("time", "")
    
    if time_str and time_str != "00:00":
        # User set a specific start time
        try:
            h, m = map(int, time_str.split(":"))
        except Exception:
            h, m = 0, 0
        first_run = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if first_run <= now:
            first_run += datetime.timedelta(minutes=sched.get("interval_minutes", 60))
        delay = max(0, (first_run - now).total_seconds())
    else:
        # No time set – start immediately, only interval matters
        delay = 0
        logger.info(f"No start time set for {sched_id}, starting immediately")
    
    jq.run_repeating(
        execute_scheduled_message,
        interval=interval,
        first=delay,
        data=sched_id,
        name=f"sched_{sched_id}",
    )
    logger.info(f"Scheduled job {sched_id} set: interval={interval}s, first in {delay:.0f}s")


def remove_scheduled_job(context, sched_id):
    """Remove a scheduled job."""
    jq = _get_job_queue(context)
    if not jq:
        return
    jobs = jq.get_jobs_by_name(f"sched_{sched_id}")
    for job in jobs:
        job.schedule_removal()


async def post_init(application):
    """Called after the application is initialized. Restore scheduled jobs."""
    bot_data = load_data()
    count = 0
    for sched in bot_data.get("scheduled", []):
        if sched.get("active"):
            schedule_job(application, sched)
            count += 1
    logger.info(f"Restored {count} scheduled jobs")


def main():
    cfg = load_config()
    token = cfg.get("bot_token", "")
    if not token or token == "DEIN_BOT_TOKEN_HIER":
        print("❌ Bitte trage deinen Bot-Token in config.json ein!")
        return

    ensure_single_instance()

    # Simple webhook clear before polling starts
    try:
        _requests.post(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=10,
        )
    except Exception:
        pass

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("registergroup", register_group))
    app.add_handler(CommandHandler("unregistergroup", unregister_group))
    app.add_handler(CommandHandler("banall", banall))
    app.add_handler(CommandHandler("unbanall", unbanall))
    app.add_handler(CommandHandler("open", handle_open_command))
    app.add_handler(CommandHandler("close", handle_close_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_handler))
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO | filters.Sticker.ALL | filters.ANIMATION | filters.Document.ALL) & filters.ChatType.PRIVATE, media_handler))
    app.add_handler(MessageHandler(filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), track_message), group=1)
    app.add_handler(ChatMemberHandler(enforce_ban_on_chat_member, ChatMemberHandler.CHAT_MEMBER), group=2)
    app.add_handler(ChatJoinRequestHandler(block_banned_join_request), group=3)

    # Delete ALL service messages (pinned, joined, left, title change, etc.)
    service_filter = (
        filters.StatusUpdate.PINNED_MESSAGE
        | filters.StatusUpdate.NEW_CHAT_MEMBERS
        | filters.StatusUpdate.LEFT_CHAT_MEMBER
        | filters.StatusUpdate.NEW_CHAT_TITLE
        | filters.StatusUpdate.NEW_CHAT_PHOTO
        | filters.StatusUpdate.DELETE_CHAT_PHOTO
        | filters.StatusUpdate.MIGRATE
    )
    app.add_handler(MessageHandler(
        service_filter & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        delete_service_message
    ), group=4)

    # Restore scheduled jobs
    if app.job_queue:
        bot_data = load_data()
        count = 0
        for sched in bot_data.get("scheduled", []):
            if sched.get("active"):
                schedule_job(app, sched)
                count += 1
        logger.info(f"Restored {count} scheduled jobs")

    print("🤖 Bot gestartet!")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
