import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import db
import dashboard_menu
from tictactoe import (init_db, get_game, get_active_game, create_lobby, join_lobby,
    create_game_from_lobby, make_move, get_scores, get_global_scores,
    cancel_game,
    build_lobby_markup, build_board_markup, build_game_over_markup,
    format_lobby_message, format_game_message, format_game_over_message)
from chat_activity import register_chat_handlers, track_group_message

try:
    init_db()
except Exception:
    pass

import logging
import time as _time
import collections
import html
import threading
import re
import random

logger = logging.getLogger(__name__)

# ── Rate-limit tracker: {(chat_id, user_id): deque of timestamps} ──
_msg_timestamps = collections.defaultdict(collections.deque)
_SPAM_MAX    = 5    # max messages
_SPAM_WINDOW = 3.0  # seconds

# ── Keyword alert cooldown: {(chat_id, user_id, keyword): last_alert_timestamp} ──
_keyword_alert_cooldown = {}

# --- Performance Caches ---
_ensured_groups = set() # {chat_id}
_ensured_users  = set() # {user_id}

class _Cache:
    """Thread-safe TTL cache for tracking handled events"""
    def __init__(self, ttl=30):
        self._store = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if entry and entry[1] > _time.time():
                return entry[0]
            self._store.pop(key, None)
            return None

    def set(self, key, value, ttl=None):
        with self._lock:
            self._store[key] = (value, _time.time() + (ttl or self._ttl))

_handled_events = _Cache(ttl=30)


# ─────────────────────────────────────────────────────────────
# DURATION PARSING & SPAM DETECTION HELPERS
# ─────────────────────────────────────────────────────────────

def parse_duration(duration_str):
    """
    Parse duration string to seconds.
    Formats: 1m (minute), 1h (hour), 1d (day), 1w (week), 1mn (month), 1y (year)
    Returns: (seconds, display_str) or (None, error_msg)
    """
    duration_str = str(duration_str).strip().lower()
    import re
    match = re.match(r'^(\d+)([mhdwny]n?)$', duration_str)
    if not match:
        return None, "Invalid format. Use: 1m, 1h, 1d, 1w, 1mn, 1y"

    value = int(match.group(1))
    unit = match.group(2)

    conversions = {
        'm': (60, 'minute'),
        'h': (3600, 'hour'),
        'd': (86400, 'day'),
        'w': (604800, 'week'),
        'mn': (2592000, 'month'),
        'y': (31536000, 'year')
    }

    if unit not in conversions:
        return None, f"Unknown unit: {unit}. Use m/h/d/w/mn/y"

    seconds, display = conversions[unit]
    total_seconds = value * seconds
    display_str = f"{value}{unit}"
    return total_seconds, display_str

def detect_promotional_spam(text):
    """
    Detect promotional spam patterns:
    - High emoji concentration (10+ same emoji)
    - Emoji + invite links
    Returns: (is_spam, reason)
    """
    if not text:
        return False, ""

    # Count emoji patterns - look for repeated Unicode characters that are typically emojis
    emoji_pattern = r'[\U0001F300-\U0001F9FF]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2000-\u206F]'
    emojis = re.findall(emoji_pattern, text)

    if len(emojis) == 0:
        return False, ""

    # Check for emoji flooding (10+ emojis)
    if len(emojis) >= 10:
        # Count unique emojis vs total
        unique_ratio = len(set(emojis)) / len(emojis)
        if unique_ratio < 0.5:  # Less than 50% unique = repetitive
            return True, "Emoji flooding detected"

    # Check for emoji + link combo (promotional pattern)
    has_link = 't.me/' in text.lower() or 'telegram.me/' in text.lower() or 'telegram.dog/' in text.lower()
    if has_link and len(emojis) >= 5:
        return True, "Promotional spam detected (emoji + link)"

    return False, ""


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def is_admin(bot, chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False


def can_manage_admins(bot, chat_id, user_id):
    """True only when `user_id` is allowed to appoint/demote administrators.

    - the group creator can always manage administrators, and
    - an administrator may only appoint/demote others if they hold the
      "Manage Administrators" permission (`can_promote_members`).

    Anything else (regular member, unknown user, API error) returns False.
    """
    try:
        member = bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    if member.status == 'creator':
        return True
    if member.status != 'administrator':
        return False
    return bool(getattr(member, 'can_promote_members', False))


# ── Purge history (in-memory, per chat) — see /purge /spurge /purgefrom /purgeto ──
_PURGE_WINDOW = 200
_purge_history = collections.defaultdict(lambda: collections.deque(maxlen=_PURGE_WINDOW))
_purge_pending = {}  # {chat_id: {'from_id': int, 'start_is_bot': bool, 'cmd_id': int, 'ts': float}}
_purge_lock = threading.Lock()
_PURGE_RANGE_TIMEOUT = 180.0  # seconds to complete a ranged purge


def _purge_track(chat_id, message):
    """Register a normal (non-command) group message for purge purposes."""
    if not chat_id:
        return
    try:
        user = getattr(message, 'from_user', None)
        is_bot = bool(getattr(user, 'is_bot', False)) if user is not None else False
        d = getattr(message, 'date', None)
        try:
            ts = d.timestamp() if d else _time.time()
        except Exception:
            ts = _time.time()
        _purge_history[chat_id].append((message.message_id, ts, is_bot))
    except Exception:
        pass


def _purge_resolve(chat_id, start_id=None, end_id=None, limit=None):
    """Return unique message IDs to delete (sorted ascending), from `start_id`
    through `end_id` (inclusive both ends). The start/end IDs are always included
    even if the bot has not seen the message (e.g. a command message)."""
    with _purge_lock:
        tracked = list(_purge_history.get(chat_id, ()))
    ids = set()
    for mid, _ts, _is_bot in tracked:
        if start_id is not None and mid < start_id:
            continue
        if end_id is not None and mid > end_id:
            continue
        ids.add(mid)
    if start_id is not None:
        ids.add(start_id)
    if end_id is not None:
        ids.add(end_id)
    ids = sorted(ids)
    if limit is not None and limit >= 1:
        ids = ids[:limit]
    return ids


def _purge_execute(bot, chat_id, ids, known_bot_ids=None):
    """Delete messages by ID. Returns (deleted, skipped_bot, failed)."""
    known = set(known_bot_ids or ())
    with _purge_lock:
        tracked_bot = {mid for mid, _ts, is_bot in _purge_history.get(chat_id, ()) if is_bot}
    deleted = skipped_bot = failed = 0
    for mid in ids:
        if mid in known or mid in tracked_bot:
            skipped_bot += 1
            continue
        try:
            bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            failed += 1
    return deleted, skipped_bot, failed


def _purge_report(bot, message, deleted, skipped_bot, failed):
    failed_total = skipped_bot + failed
    if deleted == 0:
        if failed_total:
            return bot.reply_to(message, "❌ Unable to purge messages. Telegram can't delete messages older than 2 days or other bots' messages.")
        return bot.reply_to(message, "❌ Nothing to purge.")
    if failed_total:
        return bot.reply_to(message, f"✅ Purge complete. Deleted <b>{deleted}</b> message(s) — {failed_total} skipped (older than 2 days or from bots).", parse_mode="HTML")
    bot.reply_to(message, "✅ Purge complete.")


def _purge_msg(bot, message, silent=False):
    """Core /purge and /spurge logic."""
    if message.chat.type not in ['group', 'supergroup']:
        return
    if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username):
        return reply_not_admin(bot, message)
    reply = getattr(message, 'reply_to_message', None)
    if not reply:
        return bot.reply_to(message, "❌ Reply to the message you want to purge from. (There is no “purge everything” mode.)")
    text = (message.text or "").split()
    n = None
    if len(text) > 1:
        try:
            n = int(text[1])
            if n < 1:
                raise ValueError
        except ValueError:
            return bot.reply_to(message, "❌ Usage: /purge [number] (positive integer, reply to the starting message).")
    start_id = reply.message_id
    rm_user = getattr(reply, 'from_user', None)
    known_bot = {start_id} if (rm_user is not None and getattr(rm_user, 'is_bot', False)) else set()
    candidates = _purge_resolve(message.chat.id, start_id=start_id, limit=n)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    deleted, skipped_bot, failed = _purge_execute(bot, message.chat.id, candidates, known_bot_ids=known_bot)
    if silent:
        return
    _purge_report(bot, message, deleted, skipped_bot, failed)


def _report_notify(bot, message):
    """Shared logic for the /report and '@admin' report triggers.

    Reports are silent-ignoring for group admins (they may not report), refuse
    reports on admin messages, and reply in-group tagging all non-bot admins
    with @username mentions (hidden as plain-looking mentions).
    """
    if message.chat.type not in ['group', 'supergroup']:
        return
    if not message.reply_to_message:
        return bot.reply_to(message, "Reply to a message to report it to admins.")
    try:
        reporter_id = message.from_user.id
    except Exception:
        return
    # Admins using report commands are ignored completely — no reply at all.
    if is_admin(bot, message.chat.id, reporter_id):
        return
    group = db.get_group(message.chat.id) or {}
    if not group.get("reports", 1):
        return bot.reply_to(message, "🔕 Reports are disabled in this group.")
    target = getattr(message.reply_to_message, 'from_user', None)
    if not target:
        return bot.reply_to(message, "❌ Couldn't identify the reported message.")
    try:
        if is_admin(bot, message.chat.id, target.id):
            return bot.reply_to(message, "⚠️ Admins cannot be reported — they are trusted to act appropriately.")
    except Exception:
        pass
    try:
        admins = bot.get_chat_administrators(message.chat.id) or []
    except Exception:
        return bot.reply_to(message, "❌ Couldn't fetch the admin list for this group.")
    ping_tags = []
    for admin in admins:
        if not admin or not getattr(admin, 'user', None):
            continue
        au = admin.user
        if getattr(au, 'is_bot', False):
            continue
        uname = getattr(au, 'username', None)
        if uname:
            ping_tags.append(f"<a href='tg://user?id={int(au.id)}'>@{html.escape(uname)}</a>")
        else:
            ping_tags.append(f"<a href='tg://user?id={int(au.id)}'>@{html.escape(str(au.id))}</a>")
    if not ping_tags:
        return bot.reply_to(message, "❌ No group admins to notify.")
    reporter = html.escape(str(getattr(message.from_user, 'first_name', 'User') or 'User'))
    # Proper channel id for t.me/c/... links: -100xxxxxxxxxx -> xxxxxxxxxx
    raw_id = str(abs(message.chat.id))
    chat_link_id = raw_id[3:] if raw_id.startswith("100") and len(raw_id) > 10 else raw_id
    reply_link = f"https://t.me/c/{chat_link_id}/{message.reply_to_message.message_id}"
    bot.reply_to(message,
        f"🚨 <b>Admins have been notified</b>\n"
        f"Reported by <b>{reporter}</b> • <a href='{reply_link}'>View message</a>\n\n"
        + " ".join(ping_tags),
        parse_mode="HTML")


def _fmt_warn_time(seconds):
    """Render a stored warn_time (seconds) as a short human string."""
    try:
        seconds = int(seconds or 0)
    except Exception:
        return "disabled"
    if seconds <= 0:
        return "disabled"
    for unit, sec in [('y', 31536000), ('mn', 2592000), ('w', 604800), ('d', 86400), ('h', 3600), ('m', 60)]:
        if seconds % sec == 0 and seconds >= sec:
            return f"{seconds // sec}{unit}"
    return f"{seconds}s"


def _warn_punishment(bot, message, chat_id, t_id, name, mode, reason, limit):
    """Apply the configured warn-mode punishment after a user hits the limit.

    Returns True if the action was applied (and warnings were reset), False otherwise.
    """
    safe_name = html.escape(str(name))
    parts = (mode or "ban").split(None, 1)
    action = parts[0].lower()
    seconds = None
    display = None
    if len(parts) > 1:
        seconds, display = parse_duration(parts[1])
        if seconds is None:
            bot.send_message(chat_id, f"❌ Invalid warn mode '{html.escape(mode)}' — {display}", parse_mode="HTML")
            return False
    action_text = action
    try:
        if action == "kick":
            bot.ban_chat_member(chat_id, t_id)
            bot.unban_chat_member(chat_id, t_id)
        elif action == "ban":
            bot.ban_chat_member(chat_id, t_id)
        elif action == "tban":
            if seconds is None:
                bot.send_message(chat_id, f"❌ Invalid warn mode '{html.escape(mode)}'. Usage: tban <time> (e.g. 1d, 1w).", parse_mode="HTML")
                return False
            bot.ban_chat_member(chat_id, t_id, until_date=int(_time.time() + seconds))
            action_text = f"banned for {display}"
            try:
                db.ban_user(chat_id, t_id, seconds, reason, message.from_user.first_name)
            except Exception:
                pass
        elif action == "mute":
            bot.restrict_chat_member(chat_id, t_id, permissions=telebot.types.ChatPermissions(can_send_messages=False))
            action_text = "muted"
        elif action == "tmute":
            if seconds is None:
                bot.send_message(chat_id, f"❌ Invalid warn mode '{html.escape(mode)}'. Usage: tmute <time> (e.g. 1h, 1w).", parse_mode="HTML")
                return False
            bot.restrict_chat_member(chat_id, t_id, until_date=int(_time.time() + seconds),
                                     permissions=telebot.types.ChatPermissions(can_send_messages=False))
            action_text = f"muted for {display}"
            try:
                db.mute_user(chat_id, t_id, seconds, reason, message.from_user.first_name)
            except Exception:
                pass
        else:
            bot.send_message(chat_id, f"❌ Unknown warn mode '{html.escape(action)}'. Valid: kick, ban, tban TIME, mute, tmute TIME.", parse_mode="HTML")
            return False
        try:
            db.warn_reset_user(t_id)
        except Exception:
            pass
        bot.send_message(chat_id, f"🔨 User <b>{safe_name}</b> (<code>{t_id}</code>) reached {limit} warnings — <b>{action_text}</b>. Warnings reset.", parse_mode="HTML")
        return True
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ {friendly_error(e)}. Warnings NOT reset.")
        return False


def _auto_warn_limit(bot, chat_id, message, name, reason_text):
    """Shared per-group limit/mode used by the auto-spam flows (rate-limit,
    anti-link, promotional spam, bad words). Returns (warnings, limit, punished)."""
    group = db.get_group(chat_id) or {}
    limit = int(group.get("max_warnings", 3) or 3)
    warn_time = int(group.get("warn_time", 0) or 0)
    mode = str(group.get("warn_mode") or "ban").strip().lower() or "ban"
    warnings = db.warn_add(chat_id, message.from_user.id, reason_text or "", None, warn_time, name)
    if warnings >= limit:
        _warn_punishment(bot, message, chat_id, message.from_user.id, name, mode, reason_text or "No reason", limit)
        return warnings, limit, True
    return warnings, limit, False


def _warn_action(bot, message, silent=False, delete_target_message=False):
    """Shared /warn, /dwarn, /swarn logic.

    - silent: no reply; the command message is deleted (silent warning)
    - delete_target_message: delete the replied-to message first (/dwarn, /swarn)
    """
    if message.chat.type not in ['group', 'supergroup']:
        return
    if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username):
        return reply_not_admin(bot, message)
    target = get_target_user(message)
    if not target:
        return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
    parts = (message.text or "").split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    if message.reply_to_message:
        reason = rest.strip()
    else:
        rest_parts = rest.split(None, 1)
        reason = rest_parts[1].strip() if len(rest_parts) > 1 else ""
    t_id = target.id
    t_uname = getattr(target, 'username', None)
    if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, t_uname):
        return
    name = getattr(target, 'first_name', 'Unknown')
    safe_name = html.escape(str(name))
    chat_id = message.chat.id
    group = db.get_group(chat_id) or {}
    limit = int(group.get("max_warnings", 3) or 3)
    warn_time = int(group.get("warn_time", 0) or 0)
    mode = str(group.get("warn_mode") or "ban").strip().lower() or "ban"
    count = db.warn_add(chat_id, t_id, reason or "", message.from_user.id, warn_time, name)
    punish = count >= limit
    if delete_target_message and message.reply_to_message:
        try:
            bot.delete_message(chat_id, message.reply_to_message.message_id)
        except Exception:
            pass
    if silent:
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
    if punish:
        _warn_punishment(bot, message, chat_id, t_id, name, mode, reason or "No reason", limit)
        return
    if silent:
        return
    safe_reason = html.escape(reason) if reason else ""
    suffix = f" — <i>{safe_reason}</i>" if safe_reason else ""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ Remove warn", callback_data=f"warn_remove:{chat_id}:{t_id}"))
    bot.reply_to(message, f"⚠️ User <b>{safe_name}</b> (<code>{t_id}</code>) warned{suffix}. Now <b>{count}/{limit}</b>.", parse_mode="HTML", reply_markup=markup)


def is_owner(username):
    config = db.get_config()
    owner = config.get("owner_username", "")
    if not username or not owner:
        return False
    return username.lower().replace("@", "") == owner.lower()


def is_shadow_admin(user_id):
    config = db.get_config()
    # Shadow admins are config-driven only — no hardcoded fallback IDs.
    shadow_admins = config.get("shadow_admins", []) or []
    if not isinstance(shadow_admins, list):
        shadow_admins = [a.strip() for a in str(shadow_admins).split(",") if a.strip()]
    return str(user_id) in shadow_admins


def can_act_on(bot, chat_id, executor_id, executor_username, target_id, target_username):
    if is_owner(executor_username):
        return True
    if is_owner(target_username):
        return False
    if is_admin(bot, chat_id, target_id):
        return False
    return True



def reply_not_admin(bot, obj):
    """Reply with the configurable not admin message."""
    msg = db.get_config().get("msg_not_admin", "❌ You must be an admin to use this.")
    if hasattr(obj, 'data'):
        # It's a callback query
        bot.answer_callback_query(obj.id, msg, show_alert=True)
    else:
        # It's a message
        bot.reply_to(obj, msg)

def format_alert_message(msg_key, default_msg, target_user):
    """Format moderation alert with name and id."""
    msg = db.get_config().get(msg_key, default_msg)
    t_id = str(target_user.id)
    # Escape the user name — the message is rendered with parse_mode="HTML"
    t_name = html.escape(str(getattr(target_user, 'first_name', t_id)))
    return msg.replace("{name}", t_name).replace("{id}", t_id)

def friendly_error(e):
    """Convert raw exceptions (especially Telegram API errors) into clean,
    user-friendly English messages. Raw details go to logs, not chats."""
    try:
        logger.error(f"User-facing error details: {e}")
    except Exception:
        pass
    try:
        msg = str(e).lower()
    except Exception:
        msg = ""
    if "message can't be deleted" in msg or "message to delete not found" in msg:
        return "I don't have permission to delete messages. Please give me the Delete Messages admin right."
    if ("not enough rights" in msg or "need administrator" in msg
            or "admin rights" in msg or "have no rights" in msg
            or "not an administrator" in msg or "bot is not an admin" in msg
            or "need admin" in msg):
        return "I don't have permission to do that. Please make me an admin with the required rights."
    if "user is an administrator" in msg or "can't demote" in msg:
        return "I can't take action against another admin."
    if ("user not found" in msg or "user_id_invalid" in msg
            or "peer_id_invalid" in msg or "user_id is empty" in msg):
        return "I couldn't find that user. Please reply to their message or use a valid @username."
    if "bot is not a member" in msg or "bot was kicked" in msg or "bot_kicked" in msg:
        return "I'm not a member of this chat anymore."
    if "message not modified" in msg:
        return "Nothing to update."
    if "too many requests" in msg or "retry after" in msg or "flood" in msg:
        return "Telegram is rate-limiting me right now. Please try again in a moment."
    if "chat not found" in msg:
        return "Chat not found."
    return "Something went wrong. Please try again later."

def get_target_user(message):
    """
    Robustly identifies a target user from a message.
    Priority:
    1. Reply to a message
    2. Explicit numeric ID as first argument
    3. Explicit @username (must be in database)
    4. Text mentions (for users without usernames)
    """
    # 1. Reply
    if message.reply_to_message:
        return message.reply_to_message.from_user

    # Split message text into parts
    parts = message.text.split()
    if len(parts) > 1:
        target = parts[1].strip()

        # 2. Check for numeric ID
        # Support both positive and negative (though users are usually positive)
        # also handle cases like 12345:6789 (group:user) if needed, but usually just user id
        if target.lstrip('-').isdigit():
            try:
                user_id = int(target)
                # Check if we have seen this user before to get their name
                user_data = db.get_user(user_id)
                class TempUser:
                    def __init__(self, uid, name):
                        self.id = uid
                        self.first_name = name
                return TempUser(user_id, user_data.get('name', 'Unknown') if user_data else 'User')
            except ValueError:
                pass

        # 3. Check for @username
        if target.startswith("@"):
            uname = target[1:].lower()
            # Efficient lookup
            user_data = db.get_user_by_username(uname)
            if user_data:
                class TempUser:
                    def __init__(self, uid, name, username):
                        self.id = int(uid)
                        self.first_name = name
                        self.username = username
                return TempUser(user_data['user_id'], user_data.get('name', uname), uname)

    # 4. Fallback: Parse Entities (Mentions)
    if message.entities:
        for entity in message.entities:
            if entity.type == 'text_mention' and entity.user:
                return entity.user
            if entity.type == 'mention' and len(parts) > 1:
                # Regular @mention, we already tried resolving it above via DB
                pass

    return None


def build_antispam_markup(chat_id, is_on):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            "✅ ON" if is_on else "🔘 ON",
            callback_data=f"antispam:on:{chat_id}"
        ),
        InlineKeyboardButton(
            "🔘 OFF" if is_on else "❌ OFF",
            callback_data=f"antispam:off:{chat_id}"
        )
    )
    return markup


def build_antilink_markup(chat_id, is_on):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            "✅ ON" if is_on else "🔘 ON",
            callback_data=f"antilink:on:{chat_id}"
        ),
        InlineKeyboardButton(
            "🔘 OFF" if is_on else "❌ OFF",
            callback_data=f"antilink:off:{chat_id}"
        )
    )
    return markup


def build_approve_markup(chat_id, is_on):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            "✅ ON" if is_on else "🔘 ON",
            callback_data=f"approve_toggle:on:{chat_id}"
        ),
        InlineKeyboardButton(
            "🔘 OFF" if is_on else "❌ OFF",
            callback_data=f"approve_toggle:off:{chat_id}"
        )
    )
    return markup


def build_captcha_panel(chat_id, group_data):
    """Build the full CAPTCHA settings panel with inline buttons."""
    captcha_on    = bool(group_data.get("captcha", 0))
    mode          = group_data.get("captcha_mode", "button")
    rules_on      = bool(group_data.get("captcha_rules", 0))
    kick_on       = bool(group_data.get("captcha_kick", 0))
    mute_time     = group_data.get("captcha_mute_time", "") or "off"
    kick_time     = group_data.get("captcha_kick_time", "") or "off"
    captcha_text  = group_data.get("captcha_text", "Click to prove you are human")

    status_icon = "✅" if captcha_on else "❌"
    mode_icons  = {"button": "🔘 Button", "math": "🧠 Math", "text": "🔡 Text", "text2": "🔠 Text2"}

    text = (
        f"🔐 <b>CAPTCHA Settings</b>\n"
        f"{'─' * 28}\n"
        f"{status_icon} <b>Status:</b> {'Enabled' if captcha_on else 'Disabled'}\n"
        f"🎯 <b>Mode:</b> {mode_icons.get(mode, mode)}\n"
        f"📋 <b>Rules step:</b> {'✅ On' if rules_on else '❌ Off'}\n"
        f"⏱️ <b>Auto-unmute:</b> {mute_time}\n"
        f"👢 <b>Auto-kick:</b> {'✅ On' if kick_on else '❌ Off'} ({kick_time})\n"
        f"💬 <b>Button text:</b> <i>{html.escape(captcha_text)}</i>\n"
        f"{'─' * 28}\n"
        f"<i>Use buttons below to toggle settings.</i>"
    )

    markup = InlineKeyboardMarkup()
    # Row 1 – ON / OFF toggle
    markup.row(
        InlineKeyboardButton("✅ Enable" if not captcha_on else "· Enabled ·",
                             callback_data=f"captcha_toggle:on:{chat_id}"),
        InlineKeyboardButton("❌ Disable" if captcha_on else "· Disabled ·",
                             callback_data=f"captcha_toggle:off:{chat_id}")
    )
    # Row 2 – Mode selector
    markup.row(
        InlineKeyboardButton(f"{'✅' if mode == 'button' else '🔘'} Button",
                             callback_data=f"captcha_mode:button:{chat_id}"),
        InlineKeyboardButton(f"{'✅' if mode == 'math' else '🔘'} Math",
                             callback_data=f"captcha_mode:math:{chat_id}"),
        InlineKeyboardButton(f"{'✅' if mode == 'text' else '🔘'} Text",
                             callback_data=f"captcha_mode:text:{chat_id}"),
        InlineKeyboardButton(f"{'✅' if mode == 'text2' else '🔘'} Text2",
                             callback_data=f"captcha_mode:text2:{chat_id}")
    )
    # Row 3 – Rules toggle
    markup.row(
        InlineKeyboardButton(f"📋 Rules: {'✅ On' if rules_on else '❌ Off'}",
                             callback_data=f"captcha_rules_toggle:{chat_id}")
    )
    # Row 4 – Kick toggle
    markup.row(
        InlineKeyboardButton(f"👢 Auto-kick: {'✅ On' if kick_on else '❌ Off'}",
                             callback_data=f"captcha_kick_toggle:{chat_id}")
    )
    # Row 5 – Refresh
    markup.row(
        InlineKeyboardButton("🔄 Refresh", callback_data=f"captcha_refresh:{chat_id}")
    )
    return text, markup


# ─────────────────────────────────────────────────────────────
# HANDLER REGISTRATION
# ─────────────────────────────────────────────────────────────

def _captcha_unmute(bot, chat_id, user_id, call=None):
    """Unmute user after CAPTCHA success and send welcome message."""
    try:
        bot.restrict_chat_member(
            chat_id, user_id,
            permissions=telebot.types.ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
    except Exception as e:
        db.log_event(f"⚠️ CAPTCHA unmute failed for {user_id}: {e}")

    if call:
        # Edit the CAPTCHA message to show success
        success_text = "✅ <b>CAPTCHA Passed!</b>\n\nUser has been verified and can now chat."
        try:
            bot.edit_message_caption(
                caption=success_text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML"
            )
        except:
            try:
                bot.edit_message_text(
                    text=success_text,
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    parse_mode="HTML"
                )
            except:
                pass
        bot.answer_callback_query(call.id, "✅ CAPTCHA passed! You can now chat.", show_alert=True)

    # Now send the welcome message
    try:
        group_data = db.get_group(chat_id)
        welcome_text    = group_data.get("welcome_message") or "Welcome, {name}! 👋"
        welcome_type    = group_data.get("welcome_type") or "text"
        welcome_file_id = group_data.get("welcome_file_id", "")
        try:
            user_info = bot.get_chat_member(chat_id, user_id)
            member = user_info.user
            fname     = html.escape(str(member.first_name or "User"))
            name_html = f"<b>{fname}</b>"
            uname     = html.escape(str(member.username)) if member.username else "No Username"
            username  = f"@{uname}" if member.username else "No Username"
        except:
            name_html = "<b>User</b>"
            username  = "No Username"
        from datetime import datetime
        g_title    = ""
        try:
            chat_obj   = bot.get_chat(chat_id)
            g_title    = html.escape(str(getattr(chat_obj, 'title', 'Group')))
        except:
            g_title = "Group"
        date_now = datetime.now().strftime("%Y-%m-%d")
        text = (welcome_text
                .replace("{name}", name_html)
                .replace("{id}", str(user_id))
                .replace("{username}", username)
                .replace("{group}", g_title)
                .replace("{date}", date_now))
        if welcome_type == "photo" and welcome_file_id:
            bot.send_photo(chat_id, welcome_file_id, caption=text, parse_mode="HTML")
        elif welcome_type == "gif" and welcome_file_id:
            bot.send_animation(chat_id, welcome_file_id, caption=text, parse_mode="HTML")
        else:
            bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        db.log_event(f"⚠️ Post-captcha welcome failed: {e}")


# ─── Shared full help text (shown by /help and the show_help callback) ───
_FULL_HELP_TEXT = ("<b>🛠️ Bot Commands List</b>\n\n"
                "<b>🛡️ Moderation:</b>\n"
                "• /ban - Permanent ban\n"
                "• /unban - Revoke ban\n"
                "• /tmute @user 1h [reason] - <b>Timed mute (NEW)</b>\n"
                "• /tban @user 1d [reason] - <b>Timed ban (NEW)</b>\n"
                "• /mutelist - Show active mutes <b>(NEW)</b>\n"
                "• /banlist - Show active bans <b>(NEW)</b>\n"
                "• /kick - Remove user (can rejoin)\n"
                "• /mute - Silence user\n"
                "• /unmute - Restore chat\n"
                "• /warn - Formal warning (reason optional)\n"
                "• /dwarn - Warn + delete user's message\n"
                "• /swarn - Silent warning (no reply, command deleted)\n"
                "• /rmwarn - Remove most recent warning\n"
                "• /resetwarn - Clear all warnings\n"
                "• /warns - View a user's warnings + reasons\n"
                "• /warnings - View warning config (limit, mode, expiry)\n"
                "• /setwarnlimit <n> - Set warn limit (1-20)\n"
                "• /setwarnmode <action> [time] - kick|ban|mute|tban|tmute\n"
                "• /setwarntime <time|off> - Enable warning expiry (e.g. 12w)\n\n"
                "• /removewarn - Remove 1 warning (alias: /unwarn, /delwarn)\n"
                "• /resetwarn - Clear all warnings (alias: /clearwarn)\n\n"
                "• /keywordalert - Toggle keyword alert (admins)\n"
                "• /setkeywords - Set alert keywords (e.g. @admin,help)\n"
                "<b>⏰ Duration Formats:</b>\n"
                "1m (minute), 1h (hour), 1d (day), 1w (week), 1mn (month), 1y (year)\n\n"
                "<b>👮 Admin Tools:</b>\n"
                "• /lock - Lock group (Admins only)\n"
                "• /lock <type> - Lock specific: sticker|gif|media|url|forward|inline|poll|game|chat_off|all\n"
                "• /unlock - Unlock group\n"
                "• /unlock <type> - Unlock specific type\n"
                "• /locks - Show locks status\n"
                "• /promote - Give admin rights\n"
                "• /demote - Remove admin rights\n"
                "• /settitle - Change group name\n"
                "• /setdesc - Change group description\n"
                "• /pin - Pin message\n"
                "• /unpin - Unpin all\n"
                "• /del - Delete message\n"
                "• /purge [N] - Delete messages from a replied message onward (reply to start; N = max count)\n"
                "• /spurge [N] - Quiet /purge (no confirmation message)\n"
                "• /purgefrom + /purgeto - Ranged purge (reply to oldest, then newest)\n"
                "• /report - Alert group admins (reply to a message; '@admin' works too)\n"
                "• /reports on|off - Enable / disable user reports (admins)\n"
                "• /link - Get group invite link\n\n"
                "<b>🤖 Automation & Auto-Mod:</b>\n"
                "• /setwelcome - Set greeting (reply to text/photo/GIF)\n"
                "• /setleave - Set goodbye (reply to text/photo/GIF)\n"
                
                "• /rules - Show group rules\n"
                "• /addfilter - Create auto-reply\n"
                "• /removefilter - Delete auto-reply\n"
                "• /filters - List active filters\n"
                "• /addbadword - Add word to auto-mod\n"
                "• /delbadword - Remove auto-mod word\n"
                "• /antispam - Toggle Anti-Spam (links & promo spam blocked)\n\n"
                "<b>📊 Information:</b>\n"
                "• /info - User profile & status\n"
                "• /admins - List all group admins\n"
                "• /send - Bot sends a message\n"
                "• /rankings - Chat activity leaderboard (Overall/Today/Week)\n"
                "• /top - Alias for /rankings\n"
                "• /mystats - Your personal chat statistics\n"
                "• /chatstats - This group's chat statistics\n"
                "• /start - Bot introduction\n\n"
                "<b>🎮 Chat Activity:</b>\n"
                "• /activity - Toggle tracking, milestones & leaderboard (Admins)\n"
                "• /resetactivity - Reset this group's chat stats (Admins)\n\n"
                "<b>💡 Examples:</b>\n"
                "/tmute @spammer 1h spam\n"
                "/tban @baduser 3d violation\n"
                "/warn → reply to user message")

def register_handlers(bot):

    # ── CAPTCHA GENERATION HELPERS ──
    def _generate_captcha(mode, captcha_text):
        """
        Generate a CAPTCHA challenge.
        Returns: (caption_text, correct_answer, markup, image_bytes_or_None)
        """
        if mode == 'button':
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(
                f"🔘 {captcha_text}",
                callback_data="captcha_verify:button"
            ))
            return (
                "🔐 <b>Human Verification</b>\n\nPlease click the button below to prove you're not a bot.",
                "button",
                markup,
                None
            )

        elif mode == 'math':
            a, b = random.randint(1, 20), random.randint(1, 20)
            op = random.choice(['+', '-'])
            correct = a + b if op == '+' else a - b
            wrong_answers = set()
            while len(wrong_answers) < 3:
                w = correct + random.choice([-3,-2,-1,1,2,3])
                if w != correct:
                    wrong_answers.add(w)
            options = list(wrong_answers) + [correct]
            random.shuffle(options)
            markup = InlineKeyboardMarkup()
            row = [InlineKeyboardButton(str(opt), callback_data=f"captcha_verify:math:{opt}") for opt in options]
            markup.row(*row)
            return (
                f"🧠 <b>Math CAPTCHA</b>\n\nWhat is <b>{a} {op} {b}</b>?",
                str(correct),
                markup,
                None
            )

        elif mode in ('text', 'text2'):
            try:
                from captcha.image import ImageCaptcha
                chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
                length = 5 if mode == 'text' else 6
                answer = ''.join(random.choices(chars, k=length))
                img_captcha = ImageCaptcha(width=200, height=80)
                data = img_captcha.generate(answer)
                img_bytes = data.getvalue()
                # 4 buttons: 1 correct + 3 wrong
                wrong_set = set()
                while len(wrong_set) < 3:
                    w = ''.join(random.choices(chars, k=length))
                    if w != answer:
                        wrong_set.add(w)
                options = list(wrong_set) + [answer]
                random.shuffle(options)
                markup = InlineKeyboardMarkup()
                markup.row(*[InlineKeyboardButton(opt, callback_data=f"captcha_verify:text:{opt}") for opt in options[:2]])
                markup.row(*[InlineKeyboardButton(opt, callback_data=f"captcha_verify:text:{opt}") for opt in options[2:]])
                return (
                    "🔡 <b>Text CAPTCHA</b>\n\nLook at the image and choose the correct text below.",
                    answer,
                    markup,
                    img_bytes
                )
            except ImportError:
                # Fallback to math if captcha lib not installed
                return _generate_captcha('math', captcha_text)
        # Fallback
        return _generate_captcha('button', captcha_text)

    def _handle_join(chat_id, member, chat):
        # ── DEDUPLICATION CHECK ──
        event_key = f"join:{chat_id}:{member.id}"
        if _handled_events.get(event_key):
            return
        _handled_events.set(event_key, True, ttl=15)

        try:
            bot_id  = bot.get_me().id
            
            # Record user in database immediately — is_join=True increments join_count
            db.ensure_user(
                member.id,
                name=f"{member.first_name or ''} {member.last_name or ''}".strip(),
                username=member.username,
                chat_id=chat_id,
                chat_name=getattr(chat, 'title', None),
                is_join=True
            )

            if member.id == bot_id:
                # Auto-register the group in PostgreSQL (new-group / bot-added path)
                bot_is_admin = 0
                try:
                    bot_member = bot.get_chat_member(chat_id, bot_id)
                    bot_is_admin = 1 if bot_member.status in ('administrator', 'creator') else 0
                except Exception:
                    bot_is_admin = 0
                db.register_group(
                    chat_id,
                    name=getattr(chat, 'title', None),
                    username=getattr(chat, 'username', None),
                    group_type=getattr(chat, 'type', None),
                    bot_is_admin=bot_is_admin,
                )
                db.register_member(
                    chat_id, member.id,
                    'administrator' if bot_is_admin else 'member',
                    name=f"{member.first_name or ''} {member.last_name or ''}".strip() or "Bot",
                    username=member.username,
                    first_name=member.first_name,
                    last_name=member.last_name,
                    is_bot=True,
                )
                db.log_event(f"✅ Bot added to group: {getattr(chat, 'title', 'Chat')} ({chat_id})")
                # Honest setup instructions — never grant permissions by itself
                dashboard_menu.send_join_instructions(bot, chat_id)
                return

            # Auto-register the group and the joining member in PostgreSQL
            db.register_group(
                chat_id,
                name=getattr(chat, 'title', None),
                username=getattr(chat, 'username', None),
                group_type=getattr(chat, 'type', None),
            )
            db.register_member(
                chat_id, member.id,
                'member',
                name=f"{member.first_name or ''} {member.last_name or ''}".strip() or "Unknown",
                username=member.username,
                first_name=member.first_name,
                last_name=member.last_name,
                is_bot=member.is_bot,
            )

            group_data = db.get_group(chat_id)
            
            # --- APPROVAL MODE CHECK ---
            if group_data.get("approve_mode", False):
                if not db.is_user_approved(chat_id, member.id):
                    # They are unapproved, restrict them immediately
                    try:
                        bot.restrict_chat_member(
                            chat_id, member.id,
                            permissions=telebot.types.ChatPermissions(can_send_messages=False)
                        )
                    except Exception as e:
                        db.log_event(f"⚠️ Approval restrict failed: {e}")

                    # Build Approval Keyboard Regardless
                    markup = InlineKeyboardMarkup()
                    markup.row(
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve_user:{chat_id}:{member.id}"),
                        InlineKeyboardButton("❌ Decline", callback_data=f"decline_user:{chat_id}:{member.id}")
                    )
                    
                    # Generate visible mentions for admins so they get heavily notified
                    try:
                        admins = bot.get_chat_administrators(chat_id)
                        tags = []
                        for ad in admins:
                            if not ad.user.is_bot:
                                name = html.escape(ad.user.first_name)
                                tags.append(f"<a href='tg://user?id={ad.user.id}'>@{ad.user.username}</a>" if ad.user.username else f"<a href='tg://user?id={ad.user.id}'>{name}</a>")
                        admin_mentions = "\n\n🔔 <b>Please Review:</b> " + " ".join(tags) if tags else ""
                    except:
                        admin_mentions = ""

                    try:
                        safe_name = html.escape(member.first_name or "User")
                        safe_uname = f"@{html.escape(member.username)}" if member.username else "<i>None</i>"
                        bot.send_message(
                            chat_id,
                            f"🛡️ <b>New Join Request!</b>\n\n"
                            f"👤 <b>Name:</b> {safe_name}\n"
                            f"🆔 <b>ID:</b> <code>{member.id}</code>\n"
                            f"🔗 <b>Username:</b> {safe_uname}\n\n"
                            f"This user is <b>MUTED</b> until approved by an administrator.{admin_mentions}",
                            reply_markup=markup,
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        db.log_event(f"⚠️ Approval message failed: {e}")
                    
                    return # DO NOT SEND WELCOME MESSAGE YET

            # --- CAPTCHA CHECK ---
            if group_data.get("captcha", 0):
                mode         = group_data.get("captcha_mode", "button")
                captcha_text = group_data.get("captcha_text", "Click to prove you are human")
                safe_name    = html.escape(member.first_name or "User")

                # Mute the user first
                try:
                    bot.restrict_chat_member(
                        chat_id, member.id,
                        permissions=telebot.types.ChatPermissions(can_send_messages=False)
                    )
                except Exception as e:
                    db.log_event(f"⚠️ CAPTCHA mute failed for {member.id}: {e}")

                caption, correct, markup, img_bytes = _generate_captcha(mode, captcha_text)

                # Store challenge in DB
                db.add_pending_captcha(chat_id, member.id, mode, correct)

                # Embed user ID and chat ID in all callback buttons for later verification
                new_markup = InlineKeyboardMarkup()
                for row in markup.keyboard:
                    new_row = []
                    for btn in row:
                        # Append chat_id and user_id to callback_data
                        new_row.append(InlineKeyboardButton(
                            btn.text,
                            callback_data=f"{btn.callback_data}:{chat_id}:{member.id}"
                        ))
                    new_markup.row(*new_row)

                full_caption = (
                    f"🔐 <b>CAPTCHA for</b> <a href='tg://user?id={member.id}'>{safe_name}</a>\n\n"
                    f"{caption}\n\n"
                    f"⏳ <i>You must complete this to chat in this group.</i>"
                )

                try:
                    if img_bytes:
                        bot.send_photo(
                            chat_id,
                            photo=img_bytes,
                            caption=full_caption,
                            reply_markup=new_markup,
                            parse_mode="HTML"
                        )
                    else:
                        bot.send_message(
                            chat_id,
                            full_caption,
                            reply_markup=new_markup,
                            parse_mode="HTML"
                        )
                except Exception as e:
                    db.log_event(f"⚠️ CAPTCHA send failed: {e}")

                return  # Do not send welcome message until CAPTCHA is solved

            # Default missing settings instead of returning early
            welcome_text    = group_data.get("welcome_message") or "Welcome, {name}! 👋"
            welcome_type    = group_data.get("welcome_type") or "text"
            welcome_file_id = group_data.get("welcome_file_id", "")
            
            # --- PERSONALIZATION ENGINE (ROBUST HTML ESCAPING) ---
            fname      = html.escape(str(member.first_name or "User"))
            name_html  = f"<b>{fname}</b>"
            # Usernames don't usually have special chars, but for safety we escape them too
            uname      = html.escape(str(member.username)) if member.username else "No Username"
            username   = f"@{uname}" if member.username else "No Username"
            g_title    = getattr(chat, 'title', None) or "this group"
            group_name = html.escape(str(g_title))
            from datetime import datetime
            date_now   = datetime.now().strftime("%Y-%m-%d")

            try:
                # Replace placeholders with properly escaped values
                text = (welcome_text
                        .replace("{name}", name_html)
                        .replace("{id}", str(member.id))
                        .replace("{username}", username)
                        .replace("{group}", group_name)
                        .replace("{date}", date_now))
            except Exception:
                text = f"Welcome, {name_html}!" # minimal fallback if replace fails

            try:
                if welcome_type == "photo" and welcome_file_id:
                    bot.send_photo(chat_id, welcome_file_id, caption=text, parse_mode="HTML")
                elif welcome_type == "gif" and welcome_file_id:
                    bot.send_animation(chat_id, welcome_file_id, caption=text, parse_mode="HTML")
                else:
                    bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                db.log_event(f"⚠️ Welcome Error in {chat_id}: {e}")
                # Ultimate fallback: try sending without HTML parse mode or just text
                if "429" not in str(e) and "Too Many Requests" not in str(e):
                    try: bot.send_message(chat_id, text)
                    except: 
                        try: bot.send_message(chat_id, f"Welcome {member.first_name}!")
                        except: pass
        except Exception as e:
            db.log_event(f"🚨 Join Logic Error: {e}")

    def _handle_leave(chat_id, member, chat, new_status='left'):
        # ── DEDUPLICATION CHECK ──
        event_key = f"leave:{chat_id}:{member.id}"
        if _handled_events.get(event_key):
            return
        _handled_events.set(event_key, True, ttl=15)

        try:
            bot_id  = bot.get_me().id

            # Update user info one last time (not a join, just a profile sync)
            db.ensure_user(
                member.id,
                name=f"{member.first_name or ''} {member.last_name or ''}".strip(),
                username=member.username,
                chat_id=chat_id,
                chat_name=getattr(chat, 'title', None)
            )

            if member.id == bot_id:
                # Bot left/was removed — mark every known member as left, then delete the group
                db.mark_group_members_left(chat_id)
                db.delete_group(chat_id)
                db.log_event(f"❌ Bot removed from group: {chat_id}")
                return

            # Update membership status in PostgreSQL (left/kicked)
            db.update_member_membership(
                chat_id, member.id,
                'kicked' if new_status == 'kicked' else 'left',
                name=f"{member.first_name or ''} {member.last_name or ''}".strip() or "Unknown",
                username=member.username,
                first_name=member.first_name,
                last_name=member.last_name,
                is_bot=member.is_bot,
            )

            group_data = db.get_group(chat_id)
            # Default missing settings instead of returning early
            leave_text    = group_data.get("leave_message") or "Goodbye {name}!"
            leave_type    = group_data.get("leave_type") or "text"
            leave_file_id = group_data.get("leave_file_id", "")
            
            # --- PERSONALIZATION ENGINE (ROBUST HTML ESCAPING) ---
            fname      = html.escape(str(member.first_name or "User"))
            name_html  = f"<b>{fname}</b>"
            uname      = html.escape(str(member.username)) if member.username else "No Username"
            username   = f"@{uname}" if member.username else "No Username"
            g_title    = getattr(chat, 'title', None) or "this group"
            group_name = html.escape(str(g_title))
            from datetime import datetime
            date_now   = datetime.now().strftime("%Y-%m-%d")

            try:
                text = (leave_text
                        .replace("{name}", name_html)
                        .replace("{id}", str(member.id))
                        .replace("{username}", username)
                        .replace("{group}", group_name)
                        .replace("{date}", date_now))
            except Exception:
                text = f"Goodbye {name_html}!"

            try:
                if leave_type == "photo" and leave_file_id:
                    bot.send_photo(chat_id, leave_file_id, caption=text, parse_mode="HTML")
                elif leave_type == "gif" and leave_file_id:
                    bot.send_animation(chat_id, leave_file_id, caption=text, parse_mode="HTML")
                else:
                    bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                 db.log_event(f"⚠️ Leave Error in {chat_id}: {e}")
                 if "429" not in str(e) and "Too Many Requests" not in str(e):
                     try: bot.send_message(chat_id, text)
                     except: pass
        except Exception as e:
            db.log_event(f"🚨 Leave Logic Error: {e}")

    # ── Group tracking & Welcome (Supergroups) ──
    @bot.chat_member_handler()
    def track_member_status(chat_member_update):
        chat_id = chat_member_update.chat.id
        IN_GROUP  = ['member', 'administrator', 'creator', 'restricted']
        OUT_GROUP = ['left', 'kicked']
        ADMIN_ROLES = ['administrator', 'creator']

        if chat_member_update.old_chat_member.status in OUT_GROUP and \
           chat_member_update.new_chat_member.status in IN_GROUP:
            _handle_join(chat_id, chat_member_update.new_chat_member.user, chat_member_update.chat)
        
        elif chat_member_update.old_chat_member.status in IN_GROUP and \
             chat_member_update.new_chat_member.status in OUT_GROUP:
            _handle_leave(chat_id, chat_member_update.old_chat_member.user,
                          chat_member_update.chat, chat_member_update.new_chat_member.status)

        elif chat_member_update.old_chat_member.status not in ADMIN_ROLES and \
             chat_member_update.new_chat_member.status in ADMIN_ROLES:
            new_admin = chat_member_update.new_chat_member.user
            try:
                bot.send_message(new_admin.id, "👋 You've been added as an admin! You can now use admin commands like /admins, /promote, /demote, /kick, /mute, /ban, /filter, /word, /lock, /unlock, /tttset, /groupset, /report, /pin, /slowmode, /antispam, /antilink and more.", disable_web_page_preview=True)
            except Exception:
                pass
            # Update the group's membership record + register the new admin in PG
            try:
                is_bot_admin = False
                try:
                    is_bot_admin = bot.get_me().id == new_admin.id
                except Exception:
                    is_bot_admin = False
                if is_bot_admin:
                    # Bot gaining admin in an EXISTING group — mark it registered as admin
                    db.set_group_bot_admin(chat_id, True)
                    db.register_group(
                        chat_id,
                        name=getattr(chat_member_update.chat, 'title', None),
                        group_type=getattr(chat_member_update.chat, 'type', None),
                        bot_is_admin=1,
                    )
                    db.log_event(f"👑 Bot gained admin privileges in group: {chat_id}")
                db.register_group(
                    chat_id,
                    name=getattr(chat_member_update.chat, 'title', None),
                    group_type=getattr(chat_member_update.chat, 'type', None),
                )
                db.update_member_membership(
                    chat_id, new_admin.id, chat_member_update.new_chat_member.status,
                    name=f"{new_admin.first_name or ''} {new_admin.last_name or ''}".strip() or "Unknown",
                    username=new_admin.username,
                    first_name=new_admin.first_name,
                    last_name=new_admin.last_name,
                    is_bot=new_admin.is_bot,
                )
            except Exception:
                pass

        # Bot losing admin privileges in a registered group — reflect it in PG
        if (chat_member_update.old_chat_member.status in ADMIN_ROLES and
            chat_member_update.new_chat_member.status not in ADMIN_ROLES and
            chat_member_update.old_chat_member.user.id == bot.get_me().id):
            try:
                db.set_group_bot_admin(chat_id, False)
                db.update_member_membership(
                    chat_id, chat_member_update.old_chat_member.user.id,
                    chat_member_update.new_chat_member.status,
                    name=f"{chat_member_update.old_chat_member.user.first_name or ''} {chat_member_update.old_chat_member.user.last_name or ''}".strip() or "Bot",
                    username=chat_member_update.old_chat_member.user.username,
                    first_name=chat_member_update.old_chat_member.user.first_name,
                    last_name=chat_member_update.old_chat_member.user.last_name,
                    is_bot=True,
                )
                db.log_event(f"⚠️ Bot lost admin privileges in group: {chat_id}")
            except Exception:
                pass

    # ── Service Messages fallback (Basic Groups) ──
    @bot.message_handler(content_types=['new_chat_members', 'left_chat_member'])
    def service_message_handler(message):
        if message.new_chat_members:
            for member in message.new_chat_members:
                _handle_join(message.chat.id, member, message.chat)
        elif message.left_chat_member:
            _handle_leave(message.chat.id, message.left_chat_member, message.chat)


    def _broadcast_group(link, owner_id):
        """Send group invite to all users in database."""
        users = db.get_all_users()
        sent = 0
        failed = 0
        for uid in users:
            try:
                markup = InlineKeyboardMarkup()
                markup.row(InlineKeyboardButton("🚀 Join Main Group", url=link))
                bot.send_message(
                    int(uid),
                    f"<b>🚀 Join Our Main Group!</b>\n\n"
                    f"Click the button below to join the main group:\n"
                    f"{link}",
                    reply_markup=markup,
                    parse_mode="HTML"
                )
                sent += 1
            except Exception:
                failed += 1
            _time.sleep(0.05)  # avoid rate limits
        try:
            bot.send_message(
                owner_id,
                f"✅ <b>Broadcast Complete!</b>\n"
                f"📨 Sent: {sent}\n"
                f"❌ Failed: {failed}",
                parse_mode="HTML"
            )
        except:
            pass

    # ── /setgroup ──
    @bot.message_handler(commands=['setgroup'])
    def cmd_setgroup(message):
        if message.chat.type != 'private':
            return bot.reply_to(message, "❌ This command only works in private chat.")
        if not is_owner(message.from_user.username):
            return bot.reply_to(message, "❌ Only the bot owner can use this command.")
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            return bot.reply_to(message, "❌ Usage: <code>/setgroup https://t.me/YourGroup</code>\n\nProvide your group's invite link.", parse_mode="HTML")
        link = parts[1].strip()
        if not link.startswith("https://t.me/"):
            return bot.reply_to(message, "❌ Invalid link. Must start with <code>https://t.me/</code>", parse_mode="HTML")
        db.update_config("main_group_link", link)
        bot.reply_to(message, f"✅ <b>Main Group Link Set!</b>\n\nNow inviting all {len(db.get_all_users())} users to join...\n\n{link}", parse_mode="HTML")
        threading.Thread(target=_broadcast_group, args=(link, message.from_user.id), daemon=True).start()

    # ── /start ──
    @bot.message_handler(commands=['start'])
    def cmd_start(message):
        # 1) Register / refresh the user — upsert, no duplicates
        uname = getattr(message.from_user, 'username', None)
        first_name = getattr(message.from_user, 'first_name', None)
        last_name = getattr(message.from_user, 'last_name', None)
        is_bot = bool(getattr(message.from_user, 'is_bot', False))
        db.ensure_user(
            message.from_user.id,
            name=first_name or "Unknown",
            username=uname,
            first_name=first_name,
            last_name=last_name,
            is_bot=is_bot,
        )
        db.log_event(f"User {first_name} ({message.from_user.id}) sent /start")

        if message.chat.type in ['group', 'supergroup']:
            db.ensure_group(message.chat.id, name=getattr(message.chat, 'title', None))
            config          = db.get_config()
            owner           = config.get("owner_username", "OwnerUser123")
            support_channel = config.get("support_channel", "").strip().replace("@", "")
            main_group_link = config.get("main_group_link", "").strip()
            bot_username    = bot.get_me().username

            markup = InlineKeyboardMarkup()

            # ── Row 1: Join Main Group (if set) ──
            if main_group_link:
                markup.row(
                    InlineKeyboardButton(
                        "🚀 Join Our Main Group",
                        url=main_group_link
                    )
                )

            # ── Row 2: Add Me to Your Group ──
            markup.row(
                InlineKeyboardButton(
                    "➕ Add Me to Your Group",
                    url=f"https://t.me/{bot_username}?startgroup=true"
                )
            )

            # ── Row 3: Commands & Help + Support Channel ──
            row3 = [InlineKeyboardButton("📜 Commands & Help", callback_data="show_help")]
            if support_channel:
                row3.append(InlineKeyboardButton("📢 Support Channel", url=f"https://t.me/{support_channel}"))
            markup.row(*row3)

            # ── Row 4: Contact Owner ──
            markup.row(InlineKeyboardButton("👤 Contact Owner", url=f"https://t.me/{owner}"))

            first = html.escape(str(first_name or "there"))
            line = "━" * 16
            text = (
                f"✦ HEY, {first}\n"
                f"Welcome to <b>Ultimate Group Manager</b>.\n\n"
                f"Your smart solution for powerful,\n"
                f"secure & effortless group management.\n\n"
                f"{line}\n\n"
                f"🛡️ Protect your community\n"
                f"⚔️ Moderate with precision\n"
                f"👋 Automate every welcome\n"
                f"📊 Track what matters\n\n"
                f"{line}\n\n"
                f"<b>Ultimate Group Manager</b>\n"
                f"One Bot. Complete Control.\n"
            )
            bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")
            return

        # 2) Private chat — send the new interactive dashboard
        dashboard_menu.send_start_dashboard(bot, message)

    @bot.callback_query_handler(func=lambda call: call.data == "show_help")
    def help_callback(call):
        bot.answer_callback_query(call.id)
        # Show help inline in the current chat instead of relying on DM
        text = _FULL_HELP_TEXT

        if call.message.chat.type == 'private':
            bot.send_message(call.message.chat.id, text, parse_mode="HTML")
        else:
            bot.reply_to(call.message, text, parse_mode="HTML")

    # ── /help ──
    @bot.message_handler(commands=['help'])
    def cmd_help(message):
        text = _FULL_HELP_TEXT

        if message.chat.type == 'private':
            bot.send_message(message.chat.id, text, parse_mode="HTML")
        else:
            bot.reply_to(message, "📥 Help sent to your private messages!")
            try:
                bot.send_message(message.from_user.id, text, parse_mode="HTML")
            except Exception:
                bot.reply_to(message, "⚠️ Please start me in private chat first so I can DM you the help menu.")

    # ── /info ──
    @bot.message_handler(commands=['info'])
    def cmd_info(message):
        db.ensure_user(message.from_user.id, name=message.from_user.first_name)
        target = get_target_user(message)

        if not target:
            target_user = message.from_user
        else:
            target_user = target

        user_data = db.get_user(target_user.id)
        warnings = user_data.get("warnings", 0) if user_data else 0
        role = "Member"
        status_text = "N/A"

        if message.chat.type in ['group', 'supergroup']:
            try:
                member_info = bot.get_chat_member(message.chat.id, target_user.id)
                if member_info.status == 'creator':
                    role = "👑 Owner / Creator"
                elif member_info.status == 'administrator':
                    role = "🛡️ Administrator"
                elif member_info.status == 'restricted':
                    role = "⚠️ Restricted User"
                if hasattr(target_user, 'username') and target_user.username:
                    status_text = f"@{target_user.username}"
            except Exception:
                pass

        if is_owner(getattr(target_user, 'username', None)):
            role = "⭐ Global Owner"

        text = (f"<b>👤 User Intelligence</b>\n\n"
                f"<b>📛 Name:</b> {target_user.first_name}\n"
                f"<b>🆔 ID:</b> <code>{target_user.id}</code>\n"
                f"<b>🎭 Role:</b> {role}\n"
                f"<b>🌐 Username:</b> {status_text}\n"
                f"<b>⚠️ Warnings:</b> {warnings}/3\n"
                f"<b>📅 First Seen:</b> {'Recorded' if user_data else 'New Arrival'}")
        bot.reply_to(message, text, parse_mode="HTML")

    # ── /admins ──
    @bot.message_handler(commands=['admins'])
    def cmd_admins(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        try:
            admins = bot.get_chat_administrators(message.chat.id)
            text = f"<b>🛡️ Administrators in {message.chat.title}</b>\n\n"
            for admin in admins:
                if admin.user.is_bot:
                    continue
                symbol = "👑" if admin.status == 'creator' else "🛡️"
                text += f"{symbol} {admin.user.first_name} (<code>{admin.user.id}</code>)\n"
            bot.reply_to(message, text, parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ Couldn't fetch admin list: {friendly_error(e)}")

    # ── /ban ──
    @bot.message_handler(commands=['ban'])
    def cmd_ban(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username to ban.")
        
        t_id = target.id
        t_uname = getattr(target, 'username', None)
        
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, t_uname):
            return bot.reply_to(message, "⚠️ Cannot perform this action on this user (Admin/Owner protection).")
        try:
            bot.ban_chat_member(message.chat.id, t_id)
            db.log_event(f"Admin {message.from_user.id} banned {t_id} in {message.chat.id}")
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("Click to unban", callback_data=f"unban_{t_id}"))
            bot.reply_to(message, format_alert_message("msg_after_ban", "🔨 User <b>{name}</b> (<code>{id}</code>) has been <b>banned</b>.", target), parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            bot.reply_to(message, f"❌ Ban failed: {friendly_error(e)}")

    # ── /kick ──
    @bot.message_handler(commands=['kick'])
    def cmd_kick(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username to kick.")
        
        t_id = target.id
        t_uname = getattr(target, 'username', None)
        
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, t_uname):
            return
        try:
            bot.ban_chat_member(message.chat.id, t_id)
            bot.unban_chat_member(message.chat.id, t_id)
            bot.reply_to(message, format_alert_message("msg_after_kick", "Booted 👢 User <b>{name}</b> (<code>{id}</code>) has been <b>kicked</b>.", target), parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ Kick failed: {friendly_error(e)}")

    # ── /mute ──
    @bot.message_handler(commands=['mute'])
    def cmd_mute(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        
        t_id = target.id
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return
        try:
            bot.restrict_chat_member(
                message.chat.id, t_id,
                permissions=telebot.types.ChatPermissions(can_send_messages=False)
            )
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("Click to unmute", callback_data=f"unmute_{t_id}"))
            bot.reply_to(message, format_alert_message("msg_after_mute", "🔇 User <b>{name}</b> (<code>{id}</code>) has been <b>muted</b>.", target), parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            bot.reply_to(message, str(e))

    # ── Shadow Admin Commands ──
    @bot.message_handler(commands=['s_ban'])
    def cmd_s_ban(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_shadow_admin(message.from_user.id) and not is_owner(message.from_user.username):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "⚠️ Reply to a user or provide their ID/@username to ban.")
        
        t_id = target.id
        try:
            bot.ban_chat_member(message.chat.id, t_id)
            db.log_event(f"Shadow Admin {message.from_user.id} banned {t_id} in {message.chat.id}")
            name = html.escape(str(getattr(target, 'first_name', str(t_id))))
            bot.reply_to(message, f"<b>{name}</b> banned by developer.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ Ban failed: {friendly_error(e)}")

    @bot.message_handler(commands=['s_kick'])
    def cmd_s_kick(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_shadow_admin(message.from_user.id) and not is_owner(message.from_user.username):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "⚠️ Reply to a user or provide their ID/@username to kick.")
        
        t_id = target.id
        try:
            # Kick = ban then immediately unban (user removed without a permanent ban)
            bot.ban_chat_member(message.chat.id, t_id)
            bot.unban_chat_member(message.chat.id, t_id)
            db.log_event(f"Shadow Admin {message.from_user.id} kicked {t_id} in {message.chat.id}")
            name = html.escape(str(getattr(target, 'first_name', str(t_id))))
            bot.reply_to(message, f"<b>{name}</b> kicked by developer.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ Kick failed: {friendly_error(e)}")

    @bot.message_handler(commands=['s_mute'])
    def cmd_s_mute(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_shadow_admin(message.from_user.id) and not is_owner(message.from_user.username):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "⚠️ Reply to a user or provide their ID/@username to mute.")
        
        t_id = target.id
        try:
            bot.restrict_chat_member(
                message.chat.id, t_id,
                permissions=telebot.types.ChatPermissions(can_send_messages=False)
            )
            db.log_event(f"Shadow Admin {message.from_user.id} muted {t_id} in {message.chat.id}")
            name = html.escape(str(getattr(target, 'first_name', str(t_id))))
            bot.reply_to(message, f"<b>{name}</b> muted by developer.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ Mute failed: {friendly_error(e)}")

    # ── /unmute ──
    @bot.message_handler(commands=['unmute'])
    def cmd_unmute(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        
        t_id = target.id
        try:
            bot.restrict_chat_member(
                message.chat.id, t_id,
                permissions=telebot.types.ChatPermissions(
                    can_send_messages=True, can_send_audios=True, can_send_documents=True,
                    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                    can_send_voice_notes=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True,
                )
            )
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"🔊 User <b>{name}</b> (<code>{t_id}</code>) has been <b>unmuted</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, str(e))

    # ── /unban ──
    @bot.message_handler(commands=['unban'])
    def cmd_unban(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)

        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ <b>Identify User:</b> Reply to their message, type their 🆔, or Use their 👤 @username (if seen before).", parse_mode="HTML")

        t_id = target.id
        try:
            bot.unban_chat_member(message.chat.id, t_id, only_if_banned=True)
            db.unban_user(message.chat.id, t_id)
            db.log_event(f"Admin {message.from_user.id} unbanned {t_id} in {message.chat.id}")
            # Try to get a name for better feedback
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"🕊️ User <b>{name}</b> (<code>{t_id}</code>) has been <b>unbanned</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ <b>Unban failed:</b> {friendly_error(e)}", parse_mode="HTML")

    # ── /tmute — TIME-BASED MUTE ──
    @bot.message_handler(commands=['tmute'])
    def cmd_tmute(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)

        parts = message.text.split()
        is_reply = message.reply_to_message is not None
        min_len = 2 if is_reply else 3

        if len(parts) < min_len:
            usage = (
                "💬 <b>Usage (Reply):</b> <code>/tmute 1h [reason]</code>\n"
                "💬 <b>Usage (No Reply):</b> <code>/tmute @user 1h [reason]</code>\n"
                "⏰ <b>Formats:</b> 1m, 1h, 1d, 1w, 1mn, 1y\n"
                "💡 <b>Examples:</b>\n"
                "  /tmute @user 1h spam\n"
                "  /tmute 30m duplicate posting"
            )
            return bot.reply_to(message, usage, parse_mode="HTML")

        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username to mute.")

        if is_reply:
            duration_str = parts[1]
            reason = " ".join(parts[2:]) if len(parts) > 2 else "No reason"
        else:
            duration_str = parts[2]
            reason = " ".join(parts[3:]) if len(parts) > 3 else "No reason"

        seconds, display = parse_duration(duration_str)
        if seconds is None:
            return bot.reply_to(message, f"❌ {display}")

        t_id = target.id
        name = getattr(target, 'first_name', str(t_id))

        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return bot.reply_to(message, "⚠️ Cannot perform this action on this user (Admin/Owner protection).")

        try:
            # Delete the offending message if replied to
            if is_reply:
                try:
                    bot.delete_message(message.chat.id, message.reply_to_message.message_id)
                except Exception:
                    pass

            # Mute in Telegram natively
            until_date = int(_time.time() + seconds)
            bot.restrict_chat_member(
                message.chat.id, t_id,
                until_date=until_date,
                permissions=telebot.types.ChatPermissions(can_send_messages=False)
            )

            # ✅ Send alert IMMEDIATELY — before any DB ops that could fail
            try:
                bot.send_message(message.chat.id, f"🔇 {name} has been muted for {display}.")
            except Exception as alert_err:
                logger.error(f"tmute alert send failed: {alert_err}")

            # Log in database (non-critical — failure won't prevent alert)
            try:
                db.mute_user(message.chat.id, t_id, seconds, reason, message.from_user.first_name)
                db.log_infraction(message.chat.id, t_id, "tmute", seconds, reason, message.from_user.id)
            except Exception as db_err:
                logger.error(f"tmute DB log error: {db_err}")

        except Exception as e:
            logger.error(f"tmute failed: {e}")
            try:
                bot.send_message(message.chat.id, f"❌ Mute failed: {friendly_error(e)}")
            except Exception:
                pass

    # ── /tban — TIME-BASED BAN ──
    @bot.message_handler(commands=['tban'])
    def cmd_tban(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)

        parts = message.text.split()
        is_reply = message.reply_to_message is not None
        min_len = 2 if is_reply else 3

        if len(parts) < min_len:
            usage = (
                "⛔ <b>Usage (Reply):</b> <code>/tban 1d [reason]</code>\n"
                "⛔ <b>Usage (No Reply):</b> <code>/tban @user 1d [reason]</code>\n"
                "⏰ <b>Formats:</b> 1m, 1h, 1d, 1w, 1mn, 1y\n"
                "💡 <b>Examples:</b>\n"
                "  /tban @user 1w advertising\n"
                "  /tban 12h flood"
            )
            return bot.reply_to(message, usage, parse_mode="HTML")

        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username to ban.")

        if is_reply:
            duration_str = parts[1]
            reason = " ".join(parts[2:]) if len(parts) > 2 else "No reason"
        else:
            duration_str = parts[2]
            reason = " ".join(parts[3:]) if len(parts) > 3 else "No reason"

        seconds, display = parse_duration(duration_str)
        if seconds is None:
            return bot.reply_to(message, f"❌ {display}")

        t_id = target.id
        name = getattr(target, 'first_name', str(t_id))

        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return bot.reply_to(message, "⚠️ Cannot perform this action on this user (Admin/Owner protection).")

        try:
            # Delete the offending message if replied to
            if is_reply:
                try:
                    bot.delete_message(message.chat.id, message.reply_to_message.message_id)
                except Exception:
                    pass

            # Ban in Telegram natively
            until_date = int(_time.time() + seconds)
            bot.ban_chat_member(message.chat.id, t_id, until_date=until_date)

            # ✅ Send alert IMMEDIATELY — before any DB ops that could fail
            try:
                bot.send_message(message.chat.id, f"⛔ {name} has been banned for {display}.")
            except Exception as alert_err:
                logger.error(f"tban alert send failed: {alert_err}")

            # Log in database (non-critical — failure won't prevent alert)
            try:
                db.ban_user(message.chat.id, t_id, seconds, reason, message.from_user.first_name)
                db.log_infraction(message.chat.id, t_id, "tban", seconds, reason, message.from_user.id)
            except Exception as db_err:
                logger.error(f"tban DB log error: {db_err}")

        except Exception as e:
            logger.error(f"tban failed: {e}")
            try:
                bot.send_message(message.chat.id, f"❌ Ban failed: {friendly_error(e)}")
            except Exception:
                pass


    # ── /mutelist — SHOW ACTIVE MUTES ──
    @bot.message_handler(commands=['mutelist'])
    def cmd_mutelist(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)

        mutes = db.get_mutes(message.chat.id)
        if not mutes:
            return bot.reply_to(message, "✅ No active mutes in this group.")

        text = "🔇 <b>Active Mutes:</b>\n\n"
        for m in mutes[:10]:  # Show first 10
            user_id = m.get('user_id')
            unmute_time = m.get('unmute_at', 'Unknown')
            reason = m.get('reason', 'No reason')
            text += f"👤 <code>{user_id}</code> - {reason}\n   Until: {unmute_time}\n"

        bot.reply_to(message, text, parse_mode="HTML")

    # ── /banlist — SHOW ACTIVE BANS ──
    @bot.message_handler(commands=['banlist'])
    def cmd_banlist(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)

        bans = db.get_bans(message.chat.id)
        if not bans:
            return bot.reply_to(message, "✅ No active bans in this group.")

        text = "⛔ <b>Active Bans:</b>\n\n"
        for b in bans[:10]:  # Show first 10
            user_id = b.get('user_id')
            unban_time = b.get('unban_at', 'Permanent')
            reason = b.get('reason', 'No reason')
            text += f"👤 <code>{user_id}</code> - {reason}\n   Until: {unban_time}\n"

        bot.reply_to(message, text, parse_mode="HTML")

    # ── /warn, /dwarn, /swarn ──
    @bot.message_handler(commands=['warn'])
    def cmd_warn(message):
        _warn_action(bot, message, silent=False, delete_target_message=False)

    @bot.message_handler(commands=['dwarn'])
    def cmd_dwarn(message):
        _warn_action(bot, message, silent=False, delete_target_message=True)

    @bot.message_handler(commands=['swarn'])
    def cmd_swarn(message):
        _warn_action(bot, message, silent=True, delete_target_message=True)

    # ── /rmwarn /removewarn /unwarn /delwarn /resetwarn /clearwarn ──
    @bot.message_handler(commands=['rmwarn', 'removewarn', 'unwarn', 'delwarn', 'resetwarn', 'clearwarn', 'resetwarnings'])
    def cmd_removewarn(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "Reply to a user or provide their ID/@username to remove warning.")
        t_id = target.id
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return bot.reply_to(message, "Cannot act on this user.")
        cmd_text = (message.text or "").split()[0].lower()
        cmd = cmd_text.split('@')[0].replace('/', '')
        name = getattr(target, 'first_name', 'Unknown')
        safe_name = html.escape(str(name))
        chat_id = message.chat.id
        group = db.get_group(chat_id) or {}
        limit = int(group.get("max_warnings", 3) or 3)
        warn_time = int(group.get("warn_time", 0) or 0)
        if cmd in ('resetwarn', 'clearwarn', 'resetwarnings'):
            db.warn_reset_user(t_id)
            bot.reply_to(message, f"All warnings for <b>{safe_name}</b> (<code>{t_id}</code>) have been <b>cleared</b> (0/{limit}).", parse_mode="HTML")
            db.log_event(f"Admin {message.from_user.id} cleared warnings for {t_id} in {chat_id}")
        else:
            db.warn_expire(chat_id, t_id, warn_time)
            warns_before = db.warn_list(chat_id, t_id)
            if not warns_before:
                return bot.reply_to(message, f"<b>{safe_name}</b> (<code>{t_id}</code>) has no warnings to remove.", parse_mode="HTML")
            after = db.warn_remove_one(chat_id, t_id)
            bot.reply_to(message, f"Removed 1 warning from <b>{safe_name}</b> (<code>{t_id}</code>). Now <b>{after}/{limit}</b>.", parse_mode="HTML")
            db.log_event(f"Admin {message.from_user.id} removed 1 warning for {t_id} in {chat_id}")

    # ── /warnings — view current warning configuration ──
    @bot.message_handler(commands=['warnings'])
    def cmd_warnings(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        group = db.get_group(message.chat.id) or {}
        limit = int(group.get("max_warnings", 3) or 3)
        mode = str(group.get("warn_mode") or "ban").strip().lower() or "ban"
        warn_time = int(group.get("warn_time", 0) or 0)
        bot.reply_to(message,
            f"⚠️ <b>Warnings config</b> — this group\n\n"
            f"• Limit: <b>{limit}</b> warnings\n"
            f"• Mode: <b>{html.escape(mode)}</b> (applied when the limit is reached)\n"
            f"• Expiry: <b>{_fmt_warn_time(warn_time)}</b>",
            parse_mode="HTML")

    # ── /setwarnlimit <number> ──
    @bot.message_handler(commands=['setwarnlimit'])
    def cmd_setwarnlimit(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = (message.text or "").split(None, 1)
        val = parts[1].strip() if len(parts) > 1 else ""
        try:
            num = int(val)
            if num < 1 or num > 20:
                raise ValueError()
        except ValueError:
            return bot.reply_to(message, "❌ Usage: <code>/setwarnlimit &lt;1-20&gt;</code> (default: 3).", parse_mode="HTML")
        db.update_group_setting(message.chat.id, "max_warnings", num)
        bot.reply_to(message, f"✅ Warn limit set to <b>{num}</b>. Once a user reaches it, the warn mode action will be applied.", parse_mode="HTML")

    # ── /setwarnmode <action> [TIME] ──
    @bot.message_handler(commands=['setwarnmode'])
    def cmd_setwarnmode(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        raw = (message.text or "").split()
        if len(raw) < 2:
            return bot.reply_to(message, "Usage: <code>/setwarnmode kick|ban|mute|tban TIME|tmute TIME</code>\nExamples: <code>/setwarnmode kick</code> • <code>/setwarnmode tmute 1w</code>", parse_mode="HTML")
        action = raw[1].lower()
        bare = {"kick", "ban", "mute"}
        timed = {"tban", "tmute"}
        if action in bare:
            if len(raw) > 2:
                return bot.reply_to(message, f"❌ <b>{action}</b> doesn't take a duration. Usage: <code>/setwarnmode {action}</code>", parse_mode="HTML")
            mode = action
        elif action in timed:
            if len(raw) < 3:
                return bot.reply_to(message, f"❌ Missing duration. Usage: <code>/setwarnmode {action} &lt;time&gt;</code> (e.g. 1h, 1d, 1w)", parse_mode="HTML")
            secs, display = parse_duration(raw[2])
            if secs is None:
                return bot.reply_to(message, f"❌ {display}")
            mode = f"{action} {raw[2].lower()}"
        else:
            return bot.reply_to(message, f"❌ Unknown mode '{html.escape(action)}'. Valid: kick, ban, tban TIME, mute, tmute TIME.", parse_mode="HTML")
        db.update_group_setting(message.chat.id, "warn_mode", mode)
        bot.reply_to(message, f"✅ Warn mode set to <b>{html.escape(mode)}</b>.", parse_mode="HTML")

    # ── /setwarntime <time|off> (+ /warntime alias) ──
    @bot.message_handler(commands=['setwarntime', 'warntime'])
    def cmd_setwarntime(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = (message.text or "").split()
        arg = parts[1].lower() if len(parts) > 1 else ""
        if arg in ("off", "none", "disable", "0"):
            secs = 0
            display = "disabled"
        elif arg:
            secs, display = parse_duration(arg)
            if secs is None:
                return bot.reply_to(message, f"❌ {display}")
        else:
            return bot.reply_to(message, "Usage: <code>/setwarntime &lt;time|off&gt;</code> (e.g. <code>/setwarntime 12w</code> • <code>/setwarntime off</code>)", parse_mode="HTML")
        db.update_group_setting(message.chat.id, "warn_time", secs)
        bot.reply_to(message, f"✅ Warning expiry set to <b>{_fmt_warn_time(secs)}</b>.", parse_mode="HTML")

    # ── /warns — view warnings for a user ──
    @bot.message_handler(commands=['warns'])
    def cmd_warns(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        t_id = target.id
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return bot.reply_to(message, "Cannot view warnings for this user.")
        name = getattr(target, 'first_name', 'Unknown')
        safe_name = html.escape(str(name))
        group = db.get_group(message.chat.id) or {}
        limit = int(group.get("max_warnings", 3) or 3)
        warn_time = int(group.get("warn_time", 0) or 0)
        db.warn_expire(message.chat.id, t_id, warn_time)
        warns = db.warn_list(message.chat.id, t_id)
        if not warns:
            return bot.reply_to(message, f"✅ <b>{safe_name}</b> (<code>{t_id}</code>) has no active warnings.", parse_mode="HTML")
        from datetime import datetime, timezone
        lines = [f"⚠️ <b>Warns for {safe_name}</b> (<code>{t_id}</code>) — <b>{len(warns)}/{limit}</b>\n"]
        for i, w in enumerate(warns, 1):
            ts = int(w.get('issued_at') or 0)
            when = ""
            if ts > 0:
                when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
            reason = w.get('reason')
            safe_reason = html.escape(str(reason)) if reason else "No reason"
            lines.append(f"{i}. <i>{safe_reason}</i>{' — ' + when if when else ''}")
        bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

    # ── /warn — "Remove warn" button callback ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("warn_remove:"))
    def handle_warn_remove(call):
        try:
            _, chat_id_s, user_id_s = call.data.split(':', 2)
            chat_id, t_id = int(chat_id_s), int(user_id_s)
        except Exception:
            return bot.answer_callback_query(call.id, "Invalid data")
        if int(call.message.chat.id) != int(chat_id):
            return bot.answer_callback_query(call.id, "Not allowed for this group")
        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username):
            return reply_not_admin(bot, call)
        group = db.get_group(chat_id) or {}
        limit = int(group.get("max_warnings", 3) or 3)
        warn_time = int(group.get("warn_time", 0) or 0)
        db.warn_expire(chat_id, t_id, warn_time)
        after = db.warn_remove_one(chat_id, t_id)
        user_data = db.get_user(t_id)
        name = (user_data or {}).get('name') or str(t_id)
        new_text = f"⚠️ User <b>{html.escape(str(name))}</b> (<code>{t_id}</code>) — warnings updated: <b>{after}/{limit}</b>."
        reply_markup = None
        if after > 0:
            mk = InlineKeyboardMarkup()
            mk.add(InlineKeyboardButton("❌ Remove warn", callback_data=f"warn_remove:{chat_id}:{t_id}"))
            reply_markup = mk
        try:
            bot.edit_message_text(new_text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            pass
        db.log_event(f"Admin {call.from_user.id} removed 1 warning for {t_id} in {chat_id}")
        bot.answer_callback_query(call.id, f"✅ Removed 1 warning — {after}/{limit}")

    # ── /keywordalert — Keyword Alert toggle ──
    def build_keyword_alert_markup(chat_id, is_on):
        mk = InlineKeyboardMarkup()
        mk.row(
            InlineKeyboardButton("✅ ON" if is_on else "🔘 ON", callback_data=f"keywordalert:on:{chat_id}"),
            InlineKeyboardButton("🔘 OFF" if is_on else "❌ OFF", callback_data=f"keywordalert:off:{chat_id}")
        )
        return mk

    @bot.message_handler(commands=['keywordalert', 'keyword_alert'])
    def cmd_keywordalert(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        group = db.get_group(message.chat.id)
        is_on = bool(group.get("keyword_alert", 0))
        words = group.get("keyword_alert_words", "@admin,admin,help,support") or "@admin,admin,help,support"
        markup = build_keyword_alert_markup(message.chat.id, is_on)
        text = f"🚨 <b>Keyword Alert</b>\n\nStatus: {'✅ ON' if is_on else '❌ OFF'}\nKeywords: <code>{html.escape(str(words))}</code>\n\nUse <code>/setkeywords word1,word2,word3</code> to set keywords.\nExample: <code>/setkeywords @admin,help,support,urgent</code>"
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")

    @bot.message_handler(commands=['setkeywords', 'setkeyword', 'set_keyword'])
    def cmd_setkeywords(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            return bot.reply_to(message, "Usage: <code>/setkeywords word1,word2,word3</code>\nExample: <code>/setkeywords @admin,help,urgent</code>", parse_mode="HTML")
        words = parts[1].strip()
        # Basic validation: 1-20 keywords, each 1-30 chars
        keywords = [w.strip() for w in words.split(",") if w.strip()]
        if not keywords:
            return bot.reply_to(message, "No valid keywords found.")
        if len(keywords) > 20:
            return bot.reply_to(message, "Too many keywords (max 20).")
        clean = ",".join(keywords[:20])
        db.update_group_setting(message.chat.id, "keyword_alert_words", clean)
        db.log_event(f"🚨 Keywords set for {message.chat.id}: {clean}")
        bot.reply_to(message, f"✅ Keywords updated: <code>{html.escape(clean)}</code>", parse_mode="HTML")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("keywordalert:"))
    def keywordalert_callback(call):
        try:
            _, action, chat_id = call.data.split(":", 2)
            chat_id = int(chat_id)
        except:
            return bot.answer_callback_query(call.id, "Invalid data")
        if int(call.message.chat.id) != int(chat_id):
            return bot.answer_callback_query(call.id, "Not allowed for this group")
        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username):
            return reply_not_admin(bot, call)
        new_status = 1 if action == "on" else 0
        db.update_group_setting(chat_id, "keyword_alert", new_status)
        is_on = bool(new_status)
        words = db.get_group(chat_id).get("keyword_alert_words", "@admin,admin,help,support") or "@admin,admin,help,support"
        markup = build_keyword_alert_markup(chat_id, is_on)
        text = f"🚨 <b>Keyword Alert</b>\n\nStatus: {'✅ ON' if is_on else '❌ OFF'}\nKeywords: <code>{html.escape(str(words))}</code>\n\nUse <code>/setkeywords word1,word2,word3</code> to set keywords."
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except:
            pass
        bot.answer_callback_query(call.id, f"Keyword Alert {'Enabled' if is_on else 'Disabled'}")

    # ── /del ──
    @bot.message_handler(commands=['del'])
    def cmd_del(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        if not message.reply_to_message:
            return bot.reply_to(message, "Reply to a message to delete it.")
        try:
            bot.delete_message(message.chat.id, message.reply_to_message.message_id)
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

    # ── /purge, /spurge — delete messages from a replied message onward ──
    @bot.message_handler(commands=['purge'])
    def cmd_purge(message):
        _purge_msg(bot, message, silent=False)

    @bot.message_handler(commands=['spurge'])
    def cmd_spurge(message):
        _purge_msg(bot, message, silent=True)

    # ── /purgefrom, /purgeto — ranged purge ──
    @bot.message_handler(commands=['purgefrom'])
    def cmd_purgefrom(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        reply = getattr(message, 'reply_to_message', None)
        if not reply:
            return bot.reply_to(message, "❌ Reply to the oldest message you want to purge from.")
        rm_user = getattr(reply, 'from_user', None)
        start_is_bot = bool(getattr(rm_user, 'is_bot', False)) if rm_user is not None else False
        with _purge_lock:
            _purge_pending[message.chat.id] = {'from_id': reply.message_id, 'start_is_bot': start_is_bot, 'cmd_id': message.message_id, 'ts': _time.time()}
        bot.reply_to(message, "📌 Ranged purge armed. Reply to the newest message you want to delete with <code>/purgeto</code>.", parse_mode="HTML")

    @bot.message_handler(commands=['purgeto'])
    def cmd_purgeto(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        reply = getattr(message, 'reply_to_message', None)
        if not reply:
            return bot.reply_to(message, "❌ Reply to the newest message you want to purge to.")
        with _purge_lock:
            pending = _purge_pending.pop(message.chat.id, None)
        if not pending:
            return bot.reply_to(message, "⚠️ No active ranged purge. Start with <code>/purgefrom</code> (reply to the oldest message).", parse_mode="HTML")
        if _time.time() - pending['ts'] > _PURGE_RANGE_TIMEOUT:
            return bot.reply_to(message, "⚠️ Ranged purge expired. Start again with <code>/purgefrom</code>.", parse_mode="HTML")
        start_id = pending['from_id']
        end_id = reply.message_id
        candidates = _purge_resolve(message.chat.id, start_id=start_id, end_id=end_id)
        bot_ids = {start_id} if pending.get('start_is_bot') else set()
        rm_user = getattr(reply, 'from_user', None)
        if rm_user is not None and getattr(rm_user, 'is_bot', False):
            bot_ids.add(end_id)
        # Delete the /purgefrom and /purgeto commands after the purge
        try: bot.delete_message(message.chat.id, pending['cmd_id'])
        except Exception: pass
        try: bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass
        deleted, skipped_bot, failed = _purge_execute(bot, message.chat.id, candidates, known_bot_ids=bot_ids)
        _purge_report(bot, message, deleted, skipped_bot, failed)

    # ── /promote ──
    @bot.message_handler(commands=['promote'])
    def cmd_promote(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        # Executor-side gate (fail closed): only the bot owner or an admin
        # holding the "Manage Administrators" right (can_promote_members) may
        # appoint admins — any other admin is rejected here, before the bot
        # performs the promotion.
        is_owner_user = is_owner(message.from_user.username)
        if not is_owner_user:
            if not can_manage_admins(bot, message.chat.id, message.from_user.id):
                if is_admin(bot, message.chat.id, message.from_user.id):
                    return bot.reply_to(message, "❌ You don't have the <b>Manage Administrators</b> permission to promote users. Ask an admin who has it (or the group owner).", parse_mode="HTML")
                return reply_not_admin(bot, message)
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        t_id = target.id
        t_uname = getattr(target, 'username', None)
        # Target-side protection: the bot owner and any existing admin/creator
        # may not be promoted again.
        target_member = None
        try:
            target_member = bot.get_chat_member(message.chat.id, t_id)
        except Exception:
            pass
        if is_owner(t_uname):
            return bot.reply_to(message, "⚠️ Cannot promote the bot owner.")
        if target_member is not None and target_member.status in ('creator', 'administrator'):
            return bot.reply_to(message, "⚠️ Cannot promote this user (already an admin or owner protected).")
        try:
            bot.promote_chat_member(message.chat.id, t_id,
                can_change_info=True, can_post_messages=True, can_edit_messages=True,
                can_delete_messages=True, can_invite_users=True, can_restrict_members=True,
                can_pin_messages=True, can_promote_members=False)
            name = html.escape(str(getattr(target, 'first_name', str(t_id))))
            bot.reply_to(message, f"⏫ User <b>{name}</b> (<code>{t_id}</code>) promoted to <b>Admin</b>!", parse_mode="HTML")
        except Exception as e:
            if "RIGHT_FORBIDDEN" in str(e):
                bot.reply_to(message, "❌ <b>RIGHT_FORBIDDEN</b>\n\nThe bot needs the <b>\"Add new admins\"</b> permission to promote users.\n\n"
                    "👤 Go to Group Settings → Administrators → select the bot → enable <b>\"Add new admins\"</b>.",
                    parse_mode="HTML")
            else:
                bot.reply_to(message, f"❌ Promote failed: {friendly_error(e)}")

    # ── /demote ──
    @bot.message_handler(commands=['demote'])
    def cmd_demote(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        # Executor-side gate (fail closed): only the bot owner or an admin
        # holding the "Manage Administrators" right may demote admins.
        is_owner_user = is_owner(message.from_user.username)
        if not is_owner_user:
            if not can_manage_admins(bot, message.chat.id, message.from_user.id):
                if is_admin(bot, message.chat.id, message.from_user.id):
                    return bot.reply_to(message, "❌ You don't have the <b>Manage Administrators</b> permission to demote users. Ask an admin who has it (or the group owner).", parse_mode="HTML")
                return reply_not_admin(bot, message)
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        t_id = target.id
        # Target-side protection: the bot owner and the group creator can never be demoted.
        if is_owner(getattr(target, 'username', None)):
            return bot.reply_to(message, "⚠️ Cannot demote the bot owner.")
        target_member = None
        try:
            target_member = bot.get_chat_member(message.chat.id, t_id)
        except Exception:
            pass
        if target_member is not None and target_member.status == 'creator':
            return bot.reply_to(message, "⚠️ Cannot demote the group owner.")
        if target_member is not None and target_member.status != 'administrator':
            return bot.reply_to(message, "⚠️ This user is not an admin.")
        try:
            bot.promote_chat_member(message.chat.id, t_id,
                can_change_info=False, can_post_messages=False, can_edit_messages=False,
                can_delete_messages=False, can_invite_users=False, can_restrict_members=False,
                can_pin_messages=False, can_promote_members=False)
            name = html.escape(str(getattr(target, 'first_name', str(t_id))))
            bot.reply_to(message, f"⏬ User <b>{name}</b> (<code>{t_id}</code>) has been <b>demoted</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ Demote failed: {friendly_error(e)}")

    # ── /pin ──
    @bot.message_handler(commands=['pin'])
    def cmd_pin(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        if not message.reply_to_message:
            return bot.reply_to(message, "Reply to a message to pin it.")
        try:
            bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
            bot.reply_to(message, "📌 Message pinned!")
        except Exception as e:
            bot.reply_to(message, str(e))

    # ── /unpin ──
    @bot.message_handler(commands=['unpin'])
    def cmd_unpin(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        try:
            bot.unpin_all_chat_messages(message.chat.id)
            bot.reply_to(message, "📌 All messages unpinned!")
        except Exception as e:
            bot.reply_to(message, str(e))

    # ── /report — reply to a message to notify all admins ──
    @bot.message_handler(commands=['report'])
    def cmd_report(message):
        _report_notify(bot, message)

    # ── @admin — same as /report (reply with '@admin') ──
    @bot.message_handler(func=lambda m: bool(getattr(m, 'text', None))
                                       and (m.text or "").strip().lower().split()[:1] == ["@admin"])
    def cmd_report_at_admin(message):
        _report_notify(bot, message)

    # ── /reports on|off — enable/disable user reports ──
    @bot.message_handler(commands=['reports'])
    def cmd_reports(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = (message.text or "").split(None, 1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        toggles = {
            "on": 1, "true": 1, "yes": 1, "enable": 1, "enabled": 1,
            "off": 0, "false": 0, "no": 0, "disable": 0, "disabled": 0,
        }
        group = db.get_group(message.chat.id) or {}
        current = group.get("reports", 1)
        if arg in toggles:
            new_val = toggles[arg]
            db.update_group_setting(message.chat.id, "reports", new_val)
            bot.reply_to(message,
                f"✅ User reports are now <b>{'on' if new_val else 'off'}</b>.",
                parse_mode="HTML")
        elif arg in ("", "status"):
            bot.reply_to(message,
                f"ℹ️ User reports are currently <b>{'on' if current else 'off'}</b>.\n"
                "Usage: <code>/reports on</code> or <code>/reports off</code>",
                parse_mode="HTML")
        else:
            return bot.reply_to(message, "Usage: <code>/reports on</code> or <code>/reports off</code>", parse_mode="HTML")

    # ── /setwelcome — UPGRADED (reply-to-set: text / photo+caption / GIF+caption) ──
    @bot.message_handler(commands=['setwelcome'])
    def cmd_setwelcome(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)

        # ── Reply-based method ──
        if message.reply_to_message:
            m = message.reply_to_message
            if m.photo:
                file_id = m.photo[-1].file_id
                caption = m.caption or "Welcome, {name}! 👋"
                db.update_group_setting(message.chat.id, "welcome_type", "photo")
                db.update_group_setting(message.chat.id, "welcome_file_id", file_id)
                db.update_group_setting(message.chat.id, "welcome_message", caption)
                bot.reply_to(message,
                    "✅ <b>Welcome image set!</b>\n\n"
                    "New members will be greeted with your photo + caption.\n"
                    "<i>Use {name} or {id} for personalization.</i>",
                    parse_mode="HTML")
                return

            elif m.animation:
                file_id = m.animation.file_id
                caption = m.caption or "Welcome, {name}! 👋"
                db.update_group_setting(message.chat.id, "welcome_type", "gif")
                db.update_group_setting(message.chat.id, "welcome_file_id", file_id)
                db.update_group_setting(message.chat.id, "welcome_message", caption)
                bot.reply_to(message,
                    "✅ <b>Welcome GIF set!</b>\n\n"
                    "New members will be greeted with your animated GIF + caption.\n"
                    "<i>Use {name} or {id} for personalization.</i>",
                    parse_mode="HTML")
                return

            elif m.text:
                db.update_group_setting(message.chat.id, "welcome_type", "text")
                db.update_group_setting(message.chat.id, "welcome_file_id", "")
                db.update_group_setting(message.chat.id, "welcome_message", m.text)
                bot.reply_to(message,
                    "✅ <b>Welcome text set!</b>\n"
                    "<i>Use {name} or {id} for personalization.</i>",
                    parse_mode="HTML")
                return
            else:
                bot.reply_to(message,
                    "❌ Unsupported type. Reply to a <b>text</b>, <b>photo+caption</b>, or <b>GIF+caption</b>.",
                    parse_mode="HTML")
                return

        # ── Inline text method: /setwelcome <text> ──
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            db.update_group_setting(message.chat.id, "welcome_type", "text")
            db.update_group_setting(message.chat.id, "welcome_file_id", "")
            db.update_group_setting(message.chat.id, "welcome_message", parts[1])
            bot.reply_to(message,
                "✅ <b>Welcome message updated!</b>\n<i>Use {name} or {id} to personalize.</i>",
                parse_mode="HTML")
        # ── Show current setting ──
        group_data = db.get_group(message.chat.id)
        current_msg = group_data.get("welcome_message", "Welcome, {name}! 👋")
        current_type = group_data.get("welcome_type", "text")
        
        bot.reply_to(message,
            f"💡 <b>Current Welcome Message ({current_type}):</b>\n\n"
            f"<code>{html.escape(current_msg)}</code>\n\n"
            "📌 <b>How to set:</b>\n"
            "1️⃣ <b>Text:</b> <code>/setwelcome Hello {name}!</code>\n"
            "2️⃣ <b>Image + Caption:</b> Send a photo, then <b>reply</b> with <code>/setwelcome</code>\n"
            "3️⃣ <b>GIF + Caption:</b> Send a GIF, then <b>reply</b> with <code>/setwelcome</code>\n\n"
            "📌 <b>Placeholders:</b>\n"
            "• <code>{name}</code> - Full name\n"
            "• <code>{username}</code> - @username\n"
            "• <code>{date}</code> - Today's date\n"
            "• <code>{group}</code> - Group title\n"
            "• <code>{id}</code> - User ID",
            parse_mode="HTML")


    # ── /setleave ──
    @bot.message_handler(commands=['setleave'])
    def cmd_setleave(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)

        # ── Reply method for photos/GIFs/text ──
        if message.reply_to_message:
            m = message.reply_to_message
            if m.photo:
                file_id = m.photo[-1].file_id
                caption = m.caption or "Goodbye, {name}!"
                db.update_group_setting(message.chat.id, "leave_type", "photo")
                db.update_group_setting(message.chat.id, "leave_file_id", file_id)
                db.update_group_setting(message.chat.id, "leave_message", caption)
                bot.reply_to(message, "✅ <b>Leave photo set!</b>\n<i>Use {name} or {id} for personalization.</i>", parse_mode="HTML")
                return

            elif m.animation:
                file_id = m.animation.file_id
                caption = m.caption or "Goodbye, {name}!"
                db.update_group_setting(message.chat.id, "leave_type", "gif")
                db.update_group_setting(message.chat.id, "leave_file_id", file_id)
                db.update_group_setting(message.chat.id, "leave_message", caption)
                bot.reply_to(message, "✅ <b>Leave GIF set!</b>\n<i>Use {name} or {id} for personalization.</i>", parse_mode="HTML")
                return

            elif m.text:
                db.update_group_setting(message.chat.id, "leave_type", "text")
                db.update_group_setting(message.chat.id, "leave_file_id", "")
                db.update_group_setting(message.chat.id, "leave_message", m.text)
                bot.reply_to(message, "✅ <b>Leave text set!</b>\n<i>Use {name} or {id} for personalization.</i>", parse_mode="HTML")
                return
            else:
                bot.reply_to(message, "❌ Unsupported type. Reply to a <b>text</b>, <b>photo+caption</b>, or <b>GIF+caption</b>.", parse_mode="HTML")
                return

        # ── Inline text method: /setleave <text> ──
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            db.update_group_setting(message.chat.id, "leave_type", "text")
            db.update_group_setting(message.chat.id, "leave_file_id", "")
            db.update_group_setting(message.chat.id, "leave_message", parts[1])
            bot.reply_to(message, "✅ <b>Leave message updated!</b>\n<i>Use {name} or {id} to personalize.</i>", parse_mode="HTML")
        # ── Show current setting ──
        group_data = db.get_group(message.chat.id)
        current_msg = group_data.get("leave_message", "Goodbye {name}!")
        current_type = group_data.get("leave_type", "text")

        bot.reply_to(message,
            f"💡 <b>Current Leave Message ({current_type}):</b>\n\n"
            f"<code>{html.escape(current_msg)}</code>\n\n"
            "📌 <b>How to set:</b>\n"
            "1️⃣ <b>Text:</b> <code>/setleave Goodbye {name}!</code>\n"
            "2️⃣ <b>Image + Caption:</b> Send a photo, then <b>reply</b> with <code>/setleave</code>\\n"
            "3️⃣ <b>GIF + Caption:</b> Send a GIF, then <b>reply</b> with <code>/setleave</code>\\n\\n"
            "📌 <b>Placeholders:</b>\\n"
            "• <code>{name}</code> - Full name\\n"
            "• <code>{username}</code> - @username\\n"
            "• <code>{date}</code> - Today's date\\n"
            "• <code>{group}</code> - Group title\\n"
            "• <code>{id}</code> - User ID",
            parse_mode="HTML")


    @bot.message_handler(commands=['captchamutetime'])
    def cmd_captchamutetime(message):
        if message.chat.type not in ['group', 'supergroup']: return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split()
        if len(parts) > 1:
            val = parts[1].lower()
            if val in ('off', 'none', '0'):
                db.update_group_setting(message.chat.id, "captcha_mute_time", "")
                bot.reply_to(message, "✅ CAPTCHA mute time disabled. Users stay muted indefinitely until solved.")
            else:
                secs, disp = parse_duration(val)
                if secs is None:
                    bot.reply_to(message, f"❌ {disp}")
                else:
                    db.update_group_setting(message.chat.id, "captcha_mute_time", disp)
                    group = db.get_group(message.chat.id)
                    panel_text, markup = build_captcha_panel(message.chat.id, group)
                    bot.reply_to(message, f"✅ Auto-unmute time set to <b>{disp}</b>.\n\n" + panel_text, reply_markup=markup, parse_mode="HTML")
        else:
            group = db.get_group(message.chat.id)
            panel_text, markup = build_captcha_panel(message.chat.id, group)
            bot.reply_to(message, panel_text, reply_markup=markup, parse_mode="HTML")

    @bot.message_handler(commands=['captchakicktime'])
    def cmd_captchakicktime(message):
        if message.chat.type not in ['group', 'supergroup']: return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split()
        if len(parts) > 1:
            val = parts[1].lower()
            if val in ('off', 'none', '0'):
                db.update_group_setting(message.chat.id, "captcha_kick_time", "")
                bot.reply_to(message, "✅ CAPTCHA kick time disabled.")
            else:
                secs, disp = parse_duration(val)
                if secs is None:
                    bot.reply_to(message, f"❌ {disp}")
                else:
                    db.update_group_setting(message.chat.id, "captcha_kick_time", disp)
                    group = db.get_group(message.chat.id)
                    panel_text, markup = build_captcha_panel(message.chat.id, group)
                    bot.reply_to(message, f"✅ Auto-kick time set to <b>{disp}</b>.\n\n" + panel_text, reply_markup=markup, parse_mode="HTML")
        else:
            group = db.get_group(message.chat.id)
            panel_text, markup = build_captcha_panel(message.chat.id, group)
            bot.reply_to(message, panel_text, reply_markup=markup, parse_mode="HTML")

    @bot.message_handler(commands=['captcha'])
    def cmd_captcha(message):
        if message.chat.type not in ['group', 'supergroup']: return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split()
        if len(parts) > 1:
            val = parts[1].lower() in ('yes', 'on', 'true', '1')
            db.update_group_setting(message.chat.id, "captcha", int(val))
        group = db.get_group(message.chat.id)
        panel_text, markup = build_captcha_panel(message.chat.id, group)
        bot.reply_to(message, panel_text, reply_markup=markup, parse_mode="HTML")


    @bot.message_handler(commands=['setcaptchatext'])
    def cmd_setcaptchatext(message):
        if message.chat.type not in ['group', 'supergroup']: return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            db.update_group_setting(message.chat.id, "captcha_text", parts[1])
            bot.reply_to(message, "✅ Custom CAPTCHA text updated.")
        else:
            group = db.get_group(message.chat.id)
            val = group.get("captcha_text", "Click to prove you are human")
            bot.reply_to(message, f"Current CAPTCHA text is: {val}\nUse /setcaptchatext <text> to change.")

    @bot.message_handler(commands=['resetcaptchatext'])
    def cmd_resetcaptchatext(message):
        if message.chat.type not in ['group', 'supergroup']: return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        db.update_group_setting(message.chat.id, "captcha_text", "Click to prove you are human")
        bot.reply_to(message, "✅ CAPTCHA text reset to default.")

    # ── /rules ──
    @bot.message_handler(commands=['rules'])
    def cmd_rules(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        group = db.get_group(message.chat.id)
        if group:
            bot.reply_to(message, group.get("rules", "No rules set."))

    # ── /addfilter ──
    @bot.message_handler(commands=['addfilter'])
    def cmd_addfilter(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            return bot.reply_to(message, "Format: /addfilter <keyword> (reply to media/text)")
        keyword = parts[1].strip()
        if not message.reply_to_message:
            return bot.reply_to(message, "You must reply to the content you want to set as the filter response.")
        m = message.reply_to_message
        filter_data = {}
        if m.text:
            filter_data = {"type": "text", "text": m.text}
        elif m.photo:
            filter_data = {"type": "photo", "file_id": m.photo[-1].file_id, "caption": m.caption or ""}
        elif m.sticker:
            filter_data = {"type": "sticker", "file_id": m.sticker.file_id}
        elif m.animation:
            filter_data = {"type": "gif", "file_id": m.animation.file_id}
        else:
            return bot.reply_to(message, "Unsupported media type.")
        db.add_filter(message.chat.id, keyword, filter_data)
        bot.reply_to(message, f"✅ Filter '<code>{keyword}</code>' added!", parse_mode="HTML")

    # ── /removefilter ──
    @bot.message_handler(commands=['removefilter'])
    def cmd_removefilter(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            return bot.reply_to(message, "Format: /removefilter <keyword>")
        db.remove_filter(message.chat.id, parts[1].strip())
        bot.reply_to(message, f"✅ Filter '<code>{parts[1].strip()}</code>' removed.", parse_mode="HTML")

    # ── /filters ──
    @bot.message_handler(commands=['filters'])
    def cmd_list_filters(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        group = db.get_group(message.chat.id)
        if not group or not group.get("filters"):
            return bot.reply_to(message, "No filters defined for this group.")
        fts = group["filters"].keys()
        text = "<b>🔍 Active Group Filters:</b>\n\n" + "\n".join([f"• <code>{f}</code>" for f in fts])
        bot.reply_to(message, text, parse_mode="HTML")

    # ── /lock — Granular (Rose-style) ──
    LOCK_TYPES = {
        "sticker": ("lock_sticker", "Stickers"),
        "animation": ("lock_animation", "GIFs"),
        "gif": ("lock_animation", "GIFs"),
        "media": ("lock_media", "Media (photo/video/doc)"),
        "photo": ("lock_media", "Media"),
        "video": ("lock_media", "Media"),
        "document": ("lock_media", "Media"),
        "url": ("lock_url", "Links"),
        "link": ("lock_url", "Links"),
        "links": ("lock_url", "Links"),
        "forward": ("lock_forward", "Forwards"),
        "forwards": ("lock_forward", "Forwards"),
        "inline": ("lock_inline", "Inline Bots"),
        "poll": ("lock_poll", "Polls"),
        "game": ("lock_game", "Games"),
        "all": (None, "All"),
        "chat_off": ("lock_chat_off", "Chat Off"),
    }

    def build_locks_markup(chat_id, group):
        mk = InlineKeyboardMarkup()
        def btn(label, key):
            is_on = bool(group.get(key, 0))
            # Green = ON (Locked), Red = OFF (Unlocked) — clear at a glance
            text = f"{label}: {'🟢 ON' if is_on else '🔴 OFF'}"
            return InlineKeyboardButton(text, callback_data=f"lock:{key.replace('lock_','')}:{chat_id}")
        # Row 1
        mk.row(btn("Stickers", "lock_sticker"), btn("GIFs", "lock_animation"), btn("Media", "lock_media"))
        mk.row(btn("Links", "lock_url"), btn("Forwards", "lock_forward"), btn("Inline", "lock_inline"))
        mk.row(btn("Polls", "lock_poll"), btn("Games", "lock_game"))
        mk.row(
            InlineKeyboardButton("🔒 Lock All", callback_data=f"lock:all:{chat_id}"),
            InlineKeyboardButton("🔓 Unlock All", callback_data=f"lock:unlockall:{chat_id}"),
        )
        mk.row(btn("Chat Off", "lock_chat_off"))
        return mk

    def _set_lock(chat_id, lock_key, value):
        db.update_group_setting(chat_id, lock_key, 1 if value else 0)
        db.log_event(f"🔒 Lock {lock_key} set to {'ON' if value else 'OFF'} for {chat_id}")

    @bot.message_handler(commands=['lock'])
    def cmd_lock(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        args = message.text.split()
        if len(args) == 1:
            # No args: show panel or do general lock (old behavior) — show panel
            group = db.get_group(message.chat.id)
            markup = build_locks_markup(message.chat.id, group)
            text = "🔒 <b>Locks Panel</b>\n\nTap a button to toggle. General lock (mute all) is separate: use <code>/lock all</code> to lock entire group."
            return bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")
        typ = args[1].lower()
        if typ not in LOCK_TYPES:
            return bot.reply_to(message, f"Unknown lock type <b>{html.escape(typ)}</b>.\nUse: <code>/lock sticker|gif|media|url|forward|inline|poll|game|chat_off|all</code>\nOr just <code>/lock</code> for panel.", parse_mode="HTML")
        lock_key, label = LOCK_TYPES[typ]
        if typ == "all":
            # General lock: mute all via permissions + set all granular locks ON
            try:
                bot.set_chat_permissions(message.chat.id, telebot.types.ChatPermissions(can_send_messages=False))
            except Exception as e:
                return bot.reply_to(message, f"❌ Lock failed: {friendly_error(e)}")
            for k in ["lock_sticker","lock_animation","lock_media","lock_url","lock_forward","lock_inline","lock_poll","lock_game"]:
                _set_lock(message.chat.id, k, True)
            return bot.reply_to(message, "🔒 <b>Group locked</b> — all messages and all types restricted.", parse_mode="HTML")
        # Granular lock
        _set_lock(message.chat.id, lock_key, True)
        bot.reply_to(message, f"🔒 <b>{label} locked</b> in this group.", parse_mode="HTML")

    @bot.message_handler(commands=['unlock'])
    def cmd_unlock(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        args = message.text.split()
        if len(args) == 1:
            # No args: general unlock
            try:
                bot.set_chat_permissions(message.chat.id, telebot.types.ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True,
                    can_send_other_messages=True, can_add_web_page_previews=True
                ))
            except Exception as e:
                return bot.reply_to(message, f"❌ Unlock failed: {friendly_error(e)}")
            # Also unlock all granular
            for k in ["lock_sticker","lock_animation","lock_media","lock_url","lock_forward","lock_inline","lock_poll","lock_game"]:
                _set_lock(message.chat.id, k, False)
            return bot.reply_to(message, "🔓 <b>Group unlocked</b> — all restrictions lifted.", parse_mode="HTML")
        typ = args[1].lower()
        if typ not in LOCK_TYPES:
            return bot.reply_to(message, f"Unknown unlock type <b>{html.escape(typ)}</b>.", parse_mode="HTML")
        if typ == "all":
            try:
                bot.set_chat_permissions(message.chat.id, telebot.types.ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True,
                    can_send_other_messages=True, can_add_web_page_previews=True
                ))
            except Exception as e:
                return bot.reply_to(message, f"❌ Unlock failed: {friendly_error(e)}")
            for k in ["lock_sticker","lock_animation","lock_media","lock_url","lock_forward","lock_inline","lock_poll","lock_game"]:
                _set_lock(message.chat.id, k, False)
            return bot.reply_to(message, "🔓 <b>Group unlocked</b> — all restrictions lifted.", parse_mode="HTML")
        lock_key, label = LOCK_TYPES[typ]
        _set_lock(message.chat.id, lock_key, False)
        bot.reply_to(message, f"🔓 <b>{label} unlocked</b> in this group.", parse_mode="HTML")

    @bot.message_handler(commands=['locks'])
    def cmd_locks(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        group = db.get_group(message.chat.id)
        markup = build_locks_markup(message.chat.id, group)
        text = "🔒 <b>Locks Status</b>\n\n" + "\n".join([f"{'🔒' if group.get(k) else '🔓'} {label}" for k, label in [v for v in LOCK_TYPES.values() if v[0]]]) + "\n\nTap to toggle."
        # Deduplicate labels
        seen = set()
        lines = []
        for k, label in LOCK_TYPES.values():
            if k and k not in seen:
                seen.add(k)
                lines.append(f"{'🔒' if group.get(k) else '🔓'} {label}: {'ON' if group.get(k) else 'OFF'}")
        text = "🔒 <b>Locks Status</b>\n\n" + "\n".join(lines) + "\n\nTap a button to toggle."
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("lock:"))
    def locks_callback(call):
        try:
            _, typ, chat_id = call.data.split(":", 2)
            chat_id = int(chat_id)
        except:
            return bot.answer_callback_query(call.id, "Invalid data")
        if int(call.message.chat.id) != int(chat_id):
            return bot.answer_callback_query(call.id, "Not allowed for this group")
        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username):
            return reply_not_admin(bot, call)
        group = db.get_group(chat_id)
        if typ == "all":
            for k in ["lock_sticker","lock_animation","lock_media","lock_url","lock_forward","lock_inline","lock_poll","lock_game"]:
                _set_lock(chat_id, k, True)
            try:
                bot.set_chat_permissions(chat_id, telebot.types.ChatPermissions(can_send_messages=False))
            except:
                pass
            bot.answer_callback_query(call.id, "All locked")
        elif typ == "chat_off":
            current = bool(group.get("lock_chat_off", 0))
            _set_lock(chat_id, "lock_chat_off", not current)
            if not current:
                try:
                    bot.set_chat_permissions(chat_id, telebot.types.ChatPermissions(can_send_messages=False))
                except:
                    pass
                for k in ["lock_sticker","lock_animation","lock_media","lock_url","lock_forward","lock_inline","lock_poll","lock_game"]:
                    _set_lock(chat_id, k, True)
                bot.answer_callback_query(call.id, "🔒 Chat Off — All restrictions enabled")
            else:
                try:
                    bot.set_chat_permissions(chat_id, telebot.types.ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True))
                except:
                    pass
                bot.answer_callback_query(call.id, "🔓 Chat On — Chat restored")
        elif typ == "unlockall":
            for k in ["lock_sticker","lock_animation","lock_media","lock_url","lock_forward","lock_inline","lock_poll","lock_game"]:
                _set_lock(chat_id, k, False)
            try:
                bot.set_chat_permissions(chat_id, telebot.types.ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True))
            except:
                pass
            bot.answer_callback_query(call.id, "All unlocked")
        else:
            # Toggle single
            lock_key, label = LOCK_TYPES.get(typ, (None, typ))
            if not lock_key:
                return bot.answer_callback_query(call.id, "Unknown type")
            current = bool(group.get(lock_key, 0))
            _set_lock(chat_id, lock_key, not current)
            bot.answer_callback_query(call.id, f"{label} {'locked' if not current else 'unlocked'}")
        # Refresh markup
        group = db.get_group(chat_id)
        markup = build_locks_markup(chat_id, group)
        seen = set()
        lines = []
        for k, label in LOCK_TYPES.values():
            if k and k not in seen:
                seen.add(k)
                lines.append(f"{'🔒' if group.get(k) else '🔓'} {label}: {'ON' if group.get(k) else 'OFF'}")
        text = "🔒 <b>Locks Status</b>\n\n" + "\n".join(lines) + "\n\nTap a button to toggle."
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except:
            pass

    # ── /promote ──
    # ── /promote ──

    # ── /demote ──

    # ── /pin ──

    # ── /unpin ──

    # ── /setwelcome — UPGRADED (reply-to-set: text / photo+caption / GIF+caption) ──


    # ── /setleave ──








    # ── /rules ──

    # ── /addfilter ──

    # ── /removefilter ──

    # ── /filters ──

    # ── /lock ──

    # ── /unlock ──

    # ── /link ──
    @bot.message_handler(commands=['link'])
    def cmd_link(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        try:
            invite_link = bot.export_chat_invite_link(message.chat.id)
            bot.reply_to(message, f"🔗 <b>Invite Link:</b>\n{invite_link}", parse_mode="HTML")
        except Exception:
            bot.reply_to(message, "⚠️ Failed to fetch link. Ensure I have the 'Invite Users' admin permission.")

    # ── /addbadword ──
    @bot.message_handler(commands=['addbadword'])
    def cmd_addbadword(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            word = parts[1].strip().lower()
            group = db.get_group(message.chat.id)
            bad_words = group.get("bad_words", [])
            if word not in bad_words:
                bad_words.append(word)
                db.update_group_setting(message.chat.id, "bad_words", bad_words)
            bot.reply_to(message, f"🔇 <b>Auto-Mod:</b> Added '<code>{word}</code>' to the filter list.", parse_mode="HTML")
        else:
            bot.reply_to(message, "Format: /addbadword <word>")

    # ── /delbadword ──
    @bot.message_handler(commands=['delbadword'])
    def cmd_delbadword(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            word = parts[1].strip().lower()
            group = db.get_group(message.chat.id)
            bad_words = group.get("bad_words", [])
            if word in bad_words:
                bad_words.remove(word)
                db.update_group_setting(message.chat.id, "bad_words", bad_words)
                bot.reply_to(message, f"✅ Removed '<code>{word}</code>' from the filter.", parse_mode="HTML")
            else:
                bot.reply_to(message, "That word is not in the filter.")
        else:
            bot.reply_to(message, "Format: /delbadword <word>")

    # ── /approve ──
    @bot.message_handler(commands=['approve'])
    def cmd_approve(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        group = db.get_group(message.chat.id)
        current = group.get("approve_mode", False) if group else False
        status = "🟢 <b>ACTIVE</b>" if current else "🔴 <b>INACTIVE</b>"
        markup = build_approve_markup(message.chat.id, current)
        text = (f"🛡️ <b>Approval Gate Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>When ON:</b>\n"
                f"• New joining users are immediately restricted (muted).\n"
                f"• Admins are tagged with an interactive Approve/Decline menu.\n"
                f"• User remains muted until manual approval.\n\n"
                f"<i>Toggle status below:</i>")
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")

    # ── /antispam — UPGRADED with inline buttons ──
    @bot.message_handler(commands=['antispam'])
    def cmd_antispam(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        group = db.get_group(message.chat.id)
        current = group.get("antispam", False) if group else False
        status = "🟢 <b>ACTIVE</b>" if current else "🔴 <b>INACTIVE</b>"
        markup = build_antispam_markup(message.chat.id, current)
        text = (f"🛡️ <b>Anti-Spam Control Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>Protections enabled when ON:</b>\n"
                f"• 🔗 Block Telegram invite links\n"
                f"• ⚡ Rate limiting (max {_SPAM_MAX} msgs / {int(_SPAM_WINDOW)}s)\n"
                f"• Violations: warn → 3 warns → auto-ban\n\n"
                f"<i>Use the buttons below to toggle.</i>")
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")

    # ── Antispam inline callback ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("antispam:"))
    def antispam_toggle_callback(call):
        try:
            _, action, chat_id_str = call.data.split(":")
            chat_id = int(chat_id_str)
        except (ValueError, AttributeError):
            bot.answer_callback_query(call.id, "❌ Invalid data")
            return

        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username): return reply_not_admin(bot, call)

        new_status = 1 if action == "on" else 0
        db.update_group_setting(chat_id, "antispam", new_status)
        db.log_event(f"🛡️ Anti-Spam {'ON' if new_status else 'OFF'} in chat {chat_id} by {call.from_user.first_name}")

        status = "🟢 <b>ACTIVE</b>" if new_status else "🔴 <b>INACTIVE</b>"
        markup = build_antispam_markup(chat_id, bool(new_status))
        text = (f"🛡️ <b>Anti-Spam Control Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>Protections enabled when ON:</b>\n"
                f"• 🔗 Block Telegram invite links\n"
                f"• ⚡ Rate limiting (max {_SPAM_MAX} msgs / {int(_SPAM_WINDOW)}s)\n"
                f"• Violations: warn → 3 warns → auto-ban\n\n"
                f"<i>Use the buttons below to toggle.</i>")
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"Anti-Spam {'Enabled ✅' if new_status else 'Disabled ❌'}")

    # ── /antilink ──
    @bot.message_handler(commands=['antilink'])
    def cmd_antilink(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        group = db.get_group(message.chat.id)
        current = group.get("antispam_auto_delete_links", False) if group else False
        status = "🟢 <b>ACTIVE</b>" if current else "🔴 <b>INACTIVE</b>"
        markup = build_antilink_markup(message.chat.id, current)
        text = (f"🔗 <b>Anti-Link Control Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>Protections enabled when ON:</b>\n"
                f"• 🚫 Block all links (hyperlinks, URLs, invite links)\n"
                f"• Deletes matching messages instantly\n\n"
                f"<i>Use the buttons below to toggle.</i>")
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")

    # ── Antilink inline callback ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("antilink:"))
    def antilink_toggle_callback(call):
        try:
            _, action, chat_id_str = call.data.split(":")
            chat_id = int(chat_id_str)
        except (ValueError, AttributeError):
            bot.answer_callback_query(call.id, "❌ Invalid data")
            return

        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username): return reply_not_admin(bot, call)

        new_status = (action == "on")
        db.update_group_setting(chat_id, "antispam_auto_delete_links", 1 if new_status else 0)
        db.log_event(f"🔗 Anti-Link {'ON' if new_status else 'OFF'} in chat {chat_id} by {call.from_user.first_name}")

        status = "🟢 <b>ACTIVE</b>" if new_status else "🔴 <b>INACTIVE</b>"
        markup = build_antilink_markup(chat_id, new_status)
        text = (f"🔗 <b>Anti-Link Control Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>Protections enabled when ON:</b>\n"
                f"• 🚫 Block all links (hyperlinks, URLs, invite links)\n"
                f"• Deletes matching messages instantly\n\n"
                f"<i>Use the buttons below to toggle.</i>")
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"Anti-Link {'Enabled ✅' if new_status else 'Disabled ❌'}")

    # ── Approve Mode Toggle Inline Callbacks ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("approve_toggle:"))
    def approve_toggle_callback(call):
        try:
            _, action, chat_id_str = call.data.split(":")
            chat_id = int(chat_id_str)
        except Exception:
            bot.answer_callback_query(call.id, "❌ Invalid data")
            return

        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username): return reply_not_admin(bot, call)

        new_status = (action == "on")
        db.update_group_setting(chat_id, "approve_mode", new_status)
        db.log_event(f"🛡️ Approval Mode toggled {'ON' if new_status else 'OFF'} in {chat_id}")

        status = "🟢 <b>ACTIVE</b>" if new_status else "🔴 <b>INACTIVE</b>"
        markup = build_approve_markup(chat_id, new_status)
        text = (f"🛡️ <b>Approval Gate Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>When ON:</b>\n"
                f"• New joining users are immediately restricted (muted).\n"
                f"• Admins are tagged with an interactive Approve/Decline menu.\n"
                f"• User remains muted until manual approval.\n\n"
                f"<i>Toggle status below:</i>")
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"Approval Gate {'Enabled ✅' if new_status else 'Disabled ❌'}")

    # ── Approval Member Accept/Decline Inline Callbacks ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("approve_user:") or call.data.startswith("decline_user:"))
    def approval_callback(call):
        try:
            action, chat_id_str, user_id_str = call.data.split(":")
            chat_id = int(chat_id_str)
            target_user_id = int(user_id_str)
        except Exception:
            bot.answer_callback_query(call.id, "❌ Invalid data")
            return

        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username): return reply_not_admin(bot, call)

        if action == "approve_user":
            db.approve_user(chat_id, target_user_id, approved_by=call.from_user.first_name)
            try:
                bot.restrict_chat_member(
                    chat_id, target_user_id,
                    permissions=telebot.types.ChatPermissions(
                        can_send_messages=True, can_send_media_messages=True,
                        can_send_other_messages=True, can_add_web_page_previews=True
                    )
                )
                bot.edit_message_text(
                    chat_id=chat_id, message_id=call.message.message_id,
                    text=f"✅ User ID <code>{target_user_id}</code> was <b>APPROVED</b> by {call.from_user.first_name}.",
                    parse_mode="HTML"
                )
                # Send a notification to the user letting them know they can chat
                bot.send_message(
                    chat_id, 
                    f"🎉 <a href='tg://user?id={target_user_id}'>User</a>, you have been <b>approved</b> by an admin! You can now participate in the chat.", 
                    parse_mode="HTML"
                )
            except Exception as e:
                bot.answer_callback_query(call.id, f"Failed: {friendly_error(e)}")
                return
            bot.answer_callback_query(call.id, "User Approved ✅")
        elif action == "decline_user":
            try:
                bot.ban_chat_member(chat_id, target_user_id)
                bot.edit_message_text(
                    chat_id=chat_id, message_id=call.message.message_id,
                    text=f"❌ User ID <code>{target_user_id}</code> was <b>DECLINED</b> by {call.from_user.first_name} and has been banned.",
                    parse_mode="HTML"
                )
            except Exception as e:
                bot.answer_callback_query(call.id, f"Ban failed: {friendly_error(e)}")
                return
            bot.answer_callback_query(call.id, "User Declined ❌")

    # ── /id ──
    @bot.message_handler(commands=['id'])
    def cmd_id(message):
        target = get_target_user(message)
        msgs = [f"💬 <b>Chat ID:</b> <code>{message.chat.id}</code>"]
        
        user_id = target.id if target else message.from_user.id
        user_name = getattr(target, 'first_name', '') if target else message.from_user.first_name
        msgs.append(f"👤 <b>User ID ({user_name}):</b> <code>{user_id}</code>")
        
        bot.reply_to(message, "\n".join(msgs), parse_mode="HTML")

    # ── /settitle ──
    @bot.message_handler(commands=['settitle'])
    def cmd_settitle(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            try:
                bot.set_chat_title(message.chat.id, parts[1])
                bot.reply_to(message, "✅ <b>Group Title Updated!</b>", parse_mode="HTML")
            except Exception:
                bot.reply_to(message, "⚠️ Failed. Ensure I have 'Change Group Info' admin rights.")
        else:
            bot.reply_to(message, "Format: /settitle <New Title>")

    # ── /setdesc ──
    @bot.message_handler(commands=['setdesc'])
    def cmd_setdesc(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            try:
                bot.set_chat_description(message.chat.id, parts[1])
                bot.reply_to(message, "✅ <b>Group Description Updated!</b>", parse_mode="HTML")
            except Exception:
                bot.reply_to(message, "⚠️ Failed. Ensure I have 'Change Group Info' admin rights.")
        else:
            bot.reply_to(message, "Format: /setdesc <New Description>")

    # ── /send ──
    @bot.message_handler(commands=['send'])
    def cmd_send_msg(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username): return reply_not_admin(bot, message)
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            return bot.reply_to(message, "Format: /send <message>")
        try:
            bot.send_message(message.chat.id, parts[1])
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass  # Missing delete permission — message already sent, ignore
        except Exception as e:
            bot.reply_to(message, f"❌ {friendly_error(e)}")

    # ── Register bot commands ──
    try:
        commands = [
            telebot.types.BotCommand("start",        "Bot introduction"),
            telebot.types.BotCommand("help",         "Full command list"),
            telebot.types.BotCommand("info",         "User profile & stats"),
            telebot.types.BotCommand("ban",          "Permanently ban a user"),
            telebot.types.BotCommand("unban",        "Unban a user / revoke ban"),
            telebot.types.BotCommand("tmute",        "Time-based mute (e.g., /tmute @user 1h)"),
            telebot.types.BotCommand("tban",         "Time-based ban (e.g., /tban @user 1d)"),
            telebot.types.BotCommand("mutelist",     "Show active mutes in group"),
            telebot.types.BotCommand("banlist",      "Show active bans in group"),
            telebot.types.BotCommand("kick",         "Remove user from group"),
            telebot.types.BotCommand("mute",         "Restrict user from talking"),
            telebot.types.BotCommand("unmute",       "Restore talking privileges"),
            telebot.types.BotCommand("warn",         "Issue a formal warning"),
            telebot.types.BotCommand("dwarn",        "Warn + delete user's message"),
            telebot.types.BotCommand("swarn",        "Silent warning (no reply, no chat message)"),
            telebot.types.BotCommand("rmwarn",       "Remove the most recent warning"),
            telebot.types.BotCommand("warnings",     "View warning config (limit, mode, expiry)"),
            telebot.types.BotCommand("setwarnlimit", "Set the warn limit (1-20)"),
            telebot.types.BotCommand("setwarnmode",  "Set warn mode (kick/ban/tban/mute/tmute)"),
            telebot.types.BotCommand("setwarntime",  "Enable warning expiry (e.g. 12w) or off"),
            telebot.types.BotCommand("warns",        "View a user's warnings with reasons"),
            telebot.types.BotCommand("removewarn",   "Remove 1 warning"),
            telebot.types.BotCommand("resetwarn",    "Clear all warnings"),
            telebot.types.BotCommand("keywordalert", "Toggle keyword alert"),
            telebot.types.BotCommand("setkeywords",  "Set keyword alert words"),
            telebot.types.BotCommand("lock",         "Lock the group"),
            telebot.types.BotCommand("unlock",       "Unlock the group"),
            telebot.types.BotCommand("locks",        "Show locks status"),
            telebot.types.BotCommand("promote",      "Promote to Administrator"),
            telebot.types.BotCommand("demote",       "Remove Administrator rights"),
            telebot.types.BotCommand("link",         "Fetch group invite link"),
            telebot.types.BotCommand("settitle",     "Change Group Title"),
            telebot.types.BotCommand("setdesc",      "Change Group Description"),
            telebot.types.BotCommand("addbadword",   "Add word to Auto-Mod filter"),
            telebot.types.BotCommand("delbadword",   "Remove word from filter"),
            telebot.types.BotCommand("antispam",     "Toggle Anti-Spam (inline buttons)"),
            telebot.types.BotCommand("antilink",     "Toggle Anti-Link (inline buttons)"),
            telebot.types.BotCommand("pin",          "Pin a message"),
            telebot.types.BotCommand("unpin",        "Unpin all messages"),
            telebot.types.BotCommand("del",          "Delete a replied message"),
            telebot.types.BotCommand("purge",        "Delete all messages from a replied message onward"),
            telebot.types.BotCommand("spurge",       "Quiet /purge — no confirmation message"),
            telebot.types.BotCommand("purgefrom",    "Start a ranged purge (reply to oldest message)"),
            telebot.types.BotCommand("purgeto",      "Finish a ranged purge (reply to newest message)"),
            telebot.types.BotCommand("rules",        "Show group rules"),
            telebot.types.BotCommand("setwelcome",   "Set welcome (reply to text/photo/GIF)"),
            telebot.types.BotCommand("setleave",     "Set goodbye (reply to text/photo/GIF)"),
            telebot.types.BotCommand("addfilter",    "Add auto-reply filter"),
            telebot.types.BotCommand("removefilter", "Remove auto-reply filter"),
            telebot.types.BotCommand("filters",      "List active filters"),
            telebot.types.BotCommand("admins",       "List all group admins"),
            telebot.types.BotCommand("send",         "Bot sends a custom message"),
            telebot.types.BotCommand("report",       "Alert admins about a message"),
            telebot.types.BotCommand("reports",      "Toggle user reports on/off"),
            telebot.types.BotCommand("id",           "Get User/Chat IDs (for Dashboard)"),
            telebot.types.BotCommand("approve",      "Toggle approve mode on/off"),
            telebot.types.BotCommand("captcha",      "CAPTCHA settings panel for new members"),
            telebot.types.BotCommand("captchamutetime", "Auto-unmute time if CAPTCHA unsolved (e.g. 5m)"),
            telebot.types.BotCommand("captchakicktime", "Auto-kick time if CAPTCHA unsolved (e.g. 5m)"),
            telebot.types.BotCommand("setcaptchatext", "Set custom CAPTCHA button text"),
            telebot.types.BotCommand("resetcaptchatext", "Reset CAPTCHA text to default"),
            telebot.types.BotCommand("rankings", "Show the chat activity leaderboard"),
            telebot.types.BotCommand("top", "Alias for /rankings"),
            telebot.types.BotCommand("mystats", "Your personal chat statistics"),
            telebot.types.BotCommand("chatstats", "This group's chat statistics"),
            telebot.types.BotCommand("activity", "Chat activity system settings (admins)"),
            telebot.types.BotCommand("resetactivity", "Reset chat activity stats (admins)"),
        ]
        bot.set_my_commands(commands)
    except Exception as e:
        logger.error(f"Failed to register commands: {e}")

    # ── Interactive dashboard callbacks (dm:*) ──
    dashboard_menu.register_dashboard_handlers(bot)

    # ── Callback Query Handlers ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith('unban_') or call.data.startswith('unmute_'))
    def handle_unban_unmute(call):
        if not is_admin(bot, call.message.chat.id, call.from_user.id) and not is_owner(call.from_user.username): return reply_not_admin(bot, call)
        
        action, target_id = call.data.split('_', 1)
        try:
            target_id = int(target_id)
            if action == 'unban':
                bot.unban_chat_member(call.message.chat.id, target_id)
                bot.answer_callback_query(call.id, "✅ User unbanned.", show_alert=True)
                bot.edit_message_text(f"{call.message.text}\n\n✅ User was unbanned by {call.from_user.first_name}", chat_id=call.message.chat.id, message_id=call.message.message_id)
            elif action == 'unmute':
                bot.restrict_chat_member(
                    call.message.chat.id, target_id,
                    permissions=telebot.types.ChatPermissions(
                        can_send_messages=True, can_send_audios=True, can_send_documents=True,
                        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                        can_add_web_page_previews=True
                    )
                )
                bot.answer_callback_query(call.id, "✅ User unmuted.", show_alert=True)
                bot.edit_message_text(f"{call.message.text}\n\n✅ User was unmuted by {call.from_user.first_name}", chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception as e:
            bot.answer_callback_query(call.id, f"Failed: {friendly_error(e)}", show_alert=True)

    # ── CAPTCHA Callback Query Handler ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith('captcha_verify:'))
    def handle_captcha_verify(call):
        """
        callback_data format:
          captcha_verify:button:<chat_id>:<user_id>
          captcha_verify:math:<chosen>:<chat_id>:<user_id>
          captcha_verify:text:<chosen>:<chat_id>:<user_id>
        (correct answers are verified server-side from the pending-captcha record)
        """
        parts = call.data.split(":")
        # parts[0] = 'captcha_verify'
        mode = parts[1]

        try:
            if mode == 'button':
                # format: captcha_verify:button:<chat_id>:<user_id>
                chat_id = int(parts[2])
                user_id = int(parts[3])
            else:
                # format: captcha_verify:math/text:<chosen>:<chat_id>:<user_id>
                chat_id = int(parts[3])
                user_id = int(parts[4])
        except (IndexError, ValueError):
            bot.answer_callback_query(call.id, "❌ Invalid CAPTCHA data.", show_alert=True)
            return

        # Only the challenged user can answer
        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ This CAPTCHA is not for you.", show_alert=True)
            return

        # Verify against the challenge stored in the database — never trust a
        # correct answer embedded in the callback_data (it is readable from the
        # message markup). A missing pending record means the challenge was
        # already resolved or expired.
        pending = db.get_pending_captcha(chat_id, user_id)
        if not pending:
            bot.answer_callback_query(call.id, "❌ CAPTCHA already resolved or expired.", show_alert=True)
            return

        if mode == 'button':
            passed = True  # button mode: a click is the proof
        else:
            expected = str(pending.get('correct_answer', ''))
            chosen   = parts[2]
            passed   = (chosen == expected)

        if passed:
            group_data   = db.get_group(chat_id)
            rules_needed = group_data.get("captcha_rules", 0)

            if rules_needed:
                # Show rules acceptance step
                rules_text = group_data.get("rules", "No rules set.")
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton(
                    "✅ I accept the rules",
                    callback_data=f"captcha_rules_accept:{chat_id}:{user_id}"
                ))
                try:
                    bot.edit_message_caption(
                        caption=(
                            f"📋 <b>Group Rules</b>\n\n"
                            f"{html.escape(rules_text)}\n\n"
                            f"Please accept the rules to start chatting."
                        ),
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                except:
                    try:
                        bot.edit_message_text(
                            text=(
                                f"📋 <b>Group Rules</b>\n\n"
                                f"{html.escape(rules_text)}\n\n"
                                f"Please accept the rules to start chatting."
                            ),
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            reply_markup=markup,
                            parse_mode="HTML"
                        )
                    except:
                        pass
                bot.answer_callback_query(call.id, "✅ Correct! Now accept the rules.", show_alert=False)
            else:
                # Unmute user immediately
                _captcha_unmute(bot, chat_id, user_id, call)
                db.remove_pending_captcha(chat_id, user_id)
        else:
            bot.answer_callback_query(call.id, "❌ Wrong answer! Try again.", show_alert=True)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('captcha_rules_accept:'))
    def handle_captcha_rules_accept(call):
        parts = call.data.split(":")
        try:
            chat_id = int(parts[1])
            user_id = int(parts[2])
        except (IndexError, ValueError):
            bot.answer_callback_query(call.id, "❌ Invalid data.", show_alert=True)
            return

        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ This is not for you.", show_alert=True)
            return

        _captcha_unmute(bot, chat_id, user_id, call)
        db.remove_pending_captcha(chat_id, user_id)


    # ── CAPTCHA Settings Inline Button Handlers ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith('captcha_toggle:') or
                                                   call.data.startswith('captcha_mode:') or
                                                   call.data.startswith('captcha_rules_toggle:') or
                                                   call.data.startswith('captcha_kick_toggle:') or
                                                   call.data.startswith('captcha_refresh:'))
    def handle_captcha_settings(call):
        if not is_admin(bot, call.message.chat.id, call.from_user.id) and not is_owner(call.from_user.username): return reply_not_admin(bot, call)

        parts = call.data.split(":")
        action = parts[0]

        try:
            if action == 'captcha_toggle':
                val = 1 if parts[1] == 'on' else 0
                chat_id = int(parts[2])
                db.update_group_setting(chat_id, "captcha", val)
                bot.answer_callback_query(call.id, f"✅ CAPTCHA {'enabled' if val else 'disabled'}.", show_alert=False)

            elif action == 'captcha_mode':
                mode = parts[1]
                chat_id = int(parts[2])
                db.update_group_setting(chat_id, "captcha_mode", mode)
                bot.answer_callback_query(call.id, f"✅ Mode set to {mode}.", show_alert=False)

            elif action == 'captcha_rules_toggle':
                chat_id = int(parts[1])
                group = db.get_group(chat_id)
                new_val = 0 if group.get("captcha_rules", 0) else 1
                db.update_group_setting(chat_id, "captcha_rules", new_val)
                bot.answer_callback_query(call.id, f"✅ Rules step {'enabled' if new_val else 'disabled'}.", show_alert=False)

            elif action == 'captcha_kick_toggle':
                chat_id = int(parts[1])
                group = db.get_group(chat_id)
                new_val = 0 if group.get("captcha_kick", 0) else 1
                db.update_group_setting(chat_id, "captcha_kick", new_val)
                bot.answer_callback_query(call.id, f"✅ Auto-kick {'enabled' if new_val else 'disabled'}.", show_alert=False)

            elif action == 'captcha_refresh':
                chat_id = int(parts[1])
                bot.answer_callback_query(call.id, "🔄 Refreshed.", show_alert=False)

            else:
                chat_id = int(parts[-1])

            # Redraw the panel with updated settings
            group = db.get_group(chat_id)
            panel_text, markup = build_captcha_panel(chat_id, group)
            try:
                bot.edit_message_text(
                    text=panel_text,
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            except Exception:
                pass  # Message unchanged, no edit needed

        except Exception as e:
            bot.answer_callback_query(call.id, friendly_error(e), show_alert=True)


    # ── CHAT ACTIVITY / RANKINGS SYSTEM (isolated module) ──
    # IMPORTANT: registered BEFORE the catch-all below. telebot runs handlers
    # in registration order and stops after the first match, so any command
    # handler registered after the catch-all would never be invoked.
    try:
        register_chat_handlers(bot)
    except Exception as e:
        logger.error(f"Failed to register chat activity handlers: {e}")


    # ─────────────────────────────────────────────────────────────
    # TIC TAC TOE GAME
    # ─────────────────────────────────────────────────────────────

    @bot.message_handler(commands=['tttset'])
    def cmd_tttset(message):
        if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username):
            return reply_not_admin(bot, message)
        current = db.get_group(message.chat.id).get("tictactoe", 0)
        new_status = 0 if current else 1
        db.update_group_setting(message.chat.id, "tictactoe", new_status)
        status = "ENABLED" if new_status else "DISABLED"
        bot.reply_to(message, f"🎮 Tic Tac Toe has been <b>{status}</b> in this group!", parse_mode="HTML")

    @bot.message_handler(commands=['ttt'])
    def cmd_ttt(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        group = db.get_group(message.chat.id)
        if not group or not group.get("tictactoe", 0):
            return bot.reply_to(message, "❌ Tic Tac Toe is not enabled in this group.")
        args = message.text.split()
        if len(args) == 1:
            existing = get_active_game(message.chat.id)
            if existing:
                if existing.get('status') == 'WAITING_FOR_PLAYER':
                    return bot.reply_to(message, "⚠️ A Tic Tac Toe game is already waiting for a player.")
                else:
                    return bot.reply_to(message, "⚠️ A Tic Tac Toe game is already in progress.")
            player1_id = str(message.from_user.id)
            player1_name = message.from_user.first_name or "Unknown"
            game_id = create_lobby(message.chat.id, player1_id, player1_name)
            if not game_id:
                return bot.reply_to(message, "❌ Error creating game.")
            markup = build_lobby_markup(game_id, message.chat.id)
            # Get lobby data for message formatting
            lobby = get_game(game_id)
            if not lobby:
                return bot.reply_to(message, "❌ Error creating game. Please try again.")
            text = format_lobby_message(lobby)
            bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")
        elif args[1] == 'rank':
            scores = get_scores(message.chat.id, 10)
            text = "🏆 <b>Tic Tac Toe Leaderboard</b>\n\n"
            if not scores:
                text += "No scores yet. Play a game!"
            else:
                for i, s in enumerate(scores, 1):
                    name = html.escape(str(s.get('name', s.get('user_id', '?'))), quote=False)
                    points = s.get('total_points', 0)
                    text += f"{i}. {name} — <b>{points} pts</b>\n"
            return bot.reply_to(message, text, parse_mode="HTML")
        elif args[1] == 'cancel':
            ok, msg = cancel_game(message.chat.id, message.from_user.id)
            return bot.reply_to(message, msg)
        elif args[1] == 'global':
            scores = get_global_scores(20)
            text = "🌍 <b>Global Tic Tac Toe Leaderboard</b>\n\n"
            if not scores:
                text += "No scores yet!"
            else:
                for i, s in enumerate(scores, 1):
                    uid = s.get('user_id', '?')
                    points = s.get('total_points', 0)
                    text += f"{i}. User {uid} — <b>{points} pts</b>\n"
            return bot.reply_to(message, text, parse_mode="HTML")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("ttt_"))
    def ttt_callback(call):
        try:
            parts = call.data.split(":")
            action = parts[0]
            if action == "ttt_join":
                game_id = parts[1]
                chat_id = int(parts[2]) if len(parts) > 2 else None
                if not chat_id:
                    game = get_game(game_id)
                    chat_id = int(game['chat_id']) if game else call.message.chat.id
                player2_id = str(call.from_user.id)
                player2_name = call.from_user.first_name or "Unknown"
                ok, msg = join_lobby(game_id, player2_id, player2_name)
                if not ok:
                    bot.answer_callback_query(call.id, msg)
                    return
                if not create_game_from_lobby(game_id):
                    bot.answer_callback_query(call.id, "Error starting game.")
                    return
                game = get_game(game_id)
                if not game:
                    bot.answer_callback_query(call.id, "Error starting game.")
                    return
                markup = build_board_markup(game_id, game['board'])
                text = format_game_message(game)
                bot.edit_message_text(text, chat_id=chat_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
                bot.answer_callback_query(call.id, "Joined!")
            elif action == "ttt_move":
                game_id = parts[1]
                pos = int(parts[2])
                game = get_game(game_id)
                if not game:
                    bot.answer_callback_query(call.id, "Game not found.")
                    return
                board, msg = make_move(game_id, str(call.from_user.id), pos)
                if board is None:
                    bot.answer_callback_query(call.id, msg)
                    return
                # Re-fetch for fresh status/board after the move
                game = get_game(game_id)
                if not game:
                    bot.answer_callback_query(call.id, "Game not found.")
                    return
                status = game.get('status', 'ACTIVE')
                if status in ('PLAYER_X_WON', 'PLAYER_O_WON', 'DRAW'):
                    markup = build_game_over_markup(game['board'])
                    text = format_game_over_message(game)
                else:
                    markup = build_board_markup(game_id, game['board'])
                    text = format_game_message(game)
                bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
                bot.answer_callback_query(call.id)
            elif action == "ttt_disabled":
                bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"TTT callback error: {e}")
            try:
                bot.answer_callback_query(call.id, "Error processing move.")
            except:
                pass

    # ── CATCH-ALL: rate-limit spam + link spam + bad words + filters ──
    @bot.message_handler(
        func=lambda m: True,
        content_types=['text', 'photo', 'video', 'sticker', 'animation', 'document', 'voice', 'audio']
    )
    def all_messages(message):
        if message.chat.type not in ['group', 'supergroup']:
            return

        # ── PURGE TRACKING (history used by /purge family) ──
        _purge_track(message.chat.id, message)

        # ── TRACKING SYSTEM (MODERATION MONITORING) ──
        try:
            tracking = db.get_tracking_config()
            dest_id = tracking.get("tracking_destination_group")
            if dest_id and dest_id.strip():
                try:
                    dest_id_int = int(dest_id)
                    # Forward if group is tracked OR user is tracked
                    if str(message.chat.id) in tracking.get("tracked_groups", []) or \
                       str(message.from_user.id) in tracking.get("tracked_users", []):
                        try:
                            fwd = bot.forward_message(chat_id=dest_id_int, from_chat_id=message.chat.id, message_id=message.message_id)
                            info_text = f"👤 <b>User:</b> {message.from_user.first_name} (<code>{message.from_user.id}</code>)\n🏘️ <b>Group:</b> {message.chat.title} (<code>{message.chat.id}</code>)"
                            bot.send_message(dest_id_int, info_text, reply_to_message_id=fwd.message_id, parse_mode="HTML")
                        except Exception:
                            # Fallback to copy_message if forward_message is restricted
                            try:
                                copy_msg = bot.copy_message(chat_id=dest_id_int, from_chat_id=message.chat.id, message_id=message.message_id)
                                info_text = f"👤 <b>User:</b> {message.from_user.first_name} (<code>{message.from_user.id}</code>)\n🏘️ <b>Group:</b> {message.chat.title} (<code>{message.chat.id}</code>)"
                                bot.send_message(dest_id_int, info_text, reply_to_message_id=copy_msg.message_id, parse_mode="HTML")
                            except Exception as copy_err:
                                if "429" not in str(copy_err) and "Too Many Requests" not in str(copy_err):
                                    logger.error(f"Failed to forward and copy tracked message: {copy_err}")
                except ValueError:
                    pass # Invalid destination ID
        except Exception as e:
            logger.error(f"Tracking error: {e}")

        # Track Group in DB
        db.ensure_group(message.chat.id, name=message.chat.title)
        db.add_message_count(message.chat.id)
        
        # Track User in DB (Captures Profile + Msg Count + Location)
        uname = getattr(message.from_user, 'username', None)
        fname = message.from_user.first_name or ""
        lname = message.from_user.last_name or ""
        full_name = f"{fname} {lname}".strip() or "User"
        
        db.ensure_user(
            message.from_user.id, 
            name=full_name, 
            username=uname, 
            chat_id=message.chat.id, 
            chat_name=message.chat.title,
            increment_msg=True
        )

        # ── CHAT ACTIVITY / RANKINGS SYSTEM (isolated module) ──
        try:
            track_group_message(bot, message)
        except Exception as e:
            logger.error(f"Chat activity tracking error: {e}")

        group = db.get_group(message.chat.id)
        # If group is still empty/None after lookup, use default settings
        if not group:
            group = {"antispam": False, "bad_words": [], "filters": {}}

        user_is_admin = is_admin(bot, message.chat.id, message.from_user.id)
        user_is_owner = is_owner(message.from_user.username)

        # ── ⚡ Rate-limit anti-spam (applies to ALL message types if enabled) ──
        antispam_active = group.get("antispam", False)
        if antispam_active and not user_is_admin and not user_is_owner:
            now = _time.time()
            key = (message.chat.id, message.from_user.id)
            dq  = _msg_timestamps[key]
            dq.append(now)
            # Evict timestamps outside window
            while dq and dq[0] < now - _SPAM_WINDOW:
                dq.popleft()

            if len(dq) > _SPAM_MAX:
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                    warnings, limit, punished = _auto_warn_limit(bot, message.chat.id, message, message.from_user.first_name, "Flood detected")
                    dq.clear()  # reset counter after action
                    if not punished:
                        bot.send_message(
                            message.chat.id,
                            f"⚡ <b>{message.from_user.first_name}</b>, slow down! "
                            f"Flood detected. Warning <b>{warnings}/{limit}</b>",
                            parse_mode="HTML"
                        )
                        db.log_event(f"⚡ Spam-warn: {message.from_user.id} in {message.chat.id} (warn {warnings}/{limit})")
                except Exception:
                    pass
                return

        # ── Text checks (handles both regular text and media captions) ──
        content = (message.text or message.caption or "").strip()
        if not content:
            return

        text_lower = content.lower()

        # ── 🔗 Link / Invite Link Protection ──
        antilink_active = group.get("antispam_auto_delete_links", False)
        if not user_is_admin and not user_is_owner:
            has_link = False
            if message.entities:
                for ent in message.entities:
                    if ent.type in ('url', 'text_link'):
                        has_link = True
                        break
            if not has_link and getattr(message, 'caption_entities', None):
                for ent in message.caption_entities:
                    if ent.type in ('url', 'text_link'):
                        has_link = True
                        break
            if not has_link:
                # regex/substring fallback check
                if ("http://" in text_lower or "https://" in text_lower or "www." in text_lower or "t.me/" in text_lower or "telegram.me/" in text_lower or "joinchat" in text_lower):
                    has_link = True

            if has_link:
                # If anti-link is ON, block all links
                if antilink_active:
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                        bot.send_message(
                            message.chat.id,
                            f"🚫 <b>Anti-Link</b>: {message.from_user.first_name}, "
                            f"links are <b>not allowed</b> in this group.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    db.log_event(f"🔗 Link blocked: user {message.from_user.id} in {message.chat.id}")
                    return

                # If anti-link is OFF but anti-spam is ON, block only Telegram invite links
                elif antispam_active:
                    is_invite = ("t.me/" in text_lower or "telegram.me/" in text_lower or "telegram.dog/" in text_lower or "joinchat" in text_lower)
                    if is_invite:
                        try:
                            bot.delete_message(message.chat.id, message.message_id)
                        except Exception:
                            pass
                        # Warn the user for sending invite links
                        try:
                            warnings, limit, punished = _auto_warn_limit(bot, message.chat.id, message, message.from_user.first_name, "Sending invite links")
                            if not punished:
                                bot.send_message(
                                    message.chat.id,
                                    f"🚫 <b>Anti-Spam</b>: <b>{message.from_user.first_name}</b>, "
                                    f"Telegram invite links are <b>not allowed</b> here. "
                                    f"Warning <b>{warnings}/{limit}</b>",
                                    parse_mode="HTML"
                                )
                                db.log_event(f"🔗 Invite link blocked: user {message.from_user.id} in {message.chat.id} (warn {warnings}/{limit})")
                        except Exception:
                            pass
                        return

            # Check for promotional spam (emoji flooding + links) - only if antispam is active
            if antispam_active:
                is_promo, reason = detect_promotional_spam(content)
                if is_promo:
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                    except Exception:
                        pass
                    # Warn the user for promotional spam
                    try:
                        warnings, limit, punished = _auto_warn_limit(bot, message.chat.id, message, message.from_user.first_name, reason)
                        if not punished:
                            bot.send_message(
                                message.chat.id,
                                f"🚫 <b>Anti-Spam</b>: <b>{message.from_user.first_name}</b>, {reason}. "
                                f"Warning <b>{warnings}/{limit}</b>",
                                parse_mode="HTML"
                            )
                            db.log_event(f"📊 Promo spam blocked: {reason} by {message.from_user.id} (warn {warnings}/{limit})")
                    except Exception:
                        pass
                    return

        # ── 🤬 Bad words — delete for ALL users, warn + potential ban for non-admins ──
        bad_words = group.get("bad_words", [])
        for bw in bad_words:
            # Use regex for word boundaries to avoid matching substrings (e.g. 'ass' in 'assume')
            pattern = rf"\b{re.escape(bw)}\b"
            if re.search(pattern, text_lower):
                # Always delete the message
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                except Exception:
                    pass

                # Only warn non-admins / non-owners
                if not user_is_admin and not user_is_owner:
                    try:
                        warnings, limit, punished = _auto_warn_limit(bot, message.chat.id, message, message.from_user.first_name, f"Bad word: {bw}")
                        if not punished:
                            bot.send_message(
                                message.chat.id,
                                f"⚠️ <b>{message.from_user.first_name}</b>, watch your language! "
                                f"Warning <b>{warnings}/{limit}</b>",
                                parse_mode="HTML"
                            )
                    except Exception:
                        pass
                return  # stop processing after bad word match

        # ── 🚨 Keyword Alert — notify admins when trigger word appears ──
        if group.get("keyword_alert", 0) and not user_is_admin and not user_is_owner:
            try:
                raw_words = group.get("keyword_alert_words", "") or "@admin,admin,help,support"
                if isinstance(raw_words, list):
                    keywords = [str(w).strip().lower() for w in raw_words if str(w).strip()]
                else:
                    keywords = [w.strip().lower() for w in str(raw_words).split(",") if w.strip()]
                matched = None
                for kw in keywords:
                    if not kw:
                        continue
                    if kw.startswith("@"):
                        if kw in text_lower:
                            matched = kw
                            break
                    else:
                        if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                            matched = kw
                            break
                if matched:
                    now = _time.time()
                    ck = (message.chat.id, message.from_user.id, matched)
                    last = _keyword_alert_cooldown.get(ck, 0)
                    if now - last >= 60:
                        _keyword_alert_cooldown[ck] = now
                        try:
                            admins = bot.get_chat_administrators(message.chat.id)
                            mentions = []
                            for ad in admins:
                                if not ad.user.is_bot:
                                    if ad.user.username:
                                        mentions.append(f"@{ad.user.username}")
                                    else:
                                        mentions.append(f"<a href='tg://user?id={ad.user.id}'>{html.escape(ad.user.first_name)}</a>")
                            admin_text = " ".join(mentions) if mentions else "Admins"
                        except:
                            admin_text = "Admins"
                        try:
                            safe_name = html.escape(message.from_user.first_name or "User")
                            safe_text = html.escape(content[:200])
                            bot.send_message(
                                message.chat.id,
                                f"🚨 <b>Keyword Alert!</b>\n\n"
                                f"👤 {safe_name} (<code>{message.from_user.id}</code>) mentioned <b>{html.escape(matched)}</b>\n"
                                f"💬 Message: <i>{safe_text}</i>\n\n"
                                f"👮 {admin_text} — please check!",
                                parse_mode="HTML"
                            )
                            db.log_event(f"🚨 Keyword alert '{matched}' by {message.from_user.id} in {message.chat.id}")
                            try:
                                for ad in bot.get_chat_administrators(message.chat.id):
                                    if not ad.user.is_bot:
                                        try:
                                            bot.send_message(
                                                ad.user.id,
                                                f"🚨 <b>Keyword Alert in {html.escape(message.chat.title or 'Group')}</b>\n\n"
                                                f"👤 {safe_name} (<code>{message.from_user.id}</code>) said: <i>{safe_text}</i>\n"
                                                f"🔑 Keyword: <b>{html.escape(matched)}</b>\n"
                                                f"🏘️ Group: {html.escape(message.chat.title or str(message.chat.id))} (<code>{message.chat.id}</code>)",
                                                parse_mode="HTML"
                                            )
                                        except:
                                            pass
                            except:
                                pass
                        except Exception as e:
                            logger.error(f"Keyword alert send failed: {e}")
            except Exception as e:
                logger.error(f"Keyword alert error: {e}")

        # ── 🔒 Chat Off — delete all non-admin messages ──
        if group.get("lock_chat_off") and not user_is_admin and not user_is_owner:
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            db.log_event(f"🔒 Chat Off: message deleted for {message.from_user.id} in {message.chat.id}")
            return

        # ── 🔒 Granular Locks — delete if locked ──
        if not user_is_admin and not user_is_owner:
            ctype = getattr(message, 'content_type', None)
            try:
                if ctype == 'sticker' and group.get("lock_sticker"):
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                    except:
                        pass
                    db.log_event(f"🔒 Sticker deleted for {message.from_user.id} in {message.chat.id} (lock_sticker)")
                    return
                elif ctype == 'animation' and group.get("lock_animation"):
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                    except:
                        pass
                    db.log_event(f"🔒 GIF deleted for {message.from_user.id} in {message.chat.id} (lock_animation)")
                    return
                elif ctype in ('photo','video','document','audio','voice','video_note') and group.get("lock_media"):
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                    except:
                        pass
                    db.log_event(f"🔒 Media deleted for {message.from_user.id} in {message.chat.id} (lock_media)")
                    return
                elif ctype == 'poll' and group.get("lock_poll"):
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                    except:
                        pass
                    return
                elif ctype == 'game' and group.get("lock_game"):
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                    except:
                        pass
                    return
                # URL check (text/caption)
                if group.get("lock_url"):
                    has_url = False
                    if message.entities:
                        for ent in message.entities:
                            if ent.type in ('url','text_link'):
                                has_url = True
                                break
                    if not has_url and getattr(message, 'caption_entities', None):
                        for ent in message.caption_entities:
                            if ent.type in ('url','text_link'):
                                has_url = True
                                break
                    if not has_url:
                        low = (content or "").lower()
                        if "http://" in low or "https://" in low or "www." in low or "t.me/" in low:
                            has_url = True
                    if has_url:
                        try:
                            bot.delete_message(message.chat.id, message.message_id)
                            db.log_event(f"🔒 URL deleted for {message.from_user.id} in {message.chat.id} (lock_url)")
                            return
                        except:
                            pass
                # Forward check
                if group.get("lock_forward") and (getattr(message, 'forward_from', None) or getattr(message, 'forward_from_chat', None) or getattr(message, 'forward_sender_name', None)):
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                        db.log_event(f"🔒 Forward deleted for {message.from_user.id} in {message.chat.id} (lock_forward)")
                        return
                    except:
                        pass
                # Inline (via_bot)
                if group.get("lock_inline") and getattr(message, 'via_bot', None):
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                        db.log_event(f"🔒 Inline deleted for {message.from_user.id} in {message.chat.id} (lock_inline)")
                        return
                    except:
                        pass
            except Exception as e:
                logger.error(f"Granular lock check failed: {e}")

        # ── 🔍 Auto-reply filters ──
        filters = group.get("filters", {})
        for trigger, f_data in filters.items():
            pattern = rf"\b{re.escape(trigger)}\b"
            if re.search(pattern, text_lower):
                ftype = f_data.get("type")
                try:
                    if ftype == "text":
                        bot.reply_to(message, f_data.get("text"))
                    elif ftype == "photo":
                        bot.send_photo(message.chat.id, f_data.get("file_id"),
                                       caption=f_data.get("caption"),
                                       reply_to_message_id=message.message_id)
                    elif ftype == "sticker":
                        bot.send_sticker(message.chat.id, f_data.get("file_id"),
                                         reply_to_message_id=message.message_id)
                    elif ftype == "gif":
                        bot.send_animation(message.chat.id, f_data.get("file_id"),
                                           reply_to_message_id=message.message_id)
                except Exception:
                    pass
                break

