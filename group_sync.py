"""High-speed, per-group sync engine.

Runs in daemon threads so normal bot operations are never blocked.
Duplicate jobs for the same group are prevented atomically by the partial
unique index `idx_sync_jobs_active_per_group` on sync_jobs(chat_id) — the
request fails cleanly if a pending/running job already exists.

Progress is persisted in the `sync_jobs` table so it survives process
restarts; on startup, orphaned jobs are marked `interrupted` (resumable by
simply re-triggering the sync).

IMPORTANT: Telegram's Bot API cannot enumerate full group membership. The
job only ever reports members Telegram explicitly revealed (via join/leave/
chat_member events) — it never claims to have imported every member.
"""

import threading
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_tracked_job_threads = []  # test/diagnostic handle only

MAX_JOB_ATTEMPTS = 2       # one retry for transient failures
RETRY_BACKOFF_SECONDS = 1.5


# ──────────────────────────────────────────────────────────────
# Startup: mark orphaned jobs from a previous process
# ──────────────────────────────────────────────────────────────
def mark_stale_jobs_on_startup():
    """Called once at app startup. Any pending/running job is orphaned — no
    worker from this process is alive for it, so mark it interrupted and let
    the admin re-trigger via the dashboard (or just wait for new events)."""
    try:
        from database import db
        removed = db.mark_stale_sync_jobs(older_than_minutes=0)
        if removed:
            log.info("Marked %s interrupted sync job(s) from a previous process", removed)
        return removed
    except Exception as e:
        log.warning("Could not mark stale sync jobs: %s", e)
        return 0


# ──────────────────────────────────────────────────────────────
# Manual trigger (dashboard)
# ──────────────────────────────────────────────────────────────
def start_sync_job(chat_id, reason='manual', bot=None):
    """Create a sync job for `chat_id` and launch its worker thread.

    Returns (job_id, status_message). The worker never raises into callers —
    failures are recorded on the job itself.
    """
    try:
        from database import db

        result = db.request_sync_job(chat_id, reason)
        if result is None:
            return None, 'Could not create a sync job (database error)'

        if result['already_active']:
            return result['job_id'], 'An active sync job already exists for this group'

        job_id = result['job_id']
        thread = threading.Thread(
            target=_run_job, args=(job_id, bot),
            daemon=True, name=f'sync-job-{job_id}',
        )
        thread.start()
        _tracked_job_threads.append(thread)
        return job_id, 'queued'

    except Exception as e:
        log.exception("Failed to start sync job for %s", chat_id)
        return None, str(e)[:200]


def _run_job(job_id, bot=None):
    """Worker: reconcile group registration + bulk-upsert known members.

    No per-member queries — all member upserts happen in a single SQL
    statement. Telegram API is used only for an OPTIONAL, best-effort
    member-count refresh; failures are logged and never fatal.
    """
    from database import db

    last_error = None
    for attempt in range(1, MAX_JOB_ATTEMPTS + 1):
        try:
            job = db.get_sync_job(job_id)
            if not job:
                return
            chat_id = job['chat_id']

            db.set_sync_job_status(
                job_id, 'running',
                members_before=db.count_group_members(chat_id),
            )

            # Reconcile group registration (idempotent upsert)
            db.register_group(chat_id)

            # Bulk-upsert users for every known member of the group (1 query)
            reconciled = db.reconcile_group_users(chat_id)
            members_synced = db.count_group_members(chat_id)

            # Best-effort REAL member count via the officially supported API
            members_total = None
            if bot is not None:
                try:
                    members_total = bot.get_chat_member_count(int(chat_id))
                except Exception as e:
                    log.info("get_chat_member_count unavailable for %s: %s", chat_id, str(e)[:100])
                    members_total = None

            db.set_sync_job_status(
                job_id, 'success',
                members_synced=reconciled,
                members_total=members_total,
            )
            db.log_event(f"✅ Sync complete for group {chat_id}: {members_synced} known member(s)")
            return

        except Exception as e:
            last_error = e
            log.warning("Sync job %s attempt %d failed: %s", job_id, attempt, e)
            if attempt < MAX_JOB_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS)

    # All attempts failed — record the honest failure
    try:
        db.set_sync_job_status(job_id, 'failed', error=str(last_error))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────
# Read-side helpers (dashboard)
# ──────────────────────────────────────────────────────────────
def get_sync_dashboard_data():
    """Dashboard payload — no fabricated counts anywhere."""
    try:
        from database import db
        return db.get_sync_stats()
    except Exception as e:
        return {
            "total_users": 0, "total_groups": 0, "total_memberships": 0,
            "active_jobs": 0, "success_jobs": 0, "failed_jobs": 0,
            "active_groups": [], "last_sync_at": None, "recent_jobs": [],
            "error": str(e)[:200],
        }


def get_sync_job_detail(job_id):
    try:
        from database import db
        return db.get_sync_job(job_id)
    except Exception:
        return None
