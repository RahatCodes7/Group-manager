# 🚀 Group Manager — VPS / Render Deployment Guide

PostgreSQL-only setup. No SQLite files are used anywhere — every feature
(users, groups, warnings, bans, chat activity, tictactoe, sync) runs on
PostgreSQL via `DATABASE_URL`.

## 🔑 What you need (credentials via env vars only)

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ (or DB) | Your Telegram bot token (from @BotFather) |
| `DATABASE_URL` | ✅ | PostgreSQL URL, e.g. `postgresql://user:pass@host:5432/dbname` |
| `OWNER_USERNAME` | ✅ | Your Telegram username, without `@` (owner of the bot) |
| `ADMIN_PASSWORD` | optional | Dashboard password; without it the dashboard is open |
| `SUPPORT_CHANNEL` | optional | Support channel username, without `@` |
| `PUBLIC_URL` | optional | Public base URL of the app (e.g. `https://your-app.onrender.com`) |
| `CHAT_ACTIVITY_ENABLED` | optional | `1` (default) or `0` |
| `CHAT_ACTIVITY_TIMEZONE` | optional | IANA zone, default `Asia/Dhaka` |

> ⚠️ **Never hardcode credentials in source files.** `DATABASE_URL` and
> `BOT_TOKEN` are read exclusively from your environment (or a local `.env`
> file that is git-ignored). The bot will NOT work without a valid
> `DATABASE_URL`.

## 🌐 Step 1 — Install Dependencies

```bash
pip install -r requirements.txt
```

## 🚀 Step 2 — Start the Engine

Unified ASGI app (FastAPI dashboard + bot polling in the same process):

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Dashboard: `http://localhost:8000`

### 🐳 Docker

The repo ships a production-ready multi-stage Dockerfile (build → slim
runtime), a non-root runtime user, a `/health` HEALTHCHECK and a
`$PORT`-aware CMD:

```bash
docker build -t group-manager .
docker run -d -p 8000:8000 \
  -e BOT_TOKEN=... \
  -e DATABASE_URL=postgresql://user:pass@host:5432/dbname \
  -e OWNER_USERNAME=your_username \
  group-manager
```

The container listens on `$PORT` (default `8000`) and exposes the public
`/health` endpoint. Set the same env vars as the Render/VPS path — all
credentials stay in env vars, never in the image.

## 🗄️ Step 3 — PostgreSQL Migration & Backups

The schema is created and migrated automatically on first run — just point
`DATABASE_URL` at an empty database.

For backups use the standard PostgreSQL tools (the app is PG-only and does
not create or export SQLite files):

```bash
# Backup
PGPASSWORD=... pg_dump -h host -p 5432 -U postgres -d dbname > backup.sql

# Restore
PGPASSWORD=... psql -h host -p 5432 -U postgres -d dbname < backup.sql
```

## 🔄 Step 4 — First Deploy

1. Set `BOT_TOKEN` + `DATABASE_URL` (+ optional vars) in Render/VPS env.
2. Start the app. The dashboard auto-starts the bot and all tables are created.
3. Add the bot to your group as an admin — it auto-registers the group and
   members from join/leave events.

## ⚠️ Known Telegram Limitations

The Telegram Bot API cannot enumerate the full membership of a group. The
bot registers exactly the members it sees via `chat_member` /
`new_chat_members` / `left_chat_member` events, and keeps that list in sync.
It never claims to have imported every member of a group.
