"""
Message export — formats stored messages as TXT, JSON or CSV for download.
Used by the dashboard's /api/messages/export endpoint.
"""

import csv
import io
import json
import os
import re
from datetime import datetime, timezone

# EXPORT
# ─────────────────────────────────────────────────────────────

def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("CHAT_ACTIVITY_TIMEZONE", "Asia/Dhaka").strip() or "Asia/Dhaka")
    except Exception:
        return timezone.utc


def format_sent_at(value):
    """Format a stored timestamp (datetime) in the configured timezone."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    return value.astimezone(_tz()).strftime("%Y-%m-%d %H:%M:%S")


def iso_utc(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def serialize_message(row):
    """JSON-safe dict for the dashboard API."""
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "chat_type": row["chat_type"],
        "chat_title": row["chat_title"],
        "user_id": row["user_id"],
        "user_name": row["user_name"],
        "username": row["username"],
        "content_type": row["content_type"],
        "text": row["text"],
        "reply_to_message_id": row["reply_to_message_id"],
        "reply_to_user_name": row["reply_to_user_name"],
        "reply_to_text": row["reply_to_text"],
        "sent_at": iso_utc(row["sent_at"]),
        "sent_at_local": format_sent_at(row["sent_at"]),
    }


def safe_filename(title, chat_id):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", (title or "").strip()).strip("_") or "chat"
    return f"{base[:60]}_{chat_id}"


def build_export(rows, fmt, chat_title="", chat_id=""):
    """Return (bytes, media_type, extension) for the given rows and format."""
    if fmt == "json":
        payload = {
            "chat_id": chat_id,
            "chat_title": chat_title,
            "exported_at": iso_utc(datetime.now(timezone.utc)),
            "message_count": len(rows),
            "messages": [serialize_message(r) for r in rows],
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return data, "application/json", "json"

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "sent_at", "chat_id", "chat_title", "message_id", "user_id", "user_name",
            "username", "content_type", "text", "reply_to_message_id", "reply_to_user_name",
            "reply_to_text",
        ])
        for r in rows:
            writer.writerow([
                r["id"], format_sent_at(r["sent_at"]), r["chat_id"], r["chat_title"],
                r["message_id"], r["user_id"], r["user_name"], r["username"], r["content_type"],
                r["text"], r["reply_to_message_id"] or "", r["reply_to_user_name"], r["reply_to_text"],
            ])
        # UTF-8 BOM so Excel shows Bengali / emoji text correctly
        return buf.getvalue().encode("utf-8-sig"), "text/csv", "csv"

    # default: plain text transcript
    lines = [
        f"Chat: {chat_title or chat_id} (ID {chat_id})",
        f"Messages: {len(rows)}",
        f"Exported: {format_sent_at(datetime.now(timezone.utc))}",
        "-" * 60,
    ]
    for r in rows:
        who = r["user_name"] or "User"
        if r["username"]:
            who += f" (@{r['username']})"
        line = f"[{format_sent_at(r['sent_at'])}] {who}: {r['text']}"
        if r["reply_to_message_id"]:
            target = r["reply_to_user_name"] or "message"
            line += f"\n    ↳ reply to {target}: {r['reply_to_text']}"
        lines.append(line)
    data = "\n".join(lines).encode("utf-8")
    return data, "text/plain", "txt"
