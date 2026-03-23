import json
import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

# --- Config helpers ---

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"groups": []}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

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
    
    # Ban/Unban buttons for everyone (admin + owner)
    keyboard = [
        [InlineKeyboardButton("🚫 Ban", callback_data="action_ban"),
         InlineKeyboardButton("✅ Unban", callback_data="action_unban")],
    ]
    
    # Owner-only buttons
    if is_owner(user_id):
        keyboard.append([InlineKeyboardButton("👥 Gruppen anzeigen", callback_data="show_groups")])
        keyboard.append([
            InlineKeyboardButton("➕ Admin hinzufügen", callback_data="add_admin"),
            InlineKeyboardButton("➖ Admin entfernen", callback_data="remove_admin"),
        ])
        keyboard.append([InlineKeyboardButton("📢 Log-Kanal setzen", callback_data="set_log")])

    role = "👑 Owner" if is_owner(user_id) else "🛡️ Admin"
    await update.message.reply_text(
        f"🤖 *Ban-Bot Menü* ({role})\nWähle eine Aktion:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

# --- Callback handler ---

# Conversation states
WAITING_BAN_INPUT, WAITING_UNBAN_INPUT = range(2)
WAITING_ADMIN_ADD, WAITING_ADMIN_REMOVE = range(2, 4)
WAITING_LOG_CHANNEL = 4
WAITING_GROUP_SELECT_BAN, WAITING_GROUP_SELECT_UNBAN = range(5, 7)

# Store pending data
user_data_store = {}

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_authorized(user_id):
        await query.edit_message_text("⛔ Kein Zugriff.")
        return

    data = query.data

    if data == "show_groups":
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.\nNutze /registergroup in einer Gruppe.")
            return
        text = "👥 *Registrierte Gruppen:*\n\n"
        for g in groups:
            text += f"• {g['title']} (`{g['id']}`)\n"
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "action_ban":
        groups = await get_bot_groups(context)
        if not groups:
            await query.edit_message_text("Keine Gruppen registriert.")
            return
        keyboard = []
        keyboard.append([InlineKeyboardButton("🔴 ALLE GRUPPEN", callback_data="ban_all_groups")])
        for g in groups:
            keyboard.append([InlineKeyboardButton(g["title"], callback_data=f"ban_group_{g['id']}")])
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
    if not is_admin(user_id):
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
        results = []

        for gid in groups:
            try:
                if action == "ban":
                    await context.bot.ban_chat_member(chat_id=gid, user_id=target_id)
                    results.append(f"✅ Gebannt in `{gid}`")
                else:
                    await context.bot.unban_chat_member(chat_id=gid, user_id=target_id, only_if_banned=True)
                    results.append(f"✅ Entbannt in `{gid}`")
            except Exception as e:
                results.append(f"❌ Fehler in `{gid}`: {e}")

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

# --- /registergroup - run in a group to add it ---

async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Kein Zugriff.")
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Dieser Befehl funktioniert nur in Gruppen.")
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
    if not is_admin(update.effective_user.id):
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
    if not is_admin(update.effective_user.id):
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
            await context.bot.ban_chat_member(chat_id=g["id"], user_id=target_id)
            results.append(f"✅ {g['title']}")
        except Exception as e:
            results.append(f"❌ {g['title']}: {e}")

    result_text = "\n".join(results)
    await update.message.reply_text(
        f"🚫 *{target_name}* (`{target_id}`) gebannt:\n\n{result_text}",
        parse_mode="Markdown",
    )
    await log_action(
        context,
        f"BANALL: {target_name} ({target_id}) von {update.effective_user.full_name}\n{result_text}",
    )

# --- /unbanall ---

async def unbanall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
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

    result_text = "\n".join(results)
    await update.message.reply_text(
        f"✅ *{target_name}* (`{target_id}`) entbannt:\n\n{result_text}",
        parse_mode="Markdown",
    )
    await log_action(
        context,
        f"UNBANALL: {target_name} ({target_id}) von {update.effective_user.full_name}\n{result_text}",
    )

# --- User tracker: silently track all messages in groups ---

async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track every user who sends a message in a group."""
    if update.message and update.message.from_user:
        track_user(update.message.from_user)

# --- Main ---

def main():
    cfg = load_config()
    token = cfg.get("bot_token", "")
    if not token or token == "DEIN_BOT_TOKEN_HIER":
        print("❌ Bitte trage deinen Bot-Token in config.json ein!")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("registergroup", register_group))
    app.add_handler(CommandHandler("unregistergroup", unregister_group))
    app.add_handler(CommandHandler("banall", banall))
    app.add_handler(CommandHandler("unbanall", unbanall))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_handler))
    # Track all group messages to build username → ID database
    app.add_handler(MessageHandler(filters.ALL & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), track_message), group=1)

    print("🤖 Bot gestartet!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
