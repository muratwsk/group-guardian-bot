import atexit
import datetime
import html
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
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
    data.setdefault("personal_commands", {})
    data.setdefault("warnings", {})
    data.setdefault("warn_config", {
        "max_warns": 3,
        "punishment": "mute",
    })
    data.setdefault("open_close", {
        "open_sticker": None,
        "close_sticker": None,
        "notify_groups": [],
        "open_text": "Hey Freunde, wir haben geöffnet! 🎉\nKommt rein und gönnt euch!",
        "close_text": "Wir haben geschlossen. Bis zum nächsten Mal! 👋",
        "active_open_messages": {},
    })
    data.setdefault("cmd_delete", {
        "admin_prefixes": [],
        "user_prefixes": [],
    })
    data.setdefault("antispam_links", {
        "punishment": "aus",
        "delete": True,
    })
    data.setdefault("antispam_forward", {
        "channels": False,
        "groups": False,
        "users": False,
        "bots": False,
    })
    data.setdefault("freed_users", [])
    return data

def is_freed(user_id: int) -> bool:
    """Check if a user has the 'Befreiter' role (exempt from all restrictions)."""
    bot_data = load_data()
    return user_id in bot_data.get("freed_users", [])

def _safe_load_json(filepath, default):
    """Load JSON with automatic backup recovery if file is empty/corrupt."""
    bak = filepath + ".bak"
    if not os.path.exists(filepath):
        # Try backup
        if os.path.exists(bak):
            logger.warning(f"{filepath} missing, restoring from backup")
            try:
                with open(bak, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return default
    try:
        with open(filepath, "r") as f:
            content = f.read()
        if not content.strip():
            raise ValueError("File is empty")
        result = json.loads(content)
        return result
    except Exception as e:
        logger.error(f"{filepath} corrupt ({e}), trying backup...")
        if os.path.exists(bak):
            try:
                with open(bak, "r") as f:
                    result = json.load(f)
                logger.info(f"Restored {filepath} from backup successfully!")
                # Repair the main file
                _safe_save_json(filepath, result)
                return result
            except Exception as e2:
                logger.error(f"Backup {bak} also corrupt: {e2}")
        logger.error(f"No valid backup for {filepath}, using default")
        return default

def _safe_save_json(filepath, data):
    """Atomic save: write to temp file, then rename. Also keeps .bak."""
    bak = filepath + ".bak"
    tmp = filepath + ".tmp"
    try:
        # Write to temp file first
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # Backup current file before replacing
        if os.path.exists(filepath):
            try:
                import shutil
                shutil.copy2(filepath, bak)
            except Exception:
                pass
        # Atomic rename
        os.replace(tmp, filepath)
    except OSError as e:
        logger.error(f"CRITICAL: Could not save {filepath}: {e}")
        # Don't delete tmp if rename failed
        raise

def load_data():
    default = {"groups": [], "banned_users": {}}
    return normalize_data(_safe_load_json(DATA_FILE, default))

def save_data(data):
    _safe_save_json(DATA_FILE, data)

def import_groups_from_file():
    """Import groups from groups.json into data.json on startup."""
    if not os.path.exists(GROUPS_FILE):
        return
    with open(GROUPS_FILE, "r") as f:
        groups_map = json.load(f)
    if not groups_map:
        return
    data = load_data()
    existing_ids = {g["id"] for g in data.get("groups", [])}
    added = 0
    for name, gid in groups_map.items():
        if gid not in existing_ids:
            data["groups"].append({"id": gid, "title": name})
            existing_ids.add(gid)
            added += 1
    if added > 0:
        save_data(data)
        logger.info(f"Imported {added} groups from groups.json")

# Auto-import on module load
import_groups_from_file()

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


async def ban_user_in_groups(context: ContextTypes.DEFAULT_TYPE, groups: list, target_id: int):
    """Ban user in multiple groups and return successful/failed groups."""
    successful_groups = []
    failed_groups = []

    for g in groups:
        gid = g["id"]
        title = g.get("title", str(gid))
        try:
            await context.bot.ban_chat_member(chat_id=gid, user_id=target_id, revoke_messages=True)
            successful_groups.append(g)
        except Exception as e:
            logger.error(f"Ban failed for {target_id} in {gid}: {e}")
            failed_groups.append({"id": gid, "title": title, "error": str(e)})

    return successful_groups, failed_groups

def load_users():
    return _safe_load_json(USERS_FILE, {})

def save_users(users):
    _safe_save_json(USERS_FILE, users)

def track_user(user, group_id=None):
    """Track a user's username → ID mapping, per-group message count, and first seen date."""
    if not user or user.is_bot:
        return
    users = load_users()
    now_str = now_de().strftime("%d.%m.%Y %H:%M")

    def _update_entry(key):
        existing = users.get(key, {})
        entry = {
            "id": user.id,
            "name": user.full_name,
            "username": user.username,
            "first_seen": existing.get("first_seen", now_str),
            "group_stats": existing.get("group_stats", {}),
        }
        if group_id:
            gkey = str(group_id)
            gs = entry["group_stats"].get(gkey, {"msg_count": 0, "first_seen": now_str})
            gs["msg_count"] = gs.get("msg_count", 0) + 1
            entry["group_stats"][gkey] = gs
        users[key] = entry

    if user.username:
        _update_entry(user.username.lower())
    _update_entry(str(user.id))
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

async def is_chat_admin(context, chat_id: int, user_id: int) -> bool:
    """Check if user is admin or creator in a specific chat."""
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

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
        [InlineKeyboardButton("🚫 BannALL", callback_data="menu_banall"),
         InlineKeyboardButton("📨 Messenger", callback_data="menu_messenger")],
        [InlineKeyboardButton("🔁 Wiederholte", callback_data="menu_scheduled"),
         InlineKeyboardButton("🔓 Open/Close", callback_data="menu_openclose")],
        [InlineKeyboardButton("🏗 Befehle", callback_data="pcmd_menu"),
         InlineKeyboardButton("⚠️ Warns", callback_data="menu_warns")],
        [InlineKeyboardButton("🔤 Verbotene Worte", callback_data="menu_badwords"),
         InlineKeyboardButton("🗑 Nachrichten", callback_data="menu_msgdelete")],
        [InlineKeyboardButton("🛡 Anti-Spam", callback_data="menu_antispam"),
         InlineKeyboardButton("👥 Mitglieder", callback_data="menu_members")],
        [InlineKeyboardButton("⚙️ Einstellungen", callback_data="menu_settings")],
    ]

    role = "👑 Owner" if is_owner(user_id) else "🛡️ Admin"
    await update.message.reply_text(
        f"🤖 *Bot Menü* ({role})\n_Wähle eine Einstellung:_",
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
WAITING_PCMD_NAME = 16
WAITING_PCMD_TEXT = 17
WAITING_PCMD_GROUPS = 18
WAITING_WARN_MUTE_DUR = 19
WAITING_BADWORD_ADD = 20

# --- Smart text normalizer for forbidden word evasion detection ---
LEET_MAP = {
    '0': 'o', '1': 'i', '2': 'z', '3': 'e', '4': 'a', '5': 's',
    '6': 'g', '7': 't', '8': 'b', '9': 'g',
    '@': 'a', '$': 's', '!': 'i', '|': 'l',
    '€': 'e', '£': 'l', '¥': 'y',
}

def normalize_text(text):
    """Normalize text to catch evasion tricks like C.P, c p, c=p, leet speak etc."""
    text = text.lower()
    # Replace leet speak characters
    normalized = []
    for ch in text:
        if ch in LEET_MAP:
            normalized.append(LEET_MAP[ch])
        elif ch.isalpha():
            normalized.append(ch)
        # Skip all non-alpha characters (dots, spaces, special chars)
    return ''.join(normalized)

def check_forbidden_words(text, word_list):
    """Check if any forbidden word appears as a standalone word in the text. Returns matched word or None."""
    # First check the original text split into words (normalized per-word)
    import re as _re
    # Split original text by whitespace and common separators
    raw_words = _re.split(r'[\s,.\-_;:!?/\\|+=()\[\]{}<>]+', text)
    for word in word_list:
        norm_word = normalize_text(word)
        if not norm_word:
            continue
        for raw_w in raw_words:
            norm_raw = normalize_text(raw_w)
            if norm_raw == norm_word:
                return word
    return None

import re as _re

def parse_duration_text(text):
    """Parse duration strings like '1h 30m', '2 days 3 hours', '1M 2d 12h 4m 34s'."""
    text = text.strip().lower()
    total_seconds = 0
    # Pattern: number followed by unit
    patterns = [
        (r'(\d+)\s*months?', 30 * 86400),
        (r'(\d+)\s*M(?!\w)', 30 * 86400),
        (r'(\d+)\s*days?', 86400),
        (r'(\d+)\s*d(?!\w)', 86400),
        (r'(\d+)\s*hours?', 3600),
        (r'(\d+)\s*h(?!\w)', 3600),
        (r'(\d+)\s*minutes?', 60),
        (r'(\d+)\s*mins?', 60),
        (r'(\d+)\s*m(?!\w|o|i)', 60),
        (r'(\d+)\s*seconds?', 1),
        (r'(\d+)\s*secs?', 1),
        (r'(\d+)\s*s(?!\w)', 1),
    ]
    for pattern, multiplier in patterns:
        for match in _re.finditer(pattern, text):
            total_seconds += int(match.group(1)) * multiplier
    return total_seconds

def format_duration_human(seconds):
    """Format seconds into human readable German string."""
    if seconds <= 0:
        return "Inaktiv"
    parts = []
    days = seconds // 86400
    if days > 0:
        parts.append(f"{days} Tag{'e' if days != 1 else ''}")
        seconds %= 86400
    hours = seconds // 3600
    if hours > 0:
        parts.append(f"{hours} Stunde{'n' if hours != 1 else ''}")
        seconds %= 3600
    minutes = seconds // 60
    if minutes > 0:
        parts.append(f"{minutes} Minute{'n' if minutes != 1 else ''}")
        seconds %= 60
    if seconds > 0:
        parts.append(f"{seconds} Sekunde{'n' if seconds != 1 else ''}")
    return " ".join(parts) if parts else "Inaktiv"

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


def get_info_scope_chat_id(update: Update):
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        return update.effective_chat.id
    return None


async def get_info_group_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None, user_id: int):
    state = {"is_muted": False, "is_banned_local": False, "is_premium": False}
    if not chat_id:
        return state
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status == "restricted" and getattr(member, "can_send_messages", True) is False:
            state["is_muted"] = True
        if member.status == "kicked":
            state["is_banned_local"] = True
        if getattr(member, "user", None):
            state["is_premium"] = getattr(member.user, "is_premium", False) or False
    except Exception:
        pass
    return state


async def get_info_banall_groups(context: ContextTypes.DEFAULT_TYPE, scope_chat_id: int | None):
    """Return registered groups plus the current /info group if it is missing."""
    groups = await get_bot_groups(context)
    if scope_chat_id and not any(g["id"] == scope_chat_id for g in groups):
        try:
            chat = await context.bot.get_chat(scope_chat_id)
            if chat.type in ("group", "supergroup"):
                groups.append({"id": chat.id, "title": chat.title or str(chat.id)})
        except Exception:
            groups.append({"id": scope_chat_id, "title": str(scope_chat_id)})
    return groups


def build_info_keyboard(scope_chat_id: int | None, target_id: int, is_muted: bool, is_banned_local: bool, is_banned_all: bool):
    keyboard = []
    if scope_chat_id:
        keyboard.append([
            InlineKeyboardButton(
                "✅ Unmute" if is_muted else "🔇 Mute",
                callback_data=f"info_unmute_{scope_chat_id}_{target_id}" if is_muted else f"info_mute_{scope_chat_id}_{target_id}",
            ),
            InlineKeyboardButton(
                "✅ Entsperren" if is_banned_local else "🚫 Ban",
                callback_data=f"info_unban_{scope_chat_id}_{target_id}" if is_banned_local else f"info_ban_{scope_chat_id}_{target_id}",
            ),
        ])

    keyboard.append([
        InlineKeyboardButton(
            "✅ Entsperren ALL" if is_banned_all else "🚫 BanALL",
            callback_data=f"info_unbanall_{target_id}" if is_banned_all else f"info_banall_{target_id}",
        )
    ])
    return InlineKeyboardMarkup(keyboard)

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
        await query.answer("⚠️ Du hast keine Berechtigung, diesen Vorgang auszuführen\n\n💡 Falls du denkst, berechtigt zu sein, sende /reload und versuche es erneut.", show_alert=True)
        return

    data = query.data

    # === BACK TO MAIN MENU ===
    if data == "back_main":
        keyboard = [
            [InlineKeyboardButton("🚫 BannALL", callback_data="menu_banall"),
             InlineKeyboardButton("📨 Messenger", callback_data="menu_messenger")],
            [InlineKeyboardButton("🔁 Wiederholte", callback_data="menu_scheduled"),
             InlineKeyboardButton("🔓 Open/Close", callback_data="menu_openclose")],
            [InlineKeyboardButton("🏗 Befehle", callback_data="pcmd_menu"),
             InlineKeyboardButton("⚠️ Warns", callback_data="menu_warns")],
            [InlineKeyboardButton("🔤 Verbotene Worte", callback_data="menu_badwords"),
             InlineKeyboardButton("🗑 Nachrichten", callback_data="menu_msgdelete")],
            [InlineKeyboardButton("🛡 Anti-Spam", callback_data="menu_antispam"),
             InlineKeyboardButton("👥 Mitglieder", callback_data="menu_members")],
            [InlineKeyboardButton("⚙️ Einstellungen", callback_data="menu_settings")],
        ]
        role = "👑 Owner" if is_owner(user_id) else "🛡️ Admin"
        # Clear any pending state
        user_data_store.pop(user_id, None)
        await query.edit_message_text(
            f"🤖 *Bot Menü* ({role})\n_Wähle eine Einstellung:_",
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
        import time as _time
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
        
        import time as _time
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

    elif data.startswith("sched_view_text_"):
        sched_id = data.replace("sched_view_text_", "")
        bot_data = load_data()
        for s in bot_data.get("scheduled", []):
            if str(s.get("id")) == str(sched_id):
                preview_html = s.get("text_html") or s.get("text") or "(leer)"
                keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data=f"sched_edit_text_{sched_id}")]]
                await query.edit_message_text(
                    f"📄 <b>Nachrichtentext:</b>\n\n{preview_html}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
                return
        await query.edit_message_text("⚠️ Nicht gefunden.")

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

    # === INFO MODERATION BUTTONS ===
    # IMPORTANT: banall/unbanall must be checked BEFORE ban/unban (prefix overlap)
    elif data.startswith("info_banall_"):
        target_id = int(data.replace("info_banall_", "", 1))
        scope_chat_id = query.message.chat.id if query.message and query.message.chat else None
        groups = await get_info_banall_groups(context, scope_chat_id)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.")
            return
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        # Admin-Schutz: Prüfe ob target Admin in irgendeiner Gruppe ist
        if scope_chat_id and await is_chat_admin(context, scope_chat_id, target_id):
            await query.answer("⛔ Dieser User ist ein Administrator und kann nicht gebannt werden.", show_alert=True)
            return

        successful_groups, failed_groups = await ban_user_in_groups(context, groups, target_id)
        if successful_groups:
            remember_group_ban([g["id"] for g in successful_groups], target_id, target_name, target_username)

        group_state = await get_info_group_state(context, scope_chat_id, target_id)
        is_banned_all = len(successful_groups) == len(groups)
        keyboard = build_info_keyboard(scope_chat_id, target_id, group_state["is_muted"], group_state["is_banned_local"], is_banned_all)
        uname = f"@{target_username} " if target_username else ""
        failed_preview = ", ".join(html.escape(g["title"]) for g in failed_groups[:4])
        if len(failed_groups) > 4:
            failed_preview += f" +{len(failed_groups) - 4} weitere"

        if successful_groups:
            text = f"{uname}[<code>{target_id}</code>] in <b>{len(successful_groups)}/{len(groups)}</b> Gruppen gebannt."
            if failed_groups:
                text += f"\n❌ Fehlgeschlagen: {failed_preview}"
        else:
            text = f"⚠️ {uname}[<code>{target_id}</code>] konnte in keiner Gruppe gebannt werden."
            if failed_groups:
                text += f"\n❌ Fehlgeschlagen: {failed_preview}"

        await query.edit_message_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(
            context,
            f"BANALL (via /info): {target_name} ({target_id}) von {query.from_user.full_name} — {len(successful_groups)} OK, {len(failed_groups)} Fehler",
        )

    elif data.startswith("info_unbanall_"):
        target_id = int(data.replace("info_unbanall_", "", 1))
        scope_chat_id = query.message.chat.id if query.message and query.message.chat else None
        groups = await get_info_banall_groups(context, scope_chat_id)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.")
            return
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        for g in groups:
            try:
                await context.bot.unban_chat_member(chat_id=g["id"], user_id=target_id, only_if_banned=True)
            except Exception as e:
                logger.error(f"Info unbanall failed for {target_id} in {g['id']}: {e}")
        forget_group_ban([g["id"] for g in groups], target_id)

        group_state = await get_info_group_state(context, scope_chat_id, target_id)
        keyboard = build_info_keyboard(scope_chat_id, target_id, group_state["is_muted"], group_state["is_banned_local"], False)
        uname = f"@{target_username} " if target_username else ""
        await query.edit_message_text(
            f"{uname}[<code>{target_id}</code>] überall entsperrt ✅",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, f"UNBANALL (via /info): {target_name} ({target_id}) von {query.from_user.full_name}")

    elif data.startswith("info_ban_"):
        payload = data.replace("info_ban_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        groups = await get_info_banall_groups(context, scope_chat_id)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        # Admin-Schutz
        if await is_chat_admin(context, scope_chat_id, target_id):
            await query.answer("⛔ Dieser User ist ein Administrator und kann nicht gebannt werden.", show_alert=True)
            return

        await context.bot.ban_chat_member(chat_id=scope_chat_id, user_id=target_id, revoke_messages=True)
        remember_group_ban([scope_chat_id], target_id, target_name, target_username)

        is_banned_all = bool(groups) and all(is_banned_in_group(g["id"], target_id) for g in groups)
        keyboard = build_info_keyboard(scope_chat_id, target_id, False, True, is_banned_all)
        uname = f"@{target_username} " if target_username else ""
        await query.edit_message_text(
            f"{uname}[<code>{target_id}</code>] verbannt.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, f"BAN (via /info): {target_name} ({target_id}) in {scope_chat_id} von {query.from_user.full_name}")

    elif data.startswith("info_unban_"):
        payload = data.replace("info_unban_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        groups = await get_info_banall_groups(context, scope_chat_id)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        await context.bot.unban_chat_member(chat_id=scope_chat_id, user_id=target_id, only_if_banned=True)
        forget_group_ban([scope_chat_id], target_id)

        group_state = await get_info_group_state(context, scope_chat_id, target_id)
        is_banned_all = bool(groups) and all(is_banned_in_group(g["id"], target_id) for g in groups)
        keyboard = build_info_keyboard(scope_chat_id, target_id, group_state["is_muted"], False, is_banned_all)
        uname = f"@{target_username} " if target_username else ""
        await query.edit_message_text(
            f"{uname}[<code>{target_id}</code>] entsperrt ✅",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, f"UNBAN (via /info): {target_name} ({target_id}) in {scope_chat_id} von {query.from_user.full_name}")

    elif data.startswith("info_mute_"):
        payload = data.replace("info_mute_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        groups = await get_info_banall_groups(context, scope_chat_id)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        # Admin-Schutz
        if await is_chat_admin(context, scope_chat_id, target_id):
            await query.answer("⛔ Dieser User ist ein Administrator und kann nicht gemutet werden.", show_alert=True)
            return

        await context.bot.restrict_chat_member(
            chat_id=scope_chat_id,
            user_id=target_id,
            permissions=ChatPermissions.no_permissions(),
        )

        is_banned_all = bool(groups) and all(is_banned_in_group(g["id"], target_id) for g in groups)
        group_state = await get_info_group_state(context, scope_chat_id, target_id)
        keyboard = build_info_keyboard(scope_chat_id, target_id, True, group_state["is_banned_local"], is_banned_all)
        uname = f"@{target_username} " if target_username else ""
        await query.edit_message_text(
            f"{uname}[<code>{target_id}</code>] wurde 🔇 stummgeschaltet.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, f"MUTE (via /info): {target_name} ({target_id}) in {scope_chat_id} von {query.from_user.full_name}")

    elif data.startswith("info_unmute_"):
        payload = data.replace("info_unmute_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        groups = await get_info_banall_groups(context, scope_chat_id)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        chat = await context.bot.get_chat(scope_chat_id)
        await context.bot.restrict_chat_member(
            chat_id=scope_chat_id,
            user_id=target_id,
            permissions=chat.permissions or ChatPermissions.all_permissions(),
        )

        is_banned_all = bool(groups) and all(is_banned_in_group(g["id"], target_id) for g in groups)
        group_state = await get_info_group_state(context, scope_chat_id, target_id)
        keyboard = build_info_keyboard(scope_chat_id, target_id, False, group_state["is_banned_local"], is_banned_all)
        uname = f"@{target_username} " if target_username else ""
        await query.edit_message_text(
            f"{uname}[<code>{target_id}</code>] wurde ✅ entmutet.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, f"UNMUTE (via /info): {target_name} ({target_id}) in {scope_chat_id} von {query.from_user.full_name}")

    # === CMD UNMUTE BUTTON ===
    elif data.startswith("cmd_unmute_"):
        payload = data.replace("cmd_unmute_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        try:
            chat_obj = await context.bot.get_chat(scope_chat_id)
            await context.bot.restrict_chat_member(
                chat_id=scope_chat_id,
                user_id=target_id,
                permissions=chat_obj.permissions or ChatPermissions.all_permissions(),
            )
            uname = f"@{target_username} " if target_username else ""
            await query.edit_message_text(
                f"{uname}[<code>{target_id}</code>] wurde ✅ entmutet.",
                parse_mode="HTML",
            )
            await log_action(context, f"✅ Unmute (Button): {target_name} ({target_id}) in {scope_chat_id} von {query.from_user.full_name}")
        except Exception as e:
            await query.answer(f"❌ Unmute fehlgeschlagen: {e}", show_alert=True)

    # === CMD UNBAN BUTTON ===
    elif data.startswith("cmd_unban_"):
        payload = data.replace("cmd_unban_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        try:
            await context.bot.unban_chat_member(chat_id=scope_chat_id, user_id=target_id, only_if_banned=True)
            uname = f"@{target_username} " if target_username else ""
            await query.edit_message_text(
                f"{uname}[<code>{target_id}</code>] wurde ✅ entbannt.",
                parse_mode="HTML",
            )
            await log_action(context, f"✅ Unban (Button): {target_name} ({target_id}) in {scope_chat_id} von {query.from_user.full_name}")
        except Exception as e:
            await query.answer(f"❌ Unban fehlgeschlagen: {e}", show_alert=True)

    # === LINK WARN CANCEL ===
    elif data.startswith("link_warn_cancel_"):
        payload = data.replace("link_warn_cancel_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        bot_data = load_data()
        warnings = bot_data.get("warnings", {})
        key = f"{scope_chat_id}_{target_id}"
        warn_entry = warnings.get(key)
        if warn_entry and warn_entry.get("count", 0) > 0:
            warn_entry["count"] -= 1
            if warn_entry["count"] <= 0:
                warnings.pop(key, None)
            save_data(bot_data)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None
        uname = f"@{target_username} " if target_username else ""
        await query.edit_message_text(
            f"↩️ Link-Verwarnung für {uname}[<code>{target_id}</code>] wurde zurückgenommen.",
            parse_mode="HTML",
        )
        await log_action(context, f"LINK-WARN CANCEL: {target_name} ({target_id}) in {scope_chat_id} von {query.from_user.full_name}")

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

    # === CONFIG MENU ===
    elif data == "menu_config":
        bot_data = load_data()
        cmd_count = len(bot_data.get("personal_commands", {}))
        keyboard = [
            [InlineKeyboardButton("» 🏗 Persönliche Befehle «", callback_data="pcmd_menu")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            f"⚙️ <b>Konfiguration</b>\n\n"
            f"Persönliche Befehle: {cmd_count}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # === PERSONAL COMMANDS MENU ===
    elif data == "pcmd_menu":
        bot_data = load_data()
        cmds = bot_data.get("personal_commands", {})
        cmd_count = sum(len(entries) if isinstance(entries, list) else 1 for entries in cmds.values())
        keyboard = [
            [InlineKeyboardButton("» 🏗 Persönliche Befehle «", callback_data="noop")],
            [InlineKeyboardButton("🔤 Liste", callback_data="pcmd_list")],
            [InlineKeyboardButton("➕ Hinzufügen", callback_data="pcmd_add"),
             InlineKeyboardButton("➖ Entfernen", callback_data="pcmd_remove")],
            [InlineKeyboardButton("🗑 Alle löschen", callback_data="pcmd_clear_confirm")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            f"🏗 <b>Persönliche Befehle</b>\n\n"
            f"Gespeicherte Befehle: {cmd_count}\n\n"
            f"<i>Nutze /befehl &lt;Name&gt; als Antwort auf eine Nachricht in einer Gruppe, "
            f"um einen Befehl zu erstellen.\n"
            f"Lösche mit /unbefehl &lt;Name&gt;</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "pcmd_list":
        bot_data = load_data()
        cmds = bot_data.get("personal_commands", {})
        if not cmds:
            keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="pcmd_menu")]]
            await query.edit_message_text(
                "📋 <b>Persönliche Befehle</b>\n\nKeine Befehle gespeichert.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
            return
        text = "📋 <b>Persönliche Befehle</b>\n\n"
        groups_list = bot_data.get("groups", [])
        gid_to_name = {g["id"]: g["title"] for g in groups_list}
        for name, entries in cmds.items():
            if not isinstance(entries, list):
                entries = [entries]
            for i, info in enumerate(entries):
                preview = html.escape((info.get("text") or "")[:30])
                has_media = " 🖼" if info.get("media_file_id") else ""
                cmd_groups = info.get("groups", [])
                if cmd_groups:
                    gnames = ", ".join(gid_to_name.get(gid, str(gid)) for gid in cmd_groups[:3])
                    if len(cmd_groups) > 3:
                        gnames += f" +{len(cmd_groups)-3}"
                    grp_label = f" [{gnames}]"
                else:
                    grp_label = " [Alle]"
                text += f"• /<b>{html.escape(name)}</b>{grp_label} — {preview}{has_media}\n"
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="pcmd_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pcmd_add":
        groups = await get_bot_groups(context)
        user_data_store[user_id] = {"action": "pcmd_add", "selected": set()}
        await show_pcmd_group_selection(query, context, user_id, groups)

    elif data.startswith("pcmd_grp_toggle_"):
        gid = int(data.replace("pcmd_grp_toggle_", ""))
        pending = user_data_store.get(user_id, {})
        selected = pending.get("selected", set())
        if gid in selected:
            selected.discard(gid)
        else:
            selected.add(gid)
        pending["selected"] = selected
        user_data_store[user_id] = pending
        groups = await get_bot_groups(context)
        await show_pcmd_group_selection(query, context, user_id, groups)

    elif data == "pcmd_grp_all":
        groups = await get_bot_groups(context)
        user_data_store[user_id]["selected"] = {g["id"] for g in groups}
        await show_pcmd_group_selection(query, context, user_id, groups)

    elif data == "pcmd_grp_none":
        user_data_store[user_id]["selected"] = set()
        groups = await get_bot_groups(context)
        await show_pcmd_group_selection(query, context, user_id, groups)

    elif data == "pcmd_grp_confirm":
        pending = user_data_store.get(user_id, {})
        selected = pending.get("selected", set())
        if not selected:
            await query.answer("⚠️ Wähle mindestens eine Gruppe!", show_alert=True)
            return
        pending["groups"] = list(selected)
        user_data_store[user_id] = pending
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="pcmd_menu")]]
        await query.edit_message_text(
            "➕ <b>Befehl hinzufügen</b>\n\n"
            "Sende mir den Namen für den neuen Befehl (ohne /).\n"
            "Beispiel: <code>hele</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        context.user_data["state"] = WAITING_PCMD_NAME

    elif data == "pcmd_remove":
        bot_data = load_data()
        cmds = bot_data.get("personal_commands", {})
        if not cmds:
            await query.answer("Keine Befehle vorhanden.", show_alert=True)
            return
        groups_list = bot_data.get("groups", [])
        gid_to_name = {g["id"]: g["title"] for g in groups_list}
        keyboard = []
        for name, entries in cmds.items():
            if not isinstance(entries, list):
                entries = [entries]
            for i, info in enumerate(entries):
                cmd_groups = info.get("groups", [])
                if cmd_groups:
                    gnames = ", ".join(gid_to_name.get(gid, str(gid)) for gid in cmd_groups[:2])
                    label = f"🗑 /{name} [{gnames}]"
                else:
                    label = f"🗑 /{name} [Alle]"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"pcmd_del_{name}_{i}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="pcmd_menu")])
        await query.edit_message_text(
            "➖ <b>Befehl entfernen</b>\n\nWähle den Befehl zum Löschen:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("pcmd_del_") and data != "pcmd_del_":
        parts = data.replace("pcmd_del_", "").rsplit("_", 1)
        if len(parts) == 2:
            cmd_name, idx_str = parts
            idx = int(idx_str)
        else:
            cmd_name = parts[0]
            idx = 0
        bot_data = load_data()
        cmds = bot_data.get("personal_commands", {})
        entries = cmds.get(cmd_name, [])
        if not isinstance(entries, list):
            entries = [entries]
        if 0 <= idx < len(entries):
            entries.pop(idx)
        if not entries:
            cmds.pop(cmd_name, None)
        else:
            cmds[cmd_name] = entries
        save_data(bot_data)
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="pcmd_menu")]]
        await query.edit_message_text(
            f"✅ Befehl /{html.escape(cmd_name)} gelöscht.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "pcmd_clear_confirm":
        keyboard = [
            [InlineKeyboardButton("✅ Ja, alle löschen", callback_data="pcmd_clear"),
             InlineKeyboardButton("❌ Abbrechen", callback_data="pcmd_menu")],
        ]
        await query.edit_message_text(
            "⚠️ Wirklich ALLE persönlichen Befehle löschen?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "pcmd_clear":
        bot_data = load_data()
        bot_data["personal_commands"] = {}
        save_data(bot_data)
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="pcmd_menu")]]
        await query.edit_message_text(
            "✅ Alle persönlichen Befehle gelöscht.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # === MEMBERS (Mass Unban / Unmute) ===
    elif data == "menu_members":
        keyboard = [
            [InlineKeyboardButton("✅ All Unban", callback_data="members_unban_select")],
            [InlineKeyboardButton("🔊 All Unmute", callback_data="members_unmute_select")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            "👥 <b>Mitglieder</b>\n\nWähle eine Aktion:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "members_unban_select" or data == "members_unmute_select":
        action = "mass_unban" if "unban" in data else "mass_unmute"
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("⚠️ Keine Gruppen registriert.")
            return
        user_data_store[user_id] = {"action": action, "selected": set()}
        await show_members_group_selection(query, context, user_id, groups, action)

    elif data.startswith("memgrp_toggle_"):
        payload = data.replace("memgrp_toggle_", "")
        # gid is everything before last _mass_unban or _mass_unmute
        pending = user_data_store.get(user_id, {})
        action = pending.get("action", "mass_unban")
        gid = int(payload.replace(f"_{action}", ""))
        selected = pending.get("selected", set())
        if gid in selected:
            selected.discard(gid)
        else:
            selected.add(gid)
        pending["selected"] = selected
        user_data_store[user_id] = pending
        groups = await get_bot_groups(context)
        await show_members_group_selection(query, context, user_id, groups, action)

    elif data.startswith("memgrp_all_") or data.startswith("memgrp_none_"):
        action = user_data_store.get(user_id, {}).get("action", "mass_unban")
        groups = await get_bot_groups(context)
        if data.startswith("memgrp_all_"):
            user_data_store[user_id]["selected"] = {g["id"] for g in groups}
        else:
            user_data_store[user_id]["selected"] = set()
        await show_members_group_selection(query, context, user_id, groups, action)

    elif data.startswith("memgrp_confirm_"):
        action = user_data_store.get(user_id, {}).get("action", "")
        selected = user_data_store.get(user_id, {}).get("selected", set())
        if not selected:
            await query.answer("⚠️ Keine Gruppen ausgewählt!", show_alert=True)
            return

        groups = await get_bot_groups(context)
        group_names = [g["title"] for g in groups if g["id"] in selected]
        action_label = "Unban" if "unban" in action else "Unmute"
        action_emoji = "✅" if "unban" in action else "🔊"

        keyboard = [
            [InlineKeyboardButton(f"⚡ {action_label} jetzt ausführen", callback_data=f"memgrp_exec_{action}")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_members")],
        ]
        await query.edit_message_text(
            f"{action_emoji} <b>All {action_label}</b>\n\n"
            f"👥 <b>Gruppen</b>: {len(selected)}\n"
            f"{', '.join(group_names[:5])}"
            f"{'...' if len(group_names) > 5 else ''}\n\n"
            f"⚠️ <b>Achtung:</b> Dies wird ALLE gebannten/gemuteten User in den gewählten Gruppen "
            f"{'entbannen' if 'unban' in action else 'entmuten'}!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("memgrp_exec_"):
        action = user_data_store.get(user_id, {}).get("action", "")
        selected = list(user_data_store.get(user_id, {}).get("selected", set()))
        if not selected:
            await query.answer("⚠️ Keine Gruppen!", show_alert=True)
            return

        await query.edit_message_text("⏳ Wird ausgeführt... Bitte warten.")

        success_count = 0
        error_count = 0

        if "unban" in action:
            # Get banned users from each group and unban them
            for gid in selected:
                try:
                    # Get list of banned users (kicked members)
                    banned = []
                    try:
                        async for member in context.bot.get_chat_administrators(gid):
                            pass  # just to verify bot has access
                    except Exception:
                        pass
                    # Use getChatMember won't work for listing, so we use our tracked data
                    bot_data = load_data()
                    banned_ids = set()
                    for grp_data in bot_data.get("groups", []):
                        if str(grp_data.get("id")) == str(gid):
                            banned_ids = set(grp_data.get("banned_users", []))
                            break
                    # Also check global banned data
                    for uid_str, udata in bot_data.get("users", {}).items():
                        bans = udata.get("banned_in", [])
                        if gid in bans or str(gid) in [str(b) for b in bans]:
                            banned_ids.add(int(uid_str))
                    
                    for uid in banned_ids:
                        try:
                            await context.bot.unban_chat_member(chat_id=gid, user_id=uid, only_if_banned=True)
                            success_count += 1
                        except Exception as e:
                            logger.error(f"Mass unban failed for {uid} in {gid}: {e}")
                            error_count += 1
                    # Clear tracked bans for this group
                    forget_group_ban([gid], list(banned_ids))
                except Exception as e:
                    logger.error(f"Mass unban error in group {gid}: {e}")
                    error_count += 1
        else:
            # Unmute: restrict with all permissions
            for gid in selected:
                try:
                    bot_data = load_data()
                    muted_ids = set()
                    for uid_str, udata in bot_data.get("users", {}).items():
                        mutes = udata.get("muted_in", [])
                        if gid in mutes or str(gid) in [str(m) for m in mutes]:
                            muted_ids.add(int(uid_str))
                    
                    chat_obj = await context.bot.get_chat(gid)
                    default_perms = chat_obj.permissions or ChatPermissions.all_permissions()
                    
                    for uid in muted_ids:
                        try:
                            await context.bot.restrict_chat_member(
                                chat_id=gid, user_id=uid,
                                permissions=default_perms,
                            )
                            success_count += 1
                        except Exception as e:
                            logger.error(f"Mass unmute failed for {uid} in {gid}: {e}")
                            error_count += 1
                except Exception as e:
                    logger.error(f"Mass unmute error in group {gid}: {e}")
                    error_count += 1

        action_label = "Unban" if "unban" in action else "Unmute"
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_members")]]
        await query.edit_message_text(
            f"✅ <b>All {action_label} abgeschlossen!</b>\n\n"
            f"✅ Erfolgreich: {success_count}\n"
            f"❌ Fehler: {error_count}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        await log_action(context, f"MASS {action_label.upper()}: {success_count} erfolgreich, {error_count} Fehler – von {query.from_user.full_name}")

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

    # === ANTI-SPAM MENU ===
    elif data == "menu_antispam":
        keyboard = [
            [InlineKeyboardButton("🔗 Vollständige Linksperre", callback_data="as_links_menu")],
            [InlineKeyboardButton("📬 Weiterleitung", callback_data="as_forward_menu")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            "🛡 <b>Anti-Spam</b>\n\n"
            "Hier kannst du Spam-Schutz Funktionen konfigurieren.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # --- Vollständige Linksperre ---
    elif data == "as_links_menu":
        bot_data = load_data()
        lc = bot_data.get("antispam_links", {"punishment": "aus", "delete": True})
        punishment = lc.get("punishment", "aus")
        delete_msg = lc.get("delete", True)
        p_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = p_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="as_link_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="as_link_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="as_link_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="as_link_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="as_link_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="as_link_toggle_delete")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_antispam")],
        ]
        await query.edit_message_text(
            f"🔗 <b>Vollständige Linksperre</b>\n"
            f"Wähle die Bestrafung für das Senden eines Links jeglicher Art aus.\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("as_link_set_"):
        val = data.replace("as_link_set_", "")
        bot_data = load_data()
        bot_data.setdefault("antispam_links", {})["punishment"] = val
        save_data(bot_data)
        await query.answer(f"Bestrafung auf {val} gesetzt ✅")
        # Re-render
        lc = bot_data["antispam_links"]
        punishment = lc.get("punishment", "aus")
        delete_msg = lc.get("delete", True)
        p_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = p_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="as_link_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="as_link_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="as_link_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="as_link_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="as_link_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="as_link_toggle_delete")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_antispam")],
        ]
        await query.edit_message_text(
            f"🔗 <b>Vollständige Linksperre</b>\n"
            f"Wähle die Bestrafung für das Senden eines Links jeglicher Art aus.\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "as_link_toggle_delete":
        bot_data = load_data()
        lc = bot_data.setdefault("antispam_links", {"punishment": "aus", "delete": True})
        lc["delete"] = not lc.get("delete", True)
        save_data(bot_data)
        await query.answer(f"Löschen: {'An' if lc['delete'] else 'Aus'} ✅")
        punishment = lc.get("punishment", "aus")
        delete_msg = lc.get("delete", True)
        p_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = p_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="as_link_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="as_link_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="as_link_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="as_link_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="as_link_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="as_link_toggle_delete")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_antispam")],
        ]
        await query.edit_message_text(
            f"🔗 <b>Vollständige Linksperre</b>\n"
            f"Wähle die Bestrafung für das Senden eines Links jeglicher Art aus.\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # --- Weiterleitung ---
    elif data == "as_forward_menu":
        bot_data = load_data()
        fw = bot_data.get("antispam_forward", {})
        ch = "✅" if fw.get("channels") else "❌"
        gr = "✅" if fw.get("groups") else "❌"
        us = "✅" if fw.get("users") else "❌"
        bo = "✅" if fw.get("bots") else "❌"
        keyboard = [
            [InlineKeyboardButton(f"📣 Kanäle {ch}", callback_data="as_fw_toggle_channels"),
             InlineKeyboardButton(f"👥 Gruppen {gr}", callback_data="as_fw_toggle_groups")],
            [InlineKeyboardButton(f"👤 Benutzer {us}", callback_data="as_fw_toggle_users"),
             InlineKeyboardButton(f"🤖 Bot {bo}", callback_data="as_fw_toggle_bots")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_antispam")],
        ]
        await query.edit_message_text(
            f"📬 <b>Weiterleitung</b>\n"
            f"Wähle eine Strafe für das Weiterleiten von Nachrichten* in der Gruppe "
            f"(<i>*aus Kanälen oder Posts von Nutzern / Bots</i>).\n\n"
            f"Weiterleitung aus Gruppen blockiert Nachrichten, die von einem anonymen "
            f"Administrator einer anderen Gruppe geschrieben und an diese Gruppe weitergeleitet werden.\n\n"
            f"📣 <b>Weiterleitung aus Kanälen</b>\n  └ Löschen: {ch}\n"
            f"👥 <b>Gruppen</b>\n  └ Löschen: {gr}\n"
            f"👤 <b>Benutzer</b>\n  └ Löschen: {us}\n"
            f"🤖 <b>Bot</b>\n  └ Löschen: {bo}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("as_fw_toggle_"):
        key = data.replace("as_fw_toggle_", "")
        bot_data = load_data()
        fw = bot_data.setdefault("antispam_forward", {})
        fw[key] = not fw.get(key, False)
        save_data(bot_data)
        await query.answer(f"{key.title()}: {'Löschen An' if fw[key] else 'Aus'} ✅")
        ch = "✅" if fw.get("channels") else "❌"
        gr = "✅" if fw.get("groups") else "❌"
        us = "✅" if fw.get("users") else "❌"
        bo = "✅" if fw.get("bots") else "❌"
        keyboard = [
            [InlineKeyboardButton(f"📣 Kanäle {ch}", callback_data="as_fw_toggle_channels"),
             InlineKeyboardButton(f"👥 Gruppen {gr}", callback_data="as_fw_toggle_groups")],
            [InlineKeyboardButton(f"👤 Benutzer {us}", callback_data="as_fw_toggle_users"),
             InlineKeyboardButton(f"🤖 Bot {bo}", callback_data="as_fw_toggle_bots")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_antispam")],
        ]
        await query.edit_message_text(
            f"📬 <b>Weiterleitung</b>\n"
            f"Wähle eine Strafe für das Weiterleiten von Nachrichten* in der Gruppe "
            f"(<i>*aus Kanälen oder Posts von Nutzern / Bots</i>).\n\n"
            f"Weiterleitung aus Gruppen blockiert Nachrichten, die von einem anonymen "
            f"Administrator einer anderen Gruppe geschrieben und an diese Gruppe weitergeleitet werden.\n\n"
            f"📣 <b>Weiterleitung aus Kanälen</b>\n  └ Löschen: {ch}\n"
            f"👥 <b>Gruppen</b>\n  └ Löschen: {gr}\n"
            f"👤 <b>Benutzer</b>\n  └ Löschen: {us}\n"
            f"🤖 <b>Bot</b>\n  └ Löschen: {bo}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # === MESSAGE DELETE MENU ===
    elif data == "menu_msgdelete":
        bot_data = load_data()
        cd = bot_data.get("cmd_delete", {"admin_prefixes": [], "user_prefixes": []})
        keyboard = [
            [InlineKeyboardButton("📋 Befehle löschen", callback_data="cmdel_menu")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            "🗑 <b>Nachrichten löschen</b>\n\n"
            "Hier kannst du einstellen, welche Nachrichten automatisch gelöscht werden sollen.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "cmdel_menu":
        bot_data = load_data()
        cd = bot_data.get("cmd_delete", {"admin_prefixes": [], "user_prefixes": []})
        admin_p = cd.get("admin_prefixes", [])
        user_p = cd.get("user_prefixes", [])
        all_prefixes = ["/", "!", ";", "."]

        def _prefix_status(prefixes, prefix):
            return "✅" if prefix in prefixes else ""

        admin_display = ", ".join(admin_p) if admin_p else "Nein"
        user_display = ", ".join(user_p) if user_p else "Nein"

        keyboard = [
            [InlineKeyboardButton(f"Admin", callback_data="noop"),
             InlineKeyboardButton(f"{'✅' if not admin_p else 'Nein'}", callback_data="cmdel_admin_none"),
             InlineKeyboardButton(f"/ {'✅' if '/' in admin_p else ''}", callback_data="cmdel_admin_/")],
            [InlineKeyboardButton("➡️", callback_data="noop"),
             InlineKeyboardButton(f"/!;. {'✅' if set(all_prefixes).issubset(set(admin_p)) else ''}", callback_data="cmdel_admin_all"),
             InlineKeyboardButton(f"!;. {'✅' if set(['!',';','.']).issubset(set(admin_p)) and '/' not in admin_p else ''}", callback_data="cmdel_admin_nosl")],
            [InlineKeyboardButton(f"Benutzer", callback_data="noop"),
             InlineKeyboardButton(f"{'✅' if not user_p else 'Nein'}", callback_data="cmdel_user_none"),
             InlineKeyboardButton(f"/ {'✅' if '/' in user_p else ''}", callback_data="cmdel_user_/")],
            [InlineKeyboardButton("➡️", callback_data="noop"),
             InlineKeyboardButton(f"/!;. {'✅' if set(all_prefixes).issubset(set(user_p)) else ''}", callback_data="cmdel_user_all"),
             InlineKeyboardButton(f"!;. {'✅' if set(['!',';','.']).issubset(set(user_p)) and '/' not in user_p else ''}", callback_data="cmdel_user_nosl")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_msgdelete")],
        ]
        await query.edit_message_text(
            f"📋 <b>Befehle löschen</b>\n"
            f"Welcher dieser Befehle soll gelöscht werden?\n"
            f"  Beispiel: /Hallo, !Hallo, ;Hallo, .Hallo\n\n"
            f"<b>Admin:</b> beginnend mit {admin_display}\n"
            f"<b>Nutzer:</b> beginnend mit {user_display}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("cmdel_admin_") or data.startswith("cmdel_user_"):
        role_key = "admin_prefixes" if data.startswith("cmdel_admin_") else "user_prefixes"
        action = data.split("_", 2)[2]  # after cmdel_admin_ or cmdel_user_
        bot_data = load_data()
        cd = bot_data.setdefault("cmd_delete", {"admin_prefixes": [], "user_prefixes": []})
        all_prefixes = ["/", "!", ";", "."]

        if action == "none":
            cd[role_key] = []
        elif action == "all":
            cd[role_key] = list(all_prefixes)
        elif action == "nosl":
            cd[role_key] = ["!", ";", "."]
        elif action in all_prefixes:
            current = cd.get(role_key, [])
            if action in current:
                current.remove(action)
            else:
                current.append(action)
            cd[role_key] = current

        save_data(bot_data)
        await query.answer("✅ Gespeichert")
        # Re-render the menu
        admin_p = cd.get("admin_prefixes", [])
        user_p = cd.get("user_prefixes", [])
        admin_display = ", ".join(admin_p) if admin_p else "Nein"
        user_display = ", ".join(user_p) if user_p else "Nein"

        keyboard = [
            [InlineKeyboardButton(f"Admin", callback_data="noop"),
             InlineKeyboardButton(f"{'✅' if not admin_p else 'Nein'}", callback_data="cmdel_admin_none"),
             InlineKeyboardButton(f"/ {'✅' if '/' in admin_p else ''}", callback_data="cmdel_admin_/")],
            [InlineKeyboardButton("➡️", callback_data="noop"),
             InlineKeyboardButton(f"/!;. {'✅' if set(all_prefixes).issubset(set(admin_p)) else ''}", callback_data="cmdel_admin_all"),
             InlineKeyboardButton(f"!;. {'✅' if set(['!',';','.']).issubset(set(admin_p)) and '/' not in admin_p else ''}", callback_data="cmdel_admin_nosl")],
            [InlineKeyboardButton(f"Benutzer", callback_data="noop"),
             InlineKeyboardButton(f"{'✅' if not user_p else 'Nein'}", callback_data="cmdel_user_none"),
             InlineKeyboardButton(f"/ {'✅' if '/' in user_p else ''}", callback_data="cmdel_user_/")],
            [InlineKeyboardButton("➡️", callback_data="noop"),
             InlineKeyboardButton(f"/!;. {'✅' if set(all_prefixes).issubset(set(user_p)) else ''}", callback_data="cmdel_user_all"),
             InlineKeyboardButton(f"!;. {'✅' if set(['!',';','.']).issubset(set(user_p)) and '/' not in user_p else ''}", callback_data="cmdel_user_nosl")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_msgdelete")],
        ]
        await query.edit_message_text(
            f"📋 <b>Befehle löschen</b>\n"
            f"Welcher dieser Befehle soll gelöscht werden?\n"
            f"  Beispiel: /Hallo, !Hallo, ;Hallo, .Hallo\n\n"
            f"<b>Admin:</b> beginnend mit {admin_display}\n"
            f"<b>Nutzer:</b> beginnend mit {user_display}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # === FORBIDDEN WORDS MENU ===
    elif data == "menu_badwords":
        bot_data = load_data()
        bw = bot_data.get("badwords_config", {"punishment": "aus", "delete": True})
        punishment = bw.get("punishment", "aus")
        delete_msg = bw.get("delete", True)
        word_list = bot_data.get("badwords", [])
        punishment_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = punishment_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="bw_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="bw_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="bw_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="bw_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="bw_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="bw_toggle_delete")],
            [InlineKeyboardButton("➕ Hinzufügen", callback_data="bw_add"),
             InlineKeyboardButton("➖ Entfernen", callback_data="bw_remove")],
            [InlineKeyboardButton("🔤 Liste", callback_data="bw_list")],
            [InlineKeyboardButton(f"🔢 Verbotene Worte 🆕", callback_data="bw_list") if word_list else InlineKeyboardButton("🔢 Keine Worte", callback_data="noop")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            f"🔤 <b>Verbotene Worte</b>\n"
            f"In diesem Menü kann man eine Bestrafung für diejenigen festlegen, "
            f"die jene Worte verwenden, die man verbieten möchte\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("bw_set_"):
        punishment_val = data.replace("bw_set_", "")
        bot_data = load_data()
        bot_data.setdefault("badwords_config", {})["punishment"] = punishment_val
        save_data(bot_data)
        await query.answer(f"Bestrafung auf {punishment_val} gesetzt ✅")
        # Re-render menu
        bw = bot_data.get("badwords_config", {"punishment": "aus", "delete": True})
        delete_msg = bw.get("delete", True)
        word_list = bot_data.get("badwords", [])
        punishment_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = punishment_labels.get(punishment_val, punishment_val)
        del_label = "Ja ✅" if delete_msg else "Nein"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="bw_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="bw_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="bw_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="bw_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="bw_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="bw_toggle_delete")],
            [InlineKeyboardButton("➕ Hinzufügen", callback_data="bw_add"),
             InlineKeyboardButton("➖ Entfernen", callback_data="bw_remove")],
            [InlineKeyboardButton("🔤 Liste", callback_data="bw_list")],
            [InlineKeyboardButton(f"🔢 {len(word_list)} Verbotene Worte", callback_data="bw_list")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            f"🔤 <b>Verbotene Worte</b>\n"
            f"In diesem Menü kann man eine Bestrafung für diejenigen festlegen, "
            f"die jene Worte verwenden, die man verbieten möchte\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "bw_toggle_delete":
        bot_data = load_data()
        bw = bot_data.setdefault("badwords_config", {})
        bw["delete"] = not bw.get("delete", True)
        save_data(bot_data)
        await query.answer(f"Löschen {'aktiviert' if bw['delete'] else 'deaktiviert'} ✅")
        # Trigger re-render
        punishment = bw.get("punishment", "aus")
        delete_msg = bw.get("delete", True)
        word_list = bot_data.get("badwords", [])
        punishment_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = punishment_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="bw_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="bw_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="bw_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="bw_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="bw_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="bw_toggle_delete")],
            [InlineKeyboardButton("➕ Hinzufügen", callback_data="bw_add"),
             InlineKeyboardButton("➖ Entfernen", callback_data="bw_remove")],
            [InlineKeyboardButton("🔤 Liste", callback_data="bw_list")],
            [InlineKeyboardButton(f"🔢 {len(word_list)} Verbotene Worte", callback_data="bw_list")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            f"🔤 <b>Verbotene Worte</b>\n"
            f"In diesem Menü kann man eine Bestrafung für diejenigen festlegen, "
            f"die jene Worte verwenden, die man verbieten möchte\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "bw_add":
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="menu_badwords")]]
        await query.edit_message_text(
            "➕ Sende jetzt die verbotenen Wörter (jedes Wort in eine neue Zeile):\n\n"
            "<i>Beispiel:\ncp\nfick\nhele</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        context.user_data["state"] = WAITING_BADWORD_ADD

    elif data == "bw_remove":
        bot_data = load_data()
        word_list = bot_data.get("badwords", [])
        if not word_list:
            await query.answer("Keine Worte vorhanden.", show_alert=True)
            return
        keyboard = []
        for i, word in enumerate(word_list):
            keyboard.append([InlineKeyboardButton(f"❌ {word}", callback_data=f"bw_del_{i}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_badwords")])
        await query.edit_message_text(
            "➖ Wähle ein Wort zum Entfernen:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("bw_del_"):
        idx = int(data.replace("bw_del_", ""))
        bot_data = load_data()
        word_list = bot_data.get("badwords", [])
        if 0 <= idx < len(word_list):
            removed = word_list.pop(idx)
            save_data(bot_data)
            await query.answer(f"'{removed}' entfernt ✅")
        # Back to menu
        bw = bot_data.get("badwords_config", {"punishment": "aus", "delete": True})
        punishment = bw.get("punishment", "aus")
        delete_msg = bw.get("delete", True)
        punishment_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = punishment_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="bw_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="bw_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="bw_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="bw_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="bw_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="bw_toggle_delete")],
            [InlineKeyboardButton("➕ Hinzufügen", callback_data="bw_add"),
             InlineKeyboardButton("➖ Entfernen", callback_data="bw_remove")],
            [InlineKeyboardButton("🔤 Liste", callback_data="bw_list")],
            [InlineKeyboardButton(f"🔢 {len(word_list)} Verbotene Worte", callback_data="bw_list")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            f"🔤 <b>Verbotene Worte</b>\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "bw_list":
        bot_data = load_data()
        word_list = bot_data.get("badwords", [])
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_badwords")]]
        if not word_list:
            await query.edit_message_text(
                "🔤 <b>Keine verbotenen Worte eingetragen.</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
        else:
            words_text = "\n".join(f"• <code>{html.escape(w)}</code>" for w in word_list)
            await query.edit_message_text(
                f"🔤 <b>Verbotene Worte ({len(word_list)}):</b>\n\n{words_text}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )

    # === WARN CONFIG MENU ===
    elif data == "menu_warns":
        bot_data = load_data()
        wc = bot_data.get("warn_config", {"max_warns": 3, "punishment": "mute"})
        max_w = wc.get("max_warns", 3)
        punishment = wc.get("punishment", "mute")
        warned_count = len(bot_data.get("warnings", {}))
        punishment_labels = {"aus": "❌ Aus", "kick": "❗ Kick", "mute": "📛 Mute", "ban": "🚫 Ban"}
        p_label = punishment_labels.get(punishment, punishment)
        keyboard = [
            [InlineKeyboardButton("📋 Liste der verwarnten Nutzer", callback_data="warn_list")],
            [InlineKeyboardButton("❌ Aus", callback_data="warn_set_aus"),
             InlineKeyboardButton("❗ Kick", callback_data="warn_set_kick")],
            [InlineKeyboardButton("📛 Mute", callback_data="warn_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="warn_set_ban")],
            [InlineKeyboardButton("📛 🕐 Dauer der Schreibsperre", callback_data="warn_mute_dur_menu")],
        ]
        # Max warns row
        warn_row = []
        for n in range(2, 7):
            label = f"{n} ✅" if n == max_w else str(n)
            warn_row.append(InlineKeyboardButton(label, callback_data=f"warn_max_{n}"))
        keyboard.append(warn_row)
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])
        await query.edit_message_text(
            f"❗ <b>Verwarnungen von Benutzern</b>\n\n"
            f"Das Verwarnungssystem ermöglicht es, Verwarnungen an Benutzer für "
            f"unangemessenes Verhalten in der Gruppe zu erteilen, und zwar noch vor der eigentlichen Bestrafung.\n\n"
            f"In diesem Menü kann folgendes eingestellt werden:\n"
            f"• die Art der <b>Bestrafung</b> für jene Benutzer, die die maximal zulässige Anzahl von Verwarnungen überschreiten\n"
            f"• die <b>maximale Anzahl</b> der zugelassenen Verwarnungen\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Erlaubte Verwarnungen:</b> {max_w}\n"
            f"<b>Verwarnte Nutzer:</b> {warned_count}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("warn_set_"):
        punishment_val = data.replace("warn_set_", "")
        bot_data = load_data()
        bot_data.setdefault("warn_config", {})["punishment"] = punishment_val
        save_data(bot_data)
        await query.answer(f"Bestrafung auf {punishment_val} gesetzt ✅")
        # Inline re-render of warn menu
        wc = bot_data.get("warn_config", {"max_warns": 3, "punishment": "mute"})
        max_w = wc.get("max_warns", 3)
        warned_count = len(bot_data.get("warnings", {}))
        punishment_labels = {"aus": "❌ Aus", "kick": "❗ Kick", "mute": "📛 Mute", "ban": "🚫 Ban"}
        p_label = punishment_labels.get(punishment_val, punishment_val)
        keyboard = [
            [InlineKeyboardButton("📋 Liste der verwarnten Nutzer", callback_data="warn_list")],
            [InlineKeyboardButton("❌ Aus", callback_data="warn_set_aus"),
             InlineKeyboardButton("❗ Kick", callback_data="warn_set_kick")],
            [InlineKeyboardButton("📛 Mute", callback_data="warn_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="warn_set_ban")],
            [InlineKeyboardButton("📛 🕐 Dauer der Schreibsperre", callback_data="warn_mute_dur_menu")],
        ]
        warn_row = []
        for n in range(2, 7):
            label = f"{n} ✅" if n == max_w else str(n)
            warn_row.append(InlineKeyboardButton(label, callback_data=f"warn_max_{n}"))
        keyboard.append(warn_row)
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])
        await query.edit_message_text(
            f"❗ <b>Verwarnungen von Benutzern</b>\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Erlaubte Verwarnungen:</b> {max_w}\n"
            f"<b>Verwarnte Nutzer:</b> {warned_count}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("warn_max_"):
        max_w = int(data.replace("warn_max_", ""))
        bot_data = load_data()
        bot_data.setdefault("warn_config", {})["max_warns"] = max_w
        save_data(bot_data)
        await query.answer(f"Max Warns auf {max_w} gesetzt ✅")
        wc = bot_data.get("warn_config", {"max_warns": 3, "punishment": "mute"})
        punishment = wc.get("punishment", "mute")
        warned_count = len(bot_data.get("warnings", {}))
        punishment_labels = {"aus": "❌ Aus", "kick": "❗ Kick", "mute": "📛 Mute", "ban": "🚫 Ban"}
        p_label = punishment_labels.get(punishment, punishment)
        keyboard = [
            [InlineKeyboardButton("📋 Liste der verwarnten Nutzer", callback_data="warn_list")],
            [InlineKeyboardButton("❌ Aus", callback_data="warn_set_aus"),
             InlineKeyboardButton("❗ Kick", callback_data="warn_set_kick")],
            [InlineKeyboardButton("📛 Mute", callback_data="warn_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="warn_set_ban")],
            [InlineKeyboardButton("📛 🕐 Dauer der Schreibsperre", callback_data="warn_mute_dur_menu")],
        ]
        warn_row = []
        for n in range(2, 7):
            label = f"{n} ✅" if n == max_w else str(n)
            warn_row.append(InlineKeyboardButton(label, callback_data=f"warn_max_{n}"))
        keyboard.append(warn_row)
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])
        await query.edit_message_text(
            f"❗ <b>Verwarnungen von Benutzern</b>\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Erlaubte Verwarnungen:</b> {max_w}\n"
            f"<b>Verwarnte Nutzer:</b> {warned_count}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "warn_mute_dur_menu":
        bot_data = load_data()
        wc = bot_data.get("warn_config", {})
        current_secs = wc.get("mute_duration_seconds", 0)
        if current_secs > 0:
            current_label = format_duration_human(current_secs)
        else:
            current_label = "Inaktiv"
        keyboard = [
            [InlineKeyboardButton("❌ Abbrechen", callback_data="menu_warns")],
        ]
        await query.edit_message_text(
            f"Sende jetzt die eingestellte Bestrafungsdauer (Mute)\n\n"
            f"<b>Minimum:</b> 30 Sekunden\n"
            f"<b>Maximum:</b> 365 Tage\n\n"
            f"<b>Korrektes Eingabe-Format:</b>\n"
            f"<code>1 month 1 day 2 days 1 hour 3 hours 1 minute 4 minutes 1 second 1 30seconds</code>\n"
            f"<b>oder</b> <code>3M 2d 12h 4m 34s</code>\n\n"
            f"<b>Aktuelle Dauer:</b> {current_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        context.user_data["state"] = WAITING_WARN_MUTE_DUR

    elif data == "warn_list":
        bot_data = load_data()
        warnings = bot_data.get("warnings", {})
        if not warnings:
            keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_warns")]]
            await query.edit_message_text(
                "📋 <b>Keine verwarnten Nutzer.</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
            return
        text = "📋 <b>Verwarnte Nutzer:</b>\n\n"
        wc = bot_data.get("warn_config", {"max_warns": 3})
        max_w = wc.get("max_warns", 3)
        for uid, warn_data in list(warnings.items())[:20]:
            count = warn_data.get("count", 0)
            name = warn_data.get("name", uid)
            text += f"• <b>{html.escape(name)}</b> (<code>{uid}</code>) — {count}/{max_w}\n"
        if len(warnings) > 20:
            text += f"\n… und {len(warnings) - 20} weitere"
        keyboard = [
            [InlineKeyboardButton("🗑 Alle Warns löschen", callback_data="warn_clear_confirm")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_warns")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "warn_clear_confirm":
        keyboard = [
            [InlineKeyboardButton("✅ Ja, alle löschen", callback_data="warn_clear"),
             InlineKeyboardButton("❌ Abbrechen", callback_data="warn_list")],
        ]
        await query.edit_message_text("⚠️ Wirklich ALLE Verwarnungen löschen?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "warn_clear":
        bot_data = load_data()
        bot_data["warnings"] = {}
        save_data(bot_data)
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_warns")]]
        await query.edit_message_text("✅ Alle Verwarnungen gelöscht.", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("warn_undo_"):
        parts = data.replace("warn_undo_", "").split("_")
        chat_id_str = parts[0]
        target_id_str = parts[1]
        target_id = int(target_id_str)
        bot_data = load_data()
        warnings = bot_data.get("warnings", {})
        key = f"{chat_id_str}_{target_id_str}"
        tracked = lookup_user(target_id_str)
        uname = ""
        if tracked:
            uname = f"@{tracked['username']}" if tracked.get("username") else tracked.get("name", target_id_str)
        else:
            uname = target_id_str
        if key in warnings:
            warnings[key]["count"] = max(0, warnings[key].get("count", 1) - 1)
            new_count = warnings[key]["count"]
            if warnings[key]["count"] == 0:
                warnings.pop(key)
            save_data(bot_data)
        else:
            new_count = 0
        wc = bot_data.get("warn_config", {"max_warns": 3})
        max_w = wc.get("max_warns", 3)
        keyboard = [[InlineKeyboardButton("+1", callback_data=f"warn_add1_{chat_id_str}_{target_id_str}")]]
        try:
            if new_count == 0:
                await query.edit_message_text(
                    f"{uname} [{target_id}] hat keine Verwarnungen mehr.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await query.edit_message_text(
                    f"{uname} [{target_id}] hat {new_count} von {max_w} Verwarnungen erhalten",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("-1", callback_data=f"warn_undo_{chat_id_str}_{target_id_str}"),
                         InlineKeyboardButton("+1", callback_data=f"warn_add1_{chat_id_str}_{target_id_str}")],
                        [InlineKeyboardButton("Verwarnungen auf Null setzen", callback_data=f"warn_reset_{chat_id_str}_{target_id_str}")],
                    ]),
                )
        except Exception:
            pass

    elif data.startswith("warn_add1_"):
        parts = data.replace("warn_add1_", "").split("_")
        chat_id_str = parts[0]
        target_id_str = parts[1]
        target_id = int(target_id_str)
        bot_data = load_data()
        warnings = bot_data.setdefault("warnings", {})
        key = f"{chat_id_str}_{target_id_str}"
        tracked = lookup_user(target_id_str)
        uname = ""
        t_name = target_id_str
        t_username = None
        if tracked:
            t_name = tracked.get("name", target_id_str)
            t_username = tracked.get("username")
            uname = f"@{t_username}" if t_username else t_name
        else:
            uname = target_id_str
        warn_entry = warnings.get(key, {"count": 0, "name": t_name, "username": t_username})
        warn_entry["count"] = warn_entry.get("count", 0) + 1
        warnings[key] = warn_entry
        save_data(bot_data)
        wc = bot_data.get("warn_config", {"max_warns": 3, "punishment": "mute"})
        max_w = wc.get("max_warns", 3)
        current_count = warn_entry["count"]
        if current_count >= max_w:
            punishment = wc.get("punishment", "aus")
            if punishment and punishment != "aus":
                chat_id_val = int(chat_id_str)
                action_label = ""
                result_text = f"{uname} [{target_id}] wurde verwarnt zum {current_count}. Mal (von {max_w})."
                try:
                    if punishment == "ban":
                        await context.bot.ban_chat_member(chat_id=chat_id_val, user_id=target_id, revoke_messages=True)
                        remember_group_ban([chat_id_val], target_id, t_name, t_username)
                        action_label = "• <b>Aktion:</b> Gebannt 🚫"
                    elif punishment == "kick":
                        await context.bot.ban_chat_member(chat_id=chat_id_val, user_id=target_id)
                        await context.bot.unban_chat_member(chat_id=chat_id_val, user_id=target_id)
                        action_label = "• <b>Aktion:</b> Gekickt ❗"
                    elif punishment == "mute":
                        mute_secs = wc.get("mute_duration_seconds", wc.get("mute_duration_hours", 5) * 3600)
                        until_date = now_de() + datetime.timedelta(seconds=mute_secs)
                        await context.bot.restrict_chat_member(
                            chat_id=chat_id_val, user_id=target_id,
                            permissions=ChatPermissions.no_permissions(),
                            until_date=until_date,
                        )
                        until_str = until_date.strftime("%d.%m.%y um %H:%M")
                        action_label = f"• <b>Aktion:</b> Stummgeschaltet 🤫\n• <b>Bis:</b> {until_str}"
                except Exception as e:
                    action_label = f"• ⚠️ Fehler: {e}"
                result_text += f"\n{action_label}"
                warnings.pop(f"{chat_id_str}_{target_id_str}", None)
                save_data(bot_data)
                await query.edit_message_text(result_text, parse_mode="HTML")
                await log_action(context, f"WARN AUTO-PUNISH ({punishment}): {t_name} ({target_id}) von {query.from_user.full_name}")
            else:
                keyboard = [
                    [InlineKeyboardButton("🚫 Ban", callback_data=f"warn_punish_ban_{chat_id_str}_{target_id_str}"),
                     InlineKeyboardButton("❗ Kick", callback_data=f"warn_punish_kick_{chat_id_str}_{target_id_str}"),
                     InlineKeyboardButton("📛 Mute", callback_data=f"warn_punish_mute_{chat_id_str}_{target_id_str}")],
                    [InlineKeyboardButton("-1", callback_data=f"warn_undo_{chat_id_str}_{target_id_str}")],
                    [InlineKeyboardButton("Verwarnungen auf Null setzen", callback_data=f"warn_reset_{chat_id_str}_{target_id_str}")],
                ]
                await query.edit_message_text(
                    f" [{target_id}] hat das Limit von {max_w} Verwarnungen erreicht. Was willst Du tun?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
        else:
            keyboard = [
                [InlineKeyboardButton("-1", callback_data=f"warn_undo_{chat_id_str}_{target_id_str}"),
                 InlineKeyboardButton("+1", callback_data=f"warn_add1_{chat_id_str}_{target_id_str}")],
                [InlineKeyboardButton("Verwarnungen auf Null setzen", callback_data=f"warn_reset_{chat_id_str}_{target_id_str}")],
            ]
            await query.edit_message_text(
                f"{uname} [{target_id}] hat {current_count} von {max_w} Verwarnungen erhalten",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    elif data.startswith("warn_reset_"):
        parts = data.replace("warn_reset_", "").split("_")
        chat_id_str = parts[0]
        target_id_str = parts[1]
        target_id = int(target_id_str)
        bot_data = load_data()
        warnings = bot_data.get("warnings", {})
        key = f"{chat_id_str}_{target_id_str}"
        warnings.pop(key, None)
        save_data(bot_data)
        tracked = lookup_user(target_id_str)
        uname = ""
        if tracked:
            uname = f"@{tracked['username']}" if tracked.get("username") else tracked.get("name", target_id_str)
        else:
            uname = target_id_str
        keyboard = [[InlineKeyboardButton("+1", callback_data=f"warn_add1_{chat_id_str}_{target_id_str}")]]
        await query.edit_message_text(
            f"{uname} [{target_id}] hat keine Verwarnungen mehr.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("warn_punish_"):
        rest = data.replace("warn_punish_", "")
        parts = rest.split("_")
        action = parts[0]
        chat_id_val = int(parts[1])
        target_id = int(parts[2])
        tracked = lookup_user(str(target_id))
        t_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        t_username = tracked.get("username") if tracked else None
        result_text = ""
        # Admin-Schutz
        if await is_chat_admin(context, chat_id_val, target_id):
            await query.answer("⛔ Dieser User ist ein Administrator und kann nicht bestraft werden.", show_alert=True)
            return

        try:
            if action == "ban":
                await context.bot.ban_chat_member(chat_id=chat_id_val, user_id=target_id, revoke_messages=True)
                remember_group_ban([chat_id_val], target_id, t_name, t_username)
                result_text = f"🚫 [{target_id}] wurde gebannt."
            elif action == "kick":
                await context.bot.ban_chat_member(chat_id=chat_id_val, user_id=target_id)
                await context.bot.unban_chat_member(chat_id=chat_id_val, user_id=target_id)
                result_text = f"❗ [{target_id}] wurde gekickt."
            elif action == "mute":
                await context.bot.restrict_chat_member(
                    chat_id=chat_id_val, user_id=target_id,
                    permissions=ChatPermissions.no_permissions(),
                )
                result_text = f"📛 [{target_id}] wurde gemutet."
        except Exception as e:
            result_text = f"⚠️ Fehler: {e}"
        # Reset warns
        bot_data = load_data()
        warnings = bot_data.get("warnings", {})
        warnings.pop(f"{chat_id_val}_{target_id}", None)
        save_data(bot_data)
        await query.edit_message_text(result_text)
        await log_action(context, f"WARN PUNISH ({action}): {t_name} ({target_id}) von {query.from_user.full_name}")

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
        import time
        broadcast_id = str(int(time.time() * 1000))
        sent_msgs = []
        for gid in groups:
            try:
                msg = await context.bot.send_message(
                    chat_id=gid,
                    text=update.message.text_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
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
            raw_text = update.message.text or ""
            html_text = getattr(update.message, "text_html", None) or raw_text
            bot_data = load_data()
            updated = False
            for s in bot_data.get("scheduled", []):
                if str(s.get("id")) == str(sched_id):
                    s["text"] = raw_text
                    s["text_html"] = html_text
                    save_data(bot_data)
                    updated = True
                    logger.info(f"Scheduled text saved for {sched_id}: {raw_text[:80]}")
                    break
            keyboard = [[InlineKeyboardButton("🔙 Zurück zur Nachricht", callback_data=f"sched_view_{sched_id}")]]
            await update.message.reply_text(
                "✅ Nachricht aktualisiert." if updated else "⚠️ Nachricht nicht gefunden.",
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

    elif state == WAITING_PCMD_NAME:
        pending = user_data_store.get(user_id)
        if not pending:
            await update.message.reply_text("Bitte starte mit /start.")
            return
        cmd_name = text.lower().strip().lstrip("/")
        if not cmd_name or not cmd_name.isalnum():
            await update.message.reply_text("⚠️ Der Name darf nur Buchstaben und Zahlen enthalten.")
            return
        pending["cmd_name"] = cmd_name
        pending["action"] = "pcmd_add_text"
        user_data_store[user_id] = pending
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="pcmd_menu")]]
        await update.message.reply_text(
            f"✅ Befehlname: /<b>{html.escape(cmd_name)}</b>\n\n"
            f"Sende mir jetzt die Antwort-Nachricht.\n"
            f"<i>Formatierung wird übernommen. Medien (Fotos, Videos, Sticker) auch.</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        context.user_data["state"] = WAITING_PCMD_TEXT

    elif state == WAITING_PCMD_TEXT:
        pending = user_data_store.get(user_id)
        if not pending or "cmd_name" not in pending:
            await update.message.reply_text("Bitte starte mit /start.")
            return
        cmd_name = pending["cmd_name"]
        cmd_groups = pending.get("groups", [])
        bot_data = load_data()
        new_entry = {
            "text": update.message.text or "",
            "text_html": update.message.text_html or update.message.text or "",
            "created_by": user_id,
            "created_at": now_de().strftime("%d.%m.%Y %H:%M"),
            "groups": cmd_groups,
        }
        cmds = bot_data.setdefault("personal_commands", {})
        existing = cmds.get(cmd_name, [])
        if not isinstance(existing, list):
            existing = [existing]
        existing.append(new_entry)
        cmds[cmd_name] = existing
        save_data(bot_data)
        context.user_data["state"] = None
        user_data_store.pop(user_id, None)
        grp_count = len(cmd_groups)
        grp_text = f" für {grp_count} Gruppen" if cmd_groups else " (alle Gruppen)"
        keyboard = [[InlineKeyboardButton("🔙 Zurück zu Befehle", callback_data="pcmd_menu")]]
        await update.message.reply_text(
            f"✅ Befehl /<b>{html.escape(cmd_name)}</b> gespeichert{grp_text}!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif state == WAITING_WARN_MUTE_DUR:
        text_input = update.message.text.strip()
        total_secs = parse_duration_text(text_input)
        if total_secs < 30:
            await update.message.reply_text("⚠️ Minimum ist 30 Sekunden. Bitte erneut eingeben.")
            return
        if total_secs > 365 * 86400:
            await update.message.reply_text("⚠️ Maximum ist 365 Tage. Bitte erneut eingeben.")
            return
        bot_data = load_data()
        bot_data.setdefault("warn_config", {})["mute_duration_seconds"] = total_secs
        save_data(bot_data)
        context.user_data["state"] = None
        label = format_duration_human(total_secs)
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_warns")]]
        await update.message.reply_text(
            f"✅ Mute-Dauer auf <b>{label}</b> gesetzt!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif state == WAITING_BADWORD_ADD:
        text_input = update.message.text.strip()
        words = [w.strip() for w in text_input.splitlines() if w.strip()]
        if not words:
            await update.message.reply_text("⚠️ Bitte sende mindestens ein Wort.")
            return
        bot_data = load_data()
        word_list = bot_data.setdefault("badwords", [])
        added = []
        for w in words:
            if w.lower() not in [x.lower() for x in word_list]:
                word_list.append(w)
                added.append(w)
        save_data(bot_data)
        context.user_data["state"] = None
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_badwords")]]
        if added:
            await update.message.reply_text(
                f"✅ Hinzugefügt: <code>{html.escape(', '.join(added))}</code>\n"
                f"Insgesamt: {len(word_list)} verbotene Worte",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "⚠️ Alle Worte waren bereits vorhanden.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

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

# --- /info ---

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user info card with group-specific moderation buttons and separate BanALL."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    tracked = lookup_user(str(target_id))
    username = tracked.get("username") if tracked else None

    try:
        chat_info = await context.bot.get_chat(target_id)
        bio = chat_info.bio or "—"
        has_photo = chat_info.photo is not None
        first_name = chat_info.first_name or ""
        last_name = chat_info.last_name or ""
        full_name = f"{first_name} {last_name}".strip() or target_name
        if chat_info.username:
            username = chat_info.username
    except Exception:
        bio = "—"
        has_photo = False
        full_name = target_name

    scope_chat_id = get_info_scope_chat_id(update)
    groups = await get_info_banall_groups(context, scope_chat_id)
    banned_in = sum(1 for g in groups if is_banned_in_group(g["id"], target_id))
    is_banned_all = bool(groups) and banned_in == len(groups)

    group_state = await get_info_group_state(context, scope_chat_id, target_id)
    is_muted = group_state["is_muted"]
    is_banned_local = group_state["is_banned_local"]
    is_premium = group_state["is_premium"]

    name_display = f"<a href='tg://user?id={target_id}'>{html.escape(full_name)}</a>"
    username_display = f"@{username}" if username else "—"
    photo_icon = "✅" if has_photo else "❌"
    premium_icon = "⭐ Ja" if is_premium else "Nein"
    ban_status = "🚫 In dieser Gruppe gebannt" if is_banned_local else "✅ Nicht gebannt in dieser Gruppe"
    if not scope_chat_id:
        ban_status = f"🚫 {banned_in}/{len(groups)} Gruppen" if banned_in > 0 else "✅ Nicht gebannt"

    tracked_data = lookup_user(str(target_id))
    if scope_chat_id and tracked_data and str(scope_chat_id) in tracked_data.get("group_stats", {}):
        gs = tracked_data["group_stats"][str(scope_chat_id)]
        msg_count = gs.get("msg_count", 0)
        first_seen = gs.get("first_seen", "—")
    else:
        total = 0
        first_seen = "—"
        if tracked_data:
            for gs in tracked_data.get("group_stats", {}).values():
                total += gs.get("msg_count", 0)
                if first_seen == "—":
                    first_seen = gs.get("first_seen", "—")
        msg_count = total

    freed_icon = "🔓 Ja" if is_freed(target_id) else "Nein"

    info_text = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID:</b> <code>{target_id}</code> <code>#id{target_id}</code>\n"
        f"👤 <b>Name:</b> {name_display}\n"
        f"🔗 <b>Username:</b> {username_display}\n"
        f"📷 <b>Profilbild:</b> {photo_icon}\n"
        f"⭐ <b>Premium:</b> {premium_icon}\n"
        f"🔓 <b>Befreiter:</b> {freed_icon}\n"
        f"📝 <b>Bio:</b> {html.escape(bio[:100]) if bio != '—' else '—'}\n"
        f"💬 <b>Nachrichten:</b> {msg_count}\n"
        f"📅 <b>Erste Nachricht:</b> {first_seen}\n"
        f"🚫 <b>Ban-Status:</b> {ban_status}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    await update.message.reply_text(
        info_text,
        reply_markup=build_info_keyboard(scope_chat_id, target_id, is_muted, is_banned_local, is_banned_all),
        parse_mode="HTML",
    )

# --- /mute ---

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mute a user in the group. Usage: /mute [reason] (reply to a message)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    # Admin-Schutz
    if await is_chat_admin(context, chat.id, target_id):
        await update.message.reply_text("⛔ Dieser User ist ein Administrator — Mute ist nicht möglich.")
        return

    args = list(context.args) if context.args else []
    # Strip the first arg if it was used to resolve the target (@username or numeric ID)
    if args and not update.message.reply_to_message:
        args = args[1:]  # first arg was the target
    elif args and update.message.reply_to_message and (args[0].startswith("@") or args[0].isdigit()):
        args = args[1:]
    reason = " ".join(args) if args else None

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target_id,
            permissions=ChatPermissions.no_permissions(),
        )

        # Look up username
        tracked = lookup_user(str(target_id))
        target_username = tracked.get("username") if tracked else None
        uname = f"@{target_username} " if target_username else ""

        reason_text = f"\n📝 <b>Grund:</b> {html.escape(reason)}" if reason else ""

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🕹 Rechte", url=f"tg://resolve?domain={chat.username}&admin={target_id}" if chat.username else f"tg://chat_permissions?chat_id={str(chat.id).replace('-100', '')}"),
                InlineKeyboardButton("✅ Unmute", callback_data=f"cmd_unmute_{chat.id}_{target_id}"),
            ]
        ])

        await update.message.reply_text(
            f"{uname}[<code>{target_id}</code>] wurde 🔇 stummgeschaltet.{reason_text}",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, f"🔇 Mute: {target_name} [{target_id}] in {chat.title} von {update.effective_user.first_name}" + (f" | Grund: {reason}" if reason else ""))
    except Exception as e:
        await update.message.reply_text(f"❌ Mute fehlgeschlagen: {e}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unmute a user in the group. Usage: /unmute (reply to a message)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    try:
        chat_obj = await context.bot.get_chat(chat.id)
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target_id,
            permissions=chat_obj.permissions or ChatPermissions.all_permissions(),
        )

        tracked = lookup_user(str(target_id))
        target_username = tracked.get("username") if tracked else None
        uname = f"@{target_username} " if target_username else ""

        await update.message.reply_text(
            f"{uname}[<code>{target_id}</code>] wurde ✅ entmutet.",
            parse_mode="HTML",
        )
        await log_action(context, f"✅ Unmute: {target_name} [{target_id}] in {chat.title} von {update.effective_user.first_name}")
    except Exception as e:
        await update.message.reply_text(f"❌ Unmute fehlgeschlagen: {e}")

# --- /kick ---

async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kick a user from the group (they can rejoin). Usage: /kick [reason] (reply to a message)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    # Admin-Schutz
    if await is_chat_admin(context, chat.id, target_id):
        await update.message.reply_text("⛔ Dieser User ist ein Administrator — Kick ist nicht möglich.")
        return

    args = list(context.args) if context.args else []
    if args and not update.message.reply_to_message:
        args = args[1:]
    elif args and update.message.reply_to_message and (args[0].startswith("@") or args[0].isdigit()):
        args = args[1:]
    reason = " ".join(args) if args else None

    try:
        # Ban and immediately unban = kick (user can rejoin)
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_id)
        await context.bot.unban_chat_member(chat_id=chat.id, user_id=target_id, only_if_banned=True)

        reason_text = f"\n📝 <b>Grund:</b> {html.escape(reason)}" if reason else ""
        await update.message.reply_text(
            f"👢 <b>{html.escape(target_name)}</b> wurde gekickt!{reason_text}\n\n"
            f"ℹ️ Der User kann der Gruppe wieder beitreten.",
            parse_mode="HTML",
        )
        await log_action(context, f"👢 Kick: {target_name} [{target_id}] aus {chat.title} von {update.effective_user.first_name}" + (f" | Grund: {reason}" if reason else ""))
    except Exception as e:
        await update.message.reply_text(f"❌ Kick fehlgeschlagen: {e}")

# --- /ban (single group) ---

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user from the current group. Usage: /ban [reason] (reply to a message)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    # Admin-Schutz
    if await is_chat_admin(context, chat.id, target_id):
        await update.message.reply_text("⛔ Dieser User ist ein Administrator — Ban ist nicht möglich.")
        return

    args = list(context.args) if context.args else []
    if args and not update.message.reply_to_message:
        args = args[1:]
    elif args and update.message.reply_to_message and (args[0].startswith("@") or args[0].isdigit()):
        args = args[1:]
    reason = " ".join(args) if args else None

    tracked = lookup_user(str(target_id))
    target_username = tracked.get("username") if tracked else None

    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_id, revoke_messages=True)
        remember_group_ban([chat.id], target_id, target_name, target_username)

        uname = f"@{target_username} " if target_username else ""
        reason_text = f"\n📝 <b>Grund:</b> {html.escape(reason)}" if reason else ""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Unban", callback_data=f"cmd_unban_{chat.id}_{target_id}")]
        ])
        await update.message.reply_text(
            f"🚫 {uname}[<code>{target_id}</code>] wurde gebannt!{reason_text}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await log_action(context, f"🚫 Ban: {target_name} [{target_id}] in {chat.title} von {update.effective_user.first_name}" + (f" | Grund: {reason}" if reason else ""))
    except Exception as e:
        await update.message.reply_text(f"❌ Ban fehlgeschlagen: {e}")

# --- /banall ---

# --- /warn ---

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Warn a user. Usage: /warn [reason] (reply to a message)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    # Admins/Creators dürfen nicht verwarnt werden
    if await is_chat_admin(context, chat.id, target_id):
        await update.message.reply_text("⛔ Dieser User ist ein Administrator — Warn, Mute und Ban sind nicht möglich.")
        return

    args = list(context.args) if context.args else []
    if args and not update.message.reply_to_message:
        args = args[1:]
    elif args and update.message.reply_to_message and (args[0].startswith("@") or args[0].isdigit()):
        args = args[1:]
    reason = " ".join(args) if args else None

    tracked = lookup_user(str(target_id))
    target_username = tracked.get("username") if tracked else None

    bot_data = load_data()
    wc = bot_data.get("warn_config", {"max_warns": 3, "punishment": "mute"})
    max_warns = wc.get("max_warns", 3)
    punishment = wc.get("punishment", "mute")

    warnings = bot_data.setdefault("warnings", {})
    key = f"{chat.id}_{target_id}"
    warn_entry = warnings.get(key, {"count": 0, "name": target_name, "username": target_username})
    warn_entry["count"] = warn_entry.get("count", 0) + 1
    warn_entry["name"] = target_name
    warn_entry["username"] = target_username
    warnings[key] = warn_entry
    save_data(bot_data)

    current_count = warn_entry["count"]
    uname = f"@{target_username}" if target_username else target_name

    text = f"{uname} [{target_id}] wurde verwarnt zum {current_count}. Mal (von {max_warns})."
    if reason:
        text += f"\n<b>Grund:</b> {html.escape(reason)}"

    # Check if max warns reached
    if current_count >= max_warns:
        punishment = wc.get("punishment", "mute")
        if punishment and punishment != "aus":
            # Auto-execute configured punishment
            action_label = ""
            result_text = f"{uname} [{target_id}] wurde verwarnt zum {current_count}. Mal (von {max_warns})."
            try:
                if punishment == "ban":
                    await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_id, revoke_messages=True)
                    remember_group_ban([chat.id], target_id, target_name, target_username)
                    action_label = "• <b>Aktion:</b> Gebannt 🚫"
                elif punishment == "kick":
                    await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_id)
                    await context.bot.unban_chat_member(chat_id=chat.id, user_id=target_id)
                    action_label = "• <b>Aktion:</b> Gekickt ❗"
                elif punishment == "mute":
                    mute_secs = wc.get("mute_duration_seconds", wc.get("mute_duration_hours", 5) * 3600)
                    until_date = now_de() + datetime.timedelta(seconds=mute_secs)
                    await context.bot.restrict_chat_member(
                        chat_id=chat.id, user_id=target_id,
                        permissions=ChatPermissions.no_permissions(),
                        until_date=until_date,
                    )
                    until_str = until_date.strftime("%d.%m.%y um %H:%M")
                    action_label = f"• <b>Aktion:</b> Stummgeschaltet 🤫\n• <b>Bis:</b> {until_str}"
            except Exception as e:
                action_label = f"• ⚠️ Fehler: {e}"
            result_text += f"\n{action_label}"
            if reason:
                result_text += f"\n<b>Grund:</b> {html.escape(reason)}"
            # Reset warns after punishment
            warnings.pop(f"{chat.id}_{target_id}", None)
            save_data(bot_data)
            await update.message.reply_text(result_text, parse_mode="HTML")
            await log_action(context, f"WARN AUTO-PUNISH ({punishment}): {target_name} ({target_id}) in {chat.title} — {current_count}/{max_warns}" + (f" Grund: {reason}" if reason else ""))
            return
        else:
            # No punishment configured — show choice
            text = f" [{target_id}] hat das Limit von {max_warns} Verwarnungen erreicht. Was willst Du tun?"
            if reason:
                text += f"\n<b>Grund:</b> {html.escape(reason)}"
            keyboard = [
                [InlineKeyboardButton("🚫 Ban", callback_data=f"warn_punish_ban_{chat.id}_{target_id}"),
                 InlineKeyboardButton("❗ Kick", callback_data=f"warn_punish_kick_{chat.id}_{target_id}"),
                 InlineKeyboardButton("📛 Mute", callback_data=f"warn_punish_mute_{chat.id}_{target_id}")],
                [InlineKeyboardButton("-1", callback_data=f"warn_undo_{chat.id}_{target_id}")],
                [InlineKeyboardButton("Verwarnungen auf Null setzen", callback_data=f"warn_reset_{chat.id}_{target_id}")],
            ]
    else:
        keyboard = [
            [InlineKeyboardButton("-1", callback_data=f"warn_undo_{chat.id}_{target_id}"),
             InlineKeyboardButton("+1", callback_data=f"warn_add1_{chat.id}_{target_id}")],
            [InlineKeyboardButton("Verwarnungen auf Null setzen", callback_data=f"warn_reset_{chat.id}_{target_id}")],
        ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    await log_action(context, f"WARN: {target_name} ({target_id}) in {chat.title} — {current_count}/{max_warns}" + (f" Grund: {reason}" if reason else ""))


async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a warn from a user. Usage: /unwarn (reply to a message)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    bot_data = load_data()
    warnings = bot_data.get("warnings", {})
    key = f"{chat.id}_{target_id}"
    if key not in warnings or warnings[key].get("count", 0) == 0:
        await update.message.reply_text(f"ℹ️ {target_name} hat keine Verwarnungen in dieser Gruppe.")
        return

    warnings[key]["count"] = max(0, warnings[key]["count"] - 1)
    new_count = warnings[key]["count"]
    if new_count == 0:
        warnings.pop(key)
    save_data(bot_data)

    wc = bot_data.get("warn_config", {"max_warns": 3})
    max_w = wc.get("max_warns", 3)
    await update.message.reply_text(f"✅ Verwarnung von {target_name} entfernt. ({new_count}/{max_w})")
    await log_action(context, f"UNWARN: {target_name} ({target_id}) in {chat.title} — jetzt {new_count}/{max_w}")


# --- /free & /unfree ---

async def free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant a user the 'Befreiter' role — exempt from link filter, forward filter, forbidden words."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    bot_data = load_data()
    freed = bot_data.setdefault("freed_users", [])
    if target_id in freed:
        await update.message.reply_text(f"ℹ️ {target_name} ist bereits befreit.")
        return

    freed.append(target_id)
    save_data(bot_data)

    tracked = lookup_user(str(target_id))
    target_username = tracked.get("username") if tracked else None
    uname = f"@{target_username} " if target_username else ""

    chat = update.effective_chat
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🕹 Rechte", url=f"tg://resolve?domain={chat.username}&admin={target_id}" if chat.username else f"tg://chat_permissions?chat_id={str(chat.id).replace('-100', '')}")]
    ])

    await update.message.reply_text(
        f"{uname}[<code>{target_id}</code>] wurde die Rolle 🔓 <b>Befreiter</b> erteilt.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await log_action(context, f"🔓 FREE: {target_name} [{target_id}] von {update.effective_user.first_name}")


async def unfree_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revoke the 'Befreiter' role from a user."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    bot_data = load_data()
    freed = bot_data.setdefault("freed_users", [])
    if target_id not in freed:
        await update.message.reply_text(f"ℹ️ {target_name} ist nicht befreit.")
        return

    freed.remove(target_id)
    save_data(bot_data)

    tracked = lookup_user(str(target_id))
    target_username = tracked.get("username") if tracked else None
    uname = f"@{target_username} " if target_username else ""

    await update.message.reply_text(
        f"{uname}[<code>{target_id}</code>] wurde die Rolle 🔒 <b>Befreiter</b> widerrufen.",
        parse_mode="HTML",
    )
    await log_action(context, f"🔒 UNFREE: {target_name} [{target_id}] von {update.effective_user.first_name}")


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
    successful_groups, failed_groups = await ban_user_in_groups(context, groups, target_id)
    success_count = len(successful_groups)
    fail_count = len(failed_groups)

    if successful_groups:
        remember_group_ban([g["id"] for g in successful_groups], target_id, target_name, target_username)

    lines = []
    if success_count:
        lines.append(f"✅ {target_id} in {success_count}/{len(groups)} Gruppen gebannt.")
    else:
        lines.append(f"⚠️ {target_id} konnte in keiner Gruppe gebannt werden.")

    if fail_count:
        for g in failed_groups[:8]:
            lines.append(f"❌ {g['title']} ({g['id']})")
        if fail_count > 8:
            lines.append(f"… und {fail_count - 8} weitere Fehler")

    await update.message.reply_text("\n".join(lines))
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

# --- /personal and /unpersonal commands (in groups) ---

async def personal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save a replied-to message as a personal command. Usage: /personal <name> (reply to a message)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Nutzung: /befehl <Name>\n"
            "Antwort auf eine Nachricht, die als Befehl gespeichert werden soll.\n\n"
            "Beispiel: Antworte auf eine Nachricht und schreibe /befehl hele",
        )
        return

    cmd_name = context.args[0].lower().lstrip("/")
    if not cmd_name.isalnum():
        await update.message.reply_text("⚠️ Der Name darf nur Buchstaben und Zahlen enthalten.")
        return

    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("⚠️ Antworte auf eine Nachricht, um sie als Befehl zu speichern.")
        return

    # Extract content from replied message
    cmd_data = {
        "created_by": user_id,
        "created_at": now_de().strftime("%d.%m.%Y %H:%M"),
        "groups": [update.effective_chat.id] if update.effective_chat and update.effective_chat.type in ("group", "supergroup") else [],
    }

    if reply.text:
        cmd_data["text"] = reply.text
        cmd_data["text_html"] = reply.text_html or reply.text
    elif reply.caption:
        cmd_data["text"] = reply.caption
        cmd_data["text_html"] = reply.caption_html or reply.caption

    # Save media if present
    if reply.photo:
        cmd_data["media_file_id"] = reply.photo[-1].file_id
        cmd_data["media_type"] = "photo"
    elif reply.video:
        cmd_data["media_file_id"] = reply.video.file_id
        cmd_data["media_type"] = "video"
    elif reply.animation:
        cmd_data["media_file_id"] = reply.animation.file_id
        cmd_data["media_type"] = "animation"
    elif reply.sticker:
        cmd_data["media_file_id"] = reply.sticker.file_id
        cmd_data["media_type"] = "sticker"
    elif reply.document:
        cmd_data["media_file_id"] = reply.document.file_id
        cmd_data["media_type"] = "document"

    if not cmd_data.get("text") and not cmd_data.get("media_file_id"):
        await update.message.reply_text("⚠️ Die Nachricht hat keinen speicherbaren Inhalt.")
        return

    bot_data = load_data()
    cmds = bot_data.setdefault("personal_commands", {})
    existing = cmds.get(cmd_name, [])
    if not isinstance(existing, list):
        existing = [existing]
    existing.append(cmd_data)
    cmds[cmd_name] = existing
    save_data(bot_data)

    grp_label = update.effective_chat.title if update.effective_chat and update.effective_chat.type in ("group", "supergroup") else "alle Gruppen"
    await update.message.reply_text(f"✅ Befehl /{cmd_name} gespeichert für [{grp_label}]!")
    await log_action(context, f"PERSONAL CMD: /{cmd_name} erstellt von {update.effective_user.full_name}")


async def unpersonal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a personal command. Usage: /unpersonal <name>"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Nutzung: /unbefehl <Name>\nBeispiel: /unpersonal hele")
        return

    cmd_name = context.args[0].lower().lstrip("/")
    bot_data = load_data()
    cmds = bot_data.get("personal_commands", {})
    if cmd_name not in cmds:
        await update.message.reply_text(f"⚠️ Befehl /{cmd_name} nicht gefunden.")
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    entries = cmds.get(cmd_name, [])
    if not isinstance(entries, list):
        entries = [entries]
    # Remove entry matching this group, or all if in DM
    if chat_id and any(chat_id in e.get("groups", []) for e in entries):
        entries = [e for e in entries if chat_id not in e.get("groups", [])]
    else:
        entries = []
    if entries:
        cmds[cmd_name] = entries
    else:
        cmds.pop(cmd_name, None)
    save_data(bot_data)
    await update.message.reply_text(f"✅ Befehl /{cmd_name} gelöscht!")
    await log_action(context, f"UNPERSONAL CMD: /{cmd_name} gelöscht von {update.effective_user.full_name}")


async def handle_custom_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom personal commands in groups."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text.startswith("/"):
        return

    # Extract command name (without / and without @botname)
    cmd = text.split()[0][1:].split("@")[0].lower()
    if not cmd:
        return

    bot_data = load_data()
    cmds = bot_data.get("personal_commands", {})
    if cmd not in cmds:
        return

    chat_id = update.effective_chat.id
    entries = cmds[cmd]
    if not isinstance(entries, list):
        entries = [entries]

    # Find matching entry: first check group-specific, then fallback to global (empty groups)
    cmd_data = None
    for e in entries:
        grps = e.get("groups", [])
        if grps and chat_id in grps:
            cmd_data = e
            break
    if cmd_data is None:
        for e in entries:
            if not e.get("groups", []):
                cmd_data = e
                break
    if cmd_data is None:
        return

    text_html = cmd_data.get("text_html", cmd_data.get("text", ""))
    media_fid = cmd_data.get("media_file_id")
    media_type = cmd_data.get("media_type", "photo")

    try:
        if media_fid:
            if media_type == "photo":
                await context.bot.send_photo(chat_id=chat_id, photo=media_fid, caption=text_html or None, parse_mode="HTML" if text_html else None)
            elif media_type == "video":
                await context.bot.send_video(chat_id=chat_id, video=media_fid, caption=text_html or None, parse_mode="HTML" if text_html else None)
            elif media_type == "animation":
                await context.bot.send_animation(chat_id=chat_id, animation=media_fid, caption=text_html or None, parse_mode="HTML" if text_html else None)
            elif media_type == "sticker":
                await context.bot.send_sticker(chat_id=chat_id, sticker=media_fid)
            else:
                await context.bot.send_document(chat_id=chat_id, document=media_fid, caption=text_html or None, parse_mode="HTML" if text_html else None)
        elif text_html:
            await context.bot.send_message(chat_id=chat_id, text=text_html, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Custom command /{cmd} failed in {chat_id}: {e}")

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
        track_user(update.message.from_user, group_id=update.effective_chat.id)

    # --- Command delete check ---
    msg_text_raw = update.message.text or ""
    if msg_text_raw and update.message.from_user:
        first_char = msg_text_raw[0] if msg_text_raw else ""
        if first_char in ["/", "!", ";", "."]:
            bot_data_cd = load_data()
            cd = bot_data_cd.get("cmd_delete", {"admin_prefixes": [], "user_prefixes": []})
            sender_cd = update.message.from_user
            is_adm = await is_chat_admin(context, update.effective_chat.id, sender_cd.id)
            prefixes = cd.get("admin_prefixes", []) if is_adm else cd.get("user_prefixes", [])
            if first_char in prefixes:
                try:
                    await update.message.delete()
                    logger.info(f"Auto-deleted command '{msg_text_raw[:30]}' from {'admin' if is_adm else 'user'} {sender_cd.id} in {update.effective_chat.id}")
                except Exception as e:
                    logger.error(f"Cmd delete failed: {e}")

    # --- Anti-Spam: Link check ---
    if update.message.from_user:
        sender_as = update.message.from_user
        # Check all entities for links
        all_entities = list(update.message.entities or []) + list(update.message.caption_entities or [])
        has_link = any(ent.type in ("url", "text_link") for ent in all_entities)
        if has_link:
            logger.info(f"LINK detected from {sender_as.id} in {update.effective_chat.id}")
            is_adm_as = is_authorized(sender_as.id) or await is_chat_admin(context, update.effective_chat.id, sender_as.id)
            if not is_adm_as and not is_freed(sender_as.id):
                bot_data_as = load_data()
                lc = bot_data_as.get("antispam_links", {"punishment": "aus", "delete": True})
                lc_punishment = lc.get("punishment", "aus")
                lc_delete = lc.get("delete", True)
                logger.info(f"Link config: punishment={lc_punishment}, delete={lc_delete}")
                if lc_punishment != "aus" or lc_delete:
                    chat_id_as = update.effective_chat.id
                    user_id_as = update.message.from_user.id
                    user_name_as = update.message.from_user.full_name
                    uname_as = f"@{update.message.from_user.username} " if update.message.from_user.username else ""
                    if lc_delete:
                        try:
                            await update.message.delete()
                        except Exception as e:
                            logger.error(f"Link delete failed: {e}")
                    try:
                        if lc_punishment == "warn":
                            wc = bot_data_as.get("warn_config", {"max_warns": 3})
                            max_w = wc.get("max_warns", 3)
                            warnings = bot_data_as.setdefault("warnings", {})
                            key = f"{chat_id_as}_{user_id_as}"
                            warn_entry = warnings.get(key, {"count": 0, "name": user_name_as})
                            warn_entry["count"] = warn_entry.get("count", 0) + 1
                            warn_entry["name"] = user_name_as
                            warnings[key] = warn_entry
                            save_data(bot_data_as)
                            # Check if warn limit reached
                            if warn_entry["count"] >= max_w:
                                warn_punishment = wc.get("punishment", "mute")
                                action_label_lw = ""
                                try:
                                    if warn_punishment == "ban":
                                        await context.bot.ban_chat_member(chat_id=chat_id_as, user_id=user_id_as, revoke_messages=True)
                                        remember_group_ban([chat_id_as], user_id_as, user_name_as, update.message.from_user.username)
                                        action_label_lw = "• <b>Aktion:</b> Gebannt 🚫"
                                    elif warn_punishment == "kick":
                                        await context.bot.ban_chat_member(chat_id=chat_id_as, user_id=user_id_as)
                                        await context.bot.unban_chat_member(chat_id=chat_id_as, user_id=user_id_as)
                                        action_label_lw = "• <b>Aktion:</b> Gekickt ❗"
                                    elif warn_punishment == "mute":
                                        mute_secs = wc.get("mute_duration_seconds", wc.get("mute_duration_hours", 5) * 3600)
                                        until_date = now_de() + datetime.timedelta(seconds=mute_secs)
                                        await context.bot.restrict_chat_member(
                                            chat_id=chat_id_as, user_id=user_id_as,
                                            permissions=ChatPermissions.no_permissions(),
                                            until_date=until_date,
                                        )
                                        until_str = until_date.strftime("%d.%m.%y um %H:%M")
                                        action_label_lw = f"• <b>Aktion:</b> Stummgeschaltet 🤫\n• <b>Bis:</b> {until_str}"
                                except Exception as e:
                                    action_label_lw = f"• ⚠️ Fehler: {e}"
                                # Reset warns after punishment
                                warnings.pop(key, None)
                                save_data(bot_data_as)
                                await context.bot.send_message(
                                    chat_id=chat_id_as,
                                    text=(
                                        f"{uname_as}[<code>{user_id_as}</code>] hat ohne Genehmigung einen 🔗 Link gesendet.\n"
                                        f"<b>Aktion:</b> Verwarnt ({max_w}/{max_w}) ❗\n{action_label_lw}"
                                    ),
                                    parse_mode="HTML",
                                )
                                await log_action(context, f"LINK-WARN AUTO-PUNISH ({warn_punishment}): {user_name_as} ({user_id_as}) in {chat_id_as} — {max_w}/{max_w}")
                            else:
                                keyboard_as = InlineKeyboardMarkup([
                                    [InlineKeyboardButton("❌ Abbrechen", callback_data=f"link_warn_cancel_{chat_id_as}_{user_id_as}")]
                                ])
                                await context.bot.send_message(
                                    chat_id=chat_id_as,
                                    text=(
                                        f"{uname_as}[<code>{user_id_as}</code>] hat ohne Genehmigung einen 🔗 Link gesendet.\n"
                                        f"<b>Aktion:</b> Verwarnt ({warn_entry['count']}/{max_w}) ❗"
                                    ),
                                    reply_markup=keyboard_as,
                                    parse_mode="HTML",
                                )
                        elif lc_punishment == "kick":
                            await context.bot.ban_chat_member(chat_id=chat_id_as, user_id=user_id_as)
                            await context.bot.unban_chat_member(chat_id=chat_id_as, user_id=user_id_as)
                            await context.bot.send_message(
                                chat_id=chat_id_as,
                                text=f"{uname_as}[<code>{user_id_as}</code>] hat ohne Genehmigung einen 🔗 Link gesendet.\n<b>Aktion:</b> Gekickt 👢",
                                parse_mode="HTML",
                            )
                        elif lc_punishment == "mute":
                            await context.bot.restrict_chat_member(chat_id=chat_id_as, user_id=user_id_as, permissions=ChatPermissions.no_permissions())
                            keyboard_as = InlineKeyboardMarkup([
                                [InlineKeyboardButton("✅ Unmute", callback_data=f"cmd_unmute_{chat_id_as}_{user_id_as}")]
                            ])
                            await context.bot.send_message(
                                chat_id=chat_id_as,
                                text=f"{uname_as}[<code>{user_id_as}</code>] hat ohne Genehmigung einen 🔗 Link gesendet.\n<b>Aktion:</b> Gemutet 🔇",
                                reply_markup=keyboard_as,
                                parse_mode="HTML",
                            )
                        elif lc_punishment == "ban":
                            await context.bot.ban_chat_member(chat_id=chat_id_as, user_id=user_id_as, revoke_messages=True)
                            await context.bot.send_message(
                                chat_id=chat_id_as,
                                text=f"{uname_as}[<code>{user_id_as}</code>] hat ohne Genehmigung einen 🔗 Link gesendet.\n<b>Aktion:</b> Gebannt 🚫",
                                parse_mode="HTML",
                            )
                    except Exception as e:
                        logger.error(f"Link punishment failed: {e}")
                    await log_action(context, f"LINK-SPAM: {user_name_as} ({user_id_as}) in {update.effective_chat.title} — Strafe: {lc_punishment}")
                    return

    # --- Anti-Spam: Forward check ---
    if update.message.forward_origin and update.message.from_user:
        if not is_authorized(update.message.from_user.id) and not is_freed(update.message.from_user.id):
            if not await is_chat_admin(context, update.effective_chat.id, update.message.from_user.id):
                bot_data_fw = load_data()
                fw = bot_data_fw.get("antispam_forward", {})
                origin = update.message.forward_origin
                should_delete = False
                origin_type = getattr(origin, "type", "")
                if origin_type == "channel" and fw.get("channels"):
                    should_delete = True
                elif origin_type == "chat" and fw.get("groups"):
                    should_delete = True
                elif origin_type == "user":
                    fwd_user = getattr(origin, "sender_user", None)
                    if fwd_user and fwd_user.is_bot and fw.get("bots"):
                        should_delete = True
                    elif fwd_user and not fwd_user.is_bot and fw.get("users"):
                        should_delete = True
                elif origin_type == "hidden_user" and fw.get("users"):
                    should_delete = True
                if should_delete:
                    try:
                        await update.message.delete()
                        logger.info(f"Deleted forwarded msg from {update.message.from_user.id} in {update.effective_chat.id} (origin: {origin_type})")
                    except Exception as e:
                        logger.error(f"Forward delete failed: {e}")
                    await log_action(context, f"FORWARD-SPAM: {update.message.from_user.full_name} ({update.message.from_user.id}) in {update.effective_chat.title} — Typ: {origin_type}")
                    return

    # --- Forbidden words check ---
    msg_text = update.message.text or update.message.caption or ""
    if msg_text and update.message.from_user:
        sender = update.message.from_user
        if not is_authorized(sender.id) and not is_freed(sender.id):
            bot_data = load_data()
            bw_config = bot_data.get("badwords_config", {"punishment": "aus", "delete": True})
            bw_punishment = bw_config.get("punishment", "aus")
            bw_delete = bw_config.get("delete", True)
            word_list = bot_data.get("badwords", [])
            if word_list:
                matched = check_forbidden_words(msg_text, word_list)
                if matched and (bw_delete or bw_punishment != "aus"):
                    chat_id = update.effective_chat.id
                    user_id_bw = sender.id
                    user_name = sender.full_name
                    deleted = False

                    if bw_delete:
                        try:
                            await update.message.delete()
                            deleted = True
                        except Exception as e:
                            logger.error(f"Badword delete failed in {chat_id} for {user_id_bw}: {e}")

                    try:
                        if bw_punishment == "warn":
                            wc = bot_data.get("warn_config", {"max_warns": 3})
                            max_w = wc.get("max_warns", 3)
                            warnings = bot_data.setdefault("warnings", {})
                            key = f"{chat_id}_{user_id_bw}"
                            warn_entry = warnings.get(key, {"count": 0, "name": user_name})
                            warn_entry["count"] = warn_entry.get("count", 0) + 1
                            warn_entry["name"] = user_name
                            warnings[key] = warn_entry
                            save_data(bot_data)
                            # Check if warn limit reached
                            if warn_entry["count"] >= max_w:
                                warn_punishment = wc.get("punishment", "mute")
                                action_label_bw = ""
                                try:
                                    if warn_punishment == "ban":
                                        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id_bw, revoke_messages=True)
                                        remember_group_ban([chat_id], user_id_bw, user_name, sender.username)
                                        action_label_bw = "• <b>Aktion:</b> Gebannt 🚫"
                                    elif warn_punishment == "kick":
                                        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id_bw)
                                        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id_bw)
                                        action_label_bw = "• <b>Aktion:</b> Gekickt ❗"
                                    elif warn_punishment == "mute":
                                        mute_secs = wc.get("mute_duration_seconds", wc.get("mute_duration_hours", 5) * 3600)
                                        until_date = now_de() + datetime.timedelta(seconds=mute_secs)
                                        await context.bot.restrict_chat_member(
                                            chat_id=chat_id, user_id=user_id_bw,
                                            permissions=ChatPermissions.no_permissions(),
                                            until_date=until_date,
                                        )
                                        until_str = until_date.strftime("%d.%m.%y um %H:%M")
                                        action_label_bw = f"• <b>Aktion:</b> Stummgeschaltet 🤫\n• <b>Bis:</b> {until_str}"
                                except Exception as e:
                                    action_label_bw = f"• ⚠️ Fehler: {e}"
                                warnings.pop(key, None)
                                save_data(bot_data)
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"⚠️ {html.escape(user_name)} wurde verwarnt ({max_w}/{max_w}) — Verbotenes Wort: <code>{html.escape(matched)}</code>\n{action_label_bw}",
                                    parse_mode="HTML",
                                )
                                await log_action(context, f"BADWORD-WARN AUTO-PUNISH ({warn_punishment}): {user_name} ({user_id_bw}) in {chat_id} — {max_w}/{max_w}")
                            else:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"⚠️ {html.escape(user_name)} wurde verwarnt ({warn_entry['count']}/{max_w}) — Verbotenes Wort: <code>{html.escape(matched)}</code>",
                                    parse_mode="HTML",
                                )
                        elif bw_punishment == "mute":
                            await context.bot.restrict_chat_member(
                                chat_id=chat_id, user_id=user_id_bw,
                                permissions=ChatPermissions.no_permissions(),
                            )
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=f"🤫 {html.escape(user_name)} wurde gemutet — Verbotenes Wort: <code>{html.escape(matched)}</code>",
                                parse_mode="HTML",
                            )
                        elif bw_punishment == "kick":
                            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id_bw)
                            await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id_bw)
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=f"❗ {html.escape(user_name)} wurde gekickt — Verbotenes Wort: <code>{html.escape(matched)}</code>",
                                parse_mode="HTML",
                            )
                        elif bw_punishment == "ban":
                            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id_bw, revoke_messages=True)
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=f"🚫 {html.escape(user_name)} wurde gebannt — Verbotenes Wort: <code>{html.escape(matched)}</code>",
                                parse_mode="HTML",
                            )
                    except Exception as e:
                        logger.error(f"Badword punishment failed: {e}")

                    await log_action(
                        context,
                        f"BADWORD: {user_name} ({user_id_bw}) in {update.effective_chat.title} — Wort: {matched} — Strafe: {bw_punishment} — Gelöscht: {'ja' if deleted else 'nein'}"
                    )
                    return


        left_member = update.message.left_chat_member
        if left_member and is_banned_in_group(update.effective_chat.id, left_member.id):
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
    all_groups = await get_bot_groups(context)
    group_title_map = {g["id"]: g.get("title", str(g["id"])) for g in all_groups}
    
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
        preview = html.escape(s.get("text", "")[:20])
        time_str = s.get("time", "?")
        interval = s.get("interval_label", "?")
        emoji = "🟢" if s.get("active") else "🔴"
        sched_group_ids = s.get("groups", [])
        group_titles = [group_title_map.get(gid, str(gid)) for gid in sched_group_ids]
        if len(group_titles) > 3:
            groups_preview = ", ".join(group_titles[:3]) + f" +{len(group_titles) - 3} mehr"
        else:
            groups_preview = ", ".join(group_titles) if group_titles else "Keine Gruppen"
        groups_preview = html.escape(groups_preview)
        
        text += (
            f"\n💬{emoji} <b>{i}</b> · <b>{status}</b>\n"
            f"  ├ <i>Zeit: {time_str}</i>\n"
            f"  ├ <i>{interval}</i>\n"
            f"  ├ <i>Gruppen: {groups_preview}</i>\n"
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


async def show_members_group_selection(query, context, user_id, groups, action):
    """Show group selection grid for mass unban/unmute."""
    selected = user_data_store.get(user_id, {}).get("selected", set())
    action_label = "Unban" if "unban" in action else "Unmute"
    action_emoji = "✅" if "unban" in action else "🔊"
    keyboard = []
    row = []
    for g in groups:
        check = "✅" if g["id"] in selected else "⬜"
        row.append(InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"memgrp_toggle_{g['id']}_{action}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data=f"memgrp_all_{action}"),
        InlineKeyboardButton("◻️ Keine", callback_data=f"memgrp_none_{action}"),
    ])
    keyboard.append([InlineKeyboardButton(f"⚡ Weiter ({len(selected)} gewählt)", callback_data=f"memgrp_confirm_{action}")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_members")])
    await query.edit_message_text(
        f"{action_emoji} <b>All {action_label}</b>\nWähle die Gruppen:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_pcmd_group_selection(query, context, user_id, groups):
    """Show group selection grid for personal commands."""
    selected = user_data_store.get(user_id, {}).get("selected", set())
    keyboard = []
    row = []
    for g in groups:
        check = "✅" if g["id"] in selected else "⬜"
        row.append(InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"pcmd_grp_toggle_{g['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data="pcmd_grp_all"),
        InlineKeyboardButton("◻️ Keine", callback_data="pcmd_grp_none"),
    ])
    keyboard.append([InlineKeyboardButton(f"✅ Weiter ({len(selected)} gewählt)", callback_data="pcmd_grp_confirm")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="pcmd_menu")])
    await query.edit_message_text(
        "🏗 <b>Befehl hinzufügen</b>\nWähle die Gruppen, in denen der Befehl gelten soll:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
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
    
    # Calculate next fire time
    next_fire_str = "—"
    if sched.get("active"):
        jq = _get_job_queue(context)
        if jq:
            jobs = jq.get_jobs_by_name(f"sched_{sched_id}")
            if jobs:
                job = jobs[0]
                if job.next_t:
                    next_fire_str = job.next_t.astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")
    
    last_sent = sched.get("last_sent", "—")
    
    text = (
        f"🕐 <b>Wiederholte Mitteilungen</b>\n\n"
        f"💡 <b>Status</b>: {status}\n"
        f"🕐 <b>Zeit</b>: {time_str}\n"
        f"🔁 <b>Wiederholung</b>: {interval}\n"
        f"⏭ <b>Nächster Versand</b>: {next_fire_str}\n"
        f"📤 <b>Letzter Versand</b>: {last_sent}\n"
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
        
        # Check start_date / end_date
        now = now_de()
        start_date = sched.get("start_date")
        end_date = sched.get("end_date")
        if start_date:
            try:
                sd = datetime.datetime.strptime(start_date, "%d.%m.%Y").replace(tzinfo=BERLIN_TZ)
                if now < sd:
                    logger.info(f"Scheduled {sched_id} not yet started (start_date={start_date})")
                    return
            except Exception:
                pass
        if end_date:
            try:
                ed = datetime.datetime.strptime(end_date, "%d.%m.%Y").replace(tzinfo=BERLIN_TZ).replace(hour=23, minute=59)
                if now > ed:
                    logger.info(f"Scheduled {sched_id} expired (end_date={end_date}), deactivating")
                    sched["active"] = False
                    save_data(bot_data)
                    remove_scheduled_job(context, sched_id)
                    return
            except Exception:
                pass
        
        # Check weekday filter (0=Mon, 6=Sun)
        weekdays = sched.get("weekdays")
        if weekdays and len(weekdays) > 0:
            current_weekday = now.weekday()  # 0=Monday
            if current_weekday not in weekdays:
                logger.info(f"Scheduled {sched_id} skipped: weekday {current_weekday} not in {weekdays}")
                return
        
        # Check monthday filter
        monthdays = sched.get("monthdays")
        if monthdays and len(monthdays) > 0:
            if now.day not in monthdays:
                logger.info(f"Scheduled {sched_id} skipped: day {now.day} not in {monthdays}")
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
                    msg = await context.bot.send_message(chat_id=gid, text=text_html, parse_mode="HTML", disable_web_page_preview=True)
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
        logger.error(f"Cannot schedule repeating message {sched.get('id')}: job_queue unavailable")
        return
    
    sched_id = sched["id"]
    interval_minutes = sched.get("interval_minutes", 60)
    interval = interval_minutes * 60  # convert to seconds
    interval_td = datetime.timedelta(minutes=interval_minutes)
    
    # Remove existing job if any
    remove_scheduled_job(context, sched_id)
    
    # Calculate first run time
    now = now_de()
    time_str = sched.get("time", "")
    last_sent_str = sched.get("last_sent")
    
    # Priority 1: If we have a last_sent timestamp, calculate next run from there
    if last_sent_str:
        try:
            last_sent_dt = datetime.datetime.strptime(last_sent_str, "%d.%m.%Y %H:%M").replace(tzinfo=BERLIN_TZ)
            next_run = last_sent_dt + interval_td
            # If next_run is in the past (e.g. bot was down), find the next valid time
            while next_run <= now:
                next_run += interval_td
            delay = (next_run - now).total_seconds()
            logger.info(f"Scheduled {sched_id}: last_sent={last_sent_str}, next run in {delay:.0f}s")
        except Exception as e:
            logger.error(f"Error parsing last_sent for {sched_id}: {e}")
            last_sent_str = None  # Fall through to time-based calculation
    
    # Priority 2: Use configured start time
    if not last_sent_str and time_str and time_str != "00:00":
        try:
            h, m = map(int, time_str.split(":"))
        except Exception:
            h, m = 0, 0
        first_run = now.replace(hour=h, minute=m, second=0, microsecond=0)
        while first_run <= now:
            first_run += interval_td
        delay = (first_run - now).total_seconds()
    elif not last_sent_str:
        # No time set and no last_sent – start immediately
        delay = 0
        logger.info(f"No start time or last_sent for {sched_id}, starting immediately")
    
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
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("banall", banall))
    app.add_handler(CommandHandler("unbanall", unbanall))
    app.add_handler(CommandHandler("befehl", personal_command))
    app.add_handler(CommandHandler("unbefehl", unpersonal_command))
    app.add_handler(CommandHandler("mute", mute_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(CommandHandler("kick", kick_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("unwarn", unwarn_command))
    app.add_handler(CommandHandler("free", free_command))
    app.add_handler(CommandHandler("unfree", unfree_command))
    app.add_handler(CommandHandler("open", handle_open_command))
    app.add_handler(CommandHandler("close", handle_close_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_handler))
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO | filters.Sticker.ALL | filters.ANIMATION | filters.Document.ALL) & filters.ChatType.PRIVATE, media_handler))
    # Custom command handler for groups (must be before track_message but after known commands)
    app.add_handler(MessageHandler(filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), handle_custom_command), group=0)
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

    if not app.job_queue:
        logger.error(
            "JobQueue unavailable. Repeating messages need python-telegram-bot[job-queue] / APScheduler installed."
        )
    else:
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
