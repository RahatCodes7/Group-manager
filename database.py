"""PostgreSQL data layer for the Group Manager.

This module is the single source of truth for the bot and dashboard.
PostgreSQL is the ONLY supported database — there is no SQLite fallback,
no local .db files, and no in-memory storage outside the small TTL caches.

Credentials are read from the DATABASE_URL environment variable (or a local
.env file — never hardcoded in this file or anywhere else).
"""

import contextlib
import os
import sys
import json
import threading
import time as _time
from datetime import datetime, timedelta, timezone

# Load .env so DATABASE_URL is picked up even when running the bot standalone
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.pool import ThreadedConnectionPool
    _PG_AVAILABLE = True
except Exception:
    psycopg2 = None
    RealDictCursor = None
    ThreadedConnectionPool = None
    _PG_AVAILABLE = False

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Fix Windows encoding for emoji print statements
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


# ------------------------------------------------------------------
# Connection wrapper — tiny facade that keeps the app's SQLite-style
# surface (.execute/.cursor/.commit/.rollback) over a pooled PG conn.
# ------------------------------------------------------------------
class _Conn:

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=None):
        cur = self.raw.cursor()
        if params is not None:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return cur

    def cursor(self):
        return self.raw.cursor(cursor_factory=RealDictCursor)

    def commit(self):
        try:
            self.raw.commit()
        except Exception:
            pass

    def rollback(self):
        try:
            self.raw.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass

    def set_autocommit(self, value):
        self.raw.autocommit = bool(value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ------------------------------------------------------------------
# In-memory TTL caches (hot reads only — NOT a database)
# ------------------------------------------------------------------
class _Cache:
    """Thread-safe TTL cache for hot data (groups, config)."""

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

    def invalidate(self, key):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()


_group_cache = _Cache(ttl=15)
_config_cache = _Cache(ttl=60)


class Database:
    """PostgreSQL-backed database with a lazy per-thread connection pool."""


    @contextlib.contextmanager
    def _read_guard(self):
        """Run a read-path block and guarantee the per-thread pooled connection
        is returned with NO open transaction left behind, so DDL (e.g. the
        idempotent migration) and other connections never block on a stale
        read lock."""
        try:
            yield
        finally:
            try:
                self.conn.rollback()
            except Exception:
                pass

    def __init__(self, db_path=None):
        # db_path is accepted for backward compatibility and ignored —
        # PostgreSQL is the only database; SQLite files are never created.
        self.lock = threading.RLock()
        self._pool = None
        self._local = threading.local()
        if self._connect():
            try:
                self._migrate()
            except Exception as e:
                print(f"❌ Database migration failed: {e}")
        # No SQLite fallback — this database is PostgreSQL-only.

    # ----------------------------------------------------------
    # Pool lifecycle
    # ----------------------------------------------------------
    def _ensure_pool(self):
        if not _PG_AVAILABLE:
            raise RuntimeError("psycopg2 is not installed. Install psycopg2-binary.")
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. PostgreSQL is the only supported database: "
                "set DATABASE_URL in the environment or in .env (e.g. "
                "postgresql://user:password@host:5432/database)."
            )
        if self._pool is None:
            self._pool = ThreadedConnectionPool(
                2, 12,
                DATABASE_URL,
                connect_timeout=15,
            )
        return self._pool

    def _connect(self):
        try:
            pool = self._ensure_pool()
            conn = pool.getconn()
            try:
                conn.cursor().execute("SELECT 1")
                conn.commit()
            finally:
                pool.putconn(conn)
            print("✅ PostgreSQL database connected successfully.")
            return True
        except Exception as e:
            try:
                if self._pool is not None:
                    self._pool.closeall()
            except Exception:
                pass
            self._pool = None
            self._local = threading.local()
            print(f"❌ PostgreSQL connection failed: {e}")
            return False

    def _check_conn(self):
        with self.lock:
            if self._pool is None:
                if not self._connect():
                    return False
            try:
                conn = self._pool.getconn()
                try:
                    conn.cursor().execute("SELECT 1")
                    conn.rollback()
                    return True
                finally:
                    self._pool.putconn(conn)
            except Exception:
                try:
                    self._pool.closeall()
                except Exception:
                    pass
                self._pool = None
                self._local = threading.local()
                return self._connect()

    @property
    def conn(self):
        """Per-thread checked-out connection (non-autocommit; explicit
        commit()/rollback() calls in every public method keep each statement
        atomic). Tictactoe's begin_atomic() pattern is still fully supported.
        """
        pool = self._ensure_pool()
        conn = getattr(self._local, 'conn', None)
        if conn is None or conn.raw is None:
            conn = _Conn(pool.getconn())
            conn.raw.autocommit = False
            self._local.conn = conn
        return conn

    def close(self):
        """Return this thread's checked-out connection to the pool."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.raw.rollback()
            except Exception:
                pass
            try:
                self._pool.putconn(conn.raw)
            except Exception:
                try:
                    conn.raw.close()
                except Exception:
                    pass
            self._local.conn = None

    def close_all(self):
        """Release every pooled connection (called at shutdown)."""
        self.close()
        pool = self._pool
        self._pool = None
        self._local = threading.local()
        if pool is not None:
            try:
                pool.closeall()
            except Exception:
                pass

    # ----------------------------------------------------------
    # Schema migration (PostgreSQL only)
    # ----------------------------------------------------------
    def _migrate(self):
        if not self._check_conn():
            return
        try:
            self.conn.rollback()
        except Exception:
            pass
        with self.lock:
            try:
                c = self.conn.cursor()
                self._migrate_schema(c)
                self.conn.commit()
                print("✅ PostgreSQL migration check complete.")
            except Exception as e:
                print(f"❌ PostgreSQL migration failed: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass

    def _migrate_schema(self, c):
        """Create/alter the full PostgreSQL schema (idempotent)."""

        # ── CONFIG ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # ── USERS ──
        # username has NO UNIQUE constraint — usernames can be recycled.
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                username TEXT,
                warnings INTEGER DEFAULT 0,
                role TEXT DEFAULT 'member',
                first_seen TEXT,
                reputation INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                banned_reason TEXT,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                msg_count INTEGER DEFAULT 0,
                last_group_id TEXT,
                last_group_name TEXT,
                join_count INTEGER DEFAULT 0,
                first_name TEXT,
                last_name TEXT,
                is_bot INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col in [
            "first_name TEXT",
            "last_name TEXT",
            "is_bot INTEGER DEFAULT 0",
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        ]:
            c.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active DESC)")
        # -- the global telegram id is the table PK already (users.user_id is unique).

        # ── GROUPS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                chat_id TEXT PRIMARY KEY,
                name TEXT DEFAULT 'Unknown Group',
                rules TEXT DEFAULT 'No rules set yet...',
                welcome_message TEXT DEFAULT 'Welcome {name}! 👋',
                welcome_type TEXT DEFAULT 'text',
                welcome_file_id TEXT DEFAULT '',
                leave_message TEXT DEFAULT 'Goodbye {name}!',
                leave_type TEXT DEFAULT 'text',
                leave_file_id TEXT DEFAULT '',
                antispam INTEGER DEFAULT 0,
                antispam_auto_delete_links INTEGER DEFAULT 1,
                approve_mode INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                member_count INTEGER DEFAULT 0,
                filter_count INTEGER DEFAULT 0,
                language TEXT DEFAULT 'en',
                strict_mode INTEGER DEFAULT 0,
                log_channel_id TEXT,
                max_warnings INTEGER DEFAULT 3,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                slow_mode INTEGER DEFAULT 0,
                linked_channel TEXT,
                slow_mode_delay INTEGER DEFAULT 0,
                captcha INTEGER DEFAULT 0,
                captcha_mode TEXT DEFAULT 'button',
                captcha_rules INTEGER DEFAULT 0,
                captcha_mute_time TEXT DEFAULT '',
                captcha_kick INTEGER DEFAULT 0,
                captcha_kick_time TEXT DEFAULT '',
                captcha_text TEXT DEFAULT 'Click to prove you are human',
                reports INTEGER DEFAULT 1,
                username TEXT,
                group_type TEXT,
                bot_is_admin INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_groups_last_active ON groups(last_active DESC)")
        for col in [
            "captcha INTEGER DEFAULT 0",
            "captcha_mode TEXT DEFAULT 'button'",
            "captcha_rules INTEGER DEFAULT 0",
            "captcha_mute_time TEXT DEFAULT ''",
            "captcha_kick INTEGER DEFAULT 0",
            "captcha_kick_time TEXT DEFAULT ''",
            "captcha_text TEXT DEFAULT 'Click to prove you are human'",
            "reports INTEGER DEFAULT 1",
            "chat_tracking INTEGER DEFAULT 1",
            "user_milestones INTEGER DEFAULT 1",
            "group_milestones INTEGER DEFAULT 1",
            "leaderboard INTEGER DEFAULT 1",
            "username TEXT",
            "group_type TEXT",
            "bot_is_admin INTEGER DEFAULT 0",
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "keyword_alert INTEGER DEFAULT 0",
            "keyword_alert_words TEXT",
            "warn_mode TEXT DEFAULT 'ban'",
            "warn_time INTEGER DEFAULT 0",
            "lock_sticker INTEGER DEFAULT 0",
            "lock_animation INTEGER DEFAULT 0",
            "lock_media INTEGER DEFAULT 0",
            "lock_url INTEGER DEFAULT 0",
            "lock_forward INTEGER DEFAULT 0",
            "lock_inline INTEGER DEFAULT 0",
            "lock_poll INTEGER DEFAULT 0",
            "lock_game INTEGER DEFAULT 0",
            "lock_chat_off INTEGER DEFAULT 0",
            "tictactoe INTEGER DEFAULT 1",
        ]:
            c.execute(f"ALTER TABLE groups ADD COLUMN IF NOT EXISTS {col}")

        # ── GROUP MEMBERS ──
        # Link table: which telegram users we know are in which registered group.
        c.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                id BIGSERIAL PRIMARY KEY,
                telegram_user_id TEXT NOT NULL,
                telegram_group_id TEXT NOT NULL,
                membership_status TEXT NOT NULL DEFAULT 'member',
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_user_id, telegram_group_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_gm_group ON group_members(telegram_group_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_gm_user ON group_members(telegram_user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_gm_status ON group_members(telegram_group_id, membership_status)")

        # ── SYNC JOBS (persisted job state for the sync engine) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS sync_jobs (
                id BIGSERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                members_before INTEGER DEFAULT 0,
                members_synced INTEGER DEFAULT 0,
                members_total INTEGER,
                error TEXT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sync_jobs_chat ON sync_jobs(chat_id, created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status)")
        # Atomic duplicate-prevention: only one pending/running job per group.
        try:
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_jobs_active_per_group
                ON sync_jobs(chat_id) WHERE status IN ('pending', 'running')
            """)
        except Exception:
            pass  # partial index already exists or failed — the INSERT ON CONFLICT path re-checks anyway

        # ── CHAT ACTIVITY / RANKINGS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_user_stats (
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT DEFAULT 'Unknown',
                username TEXT, total_messages INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
                PRIMARY KEY (group_id, user_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cus_group ON chat_user_stats(group_id, total_messages DESC)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_daily_stats (
                group_id TEXT NOT NULL, date TEXT NOT NULL, total_messages INTEGER DEFAULT 0,
                PRIMARY KEY (group_id, date)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cds_group_date ON chat_daily_stats(group_id, date)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_user_daily_stats (
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, date TEXT NOT NULL,
                total_messages INTEGER DEFAULT 0, PRIMARY KEY (group_id, user_id, date)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cuds_group_date ON chat_user_daily_stats(group_id, date, total_messages DESC)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_user_weekly_stats (
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, week_start TEXT NOT NULL,
                total_messages INTEGER DEFAULT 0, PRIMARY KEY (group_id, user_id, week_start)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cuws_group_week ON chat_user_weekly_stats(group_id, week_start, total_messages DESC)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_milestones (
                id BIGSERIAL PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                milestone INTEGER NOT NULL, achieved_at TEXT,
                UNIQUE(group_id, user_id, milestone)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cm_group_user ON chat_milestones(group_id, user_id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_group_milestones (
                id BIGSERIAL PRIMARY KEY, group_id TEXT NOT NULL, date TEXT NOT NULL,
                milestone INTEGER NOT NULL, achieved_at TEXT,
                UNIQUE(group_id, date, milestone)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cgm_group_date ON chat_group_milestones(group_id, date)")

        # ── APPROVED USERS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS approved_users (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
                approved_by TEXT, approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_approved_users_chat_id ON approved_users(chat_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_approved_users_user_id ON approved_users(user_id)")

        # ── PENDING CAPTCHAS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_captchas (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
                captcha_type TEXT, correct_answer TEXT,
                join_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_captchas_chat_id ON pending_captchas(chat_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_captchas_user_id ON pending_captchas(user_id)")

        # ── FILTERS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS filters (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL,
                trigger TEXT NOT NULL, filter_data TEXT NOT NULL,
                UNIQUE(chat_id, trigger)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_filters_chat_id ON filters(chat_id)")

        # ── BAD WORDS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS bad_words (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, word TEXT NOT NULL,
                UNIQUE(chat_id, word)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bad_words_chat_id ON bad_words(chat_id)")

        # ── LOGS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id BIGSERIAL PRIMARY KEY, event TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp DESC)")

        # ── GLOBAL BANS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS global_bans (
                user_id TEXT PRIMARY KEY, reason TEXT, banned_by TEXT,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── USER MUTES ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_mutes (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
                muted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, unmute_at TIMESTAMP NOT NULL,
                reason TEXT, muted_by TEXT, is_active INTEGER DEFAULT 1,
                UNIQUE(chat_id, user_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_chat_id ON user_mutes(chat_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_user_id ON user_mutes(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_mutes_unmute_at ON user_mutes(unmute_at)")

        # ── USER BANS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_bans (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, unban_at TIMESTAMP,
                reason TEXT, banned_by TEXT, is_active INTEGER DEFAULT 1,
                UNIQUE(chat_id, user_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_chat_id ON user_bans(chat_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_user_id ON user_bans(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_bans_unban_at ON user_bans(unban_at)")

        # ── INFRACTION HISTORY ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS infraction_history (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
                action_type TEXT NOT NULL, duration_seconds INTEGER,
                reason TEXT, issued_by TEXT,
                issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, resolved_at TIMESTAMP, resolved_by TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_infraction_chat_user ON infraction_history(chat_id, user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_infraction_issued_at ON infraction_history(issued_at DESC)")

        # ── SCHEDULED MESSAGES ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, message TEXT NOT NULL,
                scheduled_at TIMESTAMP NOT NULL, message_type TEXT DEFAULT 'text',
                file_id TEXT, status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, scheduled_at)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_messages_chat_id ON scheduled_messages(chat_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_messages_scheduled_at ON scheduled_messages(scheduled_at)")

        # ── USER STATS ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id TEXT PRIMARY KEY,
                messages_sent INTEGER DEFAULT 0, links_shared INTEGER DEFAULT 0,
                warnings_received INTEGER DEFAULT 0, infractions_count INTEGER DEFAULT 0,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_user_id ON user_stats(user_id)")

        # ── TIC TAC TOE ──
        c.execute("""CREATE TABLE IF NOT EXISTS ttt_lobbies (
            game_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, message_id INTEGER,
            player1_id TEXT NOT NULL, player1_name TEXT NOT NULL,
            player2_id TEXT, player2_name TEXT, board TEXT DEFAULT '.........',
            current_turn TEXT DEFAULT 'X', status TEXT DEFAULT 'WAITING_FOR_PLAYER',
            created_at TEXT, updated_at TEXT, expires_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ttt_lobby_chat ON ttt_lobbies(chat_id, status)")
        c.execute("""CREATE TABLE IF NOT EXISTS ttt_games (
            game_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, message_id INTEGER,
            player1_id TEXT NOT NULL, player1_name TEXT NOT NULL,
            player2_id TEXT, player2_name TEXT, board TEXT DEFAULT '.........',
            current_turn TEXT DEFAULT 'X', status TEXT DEFAULT 'ACTIVE',
            winner TEXT, created_at TEXT, updated_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ttt_game_chat ON ttt_games(chat_id, status)")
        c.execute("""CREATE TABLE IF NOT EXISTS ttt_scores (
            chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
            games_played INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0,
            total_points INTEGER DEFAULT 0, consecutive_wins INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ttt_score_chat ON ttt_scores(chat_id, total_points DESC)")

        # ── WARNING SYSTEM ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS warn_history (
                id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
                reason TEXT DEFAULT '', issued_by TEXT, issued_at INTEGER DEFAULT 0, active INTEGER DEFAULT 1
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_warn_history_chat_user ON warn_history(chat_id, user_id, active)")

    def get_config(self):
        with self._read_guard():
            cached = _config_cache.get("config")
            if cached is not None:
                return dict(cached)

            if not self._check_conn():
                return {}
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT key, value FROM config")
                    conf = {
                        "bot_token": "",
                        "is_running": False,
                        "owner_username": "",
                        "support_channel": "",
                        "main_group_link": ""
                    }
                    for row in c.fetchall():
                        try:
                            conf[row['key']] = json.loads(row['value'])
                        except:
                            conf[row['key']] = row['value']

                    # Environment variable overrides (if set)
                    env_token   = os.getenv('BOT_TOKEN')
                    env_owner   = os.getenv('OWNER_USERNAME')
                    env_running = os.getenv('IS_RUNNING')
                    env_support = os.getenv('SUPPORT_CHANNEL')

                    if env_token: conf["bot_token"] = env_token
                    if env_owner: conf["owner_username"] = env_owner.replace("@", "")
                    if env_running and env_running.lower() in ("1", "true", "yes"): conf["is_running"] = True
                    if env_support: conf["support_channel"] = env_support.replace("@", "")

                    _config_cache.set("config", conf)
                    return conf
                except Exception as e:
                    print(f"Error in get_config: {e}")
                    return {}

    def update_config(self, key, value):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value))
                )
                self.conn.commit()
                _config_cache.invalidate("config")
            except Exception as e:
                print(f"Error in update_config: {e}")

    def ensure_group(self, chat_id, name=None):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT INTO groups (chat_id, name, max_warnings, strict_mode, language) VALUES (%s, %s, 3, 0, 'en') ON CONFLICT(chat_id) DO NOTHING",
                    (str_id, name or 'Unknown Group')
                )
                if name:
                    c.execute(
                        "UPDATE groups SET name=%s WHERE chat_id=%s AND (name IS NULL OR name='Unknown Group')",
                        (name, str_id)
                    )
                self.conn.commit()
            except Exception as e:
                print(f"Error in ensure_group: {e}")

    def get_group(self, chat_id):
        with self._read_guard():
            str_id = str(chat_id)
            cached = _group_cache.get(f"group:{str_id}")
            if cached is not None:
                return dict(cached)

            if not self._check_conn():
                return {}
            self.ensure_group(chat_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM groups WHERE chat_id=%s", (str_id,))
                    row = c.fetchone()
                    if not row:
                        return {}

                    group = dict(row)
                    # Filters
                    c.execute("SELECT trigger, filter_data FROM filters WHERE chat_id=%s", (str_id,))
                    group["filters"] = {r['trigger']: json.loads(r['filter_data']) for r in c.fetchall()}
                    # Bad Words
                    c.execute("SELECT word FROM bad_words WHERE chat_id=%s", (str_id,))
                    group["bad_words"] = [r['word'] for r in c.fetchall()]

                    _group_cache.set(f"group:{str_id}", group)
                    return group
                except Exception as e:
                    print(f"Error in get_group: {e}")
                    return {}

    def update_group_setting(self, chat_id, key, value):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                if key == "bad_words":
                    c.execute("DELETE FROM bad_words WHERE chat_id=%s", (str_id,))
                    for w in value:
                        c.execute("INSERT INTO bad_words (chat_id, word) VALUES (%s, %s) ON CONFLICT(chat_id, word) DO NOTHING", (str_id, w.lower()))
                elif key == "filters":
                    c.execute("DELETE FROM filters WHERE chat_id=%s", (str_id,))
                    for trigger, data in value.items():
                        c.execute("INSERT INTO filters (chat_id, trigger, filter_data) VALUES (%s, %s, %s)",
                                (str_id, trigger.lower(), json.dumps(data)))
                else:
                    valid_keys = {"antispam", "antispam_auto_delete_links", "rules", "name", "welcome_message",
                                "welcome_type", "welcome_file_id", "leave_message", "leave_type", "leave_file_id",
                                "strict_mode", "max_warnings", "language", "member_count", "message_count",
                                "filter_count", "approve_mode", "slow_mode", "linked_channel", "slow_mode_delay",
                                "log_channel_id", "captcha", "captcha_mode", "captcha_rules", "captcha_mute_time",
                                "captcha_kick", "captcha_kick_time", "captcha_text",
                                "chat_tracking", "user_milestones", "group_milestones", "leaderboard",
                                "keyword_alert", "keyword_alert_words",
                                "lock_sticker", "lock_animation", "lock_media", "lock_url", "lock_forward", "lock_inline", "lock_poll", "lock_game", "lock_chat_off", "tictactoe",
                                "warn_mode", "warn_time", "reports"}
                    if key in valid_keys:
                        try:
                            c.execute(f"UPDATE groups SET {key}=%s WHERE chat_id=%s", (value, str_id))
                        except Exception as upd_e:
                            msg = str(upd_e).lower()
                            if "no such column" in msg or "does not exist" in msg or "column" in msg:
                                # Auto-migrate missing column then retry once
                                try:
                                    # Try PG syntax first, fallback to SQLite
                                    try:
                                        c.execute(f"ALTER TABLE groups ADD COLUMN IF NOT EXISTS {key} INTEGER DEFAULT 0")
                                    except:
                                        c.execute(f"ALTER TABLE groups ADD COLUMN {key} INTEGER DEFAULT 0")
                                    self.conn.commit()
                                    c.execute(f"UPDATE groups SET {key}=%s WHERE chat_id=%s", (value, str_id))
                                    print(f"Auto-migrated column {key} and updated")
                                except Exception as e2:
                                    print(f"Auto-migrate failed for {key}: {e2} / original: {upd_e}")
                                    raise
                            else:
                                raise
                self.conn.commit()
                _group_cache.invalidate(f"group:{str_id}")
            except Exception as e:
                print(f"Error in update_group_setting: {e}")


    # â”€â”€ CAPTCHA MANAGEMENT â”€â”€
    def add_pending_captcha(self, chat_id, user_id, captcha_type, correct_answer):
        if not self._check_conn(): return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT INTO pending_captchas (chat_id, user_id, captcha_type, correct_answer, join_time) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT(chat_id, user_id) DO UPDATE SET captcha_type=excluded.captcha_type, correct_answer=excluded.correct_answer, join_time=excluded.join_time",
                    (str(chat_id), str(user_id), captcha_type, str(correct_answer))
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error add_pending_captcha: {e}")

    def get_pending_captcha(self, chat_id, user_id):
        with self._read_guard():
            if not self._check_conn(): return None
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM pending_captchas WHERE chat_id=%s AND user_id=%s", (str(chat_id), str(user_id)))
                    row = c.fetchone()
                    return dict(row) if row else None
                except Exception as e:
                    print(f"Error get_pending_captcha: {e}")
                    return None

    def remove_pending_captcha(self, chat_id, user_id):
        if not self._check_conn(): return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM pending_captchas WHERE chat_id=%s AND user_id=%s", (str(chat_id), str(user_id)))
                self.conn.commit()
            except Exception as e:
                print(f"Error remove_pending_captcha: {e}")

    def get_all_pending_captchas(self):
        with self._read_guard():
            if not self._check_conn(): return []
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM pending_captchas")
                    return [dict(row) for row in c.fetchall()]
                except Exception as e:
                    print(f"Error get_all_pending_captchas: {e}")
                    return []

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # APPROVAL SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def is_user_approved(self, chat_id, user_id):
        with self._read_guard():
            if not self._check_conn():
                return True  # fail-open
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT 1 FROM approved_users WHERE chat_id=%s AND user_id=%s", (str(chat_id), str(user_id)))
                    return c.fetchone() is not None
                except:
                    return True

    def approve_user(self, chat_id, user_id, approved_by=None):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT INTO approved_users (chat_id, user_id, approved_by) VALUES (%s, %s, %s) ON CONFLICT(chat_id, user_id) DO NOTHING",
                    (str(chat_id), str(user_id), str(approved_by) if approved_by else None)
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error in approve_user: {e}")

    def disapprove_user(self, chat_id, user_id):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM approved_users WHERE chat_id=%s AND user_id=%s", (str(chat_id), str(user_id)))
                self.conn.commit()
            except Exception as e:
                print(f"Error in disapprove_user: {e}")

    def get_approved_users(self, chat_id):
        with self._read_guard():
            if not self._check_conn():
                return []
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT user_id, approved_by, approved_at FROM approved_users WHERE chat_id=%s ORDER BY approved_at DESC", (str(chat_id),))
                    return [dict(r) for r in c.fetchall()]
                except:
                    return []

    def add_filter(self, chat_id, trigger, filter_data):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "INSERT INTO filters (chat_id, trigger, filter_data) VALUES (%s, %s, %s) ON CONFLICT(chat_id, trigger) DO UPDATE SET filter_data=excluded.filter_data",
                    (str_id, trigger.lower(), json.dumps(filter_data))
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error in add_filter: {e}")

    def remove_filter(self, chat_id, trigger):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM filters WHERE chat_id=%s AND trigger=%s", (str_id, trigger.lower()))
                self.conn.commit()
            except Exception as e:
                print(f"Error in remove_filter: {e}")

    def get_user_info(self, user_id):
        with self._read_guard():
            if not self._check_conn():
                return {}
            str_id = str(user_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT name, username, reputation, is_banned, banned_reason, warnings FROM users WHERE user_id=%s", (str_id,))
                    row = c.fetchone()
                    return dict(row) if row else {"name": "Unknown", "username": None, "reputation": 0, "is_banned": False, "warnings": 0}
                except:
                    return {}

    def ensure_user(self, user_id, name="Unknown", username=None, role="member", chat_id=None, chat_name=None,
                    increment_msg=False, is_join=False, first_name=None, last_name=None, is_bot=None):
        """
        Ensure a user exists in the database and update their profile.
        - increment_msg: True when called from a message event (increments msg_count)
        - is_join:       True when called from a join event (increments join_count)
        - first_name/last_name/is_bot: profile details refreshed (if provided)
        """
        if not self._check_conn():
            return
        str_id = str(user_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT user_id FROM users WHERE user_id=%s", (str_id,))
                exists = c.fetchone()

                fname = (first_name or "").strip() or None
                lname = (last_name or "").strip() or None
                if fname is None and lname is None and name and name != "Unknown":
                    parts = name.split(None, 1)
                    fname = parts[0]
                    lname = parts[1] if len(parts) > 1 else None
                if is_bot is None:
                    is_bot = 0

                if not exists:
                    c.execute(
                        """INSERT INTO users (user_id, name, username, role, first_seen, last_active,
                           last_group_id, last_group_name, msg_count, join_count,
                           first_name, last_name, is_bot, created_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            str_id,
                            name or "Unknown",
                            username or None,
                            role,
                            now,
                            now,
                            str(chat_id) if chat_id else None,
                            chat_name,
                            1 if increment_msg else 0,
                            1 if is_join else 0,
                            fname, lname, 1 if is_bot else 0, now, now,
                        )
                    )
                else:
                    c.execute(
                        """UPDATE users SET
                           name=%s,
                           username=%s,
                           last_active=%s,
                           last_group_id=COALESCE(%s, last_group_id),
                           last_group_name=COALESCE(%s, last_group_name),
                           msg_count=msg_count + %s,
                           join_count=join_count + %s,
                           first_name=COALESCE(%s, first_name),
                           last_name=COALESCE(%s, last_name),
                           is_bot=COALESCE(%s, is_bot),
                           updated_at=%s
                           WHERE user_id=%s""",
                        (
                            name or "Unknown",
                            username or None,
                            now,
                            str(chat_id) if chat_id else None,
                            chat_name,
                            1 if increment_msg else 0,
                            1 if is_join else 0,
                            fname, lname, 1 if is_bot else 0 if is_bot is not None else None, now, str_id
                        )
                    )
                self.conn.commit()
            except Exception as e:
                print(f"Error in ensure_user (user_id={str_id}): {e}")

    def add_message_count(self, chat_id):
        if not self._check_conn():
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE groups SET message_count = message_count + 1, last_active = %s WHERE chat_id=%s", (now, str(chat_id)))
                self.conn.commit()
            except:
                pass

    def get_extra_group_info(self):
        with self._read_guard():
            if not self._check_conn():
                return {}
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT chat_id, name, message_count, member_count, last_active, antispam, strict_mode, max_warnings FROM groups")
                    return {row['chat_id']: dict(row) for row in c.fetchall()}
                except:
                    return {}

    def get_user(self, user_id):
        with self._read_guard():
            if not self._check_conn():
                return {}
            str_id = str(user_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM users WHERE user_id=%s", (str_id,))
                    row = c.fetchone()
                    return dict(row) if row else {}
                except:
                    return {}

    def get_user_by_username(self, username):
        with self._read_guard():
            if not self._check_conn():
                return {}
            uname = username.replace("@", "").strip().lower()
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM users WHERE LOWER(username)=%s", (uname,))
                    row = c.fetchone()
                    return dict(row) if row else {}
                except:
                    return {}

    def add_warning(self, user_id, name="Unknown"):
        if not self._check_conn():
            return 0
        self.ensure_user(user_id, name)
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET warnings = warnings + 1 WHERE user_id=%s", (str_id,))
                c.execute("SELECT warnings FROM users WHERE user_id=%s", (str_id,))
                row = c.fetchone()
                self.conn.commit()
                return row['warnings'] if row else 0
            except:
                return 0

    def remove_warning(self, user_id):
        """Remove one warning (decrement by 1, not below 0). Returns new count."""
        if not self._check_conn():
            return 0
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET warnings = CASE WHEN warnings > 0 THEN warnings - 1 ELSE 0 END WHERE user_id=%s", (str_id,))
                c.execute("SELECT warnings FROM users WHERE user_id=%s", (str_id,))
                row = c.fetchone()
                self.conn.commit()
                return int(row['warnings']) if row else 0
            except:
                return 0

    def reset_warnings(self, user_id):
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET warnings = 0 WHERE user_id=%s", (str_id,))
                self.conn.commit()
            except:
                pass

    # ── WARNING HISTORY (per-group) ──
    def _sync_global_warnings(self, c, user_id):
        """Keep the legacy global users.warnings counter in sync with the
        active rows of warn_history (summed across all groups)."""
        try:
            c.execute("SELECT COUNT(*) as cw FROM warn_history WHERE user_id=%s AND active=1", (str(user_id),))
            row = c.fetchone()
            total = int(row['cw']) if row else 0
            c.execute("UPDATE users SET warnings = %s WHERE user_id=%s", (total, str(user_id)))
        except Exception:
            pass

    def warn_expire(self, chat_id, user_id, seconds):
        """Deactivate active warnings older than `seconds`. Returns count expired."""
        if not seconds or seconds <= 0:
            return 0
        if not self._check_conn():
            return 0
        with self.lock:
            try:
                c = self.conn.cursor()
                cutoff = int(_time.time() - seconds)
                c.execute("UPDATE warn_history SET active=0 WHERE chat_id=%s AND user_id=%s AND active=1 AND issued_at < %s", (str(chat_id), str(user_id), cutoff))
                self.conn.commit()
                return max(0, c.rowcount or 0)
            except Exception:
                return 0

    def warn_add(self, chat_id, user_id, reason="", issued_by=None, warn_time=0, name="Unknown"):
        """Insert a new warning. Sweeps expired warnings for this chat+user first.
        Returns the active warning count for that chat."""
        if not self._check_conn():
            return 0
        str_id = str(user_id)
        self.ensure_user(user_id, name)
        with self.lock:
            try:
                c = self.conn.cursor()
                if warn_time and warn_time > 0:
                    cutoff = int(_time.time() - warn_time)
                    c.execute("UPDATE warn_history SET active=0 WHERE chat_id=%s AND user_id=%s AND active=1 AND issued_at < %s", (str(chat_id), str_id, cutoff))
                c.execute("INSERT INTO warn_history (chat_id, user_id, reason, issued_by, issued_at, active) VALUES (%s, %s, %s, %s, %s, 1)",
                          (str(chat_id), str_id, reason or "", str(issued_by) if issued_by is not None else None, int(_time.time())))
                c.execute("SELECT COUNT(*) as cw FROM warn_history WHERE chat_id=%s AND user_id=%s AND active=1", (str(chat_id), str_id))
                row = c.fetchone()
                self._sync_global_warnings(c, str_id)
                self.conn.commit()
                return int(row['cw']) if row else 0
            except Exception:
                return 0

    def warn_remove_one(self, chat_id, user_id):
        """Deactivate the most recent active warning for this chat+user.
        Returns the remaining active count for that chat."""
        if not self._check_conn():
            return 0
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("SELECT id FROM warn_history WHERE chat_id=%s AND user_id=%s AND active=1 ORDER BY issued_at DESC, id DESC LIMIT 1", (str(chat_id), str_id))
                row = c.fetchone()
                if not row:
                    return 0
                c.execute("UPDATE warn_history SET active=0 WHERE id=%s", (row['id'],))
                c.execute("SELECT COUNT(*) as cw FROM warn_history WHERE chat_id=%s AND user_id=%s AND active=1", (str(chat_id), str_id))
                row = c.fetchone()
                self._sync_global_warnings(c, str_id)
                self.conn.commit()
                return int(row['cw']) if row else 0
            except Exception:
                return 0

    def warn_list(self, chat_id, user_id):
        with self._read_guard():
            """Return active warnings for this chat+user, newest first."""
            if not self._check_conn():
                return []
            try:
                c = self.conn.cursor()
                c.execute("SELECT reason, issued_at, issued_by FROM warn_history WHERE chat_id=%s AND user_id=%s AND active=1 ORDER BY issued_at DESC, id DESC",
                          (str(chat_id), str(user_id)))
                return [dict(r) for r in c.fetchall()]
            except Exception:
                return []

    def warn_reset_user(self, user_id):
        """Deactivate all active warnings for the user (across every group) and zero the global counter."""
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE warn_history SET active=0 WHERE user_id=%s AND active=1", (str_id,))
                c.execute("UPDATE users SET warnings = 0 WHERE user_id=%s", (str_id,))
                self.conn.commit()
            except Exception:
                pass

    def warn_reset_chat(self, chat_id, user_id):
        """Deactivate all active warnings for this chat+user, sync counter."""
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE warn_history SET active=0 WHERE chat_id=%s AND user_id=%s AND active=1", (str(chat_id), str_id))
                self._sync_global_warnings(c, str_id)
                self.conn.commit()
            except Exception:
                pass

    def get_all_stats(self):
        with self._read_guard():
            base = {"total_users": 0, "total_groups": 0, "total_messages": 0, "total_logs": 0,
                    "total_filters": 0, "total_bad_words": 0, "total_warned_users": 0,
                    "total_mutes": 0, "total_bans": 0, "total_scheduled": 0}
            if not self._check_conn():
                return base
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT COUNT(*) as cu FROM users")
                    base['total_users'] = c.fetchone()['cu']
                    c.execute("SELECT COUNT(*) as cg FROM groups")
                    base['total_groups'] = c.fetchone()['cg']
                    c.execute("SELECT SUM(message_count) as cm FROM groups")
                    row = c.fetchone()
                    base['total_messages'] = row['cm'] if row['cm'] else 0
                    c.execute("SELECT COUNT(*) as cl FROM logs")
                    base['total_logs'] = c.fetchone()['cl']
                    c.execute("SELECT COUNT(*) as cf FROM filters")
                    base['total_filters'] = c.fetchone()['cf']
                    c.execute("SELECT COUNT(*) as cb FROM bad_words")
                    base['total_bad_words'] = c.fetchone()['cb']
                    c.execute("SELECT COUNT(*) as cw FROM users WHERE warnings > 0")
                    base['total_warned_users'] = c.fetchone()['cw']
                    c.execute("SELECT COUNT(*) as cm FROM user_mutes WHERE is_active=1")
                    base['total_mutes'] = c.fetchone()['cm']
                    c.execute("SELECT COUNT(*) as cb FROM user_bans WHERE is_active=1")
                    base['total_bans'] = c.fetchone()['cb']
                    c.execute("SELECT COUNT(*) as cs FROM scheduled_messages WHERE status='pending'")
                    base['total_scheduled'] = c.fetchone()['cs']
                    return base
                except:
                    return base

    def get_all_users(self):
        with self._read_guard():
            if not self._check_conn():
                return {}
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM users ORDER BY first_seen DESC")
                    return {row['user_id']: dict(row) for row in c.fetchall()}
                except:
                    return {}

    def get_all_groups(self):
        if not self._check_conn():
            return {}
        with self._read_guard():
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("""
                        SELECT g.chat_id, g.name, g.message_count, g.member_count,
                               g.last_active, g.antispam, g.antispam_auto_delete_links,
                               g.keyword_alert, g.keyword_alert_words,
                               g.lock_sticker, g.lock_animation, g.lock_media, g.lock_url,
                               g.lock_forward, g.lock_inline, g.lock_poll, g.lock_game,
                               g.tictactoe,
                               g.welcome_message, g.welcome_type, g.welcome_file_id,
                               g.leave_message, g.leave_type, g.leave_file_id, g.strict_mode, g.max_warnings,
                               (SELECT COUNT(*) FROM filters WHERE chat_id = g.chat_id) as filter_count,
                               (SELECT COUNT(*) FROM bad_words WHERE chat_id = g.chat_id) as bad_words_count
                        FROM groups g ORDER BY g.last_active DESC
                    """)
                    return {row['chat_id']: dict(row) for row in c.fetchall()}
                except Exception:
                    # Fallback for DBs not yet migrated (old columns missing).
                    # Roll back the failed transaction first so the fallback
                    # query can actually run.
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
                    try:
                        c = self.conn.cursor()
                        c.execute("""
                            SELECT g.chat_id, g.name, g.message_count, g.member_count,
                                   g.last_active, g.antispam, g.welcome_message, g.welcome_type, g.welcome_file_id,
                                   g.leave_message, g.leave_type, g.leave_file_id, g.strict_mode, g.max_warnings,
                                   (SELECT COUNT(*) FROM filters WHERE chat_id = g.chat_id) as filter_count,
                                   (SELECT COUNT(*) FROM bad_words WHERE chat_id = g.chat_id) as bad_words_count
                            FROM groups g ORDER BY g.last_active DESC
                        """)
                        result = {}
                        for row in c.fetchall():
                            d = dict(row)
                            d.setdefault("antispam_auto_delete_links", 1)
                            d.setdefault("keyword_alert", 0)
                            d.setdefault("keyword_alert_words", "@admin,admin,help,support")
                            d.setdefault("lock_sticker", 0)
                            d.setdefault("lock_animation", 0)
                            d.setdefault("lock_media", 0)
                            d.setdefault("lock_url", 0)
                            d.setdefault("lock_forward", 0)
                            d.setdefault("lock_inline", 0)
                            d.setdefault("lock_poll", 0)
                            d.setdefault("lock_game", 0)
                            d.setdefault("tictactoe", 0)
                            result[d["chat_id"]] = d
                        # Trigger migration for next request
                        try:
                            self._migrate()
                        except Exception:
                            pass
                        return result
                    except Exception:
                        return {}

    def search_items(self, query):
        with self._read_guard():
            if not self._check_conn():
                return {}, {}
            with self.lock:
                try:
                    c = self.conn.cursor()
                    q = f"%{query}%"
                    c.execute("SELECT * FROM users WHERE user_id LIKE %s OR name LIKE %s OR username LIKE %s", (q, q, q))
                    users = {row['user_id']: dict(row) for row in c.fetchall()}
                    c.execute("SELECT * FROM groups WHERE chat_id LIKE %s OR name LIKE %s", (q, q))
                    groups = {row['chat_id']: dict(row) for row in c.fetchall()}
                    return users, groups
                except:
                    return {}, {}

    def get_warnings_leaderboard(self, limit=10):
        with self._read_guard():
            if not self._check_conn():
                return []
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT user_id, name, warnings FROM users WHERE warnings > 0 ORDER BY warnings DESC LIMIT %s", (limit,))
                    return [dict(row) for row in c.fetchall()]
                except:
                    return []

    def delete_user(self, user_id):
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM users WHERE user_id=%s", (str_id,))
                self.conn.commit()
            except:
                pass

    def delete_group(self, chat_id):
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM groups WHERE chat_id=%s", (str_id,))
                c.execute("DELETE FROM filters WHERE chat_id=%s", (str_id,))
                c.execute("DELETE FROM bad_words WHERE chat_id=%s", (str_id,))
                c.execute("DELETE FROM approved_users WHERE chat_id=%s", (str_id,))
                self.conn.commit()
            except:
                pass

    def global_ban_user(self, user_id, reason="Admin banned", banner="System"):
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET is_banned=1, banned_reason=%s WHERE user_id=%s", (reason, str_id))
                c.execute("INSERT INTO global_bans (user_id, reason, banned_by) VALUES (%s, %s, %s) ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, banned_by=excluded.banned_by, banned_at=CURRENT_TIMESTAMP", (str_id, reason, banner))
                self.conn.commit()
            except:
                pass

    def global_unban_user(self, user_id):
        if not self._check_conn():
            return
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET is_banned=0, banned_reason=NULL WHERE user_id=%s", (str_id,))
                c.execute("DELETE FROM global_bans WHERE user_id=%s", (str_id,))
                self.conn.commit()
            except:
                pass

    def get_global_bans(self):
        with self._read_guard():
            if not self._check_conn():
                return []
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM global_bans ORDER BY banned_at DESC")
                    return [dict(r) for r in c.fetchall()]
                except:
                    return []

    def update_reputation(self, user_id, amount):
        if not self._check_conn():
            return 0
        str_id = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE users SET reputation = reputation + %s WHERE user_id=%s", (amount, str_id))
                c.execute("SELECT reputation FROM users WHERE user_id=%s", (str_id,))
                row = c.fetchone()
                self.conn.commit()
                return row['reputation'] if row else 0
            except:
                return 0

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # TIME-BASED MUTE/BAN SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def mute_user(self, chat_id, user_id, duration_seconds, reason="Spam", muted_by=None):
        """Add a temporary mute for user in chat"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        unmute_at = (datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO user_mutes (chat_id, user_id, unmute_at, reason, muted_by, is_active)
                       VALUES (%s, %s, %s, %s, %s, 1)
                       ON CONFLICT(chat_id, user_id) DO UPDATE SET unmute_at=excluded.unmute_at, reason=excluded.reason, is_active=1""",
                    (str_chat, str_user, unmute_at, reason, muted_by)
                )
                self.conn.commit()
                self.log_event(f"ðŸ”‡ Mute: {str_user} in {str_chat} for {duration_seconds}s by {muted_by}")
            except Exception as e:
                print(f"Error in mute_user: {e}")

    def unmute_user(self, chat_id, user_id):
        """Manually unmute user"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM user_mutes WHERE chat_id=%s AND user_id=%s", (str_chat, str_user))
                self.conn.commit()
            except Exception as e:
                print(f"Error in unmute_user: {e}")

    def is_user_muted(self, chat_id, user_id):
        with self._read_guard():
            """Check if user is currently muted"""
            if not self._check_conn():
                return False
            str_chat = str(chat_id)
            str_user = str(user_id)
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM user_mutes WHERE chat_id=%s AND user_id=%s AND is_active=1 AND unmute_at > %s", (str_chat, str_user, now_str))
                    return c.fetchone() is not None
                except:
                    return False

    def get_expired_mutes(self):
        with self._read_guard():
            """Get all mutes that have expired (for auto-unmute)"""
            if not self._check_conn():
                return []
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM user_mutes WHERE is_active=1 AND unmute_at <= %s", (now_str,))
                    return [dict(r) for r in c.fetchall()]
                except:
                    return []

    def ban_user(self, chat_id, user_id, duration_seconds=None, reason="Violation", banned_by=None):
        """Add a temporary/permanent ban for user in chat"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        unban_at = None
        if duration_seconds:
            unban_at = (datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO user_bans (chat_id, user_id, unban_at, reason, banned_by, is_active)
                       VALUES (%s, %s, %s, %s, %s, 1)
                       ON CONFLICT(chat_id, user_id) DO UPDATE SET unban_at=excluded.unban_at, reason=excluded.reason, is_active=1""",
                    (str_chat, str_user, unban_at, reason, banned_by)
                )
                self.conn.commit()
                dur_str = f"{duration_seconds}s" if duration_seconds else "permanent"
                self.log_event(f"ðŸš« Ban: {str_user} in {str_chat} ({dur_str}) by {banned_by}")
            except Exception as e:
                print(f"Error in ban_user: {e}")

    def unban_user(self, chat_id, user_id):
        """Manually unban user"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM user_bans WHERE chat_id=%s AND user_id=%s", (str_chat, str_user))
                self.conn.commit()
            except Exception as e:
                print(f"Error in unban_user: {e}")

    def is_user_banned(self, chat_id, user_id):
        with self._read_guard():
            """Check if user is currently banned (temp or permanent)"""
            if not self._check_conn():
                return False
            str_chat = str(chat_id)
            str_user = str(user_id)
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM user_bans WHERE chat_id=%s AND user_id=%s AND is_active=1 AND (unban_at IS NULL OR unban_at > %s)", (str_chat, str_user, now_str))
                    return c.fetchone() is not None
                except:
                    return False

    def get_expired_bans(self):
        with self._read_guard():
            """Get all bans that have expired (for auto-unban)"""
            if not self._check_conn():
                return []
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM user_bans WHERE is_active=1 AND unban_at IS NOT NULL AND unban_at <= %s", (now_str,))
                    return [dict(r) for r in c.fetchall()]
                except:
                    return []

    def get_mutes(self, chat_id):
        with self._read_guard():
            """Get all active mutes in a chat"""
            if not self._check_conn():
                return []
            str_chat = str(chat_id)
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM user_mutes WHERE chat_id=%s AND is_active=1 AND unmute_at > %s ORDER BY unmute_at ASC", (str_chat, now_str))
                    return [dict(r) for r in c.fetchall()]
                except:
                    return []

    def get_bans(self, chat_id):
        with self._read_guard():
            """Get all active bans in a chat"""
            if not self._check_conn():
                return []
            str_chat = str(chat_id)
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM user_bans WHERE chat_id=%s AND is_active=1 AND (unban_at IS NULL OR unban_at > %s) ORDER BY banned_at DESC", (str_chat, now_str))
                    return [dict(r) for r in c.fetchall()]
                except:
                    return []

    def get_all_active_mutes(self):
        with self._read_guard():
            """Get all active mutes across all chats"""
            if not self._check_conn():
                return []
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("""
                        SELECT m.*, g.name as group_name, u.name as user_name 
                        FROM user_mutes m
                        LEFT JOIN groups g ON m.chat_id = g.chat_id
                        LEFT JOIN users u ON m.user_id = u.user_id
                        WHERE m.is_active=1 AND m.unmute_at > %s
                        ORDER BY m.unmute_at ASC
                    """, (now_str,))
                    return [dict(r) for r in c.fetchall()]
                except Exception as e:
                    print(f"Error in get_all_active_mutes: {e}")
                    return []

    def get_all_active_bans(self):
        with self._read_guard():
            """Get all active bans across all chats"""
            if not self._check_conn():
                return []
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("""
                        SELECT b.*, g.name as group_name, u.name as user_name 
                        FROM user_bans b
                        LEFT JOIN groups g ON b.chat_id = g.chat_id
                        LEFT JOIN users u ON b.user_id = u.user_id
                        WHERE b.is_active=1 AND (b.unban_at IS NULL OR b.unban_at > %s)
                        ORDER BY b.banned_at DESC
                    """, (now_str,))
                    return [dict(r) for r in c.fetchall()]
                except Exception as e:
                    print(f"Error in get_all_active_bans: {e}")
                    return []

    def log_infraction(self, chat_id, user_id, action_type, duration_seconds=None, reason=None, issued_by=None):
        """Log moderation action for audit trail"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO infraction_history (chat_id, user_id, action_type, duration_seconds, reason, issued_by)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (str_chat, str_user, action_type, duration_seconds, reason, issued_by)
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error in log_infraction: {e}")

    def get_user_infractions(self, chat_id, user_id, limit=10):
        with self._read_guard():
            """Get infraction history for user in chat"""
            if not self._check_conn():
                return []
            str_chat = str(chat_id)
            str_user = str(user_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute(
                        "SELECT * FROM infraction_history WHERE chat_id=%s AND user_id=%s ORDER BY issued_at DESC LIMIT %s",
                        (str_chat, str_user, limit)
                    )
                    return [dict(r) for r in c.fetchall()]
                except:
                    return []

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # SCHEDULED MESSAGES SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def add_scheduled_message(self, chat_id, message, scheduled_at, message_type="text", file_id=None):
        """Add a scheduled message to the database"""
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO scheduled_messages (chat_id, message, scheduled_at, message_type, file_id, status)
                       VALUES (%s, %s, %s, %s, %s, 'pending')""",
                    (str_chat, message, scheduled_at, message_type, file_id)
                )
                self.conn.commit()
            except Exception as e:
                print(f"Error in add_scheduled_message: {e}")

    def get_pending_scheduled_messages(self):
        with self._read_guard():
            """Get all pending scheduled messages that are due"""
            if not self._check_conn():
                return []
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM scheduled_messages WHERE status='pending' AND scheduled_at <= NOW() ORDER BY scheduled_at ASC")
                    return [dict(r) for r in c.fetchall()]
                except:
                    return []

    def update_scheduled_message_status(self, message_id, status):
        """Update the status of a scheduled message"""
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("UPDATE scheduled_messages SET status=%s WHERE id=%s", (status, message_id))
                self.conn.commit()
            except Exception as e:
                print(f"Error in update_scheduled_message_status: {e}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # LOG SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def log_event(self, event):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("INSERT INTO logs (event) VALUES (%s)", (str(event),))
                self.conn.commit()
            except Exception as e:
                print(f"Error in log_event: {e}")

    def get_recent_logs(self, limit=50):
        with self._read_guard():
            if not self._check_conn():
                return []
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT event, timestamp FROM logs ORDER BY timestamp DESC LIMIT %s", (limit,))
                    return [dict(row) for row in c.fetchall()]
                except:
                    return []

    def clear_logs(self):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("DELETE FROM logs")
                self.conn.commit()
            except Exception as e:
                print(f"Error in clear_logs: {e}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # TRACKING SYSTEM CONFIG
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def get_tracking_config(self):
        cached = _config_cache.get("tracking_config")
        if cached is not None:
            return dict(cached)
            
        config = self.get_config()
        tracking = {
            "tracking_destination_group": config.get("tracking_destination_group", ""),
            "tracked_users": config.get("tracked_users", []),
            "tracked_groups": config.get("tracked_groups", [])
        }
        _config_cache.set("tracking_config", tracking)
        return tracking
        
    def add_tracked_user(self, user_id):
        tracking = self.get_tracking_config()
        if str(user_id) not in tracking["tracked_users"]:
            tracking["tracked_users"].append(str(user_id))
            self.update_config("tracked_users", tracking["tracked_users"])
            _config_cache.invalidate("tracking_config")
            
    def remove_tracked_user(self, user_id):
        tracking = self.get_tracking_config()
        if str(user_id) in tracking["tracked_users"]:
            tracking["tracked_users"].remove(str(user_id))
            self.update_config("tracked_users", tracking["tracked_users"])
            _config_cache.invalidate("tracking_config")
            
    def add_tracked_group(self, group_id):
        tracking = self.get_tracking_config()
        if str(group_id) not in tracking["tracked_groups"]:
            tracking["tracked_groups"].append(str(group_id))
            self.update_config("tracked_groups", tracking["tracked_groups"])
            _config_cache.invalidate("tracking_config")
            
    def remove_tracked_group(self, group_id):
        tracking = self.get_tracking_config()
        if str(group_id) in tracking["tracked_groups"]:
            tracking["tracked_groups"].remove(str(group_id))
            self.update_config("tracked_groups", tracking["tracked_groups"])
            _config_cache.invalidate("tracking_config")
            
    def set_tracking_destination(self, dest_id):
        self.update_config("tracking_destination_group", str(dest_id))
        _config_cache.invalidate("tracking_config")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # USER STATS SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def update_user_stats(self, user_id, messages_sent=0, links_shared=0):
        """Update user statistics"""
        if not self._check_conn():
            return
        str_id = str(user_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute("""
                    INSERT INTO user_stats (user_id, messages_sent, links_shared, last_activity)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                    messages_sent = messages_sent + %s,
                    links_shared = links_shared + %s,
                    last_activity = %s
                """, (str_id, messages_sent, links_shared, now, messages_sent, links_shared, now))
                self.conn.commit()
            except Exception as e:
                print(f"Error in update_user_stats: {e}")

    def get_user_stats(self, user_id):
        with self._read_guard():
            """Get user statistics"""
            if not self._check_conn():
                return {}
            str_id = str(user_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM user_stats WHERE user_id=%s", (str_id,))
                    row = c.fetchone()
                    return dict(row) if row else {"user_id": str_id, "messages_sent": 0, "links_shared": 0, "warnings_received": 0, "infractions_count": 0, "last_activity": None}
                except:
                    return {}

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # BATCH OPERATIONS
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def batch_update_users(self, updates):
        """Batch update multiple users"""
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                for user_id, data in updates.items():
                    str_id = str(user_id)
                    c.execute("""
                        UPDATE users SET
                            name=%s,
                            username=%s
                        WHERE user_id=%s
                    """, (data.get('name'), data.get('username'), str_id))
                self.conn.commit()
            except Exception as e:
                print(f"Error in batch_update_users: {e}")

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # UTILITY METHODS
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def replace_database(self, backup_file):
        """PostgreSQL is not file-based — replacing the database through a
        backup-file copy is not supported. Restore via pg_dump/psql outside
        this process instead."""
        print("❌ replace_database is not supported on PostgreSQL — restore via pg_dump/psql outside this process.")
        return False

    # CHAT ACTIVITY / RANKINGS SYSTEM
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def track_chat_message(self, chat_id, user_id, display_name, username, today_str, week_start,
                           user_milestones, group_milestones):
        """
        Atomically record a single valid group message and check milestones.

        user_milestones / group_milestones: lists of thresholds to evaluate
        (pass empty lists to skip milestone recording for a category).

        Returns a dict:
          {user_total, group_today_total, user_milestones:[...], group_milestones:[...]}
        Only milestones that were newly achieved (recorded for the first time)
        are included in the returned lists, so announcements never duplicate.
        """
        result = {"user_total": 0, "group_today_total": 0, "user_milestones": [], "group_milestones": []}
        if not self._check_conn():
            return result
        gid = str(chat_id)
        uid = str(user_id)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            try:
                c = self.conn.cursor()

                # 1) Overall per-user total
                c.execute("""
                    INSERT INTO chat_user_stats (group_id, user_id, display_name, username, total_messages, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, 1, %s, %s)
                    ON CONFLICT(group_id, user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        username = CASE WHEN excluded.username IS NOT NULL THEN excluded.username ELSE chat_user_stats.username END,
                        total_messages = chat_user_stats.total_messages + 1,
                        updated_at = excluded.updated_at
                """, (gid, uid, display_name or "Unknown", username, now, now))

                # 2) Daily group total
                c.execute("""
                    INSERT INTO chat_daily_stats (group_id, date, total_messages)
                    VALUES (%s, %s, 1)
                    ON CONFLICT(group_id, date) DO UPDATE SET
                        total_messages = chat_daily_stats.total_messages + 1
                """, (gid, today_str))
                c.execute("SELECT total_messages FROM chat_daily_stats WHERE group_id=%s AND date=%s", (gid, today_str))
                row = c.fetchone()
                result["group_today_total"] = int(row["total_messages"]) if row else 1

                # 3) Daily per-user total
                c.execute("""
                    INSERT INTO chat_user_daily_stats (group_id, user_id, date, total_messages)
                    VALUES (%s, %s, %s, 1)
                    ON CONFLICT(group_id, user_id, date) DO UPDATE SET
                        total_messages = chat_user_daily_stats.total_messages + 1
                """, (gid, uid, today_str))

                # 4) Weekly per-user total
                c.execute("""
                    INSERT INTO chat_user_weekly_stats (group_id, user_id, week_start, total_messages)
                    VALUES (%s, %s, %s, 1)
                    ON CONFLICT(group_id, user_id, week_start) DO UPDATE SET
                        total_messages = chat_user_weekly_stats.total_messages + 1
                """, (gid, uid, week_start))

                # 5) Overall user total (for milestone checks)
                c.execute("SELECT total_messages FROM chat_user_stats WHERE group_id=%s AND user_id=%s", (gid, uid))
                row = c.fetchone()
                result["user_total"] = int(row["total_messages"]) if row else 1

                # 6) User milestones â€” recorded once ever per group/user/milestone.
                #    INSERT OR IGNORE + rowcount makes this safe under concurrency.
                for m in user_milestones:
                    if result["user_total"] >= m:
                        c.execute(
                            "INSERT INTO chat_milestones (group_id, user_id, milestone, achieved_at) VALUES (%s, %s, %s, %s) ON CONFLICT(group_id, user_id, milestone) DO NOTHING",
                            (gid, uid, m, now)
                        )
                        if c.rowcount > 0:
                            result["user_milestones"].append(m)

                # 7) Daily group milestones â€” recorded once per group/date/milestone.
                for m in group_milestones:
                    if result["group_today_total"] >= m:
                        c.execute(
                            "INSERT INTO chat_group_milestones (group_id, date, milestone, achieved_at) VALUES (%s, %s, %s, %s) ON CONFLICT(group_id, date, milestone) DO NOTHING",
                            (gid, today_str, m, now)
                        )
                        if c.rowcount > 0:
                            result["group_milestones"].append(m)

                self.conn.commit()
            except Exception as e:
                print(f"Error in track_chat_message: {e}")
        return result

    def get_chat_rankings(self, chat_id, mode="overall", ref_key=None, limit=10, offset=0):
        with self._read_guard():
            """
            Fetch ranked rows for a group.
            mode: 'overall' | 'today' | 'week'
            ref_key: today date string (today) or week_start string (week); ignored for overall.
            """
            if not self._check_conn():
                return []
            gid = str(chat_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    if mode == "today":
                        c.execute("""
                            SELECT s.user_id,
                                   COALESCE(u.display_name, 'Unknown') AS display_name,
                                   u.username,
                                   s.total_messages
                            FROM chat_user_daily_stats s
                            LEFT JOIN chat_user_stats u ON u.group_id = s.group_id AND u.user_id = s.user_id
                            WHERE s.group_id = %s AND s.date = %s
                            ORDER BY s.total_messages DESC, s.user_id
                            LIMIT %s OFFSET %s
                        """, (gid, ref_key, limit, offset))
                    elif mode == "week":
                        c.execute("""
                            SELECT s.user_id,
                                   COALESCE(u.display_name, 'Unknown') AS display_name,
                                   u.username,
                                   s.total_messages
                            FROM chat_user_weekly_stats s
                            LEFT JOIN chat_user_stats u ON u.group_id = s.group_id AND u.user_id = s.user_id
                            WHERE s.group_id = %s AND s.week_start = %s
                            ORDER BY s.total_messages DESC, s.user_id
                            LIMIT %s OFFSET %s
                        """, (gid, ref_key, limit, offset))
                    else:
                        c.execute("""
                            SELECT user_id, display_name, username, total_messages
                            FROM chat_user_stats
                            WHERE group_id = %s
                            ORDER BY total_messages DESC, user_id
                            LIMIT %s OFFSET %s
                        """, (gid, limit, offset))
                    return [dict(r) for r in c.fetchall()]
                except Exception as e:
                    print(f"Error in get_chat_rankings: {e}")
                    return []

    def count_chat_rankings(self, chat_id, mode="overall", ref_key=None):
        with self._read_guard():
            """Count ranked users for a group (used for pagination)."""
            if not self._check_conn():
                return 0
            gid = str(chat_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    if mode == "today":
                        c.execute("SELECT COUNT(*) AS n FROM chat_user_daily_stats WHERE group_id=%s AND date=%s", (gid, ref_key))
                    elif mode == "week":
                        c.execute("SELECT COUNT(*) AS n FROM chat_user_weekly_stats WHERE group_id=%s AND week_start=%s", (gid, ref_key))
                    else:
                        c.execute("SELECT COUNT(*) AS n FROM chat_user_stats WHERE group_id=%s", (gid,))
                    row = c.fetchone()
                    return int(row["n"]) if row else 0
                except Exception as e:
                    print(f"Error in count_chat_rankings: {e}")
                    return 0

    def get_chat_total_messages(self, chat_id, mode="overall", ref_key=None):
        with self._read_guard():
            """Total message count for a group in the given mode."""
            if not self._check_conn():
                return 0
            gid = str(chat_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    if mode == "today":
                        c.execute("SELECT total_messages FROM chat_daily_stats WHERE group_id=%s AND date=%s", (gid, ref_key))
                        row = c.fetchone()
                        return int(row["total_messages"]) if row else 0
                    elif mode == "week":
                        c.execute("SELECT COALESCE(SUM(total_messages), 0) AS n FROM chat_user_weekly_stats WHERE group_id=%s AND week_start=%s", (gid, ref_key))
                    else:
                        c.execute("SELECT COALESCE(SUM(total_messages), 0) AS n FROM chat_user_stats WHERE group_id=%s", (gid,))
                    row = c.fetchone()
                    return int(row["n"]) if row else 0
                except Exception as e:
                    print(f"Error in get_chat_total_messages: {e}")
                    return 0

    def get_user_chat_stats(self, chat_id, user_id, today_str, week_start):
        with self._read_guard():
            """Personal stats for one user in one group (overall/today/week + overall rank).

            rank is the user's 1-based overall position among all tracked users in the
            group (tie-safe: users with equal totals share the rank determined by the
            number of users strictly above). Returns rank=None when the user has no
            tracked messages at all.
            """
            result = {
                "total_messages": 0,
                "today_messages": 0,
                "week_messages": 0,
                "rank": None,
                "display_name": "Unknown",
                "username": None,
            }
            if not self._check_conn():
                return result
            gid = str(chat_id)
            uid = str(user_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    has_total = False
                    c.execute(
                        "SELECT display_name, username, total_messages FROM chat_user_stats WHERE group_id=%s AND user_id=%s",
                        (gid, uid),
                    )
                    row = c.fetchone()
                    if row:
                        has_total = True
                        result["display_name"] = row["display_name"] or "Unknown"
                        result["username"] = row["username"]
                        result["total_messages"] = int(row["total_messages"])

                    c.execute(
                        "SELECT total_messages FROM chat_user_daily_stats WHERE group_id=%s AND user_id=%s AND date=%s",
                        (gid, uid, today_str),
                    )
                    row = c.fetchone()
                    if row:
                        result["today_messages"] = int(row["total_messages"])

                    c.execute(
                        "SELECT total_messages FROM chat_user_weekly_stats WHERE group_id=%s AND user_id=%s AND week_start=%s",
                        (gid, uid, week_start),
                    )
                    row = c.fetchone()
                    if row:
                        result["week_messages"] = int(row["total_messages"])

                    if has_total:
                        c.execute(
                            """
                            SELECT COUNT(*) AS n FROM chat_user_stats
                            WHERE group_id=%s AND total_messages > %s
                            """,
                            (gid, result["total_messages"]),
                        )
                        row = c.fetchone()
                        result["rank"] = (int(row["n"]) if row else 0) + 1
                    return result
                except Exception as e:
                    print(f"Error in get_user_chat_stats: {e}")
                    return result

    def get_chat_group_stats(self, chat_id, today_str, week_start):
        with self._read_guard():
            """Aggregate chat stats for a group (total / today / week / active-today / top chatter)."""
            result = {
                "total_messages": 0,
                "today_messages": 0,
                "week_messages": 0,
                "active_today": 0,
                "top_chatter": None,
            }
            if not self._check_conn():
                return result
            gid = str(chat_id)
            with self.lock:
                try:
                    c = self.conn.cursor()

                    c.execute("SELECT COALESCE(SUM(total_messages), 0) AS n FROM chat_user_stats WHERE group_id=%s", (gid,))
                    row = c.fetchone()
                    result["total_messages"] = int(row["n"]) if row else 0

                    c.execute("SELECT total_messages FROM chat_daily_stats WHERE group_id=%s AND date=%s", (gid, today_str))
                    row = c.fetchone()
                    result["today_messages"] = int(row["total_messages"]) if row else 0

                    c.execute(
                        "SELECT COALESCE(SUM(total_messages), 0) AS n FROM chat_user_weekly_stats WHERE group_id=%s AND week_start=%s",
                        (gid, week_start),
                    )
                    row = c.fetchone()
                    result["week_messages"] = int(row["n"]) if row else 0

                    c.execute(
                        "SELECT COUNT(DISTINCT user_id) AS n FROM chat_user_daily_stats WHERE group_id=%s AND date=%s",
                        (gid, today_str),
                    )
                    row = c.fetchone()
                    result["active_today"] = int(row["n"]) if row else 0

                    c.execute(
                        """
                        SELECT display_name, username FROM chat_user_stats
                        WHERE group_id=%s ORDER BY total_messages DESC, user_id LIMIT 1
                        """,
                        (gid,),
                    )
                    row = c.fetchone()
                    if row:
                        result["top_chatter"] = {
                            "display_name": row["display_name"] or "Unknown",
                            "username": row["username"],
                        }
                    return result
                except Exception as e:
                    print(f"Error in get_chat_group_stats: {e}")
                    return result

    def reset_chat_user_stats(self, chat_id, user_id=None):
        """Administrator reset for a group's (or one user's) chat statistics."""
        if not self._check_conn():
            return
        gid = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                if user_id is not None:
                    uid = str(user_id)
                    c.execute("DELETE FROM chat_user_stats WHERE group_id=%s AND user_id=%s", (gid, uid))
                    c.execute("DELETE FROM chat_user_daily_stats WHERE group_id=%s AND user_id=%s", (gid, uid))
                    c.execute("DELETE FROM chat_user_weekly_stats WHERE group_id=%s AND user_id=%s", (gid, uid))
                    c.execute("DELETE FROM chat_milestones WHERE group_id=%s AND user_id=%s", (gid, uid))
                else:
                    c.execute("DELETE FROM chat_user_stats WHERE group_id=%s", (gid,))
                    c.execute("DELETE FROM chat_daily_stats WHERE group_id=%s", (gid,))
                    c.execute("DELETE FROM chat_user_daily_stats WHERE group_id=%s", (gid,))
                    c.execute("DELETE FROM chat_user_weekly_stats WHERE group_id=%s", (gid,))
                    c.execute("DELETE FROM chat_milestones WHERE group_id=%s", (gid,))
                    c.execute("DELETE FROM chat_group_milestones WHERE group_id=%s", (gid,))
                self.conn.commit()
            except Exception as e:
                print(f"Error in reset_chat_user_stats: {e}")

# Initialize database instance

    # ═══════════════════════════════════════════════════════════
    # GROUP REGISTRATION & SYNC ENGINE
    # ═══════════════════════════════════════════════════════════

    # ── Group registration ───────────────────────────────────
    def register_group(self, chat_id, name=None, username=None, group_type=None, bot_is_admin=None):
        """Register (or refresh) a group in PostgreSQL. Idempotent.

        - Existing rows keep their name/settings and only get their
          username/group_type/bot_is_admin/timestamps refreshed.
        - The global `groups` table (chat_id PK) IS the canonical registry —
          existing settings (warnings, antispam, etc.) are preserved.
        """
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO groups (chat_id, name, username, group_type, bot_is_admin, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT(chat_id) DO UPDATE SET
                           name         = CASE WHEN excluded.name IS NOT NULL AND excluded.name <> 'Unknown Group'
                                               THEN excluded.name ELSE groups.name END,
                           username     = COALESCE(excluded.username, groups.username),
                           group_type   = COALESCE(excluded.group_type, groups.group_type),
                           bot_is_admin = CASE WHEN excluded.bot_is_admin IS NOT NULL
                                               THEN excluded.bot_is_admin ELSE groups.bot_is_admin END,
                           updated_at   = CURRENT_TIMESTAMP""",
                    (str_id, name or 'Unknown Group', username, group_type,
                     bot_is_admin if bot_is_admin is not None else None)
                )
                self.conn.commit()
                _group_cache.invalidate(f"group:{str_id}")
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"Error in register_group ({str_id}): {e}")

    def set_group_bot_admin(self, chat_id, is_admin):
        """Record whether the bot is an admin of a registered group."""
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "UPDATE groups SET bot_is_admin=%s, updated_at=CURRENT_TIMESTAMP WHERE chat_id=%s",
                    (1 if is_admin else 0, str_id)
                )
                self.conn.commit()
                _group_cache.invalidate(f"group:{str_id}")
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"Error in set_group_bot_admin ({str_id}): {e}")

    def get_group_registration(self, chat_id):
        with self._read_guard():
            """Return the registration record (chat_id, name, username, group_type,
            bot_is_admin, created_at, updated_at) or None if not registered."""
            if not self._check_conn():
                return None
            str_id = str(chat_id)
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute(
                        "SELECT chat_id, name, username, group_type, bot_is_admin, created_at, updated_at, member_count, message_count FROM groups WHERE chat_id=%s",
                        (str_id,)
                    )
                    row = c.fetchone()
                    return dict(row) if row else None
                except Exception:
                    return None

    # ── Member registration ──────────────────────────────────
    def is_member_registered(self, chat_id, user_id):
        with self._read_guard():
            if not self._check_conn():
                return False
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute(
                        "SELECT 1 FROM group_members WHERE telegram_user_id=%s AND telegram_group_id=%s",
                        (str(user_id), str(chat_id))
                    )
                    return c.fetchone() is not None
                except Exception:
                    return False

    def get_member_membership(self, chat_id, user_id):
        with self._read_guard():
            if not self._check_conn():
                return None
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute(
                        "SELECT telegram_user_id, telegram_group_id, membership_status, joined_at, updated_at FROM group_members WHERE telegram_user_id=%s AND telegram_group_id=%s",
                        (str(user_id), str(chat_id))
                    )
                    row = c.fetchone()
                    return dict(row) if row else None
                except Exception:
                    return None

    def register_member(self, chat_id, user_id, membership_status="member",
                        name=None, username=None, first_name=None, last_name=None, is_bot=None):
        """Upsert a member: global users row + group_members row.

        - new user       → global row created (is_join-like: join_count untouched here)
        - existing user  → profile fields refreshed (COALESCE, never overwrite good data)
        - member rows    → membership_status refreshed; joined_at refreshed on re-join
        """
        if not self._check_conn():
            return
        str_chat = str(chat_id)
        str_user = str(user_id)
        try:
            self.ensure_user(
                user_id,
                name=name if name not in (None, "") else "Unknown",
                username=username,
                role="member",
                chat_id=chat_id,
                first_name=first_name,
                last_name=last_name,
                is_bot=is_bot,
            )
        except Exception as e:
            print(f"Error in register_member (ensure_user {str_user}): {e}")
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO group_members (telegram_user_id, telegram_group_id, membership_status, joined_at, updated_at)
                       VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT(telegram_user_id, telegram_group_id) DO UPDATE SET
                           membership_status = excluded.membership_status,
                           joined_at = CASE
                               WHEN excluded.membership_status IN ('member','administrator','creator','restricted')
                                    AND group_members.membership_status IN ('left','kicked')
                               THEN CURRENT_TIMESTAMP
                               ELSE group_members.joined_at
                           END,
                           updated_at = CURRENT_TIMESTAMP""",
                    (str_user, str_chat, membership_status)
                )
                self.conn.commit()
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"Error in register_member ({str_user} @ {str_chat}): {e}")

    def update_member_membership(self, chat_id, user_id, membership_status, name=None, username=None,
                                 first_name=None, last_name=None, is_bot=None):
        """Set a member's membership status (join/admin-change/leave/kick)."""
        self.register_member(chat_id, user_id, membership_status,
                             name=name, username=username, first_name=first_name,
                             last_name=last_name, is_bot=is_bot)

    def mark_group_members_left(self, chat_id):
        """Bot left / was removed: mark every known member as left."""
        if not self._check_conn():
            return
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "UPDATE group_members SET membership_status='left', updated_at=CURRENT_TIMESTAMP WHERE telegram_group_id=%s AND membership_status NOT IN ('left','kicked')",
                    (str_id,)
                )
                self.conn.commit()
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"Error in mark_group_members_left ({str_id}): {e}")

    def count_group_members(self, chat_id, status=None):
        with self._read_guard():
            """Count registered members for a group. If `status` is None,
            counts ALL registered rows (including left/kicked — the honest
            'known members' total; pass 'member'/'administrator'/etc to filter)."""
            if not self._check_conn():
                return 0
            with self.lock:
                try:
                    c = self.conn.cursor()
                    if status:
                        c.execute(
                            "SELECT COUNT(*) AS n FROM group_members WHERE telegram_group_id=%s AND membership_status=%s",
                            (str(chat_id), status)
                        )
                    else:
                        c.execute(
                            "SELECT COUNT(*) AS n FROM group_members WHERE telegram_group_id=%s",
                            (str(chat_id),)
                        )
                    row = c.fetchone()
                    return int(row['n']) if row else 0
                except Exception:
                    return 0

    def get_group_members(self, chat_id, limit=1000):
        with self._read_guard():
            """Fetch known members for a group (Telegram makes this available
            only through member-update events — there is NO full-enumeration
            endpoint, so this never claims to include members we never saw)."""
            if not self._check_conn():
                return []
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute(
                        "SELECT telegram_user_id, telegram_group_id, membership_status, joined_at, updated_at FROM group_members WHERE telegram_group_id=%s ORDER BY updated_at DESC LIMIT %s",
                        (str(chat_id), limit)
                    )
                    return [dict(row) for row in c.fetchall()]
                except Exception:
                    return []

    # ── Sync jobs (persisted) ────────────────────────────────
    def _job_now(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def request_sync_job(self, chat_id, reason=None):
        """Atomic duplicate-prevention: create a pending job or return the
        existing one (caller must not launch a duplicate worker).

        Returns {'job_id': id, 'already_active': bool} or None on failure.
        """
        if not self._check_conn():
            return None
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """INSERT INTO sync_jobs (chat_id, status, reason, started_at, updated_at)
                       VALUES (%s, 'pending', %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT (chat_id) WHERE status IN ('pending','running') DO NOTHING
                       RETURNING id""",
                    (str_id, reason or 'manual')
                )
                row = c.fetchone()
                if row:
                    job_id = row['id']
                    self.conn.commit()
                    return {'job_id': job_id, 'already_active': False}
                # Duplicate conflict — reuse the active job
                c.execute(
                    "SELECT id FROM sync_jobs WHERE chat_id=%s AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1",
                    (str_id,)
                )
                active = c.fetchone()
                self.conn.commit()
                if active:
                    return {'job_id': active['id'], 'already_active': True}
                return None
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"Error in request_sync_job ({str_id}): {e}")
                return None

    def set_sync_job_status(self, job_id, status, error=None, members_synced=None,
                            members_total=None, members_before=None):
        if not self._check_conn():
            return
        with self.lock:
            try:
                c = self.conn.cursor()
                updates = []
                params = []
                status_lower = str(status).lower()
                updates.append("status=%s")
                params.append(status_lower)
                updates.append("updated_at=CURRENT_TIMESTAMP")
                if status_lower == "running" and members_before is None:
                    updates.append("started_at=CURRENT_TIMESTAMP")
                if status_lower == "success":
                    updates.append("completed_at=CURRENT_TIMESTAMP")
                if members_synced is not None:
                    updates.append("members_synced=%s")
                    params.append(int(members_synced))
                if members_total is not None:
                    updates.append("members_total=%s")
                    params.append(int(members_total))
                if members_before is not None:
                    updates.append("members_before=%s")
                    params.append(int(members_before))
                if error is not None:
                    updates.append("error=%s")
                    params.append(str(error)[:500])
                params.append(job_id)
                c.execute(
                    f"UPDATE sync_jobs SET {', '.join(updates)} WHERE id=%s",
                    params
                )
                self.conn.commit()
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"Error in set_sync_job_status (job {job_id}): {e}")

    def get_sync_job(self, job_id):
        with self._read_guard():
            if not self._check_conn():
                return None
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT * FROM sync_jobs WHERE id=%s", (job_id,))
                    row = c.fetchone()
                    return dict(row) if row else None
                except Exception:
                    return None

    def get_sync_jobs(self, limit=50, chat_id=None, status=None):
        with self._read_guard():
            if not self._check_conn():
                return []
            with self.lock:
                try:
                    c = self.conn.cursor()
                    where = []
                    params = []
                    if chat_id is not None:
                        where.append("chat_id=%s")
                        params.append(str(chat_id))
                    if status:
                        where.append("status=%s")
                        params.append(str(status).lower())
                    sql = "SELECT * FROM sync_jobs"
                    if where:
                        sql += " WHERE " + " AND ".join(where)
                    sql += " ORDER BY created_at DESC LIMIT %s"
                    params.append(limit)
                    c.execute(sql, params)
                    return [dict(row) for row in c.fetchall()]
                except Exception:
                    return []

    def mark_stale_sync_jobs(self, older_than_minutes=1):
        """On app start: mark any pending/running job as interrupted — no
        in-process worker was alive when the process booted, so any persisted
        job is orphaned. Honest resume semantics; admin can re-trigger.
        """
        if not self._check_conn():
            return 0
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """UPDATE sync_jobs SET status='interrupted', error='Interrupted by process restart', updated_at=CURRENT_TIMESTAMP
                       WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '1 minute' * %s""",
                    (int(older_than_minutes),)
                )
                count = c.rowcount or 0
                self.conn.commit()
                return count
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"Error in mark_stale_sync_jobs: {e}")
                return 0

    def get_sync_stats(self):
        with self._read_guard():
            """Dashboard payload: registration totals + job counters + last sync."""
            if not self._check_conn():
                return {
                    "total_users": 0, "total_groups": 0, "total_memberships": 0,
                    "active_jobs": 0, "success_jobs": 0, "failed_jobs": 0,
                    "active_groups": [], "last_sync_at": None, "recent_jobs": [],
                }
            with self.lock:
                try:
                    c = self.conn.cursor()
                    c.execute("SELECT COUNT(*) AS n FROM users")
                    total_users = int(c.fetchone()['n'])
                    c.execute("SELECT COUNT(*) AS n FROM groups")
                    total_groups = int(c.fetchone()['n'])
                    c.execute("SELECT COUNT(*) AS n FROM group_members")
                    total_memberships = int(c.fetchone()['n'])
                    c.execute("SELECT COUNT(*) AS n FROM sync_jobs WHERE status IN ('pending','running')")
                    active_jobs = int(c.fetchone()['n'])
                    c.execute("SELECT COUNT(*) AS n FROM sync_jobs WHERE status='success'")
                    success_jobs = int(c.fetchone()['n'])
                    c.execute("SELECT COUNT(*) AS n FROM sync_jobs WHERE status IN ('failed','interrupted')")
                    failed_jobs = int(c.fetchone()['n'])
                    c.execute("SELECT chat_id, status, members_synced, members_total, started_at, created_at FROM sync_jobs WHERE status IN ('pending','running') ORDER BY created_at DESC")
                    active_groups = [dict(row) for row in c.fetchall()]
                    c.execute("SELECT MAX(completed_at) AS last FROM sync_jobs WHERE status='success'")
                    row = c.fetchone()
                    last_sync_at = row['last'] if row else None
                    c.execute("SELECT * FROM sync_jobs ORDER BY created_at DESC LIMIT 10")
                    recent_jobs = [dict(row) for row in c.fetchall()]
                    return {
                        "total_users": total_users,
                        "total_groups": total_groups,
                        "total_memberships": total_memberships,
                        "active_jobs": active_jobs,
                        "success_jobs": success_jobs,
                        "failed_jobs": failed_jobs,
                        "active_groups": active_groups,
                        "last_sync_at": last_sync_at.strftime("%Y-%m-%d %H:%M:%S") if last_sync_at else None,
                        "recent_jobs": recent_jobs,
                    }
                except Exception as e:
                    print(f"Error in get_sync_stats: {e}")
                    return {
                        "total_users": 0, "total_groups": 0, "total_memberships": 0,
                        "active_jobs": 0, "success_jobs": 0, "failed_jobs": 0,
                        "active_groups": [], "last_sync_at": None, "recent_jobs": [],
                    }

    def reconcile_group_users(self, chat_id):
        """Bulk-upsert global user rows for every registered member of the group.

        One query — no per-member round-trips. This is the only path that bulk-
        registers members; everything else goes through register_member.
        Returns the number of members reconciled.
        """
        if not self._check_conn():
            return 0
        str_id = str(chat_id)
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    "SELECT telegram_user_id FROM group_members WHERE telegram_group_id=%s",
                    (str_id,)
                )
                member_rows = [row['telegram_user_id'] for row in c.fetchall()]
                if not member_rows:
                    return 0
                c.execute(
                    """INSERT INTO users (user_id, name, username)
                       SELECT telegram_user_id, 'Unknown', NULL
                       FROM group_members WHERE telegram_group_id=%s
                       ON CONFLICT(user_id) DO NOTHING""",
                    (str_id,)
                )
                self.conn.commit()
                return len(member_rows)
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"Error in reconcile_group_users ({str_id}): {e}")
                return 0

db = Database()
