"""
MISS WAGURI — Interactive interactive dashboard for /start.

Replaces the old static welcome card with a fully interactive dashboard:

  ┌──────────────────────────────┐
  │ 📺 media + WELCOME personalized message
  │ 🛡 Group mgmt & moderation    │
  │ 📊 Chat activity & leaderboard │
  │ 🎮 Tic-Tac-Toe                │
  │ MISS WAGURI 💕                 │
  └──────────────────────────────┘
  [⚙️ Commands] [❓ Help]
  [✨ About] [👨‍💻 Developer]
  [📢 Updates]
  [➕ Add to Group]

Every inline button is a callback (or a real external link) — nothing is
decorative. Commands, help pages and About/Developer/Updates are generated
from the code that actually exists — no invented commands,
no fake release notes.
"""

import html
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Configuration — env wins, DB config secondary, safe defaults third
# ─────────────────────────────────────────────────────────────

_DEFAULT_NAME = "MISS WAGURI"
# No version is fabricated: unset BOT_VERSION means the version is simply
# omitted on the pages that show it (no invented number).
_DEFAULT_VERSION = ""

_STYLE_PRIMARY = "primary"
_STYLE_SUCCESS = "success"
_STYLE_DANGER = "danger"

_CFG_CACHE_TTL = 60.0
_cfg_cache = {"data": None, "ts": 0.0, "lock": threading.Lock()}

# Tiny TTL dedup — Telegram can deliver the same callback_query twice.
_recent_callbacks = {}
_recent_callbacks_lock = threading.Lock()


def _is_duplicate_callback(call_id):
    now = time.time()
    with _recent_callbacks_lock:
        for k, ts in list(_recent_callbacks.items()):
            if now - ts > 30:
                del _recent_callbacks[k]
        if call_id in _recent_callbacks:
            return True
        _recent_callbacks[call_id] = now
        return False


def _cfg(key, default=""):
    """DB-first config — the database is the runtime source of truth (the owner
    changes it via the dashboard's Save & Apply). Env vars are the first-boot
    seed only (seeded into the DB at startup); they apply as a fallback when
    the database has nothing."""
    env_val = os.getenv(key, "").strip()
    try:
        conf = db.get_config()
        val = conf.get(key.lower(), "") if isinstance(conf, dict) else ""
        if val is not None and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    if env_val:
        return env_val
    return default


def _bot_name():
    return _cfg("BOT_NAME", _DEFAULT_NAME)


def _bot_version():
    return _cfg("BOT_VERSION", _DEFAULT_VERSION)


def _bot_username(bot):
    """Env override wins; otherwise the live username from Telegram."""
    env_val = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    if env_val:
        return env_val
    try:
        return bot.get_me().username or ""
    except Exception:
        return _cfg("BOT_USERNAME")


def _welcome_media():
    return _cfg("WELCOME_MEDIA")


def _welcome_media_type():
    val = _cfg("WELCOME_MEDIA_TYPE", "photo").lower()
    return val if val in ("photo", "video", "animation") else "photo"


def _developer_name():
    return _cfg("DEVELOPER_NAME")


def _developer_username():
    return _cfg("DEVELOPER_USERNAME").lstrip("@")


def _developer_url():
    return _cfg("DEVELOPER_URL")


def _support_url():
    return _cfg("SUPPORT_URL")


def _update_channel_url():
    return _cfg("UPDATE_CHANNEL_URL") or _cfg("SUPPORT_CHANNEL")


def _support_channel():
    """Official support channel username (without @) — DB-first, env seed for first boot."""
    return _cfg("SUPPORT_CHANNEL").lstrip("@")


def _main_group_link():
    """Support group link configured via /setgroup (stored in DB config)."""
    try:
        conf = db.get_config()
        return str(conf.get("main_group_link", "") or "").strip()
    except Exception:
        return ""


_GROUP_NAME_CACHE = {}
_GROUP_NAME_LOCK = threading.Lock()
_GROUP_NAME_TTL = 300.0


def _main_group_name(bot, link):
    """Best-effort resolution of the support group's real title.

    Falls back to the link slug (never invented) when Telegram can't be
    reached or the link is an invite link. Cached briefly.
    """
    if not link:
        return ""
    now = time.time()
    with _GROUP_NAME_LOCK:
        entry = _GROUP_NAME_CACHE.get(link)
        if entry and now - entry[1] < _GROUP_NAME_TTL:
            return entry[0]
    name = ""
    try:
        m = re.match(r"https?://t\.me/([A-Za-z][A-Za-z0-9_\d\.]+)", link.strip())
        slug = m.group(1) if m else ""
        if slug:
            try:
                chat = bot.get_chat("@" + slug)
                name = (getattr(chat, "title", "") or "").strip()
            except Exception:
                name = ""
        if not name and slug:
            name = slug  # honest fallback — the slug from the link, nothing fabricated
    except Exception:
        name = ""
    name = name or link
    with _GROUP_NAME_LOCK:
        _GROUP_NAME_CACHE[link] = (name, now)
    return name


def _github_url():
    return _cfg("GITHUB_URL")


def _other_projects():
    return _cfg("OTHER_PROJECTS")


def _update_changelog():
    """Optional configured release notes (multiline). Never fabricated."""
    return _cfg("UPDATE_CHANGELOG")


def _is_bot_running():
    try:
        return bool(db.get_config().get("is_running", False))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Auto-collect registered commands from the actual source files
# ─────────────────────────────────────────────────────────────

_COMMAND_SOURCE_FILES = ("bot_handlers.py", "chat_activity.py")


# Canonical aliases, taken from the actual handler registrations in the source.
# Aliases collapse to their canonical command with the canonical metadata —
# never a fake "Other Features" entry.
_ALIAS_CANONICAL = {
    "removewarn": "rmwarn",
    "unwarn": "rmwarn",
    "delwarn": "rmwarn",
    "resetwarn": "rmwarn",
    "clearwarn": "rmwarn",
    "resetwarnings": "rmwarn",
    "warntime": "setwarntime",
    "keyword_alert": "keywordalert",
    "setkeyword": "setkeywords",
    "set_keyword": "setkeywords",
    "top": "rankings",
}


def _source_commands():
    """Scan the bot source files for registered command handlers.

    Returns a stable, de-duplicated list of command names that are actually
    wired to handlers — the dashboard never advertises a command that does
    not exist.
    """
    import pathlib

    commands = []
    seen = set()
    for filename in _COMMAND_SOURCE_FILES:
        try:
            text = pathlib.Path(filename).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for match in re.finditer(r"commands=\[(.*?)\]", text, re.DOTALL):
            for token in re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)):
                if token not in seen:
                    seen.add(token)
                    commands.append(token)
    return commands


# ── Real metadata per command (verified against the handlers) ──
# Each entry is honest: name, category, description, usage, args, example,
# required permissions. Any command found in the source but missing from
# this catalog is shown under "Other Features" with a generic honest label.
_COMMANDS = [
    {
        "name": "setgroup", "cat": "group", "desc": "Broadcast the main group link to all known users (owner only).",
        "usage": "/setgroup <link>", "args": "Invite link", "example": "/setgroup https://t.me/+AbC123", "permission": "Owner",
    },
    {
        "name": "start", "cat": "utility", "desc": "Opens the interactive dashboard (welcome, commands, help, about).",
        "usage": "/start", "args": "None", "example": "/start", "permission": "Everyone",
    },
    {
        "name": "help", "cat": "utility", "desc": "Sends the full command list to your private messages.",
        "usage": "/help", "args": "None", "example": "/help", "permission": "Everyone",
    },
    {
        "name": "info", "cat": "info", "desc": "Shows your chat profile and status.",
        "usage": "/info", "args": "None", "example": "/info", "permission": "Everyone",
    },
    {
        "name": "admins", "cat": "info", "desc": "Lists all group administrators.",
        "usage": "/admins", "args": "None", "example": "/admins", "permission": "Members",
    },
    {
        "name": "ban", "cat": "moderation", "desc": "Permanently ban a user from the group.",
        "usage": "/ban @username [reason]", "args": "Username / user mention + optional reason",
        "example": "/ban @spammer spam", "permission": "Admin",
    },
    {
        "name": "kick", "cat": "moderation", "desc": "Remove a user (they can rejoin).",
        "usage": "/kick @username [reason]", "args": "Username / user mention + optional reason",
        "example": "/kick @spammer spam", "permission": "Admin",
    },
    {
        "name": "mute", "cat": "moderation", "desc": "Silence a user (restrict their messages).",
        "usage": "/mute @username", "args": "Username / user mention",
        "example": "/mute @spammer", "permission": "Admin",
    },
    {
        "name": "s_ban", "cat": "moderation", "desc": "Silent ban — bans without a chat notification.",
        "usage": "/s_ban @username [reason]", "args": "Username / user mention + optional reason",
        "example": "/s_ban @spammer", "permission": "Admin",
    },
    {
        "name": "s_kick", "cat": "moderation", "desc": "Silent kick — kicks without a chat notification.",
        "usage": "/s_kick @username [reason]", "args": "Username / user mention + optional reason",
        "example": "/s_kick @spammer", "permission": "Admin",
    },
    {
        "name": "s_mute", "cat": "moderation", "desc": "Silent mute — mutes without a chat notification.",
        "usage": "/s_mute @username", "args": "Username / user mention",
        "example": "/s_mute @spammer", "permission": "Admin",
    },
    {
        "name": "unmute", "cat": "moderation", "desc": "Restore a muted user's talking privileges.",
        "usage": "/unmute @username", "args": "Username / user mention",
        "example": "/unmute @spammer", "permission": "Admin",
    },
    {
        "name": "unban", "cat": "moderation", "desc": "Revoke a ban and let the user re-join.",
        "usage": "/unban @username", "args": "Username / user mention",
        "example": "/unban @spammer", "permission": "Admin",
    },
    {
        "name": "tmute", "cat": "moderation", "desc": "Timed mute — restrict a user for a duration and auto-unmute later.",
        "usage": "/tmute @username <duration> [reason]", "args": "Duration (1m/1h/1d/1w/1mn/1y)",
        "example": "/tmute @spammer 1h spam", "permission": "Admin",
    },
    {
        "name": "tban", "cat": "moderation", "desc": "Timed ban — ban a user for a duration and auto-unban later.",
        "usage": "/tban @username <duration> [reason]", "args": "Duration (1m/1h/1d/1w/1mn/1y)",
        "example": "/tban @baduser 3d violation", "permission": "Admin",
    },
    {
        "name": "mutelist", "cat": "moderation", "desc": "List currently muted users.",
        "usage": "/mutelist", "args": "None", "example": "/mutelist", "permission": "Members",
    },
    {
        "name": "banlist", "cat": "moderation", "desc": "List currently banned users.",
        "usage": "/banlist", "args": "None", "example": "/banlist", "permission": "Members",
    },
    {
        "name": "warn", "cat": "moderation", "desc": "Issue a formal warning to a user (reason optional).",
        "usage": "/warn @username [reason]", "args": "Username / user mention + optional reason",
        "example": "/warn @spammer spam", "permission": "Admin",
    },
    {
        "name": "dwarn", "cat": "moderation", "desc": "Warn the user AND delete their offending message.",
        "usage": "/dwarn @username [reason]", "args": "Username / user mention + optional reason",
        "example": "/dwarn @spammer spam", "permission": "Admin",
    },
    {
        "name": "swarn", "cat": "moderation", "desc": "Silent warning — no chat reply, command deleted.",
        "usage": "/swarn @username [reason]", "args": "Username / user mention + optional reason",
        "example": "/swarn @spammer spam", "permission": "Admin",
    },
    {
        "name": "rmwarn", "cat": "moderation", "desc": "Remove the most recent warning for a user (aliases: /removewarn, /unwarn, /delwarn, /resetwarn, /clearwarn, /resetwarnings).",
        "usage": "/rmwarn @username", "args": "Username / user mention",
        "example": "/rmwarn @spammer", "permission": "Admin",
    },
    {
        "name": "warnings", "cat": "moderation", "desc": "View the warning configuration (limit, mode, expiry) for the group.",
        "usage": "/warnings", "args": "None", "example": "/warnings", "permission": "Members",
    },
    {
        "name": "setwarnlimit", "cat": "moderation", "desc": "Set the warning limit before action is taken (1–20).",
        "usage": "/setwarnlimit <n>", "args": "Number 1–20", "example": "/setwarnlimit 5", "permission": "Admin",
    },
    {
        "name": "setwarnmode", "cat": "moderation", "desc": "Set the action when a user hits the warning limit.",
        "usage": "/setwarnmode <action> [time]", "args": "kick | ban | mute | tban | tmute (+ optional duration)",
        "example": "/setwarnmode kick 12w", "permission": "Admin",
    },
    {
        "name": "setwarntime", "cat": "moderation", "desc": "Enable warning expiry or disable it (e.g. 12w or off).",
        "usage": "/setwarntime <time|off>", "args": "Duration or off",
        "example": "/setwarntime 12w", "permission": "Admin",
    },
    {
        "name": "warns", "cat": "moderation", "desc": "View a user's warnings with reasons (reply or mention).",
        "usage": "/warns @username", "args": "Username / user mention",
        "example": "/warns @spammer", "permission": "Members",
    },
    {
        "name": "keywordalert", "cat": "moderation", "desc": "Toggle the keyword alert feature for the group.",
        "usage": "/keywordalert", "args": "None", "example": "/keywordalert", "permission": "Admin",
    },
    {
        "name": "setkeywords", "cat": "moderation", "desc": "Set the keyword list that triggers alerts (aliases: /setkeyword, /set_keyword).",
        "usage": "/setkeywords <words>", "args": "Comma-separated words",
        "example": "/setkeywords @admin,help", "permission": "Admin",
    },
    {
        "name": "lock", "cat": "admin", "desc": "Lock the group (or a specific content type).",
        "usage": "/lock [type]", "args": "sticker | gif | media | url | forward | inline | poll | game | chat_off | all",
        "example": "/lock url", "permission": "Admin",
    },
    {
        "name": "unlock", "cat": "admin", "desc": "Unlock the group (or a specific content type).",
        "usage": "/unlock [type]", "args": "Same types as /lock",
        "example": "/unlock url", "permission": "Admin",
    },
    {
        "name": "locks", "cat": "admin", "desc": "Show the current lock status of the group.",
        "usage": "/locks", "args": "None", "example": "/locks", "permission": "Members",
    },
    {
        "name": "promote", "cat": "admin", "desc": "Give a user administrator rights.",
        "usage": "/promote @username", "args": "Username / user mention",
        "example": "/promote @trusted_user", "permission": "Admin",
    },
    {
        "name": "demote", "cat": "admin", "desc": "Remove administrator rights from a user.",
        "usage": "/demote @username", "args": "Username / user mention",
        "example": "/demote @trusted_user", "permission": "Admin",
    },
    {
        "name": "settitle", "cat": "group", "desc": "Change the group title.",
        "usage": "/settitle <new title>", "args": "New group name",
        "example": "/settitle My Awesome Group", "permission": "Admin",
    },
    {
        "name": "setdesc", "cat": "group", "desc": "Change the group description.",
        "usage": "/setdesc <new description>", "args": "New group description",
        "example": "/setdesc Welcome to our group!", "permission": "Admin",
    },
    {
        "name": "pin", "cat": "admin", "desc": "Pin a message (reply to it).",
        "usage": "/pin (reply to message)", "args": "None", "example": "/pin", "permission": "Admin",
    },
    {
        "name": "unpin", "cat": "admin", "desc": "Unpin all messages in the group.",
        "usage": "/unpin", "args": "None", "example": "/unpin", "permission": "Admin",
    },
    {
        "name": "report", "cat": "info", "desc": "Alert group admins about a message (reply to it, or @admin).",
        "usage": "/report", "args": "None", "example": "/report", "permission": "Everyone",
    },
    {
        "name": "reports", "cat": "admin", "desc": "Toggle user reports on/off.",
        "usage": "/reports on|off", "args": "on or off",
        "example": "/reports on", "permission": "Admin",
    },
    {
        "name": "setwelcome", "cat": "group", "desc": "Set the welcome message (reply to text/photo/GIF).",
        "usage": "/setwelcome", "args": "None", "example": "Reply to a photo with /setwelcome", "permission": "Admin",
    },
    {
        "name": "setleave", "cat": "group", "desc": "Set the goodbye message (reply to text/photo/GIF).",
        "usage": "/setleave", "args": "None", "example": "Reply to a GIF with /setleave", "permission": "Admin",
    },
    {
        "name": "rules", "cat": "group", "desc": "Show the current group rules.",
        "usage": "/rules", "args": "None", "example": "/rules", "permission": "Members",
    },
    {
        "name": "addfilter", "cat": "automation", "desc": "Create an auto-reply filter (reply to the trigger message).",
        "usage": "/addfilter", "args": "None", "example": "/addfilter", "permission": "Admin",
    },
    {
        "name": "removefilter", "cat": "automation", "desc": "Delete an auto-reply filter.",
        "usage": "/removefilter <trigger>", "args": "Trigger word or /removefilter (interactive list)",
        "example": "/removefilter help", "permission": "Admin",
    },
    {
        "name": "filters", "cat": "automation", "desc": "List all active auto-reply filters.",
        "usage": "/filters", "args": "None", "example": "/filters", "permission": "Members",
    },
    {
        "name": "captcha", "cat": "automation", "desc": "CAPTCHA settings panel for new members (admins).",
        "usage": "/captcha", "args": "None", "example": "/captcha", "permission": "Admin",
    },
    {
        "name": "captchamutetime", "cat": "automation", "desc": "Auto-unmute time if the CAPTCHA is not solved.",
        "usage": "/captchamutetime <duration>", "args": "e.g. 5m", "example": "/captchamutetime 5m", "permission": "Admin",
    },
    {
        "name": "captchakicktime", "cat": "automation", "desc": "Auto-kick time if the CAPTCHA is not solved.",
        "usage": "/captchakicktime <duration>", "args": "e.g. 5m", "example": "/captchakicktime 5m", "permission": "Admin",
    },
    {
        "name": "setcaptchatext", "cat": "automation", "desc": "Set a custom CAPTCHA button text.",
        "usage": "/setcaptchatext <text>", "args": "New button text",
        "example": "/setcaptchatext Click to prove you are human", "permission": "Admin",
    },
    {
        "name": "resetcaptchatext", "cat": "automation", "desc": "Reset the CAPTCHA text to the default.",
        "usage": "/resetcaptchatext", "args": "None", "example": "/resetcaptchatext", "permission": "Admin",
    },
    {
        "name": "link", "cat": "group", "desc": "Fetch the group's invite link.",
        "usage": "/link", "args": "None", "example": "/link", "permission": "Admin",
    },
    {
        "name": "addbadword", "cat": "moderation", "desc": "Add a word to the auto-moderation filter (links & bad words).",
        "usage": "/addbadword <word>", "args": "Word to filter",
        "example": "/addbadword spamword", "permission": "Admin",
    },
    {
        "name": "delbadword", "cat": "moderation", "desc": "Remove a word from the auto-moderation filter.",
        "usage": "/delbadword <word>", "args": "Word to remove",
        "example": "/delbadword spamword", "permission": "Admin",
    },
    {
        "name": "approve", "cat": "moderation", "desc": "Toggle approval mode (new members must be approved).",
        "usage": "/approve", "args": "None", "example": "/approve", "permission": "Admin",
    },
    {
        "name": "antispam", "cat": "moderation", "desc": "Toggle Anti-Spam (block links & promotional spam).",
        "usage": "/antispam", "args": "None", "example": "/antispam", "permission": "Admin",
    },
    {
        "name": "antilink", "cat": "moderation", "desc": "Toggle Anti-Link mode (block invite links).",
        "usage": "/antilink", "args": "None", "example": "/antilink", "permission": "Admin",
    },
    {
        "name": "id", "cat": "info", "desc": "Show your and the group's Telegram IDs (for the dashboard).",
        "usage": "/id", "args": "None", "example": "/id", "permission": "Everyone",
    },
    {
        "name": "send", "cat": "info", "desc": "Bot sends a custom message to the group.",
        "usage": "/send <message>", "args": "Message text",
        "example": "/send Hello everyone!", "permission": "Admin",
    },
    {
        "name": "rankings", "cat": "info", "desc": "Show the chat activity leaderboard (Overall / Today / Week).",
        "usage": "/rankings", "args": "None", "example": "/rankings", "permission": "Everyone",
    },
    {
        "name": "top", "cat": "info", "desc": "Alias for /rankings.",
        "usage": "/top", "args": "None", "example": "/top", "permission": "Everyone",
    },
    {
        "name": "mystats", "cat": "info", "desc": "Show your personal chat statistics in this group.",
        "usage": "/mystats", "args": "None", "example": "/mystats", "permission": "Everyone",
    },
    {
        "name": "chatstats", "cat": "info", "desc": "Show this group's chat statistics.",
        "usage": "/chatstats", "args": "None", "example": "/chatstats", "permission": "Everyone",
    },
    {
        "name": "activity", "cat": "admin", "desc": "Chat activity settings — toggle tracking, milestones & leaderboard (admins).",
        "usage": "/activity", "args": "None", "example": "/activity", "permission": "Admin",
    },
    {
        "name": "resetactivity", "cat": "admin", "desc": "Reset this group's chat stats (admins).",
        "usage": "/resetactivity", "args": "None", "example": "/resetactivity", "permission": "Admin",
    },
    {
        "name": "del", "cat": "purge", "desc": "Delete a specific message (reply to it).",
        "usage": "/del", "args": "None", "example": "Reply to a message with /del", "permission": "Admin",
    },
    {
        "name": "purge", "cat": "purge", "desc": "Delete messages from a replied message onward (optionally N messages).",
        "usage": "/purge [N]", "args": "Optional max count",
        "example": "/purge 15", "permission": "Admin",
    },
    {
        "name": "spurge", "cat": "purge", "desc": "Quiet /purge — no confirmation message.",
        "usage": "/spurge [N]", "args": "Optional max count",
        "example": "/spurge 15", "permission": "Admin",
    },
    {
        "name": "purgefrom", "cat": "purge", "desc": "Mark the starting message for a ranged purge (reply to the oldest).",
        "usage": "/purgefrom", "args": "None", "example": "/purgefrom", "permission": "Admin",
    },
    {
        "name": "purgeto", "cat": "purge", "desc": "Mark the ending message for a ranged purge (reply to the newest).",
        "usage": "/purgeto", "args": "None", "example": "/purgeto", "permission": "Admin",
    },
    {
        "name": "tttset", "cat": "ttt", "desc": "Toggle Tic-Tac-Toe in this group (admins).",
        "usage": "/tttset", "args": "None", "example": "/tttset", "permission": "Admin",
    },
    {
        "name": "ttt", "cat": "ttt", "desc": "Start / join a Tic-Tac-Toe game (or /ttt rank for scores).",
        "usage": "/ttt", "args": "Optional: rank",
        "example": "/ttt", "permission": "Members",
    },
]

_COMMAND_CATEGORY_META = [
    ("group", "🏠 Group Management", "group"),
    ("admin", "👮 Admin Commands", "admin"),
    ("moderation", "🛡️ Moderation", "moderation"),
    ("purge", "🧹 Purge Commands", "purge"),
    ("info", "📊 Information", "info"),
    ("automation", "🤖 Automation & Auto-Mod", "automation"),
    ("ttt", "🎮 Tic-Tac-Toe", "ttt"),
    ("utility", "⚙️ Utility", "utility"),
    ("other", "✨ Other Features", "other"),
]

_COMMAND_CATEGORIES = {
    "group": ("🏠 Group Management", "Set group title/description, welcome & goodbye messages, rules, invite link."),
    "admin": ("👮 Admin Commands", "Locks, promotions, moderation controls, activity settings."),
    "moderation": ("🛡️ Moderation", "Bans, kicks, mutes, timed actions, warnings, keyword alerts."),
    "purge": ("🧹 Purge Commands", "Delete messages — from a single reply to a ranged purge."),
    "info": ("📊 Information", "Profiles, admins, activity leaderboard, stats."),
    "automation": ("🤖 Automation & Auto-Mod", "Auto-reply filters, CAPTCHA settings, welcome."),
    "ttt": ("🎮 Tic-Tac-Toe", "Start and manage Tic-Tac-Toe in the group."),
    "utility": ("⚙️ Utility", "Basic utility commands."),
    "other": ("✨ Other Features", "Other registered commands."),
}


def _all_commands():
    """All real commands from the source, with real metadata where available."""
    source = _source_commands()
    by_name = {c["name"]: c for c in _COMMANDS}
    seen = set()
    out = []
    for name in source:
        name = _ALIAS_CANONICAL.get(name, name)
        if name in seen:
            continue
        seen.add(name)
        meta = by_name.get(name)
        if meta:
            out.append(dict(meta))
        else:
            out.append({
                "name": name,
                "cat": "other",
                "desc": f"Registered /{name} command.",
                "usage": f"/{name}",
                "args": "See the command handler",
                "example": f"/{name}",
                "permission": "Unknown",
            })
    return out


def _commands_by_category():
    cat_map = {}
    for cmd in _all_commands():
        cat_map.setdefault(cmd["cat"], []).append(cmd)
    return cat_map


# ─────────────────────────────────────────────────────────────
# Keyboard helpers
# ─────────────────────────────────────────────────────────────

def _btn(text, data=None, url=None, style=None, disabled=None):
    return InlineKeyboardButton(text, callback_data=data, url=url, style=style, disabled=disabled)


def _markup_buttons(rows):
    markup = InlineKeyboardMarkup()
    for row in rows:
        if isinstance(row, (list, tuple)):
            markup.row(*row)
        else:
            markup.row(row)
    return markup


def _nav_buttons(parent=None, main=True):
    """Back + Main Menu row for sub-pages."""
    rows = []
    back_row = []
    if parent:
        back_row.append(_btn("⬅️ Back", f"dm:back:{parent}", style=_STYLE_PRIMARY))
    back_row.append(_btn("🏠 Main Menu", "dm:main", style=_STYLE_PRIMARY))
    rows.append(back_row)
    return rows


# ─────────────────────────────────────────────────────────────
# Text builders
# ─────────────────────────────────────────────────────────────

def build_main_menu_text(first_name="there"):
    bot_name = _bot_name()
    version = _bot_version()
    first = html.escape(str(first_name or "there"))
    text = (
        f"🎉 WELCOME!\n\n"
        f"Hey <b>{first}</b> 👋\n\n"
        f"Welcome to <b>{html.escape(bot_name)}</b> 💕\n\n"
        f"Explore the available features below:\n\n"
        f"🛡 {html.escape(bot_name)} group management &amp; moderation\n"
        f"📊 Chat activity &amp; leaderboard\n"
        f"🎮 Tic-Tac-Toe\n"
        f"⚙️ Advanced administration tools\n\n"
        f"✨ <b>{html.escape(bot_name)}</b> 💕{f' v{html.escape(version)}' if version else ''}"
    )
    return text


def build_main_menu_markup(bot):
    username = _bot_username(bot)
    rows = [
        [
            _btn("⚙️ Commands", "dm:commands", style=_STYLE_PRIMARY),
            _btn("❔ Help", "dm:help", style=_STYLE_PRIMARY),
        ],
        [
            _btn("✨ About", "dm:about", style=_STYLE_PRIMARY),
            _btn("👨‍💻 Developer", "dm:dev", style=_STYLE_PRIMARY),
        ],
        [_btn("📢 Updates", "dm:updates", style=_STYLE_PRIMARY)],
    ]
    if username:
        rows.append([
            InlineKeyboardButton(
                "➕ Add to Group",
                url=f"https://t.me/{username}?startgroup=true",
                style=_STYLE_SUCCESS,
            )
        ])
    return _markup_buttons(rows)


# ─────────────────────────────────────────────────────────────
# Commands directory
# ─────────────────────────────────────────────────────────────

_PAGE_SIZE = 6


def _commands_index_text():
    cat_map = _commands_by_category()
    lines = [
        "<b>⚙️ COMMAND DIRECTORY</b>",
        "",
        "Choose a category to browse the commands,",
        "with descriptions, usage, examples and permissions:",
        "",
    ]
    total = sum(len(v) for v in cat_map.values())
    lines.append(f"📚 <b>{total}</b> commands across <b>{len(cat_map)}</b> categories")
    lines.append("")
    lines.append("<i>Every listed command is registered in the bot's source — nothing is invented.</i>")
    return "\n".join(lines)


def _commands_index_markup():
    cat_map = _commands_by_category()
    order = ["group", "admin", "moderation", "purge", "info", "automation", "ttt", "utility", "other"]
    rows = []
    buttons = []
    for cat in order:
        commands = cat_map.get(cat)
        if not commands:
            continue
        label, _ = _COMMAND_CATEGORIES.get(cat, (cat.upper(), ""))
        buttons.append(_btn(f"{label} · {len(commands)}", f"dm:cat:{cat}:0", style=_STYLE_PRIMARY))
    # two buttons per row
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i + 2])
    rows += _nav_buttons(parent=None, main=True)
    return _markup_buttons(rows)


def _commands_category_text(cat, page):
    cat_map = _commands_by_category()
    commands = cat_map.get(cat, [])
    total_pages = max(1, (len(commands) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    page_items = commands[start:start + _PAGE_SIZE]

    label, _desc = _COMMAND_CATEGORIES.get(cat, (cat.upper(), ""))
    lines = [
        f"<b>{label}</b>",
        f"<i>Page {page + 1} of {total_pages} — {len(commands)} command(s)</i>",
        "",
    ]
    for cmd in page_items:
        lines.append(f"<b>/{cmd['name']}</b> — {cmd['desc']}")
        lines.append(f"   📌 Usage: <code>{html.escape(cmd['usage'])}</code>")
        if cmd.get("example") and cmd["example"] != f"/{cmd['name']}":
            lines.append(f"   💡 Example: <code>{html.escape(cmd['example'])}</code>")
        lines.append(f"   🔑 Permission: {html.escape(cmd['permission'])}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _commands_category_markup(cat, page):
    cat_map = _commands_by_category()
    commands = cat_map.get(cat, [])
    total_pages = max(1, (len(commands) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = []
    nav = []
    if page > 0:
        nav.append(_btn("◀️ Prev", f"dm:cat:{cat}:{page - 1}", style=_STYLE_PRIMARY))
    if page < total_pages - 1:
        nav.append(_btn("Next ▶️", f"dm:cat:{cat}:{page + 1}", style=_STYLE_PRIMARY))
    if nav:
        rows.append(nav)
    rows += _nav_buttons(parent="commands", main=True)
    return _markup_buttons(rows)


# ─────────────────────────────────────────────────────────────
# Help center
# ─────────────────────────────────────────────────────────────

_HELP_CATEGORIES = [
    ("start", "🚀 Getting Started",
     "Welcome! 🎉\n\n"
     "This bot helps you manage groups: moderation, welcome, captcha, "
     "auto-filters, leaderboard and more.\n\n"
     "1️⃣ Add the bot to your group.\n"
     "2️⃣ Open it in a DM and press /start — the interactive dashboard will open.\n"
     "3️⃣ Add the bot as an administrator if you want moderation features.\n"
     "4️⃣ Explore the Commands section for the full directory.\n\n"
     "💬 Chat types: Private, Group and Supergroup."),
    ("add", "➕ Adding the bot to Groups",
     "Add the bot to your group using the ➕ Add to Group button on the main dashboard.\n\n"
     "After joining the bot:\n"
     "• It registers the group automatically.\n"
     "• It reads whether it is an admin (if so, moderation features are enabled).\n"
     "• For moderation features (mutes, bans, purge, locks), make the bot an administrator with the needed rights.\n\n"
     "The bot does not change any permission on its own."),
    ("config", "⚙️ Group Configuration",
     "Group settings live in PostgreSQL — they survive restarts.\n\n"
     "• /rules — show the current group rules.\n"
     "• /setwelcome — reply to text/photo/GIF to set the greeting.\n"
     "• /setleave — reply to text/photo/GIF to set the goodbye.\n"
     "• /link — invite link.\n"
     "• /captcha — CAPTCHA settings panel (button, math, text).\n"
     "• /antispam, /antilink — toggle link/spam protection.\n"
     "• /activity — chat activity leaderboard settings.\n\n"
     "All settings are per-group."),
    ("admin", "👮 Admin Permissions",
     "Most moderation commands require admin rights.\n\n"
     "• /promote and /demote — manage admin rights.\n"
     "• /lock and /unlock — restrict content types.\n"
     "• /setwelcome, /setleave, /addfilter — group configuration.\n"
     "• /reports — toggle report alerts.\n\n"
     "Restrictions: the bot cannot grant admin rights to itself or bypass Telegram's permissions. Add it as an admin manually."),
    ("moderation", "🛡️ Moderation Guide",
     "Moderation actions:\n\n"
     "• /ban /kick — ban or remove a user (kicks allow rejoin).\n"
     "• /mute /unmute — restrict talking privileges.\n"
     "• /tmute @user 1h — timed mute with auto-unmute.\n"
     "• /tban @user 1d — timed ban with auto-unban.\n"
     "• /warn /dwarn /swarn — warnings (deleting message / silent).\n"
     "• /setwarnlimit <n> — warnings before action.\n"
     "• /setwarnmode <kick|ban|mute|tban|tmute> [time]\n"
     "• /setwarntime <time|off> — expire old warnings.\n\n"
     "💡 Warnings reset at the limit; with /setwarntime, old warnings expire."),
    ("purge", "🧹 Purge Guide",
     "Messages can be deleted in several ways:\n\n"
     "• /del — reply to a message to delete it.\n"
     "• /purge [N] — delete from a replied message onward (N = max count).\n"
     "• /spurge [N] — quiet /purge (no confirmation).\n"
     "• /purgefrom + /purgeto — mark the oldest, then the newest, to purge a range.\n\n"
     "Example: reply to a spam message, then /purge 15 to delete up to 15 messages from there on."),
    ("activity", "📊 Chat Activity",
     "Track chat activity and show the leaderboard:\n\n"
     "• /rankings (or /top) — Overall / Today / Week leaderboard.\n"
     "• /mystats — your personal stats.\n"
     "• /chatstats — group-wide stats.\n"
     "• /activity — toggle tracking, milestones and leaderboard (admins).\n"
     "• /resetactivity — reset the group's stats (admins).\n\n"
     "Chat activity is per-group and survives restarts."),
    ("ttt", "🎮 Tic-Tac-Toe",
     "• /tttset — toggle Tic-Tac-Toe in this group (admins).\n"
     "• /ttt — start a game (one active game per group).\n"
     "• /ttt rank — show scores.\n\n"
     "First player to X goes first; scores are tracked per group."),
    ("trouble", "🔧 Troubleshooting",
     "• The dashboard is empty — restart the bot and check the deployment logs.\n"
     "• The bot doesn't respond — check BOT_TOKEN and that the bot has the needed rights.\n"
     "• Category buttons don't open — the dashboard may have been restarted; run /start again."),
    ("faq", "❓ FAQ",
     "Q: Can I see the bot's statistics?\n"
     "A: Yes — per-group chat activity and the Live Leaderboard are available on the dashboard;\n"
     "   /mystats and /chatstats show per-group numbers (registered users and groups are never shown as a global count).\n\n"
     "Q: Are my settings saved?\n"
     "A: Yes — group settings and scores persist in PostgreSQL.\n\n"
     "Q: Do commands work for everyone?\n"
     "A: Moderation commands require admin rights; information commands work for everyone.\n\n"
     "Q: Can I change the welcome media?\n"
     "A: Yes — via the WELCOME_MEDIA environment variable (see README)."),
]

_HELP_INDEX_TEXT = (
    "<b>❔ HELP CENTER</b>\n\n"
    "Choose a topic to get detailed instructions,\n"
    "real command examples and practical guidance.\n\n"
    "<i>Nothing here is invented — every command and feature listed "
    "exists in the bot right now.</i>"
)

_HELP_INDEX_MARKUP_ROWS = [
    ("start", "🚀 Getting Started"),
    ("add", "➕ Add to Group"),
    ("config", "⚙️ Group Configuration"),
    ("admin", "👮 Admin Permissions"),
    ("moderation", "🛡️ Moderation Guide"),
    ("purge", "🧹 Purge Guide"),
    ("activity", "📊 Chat Activity"),
    ("ttt", "🎮 Tic-Tac-Toe"),
    ("trouble", "🔧 Troubleshooting"),
    ("faq", "❓ FAQ"),
]


def _help_index_markup():
    rows = []
    buttons = [_btn(label, f"dm:helpcat:{key}", style=_STYLE_PRIMARY) for key, label in _HELP_INDEX_MARKUP_ROWS]
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i + 2])
    rows += _nav_buttons(parent=None, main=True)
    return _markup_buttons(rows)


def _help_page_text(key):
    for k, _label, text in _HELP_CATEGORIES:
        if k == key:
            return text
    return "<b>❓ Help</b>\n\nThis topic is not available."


def _help_page_markup(key):
    label = next((l for k, l, _ in _HELP_CATEGORIES if k == key), "❓ Help")
    rows = []
    rows += _nav_buttons(parent="help", main=True)
    return _markup_buttons(rows)


# ─────────────────────────────────────────────────────────────
# About / Developer / Updates
# ─────────────────────────────────────────────────────────────

def _about_text(bot):
    name = _bot_name()
    version = _bot_version()
    dev_name = _developer_name()
    dev_username = _developer_username()
    running = _is_bot_running()
    features = [
        "🛡 Group management &amp; moderation",
        "📊 Chat activity &amp; leaderboard",
        "🎮 Tic-Tac-Toe",
        "🤖 Auto-moderation, CAPTCHA &amp; auto-reply filters",
        "📈 Per-group chat activity &amp; leaderboards",
    ]
    lines = [
        f"<b>ℹ️ ABOUT {html.escape(name)}</b>",
        "",
        f"📛 <b>Name:</b> {html.escape(name)}",
        f"📝 <b>Description:</b> Interactive group management &amp; moderation bot with a live interactive dashboard.",
    ]
    if version:
        lines.append(f"📦 <b>Version:</b> {html.escape(version)}")
    lines += [
        "",
        "<b>✨ Main Features:</b>",
    ]
    for f in features:
        lines.append(f"  • {f}")
    lines += [
        "",
        "💬 <b>Chat types:</b> Private, Group &amp; Supergroup",
        "🗄️ <b>Database:</b> PostgreSQL (persistent)",
        "🔧 <b>Engine:</b> pyTelegramBotAPI + FastAPI dashboard",
    ]
    if dev_name or dev_username:
        who = dev_name or f"@{dev_username}"
        lines.append(f"👨‍💻 <b>Developer:</b> {html.escape(who)}")
    status = "Running" if running else "Not running"
    lines.append(f"🟢 <b>Status:</b> {status}")
    lines += [
        "",
        f"🕒 <b>Rendered:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    return "\n".join(lines)


def _developer_text(bot):
    name = _developer_name()
    username = _developer_username()
    url = _developer_url()
    support = _support_url()
    support_channel = _support_channel()
    main_group_link = _main_group_link()
    github = _github_url()
    other = _other_projects()

    lines = [
        "<b>👨‍💻 DEVELOPER</b>",
        "",
    ]
    if name:
        lines.append(f"👤 <b>Name:</b> {html.escape(name)}")
    if username:
        lines.append(f"🔗 <b>Username:</b> @{html.escape(username)}")
    if url:
        lines.append(f"🔗 <b>Profile:</b> {html.escape(url)}")
    if support:
        lines.append(f"💬 <b>Support contact:</b> {html.escape(support)}")
    if main_group_link:
        group_name = _main_group_name(bot, main_group_link)
        lines.append(f"👥 <b>Support group:</b> {html.escape(group_name)} — {html.escape(main_group_link)}")
    if github:
        lines.append(f"🐙 <b>GitHub:</b> {html.escape(github)}")
    if other:
        lines.append(f"🚀 <b>Other projects:</b> {html.escape(other)}")
    if len(lines) <= 2 and not support_channel:
        lines.append("ℹ️ No developer information configured yet.")
        lines.append("   Set DEVELOPER_NAME, DEVELOPER_USERNAME, DEVELOPER_URL, SUPPORT_URL,")
        lines.append("   SUPPORT_CHANNEL, GITHUB_URL and OTHER_PROJECTS env vars — and /setgroup for the")
        lines.append("   support group — to display it here.")
    else:
        lines.append("")
        lines.append("<i>Only configured destinations are shown as links.</i>")
    return "\n".join(lines)


def _developer_markup(bot):
    rows = []
    url = _developer_url()
    username = _developer_username()
    support = _support_url()
    support_channel = _support_channel()
    main_group_link = _main_group_link()
    github = _github_url()
    if url:
        rows.append([InlineKeyboardButton("🔗 Open profile", url=url, style=_STYLE_PRIMARY)])
    elif username:
        rows.append([InlineKeyboardButton("👤 Open @" + username, url=f"https://t.me/{username}", style=_STYLE_PRIMARY)])
    if support:
        rows.append([InlineKeyboardButton("💬 Support contact", url=support, style=_STYLE_PRIMARY)])
    if support_channel:
        rows.append([InlineKeyboardButton("📢 Support Channel", url=f"https://t.me/{support_channel}", style=_STYLE_PRIMARY)])
    if main_group_link:
        rows.append([InlineKeyboardButton("👥 Support Group", url=main_group_link, style=_STYLE_PRIMARY)])
    if github:
        rows.append([InlineKeyboardButton("🐙 GitHub repository", url=github, style=_STYLE_PRIMARY)])
    rows += _nav_buttons(parent="main", main=True)
    return _markup_buttons(rows)


def _updates_text():
    version = _bot_version()
    changelog = _update_changelog()
    lines = [
        "<b>📢 UPDATES</b>",
        "",
        (f"📦 <b>Current version:</b> {html.escape(version)}" if version else "📦 <b>Current version:</b> Not configured"),
        "",
        "🔔 <b>Release notes:</b>",
    ]
    if changelog:
        lines.append(changelog)
    else:
        lines.append(
            "No configured changelog — release notes live in the project repository. "
            "Check the repository's CHANGELOG / Releases page."
        )
    lines.append("")
    lines.append("<i>Never fake release notes: this page shows only what is actually configured or released.</i>")
    return "\n".join(lines)


def _updates_markup(bot):
    rows = []
    channel_url = _update_channel_url()
    if channel_url:
        if channel_url.startswith("@") or re.fullmatch(r"[A-Za-z][A-Za-z0-9_\d\.]{3,31}", channel_url):
            rows.append([InlineKeyboardButton("📢 Open updates channel", url=f"https://t.me/{channel_url.lstrip('@')}", style=_STYLE_PRIMARY)])
        else:
            rows.append([InlineKeyboardButton("📢 Open updates channel", url=channel_url, style=_STYLE_PRIMARY)])
    rows += _nav_buttons(parent="main", main=True)
    return _markup_buttons(rows)


# ─────────────────────────────────────────────────────────────
# Edit / send helpers
# ─────────────────────────────────────────────────────────────

def _is_media_message(message):
    if not message:
        return False
    return bool(getattr(message, "photo", None) or getattr(message, "video", None) or getattr(message, "animation", None))


def _edit_message_text_new(bot, call, text, markup):
    """Edit the existing message in place; fall back to a fresh message."""
    message = call.message
    try:
        if message is not None and _is_media_message(message):
            try:
                bot.edit_message_caption(
                    caption=text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
                return
            except Exception:
                pass
        if message is not None:
            bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
    except Exception as e:
        logger.warning(f"edit_message failed, sending new message: {e}")
    # Final fallback — the message could not be edited (too old, deleted)
    try:
        bot.send_message(call.message.chat.id if call.message else call.from_user.id, text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass


def _answer_and_edit(bot, call, text, markup):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _edit_message_text_new(bot, call, text, markup)


# ─────────────────────────────────────────────────────────────
# Main / commands / help builders used by handlers
# ─────────────────────────────────────────────────────────────

def build_main_menu_message(first_name="there"):
    return build_main_menu_text(first_name)


def _main_menu_markup(bot):
    return build_main_menu_markup(bot)


# ─────────────────────────────────────────────────────────────
# /start handler — welcome media + dashboard (private chat)
# ─────────────────────────────────────────────────────────────

def send_start_dashboard(bot, message):
    """Send the interactive dashboard in private chat.

    Media is optional — if WELCOME_MEDIA is unset, or the media send fails,
    a text dashboard is sent instead.
    """
    first_name = getattr(message.from_user, "first_name", "there") if message.from_user else "there"
    text = build_main_menu_text(first_name)
    markup = _main_menu_markup(bot)

    media = _welcome_media()
    media_type = _welcome_media_type()

    if media:
        try:
            if media_type == "video":
                bot.send_video(message.chat.id, media, caption=text, reply_markup=markup, parse_mode="HTML")
                return
            if media_type == "animation":
                bot.send_animation(message.chat.id, media, caption=text, reply_markup=markup, parse_mode="HTML")
                return
            bot.send_photo(message.chat.id, media, caption=text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception as e:
            logger.warning(f"Welcome media send failed, falling back to text: {e}")

    try:
        bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to send dashboard: {e}")


# ─────────────────────────────────────────────────────────────
# Callback handlers
# ─────────────────────────────────────────────────────────────

def register_dashboard_handlers(bot):
    """Register all dashboard callback handlers with the bot."""

    @bot.callback_query_handler(func=lambda call: call.data.startswith("dm:"))
    def handle_dashboard(call):
        # Drop duplicate deliveries of the same callback_query
        try:
            if _is_duplicate_callback(call.id):
                return
        except Exception:
            pass
        # Register / refresh the interacting user — upsert, no duplicates.
        # A DB hiccup here must never break the button.
        try:
            if call.from_user:
                db.ensure_user(
                    call.from_user.id,
                    name=getattr(call.from_user, "first_name", None) or "Unknown",
                    username=getattr(call.from_user, "username", None),
                    first_name=getattr(call.from_user, "first_name", None),
                    last_name=getattr(call.from_user, "last_name", None),
                    is_bot=bool(getattr(call.from_user, "is_bot", False)),
                )
        except Exception:
            pass
        try:
            parts = (call.data or "").split(":", 2)
            action = parts[1] if len(parts) >= 2 else ""

            # Commands index
            if action == "commands":
                _answer_and_edit(bot, call, _commands_index_text(), _commands_index_markup())
                return

            # Command category page + pagination
            if action == "cat":
                cat = parts[2].split(":")[0] if len(parts) >= 3 else ""
                try:
                    page = int(parts[2].split(":")[1]) if len(parts) >= 3 and ":" in parts[2] else 0
                except Exception:
                    page = 0
                _answer_and_edit(bot, call, _commands_category_text(cat, page), _commands_category_markup(cat, page))
                return

            # Help index
            if action == "help":
                _answer_and_edit(bot, call, _HELP_INDEX_TEXT, _help_index_markup())
                return

            # Help page
            if action == "helpcat":
                fkey = parts[2] if len(parts) >= 3 else ""
                label_map = {
                    "start": "🚀 Getting Started", "add": "➕ Add to Group",
                    "config": "⚙️ Group Configuration", "admin": "👮 Admin Permissions",
                    "moderation": "🛡️ Moderation Guide", "purge": "🧹 Purge Guide",
                    "activity": "📊 Chat Activity", "ttt": "🎮 Tic-Tac-Toe",
                    "trouble": "🔧 Troubleshooting", "faq": "❓ FAQ",
                }
                title = label_map.get(fkey, "❓ Help")
                text = f"<b>{title}</b>\n\n{_help_page_text(fkey)}"
                _answer_and_edit(bot, call, text, _help_page_markup(fkey))
                return

            # About
            if action == "about":
                _answer_and_edit(bot, call, _about_text(bot), _markup_buttons(_nav_buttons(parent="main", main=True)))
                return

            # Developer
            if action == "dev":
                _answer_and_edit(bot, call, _developer_text(bot), _developer_markup(bot))
                return

            # Updates
            if action == "updates":
                _answer_and_edit(bot, call, _updates_text(), _updates_markup(bot))
                return

            # Main menu / back
            if action in ("main", "back"):
                if action == "main":
                    target = "main"
                else:
                    target = parts[2] if len(parts) >= 3 else "main"
                if target == "commands":
                    _answer_and_edit(bot, call, _commands_index_text(), _commands_index_markup())
                    return
                if target == "help":
                    _answer_and_edit(bot, call, _HELP_INDEX_TEXT, _help_index_markup())
                    return
                # back to main
                _answer_and_edit(bot, call, build_main_menu_text(
                    getattr(call.from_user, "first_name", "there")),
                    _main_menu_markup(bot))
                return

            # Fallback for unknown dm actions
            try:
                bot.answer_callback_query(call.id, "⚠️ This button is not valid right now.", show_alert=True)
            except Exception:
                pass

        except Exception as e:
            logger.warning(f"Dashboard callback failed: {e}")
            try:
                bot.answer_callback_query(call.id, "⚠️ Couldn't process that button — please try again.", show_alert=True)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# Group setup-instructions message (sent when the bot is added)
# ─────────────────────────────────────────────────────────────

def send_join_instructions(bot, chat_id):
    """Short, honest setup instructions when the bot joins a group."""
    name = _bot_name()
    try:
        bot.send_message(
            chat_id,
            f"👋 Thanks for adding <b>{html.escape(name)}</b>!\n\n"
            "Next steps:\n"
            "1️⃣ Press <b>/start</b> in a DM to open the interactive dashboard.\n"
            "2️⃣ If you want moderation features, make the bot an <b>Administrator</b> "
            "with the rights you need — the bot never changes permissions by itself.\n"
            "3️⃣ Open ⚙️ Commands on the dashboard to explore everything.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not send join instructions for {chat_id}: {e}")
