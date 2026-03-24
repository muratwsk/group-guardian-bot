import atexit
import json
import logging
import os
import signal
import subprocess
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

# Store pending data
user_data_store = {}

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

    # === SETTINGS ===
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
            "date": datetime.datetime.now().strftime("%d.%m %H:%M"),
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

# --- Import groups from JSON file ---

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
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_handler))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, document_handler))
    app.add_handler(MessageHandler(filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), track_message), group=1)
    app.add_handler(ChatMemberHandler(enforce_ban_on_chat_member, ChatMemberHandler.CHAT_MEMBER), group=2)
    app.add_handler(ChatJoinRequestHandler(block_banned_join_request), group=3)

    print("🤖 Bot gestartet!")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
