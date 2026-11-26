"""
Import OLD messages into the dashboard Messages viewer from a Telegram Desktop export.

The Bot API cannot fetch a chat's past history, so messages sent before the
Messages viewer was deployed only exist in Telegram itself. To bring them in:

  1. In Telegram Desktop: open the group (or your bot's chat) → ⋮ menu →
     "Export chat history" → Format: JSON → untick media (optional) → Export.
  2. Run this script on the same machine/environment that has DATABASE_URL:

       python scripts/import_telegram_export.py result.json

     Options:
       --dry-run     parse and count only, write nothing
       --chat-id ID  override the chat id (use if the export id does not match)

Imported rows are stored exactly like live messages. Re-running the import is
safe: rows that already exist (same chat + message id) are skipped.
"""

import argparse
import json
from datetime import datetime, timezone

# Telegram Desktop export "type" values → Bot API style chat ids
#   personal_chat          → chat id = user id (positive)
#   private_group          → basic group   → -<id>
#   private_supergroup /
#   public_supergroup /
#   public_channel         → supergroup    → -100<id>
_LABELS = {
    "photo": "📷 Photo",
    "video_file": "🎬 Video",
    "animation": "🎞️ GIF",
    "sticker": "🎭 Sticker",
    "file": "📎 File",
    "document": "📎 File",
    "voice_message": "🎤 Voice message",
    "audio_file": "🎵 Audio",
    "video_message": "⏺️ Video message",
    "poll": "📊 Poll",
    "contact": "👤 Contact",
    "location": "📍 Location",
}
_MAX_TEXT = 4000


def chat_id_from_export(chat_type, raw_id):
    raw = str(raw_id)
    if chat_type == "personal_chat":
        return raw
    if chat_type == "private_group":
        return "-" + raw
    return "-100" + raw


def flatten_text(text):
    """Telegram export stores text either as a string or a list of strings/entities."""
    if isinstance(text, str):
        return text
    parts = []
    for item in text or []:
        parts.append(item if isinstance(item, str) else (item.get("text") or ""))
    return "".join(parts)


def message_body(msg):
    body = flatten_text(msg.get("text", ""))
    media = msg.get("media_type")
    if media in _LABELS:
        kind = media
    elif msg.get("photo"):
        kind = "photo"
    elif msg.get("file") and not body:
        kind = "document"
    else:
        kind = None
    if kind:
        label = _LABELS[kind]
        return kind, (f"{label}: {body}" if body else label)
    return "text", body


def user_id_from(from_id):
    """'user123' -> '123'; channels/others are kept as-is."""
    s = str(from_id or "")
    return s[4:] if s.startswith("user") else s


def build_rows(data, chat_id_override=None):
    chat_type = data.get("type", "")
    chat_id = str(chat_id_override) if chat_id_override else chat_id_from_export(chat_type, data.get("id"))
    chat_title = data.get("name") or chat_id
    chat_kind = "private" if chat_type == "personal_chat" else ("group" if chat_type == "private_group" else "supergroup")

    messages = [m for m in data.get("messages", []) if m.get("type") == "message"]
    by_id = {m["id"]: m for m in messages}

    rows = []
    for m in messages:
        content_type, body = message_body(m)
        reply_id = m.get("reply_to_message_id")
        target = by_id.get(reply_id) if reply_id else None
        reply_name, reply_text = "", ""
        if reply_id:
            if target:
                reply_name = target.get("from") or ""
                reply_text = (message_body(target)[1] or "")[:300]
            else:
                reply_name = "message"  # replied-to message is not in this export
        ts = m.get("date_unixtime")
        if ts is None:
            try:
                ts = int(datetime.fromisoformat(m["date"]).replace(tzinfo=timezone.utc).timestamp())
            except Exception:
                ts = int(datetime.now(timezone.utc).timestamp())
        rows.append({
            "chat_id": chat_id,
            "chat_type": chat_kind,
            "chat_title": chat_title[:200],
            "message_id": int(m["id"]),
            "user_id": user_id_from(m.get("from_id")),
            "user_name": (m.get("from") or "User")[:200],
            "username": "",
            "content_type": content_type,
            "text": body[:_MAX_TEXT],
            "reply_to_message_id": int(reply_id) if reply_id else None,
            "reply_to_user_name": reply_name[:200],
            "reply_to_text": reply_text,
            "sent_at": int(ts),
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="path to result.json from Telegram Desktop")
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    ap.add_argument("--chat-id", help="override the chat id")
    args = ap.parse_args()

    with open(args.export, encoding="utf-8") as f:
        data = json.load(f)
    rows = build_rows(data, args.chat_id)
    print(f"Parsed {len(rows)} message(s) for chat {rows[0]['chat_id'] if rows else '?'} ({data.get('name')!r}).")
    if args.dry_run or not rows:
        return 0

    from database import db  # imported late: needs DATABASE_URL
    for row in rows:
        db.save_chat_message(row)  # duplicates (same chat + message id) are ignored by the DB
    print(f"Done. Processed {len(rows)} message(s); ones already in the database were skipped.")
    return 0
