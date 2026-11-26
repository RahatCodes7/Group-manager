"""
Message capture — stores incoming user messages (private DMs with the bot and
group messages) so the dashboard "Messages" viewer can show them as chats,
including who replied to whom, and export them as TXT / JSON / CSV.

How it hooks in:
  * `register_message_log(bot)` adds a telebot *middleware* handler. Middleware
    runs before every normal handler and never consumes a message, so all
    existing commands and moderation handlers behave exactly as before.
  * Storage is best-effort: any failure is logged and ignored.

Configuration (environment):
  No environment setup is required. Defaults are built in:
  MESSAGE_LOG_ENABLED         optional override, default on (0 disables capture)
  MESSAGE_LOG_RETENTION_DAYS  optional override, default 0 = keep every message
  CHAT_ACTIVITY_TIMEZONE      used to format timestamps in exports
"""

import logging
import os
import threading
import time as _time

from database import db

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 4000        # hard cap per stored message body
MAX_REPLY_CHARS = 300        # cap for the quoted reply snippet
PRUNE_INTERVAL = 3600        # seconds between automatic retention sweeps

# Telegram content types we store (service events such as "user joined" are skipped)
_LABELS = {
    "photo": "📷 Photo",
    "video": "🎬 Video",
    "animation": "🎞️ GIF",
    "sticker": "🎭 Sticker",
    "document": "📎 File",
    "voice": "🎤 Voice message",
    "audio": "🎵 Audio",
    "video_note": "⏺️ Video message",
    "contact": "👤 Contact",
    "location": "📍 Location",
    "venue": "📍 Venue",
    "poll": "📊 Poll",
    "dice": "🎲 Dice",
    "game": "🎮 Game",
}
_STORED_TYPES = {"text"} | set(_LABELS)

_last_prune = 0.0
_prune_lock = threading.Lock()


def message_log_enabled():
    return os.getenv("MESSAGE_LOG_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _retention_days():
    try:
        return int(os.getenv("MESSAGE_LOG_RETENTION_DAYS", "0").strip() or "0")
    except ValueError:
        return 0  # keep everything unless explicitly configured


def _display_name(user_or_chat):
    if user_or_chat is None:
        return ""
    first = getattr(user_or_chat, "first_name", None) or ""
    last = getattr(user_or_chat, "last_name", None) or ""
    name = f"{first} {last}".strip()
    if not name:
        name = getattr(user_or_chat, "title", None) or getattr(user_or_chat, "username", None) or ""
    return name.strip()


def _clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _body_for(message):
    """Text of the message, or a label for media (caption is kept when present)."""
    content_type = getattr(message, "content_type", "text") or "text"
    if content_type == "text":
        return getattr(message, "text", None) or ""
    label = _LABELS.get(content_type, "[" + content_type + "]")
    caption = getattr(message, "caption", None) or ""
    return f"{label}: {caption}" if caption else label


def _reply_info(message):
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None, "", ""
    sender = getattr(reply, "from_user", None) or getattr(reply, "sender_chat", None)
    snippet = _body_for(reply) if getattr(reply, "content_type", "text") in _STORED_TYPES else "[message]"
    return (
        getattr(reply, "message_id", None),
        _clip(_display_name(sender), 200),
        _clip(snippet, MAX_REPLY_CHARS),
    )


def _maybe_prune():
    global _last_prune
    now = _time.time()
    if now - _last_prune < PRUNE_INTERVAL:
        return
    with _prune_lock:
        if now - _last_prune < PRUNE_INTERVAL:
            return
        _last_prune = now
    try:
        removed = db.prune_chat_messages(_retention_days())
        if removed:
            logger.info(f"Message log retention: removed {removed} old message(s).")
    except Exception as e:
        logger.error(f"Message log prune failed: {e}")


def log_message(message):
    """Store one incoming message. Never raises."""
    try:
        if not message_log_enabled():
            return
        chat = getattr(message, "chat", None)
        user = getattr(message, "from_user", None)
        if chat is None or user is None:
            return
        if chat.type not in ("private", "group", "supergroup"):
            return
        if getattr(user, "is_bot", False):
            return
        content_type = getattr(message, "content_type", "text") or "text"
        if content_type not in _STORED_TYPES:
            return

        reply_id, reply_name, reply_text = _reply_info(message)
        row = {
            "chat_id": str(chat.id),
            "chat_type": chat.type,
            "chat_title": _clip(_display_name(chat), 200),
            "message_id": int(message.message_id),
            "user_id": str(user.id),
            "user_name": _clip(_display_name(user), 200) or "User",
            "username": getattr(user, "username", None) or "",
            "content_type": content_type,
            "text": _clip(_body_for(message), MAX_TEXT_CHARS),
            "reply_to_message_id": reply_id,
            "reply_to_user_name": reply_name,
            "reply_to_text": reply_text,
            "sent_at": int(getattr(message, "date", None) or _time.time()),
        }
        db.save_chat_message(row)
        _maybe_prune()
    except Exception as e:
        logger.error(f"Message log error: {e}")


def register_message_log(bot):
    """Attach the capture middleware to a TeleBot instance (must be created
    after apihelper.ENABLE_MIDDLEWARE = True)."""
    @bot.middleware_handler(update_types=["message"])
    def _capture_incoming_message(bot_instance, message):
        log_message(message)
