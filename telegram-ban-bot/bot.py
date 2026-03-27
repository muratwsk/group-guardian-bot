import asyncio
import atexit
import datetime
import html
import json
import logging
import os
import signal
import subprocess
import time
from zoneinfo import ZoneInfo
import re


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

GROUPS_FILE = os.path.join(os.path.dirname(__file__), "groups.json")
LOCK_FILE = os.path.join(os.path.dirname(__file__), "bot.lock")

# ── SQLite-backed storage ──────────────────────────────────────────
from db import (
    load_config, save_config,
    load_data as _db_load_data, save_data as _db_save_data,
    load_users, save_users,
    load_protokoll, save_protokoll,
    invalidate_cache as db_invalidate_cache,
)

ADMIN_STATUS_CACHE = {}
ADMIN_CACHE_TTL_SEC = 30
USER_TRACK_LAST_SAVE = {}
USER_TRACK_SAVE_INTERVAL_SEC = 20
ACTION_DEDUPE_TTL_SEC = 4.0
BOT_USERNAME_CACHE = None  # Cached bot username to avoid get_me() API calls

UNMUTE_PERMISSIONS = ChatPermissions.all_permissions()

# --- Duration parser for time-based mute/ban ---
DURATION_PATTERN = re.compile(r"(\d+)\s*(m|min|h|std|d|t|w)\b", re.IGNORECASE)
DURATION_UNITS = {
    "m": 60, "min": 60,
    "h": 3600, "std": 3600,
    "d": 86400, "t": 86400,
    "w": 604800,
}

def parse_duration(args: list[str]) -> tuple[list[str], int | None, str | None]:
    """Extract a duration like '2h' or '30m' from args.
    Returns (remaining_args, seconds, human_label)."""
    text = " ".join(args)
    match = DURATION_PATTERN.search(text)
    if not match:
        return args, None, None
    amount = int(match.group(1))
    unit_key = match.group(2).lower()
    seconds = amount * DURATION_UNITS.get(unit_key, 60)
    # Build human label
    unit_labels = {"m": "Min", "min": "Min", "h": "Std", "std": "Std", "d": "Tag(e)", "t": "Tag(e)", "w": "Woche(n)"}
    label = f"{amount} {unit_labels.get(unit_key, unit_key)}"
    # Remove the duration from args
    cleaned = text[:match.start()] + text[match.end():]
    remaining = cleaned.split()
    return remaining, seconds, label


def normalize_data(data):
    data.setdefault("groups", [])
    data.setdefault("banned_users", {})
    data.setdefault("broadcasts", {})
    data.setdefault("scheduled", [])
    data.setdefault("personal_commands", {})
    data.setdefault("warnings", {})
    data.setdefault("active_mutes", {})
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
    data.setdefault("auto_approve", {})
    data.setdefault("antispam_links", {
        "punishment": "aus",
        "delete": True,
        "groups": [],
    })
    if "groups" not in data.get("antispam_links", {}):
        data["antispam_links"]["groups"] = []
    data.setdefault("antispam_forward", {
        "channels": False,
        "groups": False,
        "users": False,
        "bots": False,
    })
    data.setdefault("freed_users", [])
    data.setdefault("exempt_groups", [])
    data.setdefault("protokoll_channels", {})
    ar = data.setdefault("admin_report", {
        "active": False,
        "staff_group": None,
        "notify_users": [],
        "group_routes": {},
    })
    ar.setdefault("group_routes", {})
    return data


def load_data():
    return normalize_data(_db_load_data())


def save_data(data):
    _db_save_data(normalize_data(data))


def is_freed(user_id: int) -> bool:
    """Check if a user has the 'Befreiter' role (exempt from all restrictions)."""
    bot_data = load_data()
    return user_id in bot_data.get("freed_users", [])


def sync_groups_to_file():
    """Sync registered groups from data back to groups.json."""
    data = load_data()
    groups = data.get("groups", [])
    groups_map = {g["title"]: g["id"] for g in groups}
    tmp = GROUPS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(groups_map, f, indent=4, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, GROUPS_FILE)


def import_groups_from_file():
    """Import groups from groups.json into SQLite on startup."""
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
    sync_groups_to_file()


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


def should_skip_recent_action(context: ContextTypes.DEFAULT_TYPE, action_key: str, ttl_sec: float = ACTION_DEDUPE_TTL_SEC) -> bool:
    """Return True if the same moderation action was already triggered moments ago."""
    recent_actions = context.application.bot_data.setdefault("_recent_action_keys", {})
    now = time.monotonic()

    for key, expires_at in list(recent_actions.items()):
        if expires_at <= now:
            recent_actions.pop(key, None)

    if recent_actions.get(action_key, 0.0) > now:
        logger.info(f"Skipped duplicate action: {action_key}")
        return True

    recent_actions[action_key] = now + ttl_sec
    return False


def set_active_mute(chat_id: int, user_id: int, until_ts: float | None = None):
    bot_data = load_data()
    active_mutes = bot_data.setdefault("active_mutes", {})
    active_mutes[f"{chat_id}:{user_id}"] = {"until_ts": until_ts}
    save_data(bot_data)


def clear_active_mute(chat_id: int, user_id: int):
    bot_data = load_data()
    active_mutes = bot_data.setdefault("active_mutes", {})
    active_mutes.pop(f"{chat_id}:{user_id}", None)
    save_data(bot_data)


def has_active_mute(chat_id: int, user_id: int) -> bool:
    bot_data = load_data()
    active_mutes = bot_data.setdefault("active_mutes", {})
    key = f"{chat_id}:{user_id}"
    entry = active_mutes.get(key)
    if not entry:
        return False
    until_ts = entry.get("until_ts")
    if until_ts and until_ts <= time.time():
        active_mutes.pop(key, None)
        save_data(bot_data)
        return False
    return True


async def is_user_currently_muted(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    return has_active_mute(chat_id, user_id)


async def wait_for_mute_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, expected_muted: bool, attempts: int = 5, delay: float = 0.4) -> bool:
    for attempt in range(attempts):
        if await is_user_currently_muted(context, chat_id, user_id) == expected_muted:
            return True
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    return False


async def is_user_currently_banned(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status == "kicked"
    except Exception:
        return is_banned_in_group(chat_id, user_id)


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


def get_tracked_banned_user_ids(group_id: int) -> list[int]:
    """Return all tracked banned user IDs for one group."""
    data = load_data()
    group_bans = data.get("banned_users", {}).get(str(group_id), {})
    result = []
    for uid_str in group_bans.keys():
        try:
            result.append(int(uid_str))
        except (TypeError, ValueError):
            continue
    return result





async def _delete_message_later(bot, chat_id: int, message_id: int, preview: str = "", delay: float = 0.5):
    """Delete a command message shortly after handler execution finished, with retry."""
    await asyncio.sleep(delay)
    for attempt in range(3):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info(f"Auto-deleted command '{preview}' in {chat_id}")
            return
        except Exception as e:
            if "message to delete not found" in str(e).lower() or "message can't be deleted" in str(e).lower():
                logger.info(f"Message {message_id} already gone or can't delete in {chat_id}")
                return
            if attempt < 2:
                logger.warning(f"Cmd delete attempt {attempt+1} failed for {message_id} in {chat_id}: {e}, retrying...")
                await asyncio.sleep(1.0)
            else:
                logger.error(f"Cmd delete failed after 3 attempts for {message_id} in {chat_id}: {e}")


async def auto_delete_command(update: Update, context):
    """Schedule command-like message deletion in groups without breaking command execution."""
    if not update.message or not update.message.text or not update.effective_chat:
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return

    msg_text = update.message.text.strip()
    first_char = msg_text[0] if msg_text else ""
    rest = msg_text[1:] if len(msg_text) > 1 else ""
    if not (first_char in ["/", "!", ";", "."] and rest and rest[0].isalnum()):
        return

    msg_id = update.message.message_id
    if context.chat_data.get("_last_scheduled_cmd_delete") == msg_id:
        return

    try:
        bot_data_cd = load_data()
        cd = bot_data_cd.get("cmd_delete", {"admin_prefixes": [], "user_prefixes": []})
        sender = update.message.from_user
        if not sender:
            return

        is_adm = await is_chat_admin(context, chat.id, sender.id)
        prefixes = cd.get("admin_prefixes", []) if is_adm else cd.get("user_prefixes", [])
        if first_char in prefixes:
            context.chat_data["_last_scheduled_cmd_delete"] = msg_id
            context.application.create_task(
                _delete_message_later(
                    context.bot,
                    chat.id,
                    msg_id,
                    msg_text[:30],
                )
            )
            logger.info(f"Scheduled command delete '{msg_text[:30]}' from {'admin' if is_adm else 'user'} {sender.id} in {chat.id}")
    except Exception as e:
        logger.error(f"Cmd delete scheduling failed: {e}")


def track_user(user, group_id=None):
    """Track a user's username → ID mapping, per-group message count, and first seen date."""
    if not user or user.is_bot:
        return

    users = load_users()
    now_str = now_de().strftime("%d.%m.%Y %H:%M")
    changed = False
    should_persist = False
    group_save_key = f"{user.id}:{group_id or 'global'}"

    def _update_entry(key):
        nonlocal changed, should_persist
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
            changed = True
            if gs["msg_count"] == 1:
                should_persist = True

        if (
            existing.get("id") != entry["id"]
            or existing.get("name") != entry["name"]
            or existing.get("username") != entry["username"]
            or existing.get("first_seen") != entry["first_seen"]
        ):
            changed = True
            should_persist = True

        users[key] = entry

    if user.username:
        _update_entry(user.username.lower())
    _update_entry(str(user.id))

    if not changed:
        return

    # Update cache and persist periodically
    now_ts = datetime.datetime.now().timestamp()
    last_save = USER_TRACK_LAST_SAVE.get(group_save_key, 0)
    if should_persist or (now_ts - last_save) >= USER_TRACK_SAVE_INTERVAL_SEC:
        save_users(users)
        USER_TRACK_LAST_SAVE[group_save_key] = now_ts


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


async def is_group_authorized(context, user_id: int, chat=None) -> bool:
    """Check if user is config-admin OR a Telegram admin in the given chat."""
    if is_admin(user_id):
        return True
    if chat and chat.type in ("group", "supergroup"):
        return await is_chat_admin(context, chat.id, user_id)
    return False


async def is_chat_admin(context, chat_id: int, user_id: int) -> bool:
    """Check if user is admin or creator in a specific chat, with short TTL cache."""
    cache_key = f"{chat_id}:{user_id}"
    now_ts = datetime.datetime.now().timestamp()
    cached = ADMIN_STATUS_CACHE.get(cache_key)
    if cached and (now_ts - cached["ts"]) < ADMIN_CACHE_TTL_SEC:
        return cached["value"]

    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        value = member.status in ("administrator", "creator")
    except Exception:
        value = False

    ADMIN_STATUS_CACHE[cache_key] = {"value": value, "ts": now_ts}
    return value


async def _send_logs_async(context: ContextTypes.DEFAULT_TYPE, targets: set[int], text: str):
    async def _send_log(chat_id: int):
        try:
            await asyncio.wait_for(
                context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML"),
                timeout=3.0,
            )
        except Exception as e:
            logger.error(f"Log channel {chat_id} error: {e}")

    await asyncio.gather(*[_send_log(chat_id) for chat_id in targets], return_exceptions=True)


def _format_log_block(category: str, action: str, details: dict) -> str:
    """Format a structured log block for Telegram."""
    now_str = now_de().strftime("%d.%m.%Y %H:%M:%S")

    if category == "admin":
        icon = "⚙️"
    else:
        action_icons = {
            "BAN": "🚫", "UNBAN": "✅", "BANALL": "🚫", "UNBANALL": "✅",
             "MUTE": "🔇", "UNMUTE": "🔊", "KICK": "👢", "WARN": "⚠️",
             "UNWARN": "↩️", "BADWORD": "🔤", "LINK": "🔗", "AUTO-WIEDERBANN": "🔄",
            "FREE": "🛡", "UNFREE": "🛡", "MASS UNBAN": "✅", "MASS UNMUTE": "🔊",
            "DELETE": "🗑", "FORWARD-SPAM": "🔀", "LINK-WARN CANCEL": "↩️",
            "MASS BAN": "🚫", "MASS MUTE": "🔇", "MASS KICK": "👢",
        }
        icon = action_icons.get(action.upper(), "📋")

    lines = [f"{icon} <b>{html.escape(action)}</b>"]
    lines.append(f"━━━━━━━━━━━━━━━")

    field_order = ["user", "user_id", "gruppe", "von", "von_id", "grund", "dauer", "details", "ergebnis"]
    field_labels = {
        "user": "👤 User", "user_id": "🆔 ID", "gruppe": "📍 Gruppe",
        "von": "👮 Von", "von_id": "👮 Admin-ID", "grund": "📝 Grund", "dauer": "⏱ Dauer",
        "details": "ℹ️ Details", "ergebnis": "📊 Ergebnis",
    }

    for key in field_order:
        if key in details and details[key]:
            label = field_labels.get(key, key)
            lines.append(f"{label}: <code>{html.escape(str(details[key]))}</code>")

    # Any extra keys not in field_order
    for key, val in details.items():
        if key not in field_order and val:
            lines.append(f"ℹ️ {key}: <code>{html.escape(str(val))}</code>")

    lines.append(f"🕐 {now_str}")
    return "\n".join(lines)


# Log categories
LOG_CAT_MOD = "mod"       # Moderation: ban, mute, warn, filter hits
LOG_CAT_ADMIN = "admin"   # Admin: settings changes, bot config

async def log_action(context: ContextTypes.DEFAULT_TYPE, text: str, group_id: int = None, group_name: str = None, category: str = LOG_CAT_MOD, action: str = "", details: dict = None):
    """Log action to matching protokoll channels based on category and group routing."""
    cfg = load_config()
    targets_mod = set()
    targets_admin = set()

    # Legacy global log channel → receives everything
    channel = cfg.get("log_channel_id")
    if channel:
        targets_mod.add(int(channel))
        targets_admin.add(int(channel))

    bot_data = load_data()
    proto_channels = bot_data.get("protokoll_channels", {})
    for ch_id_str, ch_cfg in proto_channels.items():
        ch_type = ch_cfg.get("type", "mod")  # default to mod for backwards compat
        ch_groups = ch_cfg.get("groups", [])

        if ch_type == "admin":
            targets_admin.add(int(ch_id_str))
        else:
            # mod channel: check group routing
            should_send = False
            if "all" in ch_groups:
                should_send = True
            elif group_id and str(group_id) in [str(g) for g in ch_groups]:
                should_send = True
            if should_send:
                targets_mod.add(int(ch_id_str))

    # Build formatted message
    if details and action:
        formatted = _format_log_block(category, action, details)
    else:
        # Legacy fallback: plain text with timestamp
        now_str = now_de().strftime("%d.%m.%Y %H:%M:%S")
        formatted = f"📋 {text}\n🕐 {now_str}"

    targets = targets_admin if category == LOG_CAT_ADMIN else targets_mod
    if targets:
        asyncio.create_task(_send_logs_async(context, targets, formatted))


async def render_protokoll_channel_config(query, ch_id: str):
    bot_data = load_data()
    ch_cfg = bot_data.get("protokoll_channels", {}).get(str(ch_id), {})
    ch_type = ch_cfg.get("type", "mod")
    ch_name = ch_cfg.get("name", str(ch_id))

    if ch_type == "admin":
        # Admin channels don't need group routing
        type_icon = "⚙️"
        keyboard = [
            [InlineKeyboardButton("🔄 Zu Moderations-Log wechseln", callback_data=f"proto_switch_mod_{ch_id}")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_protokoll")],
        ]
        await query.edit_message_text(
            f"{type_icon} <b>Admin-Protokoll: {html.escape(ch_name)}</b>\n\n"
            f"Dieser Kanal protokolliert alle <b>Bot-Einstellungen</b>:\n"
            f"• Admin hinzufügen/entfernen\n"
            f"• Einstellungen ändern\n"
            f"• Wiederholte Nachrichten\n"
            f"• Befehle erstellen/löschen\n"
            f"• Freigabemodus\n"
            f"• Sperr-Einstellungen",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
    else:
        # Mod channels have group routing
        ch_groups = [str(x) for x in ch_cfg.get("groups", [])]
        groups = bot_data.get("groups", [])
        type_icon = "🛡"
        keyboard = []
        all_check = "✅" if "all" in ch_groups else "⬜"
        keyboard.append([InlineKeyboardButton(f"{all_check} Alle Gruppen", callback_data=f"proto_tga_{ch_id}")])
        for g in groups:
            gid = str(g["id"])
            check = "✅" if gid in ch_groups else "⬜"
            keyboard.append([InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"proto_tgg_{gid}_{ch_id}")])
        keyboard.append([InlineKeyboardButton("🔄 Zu Admin-Log wechseln", callback_data=f"proto_switch_admin_{ch_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_protokoll")])
        await query.edit_message_text(
            f"{type_icon} <b>Moderations-Log: {html.escape(ch_name)}</b>\n\n"
            f"Dieser Kanal protokolliert <b>Moderationsaktionen</b>:\n"
            f"• Ban / Unban / BanAll / UnbanAll\n"
            f"• Mute / Unmute / Kick\n"
            f"• Warn / Unwarn\n"
            f"• Verbotene Wörter / Links\n"
            f"• Auto-Reban\n\n"
            f"Wähle welche Gruppen protokolliert werden:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )


async def render_sperr_bot_groups(query, context: ContextTypes.DEFAULT_TYPE):
    bot_data = load_data()
    sb = bot_data.get("sperr_bots", {"groups": []})
    selected = [str(g) for g in sb.get("groups", [])]
    groups = await get_bot_groups(context)

    keyboard = []
    all_check = "✅" if not selected else "⬜"
    keyboard.append([InlineKeyboardButton(f"{all_check} Alle Gruppen", callback_data="sperr_bot_tga")])
    for g in groups:
        gid_str = str(g["id"])
        check = "✅" if gid_str in selected else "⬜"
        keyboard.append([InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"sperr_bot_tgg_{gid_str}")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="sperr_bot_menu")])
    grp_info = f"{len(selected)} ausgewählt" if selected else "Alle Gruppen"
    await query.edit_message_text(
        f"🤖 <b>Bot Sperren — Gruppen</b>\n\n"
        f"Wähle die Gruppen, in denen Bot Sperren aktiv sein soll.\n"
        f"Wenn keine Gruppe ausgewählt ist, gilt es für <b>alle</b> Gruppen.\n\n"
        f"<b>Aktuell:</b> {grp_info}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


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

# --- /reload ---

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reload config — clears caches but does NOT import group admins as bot admins."""
    user_id = update.effective_user.id
    chat = update.effective_chat
    if not is_authorized(user_id):
        if not (chat and chat.type in ("group", "supergroup") and await is_chat_admin(context, chat.id, user_id)):
            return

    # Clear caches so fresh data is loaded
    db_invalidate_cache()
    ADMIN_STATUS_CACHE.clear()

    await update.message.reply_text("✅ Bot-Konfiguration neu geladen.")

# --- /start ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
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
        [InlineKeyboardButton("🚪 Freigabemodus", callback_data="menu_freigabe"),
         InlineKeyboardButton("📋 Protokoll", callback_data="menu_protokoll")],
        [InlineKeyboardButton("🔒 Sperren", callback_data="menu_sperren"),
         InlineKeyboardButton("🆘 @admin", callback_data="menu_admin_report")],
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
WAITING_CLOSE_STICKER = 15
WAITING_PCMD_NAME = 16
WAITING_PCMD_TEXT = 17
WAITING_PCMD_GROUPS = 18
WAITING_WARN_MUTE_DUR = 19
WAITING_BADWORD_ADD = 20
WAITING_BADWORD_REMOVE = 21
WAITING_PROTO_CHANNEL = 22
WAITING_GROUP_ADD_ID = 23

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
    # Split original text by whitespace and common separators
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
        if await is_user_currently_muted(context, chat_id, user_id):
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

async def _render_admin_report_menu(query):
    """Render the @admin settings menu."""
    bot_data = load_data()
    ar = bot_data.get("admin_report", {})
    active = ar.get("active", False)
    staff_group = ar.get("staff_group")
    notify_users = ar.get("notify_users", [])
    group_routes = ar.get("group_routes", {})

    status_icon = "✅ Aktiv" if active else "❌ Inaktiv"

    # Resolve staff group name
    if staff_group:
        groups = bot_data.get("groups", [])
        sg_name = next((g["title"] for g in groups if g["id"] == staff_group), str(staff_group))
        staff_str = f"👥 {sg_name}"
    else:
        staff_str = "❗️ Nicht definiert"

    notify_str = "Keine"
    if notify_users:
        parts = []
        for uid in notify_users:
            try:
                u_entry = load_users().get(str(uid), {})
                parts.append(u_entry.get("name", str(uid)))
            except Exception:
                parts.append(str(uid))
        notify_str = ", ".join(parts)

    # Group routes info (grouped by target)
    routes_str = ""
    route_also_default = ar.get("route_also_default", {})
    if group_routes:
        groups = bot_data.get("groups", [])
        gmap = {g["id"]: g["title"] for g in groups}
        # Group by target
        targets = {}
        for src_id, dst_id in group_routes.items():
            targets.setdefault(dst_id, []).append(src_id)
        parts = []
        for dst_id, src_ids in targets.items():
            dst_name = gmap.get(dst_id, str(dst_id))
            src_names = []
            for s in src_ids:
                sn = gmap.get(int(s), s)
                also = route_also_default.get(s, True)
                mode = "+Std" if also else "nur"
                src_names.append(f"{sn} [{mode}]")
            parts.append(f"  📌 {dst_name} (<code>{dst_id}</code>):\n" + "\n".join(f"    • {n}" for n in src_names))
        routes_str = "\n\n📋 <b>Gruppen-Routing:</b>\n" + "\n".join(parts)

    text = (
        f"🆘 <b>@admin-Befehl</b>\n\n"
        f"Status: {status_icon}\n\n"
        f"🏢 <b>Standard-Team:</b> {staff_str}\n"
        f"<i>→ Bekommt Meldungen aus Gruppen ohne eigene Route (oder wenn 'auch Standard-Team' aktiv)</i>\n\n"
        f"🔔 <b>Benachrichtigen:</b> {notify_str}"
        f"{routes_str}"
    )

    if not staff_group and not group_routes:
        text += "\n\n❗️ Es wurde keine Mitarbeitergruppe definiert."

    keyboard = [
        [InlineKeyboardButton(f"{'❌ Deaktivieren' if active else '✅ Aktivieren'}", callback_data="ar_toggle")],
        [InlineKeyboardButton("👥 Standard-Team setzen", callback_data="ar_set_group")],
        [InlineKeyboardButton("📋 Gruppen-Routing", callback_data="ar_routes_menu")],
        [InlineKeyboardButton("🔔 Benutzer benachrichtigen", callback_data="ar_notify_menu")],
        [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data and any(data.startswith(prefix) for prefix in ("info_", "cmd_")):
        scope_chat_id = query.message.chat.id if query.message and query.message.chat else None
        if scope_chat_id is not None and should_skip_recent_action(context, f"button:{data}:{query.from_user.id}:{scope_chat_id}"):
            return

    # Group-context moderation buttons allow Telegram group admins
    group_button_prefixes = (
        "info_",
        "cmd_",
        "warn_undo_",
        "warn_add1_",
        "warn_reset_",
        "warn_punish_",
        "link_warn_cancel_",
    )
    # Buttons that anyone in the staff group can press (no auth needed)
    open_button_prefixes = (
        "ar_solved_",
    )
    if data and any(data.startswith(prefix) for prefix in group_button_prefixes):
        chat = query.message.chat if query.message else None
        if not await is_group_authorized(context, user_id, chat):
            return
    elif data and any(data.startswith(prefix) for prefix in open_button_prefixes):
        pass  # Allow anyone in the staff group to press these
    elif not is_authorized(user_id):
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
            [InlineKeyboardButton("🚪 Freigabemodus", callback_data="menu_freigabe"),
             InlineKeyboardButton("📋 Protokoll", callback_data="menu_protokoll")],
            [InlineKeyboardButton("🔒 Sperren", callback_data="menu_sperren"),
             InlineKeyboardButton("🆘 @admin", callback_data="menu_admin_report")],
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
            "next_run_at": (now_de() + datetime.timedelta(minutes=minutes)).strftime("%d.%m.%Y %H:%M"),
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
                if s["active"]:
                    s["next_run_at"] = (now_de() + datetime.timedelta(minutes=s.get("interval_minutes", 60))).strftime("%d.%m.%Y %H:%M")
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
                if s.get("active"):
                    s["next_run_at"] = (now_de() + datetime.timedelta(minutes=s.get("interval_minutes", 60))).strftime("%d.%m.%Y %H:%M")
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
                s["next_run_at"] = (now_de() + datetime.timedelta(minutes=minutes)).strftime("%d.%m.%Y %H:%M")
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
        await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="BANALL", details={"user": target_name, "user_id": str(target_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id), "ergebnis": f"{len(successful_groups)} OK, {len(failed_groups)} Fehler"})

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
        await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="UNBANALL", details={"user": target_name, "user_id": str(target_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id)})

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

        # Prüfen ob bereits gebannt
        if await is_user_currently_banned(context, scope_chat_id, target_id):
            await query.answer("ℹ️ Dieser User ist bereits gebannt.", show_alert=True)
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
        await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="BAN", details={"user": target_name, "user_id": str(target_id), "gruppe": str(scope_chat_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id)})

    elif data.startswith("info_unban_"):
        payload = data.replace("info_unban_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        groups = await get_info_banall_groups(context, scope_chat_id)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        # Prüfen ob tatsächlich gebannt
        if not await is_user_currently_banned(context, scope_chat_id, target_id):
            await query.answer("ℹ️ Dieser User ist nicht gebannt.", show_alert=True)
            return

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
        await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="UNBAN", details={"user": target_name, "user_id": str(target_id), "gruppe": str(scope_chat_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id)})

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

        # Prüfen ob bereits gemutet
        if await is_user_currently_muted(context, scope_chat_id, target_id):
            if not await wait_for_mute_state(context, scope_chat_id, target_id, False, attempts=3, delay=0.5):
                await query.answer("ℹ️ Dieser User ist bereits stummgeschaltet.", show_alert=True)
                return

        await context.bot.restrict_chat_member(
            chat_id=scope_chat_id,
            user_id=target_id,
            permissions=ChatPermissions.no_permissions(),
        )
        set_active_mute(scope_chat_id, target_id)

        is_banned_all = bool(groups) and all(is_banned_in_group(g["id"], target_id) for g in groups)
        group_state = await get_info_group_state(context, scope_chat_id, target_id)
        keyboard = build_info_keyboard(scope_chat_id, target_id, True, group_state["is_banned_local"], is_banned_all)
        uname = f"@{target_username} " if target_username else ""
        await query.edit_message_text(
            f"{uname}[<code>{target_id}</code>] wurde 🔇 stummgeschaltet.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="MUTE", details={"user": target_name, "user_id": str(target_id), "gruppe": str(scope_chat_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id)})

    elif data.startswith("info_unmute_"):
        payload = data.replace("info_unmute_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        groups = await get_info_banall_groups(context, scope_chat_id)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        # Prüfen ob tatsächlich gemutet
        if not await is_user_currently_muted(context, scope_chat_id, target_id):
            await query.answer("ℹ️ Dieser User ist nicht stummgeschaltet.", show_alert=True)
            return

        chat = await context.bot.get_chat(scope_chat_id)
        await context.bot.restrict_chat_member(
            chat_id=scope_chat_id,
            user_id=target_id,
            permissions=UNMUTE_PERMISSIONS,
        )
        clear_active_mute(scope_chat_id, target_id)

        if not await wait_for_mute_state(context, scope_chat_id, target_id, False):
            await query.answer("⚠️ Telegram zeigt den User noch kurz als gemutet. Bitte direkt nochmal prüfen.", show_alert=True)
            return

        is_banned_all = bool(groups) and all(is_banned_in_group(g["id"], target_id) for g in groups)
        group_state = await get_info_group_state(context, scope_chat_id, target_id)
        keyboard = build_info_keyboard(scope_chat_id, target_id, False, group_state["is_banned_local"], is_banned_all)
        uname = f"@{target_username} " if target_username else ""
        await query.edit_message_text(
            f"{uname}[<code>{target_id}</code>] wurde ✅ entmutet.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="UNMUTE", details={"user": target_name, "user_id": str(target_id), "gruppe": str(scope_chat_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id)})

    # === CMD UNMUTE BUTTON ===
    elif data.startswith("cmd_unmute_"):
        payload = data.replace("cmd_unmute_", "", 1)
        scope_chat_id_str, target_id_str = payload.rsplit("_", 1)
        scope_chat_id = int(scope_chat_id_str)
        target_id = int(target_id_str)
        tracked = lookup_user(str(target_id))
        target_name = tracked.get("name", str(target_id)) if tracked else str(target_id)
        target_username = tracked.get("username") if tracked else None

        if not await is_user_currently_muted(context, scope_chat_id, target_id):
            await query.answer("ℹ️ Dieser User ist nicht stummgeschaltet.", show_alert=True)
            return

        try:
            chat_obj = await context.bot.get_chat(scope_chat_id)
            await context.bot.restrict_chat_member(
                chat_id=scope_chat_id,
                user_id=target_id,
                permissions=UNMUTE_PERMISSIONS,
            )
            clear_active_mute(scope_chat_id, target_id)
            if not await wait_for_mute_state(context, scope_chat_id, target_id, False):
                await query.answer("⚠️ Telegram zeigt den User noch kurz als gemutet. Bitte direkt nochmal prüfen.", show_alert=True)
                return
            uname = f"@{target_username} " if target_username else ""
            await query.edit_message_text(
                f"{uname}[<code>{target_id}</code>] wurde ✅ entmutet.",
                parse_mode="HTML",
            )
            await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="UNMUTE", details={"user": target_name, "user_id": str(target_id), "gruppe": str(scope_chat_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id), "details": "via Button"})
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

        if not await is_user_currently_banned(context, scope_chat_id, target_id):
            await query.answer("ℹ️ Dieser User ist nicht gebannt.", show_alert=True)
            return

        try:
            await context.bot.unban_chat_member(chat_id=scope_chat_id, user_id=target_id, only_if_banned=True)
            forget_group_ban([scope_chat_id], target_id)
            uname = f"@{target_username} " if target_username else ""
            await query.edit_message_text(
                f"{uname}[<code>{target_id}</code>] wurde ✅ entbannt.",
                parse_mode="HTML",
            )
            await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="UNBAN", details={"user": target_name, "user_id": str(target_id), "gruppe": str(scope_chat_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id), "details": "via Button"})
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
        await log_action(context, "", group_id=scope_chat_id, group_name=str(scope_chat_id), category=LOG_CAT_MOD, action="LINK-WARN CANCEL", details={"user": target_name, "user_id": str(target_id), "gruppe": str(scope_chat_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id)})

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

    elif data == "oc_notify_groups" or data == "oc_source_groups":
        await show_oc_source_groups(query, context)

    elif data.startswith("oc_src_"):
        source_gid = int(data.replace("oc_src_", ""))
        await show_oc_notify_for_source(query, context, source_gid)

    elif data.startswith("oc_ntfy_all_"):
        source_gid = int(data.replace("oc_ntfy_all_", ""))
        groups = await get_bot_groups(context)
        bot_data = load_data()
        per_group = bot_data["open_close"].setdefault("per_group_notify", {})
        per_group[str(source_gid)] = [g["id"] for g in groups if g["id"] != source_gid]
        save_data(bot_data)
        await show_oc_notify_for_source(query, context, source_gid)

    elif data.startswith("oc_ntfy_none_"):
        source_gid = int(data.replace("oc_ntfy_none_", ""))
        bot_data = load_data()
        per_group = bot_data["open_close"].setdefault("per_group_notify", {})
        per_group[str(source_gid)] = []
        save_data(bot_data)
        await show_oc_notify_for_source(query, context, source_gid)

    elif data.startswith("oc_ntfy_"):
        # Format: oc_ntfy_{source_gid}_{target_gid}
        parts = data.replace("oc_ntfy_", "").split("_")
        source_gid = int(parts[0])
        target_gid = int(parts[1])
        bot_data = load_data()
        per_group = bot_data["open_close"].setdefault("per_group_notify", {})
        notify = set(per_group.get(str(source_gid), []))
        if target_gid in notify:
            notify.discard(target_gid)
        else:
            notify.add(target_gid)
        per_group[str(source_gid)] = list(notify)
        save_data(bot_data)
        await show_oc_notify_for_source(query, context, source_gid)

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
            f"<i>Nutze /personal &lt;Name&gt; als Antwort auf eine Nachricht in einer Gruppe, "
            f"um einen Befehl zu erstellen.\n"
            f"Lösche mit /unpersonal &lt;Name&gt;</i>",
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
        keyboard = []
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
                keyboard.append([InlineKeyboardButton(f"✏️ /{name} Gruppen ändern", callback_data=f"pcmd_editgrp_{name}_{i}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="pcmd_menu")])
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

    # === /personal GROUP SELECTION (from group chat) ===
    elif data.startswith("pers_grp_") and data not in ("pers_grp_all", "pers_grp_none", "pers_grp_save", "pers_grp_cancel"):
        gid = int(data.replace("pers_grp_", ""))
        pending = user_data_store.get(user_id, {})
        selected = pending.get("selected", set())
        if gid in selected:
            selected.discard(gid)
        else:
            selected.add(gid)
        pending["selected"] = selected
        user_data_store[user_id] = pending
        await _render_pers_grp_menu(query, pending)

    elif data == "pers_grp_all":
        pending = user_data_store.get(user_id, {})
        bot_data = load_data()
        pending["selected"] = {g["id"] for g in bot_data.get("groups", [])}
        user_data_store[user_id] = pending
        await _render_pers_grp_menu(query, pending)

    elif data == "pers_grp_none":
        pending = user_data_store.get(user_id, {})
        pending["selected"] = set()
        user_data_store[user_id] = pending
        await _render_pers_grp_menu(query, pending)

    elif data == "pers_grp_save":
        pending = user_data_store.get(user_id, {})
        cmd_name = pending.get("cmd_name", "")
        cmd_data = pending.get("cmd_data", {})
        selected = pending.get("selected", set())
        cmd_data["groups"] = list(selected)

        bot_data = load_data()
        cmds = bot_data.setdefault("personal_commands", {})
        existing = cmds.get(cmd_name, [])
        if not isinstance(existing, list):
            existing = [existing]
        existing.append(cmd_data)
        cmds[cmd_name] = existing
        save_data(bot_data)
        user_data_store.pop(user_id, None)

        grp_text = f"{len(selected)} Gruppen" if selected else "alle Gruppen"
        keyboard = [[InlineKeyboardButton("🔙 Menü", callback_data="pcmd_menu")]]
        await query.edit_message_text(
            f"✅ Befehl /<b>{html.escape(cmd_name)}</b> gespeichert für {grp_text}!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        await log_action(context, f"PERSONAL CMD: /{cmd_name} erstellt von {query.from_user.full_name} für {grp_text}")

    elif data == "pers_grp_cancel":
        user_data_store.pop(user_id, None)
        await query.edit_message_text("❌ Abgebrochen.")

    # === EDIT GROUPS FOR EXISTING PERSONAL COMMAND ===
    elif data.startswith("pcmd_editgrp_"):
        parts = data.replace("pcmd_editgrp_", "").rsplit("_", 1)
        cmd_name = parts[0]
        idx = int(parts[1]) if len(parts) > 1 else 0
        bot_data = load_data()
        cmds = bot_data.get("personal_commands", {})
        entries = cmds.get(cmd_name, [])
        if not isinstance(entries, list):
            entries = [entries]
        if idx >= len(entries):
            await query.answer("Befehl nicht gefunden.", show_alert=True)
            return
        current_groups = set(entries[idx].get("groups", []))
        user_data_store[user_id] = {
            "action": "pcmd_edit_groups",
            "cmd_name": cmd_name,
            "cmd_idx": idx,
            "selected": current_groups,
        }
        await _render_pcmd_editgrp_menu(query, cmd_name, current_groups)

    elif data.startswith("pcmd_egrp_") and data not in ("pcmd_egrp_all", "pcmd_egrp_none", "pcmd_egrp_save"):
        gid = int(data.replace("pcmd_egrp_", ""))
        pending = user_data_store.get(user_id, {})
        selected = pending.get("selected", set())
        if gid in selected:
            selected.discard(gid)
        else:
            selected.add(gid)
        pending["selected"] = selected
        user_data_store[user_id] = pending
        await _render_pcmd_editgrp_menu(query, pending["cmd_name"], selected)

    elif data == "pcmd_egrp_all":
        pending = user_data_store.get(user_id, {})
        bot_data = load_data()
        pending["selected"] = {g["id"] for g in bot_data.get("groups", [])}
        user_data_store[user_id] = pending
        await _render_pcmd_editgrp_menu(query, pending["cmd_name"], pending["selected"])

    elif data == "pcmd_egrp_none":
        pending = user_data_store.get(user_id, {})
        pending["selected"] = set()
        user_data_store[user_id] = pending
        await _render_pcmd_editgrp_menu(query, pending["cmd_name"], pending["selected"])

    elif data == "pcmd_egrp_save":
        pending = user_data_store.get(user_id, {})
        cmd_name = pending.get("cmd_name", "")
        idx = pending.get("cmd_idx", 0)
        selected = pending.get("selected", set())
        bot_data = load_data()
        cmds = bot_data.get("personal_commands", {})
        entries = cmds.get(cmd_name, [])
        if not isinstance(entries, list):
            entries = [entries]
        if idx < len(entries):
            entries[idx]["groups"] = list(selected)
            cmds[cmd_name] = entries
            save_data(bot_data)
        user_data_store.pop(user_id, None)
        grp_text = f"{len(selected)} Gruppen" if selected else "alle Gruppen"
        keyboard = [[InlineKeyboardButton("🔙 Zur Liste", callback_data="pcmd_list")]]
        await query.edit_message_text(
            f"✅ /<b>{html.escape(cmd_name)}</b> aktualisiert — gilt jetzt für {grp_text}.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

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
        skipped_count = 0

        if "unban" in action:
            for gid in selected:
                banned_ids = get_tracked_banned_user_ids(gid)

                group_obj = next((g for g in (await get_bot_groups(context)) if g["id"] == gid), None)
                group_title = group_obj["title"] if group_obj else str(gid)

                logger.info(f"Mass unban: {len(banned_ids)} tracked banned users in {group_title}")

                if not banned_ids:
                    skipped_count += 1
                    continue

                try:
                    await query.edit_message_text(
                        f"⏳ Entbanne {len(banned_ids)} User in <b>{html.escape(group_title)}</b>...",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                for uid in banned_ids:
                    try:
                        await context.bot.unban_chat_member(chat_id=gid, user_id=uid, only_if_banned=True)
                        success_count += 1
                    except Exception as e:
                        logger.error(f"Mass unban failed for {uid} in {gid}: {e}")
                        error_count += 1
                    await asyncio.sleep(0.3)  # Rate-Limit Schutz

                # Clear tracked bans for this group
                bot_data = load_data()
                bot_data.setdefault("banned_users", {})[str(gid)] = {}
                save_data(bot_data)
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
        # Send notification to each selected group
        groups = await get_bot_groups(context)
        for gid in selected:
            group_title = next((g["title"] for g in groups if g["id"] == gid), str(gid))
            try:
                await context.bot.send_message(
                    chat_id=gid,
                    text=f"✅ <b>All {action_label} abgeschlossen</b>\n\n"
                         f"✅ Erfolgreich: {success_count}\n"
                         f"❌ Fehler: {error_count}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to send {action_label} notification to {gid}: {e}")
        await log_action(context, "", category=LOG_CAT_MOD, action=f"MASS {action_label.upper()}", details={"von": query.from_user.full_name, "von_id": str(query.from_user.id), "ergebnis": f"{success_count} OK, {error_count} Fehler"})

    # === FREIGABEMODUS ===
    elif data == "menu_freigabe":
        bot_data = load_data()
        auto_approve = bot_data.get("auto_approve", {})
        groups = await get_bot_groups(context)

        if not groups:
            keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="back_main")]]
            await query.edit_message_text(
                "🚪 <b>Freigabemodus</b>\n\nKeine Gruppen registriert.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
            return

        enabled_count = sum(1 for g in groups if auto_approve.get(str(g["id"]), False))

        text = (
            "📬 <b>Freigabemodus</b>\n\n"
            "In diesem Menü kannst du entscheiden, ob du die Verwaltung der "
            "Gruppenbeitritts-Freigabe an den Bot delegieren möchtest, sobald "
            "ein Benutzer den Beitritt über einen freigabepflichtigen Link beantragt.\n\n"
            "🧠 Da das Captcha nicht aktiv ist und du die Auto-Freigabe aktivierst, "
            "werden die <b>Nutzer automatisch in die Gruppe aufgenommen</b>, "
            "sobald sie die Anfrage stellen (es sei denn, es wird eine andere Prüfung durchgeführt).\n\n"
            "🔦 Falls die automatische Genehmigung aktiviert ist, werden alle Prüfungen "
            "(Namenssperre, Gebannt...) durchgeführt, bevor der Nutzer der Gruppe beitritt – "
            "falls nicht bestanden, wird der Benutzer <b>nicht genehmigt</b>.\n\n"
            "👥 Wenn ein Benutzer über einen Link beitritt, der <u>nicht genehmigungspflichtig</u> ist, "
            "erfolgt die Prozedur ganz normal <b>in der Gruppe</b>.\n\n"
            f"💡 <b>Status:</b> {enabled_count}/{len(groups)} Gruppen aktiv"
        )

        keyboard = []
        for g in groups:
            gid_str = str(g["id"])
            is_on = auto_approve.get(gid_str, False)
            status = "✅" if is_on else "❌"
            keyboard.append([InlineKeyboardButton(
                f"{status} {g['title']}", callback_data=f"freigabe_toggle_{g['id']}"
            )])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("freigabe_toggle_"):
        gid = int(data.replace("freigabe_toggle_", ""))
        bot_data = load_data()
        auto_approve = bot_data.setdefault("auto_approve", {})
        gid_str = str(gid)
        current = auto_approve.get(gid_str, False)
        auto_approve[gid_str] = not current
        save_data(bot_data)

        new_state = "aktiviert ✅" if not current else "deaktiviert ❌"
        groups = await get_bot_groups(context)
        group_title = next((g["title"] for g in groups if g["id"] == gid), str(gid))
        try:
            await query.answer(f"Auto-Freigabe für {group_title} {new_state}", show_alert=False)
        except Exception:
            pass
        await log_action(context, f"Freigabemodus {new_state} für {group_title} von {query.from_user.full_name}", category=LOG_CAT_ADMIN, action="Freigabemodus", details={"details": f"{new_state} für {group_title}", "von": query.from_user.full_name})

        # Re-render menu
        enabled_count = sum(1 for g in groups if auto_approve.get(str(g["id"]), False))
        text = (
            "📬 <b>Freigabemodus</b>\n\n"
            "In diesem Menü kannst du entscheiden, ob du die Verwaltung der "
            "Gruppenbeitritts-Freigabe an den Bot delegieren möchtest, sobald "
            "ein Benutzer den Beitritt über einen freigabepflichtigen Link beantragt.\n\n"
            "🧠 Da das Captcha nicht aktiv ist und du die Auto-Freigabe aktivierst, "
            "werden die <b>Nutzer automatisch in die Gruppe aufgenommen</b>, "
            "sobald sie die Anfrage stellen (es sei denn, es wird eine andere Prüfung durchgeführt).\n\n"
            "🔦 Falls die automatische Genehmigung aktiviert ist, werden alle Prüfungen "
            "(Namenssperre, Gebannt...) durchgeführt, bevor der Nutzer der Gruppe beitritt – "
            "falls nicht bestanden, wird der Benutzer <b>nicht genehmigt</b>.\n\n"
            "👥 Wenn ein Benutzer über einen Link beitritt, der <u>nicht genehmigungspflichtig</u> ist, "
            "erfolgt die Prozedur ganz normal <b>in der Gruppe</b>.\n\n"
            f"💡 <b>Status:</b> {enabled_count}/{len(groups)} Gruppen aktiv"
        )
        keyboard = []
        for g in groups:
            gs = str(g["id"])
            is_on = auto_approve.get(gs, False)
            status = "✅" if is_on else "❌"
            keyboard.append([InlineKeyboardButton(
                f"{status} {g['title']}", callback_data=f"freigabe_toggle_{g['id']}"
            )])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # === PROTOKOLL ===
    elif data == "menu_protokoll":
        bot_data = load_data()
        proto_channels = bot_data.get("protokoll_channels", {})
        bot_me = await context.bot.get_me()
        bot_username = bot_me.username

        admin_channels = {k: v for k, v in proto_channels.items() if v.get("type") == "admin"}
        mod_channels = {k: v for k, v in proto_channels.items() if v.get("type", "mod") == "mod"}

        text = (
            f"📋 <b>Protokoll-System</b>\n\n"
            f"Konfiguriere zwei Arten von Protokoll-Kanälen:\n\n"
            f"⚙️ <b>Admin-Log</b> — Bot-Einstellungen, Admin-Änderungen\n"
            f"🛡 <b>Moderations-Log</b> — Bans, Mutes, Filter-Treffer\n\n"
            f"ℹ️ @{bot_username} muss Admin im Kanal sein."
        )

        if admin_channels:
            text += "\n\n⚙️ <b>Admin-Log:</b>"
            for ch_id, ch_cfg in admin_channels.items():
                text += f"\n• {html.escape(ch_cfg.get('name', ch_id))}"

        if mod_channels:
            text += "\n\n🛡 <b>Moderations-Log:</b>"
            for ch_id, ch_cfg in mod_channels.items():
                ch_name = ch_cfg.get("name", ch_id)
                ch_groups = ch_cfg.get("groups", [])
                if "all" in ch_groups:
                    scope = "Alle Gruppen"
                else:
                    names = []
                    for gid in ch_groups:
                        found = next((g["title"] for g in bot_data.get("groups", []) if str(g["id"]) == str(gid)), str(gid))
                        names.append(found)
                    scope = ", ".join(names) if names else "Keine Gruppen"
                text += f"\n• {html.escape(ch_name)} → {scope}"

        keyboard = [
            [InlineKeyboardButton("⚙️ Admin-Log hinzufügen", callback_data="proto_add_admin"),
             InlineKeyboardButton("🛡 Mod-Log hinzufügen", callback_data="proto_add_mod")],
        ]
        if proto_channels:
            keyboard.append([InlineKeyboardButton("🎯 Kanäle konfigurieren", callback_data="proto_what")])
            keyboard.append([InlineKeyboardButton("🗑 Kanal entfernen", callback_data="proto_remove_menu")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data in ("proto_add", "proto_add_admin", "proto_add_mod"):
        proto_type = "admin" if data == "proto_add_admin" else "mod"
        context.user_data["state"] = WAITING_PROTO_CHANNEL
        context.user_data["proto_add_type"] = proto_type
        type_label = "Admin-Log ⚙️" if proto_type == "admin" else "Moderations-Log 🛡"
        keyboard = [[InlineKeyboardButton("🔙 Abbrechen", callback_data="menu_protokoll")]]
        await query.edit_message_text(
            f"📋 <b>{type_label} — Kanal hinzufügen</b>\n\n"
            f"Sende mir die <b>Kanal-ID</b> (z.B. <code>-1001234567890</code>).\n\n"
            f"💡 Die Kanal-ID findest du z.B. über @userinfobot.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "proto_remove_menu":
        bot_data = load_data()
        proto_channels = bot_data.get("protokoll_channels", {})
        keyboard = []
        for ch_id, ch_cfg in proto_channels.items():
            ch_name = ch_cfg.get("name", ch_id)
            ch_type = ch_cfg.get("type", "mod")
            icon = "⚙️" if ch_type == "admin" else "🛡"
            keyboard.append([InlineKeyboardButton(f"🗑 {icon} {ch_name}", callback_data=f"proto_rm_{ch_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_protokoll")])
        await query.edit_message_text(
            "🗑 <b>Protokoll-Kanal entfernen</b>\nWähle den Kanal:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("proto_rm_"):
        ch_id = data.replace("proto_rm_", "")
        bot_data = load_data()
        removed = bot_data.get("protokoll_channels", {}).pop(ch_id, None)
        save_data(bot_data)
        name = removed.get("name", ch_id) if removed else ch_id
        await query.answer(f"✅ {name} entfernt!", show_alert=True)
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_protokoll")]]
        await query.edit_message_text(f"✅ Protokoll-Kanal <code>{html.escape(name)}</code> entfernt.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "proto_what":
        bot_data = load_data()
        proto_channels = bot_data.get("protokoll_channels", {})
        keyboard = []
        for ch_id, ch_cfg in proto_channels.items():
            ch_name = ch_cfg.get("name", ch_id)
            ch_type = ch_cfg.get("type", "mod")
            icon = "⚙️" if ch_type == "admin" else "🛡"
            keyboard.append([InlineKeyboardButton(f"{icon} {ch_name}", callback_data=f"proto_cfg_{ch_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_protokoll")])
        await query.edit_message_text(
            "🎯 <b>Kanal konfigurieren</b>\nWähle einen Kanal:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("proto_cfg_"):
        ch_id = data.replace("proto_cfg_", "")
        await render_protokoll_channel_config(query, ch_id)

    elif data.startswith("proto_switch_admin_"):
        ch_id = data.replace("proto_switch_admin_", "")
        bot_data = load_data()
        ch_cfg = bot_data.get("protokoll_channels", {}).get(ch_id, {})
        ch_cfg["type"] = "admin"
        ch_cfg["groups"] = []
        bot_data["protokoll_channels"][ch_id] = ch_cfg
        save_data(bot_data)
        await query.answer("✅ Auf Admin-Log gewechselt!")
        await render_protokoll_channel_config(query, ch_id)

    elif data.startswith("proto_switch_mod_"):
        ch_id = data.replace("proto_switch_mod_", "")
        bot_data = load_data()
        ch_cfg = bot_data.get("protokoll_channels", {}).get(ch_id, {})
        ch_cfg["type"] = "mod"
        ch_cfg["groups"] = ["all"]
        bot_data["protokoll_channels"][ch_id] = ch_cfg
        save_data(bot_data)
        await query.answer("✅ Auf Moderations-Log gewechselt!")
        await render_protokoll_channel_config(query, ch_id)

    elif data.startswith("proto_tga_"):
        ch_id = data.replace("proto_tga_", "")
        bot_data = load_data()
        ch_cfg = bot_data.get("protokoll_channels", {}).get(ch_id, {})
        ch_groups = ch_cfg.get("groups", [])
        if "all" in ch_groups:
            ch_groups.remove("all")
        else:
            ch_groups = ["all"]
        ch_cfg["groups"] = ch_groups
        bot_data["protokoll_channels"][ch_id] = ch_cfg
        save_data(bot_data)
        await query.answer("✅ Aktualisiert")
        await render_protokoll_channel_config(query, ch_id)

    elif data.startswith("proto_tgg_"):
        # proto_tgg_{group_id}_{channel_id}
        rest = data.replace("proto_tgg_", "")
        # group IDs are negative, so split from the right
        parts = rest.rsplit("_", 1)
        if len(parts) != 2:
            return
        gid_str, ch_id = parts
        bot_data = load_data()
        ch_cfg = bot_data.get("protokoll_channels", {}).get(ch_id, {})
        ch_groups = ch_cfg.get("groups", [])
        if "all" in ch_groups:
            ch_groups.remove("all")
        if gid_str in [str(x) for x in ch_groups]:
            ch_groups = [x for x in ch_groups if str(x) != gid_str]
        else:
            ch_groups.append(gid_str)
        ch_cfg["groups"] = ch_groups
        bot_data["protokoll_channels"][ch_id] = ch_cfg
        save_data(bot_data)
        await query.answer("✅ Aktualisiert")
        await render_protokoll_channel_config(query, ch_id)


    # === @ADMIN / REPORT MENU ===
    elif data == "menu_admin_report":
        await _render_admin_report_menu(query)

    elif data == "ar_toggle":
        bot_data = load_data()
        ar = bot_data.setdefault("admin_report", {"active": False, "staff_group": None, "notify_users": [], "group_routes": {}})
        ar["active"] = not ar.get("active", False)
        save_data(bot_data)
        await query.answer(f"{'✅ Aktiviert' if ar['active'] else '❌ Deaktiviert'}")
        await _render_admin_report_menu(query)

    elif data == "ar_set_group":
        context.user_data["state"] = "ar_set_group"
        await query.edit_message_text(
            "👥 Sende mir die <b>Chat-ID</b> der Mitarbeitergruppe.\n\n"
            "💡 Tipp: Leite eine Nachricht aus der Gruppe weiter oder nutze @userinfobot um die ID herauszufinden.",
            parse_mode="HTML",
        )

    elif data == "ar_notify_menu":
        bot_data = load_data()
        ar = bot_data.get("admin_report", {})
        notify_users = ar.get("notify_users", [])
        keyboard = []
        for uid in notify_users:
            users_db = load_users()
            name = users_db.get(str(uid), {}).get("name", str(uid))
            keyboard.append([InlineKeyboardButton(f"❌ {name}", callback_data=f"ar_notify_remove_{uid}")])
        keyboard.append([InlineKeyboardButton("➕ Benutzer hinzufügen", callback_data="ar_notify_add")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_admin_report")])
        await query.edit_message_text(
            "🔔 <b>Benutzer benachrichtigen</b>\n\n"
            "Diese Benutzer werden bei einer @admin-Meldung per Erwähnung benachrichtigt:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "ar_notify_add":
        context.user_data["state"] = "ar_notify_add"
        await query.edit_message_text(
            "🔔 Sende mir die <b>User-ID</b> des Benutzers, der benachrichtigt werden soll.",
            parse_mode="HTML",
        )

    elif data.startswith("ar_notify_remove_"):
        uid_to_remove = int(data.split("_", 3)[3])
        bot_data = load_data()
        ar = bot_data.setdefault("admin_report", {"active": False, "staff_group": None, "notify_users": []})
        if uid_to_remove in ar.get("notify_users", []):
            ar["notify_users"].remove(uid_to_remove)
            save_data(bot_data)
        await query.answer("✅ Entfernt")
        # Re-render notify menu inline
        notify_users = ar.get("notify_users", [])
        keyboard = []
        for uid in notify_users:
            name = load_users().get(str(uid), {}).get("name", str(uid))
            keyboard.append([InlineKeyboardButton(f"❌ {name}", callback_data=f"ar_notify_remove_{uid}")])
        keyboard.append([InlineKeyboardButton("➕ Benutzer hinzufügen", callback_data="ar_notify_add")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_admin_report")])
        await query.edit_message_text(
            "🔔 <b>Benutzer benachrichtigen</b>\n\n"
            "Diese Benutzer werden bei einer @admin-Meldung per Erwähnung benachrichtigt:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # === @ADMIN GROUP ROUTING (Target-based) ===
    elif data == "ar_routes_menu":
        bot_data = load_data()
        ar = bot_data.get("admin_report", {})
        group_routes = ar.get("group_routes", {})  # {src_id_str: dst_id_int}
        route_also_default = ar.get("route_also_default", {})
        groups = bot_data.get("groups", [])
        gmap = {g["id"]: g["title"] for g in groups}

        # Group by target
        targets = {}  # {dst_id: [src_ids]}
        for src_id, dst_id in group_routes.items():
            targets.setdefault(dst_id, []).append(src_id)

        text = "📋 <b>Gruppen-Routing</b>\n\nZiel-Kanal festlegen → Gruppen zuweisen.\n"
        if targets:
            text += "\n<b>Aktive Routen:</b>\n"
            for dst_id, src_ids in targets.items():
                dst_name = gmap.get(dst_id, str(dst_id))
                src_names = [gmap.get(int(s), s) for s in src_ids]
                text += f"\n📌 <code>{dst_id}</code> ({dst_name}):\n"
                for sn in src_names:
                    text += f"  • {sn}\n"

        keyboard = []
        if targets:
            for dst_id in targets:
                dst_name = gmap.get(dst_id, str(dst_id))
                keyboard.append([InlineKeyboardButton(f"✏️ {dst_name} ({dst_id})", callback_data=f"ar_target_{dst_id}")])
        keyboard.append([InlineKeyboardButton("➕ Neues Ziel hinzufügen", callback_data="ar_target_new")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_admin_report")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "ar_target_new":
        context.user_data["state"] = "ar_target_new_input"
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="ar_routes_menu")]]
        await query.edit_message_text(
            "📋 <b>Neues Routing-Ziel</b>\n\n"
            "Sende jetzt die <b>Chat-ID</b> des Ziel-Kanals/Gruppe.\n"
            "<i>(z.B. -1001234567890)</i>",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("ar_target_del_"):
        dst_id = int(data.split("ar_target_del_")[1])
        bot_data = load_data()
        ar = bot_data.get("admin_report", {})
        routes = ar.get("group_routes", {})
        also = ar.get("route_also_default", {})
        # Remove all routes pointing to this target
        to_remove = [s for s, d in routes.items() if d == dst_id]
        for s in to_remove:
            routes.pop(s, None)
            also.pop(s, None)
        save_data(bot_data)
        await query.answer("✅ Ziel und alle Routen entfernt")
        await _render_admin_report_menu(query)

    elif data.startswith("ar_target_") and not data.startswith("ar_target_grp_") and not data.startswith("ar_target_also_") and not data.startswith("ar_target_del_") and not data.startswith("ar_target_new") and not data.startswith("ar_target_chid_"):
        dst_id = int(data.split("ar_target_")[1])
        bot_data = load_data()
        ar = bot_data.get("admin_report", {})
        group_routes = ar.get("group_routes", {})
        route_also_default = ar.get("route_also_default", {})
        groups = bot_data.get("groups", [])
        gmap = {g["id"]: g["title"] for g in groups}
        dst_name = gmap.get(dst_id, str(dst_id))

        # Which groups currently route to this target
        assigned = {int(s) for s, d in group_routes.items() if d == dst_id}

        text = f"📌 <b>Ziel: {dst_name}</b>\n<code>{dst_id}</code>\n\nWähle Gruppen aus/ab:"
        keyboard = []
        for g in groups:
            is_assigned = g["id"] in assigned
            icon = "✅" if is_assigned else "❌"
            keyboard.append([InlineKeyboardButton(f"{icon} {g['title']}", callback_data=f"ar_target_grp_{dst_id}_{g['id']}")])
        # Also-default toggle for all assigned groups
        if assigned:
            # Show toggle per assigned group
            for gid in assigned:
                gname = gmap.get(gid, str(gid))
                also = route_also_default.get(str(gid), True)
                aicon = "✅" if also else "❌"
                keyboard.append([InlineKeyboardButton(f"{aicon} {gname} → +Standard", callback_data=f"ar_target_also_{dst_id}_{gid}")])
        keyboard.append([InlineKeyboardButton("✏️ Ziel-ID ändern", callback_data=f"ar_target_chid_{dst_id}")])
        keyboard.append([InlineKeyboardButton("🗑 Ziel löschen", callback_data=f"ar_target_del_{dst_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="ar_routes_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("ar_target_grp_"):
        parts = data.split("_")
        dst_id = int(parts[3])
        grp_id = int(parts[4])
        bot_data = load_data()
        ar = bot_data.setdefault("admin_report", {})
        routes = ar.setdefault("group_routes", {})
        also = ar.setdefault("route_also_default", {})
        if routes.get(str(grp_id)) == dst_id:
            # Remove
            routes.pop(str(grp_id), None)
            also.pop(str(grp_id), None)
            await query.answer("❌ Gruppe entfernt")
        else:
            routes[str(grp_id)] = dst_id
            await query.answer("✅ Gruppe hinzugefügt")
        save_data(bot_data)
        # Re-render target page
        groups = bot_data.get("groups", [])
        gmap = {g["id"]: g["title"] for g in groups}
        dst_name = gmap.get(dst_id, str(dst_id))
        assigned = {int(s) for s, d in routes.items() if d == dst_id}
        text = f"📌 <b>Ziel: {dst_name}</b>\n<code>{dst_id}</code>\n\nWähle Gruppen aus/ab:"
        keyboard = []
        for g in groups:
            is_assigned = g["id"] in assigned
            icon = "✅" if is_assigned else "❌"
            keyboard.append([InlineKeyboardButton(f"{icon} {g['title']}", callback_data=f"ar_target_grp_{dst_id}_{g['id']}")])
        if assigned:
            for gid in assigned:
                gname = gmap.get(gid, str(gid))
                a = also.get(str(gid), True)
                aicon = "✅" if a else "❌"
                keyboard.append([InlineKeyboardButton(f"{aicon} {gname} → +Standard", callback_data=f"ar_target_also_{dst_id}_{gid}")])
        keyboard.append([InlineKeyboardButton("✏️ Ziel-ID ändern", callback_data=f"ar_target_chid_{dst_id}")])
        keyboard.append([InlineKeyboardButton("🗑 Ziel löschen", callback_data=f"ar_target_del_{dst_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="ar_routes_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("ar_target_also_"):
        parts = data.split("_")
        dst_id = int(parts[3])
        grp_id = int(parts[4])
        bot_data = load_data()
        ar = bot_data.setdefault("admin_report", {})
        route_also = ar.setdefault("route_also_default", {})
        current = route_also.get(str(grp_id), True)
        route_also[str(grp_id)] = not current
        save_data(bot_data)
        await query.answer("✅ +Standard" if not current else "❌ Nur Route")
        # Re-render target page
        routes = ar.get("group_routes", {})
        groups = bot_data.get("groups", [])
        gmap = {g["id"]: g["title"] for g in groups}
        dst_name = gmap.get(dst_id, str(dst_id))
        assigned = {int(s) for s, d in routes.items() if d == dst_id}
        text = f"📌 <b>Ziel: {dst_name}</b>\n<code>{dst_id}</code>\n\nWähle Gruppen aus/ab:"
        keyboard = []
        for g in groups:
            is_assigned = g["id"] in assigned
            icon = "✅" if is_assigned else "❌"
            keyboard.append([InlineKeyboardButton(f"{icon} {g['title']}", callback_data=f"ar_target_grp_{dst_id}_{g['id']}")])
        if assigned:
            for gid in assigned:
                gname = gmap.get(gid, str(gid))
                a = route_also.get(str(gid), True)
                aicon = "✅" if a else "❌"
                keyboard.append([InlineKeyboardButton(f"{aicon} {gname} → +Standard", callback_data=f"ar_target_also_{dst_id}_{gid}")])
        keyboard.append([InlineKeyboardButton("✏️ Ziel-ID ändern", callback_data=f"ar_target_chid_{dst_id}")])
        keyboard.append([InlineKeyboardButton("🗑 Ziel löschen", callback_data=f"ar_target_del_{dst_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="ar_routes_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("ar_target_chid_"):
        dst_id = int(data.split("ar_target_chid_")[1])
        context.user_data["state"] = f"ar_target_change_{dst_id}"
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data=f"ar_target_{dst_id}")]]
        await query.edit_message_text(
            f"✏️ <b>Neue Ziel-ID eingeben</b>\n\nAktuelle ID: <code>{dst_id}</code>\n\n"
            f"Sende die neue Chat-ID:",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("ar_solved_"):
        parts = data.split("_")
        # ar_solved_{chat_id}_{sender_id}
        solver = update.effective_user
        solver_name = solver.full_name if solver else "Unbekannt"
        original_text = query.message.text or query.message.caption or ""
        solved_text = (
            f"{original_text}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"✅ <b>Gelöst</b> von {solver_name}\n"
            f"🕐 {now_de().strftime('%d.%m.%Y %H:%M')}"
        )
        await query.edit_message_text(solved_text, parse_mode="HTML")
        await query.answer("✅ Als gelöst markiert")

    # === SETTINGS ===
    elif data == "menu_settings":
        if not is_owner(user_id):
            await query.edit_message_text("⛔ Nur für Owner.")
            return
        cfg = load_config()
        admins = cfg.get("admin_ids", [])
        log_ch = cfg.get("log_channel_id", "Nicht gesetzt")
        users = load_users()
        user_count = len(users)
        data = load_data()
        group_count = len(data.get("groups", []))
        banned_count = len(data.get("banned_users", {}))
        text = (
            f"⚙️ *Einstellungen*\n\n"
            f"👥 Bekannte User: {user_count}\n"
            f"📊 Gruppen: {group_count}\n"
            f"🚫 Gebannte User: {banned_count}\n"
            f"👮 Admins: {len(admins)}\n"
            f"📋 Log-Kanal: `{log_ch}`"
        )
        exempt_count = len(data.get("exempt_groups", []))
        keyboard = [
            [InlineKeyboardButton("👮 Admins verwalten", callback_data="settings_admins")],
            [InlineKeyboardButton("➕ Admin hinzufügen", callback_data="add_admin"),
             InlineKeyboardButton("➖ Admin entfernen", callback_data="remove_admin")],
            [InlineKeyboardButton("📋 Log-Kanal setzen", callback_data="set_log")],
            [InlineKeyboardButton("👥 Gruppen anzeigen", callback_data="show_groups")],
            [InlineKeyboardButton(f"🛡 Filterfreie Gruppen ({exempt_count})", callback_data="menu_exempt_groups")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "menu_exempt_groups":
        if not is_owner(user_id):
            await query.answer("⛔ Nur für Owner.", show_alert=True)
            return
        bot_data = load_data()
        exempt = set(bot_data.get("exempt_groups", []))
        groups = bot_data.get("groups", [])
        gmap = {g["id"]: g["title"] for g in groups}

        text = (
            "🛡 <b>Filterfreie Gruppen</b>\n\n"
            "Gruppen ohne Einschränkungen — alle Filter (verbotene Wörter, Links, Forwards) sind deaktiviert.\n\n"
            "Ideal für Team-/Mitarbeiter-Gruppen.\n"
        )
        if exempt:
            text += "\n<b>Aktive Befreiungen:</b>\n"
            for gid in exempt:
                name = gmap.get(gid, str(gid))
                text += f"  • ✅ {name}\n"

        keyboard = []
        for g in groups:
            is_exempt = g["id"] in exempt
            icon = "✅" if is_exempt else "❌"
            keyboard.append([InlineKeyboardButton(f"{icon} {g['title']}", callback_data=f"toggle_exempt_{g['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("toggle_exempt_"):
        if not is_owner(user_id):
            await query.answer("⛔ Nur für Owner.", show_alert=True)
            return
        gid = int(data.split("toggle_exempt_")[1])
        bot_data = load_data()
        exempt = bot_data.setdefault("exempt_groups", [])
        if gid in exempt:
            exempt.remove(gid)
            await query.answer("❌ Filter aktiviert")
        else:
            exempt.append(gid)
            await query.answer("✅ Gruppe befreit")
        save_data(bot_data)
        # Re-render
        groups = bot_data.get("groups", [])
        gmap = {g["id"]: g["title"] for g in groups}
        exempt_set = set(exempt)
        text = (
            "🛡 <b>Filterfreie Gruppen</b>\n\n"
            "Gruppen ohne Einschränkungen — alle Filter (verbotene Wörter, Links, Forwards) sind deaktiviert.\n\n"
            "Ideal für Team-/Mitarbeiter-Gruppen.\n"
        )
        if exempt_set:
            text += "\n<b>Aktive Befreiungen:</b>\n"
            for eid in exempt_set:
                name = gmap.get(eid, str(eid))
                text += f"  • ✅ {name}\n"
        kb = []
        for g in groups:
            is_ex = g["id"] in exempt_set
            icon = "✅" if is_ex else "❌"
            kb.append([InlineKeyboardButton(f"{icon} {g['title']}", callback_data=f"toggle_exempt_{g['id']}")])
        kb.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


        if not is_owner(user_id):
            await query.answer("⛔ Nur für Owner.", show_alert=True)
            return
        cfg = load_config()
        admins = cfg.get("admin_ids", [])
        owners = cfg.get("owner_ids", [])
        text = "👮 <b>Bot-Admins</b>\n\n"
        text += "<b>👑 Owner:</b>\n"
        for oid in owners:
            tracked = lookup_user(str(oid))
            name = tracked.get("name", str(oid)) if tracked else str(oid)
            text += f"  • {html.escape(name)} (<code>{oid}</code>)\n"
        text += f"\n<b>🛡️ Admins ({len(admins)}):</b>\n"
        keyboard = []
        if admins:
            for aid in admins:
                tracked = lookup_user(str(aid))
                name = tracked.get("name", str(aid)) if tracked else str(aid)
                text += f"  • {html.escape(name)} (<code>{aid}</code>)\n"
                keyboard.append([InlineKeyboardButton(f"❌ {name} entfernen", callback_data=f"settings_rmadmin_{aid}")])
        else:
            text += "  <i>Keine Admins konfiguriert.</i>\n"
        keyboard.append([InlineKeyboardButton("♻️ Adminliste zurücksetzen", callback_data="settings_reset_admins")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "settings_reset_admins":
        if not is_owner(user_id):
            await query.answer("⛔ Nur für Owner.", show_alert=True)
            return
        cfg = load_config()
        cfg["admin_ids"] = [8394295062]
        save_config(cfg)
        await query.answer("✅ Adminliste wurde zurückgesetzt!", show_alert=True)
        await log_action(context, f"Adminliste zurückgesetzt von {query.from_user.full_name}", category=LOG_CAT_ADMIN, action="Adminliste Reset", details={"von": f"{query.from_user.full_name} ({query.from_user.id})"})

        admins = cfg.get("admin_ids", [])
        owners = cfg.get("owner_ids", [])
        text = "👮 <b>Bot-Admins</b>\n\n"
        text += "<b>👑 Owner:</b>\n"
        for oid in owners:
            tracked = lookup_user(str(oid))
            name = tracked.get("name", str(oid)) if tracked else str(oid)
            text += f"  • {html.escape(name)} (<code>{oid}</code>)\n"
        text += f"\n<b>🛡️ Admins ({len(admins)}):</b>\n"
        for aid in admins:
            tracked = lookup_user(str(aid))
            name = tracked.get("name", str(aid)) if tracked else str(aid)
            text += f"  • {html.escape(name)} (<code>{aid}</code>)\n"
        keyboard = [[InlineKeyboardButton(f"❌ {lookup_user(str(admins[0])).get('name', str(admins[0])) if admins and lookup_user(str(admins[0])) else str(admins[0])} entfernen", callback_data=f"settings_rmadmin_{admins[0]}")]] if admins else []
        keyboard.append([InlineKeyboardButton("♻️ Adminliste zurücksetzen", callback_data="settings_reset_admins")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("settings_rmadmin_"):
        if not is_owner(user_id):
            await query.answer("⛔ Nur für Owner.", show_alert=True)
            return
        aid = int(data.replace("settings_rmadmin_", ""))
        cfg = load_config()
        if aid in cfg.get("admin_ids", []):
            cfg["admin_ids"].remove(aid)
            save_config(cfg)
            tracked = lookup_user(str(aid))
            name = tracked.get("name", str(aid)) if tracked else str(aid)
            await query.answer(f"✅ {name} entfernt!", show_alert=True)
            await log_action(context, f"Admin entfernt (Menü): {name} ({aid})", category=LOG_CAT_ADMIN, action="Admin entfernt", details={"user": name, "user_id": str(aid), "von": query.from_user.full_name})
        else:
            await query.answer("Nicht in der Admin-Liste.", show_alert=True)
        # Re-render admin list
        cfg = load_config()
        admins = cfg.get("admin_ids", [])
        owners = cfg.get("owner_ids", [])
        text = "👮 <b>Bot-Admins</b>\n\n"
        text += "<b>👑 Owner:</b>\n"
        for oid in owners:
            tracked = lookup_user(str(oid))
            name = tracked.get("name", str(oid)) if tracked else str(oid)
            text += f"  • {html.escape(name)} (<code>{oid}</code>)\n"
        text += f"\n<b>🛡️ Admins ({len(admins)}):</b>\n"
        keyboard = []
        if admins:
            for a in admins:
                tracked = lookup_user(str(a))
                name = tracked.get("name", str(a)) if tracked else str(a)
                text += f"  • {html.escape(name)} (<code>{a}</code>)\n"
                keyboard.append([InlineKeyboardButton(f"❌ {name} entfernen", callback_data=f"settings_rmadmin_{a}")])
        else:
            text += "  <i>Keine Admins konfiguriert.</i>\n"
        keyboard.append([InlineKeyboardButton("♻️ Adminliste zurücksetzen", callback_data="settings_reset_admins")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # === SPERREN MENU ===
    elif data == "menu_sperren":
        keyboard = [
            [InlineKeyboardButton("🤖 Bot Sperren", callback_data="sperr_bot_menu")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")],
        ]
        await query.edit_message_text(
            "🔒 <b>Sperren</b>\n\nWähle eine Sperr-Funktion:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # === BOT SPERREN MENU ===
    elif data == "sperr_bot_menu":
        bot_data = load_data()
        sb = bot_data.get("sperr_bots", {"enabled": False, "punishment": "ban", "delete": True, "groups": []})
        enabled = sb.get("enabled", False)
        punishment = sb.get("punishment", "ban")
        delete_msg = sb.get("delete", True)
        selected_groups = sb.get("groups", [])
        p_labels = {"warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = p_labels.get(punishment, punishment)
        status = f"Aktiv (Bestrafung: {p_label})" if enabled else "Inaktiv"
        del_label = "Ja ✅" if delete_msg else "Nein"
        grp_label = f"{len(selected_groups)} Gruppen" if selected_groups else "Alle Gruppen"
        keyboard = [
            [InlineKeyboardButton("❌ Aus" if not enabled else "✖️ Aus", callback_data="sperr_bot_off"),
             InlineKeyboardButton("✔️ Ein" if enabled else "☑️ Ein", callback_data="sperr_bot_on")],
            [InlineKeyboardButton(f"{'❌' if not delete_msg else '✅'} Auto-Delete", callback_data="sperr_bot_del")],
            [InlineKeyboardButton("❗ Warn", callback_data="sperr_bot_p_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="sperr_bot_p_kick")],
            [InlineKeyboardButton("🔇 Mute", callback_data="sperr_bot_p_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="sperr_bot_p_ban")],
            [InlineKeyboardButton(f"📋 Gruppen ({grp_label})", callback_data="sperr_bot_groups")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_sperren")],
        ]
        await query.edit_message_text(
            f"🤖 <b>Bots Sperren</b>\n"
            f"Wenn du diese Funktion aktivierst, können der Gruppe keine Bots von Nutzern hinzugefügt werden.\n"
            f"Darüberhinaus kannst du eine Bestrafung für Benutzer festlegen, die versuchen, dies zu tun.\n\n"
            f"<b>Status</b>: {status}\n"
            f"<b>Auto-Delete</b>: {del_label}\n"
            f"<b>Gruppen</b>: {grp_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "sperr_bot_on":
        bot_data = load_data()
        sb = bot_data.setdefault("sperr_bots", {"enabled": False, "punishment": "ban", "delete": True, "groups": []})
        sb["enabled"] = True
        save_data(bot_data)
        await render_sperr_bot_menu(query, context)
        return

    elif data == "sperr_bot_off":
        bot_data = load_data()
        sb = bot_data.setdefault("sperr_bots", {"enabled": False, "punishment": "ban", "delete": True, "groups": []})
        sb["enabled"] = False
        save_data(bot_data)
        await render_sperr_bot_menu(query, context)
        return

    elif data == "sperr_bot_del":
        bot_data = load_data()
        sb = bot_data.setdefault("sperr_bots", {"enabled": False, "punishment": "ban", "delete": True, "groups": []})
        sb["delete"] = not sb.get("delete", True)
        save_data(bot_data)
        await render_sperr_bot_menu(query, context)
        return

    elif data.startswith("sperr_bot_p_"):
        p = data.replace("sperr_bot_p_", "")
        bot_data = load_data()
        sb = bot_data.setdefault("sperr_bots", {"enabled": False, "punishment": "ban", "delete": True, "groups": []})
        sb["punishment"] = p
        save_data(bot_data)
        await render_sperr_bot_menu(query, context)
        return

    elif data == "sperr_bot_groups":
        await render_sperr_bot_groups(query, context)

    elif data == "sperr_bot_tga":
        bot_data = load_data()
        sb = bot_data.setdefault("sperr_bots", {"enabled": False, "punishment": "ban", "delete": True, "groups": []})
        current = [str(g) for g in sb.get("groups", [])]
        sb["groups"] = [] if current else [str(g["id"]) for g in await get_bot_groups(context)]
        save_data(bot_data)
        await query.answer("✅ Aktualisiert")
        await render_sperr_bot_groups(query, context)

    elif data.startswith("sperr_bot_tgg_"):
        gid_str = data.replace("sperr_bot_tgg_", "")
        bot_data = load_data()
        sb = bot_data.setdefault("sperr_bots", {"enabled": False, "punishment": "ban", "delete": True, "groups": []})
        groups_list = [str(g) for g in sb.get("groups", [])]
        if gid_str in groups_list:
            groups_list = [g for g in groups_list if g != gid_str]
        else:
            groups_list.append(gid_str)
        sb["groups"] = groups_list
        save_data(bot_data)
        await query.answer("✅ Aktualisiert")
        await render_sperr_bot_groups(query, context)

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
        lc = bot_data.get("antispam_links", {"punishment": "aus", "delete": True, "groups": []})
        punishment = lc.get("punishment", "aus")
        delete_msg = lc.get("delete", True)
        selected_groups = lc.get("groups", [])
        p_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = p_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        grp_label = f"{len(selected_groups)} Gruppen" if selected_groups else "Alle Gruppen"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="as_link_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="as_link_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="as_link_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="as_link_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="as_link_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="as_link_toggle_delete")],
            [InlineKeyboardButton(f"👥 Gruppen: {grp_label}", callback_data="as_link_groups_menu")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_antispam")],
        ]
        await query.edit_message_text(
            f"🔗 <b>Vollständige Linksperre</b>\n"
            f"Wähle die Bestrafung für das Senden eines Links jeglicher Art aus.\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}\n"
            f"<b>Gruppen:</b> {grp_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("as_link_set_"):
        val = data.replace("as_link_set_", "")
        bot_data = load_data()
        bot_data.setdefault("antispam_links", {})["punishment"] = val
        save_data(bot_data)
        await query.answer(f"Bestrafung auf {val} gesetzt ✅")
        lc = bot_data["antispam_links"]
        punishment = lc.get("punishment", "aus")
        delete_msg = lc.get("delete", True)
        selected_groups = lc.get("groups", [])
        p_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = p_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        grp_label = f"{len(selected_groups)} Gruppen" if selected_groups else "Alle Gruppen"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="as_link_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="as_link_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="as_link_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="as_link_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="as_link_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="as_link_toggle_delete")],
            [InlineKeyboardButton(f"👥 Gruppen: {grp_label}", callback_data="as_link_groups_menu")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_antispam")],
        ]
        await query.edit_message_text(
            f"🔗 <b>Vollständige Linksperre</b>\n"
            f"Wähle die Bestrafung für das Senden eines Links jeglicher Art aus.\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}\n"
            f"<b>Gruppen:</b> {grp_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data == "as_link_toggle_delete":
        bot_data = load_data()
        lc = bot_data.setdefault("antispam_links", {"punishment": "aus", "delete": True, "groups": []})
        lc["delete"] = not lc.get("delete", True)
        save_data(bot_data)
        await query.answer(f"Löschen: {'An' if lc['delete'] else 'Aus'} ✅")
        punishment = lc.get("punishment", "aus")
        delete_msg = lc.get("delete", True)
        selected_groups = lc.get("groups", [])
        p_labels = {"aus": "Aus", "warn": "Warn", "kick": "Kick", "mute": "Mute", "ban": "Ban"}
        p_label = p_labels.get(punishment, punishment)
        del_label = "Ja ✅" if delete_msg else "Nein"
        grp_label = f"{len(selected_groups)} Gruppen" if selected_groups else "Alle Gruppen"
        keyboard = [
            [InlineKeyboardButton("❌ Aus", callback_data="as_link_set_aus"),
             InlineKeyboardButton("❗ Warn", callback_data="as_link_set_warn"),
             InlineKeyboardButton("❗ Kick", callback_data="as_link_set_kick")],
            [InlineKeyboardButton("🤫 Mute", callback_data="as_link_set_mute"),
             InlineKeyboardButton("🚫 Ban", callback_data="as_link_set_ban")],
            [InlineKeyboardButton(f"🗑 Nachrichten Löschen {'✅' if delete_msg else '❌'}", callback_data="as_link_toggle_delete")],
            [InlineKeyboardButton(f"👥 Gruppen: {grp_label}", callback_data="as_link_groups_menu")],
            [InlineKeyboardButton("🔙 Zurück", callback_data="menu_antispam")],
        ]
        await query.edit_message_text(
            f"🔗 <b>Vollständige Linksperre</b>\n"
            f"Wähle die Bestrafung für das Senden eines Links jeglicher Art aus.\n\n"
            f"<b>Bestrafung:</b> {p_label}\n"
            f"<b>Löschen:</b> {del_label}\n"
            f"<b>Gruppen:</b> {grp_label}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    # --- Linksperre Gruppen-Auswahl ---
    elif data == "as_link_groups_menu":
        bot_data = load_data()
        lc = bot_data.get("antispam_links", {"punishment": "aus", "delete": True, "groups": []})
        selected_groups = set(lc.get("groups", []))
        groups = await get_bot_groups(context)
        keyboard = []
        for g in groups:
            check = "✅" if g["id"] in selected_groups else "❌"
            keyboard.append([InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"as_link_grp_{g['id']}")])
        keyboard.append([InlineKeyboardButton("✅ Alle auswählen", callback_data="as_link_grp_all")])
        keyboard.append([InlineKeyboardButton("❌ Alle abwählen", callback_data="as_link_grp_none")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="as_links_menu")])
        grp_info = f"{len(selected_groups)} ausgewählt" if selected_groups else "Alle (keine Einschränkung)"
        await query.edit_message_text(
            f"🔗 <b>Linksperre — Gruppen</b>\n\n"
            f"Wähle die Gruppen, in denen die Linksperre aktiv sein soll.\n"
            f"Wenn keine Gruppe ausgewählt ist, gilt sie für <b>alle</b> Gruppen.\n\n"
            f"<b>Aktuell:</b> {grp_info}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif data.startswith("as_link_grp_"):
        val = data.replace("as_link_grp_", "")
        bot_data = load_data()
        lc = bot_data.setdefault("antispam_links", {"punishment": "aus", "delete": True, "groups": []})
        selected_groups = set(lc.get("groups", []))
        if val == "all":
            groups = await get_bot_groups(context)
            selected_groups = {g["id"] for g in groups}
            await query.answer("Alle Gruppen ausgewählt ✅")
        elif val == "none":
            selected_groups = set()
            await query.answer("Alle abgewählt (gilt für alle) ✅")
        else:
            gid = int(val)
            if gid in selected_groups:
                selected_groups.discard(gid)
            else:
                selected_groups.add(gid)
            await query.answer("Aktualisiert ✅")
        lc["groups"] = list(selected_groups)
        save_data(bot_data)
        # Re-render groups menu
        groups = await get_bot_groups(context)
        keyboard = []
        for g in groups:
            check = "✅" if g["id"] in selected_groups else "❌"
            keyboard.append([InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"as_link_grp_{g['id']}")])
        keyboard.append([InlineKeyboardButton("✅ Alle auswählen", callback_data="as_link_grp_all")])
        keyboard.append([InlineKeyboardButton("❌ Alle abwählen", callback_data="as_link_grp_none")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="as_links_menu")])
        grp_info = f"{len(selected_groups)} ausgewählt" if selected_groups else "Alle (keine Einschränkung)"
        await query.edit_message_text(
            f"🔗 <b>Linksperre — Gruppen</b>\n\n"
            f"Wähle die Gruppen, in denen die Linksperre aktiv sein soll.\n"
            f"Wenn keine Gruppe ausgewählt ist, gilt sie für <b>alle</b> Gruppen.\n\n"
            f"<b>Aktuell:</b> {grp_info}",
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
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="menu_badwords")]]
        await query.edit_message_text(
            "➖ Sende jetzt das Wort, das entfernt werden soll.\n\n"
            "<i>Beispiel:\nFan</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        context.user_data["state"] = WAITING_BADWORD_REMOVE

    elif data.startswith("bw_del_"):
        await query.answer("Bitte nutze jetzt die Texteingabe zum Entfernen.", show_alert=True)

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
                        set_active_mute(chat_id_val, target_id, until_date.timestamp() if until_date else None)
                        until_str = until_date.strftime("%d.%m.%y um %H:%M")
                        action_label = f"• <b>Aktion:</b> Stummgeschaltet 🤫\n• <b>Bis:</b> {until_str}"
                except Exception as e:
                    action_label = f"• ⚠️ Fehler: {e}"
                result_text += f"\n{action_label}"
                warnings.pop(f"{chat_id_str}_{target_id_str}", None)
                save_data(bot_data)
                await query.edit_message_text(result_text, parse_mode="HTML")
                await log_action(context, "", group_id=int(chat_id_str), group_name=str(chat_id_str), category=LOG_CAT_MOD, action="WARN", details={"user": t_name, "user_id": str(target_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id), "details": f"Auto-{punishment}"})
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
                result_text = f"🚫 {t_name} [<code>{target_id}</code>] wurde gebannt."
            elif action == "kick":
                await context.bot.ban_chat_member(chat_id=chat_id_val, user_id=target_id)
                await context.bot.unban_chat_member(chat_id=chat_id_val, user_id=target_id)
                result_text = f"❗ {t_name} [<code>{target_id}</code>] wurde gekickt."
            elif action == "mute":
                await context.bot.restrict_chat_member(
                    chat_id=chat_id_val, user_id=target_id,
                    permissions=ChatPermissions.no_permissions(),
                )
                set_active_mute(chat_id_val, target_id)
                result_text = f"📛 {t_name} [<code>{target_id}</code>] wurde gemutet."
        except Exception as e:
            result_text = f"⚠️ Fehler: {e}"
        # Reset warns
        bot_data = load_data()
        warnings = bot_data.get("warnings", {})
        warnings.pop(f"{chat_id_val}_{target_id}", None)
        save_data(bot_data)
        await query.edit_message_text(result_text)
        await log_action(context, "", group_id=int(chat_id_val), group_name=str(chat_id_val), category=LOG_CAT_MOD, action=action.upper(), details={"user": t_name, "user_id": str(target_id), "von": query.from_user.full_name, "von_id": str(query.from_user.id)})

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
            keyboard = [
                [InlineKeyboardButton("➕ Gruppe/Kanal hinzufügen", callback_data="add_group_manual")],
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")],
            ]
            await query.edit_message_text("Keine Gruppen registriert.\n\nDu kannst eine Gruppe/Kanal per ID hinzufügen oder /registergroup in einer Gruppe nutzen.",
                                          reply_markup=InlineKeyboardMarkup(keyboard))
            return
        text = "👥 *Registrierte Gruppen:*\n\n"
        keyboard = []
        for g in groups:
            text += f"• {g['title']} (`{g['id']}`)\n"
            keyboard.append([InlineKeyboardButton(f"❌ {g['title']}", callback_data=f"remove_group_{g['id']}")])
        keyboard.append([InlineKeyboardButton("➕ Gruppe/Kanal hinzufügen", callback_data="add_group_manual")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("remove_group_"):
        if not is_owner(user_id):
            try:
                await query.answer("⛔ Nur für Owner.", show_alert=True)
            except Exception:
                pass
            return
        try:
            gid = int(data.replace("remove_group_", ""))
        except ValueError:
            logger.error(f"Invalid group ID in callback: {data}")
            return
        bot_data = load_data()
        groups = bot_data.get("groups", [])
        removed_name = None
        for g in groups:
            if g["id"] == gid:
                removed_name = g["title"]
                break
        if removed_name is None:
            # Try string comparison as fallback
            for g in groups:
                if str(g["id"]) == str(gid):
                    removed_name = g["title"]
                    gid = g["id"]
                    break
        bot_data["groups"] = [g for g in groups if g["id"] != gid]
        save_data(bot_data)
        sync_groups_to_file()
        logger.info(f"Group removed via menu: {removed_name} ({gid})")
        # Refresh group list
        groups = bot_data["groups"]
        if not groups:
            keyboard = [
                [InlineKeyboardButton("➕ Gruppe/Kanal hinzufügen", callback_data="add_group_manual")],
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")],
            ]
            await query.edit_message_text(
                f"✅ *{removed_name or gid}* entfernt.\n\nKeine Gruppen mehr registriert.",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        text = f"✅ *{removed_name or gid}* entfernt.\n\n👥 *Registrierte Gruppen:*\n\n"
        keyboard = []
        for g in groups:
            text += f"• {g['title']} (`{g['id']}`)\n"
            keyboard.append([InlineKeyboardButton(f"❌ {g['title']}", callback_data=f"remove_group_{g['id']}")])
        keyboard.append([InlineKeyboardButton("➕ Gruppe/Kanal hinzufügen", callback_data="add_group_manual")])
        keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_settings")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        await log_action(context, f"Gruppe entfernt (Menü): {removed_name} ({gid})")

    elif data == "add_group_manual":
        if not is_owner(user_id):
            await query.answer("⛔ Nur für Owner.", show_alert=True)
            return
        keyboard = [[InlineKeyboardButton("❌ Abbrechen", callback_data="show_groups")]]
        await query.edit_message_text(
            "➕ *Gruppe/Kanal hinzufügen*\n\n"
            "Sende mir die Chat-ID der Gruppe oder des Kanals.\n"
            "Beispiel: `-1001234567890`\n\n"
            "💡 Der Bot muss dort bereits Admin sein.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        context.user_data["state"] = WAITING_GROUP_ADD_ID

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
        uname = f"@{target_username} " if target_username else ""
        await update.message.reply_text(f"Ergebnis für {uname}{target_name} [<code>{target_id}</code>]:\n\n{result_text}", parse_mode="HTML")
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
                await log_action(context, f"Admin hinzugefügt: {new_admin} von {user_id}", category=LOG_CAT_ADMIN, action="Admin hinzugefügt", details={"user_id": str(new_admin), "von": str(user_id)})
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
                await log_action(context, f"Admin entfernt: {rem_admin} von {user_id}", category=LOG_CAT_ADMIN, action="Admin entfernt", details={"user_id": str(rem_admin), "von": str(user_id)})
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

    elif state == WAITING_PROTO_CHANNEL:
        proto_type = context.user_data.get("proto_add_type", "mod")
        try:
            channel_id = int(text.strip())
            try:
                chat = await context.bot.get_chat(channel_id)
                ch_name = chat.title or str(channel_id)
            except Exception:
                await update.message.reply_text("⚠️ Kanal nicht gefunden. Ist der Bot dort Admin?")
                context.user_data["state"] = None
                return

            bot_data = load_data()
            proto_channels = bot_data.setdefault("protokoll_channels", {})
            channel_cfg = {
                "name": ch_name,
                "type": proto_type,
            }
            if proto_type == "mod":
                channel_cfg["groups"] = ["all"]
            else:
                channel_cfg["groups"] = []
            proto_channels[str(channel_id)] = channel_cfg
            save_data(bot_data)
            context.user_data["state"] = None
            context.user_data.pop("proto_add_type", None)

            type_label = "Admin-Log ⚙️" if proto_type == "admin" else "Moderations-Log 🛡"
            keyboard = [
                [InlineKeyboardButton("🎯 Konfigurieren", callback_data=f"proto_cfg_{channel_id}")],
                [InlineKeyboardButton("🔙 Zum Protokoll-Menü", callback_data="menu_protokoll")],
            ]
            extra = "\nStandardmäßig werden <b>alle Gruppen</b> protokolliert." if proto_type == "mod" else ""
            await update.message.reply_text(
                f"✅ <b>{type_label}</b> — <code>{html.escape(ch_name)}</code> hinzugefügt!{extra}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
        except ValueError:
            await update.message.reply_text("⚠️ Bitte eine gültige numerische Kanal-ID senden (z.B. <code>-1001234567890</code>).", parse_mode="HTML")
        context.user_data["state"] = None

    elif state == WAITING_GROUP_ADD_ID:
        context.user_data["state"] = None
        try:
            chat_id = int(text.strip())
        except ValueError:
            await update.message.reply_text("⚠️ Bitte eine gültige numerische Chat-ID senden.\nBeispiel: `-1001234567890`", parse_mode="Markdown")
            return

        # Auto-correct: if positive and looks like a group ID, prepend -100
        if chat_id > 0:
            corrected = -int(f"100{chat_id}") if not str(chat_id).startswith("100") else -chat_id
            keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="show_groups")]]
            await update.message.reply_text(
                f"⚠️ Gruppen/Kanäle haben immer eine *negative* ID.\n\n"
                f"Meintest du vielleicht `{corrected}`?\n"
                f"Bitte erneut mit `-` davor eingeben.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # Check if already registered
        bot_data = load_data()
        groups = bot_data.get("groups", [])
        if any(g["id"] == chat_id for g in groups):
            keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="show_groups")]]
            await update.message.reply_text("✅ Diese Gruppe/Kanal ist bereits registriert.", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        # Try to get chat info
        try:
            chat_info = await context.bot.get_chat(chat_id)
            chat_title = chat_info.title or str(chat_id)
        except Exception:
            keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="show_groups")]]
            await update.message.reply_text(
                f"⚠️ Chat `{chat_id}` nicht gefunden.\n\nIst der Bot dort Admin?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        groups.append({"id": chat_id, "title": chat_title})
        bot_data["groups"] = groups
        save_data(bot_data)
        sync_groups_to_file()

        keyboard = [[InlineKeyboardButton("🔙 Zur Gruppenliste", callback_data="show_groups")]]
        await update.message.reply_text(
            f"✅ *{chat_title}* (`{chat_id}`) wurde hinzugefügt!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        await log_action(context, f"Gruppe manuell hinzugefügt: {chat_title} ({chat_id})")

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
        msg = update.message

        for gid in groups:
            try:
                sent = await msg.copy(chat_id=gid)
                sent_msgs.append((gid, sent.message_id))
                success += 1
            except Exception as e:
                fail += 1
                logger.error(f"Messenger send failed in {gid}: {e}")

        # Save broadcast persistently
        bot_data = load_data()
        preview_text = (msg.text_html or msg.text or "")
        bot_data.setdefault("broadcasts", {})[broadcast_id] = {
            "messages": sent_msgs,
            "date": now_de().strftime("%d.%m %H:%M"),
            "count": success,
            "preview": preview_text[:50] if preview_text else "...",
        }
        save_data(bot_data)

        keyboard = [[InlineKeyboardButton("🗑 Nachricht in allen Gruppen löschen", callback_data=f"del_broadcast_{broadcast_id}")]]
        await update.message.reply_text(
            f"📨 Nachricht gesendet!\n✅ {success} Gruppen erfolgreich"
            + (f"\n❌ {fail} Fehler" if fail else ""),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        text_preview = preview_text[:100]
        await log_action(context, f"MESSENGER: {update.effective_user.full_name} ({user_id}) → {success} Gruppen\nText: {text_preview}")
        context.user_data["state"] = None
        user_data_store.pop(user_id, None)

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

    elif state == WAITING_BADWORD_REMOVE:
        text_input = update.message.text.strip()
        words = [w.strip() for w in text_input.splitlines() if w.strip()]
        if not words:
            await update.message.reply_text("⚠️ Bitte sende mindestens ein Wort zum Entfernen.")
            return
        bot_data = load_data()
        word_list = bot_data.get("badwords", [])
        if not word_list:
            context.user_data["state"] = None
            await update.message.reply_text("⚠️ Keine verbotenen Worte vorhanden.")
            return
        remove_set = {w.lower() for w in words}
        removed = [w for w in word_list if w.lower() in remove_set]
        bot_data["badwords"] = [w for w in word_list if w.lower() not in remove_set]
        save_data(bot_data)
        context.user_data["state"] = None
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_badwords")]]
        if removed:
            await update.message.reply_text(
                f"✅ Entfernt: <code>{html.escape(', '.join(removed))}</code>\n"
                f"Verbleibend: {len(bot_data['badwords'])} verbotene Worte",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "⚠️ Keines der gesendeten Worte wurde gefunden.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    elif state == "ar_set_group":
        try:
            group_id = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Bitte sende eine gültige numerische Chat-ID.")
            return
        bot_data = load_data()
        ar = bot_data.setdefault("admin_report", {"active": False, "staff_group": None, "notify_users": []})
        ar["staff_group"] = group_id
        save_data(bot_data)
        context.user_data["state"] = None
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="menu_admin_report")]]
        await update.message.reply_text(
            f"✅ Mitarbeitergruppe gesetzt: <code>{group_id}</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif state == "ar_target_new_input":
        try:
            dst_id = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Bitte sende eine gültige numerische Chat-ID (z.B. -1001234567890).")
            return
        dst_name = str(dst_id)
        try:
            chat_info = await context.bot.get_chat(dst_id)
            dst_name = chat_info.title or str(dst_id)
        except Exception:
            pass
        context.user_data["state"] = None
        keyboard = [[InlineKeyboardButton(f"✏️ Gruppen zuweisen", callback_data=f"ar_target_{dst_id}")],
                     [InlineKeyboardButton("🔙 Zurück", callback_data="ar_routes_menu")]]
        await update.message.reply_text(
            f"✅ Ziel erstellt: <code>{dst_id}</code> ({html.escape(dst_name)})\n\n"
            f"Klicke jetzt auf 'Gruppen zuweisen' um Gruppen zuzuordnen.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif state and state.startswith("ar_target_change_"):
        old_dst = int(state.replace("ar_target_change_", ""))
        try:
            new_dst = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Bitte sende eine gültige numerische Chat-ID.")
            return
        bot_data = load_data()
        ar = bot_data.setdefault("admin_report", {})
        routes = ar.setdefault("group_routes", {})
        also = ar.setdefault("route_also_default", {})
        # Move all routes from old target to new target
        for src_id in list(routes.keys()):
            if routes[src_id] == old_dst:
                routes[src_id] = new_dst
        save_data(bot_data)
        context.user_data["state"] = None
        new_name = str(new_dst)
        try:
            chat_info = await context.bot.get_chat(new_dst)
            new_name = chat_info.title or str(new_dst)
        except Exception:
            pass
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data=f"ar_target_{new_dst}")]]
        await update.message.reply_text(
            f"✅ Ziel geändert: <code>{new_dst}</code> ({html.escape(new_name)})",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    elif state == "ar_notify_add":
        try:
            uid = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Bitte sende eine gültige numerische User-ID.")
            return
        bot_data = load_data()
        ar = bot_data.setdefault("admin_report", {"active": False, "staff_group": None, "notify_users": []})
        if uid not in ar.get("notify_users", []):
            ar.setdefault("notify_users", []).append(uid)
            save_data(bot_data)
        context.user_data["state"] = None
        users_db = load_users()
        name = users_db.get(str(uid), {}).get("name", str(uid))
        keyboard = [[InlineKeyboardButton("🔙 Zurück", callback_data="ar_notify_menu")]]
        await update.message.reply_text(
            f"✅ {name} wird jetzt benachrichtigt.",
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
    sync_groups_to_file()
    await update.message.reply_text(f"✅ Gruppe registriert: *{chat.title}*", parse_mode="Markdown")
    await log_action(context, f"Gruppe registriert: {chat.title} ({chat.id})")

# --- /unregistergroup ---

async def unregister_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Dieser Befehl funktioniert nur in Gruppen.")
        return

    data = load_data()
    groups = data.get("groups", [])
    data["groups"] = [g for g in groups if g["id"] != chat.id]
    save_data(data)
    sync_groups_to_file()
    await update.message.reply_text(f"✅ Gruppe entfernt: *{chat.title}*", parse_mode="Markdown")

# --- Helper: resolve target user from reply or argument ---

async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve target user from reply, mention entity, tracked username, or numeric ID."""
    global BOT_USERNAME_CACHE

    # Option 1: Reply to a message
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        user = update.message.reply_to_message.from_user
        track_user(user)
        return user.id, user.full_name

    # Ensure bot username is cached (fallback if post_init didn't cache it)
    if BOT_USERNAME_CACHE is None:
        try:
            me = await context.bot.get_me()
            BOT_USERNAME_CACHE = me.username
        except Exception as e:
            logger.error(f"get_me() failed in resolve_target: {e}")
            BOT_USERNAME_CACHE = ""  # Set empty string to prevent repeated failures

    # Option 2: Check for mention entities (text_mention has user object)
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "text_mention" and entity.user:
                track_user(entity.user)
                return entity.user.id, entity.user.full_name or str(entity.user.id)
            if entity.type == "mention":
                username = update.message.text[entity.offset + 1:entity.offset + entity.length]
                if BOT_USERNAME_CACHE and username.lower() == BOT_USERNAME_CACHE.lower():
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
    try:
        await auto_delete_command(update, context)
        user_id = update.effective_user.id
        if not await is_group_authorized(context, user_id, update.effective_chat):
            return
    except Exception as e:
        logger.error(f"info_command init error: {e}")
        return

    try:
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
    except Exception as e:
        logger.error(f"info_command error for user {update.effective_user.id}: {e}", exc_info=True)
        try:
            await update.message.reply_text("⚠️ Fehler beim Abrufen der Info. Bitte erneut versuchen.")
        except Exception:
            pass

# --- /mute ---

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mute a user in the group. Usage: /mute [reason] (reply to a message)."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    if should_skip_recent_action(context, f"mute:{chat.id}:{target_id}"):
        return

    # Admin-Schutz
    if await is_chat_admin(context, chat.id, target_id):
        await update.message.reply_text("⛔ Dieser User ist ein Administrator — Mute ist nicht möglich.")
        return

    # Prüfen ob User bereits gemutet ist
    if await is_user_currently_muted(context, chat.id, target_id):
        if not await wait_for_mute_state(context, chat.id, target_id, False, attempts=3, delay=0.5):
            await update.message.reply_text("ℹ️ Dieser User ist bereits stummgeschaltet.")
            return

    args = list(context.args) if context.args else []
    # Strip the first arg if it was used to resolve the target (@username or numeric ID)
    if args and not update.message.reply_to_message:
        args = args[1:]  # first arg was the target
    elif args and update.message.reply_to_message and (args[0].startswith("@") or args[0].isdigit()):
        args = args[1:]
    # Parse duration (e.g. "2h", "30m")
    args, duration_sec, duration_label = parse_duration(args)
    reason = " ".join(args) if args else None
    until_date = None
    if duration_sec:
        until_date = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=duration_sec)

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target_id,
            permissions=ChatPermissions.no_permissions(),
            until_date=until_date,
        )
        set_active_mute(chat.id, target_id, until_date.timestamp() if until_date else None)

        # Look up username
        tracked = lookup_user(str(target_id))
        target_username = tracked.get("username") if tracked else None
        uname = f"@{target_username} " if target_username else ""

        duration_text = f"\n⏱ <b>Dauer:</b> {duration_label}" if duration_label else ""
        reason_text = f"\n📝 <b>Grund:</b> {html.escape(reason)}" if reason else ""

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🕹 Rechte", url=f"tg://resolve?domain={chat.username}&admin={target_id}" if chat.username else f"tg://chat_permissions?chat_id={str(chat.id).replace('-100', '')}"),
                InlineKeyboardButton("✅ Unmute", callback_data=f"cmd_unmute_{chat.id}_{target_id}"),
            ]
        ])

        await update.message.reply_text(
            f"{uname}[<code>{target_id}</code>] wurde 🔇 stummgeschaltet.{duration_text}{reason_text}",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await log_action(context, "", group_id=chat.id, group_name=chat.title, category=LOG_CAT_MOD, action="MUTE", details={"user": target_name, "user_id": str(target_id), "gruppe": chat.title, "von": update.effective_user.full_name, "von_id": str(update.effective_user.id), "dauer": duration_label or "Unbegrenzt", "grund": reason})
    except Exception as e:
        await update.message.reply_text(f"❌ Mute fehlgeschlagen: {e}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unmute a user in the group. Usage: /unmute (reply to a message)."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    if should_skip_recent_action(context, f"unmute:{chat.id}:{target_id}"):
        return

    # Prüfen ob User tatsächlich gemutet ist
    if not await is_user_currently_muted(context, chat.id, target_id):
        await update.message.reply_text("ℹ️ Dieser User ist nicht stummgeschaltet.")
        return

    try:
        chat_obj = await context.bot.get_chat(chat.id)
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target_id,
            permissions=UNMUTE_PERMISSIONS,
        )
        clear_active_mute(chat.id, target_id)

        if not await wait_for_mute_state(context, chat.id, target_id, False):
            await update.message.reply_text("⚠️ Unmute wurde gesendet, aber Telegram zeigt den User noch kurz als gemutet. Bitte direkt nochmal prüfen.")
            return

        tracked = lookup_user(str(target_id))
        target_username = tracked.get("username") if tracked else None
        uname = f"@{target_username} " if target_username else ""
        display_name = target_name or (f"@{target_username}" if target_username else str(target_id))

        await update.message.reply_text(
            f"✅ {uname}[<code>{target_id}</code>] wurde entmutet.",
            parse_mode="HTML",
        )
        await log_action(context, "", group_id=chat.id, group_name=chat.title, category=LOG_CAT_MOD, action="UNMUTE", details={"user": display_name, "user_id": str(target_id), "gruppe": chat.title, "von": update.effective_user.full_name, "von_id": str(update.effective_user.id)})
    except Exception as e:
        await update.message.reply_text(f"❌ Unmute fehlgeschlagen: {e}")

# --- /kick ---

async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kick a user from the group (they can rejoin). Usage: /kick [reason] (reply to a message)."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    if should_skip_recent_action(context, f"kick:{chat.id}:{target_id}"):
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
        await asyncio.sleep(1.0)  # Wait for ban to propagate before unban
        await context.bot.unban_chat_member(chat_id=chat.id, user_id=target_id, only_if_banned=True)

        reason_text = f"\n📝 <b>Grund:</b> {html.escape(reason)}" if reason else ""
        await update.message.reply_text(
            f"👢 <b>{html.escape(target_name)}</b> wurde gekickt!{reason_text}\n\n"
            f"ℹ️ Der User kann der Gruppe wieder beitreten.",
            parse_mode="HTML",
        )
        await log_action(context, "", group_id=chat.id, group_name=chat.title, category=LOG_CAT_MOD, action="KICK", details={"user": target_name, "user_id": str(target_id), "gruppe": chat.title, "von": update.effective_user.full_name, "von_id": str(update.effective_user.id), "grund": reason})
    except Exception as e:
        await update.message.reply_text(f"❌ Kick fehlgeschlagen: {e}")

# --- /ban (single group) ---

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user from the current group. Usage: /ban [reason] (reply to a message)."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    if should_skip_recent_action(context, f"ban:{chat.id}:{target_id}"):
        return

    # Admin-Schutz
    if await is_chat_admin(context, chat.id, target_id):
        await update.message.reply_text("⛔ Dieser User ist ein Administrator — Ban ist nicht möglich.")
        return

    # Prüfen ob User bereits gebannt ist
    if await is_user_currently_banned(context, chat.id, target_id):
        await update.message.reply_text("ℹ️ Dieser User ist bereits gebannt.")
        return

    args = list(context.args) if context.args else []
    if args and not update.message.reply_to_message:
        args = args[1:]
    elif args and update.message.reply_to_message and (args[0].startswith("@") or args[0].isdigit()):
        args = args[1:]
    # Parse duration (e.g. "2h", "30m")
    args, duration_sec, duration_label = parse_duration(args)
    reason = " ".join(args) if args else None
    until_date = None
    if duration_sec:
        until_date = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=duration_sec)

    tracked = lookup_user(str(target_id))
    target_username = tracked.get("username") if tracked else None

    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_id, revoke_messages=True, until_date=until_date)
        remember_group_ban([chat.id], target_id, target_name, target_username)

        uname = f"@{target_username} " if target_username else ""
        duration_text = f"\n⏱ <b>Dauer:</b> {duration_label}" if duration_label else ""
        reason_text = f"\n📝 <b>Grund:</b> {html.escape(reason)}" if reason else ""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Entsperren", callback_data=f"cmd_unban_{chat.id}_{target_id}")]
        ])
        await update.message.reply_text(
            f"{uname}[<code>{target_id}</code>] verbannt.{duration_text}{reason_text}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await log_action(context, "", group_id=chat.id, group_name=chat.title, category=LOG_CAT_MOD, action="BAN", details={"user": target_name, "user_id": str(target_id), "gruppe": chat.title, "von": update.effective_user.full_name, "von_id": str(update.effective_user.id), "dauer": duration_label, "grund": reason})
    except Exception as e:
        await update.message.reply_text(f"❌ Ban fehlgeschlagen: {e}")

# --- /unban (single group) ---

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unban a user from the current group. Usage: /unban (reply to a message or /unban @username or /unban user_id)."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Dieser Befehl funktioniert nur in Gruppen.")
        return

    target_id, target_name = await resolve_target(update, context)
    if target_id is None:
        return

    if should_skip_recent_action(context, f"unban:{chat.id}:{target_id}"):
        return

    # Prüfen ob User tatsächlich gebannt ist
    if not await is_user_currently_banned(context, chat.id, target_id):
        await update.message.reply_text("ℹ️ Dieser User ist nicht gebannt.")
        return

        tracked = lookup_user(str(target_id))
        target_username = tracked.get("username") if tracked else None
        display_name = target_name or (f"@{target_username}" if target_username else str(target_id))

        try:
            await context.bot.unban_chat_member(chat_id=chat.id, user_id=target_id, only_if_banned=True)
            forget_group_ban([chat.id], target_id)

            uname = f"@{target_username} " if target_username else ""
            await update.message.reply_text(
                f"✅ {uname}[<code>{target_id}</code>] wurde entsperrt.",
                parse_mode="HTML",
            )
            await log_action(context, "", group_id=chat.id, group_name=chat.title, category=LOG_CAT_MOD, action="UNBAN", details={"user": display_name, "user_id": str(target_id), "gruppe": chat.title, "von": update.effective_user.full_name, "von_id": str(update.effective_user.id)})
    except Exception as e:
        await update.message.reply_text(f"❌ Unban fehlgeschlagen: {e}")

# --- /banall ---

# --- /warn ---

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Warn a user. Usage: /warn [reason] (reply to a message)."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
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
                    set_active_mute(chat.id, target_id, until_date.timestamp() if until_date else None)
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
            await log_action(context, "", group_id=chat.id, group_name=chat.title, category=LOG_CAT_MOD, action="WARN", details={"user": target_name, "user_id": str(target_id), "gruppe": chat.title, "von": update.effective_user.full_name, "von_id": str(update.effective_user.id), "details": f"{current_count}/{max_warns} → Auto-{punishment}", "grund": reason})
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
    await log_action(context, "", group_id=chat.id, group_name=chat.title, category=LOG_CAT_MOD, action="WARN", details={"user": target_name, "user_id": str(target_id), "gruppe": chat.title, "von": update.effective_user.full_name, "von_id": str(update.effective_user.id), "details": f"{current_count}/{max_warns}", "grund": reason})


async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a warn from a user. Usage: /unwarn (reply to a message)."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
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
    await log_action(context, "", group_id=chat.id, group_name=chat.title, category=LOG_CAT_MOD, action="UNWARN", details={"user": target_name, "user_id": str(target_id), "gruppe": chat.title, "von": update.effective_user.full_name, "von_id": str(update.effective_user.id), "details": f"{new_count}/{max_w}"})


# --- /free & /unfree ---

async def free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant a user the 'Befreiter' role — exempt from link filter, forward filter, forbidden words."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
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
    await log_action(context, "", group_id=chat.id if chat else None, group_name=chat.title if chat else None, category=LOG_CAT_MOD, action="FREE", details={"user": target_name, "user_id": str(target_id), "von": update.effective_user.full_name, "von_id": str(update.effective_user.id)})


async def unfree_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revoke the 'Befreiter' role from a user."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
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
    await log_action(context, "", group_id=chat.id if chat else None, group_name=chat.title if chat else None, category=LOG_CAT_MOD, action="UNFREE", details={"user": target_name, "user_id": str(target_id), "von": update.effective_user.full_name, "von_id": str(update.effective_user.id)})


async def multidel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete all messages from the replied-to message up to the /multidel command message."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Dieser Befehl funktioniert nur in Gruppen.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Antworte auf eine Nachricht, um alle Nachrichten ab dort bis hierher zu löschen.")
        return

    start_msg_id = update.message.reply_to_message.message_id
    end_msg_id = update.message.message_id
    chat_id = update.effective_chat.id

    deleted = 0
    failed = 0
    # Delete in batches (Telegram allows deleting messages by ID)
    msg_ids = list(range(start_msg_id, end_msg_id + 1))

    # Telegram delete_messages supports max 100 per call
    for i in range(0, len(msg_ids), 100):
        batch = msg_ids[i:i + 100]
        try:
            result = await context.bot.delete_messages(chat_id=chat_id, message_ids=batch)
            if result:
                deleted += len(batch)
            else:
                failed += len(batch)
        except Exception:
            failed += len(batch)

    confirm = await update.effective_chat.send_message(
        f"🗑 <b>Multidel abgeschlossen</b>\n"
        f"✅ {deleted} Nachrichten gelöscht\n"
        f"❌ {failed} fehlgeschlagen",
        parse_mode="HTML"
    )
    # Auto-delete confirmation after 5 seconds
    await asyncio.sleep(5)
    try:
        await confirm.delete()
    except Exception:
        pass


async def banall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await auto_delete_command(update, context)
        if not is_authorized(update.effective_user.id):
            return

        target_id, target_name = await resolve_target(update, context)
        if target_id is None:
            return

        chat = update.effective_chat
        chat_id = chat.id
        thread_id = getattr(update.effective_message, "message_thread_id", None)
        send_kwargs = {"chat_id": chat_id}
        if thread_id is not None:
            send_kwargs["message_thread_id"] = thread_id

        groups = await get_bot_groups(context)
        if not groups:
            try:
                await context.bot.send_message(text="Keine Gruppen registriert.", **send_kwargs)
            except Exception:
                pass
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
            lines.append(f"✅ Erfolgreich gebannt in <b>{success_count}/{len(groups)}</b> Gruppen")
        else:
            lines.append(f"⚠️ {target_id} konnte in keiner Gruppe gebannt werden.")

        if fail_count:
            lines.append(f"❌ {fail_count} Gruppen fehlgeschlagen.")

        try:
            await context.bot.send_message(text="\n".join(lines), parse_mode="HTML", **send_kwargs)
        except Exception as e:
            logger.error(f"banall send_message error: {e}")

        source_group_id = chat.id if chat and chat.type in ("group", "supergroup") else None
        source_group_name = chat.title if source_group_id else None
        await log_action(
            context,
            f"BANALL: {target_name} ({target_id})",
            group_id=source_group_id,
            group_name=source_group_name,
            category=LOG_CAT_MOD,
            action="BANALL",
            details={"user": target_name, "user_id": str(target_id), "von": update.effective_user.full_name, "ergebnis": f"{success_count} OK, {fail_count} Fehler"},
        )
        for group in successful_groups:
            await log_action(
                context,
                f"BANALL: {target_name} ({target_id}) in {group['title']}",
                group_id=group["id"],
                group_name=group["title"],
                category=LOG_CAT_MOD,
                action="BANALL",
                details={"user": target_name, "user_id": str(target_id), "gruppe": group["title"], "von": update.effective_user.full_name},
            )
    except Exception as e:
        logger.error(f"banall error: {e}")

# --- /unbanall ---

async def unbanall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await auto_delete_command(update, context)
        if not is_authorized(update.effective_user.id):
            return

        target_id, target_name = await resolve_target(update, context)
        if target_id is None:
            return

        chat = update.effective_chat
        chat_id = chat.id
        thread_id = getattr(update.effective_message, "message_thread_id", None)
        send_kwargs = {"chat_id": chat_id}
        if thread_id is not None:
            send_kwargs["message_thread_id"] = thread_id

        groups = await get_bot_groups(context)
        if not groups:
            try:
                await context.bot.send_message(text="Keine Gruppen registriert.", **send_kwargs)
            except Exception:
                pass
            return

        successful_groups = []
        failed_groups = []
        for g in groups:
            try:
                await context.bot.unban_chat_member(chat_id=g["id"], user_id=target_id, only_if_banned=True)
                successful_groups.append(g)
            except Exception:
                failed_groups.append(g)

        forget_group_ban([g["id"] for g in groups], target_id)

        lines = []
        if successful_groups:
            lines.append(f"✅ Erfolgreich entbannt in <b>{len(successful_groups)}/{len(groups)}</b> Gruppen")
        else:
            lines.append(f"⚠️ {target_id} konnte in keiner Gruppe entbannt werden.")
        if failed_groups:
            lines.append(f"❌ {len(failed_groups)} Gruppen fehlgeschlagen.")

        try:
            await context.bot.send_message(text="\n".join(lines), parse_mode="HTML", **send_kwargs)
        except Exception as e:
            logger.error(f"unbanall send_message error: {e}")

        source_group_id = chat.id if chat and chat.type in ("group", "supergroup") else None
        source_group_name = chat.title if source_group_id else None
        await log_action(
            context,
            f"UNBANALL: {target_name} ({target_id})",
            group_id=source_group_id,
            group_name=source_group_name,
            category=LOG_CAT_MOD,
            action="UNBANALL",
            details={"user": target_name, "user_id": str(target_id), "von": update.effective_user.full_name, "ergebnis": f"{len(successful_groups)} OK, {len(failed_groups)} Fehler"},
        )
        for group in successful_groups:
            await log_action(
                context,
                f"UNBANALL: {target_name} ({target_id}) in {group['title']}",
                group_id=group["id"],
                group_name=group["title"],
                category=LOG_CAT_MOD,
                action="UNBANALL",
                details={"user": target_name, "user_id": str(target_id), "gruppe": group["title"], "von": update.effective_user.full_name},
            )
    except Exception as e:
        logger.error(f"unbanall error: {e}")

# --- /personal and /unpersonal commands (in groups) ---

async def personal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save a replied-to message as a personal command. Usage: /personal <name> (reply to a message)."""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Nutzung: /personal <Name>\n"
            "Antwort auf eine Nachricht, die als Befehl gespeichert werden soll.\n\n"
            "Beispiel: Antworte auf eine Nachricht und schreibe /personal hele",
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
        "groups": [],
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

    # Store pending data and show group selection
    user_data_store[user_id] = {
        "action": "personal_grp_select",
        "cmd_name": cmd_name,
        "cmd_data": cmd_data,
        "selected": set(),
    }

    bot_data = load_data()
    groups = bot_data.get("groups", [])
    keyboard = []
    row = []
    for g in groups:
        row.append(InlineKeyboardButton(f"⬜ {g['title']}", callback_data=f"pers_grp_{g['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data="pers_grp_all"),
        InlineKeyboardButton("◻️ Keine", callback_data="pers_grp_none"),
    ])
    keyboard.append([InlineKeyboardButton("✅ Speichern", callback_data="pers_grp_save")])
    keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data="pers_grp_cancel")])

    await update.message.reply_text(
        f"🏗 <b>/{html.escape(cmd_name)}</b> — Wähle Gruppen:\n\n"
        f"<i>Keine Auswahl = gilt für alle Gruppen</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def unpersonal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a personal command. Usage: /unpersonal <name>"""
    await auto_delete_command(update, context)
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Nutzung: /unpersonal <Name>\nBeispiel: /unpersonal hele")
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
    await auto_delete_command(update, context)
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

    # Befehle: erst gruppenspezifisch, dann Fallback auf global (leere groups)
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

# --- @admin / /report command ---

async def handle_admin_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/report command – members can alert staff. Ignored when used by admins."""
    await auto_delete_command(update, context)
    if not update.message or not update.effective_chat:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    sender = update.message.from_user
    if not sender:
        return

    # Admins/Mods dürfen den Befehl NICHT nutzen
    if is_authorized(sender.id) or await is_chat_admin(context, chat.id, sender.id):
        return

    bot_data = load_data()
    ar = bot_data.get("admin_report", {})
    if not ar.get("active"):
        return

    # Collect target groups based on per-group routing config
    group_routes = ar.get("group_routes", {})
    route_also_default = ar.get("route_also_default", {})
    default_team = ar.get("staff_group")
    specific_route = group_routes.get(str(chat.id))

    target_groups = set()
    if specific_route:
        target_groups.add(int(specific_route))
        # Check if this group also sends to Standard-Team
        also_default = route_also_default.get(str(chat.id), True)
        if also_default and default_team:
            target_groups.add(int(default_team))
    elif default_team:
        # No specific route → send to Standard-Team
        target_groups.add(int(default_team))

    if not target_groups:
        return

    # Build report message
    thread_id = getattr(update.effective_message, "message_thread_id", None)
    reply_to = update.message.reply_to_message
    reported_info = ""
    message_link = None
    if reply_to and reply_to.from_user:
        ru = reply_to.from_user
        reported_info = (
            f"\n\n📌 <b>Gemeldete Nachricht von:</b>\n"
            f"  👤 {ru.full_name} (<code>{ru.id}</code>)\n"
            f"  💬 <i>{(reply_to.text or '[Medien]')[:200]}</i>"
        )
        # Build deep link to the reported message
        chat_id_str = str(chat.id)
        if chat_id_str.startswith("-100"):
            chat_link_id = chat_id_str[4:]  # Remove -100 prefix
            message_link = f"https://t.me/c/{chat_link_id}/{reply_to.message_id}"
    elif update.message:
        # Link to the report message itself
        chat_id_str = str(chat.id)
        if chat_id_str.startswith("-100"):
            chat_link_id = chat_id_str[4:]
            message_link = f"https://t.me/c/{chat_link_id}/{update.message.message_id}"

    # Extract user text after @admin
    user_text = ""
    if update.message.text:
        import re as _re
        # Remove all @admin mentions and strip
        cleaned = _re.sub(r'@admin', '', update.message.text, flags=_re.IGNORECASE).strip()
        if cleaned:
            user_text = f"\n\n💬 <b>Nachricht:</b> <i>{html.escape(cleaned)[:500]}</i>"

    report_text = (
        f"🆘 <b>Admin-Meldung</b>\n\n"
        f"📍 <b>Gruppe:</b> {chat.title}\n"
        f"👤 <b>Gemeldet von:</b> {sender.full_name} (<code>{sender.id}</code>)\n"
        f"🕐 {now_de().strftime('%d.%m.%Y %H:%M')}"
        f"{user_text}"
        f"{reported_info}"
    )

    # Collect all users to mention: configured notify_users + all group admins
    mention_uids = set()
    notify_users = ar.get("notify_users", [])
    for uid in notify_users:
        mention_uids.add(uid)

    # Fetch all admins of the source group and mention them
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        for admin in admins:
            if not admin.user.is_bot:
                mention_uids.add(admin.user.id)
    except Exception as e:
        logger.warning(f"Could not fetch group admins for mention: {e}")

    if mention_uids:
        # Use zero-width space mentions so admins get notified without visible names
        mentions = []
        for uid in mention_uids:
            mentions.append(f'<a href="tg://user?id={uid}">\u200b</a>')
        report_text += "\n" + "".join(mentions)

    # Build inline keyboard with "Go to message" and "Solved" buttons
    buttons = []
    if message_link:
        buttons.append(InlineKeyboardButton("📍 Zur Nachricht", url=message_link))
    reply_markup = InlineKeyboardMarkup([
        buttons,
        [InlineKeyboardButton("✅ Gelöst", callback_data=f"ar_solved_{chat.id}_{sender.id}")],
    ]) if buttons else InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Gelöst", callback_data=f"ar_solved_{chat.id}_{sender.id}")],
    ])

    sent_ok = False
    for tg_id in target_groups:
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=report_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            logger.info(f"Admin report from {sender.id} in {chat.id} sent to {tg_id}")
            sent_ok = True
        except Exception as e:
            logger.error(f"Failed to send admin report to {tg_id}: {e}")

    if sent_ok:
        try:
            confirm_msg = await update.message.reply_text("✅ Admin wurde informiert.")
            asyncio.get_event_loop().call_later(
                10,
                lambda mid=confirm_msg.message_id, cid=chat.id: asyncio.ensure_future(
                    context.bot.delete_message(chat_id=cid, message_id=mid)
                ),
            )
        except Exception:
            pass

    # Log to moderation protocol
    await log_action(context, None, category="mod", action="REPORT",
                     details={"👤 Gemeldet von": f"{sender.full_name} ({sender.id})",
                              "📍 Gruppe": chat.title},
                     group_id=chat.id, group_name=chat.title)


async def _check_admin_mention(update: Update, context):
    """Check if message text contains @admin and trigger report."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.lower()
    if "@admin" not in text:
        return
    # Delegate to the report handler
    await handle_admin_report(update, context)


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
    await auto_delete_command(update, context)

    # --- @admin mention check ---
    await _check_admin_mention(update, context)

    # --- Check if group is exempt from all filters ---
    if update.effective_chat and update.effective_chat.id:
        _exempt_data = load_data()
        _exempt_groups = _exempt_data.get("exempt_groups", [])
        if update.effective_chat.id in _exempt_groups:
            # This group is exempt from all filters (links, forwards, forbidden words)
            return

    # --- Anti-Spam: Link check ---
    if update.message.from_user:
        sender_as = update.message.from_user
        # Check all entities for links (filter out false positives like "." or short non-URLs)
        all_entities = list(update.message.entities or []) + list(update.message.caption_entities or [])
        text_full = (update.message.text or "") + (update.message.caption or "")
        real_links = []
        for ent in all_entities:
            if ent.type in ("url", "text_link"):
                if ent.type == "text_link":
                    real_links.append(ent)
                else:
                    # Extract the actual text Telegram marked as URL
                    url_text = text_full[ent.offset:ent.offset + ent.length].strip()
                    # Only count it if it has at least one dot with text on both sides (like "x.y")
                    parts = url_text.split(".")
                    if len(parts) >= 2 and all(len(p.strip()) > 0 for p in parts[:2]):
                        real_links.append(ent)
                    else:
                        logger.info(f"Ignoring false-positive URL entity: '{url_text}'")
        has_link = len(real_links) > 0
        if has_link:
            logger.info(f"LINK detected from {sender_as.id} in {update.effective_chat.id}")
            is_adm_as = is_authorized(sender_as.id) or await is_chat_admin(context, update.effective_chat.id, sender_as.id)
            if not is_adm_as and not is_freed(sender_as.id):
                bot_data_as = load_data()
                lc = bot_data_as.get("antispam_links", {"punishment": "aus", "delete": True, "groups": []})
                lc_groups = lc.get("groups", [])
                # If groups are specified, only enforce in those groups
                if lc_groups and update.effective_chat.id not in lc_groups:
                    logger.info(f"Link detected but group {update.effective_chat.id} not in linksperre groups, skipping")
                else:
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
                                    await log_action(context, "", group_id=chat_id_as, group_name=update.effective_chat.title, category=LOG_CAT_MOD, action="LINK", details={"user": user_name_as, "user_id": str(user_id_as), "gruppe": update.effective_chat.title, "details": f"Auto-{warn_punishment} ({max_w}/{max_w})"})
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
                                set_active_mute(chat_id_as, user_id_as)
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
                                remember_group_ban([chat_id_as], user_id_as, user_name_as, update.message.from_user.username)
                                await context.bot.send_message(
                                    chat_id=chat_id_as,
                                    text=f"{uname_as}[<code>{user_id_as}</code>] hat ohne Genehmigung einen 🔗 Link gesendet.\n<b>Aktion:</b> Gebannt 🚫",
                                    parse_mode="HTML",
                                )
                        except Exception as e:
                            logger.error(f"Link punishment failed: {e}")
                        await log_action(context, "", group_id=chat_id_as, group_name=update.effective_chat.title, category=LOG_CAT_MOD, action="LINK", details={"user": user_name_as, "user_id": str(user_id_as), "gruppe": update.effective_chat.title, "details": f"Strafe: {lc_punishment}"})
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
                    await log_action(context, "", group_id=update.effective_chat.id, group_name=update.effective_chat.title, category=LOG_CAT_MOD, action="FORWARD-SPAM", details={"user": update.message.from_user.full_name, "user_id": str(update.message.from_user.id), "gruppe": update.effective_chat.title, "details": f"Typ: {origin_type}"})
                    return

    # --- Forbidden words check ---
    msg_text = update.message.text or update.message.caption or ""
    if msg_text and update.message.from_user:
        sender = update.message.from_user
        # Admins (Owner, Bot-Admins, Gruppen-Admins) sind von ALLEN Filtern ausgenommen
        sender_is_admin = is_authorized(sender.id) or is_freed(sender.id)
        if not sender_is_admin and update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            sender_is_admin = await is_chat_admin(context, update.effective_chat.id, sender.id)
        if sender_is_admin:
            logger.info(f"Skipping forbidden words for admin/freed user {sender.id} ({sender.full_name})")
        if not sender_is_admin:
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
                                await log_action(context, "", group_id=chat_id, group_name=update.effective_chat.title, category=LOG_CAT_MOD, action="BADWORD", details={"user": user_name, "user_id": str(user_id_bw), "gruppe": update.effective_chat.title, "details": f"Wort: {matched} → Auto-{warn_punishment} ({max_w}/{max_w})"})
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
                            remember_group_ban([chat_id], user_id_bw, user_name, sender.username)
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=f"🚫 {html.escape(user_name)} wurde gebannt — Verbotenes Wort: <code>{html.escape(matched)}</code>",
                                parse_mode="HTML",
                            )
                    except Exception as e:
                        logger.error(f"Badword punishment failed: {e}")

                    await log_action(context, "", group_id=chat_id, group_name=update.effective_chat.title, category=LOG_CAT_MOD, action="BADWORD", details={"user": user_name, "user_id": str(user_id_bw), "gruppe": update.effective_chat.title, "details": f"Wort: {matched} — Strafe: {bw_punishment} — Gelöscht: {'ja' if deleted else 'nein'}"})
                    return


        left_member = update.message.left_chat_member
        if left_member and is_banned_in_group(update.effective_chat.id, left_member.id):
            try:
                await update.message.delete()
            except Exception as e:
                logger.error(f"Could not delete ban service message for {left_member.id} in {update.effective_chat.id}: {e}")

    for member in update.message.new_chat_members or []:
        track_user(member)
        chat_id = update.effective_chat.id

        # --- Bot Sperren: block bots added by non-admins ---
        if member.is_bot and member.id != context.bot.id:
            bot_data_sb = load_data()
            sb = bot_data_sb.get("sperr_bots", {"enabled": False})
            if sb.get("enabled", False):
                sb_groups = [str(g) for g in sb.get("groups", [])]
                applies = not sb_groups or str(chat_id) in sb_groups
                if applies:
                    adder = update.message.from_user
                    is_adm = await is_chat_admin(context, chat_id, adder.id) if adder else False
                    if not is_adm:
                        # Remove the bot
                        try:
                            await context.bot.ban_chat_member(chat_id=chat_id, user_id=member.id, revoke_messages=True)
                            await context.bot.unban_chat_member(chat_id=chat_id, user_id=member.id, only_if_banned=True)
                        except Exception as e:
                            logger.error(f"Bot-Sperren: Could not remove bot {member.id}: {e}")
                        # Delete service message
                        if sb.get("delete", True):
                            try:
                                await update.message.delete()
                            except Exception:
                                pass
                        # Punish the adder
                        punishment = sb.get("punishment", "ban")
                        adder_name = adder.full_name if adder else "Unknown"
                        adder_id = adder.id if adder else 0
                        try:
                            if punishment == "ban":
                                await context.bot.ban_chat_member(chat_id=chat_id, user_id=adder_id, revoke_messages=False)
                                remember_group_ban([chat_id], adder_id)
                            elif punishment == "kick":
                                await context.bot.ban_chat_member(chat_id=chat_id, user_id=adder_id, revoke_messages=False)
                                await context.bot.unban_chat_member(chat_id=chat_id, user_id=adder_id, only_if_banned=True)
                            elif punishment == "mute":
                                await context.bot.restrict_chat_member(chat_id=chat_id, user_id=adder_id, permissions=ChatPermissions.no_permissions())
                                set_active_mute(chat_id, adder_id)
                            elif punishment == "warn":
                                # Add a warning (use proper dict format)
                                warnings_sb = bot_data_sb.setdefault("warnings", {})
                                key = f"{chat_id}_{adder_id}"
                                warn_entry = warnings_sb.get(key, {"count": 0, "name": adder_name})
                                if not isinstance(warn_entry, dict):
                                    warn_entry = {"count": int(warn_entry) if isinstance(warn_entry, (int, float)) else 0, "name": adder_name}
                                warn_entry["count"] = warn_entry.get("count", 0) + 1
                                warn_entry["name"] = adder_name
                                warnings_sb[key] = warn_entry
                                save_data(bot_data_sb)
                        except Exception as e:
                            logger.error(f"Bot-Sperren punishment failed for {adder_id}: {e}")
                        await log_action(
                            context,
                            f"🤖 BOT-SPERREN: Bot {member.full_name} ({member.id}) entfernt aus {update.effective_chat.title}. "
                            f"Hinzugefügt von {adder_name} ({adder_id}) — Strafe: {punishment}",
                            group_id=chat_id,
                            group_name=update.effective_chat.title,
                        )
                        continue

        if is_banned_in_group(chat_id, member.id):
            try:
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=member.id, revoke_messages=True)
                try:
                    await update.message.delete()
                except Exception:
                    pass
                tracked_u = lookup_user(str(member.id))
                t_uname = f"@{tracked_u['username']}" if tracked_u and tracked_u.get("username") else member.full_name
                await log_action(context, "", group_id=chat_id, group_name=update.effective_chat.title, category=LOG_CAT_MOD, action="AUTO-WIEDERBANN", details={"user": t_uname, "user_id": str(member.id), "gruppe": update.effective_chat.title})
            except Exception as e:
                logger.error(f"Auto-reban via new_chat_members failed for {member.id} in {chat_id}: {e}")

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
            await log_action(context, "", group_id=update.effective_chat.id, group_name=update.effective_chat.title, category=LOG_CAT_MOD, action="AUTO-REBAN", details={"user": member.full_name, "user_id": str(member.id), "gruppe": update.effective_chat.title})
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
    chat_id = request.chat.id

    # Always block banned users
    if is_banned_in_group(chat_id, member.id):
        try:
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=member.id)
            await log_action(context, f"🚫 JOIN-REQUEST ABGELEHNT (gebannt): {member.full_name} ({member.id}) in {request.chat.title}")
        except Exception as e:
            logger.error(f"Decline join request failed for {member.id} in {chat_id}: {e}")
        return

    # Check if Freigabemodus is enabled for this group
    bot_data = load_data()
    auto_approve = bot_data.get("auto_approve", {})
    if not auto_approve.get(str(chat_id), False):
        return  # Freigabemodus not active, let Telegram handle it

    # --- Pre-approval checks ---

    # Check badwords in user's name
    badwords = bot_data.get("badwords", [])
    bw_config = bot_data.get("badwords_config", {"punishment": "aus"})
    if bw_config.get("punishment", "aus") != "aus" and badwords:
        user_name_lower = (member.full_name or "").lower()
        for word in badwords:
            if word.lower() in user_name_lower:
                try:
                    await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=member.id)
                    await log_action(context, f"🚫 JOIN-REQUEST ABGELEHNT (Namenssperre '{word}'): {member.full_name} ({member.id}) in {request.chat.title}")
                except Exception as e:
                    logger.error(f"Decline join request (name ban) failed for {member.id} in {chat_id}: {e}")
                return

    # All checks passed → auto-approve
    try:
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=member.id)
        logger.info(f"✅ Auto-approved join request: {member.full_name} ({member.id}) in {request.chat.title}")
    except Exception as e:
        logger.error(f"Auto-approve join request failed for {member.id} in {chat_id}: {e}")

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

    # If waiting for messenger input (broadcast with media)
    if state == WAITING_MESSENGER_INPUT:
        pending = user_data_store.get(user_id)
        if not pending or pending.get("action") != "messenger" or not pending.get("groups"):
            return
        msg = update.message

        media_file_id = None
        media_type = None
        if msg.photo:
            media_file_id = msg.photo[-1].file_id
            media_type = "photo"
        elif msg.video:
            media_file_id = msg.video.file_id
            media_type = "video"
        elif msg.animation:
            media_file_id = msg.animation.file_id
            media_type = "animation"
        elif msg.document and not (msg.document.file_name and msg.document.file_name.endswith(".json")):
            media_file_id = msg.document.file_id
            media_type = "document"

        if not media_file_id:
            return

        caption_html = msg.caption_html or msg.caption or ""
        groups = pending["groups"]
        success = 0
        fail = 0
        import time
        broadcast_id = str(int(time.time() * 1000))
        sent_msgs = []

        logger.info(
            "Messenger media broadcast start: user=%s media_type=%s caption_len=%s groups=%s chat_id=%s message_id=%s",
            user_id,
            media_type,
            len(caption_html),
            len(groups),
            msg.chat_id,
            msg.message_id,
        )

        for gid in groups:
            try:
                sent = None
                try:
                    if media_type == "photo":
                        sent = await context.bot.send_photo(
                            chat_id=gid,
                            photo=media_file_id,
                            caption=caption_html or None,
                            parse_mode="HTML" if caption_html else None,
                        )
                    elif media_type == "video":
                        sent = await context.bot.send_video(
                            chat_id=gid,
                            video=media_file_id,
                            caption=caption_html or None,
                            parse_mode="HTML" if caption_html else None,
                        )
                    elif media_type == "animation":
                        sent = await context.bot.send_animation(
                            chat_id=gid,
                            animation=media_file_id,
                            caption=caption_html or None,
                            parse_mode="HTML" if caption_html else None,
                        )
                    else:
                        sent = await context.bot.send_document(
                            chat_id=gid,
                            document=media_file_id,
                            caption=caption_html or None,
                            parse_mode="HTML" if caption_html else None,
                        )
                except Exception as send_err:
                    logger.error(f"Messenger direct media send failed in {gid}: {send_err}")
                    sent = await context.bot.copy_message(
                        chat_id=gid,
                        from_chat_id=msg.chat_id,
                        message_id=msg.message_id,
                    )

                sent_msgs.append((gid, sent.message_id))
                success += 1
            except Exception as e:
                fail += 1
                logger.error(f"Messenger media fallback failed in {gid}: {e}")

        bot_data = load_data()
        bot_data.setdefault("broadcasts", {})[broadcast_id] = {
            "messages": sent_msgs,
            "date": now_de().strftime("%d.%m %H:%M"),
            "count": success,
            "preview": ((msg.caption or "")[:50] if msg.caption else f"[{media_type}]"),
        }
        save_data(bot_data)

        keyboard = [[InlineKeyboardButton("🗑 Nachricht in allen Gruppen löschen", callback_data=f"del_broadcast_{broadcast_id}")]]
        await msg.reply_text(
            f"📨 Nachricht gesendet!\n✅ {success} Gruppen erfolgreich"
            + (f"\n❌ {fail} Fehler" if fail else ""),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        preview = ((msg.caption_html or msg.caption or "")[:100] if (msg.caption_html or msg.caption) else f"[{media_type}]")
        await log_action(context, f"MESSENGER: {update.effective_user.full_name} ({user_id}) → {success} Gruppen\nMedia: {media_type}\nText: {preview}")
        context.user_data["state"] = None
        user_data_store.pop(user_id, None)
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
    
    # Count groups that have notify config
    per_group = oc.get("per_group_notify", {})
    configured_count = sum(1 for v in per_group.values() if v)
    
    # Check which groups are currently open
    active = oc.get("active_open_messages", {})
    all_groups = await get_bot_groups(context)
    open_groups = [g["title"] for g in all_groups if str(g["id"]) in active]
    open_str = ", ".join(open_groups) if open_groups else "Keine"
    
    text = (
        f"🔓 <b>Open / Close</b>\n\n"
        f"🎨 Open-Sticker: {has_open_sticker}\n"
        f"🎨 Close-Sticker: {has_close_sticker}\n"
        f"📢 Gruppen mit Benachrichtigung: {configured_count}\n"
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
    keyboard.append([InlineKeyboardButton(f"📢 Gruppen-Benachrichtigungen ({configured_count})", callback_data="oc_source_groups")])
    keyboard.append([InlineKeyboardButton("✏️ Open-Text ändern", callback_data="oc_edit_open_text")])
    keyboard.append([InlineKeyboardButton("✏️ Close-Text ändern", callback_data="oc_edit_close_text")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="back_main")])
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_oc_source_groups(query, context):
    """Show list of source groups to configure notify targets for."""
    bot_data = load_data()
    oc = bot_data.get("open_close", {})
    per_group = oc.get("per_group_notify", {})
    all_groups = await get_bot_groups(context)
    
    keyboard = []
    for g in all_groups:
        notify_list = per_group.get(str(g["id"]), [])
        count = len(notify_list)
        label = f"{'✅' if count > 0 else '⬜'} {g['title']} → {count} Gruppen"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"oc_src_{g['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="menu_openclose")])
    
    await query.edit_message_text(
        "📢 <b>Gruppen-Benachrichtigungen</b>\n\n"
        "Wähle eine <b>Quell-Gruppe</b>, um festzulegen welche Gruppen benachrichtigt werden, "
        "wenn dort /open gemacht wird:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def show_oc_notify_for_source(query, context, source_gid):
    """Show notify group selection for a specific source group."""
    bot_data = load_data()
    oc = bot_data.get("open_close", {})
    per_group = oc.get("per_group_notify", {})
    notify = set(per_group.get(str(source_gid), []))
    all_groups = await get_bot_groups(context)
    
    source_name = next((g["title"] for g in all_groups if g["id"] == source_gid), str(source_gid))
    
    keyboard = []
    for g in all_groups:
        if g["id"] == source_gid:
            continue  # Don't show source group as target
        check = "✅" if g["id"] in notify else "⬜"
        keyboard.append([InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"oc_ntfy_{source_gid}_{g['id']}")])
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data=f"oc_ntfy_all_{source_gid}"),
        InlineKeyboardButton("◻️ Keine", callback_data=f"oc_ntfy_none_{source_gid}"),
    ])
    keyboard.append([InlineKeyboardButton(f"🔙 Zurück ({len(notify)} gewählt)", callback_data="oc_source_groups")])
    
    await query.edit_message_text(
        f"📢 <b>Benachrichtigungen für: {source_name}</b>\n\n"
        f"Wenn in <b>{source_name}</b> /open gemacht wird, welche Gruppen sollen benachrichtigt werden?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def handle_open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /open command in a group."""
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return
    
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Dieser Befehl funktioniert nur in Gruppen.")
        return
    
    bot_data = load_data()
    oc = bot_data.get("open_close", {})
    # Get notify groups for THIS source group (per-group config)
    per_group = oc.get("per_group_notify", {})
    notify_groups = per_group.get(str(chat.id), [])
    
    # Fallback to old global notify_groups if per_group not configured
    if not notify_groups and not per_group:
        notify_groups = oc.get("notify_groups", [])
    
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
    
    # Delete the /open command message itself
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Delete /open command failed: {e}")
    
    notify_text = f"\n📢 {len(sent_messages)} Gruppen benachrichtigt." if sent_messages else ""
    await context.bot.send_message(
        chat_id=chat.id,
        text=f"🔓 *{chat.title}* ist jetzt OPEN!{notify_text}",
        parse_mode="Markdown",
    )
    await log_action(context, f"OPEN: {chat.title} von {update.effective_user.full_name} → {len(sent_messages)} Gruppen benachrichtigt")


async def handle_close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /close command in a group - deletes the open notifications."""
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
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
    
    # Delete the /close command message itself
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Delete /close command failed: {e}")
    
    # Always show close confirmation
    close_info = f"\n🗑 {deleted_count} Open-Nachrichten gelöscht." if deleted_count > 0 else ""
    await context.bot.send_message(
        chat_id=chat.id,
        text=f"🔒 *{chat.title}* ist jetzt CLOSED!{close_info}",
        parse_mode="Markdown",
    )
    
    await log_action(context, f"CLOSE: {chat.title} von {update.effective_user.full_name} → {deleted_count} Nachrichten gelöscht")


# --- Main ---

# --- /del - Delete a replied-to message ---

async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete the message that was replied to."""
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Antworte auf eine Nachricht, die gelöscht werden soll.")
        return

    try:
        await update.message.reply_to_message.delete()
    except Exception as e:
        logger.error(f"Delete message failed: {e}")
        await update.message.reply_text("❌ Konnte die Nachricht nicht löschen.")
        return

    # Delete the /del command itself
    try:
        await update.message.delete()
    except Exception:
        pass


# --- /send - Send anonymous message through bot ---

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send an anonymous message through the bot in the current group."""
    user_id = update.effective_user.id
    if not await is_group_authorized(context, user_id, update.effective_chat):
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Dieser Befehl funktioniert nur in Gruppen.")
        return

    # Get text after /send
    text = update.message.text.split(None, 1)
    if len(text) < 2:
        await update.message.reply_text("⚠️ Nutzung: /send <Nachricht>")
        return

    msg_text = text[1]

    # Delete the /send command message
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Delete /send command failed: {e}")

    # Send as bot
    await context.bot.send_message(chat_id=chat.id, text=msg_text, parse_mode="HTML")


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


async def _render_pers_grp_menu(query, pending):
    """Render group selection for /personal command."""
    cmd_name = pending.get("cmd_name", "")
    selected = pending.get("selected", set())
    bot_data = load_data()
    groups = bot_data.get("groups", [])
    keyboard = []
    row = []
    for g in groups:
        check = "✅" if g["id"] in selected else "⬜"
        row.append(InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"pers_grp_{g['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data="pers_grp_all"),
        InlineKeyboardButton("◻️ Keine", callback_data="pers_grp_none"),
    ])
    keyboard.append([InlineKeyboardButton(f"✅ Speichern ({len(selected)} gewählt)", callback_data="pers_grp_save")])
    keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data="pers_grp_cancel")])
    await query.edit_message_text(
        f"🏗 <b>/{html.escape(cmd_name)}</b> — Wähle Gruppen:\n\n"
        f"<i>Keine Auswahl = gilt für alle Gruppen</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def _render_pcmd_editgrp_menu(query, cmd_name, selected):
    """Render group selection for editing existing personal command groups."""
    bot_data = load_data()
    groups = bot_data.get("groups", [])
    keyboard = []
    row = []
    for g in groups:
        check = "✅" if g["id"] in selected else "⬜"
        row.append(InlineKeyboardButton(f"{check} {g['title']}", callback_data=f"pcmd_egrp_{g['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("☑️ Alle", callback_data="pcmd_egrp_all"),
        InlineKeyboardButton("◻️ Keine", callback_data="pcmd_egrp_none"),
    ])
    keyboard.append([InlineKeyboardButton(f"✅ Speichern ({len(selected)} gewählt)", callback_data="pcmd_egrp_save")])
    keyboard.append([InlineKeyboardButton("🔙 Zurück", callback_data="pcmd_list")])
    await query.edit_message_text(
        f"✏️ <b>/{html.escape(cmd_name)}</b> — Gruppen bearbeiten:\n\n"
        f"<i>Keine Auswahl = gilt für alle Gruppen</i>",
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
        if next_fire_str == "—" and sched.get("next_run_at"):
            next_fire_str = sched.get("next_run_at")
    
    last_sent = sched.get("last_sent") or "None"
    
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



# get_interval_label is already defined at module top level (line ~638)


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
        sched["next_run_at"] = (now_de() + datetime.timedelta(minutes=sched.get("interval_minutes", 60))).strftime("%d.%m.%Y %H:%M")
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
    next_run_at_str = sched.get("next_run_at")
    delay = None

    # Priority 1: Explicit next-run anchor (used after create/edit/activate like Group Help)
    if next_run_at_str:
        try:
            next_run = datetime.datetime.strptime(next_run_at_str, "%d.%m.%Y %H:%M").replace(tzinfo=BERLIN_TZ)
            while next_run <= now:
                next_run += interval_td
            delay = (next_run - now).total_seconds()
            logger.info(f"Scheduled {sched_id}: next_run_at={next_run_at_str}, next run in {delay:.0f}s")
        except Exception as e:
            logger.error(f"Error parsing next_run_at for {sched_id}: {e}")

    # Priority 2: If we have a real last_sent timestamp, calculate next run from there
    if delay is None and last_sent_str:
        try:
            last_sent_dt = datetime.datetime.strptime(last_sent_str, "%d.%m.%Y %H:%M").replace(tzinfo=BERLIN_TZ)
            next_run = last_sent_dt + interval_td
            while next_run <= now:
                next_run += interval_td
            delay = (next_run - now).total_seconds()
            logger.info(f"Scheduled {sched_id}: last_sent={last_sent_str}, next run in {delay:.0f}s")
        except Exception as e:
            logger.error(f"Error parsing last_sent for {sched_id}: {e}")

    # Priority 3: Legacy fallback to configured start time
    if delay is None and time_str and time_str != "00:00":
        try:
            h, m = map(int, time_str.split(":"))
        except Exception:
            h, m = 0, 0
        first_run = now.replace(hour=h, minute=m, second=0, microsecond=0)
        while first_run <= now:
            first_run += interval_td
        delay = (first_run - now).total_seconds()
    elif delay is None:
        delay = max(1, interval)
        logger.info(f"No start time, next_run_at or last_sent for {sched_id}, starting after one interval")
    
    jq.run_repeating(
        execute_scheduled_message,
        interval=interval,
        first=max(1, int(delay)),
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
    # Pre-cache bot username so resolve_target never needs a slow get_me() call
    global BOT_USERNAME_CACHE
    try:
        me = await application.bot.get_me()
        BOT_USERNAME_CACHE = me.username
        logger.info(f"Bot username cached: @{BOT_USERNAME_CACHE}")
    except Exception as e:
        logger.error(f"Failed to cache bot username: {e}")

    # Command menu: only visible for admins/private chats, hidden for normal group members
    from telegram import (
        BotCommandScopeDefault,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeAllChatAdministrators,
        BotCommand,
    )

    admin_commands = [
        BotCommand("ban", "Benutzer bannen"),
        BotCommand("unban", "Benutzer entbannen"),
        BotCommand("mute", "Benutzer muten"),
        BotCommand("warn", "Benutzer verwarnen"),
        BotCommand("kick", "Benutzer kicken"),
        BotCommand("del", "Nachricht löschen"),
        BotCommand("multidel", "Mehrere Nachrichten löschen"),
        BotCommand("send", "Anonyme Nachricht senden"),
        BotCommand("banall", "In allen Gruppen bannen"),
    ]

    # 1) Clear default/global commands so normal users don't inherit any menu
    try:
        await application.bot.delete_my_commands(scope=BotCommandScopeDefault())
        logger.info("Default bot commands cleared")
    except Exception as e:
        logger.warning(f"Could not clear default commands: {e}")

    # 2) Clear generic group commands for non-admin members
    try:
        await application.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
        logger.info("Bot commands hidden for normal group members")
    except Exception as e:
        logger.warning(f"Could not delete group commands: {e}")

    # 3) Show commands for group admins only
    try:
        await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeAllChatAdministrators())
        logger.info("Bot commands set for group admins")
    except Exception as e:
        logger.warning(f"Could not set admin commands: {e}")

    # 4) Show commands in private chats
    try:
        private_commands = [BotCommand("start", "Bot starten"), BotCommand("settings", "Einstellungen")] + admin_commands
        await application.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        logger.info("Bot commands set for private chats")
    except Exception as e:
        logger.warning(f"Could not set private chat commands: {e}")

    bot_data = load_data()
    count = 0
    for sched in bot_data.get("scheduled", []):
        if sched.get("active"):
            schedule_job(application, sched)
            count += 1
    logger.info(f"Restored {count} scheduled jobs")


# --- /teamgruppe command ---
async def teamgruppe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle filter exemption for the current group. Admin-only."""
    await auto_delete_command(update, context)
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        return
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return
    chat = update.effective_chat
    bot_data = load_data()
    exempt = bot_data.setdefault("exempt_groups", [])
    if chat.id in exempt:
        exempt.remove(chat.id)
        save_data(bot_data)
        try:
            await update.message.reply_text(
                f"❌ <b>{html.escape(chat.title)}</b> ist jetzt NICHT mehr filterfrei.\n"
                f"Alle Filter (verbotene Wörter, Links, Forwards) sind wieder aktiv.",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        exempt.append(chat.id)
        save_data(bot_data)
        try:
            await update.message.reply_text(
                f"✅ <b>{html.escape(chat.title)}</b> ist jetzt filterfrei.\n"
                f"Alle Filter (verbotene Wörter, Links, Forwards) sind deaktiviert.",
                parse_mode="HTML"
            )
        except Exception:
            pass


def main():
    cfg = load_config()
    token = cfg.get("bot_token", "")
    if not token or token == "DEIN_BOT_TOKEN_HIER":
        print("❌ Bitte trage deinen Bot-Token in config.json ein!")
        return

    # Lock file to prevent multiple instances
    lock_path = os.path.join(os.path.dirname(__file__), "bot.lock")
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            logger.warning(f"Bot already running (PID {old_pid}), killing old instance...")
            os.kill(old_pid, signal.SIGTERM)
            import time; time.sleep(2)
        except (ProcessLookupError, ValueError, OSError):
            pass
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(lock_path) and os.remove(lock_path))

    # Simple webhook clear before polling starts
    try:
        _requests.post(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=10,
        )
    except Exception:
        pass

    app = Application.builder().token(token).concurrent_updates(False).post_init(post_init).build()

    app.add_handler(CommandHandler("reload", reload_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("registergroup", register_group))
    app.add_handler(CommandHandler("unregistergroup", unregister_group))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("banall", banall))
    app.add_handler(CommandHandler("unbanall", unbanall))
    app.add_handler(CommandHandler("personal", personal_command))
    app.add_handler(CommandHandler("unpersonal", unpersonal_command))
    app.add_handler(CommandHandler("mute", mute_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(CommandHandler("kick", kick_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("unwarn", unwarn_command))
    app.add_handler(CommandHandler("free", free_command))
    app.add_handler(CommandHandler("unfree", unfree_command))
    app.add_handler(CommandHandler("open", handle_open_command))
    app.add_handler(CommandHandler("close", handle_close_command))
    app.add_handler(CommandHandler("multidel", multidel_command))
    app.add_handler(CommandHandler("del", del_command))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler("report", handle_admin_report))
    app.add_handler(CommandHandler("teamgruppe", teamgruppe_command))
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

    # Global error handler to log uncaught exceptions
    async def error_handler(update, context):
        logger.error(f"Unhandled exception: {context.error}", exc_info=context.error)
    app.add_error_handler(error_handler)

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
