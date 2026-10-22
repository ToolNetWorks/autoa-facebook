"""Auto Comment: Smart scheduler for Pipeline-level comment scheduling."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from app import db, iso

LOG = logging.getLogger("facebook-pipeline.auto-comment.scheduler")

#: Múi giờ mặc định khi Workspace chưa cấu hình.
DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"

Mode = Literal["group", "feed"]
Status = Literal["enabled", "disabled", "paused", "error"]
JobStatus = Literal["pending", "scheduled", "ready", "posting", "posted", "failed", "submitted_unverified", "cancelled", "duplicate_limit"]


@dataclass(frozen=True)
class AutoCommentSettings:
    """Pipeline-level auto comment configuration."""
    pipeline_id: int
    mode_group_enabled: bool
    mode_feed_enabled: bool
    start_hour: int  # 0-23
    end_hour: int    # 0-23
    active_days: str  # JSON array of weekday numbers 0-6 (Mon=0)
    timezone: str
    max_comments_per_account_per_day: int
    min_interval_seconds: int
    max_comments_per_group_per_day: int
    max_total_comments_per_day: int
    status: Status
    group_account_ids: list[int]  # specific accounts for group mode, empty = all
    feed_account_ids: list[int]   # specific accounts for feed mode, empty = all
    brand_guidelines: str
    group_default_rules: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CommentJob:
    """A scheduled comment job."""
    id: int
    pipeline_id: int
    mode: Mode
    source_id: int  # group_source_id or feed_source_id
    fb_post_id: str
    account_id: int
    comment_text: str
    status: JobStatus
    scheduled_for: str
    posted_at: str | None
    fb_comment_id: str | None
    error: str
    fingerprint_id: int | None
    idempotency_key: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AccountDailyLimits:
    """Daily limits for a specific account."""
    account_id: int
    date: str  # YYYY-MM-DD
    comments_posted: int
    last_comment_at: str | None


DEFAULT_MIN_INTERVAL_SECONDS = 300  # 5 minutes
MAX_CONCURRENT_POSTING = 2  # per pipeline


def _day_bounds_utc(pipeline_id: int, day: str) -> tuple[str, str]:
    """Hai moc UTC cua mot ngay theo mui gio cua Workspace."""
    name = DEFAULT_TIMEZONE
    with db() as conn:
        row = conn.execute("""SELECT timezone FROM auto_comment_settings WHERE pipeline_id=?""",
                           (int(pipeline_id),)).fetchone()
        if row is None:
            row = conn.execute("""SELECT timezone FROM pipeline_auto_post_settings
                                  WHERE pipeline_id=?""", (int(pipeline_id),)).fetchone()
    if row is not None and row["timezone"]:
        name = row["timezone"]
    try:
        import pytz
        zone = pytz.timezone(name)
    except Exception:
        zone = datetime.now().astimezone().tzinfo
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
    end = start + datetime.timedelta(days=1)
    return (start.astimezone(datetime.timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(datetime.timezone.utc).isoformat(timespec="seconds"))


def _workspace_day(pipeline_id: int) -> str:
    """Ngay theo mui gio cua Workspace, de han muc khong lech o ranh giooi."""
    with db() as conn:
        row = conn.execute("""SELECT timezone FROM auto_comment_settings WHERE pipeline_id=?""",
                           (int(pipeline_id),)).fetchone()
    if row is None:
        row = conn.execute("""SELECT timezone FROM pipeline_auto_post_settings
                              WHERE pipeline_id=?""", (int(pipeline_id),)).fetchone()
    name = (row["timezone"] if row else None) or DEFAULT_TIMEZONE
    try:
        import pytz
        zone = pytz.timezone(name)
    except Exception:
        zone = datetime.now().astimezone().tzinfo
    return datetime.now(zone).date().isoformat()


class AutoCommentScheduler:
    """Pipeline-level auto comment scheduler."""
    
    def __init__(self, pipeline_id: int):
        self.pipeline_id = pipeline_id
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
    
    def load_settings(self) -> AutoCommentSettings | None:
        """Load pipeline auto comment settings."""
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM auto_comment_settings WHERE pipeline_id = ?",
                (self.pipeline_id,)
            ).fetchone()
            if not row:
                return None
            
            import json
            d = dict(row)
            d["active_days"] = json.loads(d.get("active_days") or "[0,1,2,3,4,5,6]")
            d["group_account_ids"] = json.loads(d.get("group_account_ids") or "[]")
            d["feed_account_ids"] = json.loads(d.get("feed_account_ids") or "[]")
            return AutoCommentSettings(**d)
    
    def save_settings(self, settings: dict) -> AutoCommentSettings:
        """Create or update pipeline settings."""
        import json
        from app import iso
        
        now = iso()
        pipeline_id = self.pipeline_id
        
        with db() as conn:
            row = conn.execute(
                "SELECT id FROM auto_comment_settings WHERE pipeline_id = ?",
                (pipeline_id,)
            ).fetchone()
            
            if row:
                # Update
                set_clause = ", ".join(f"{k} = ?" for k in settings)
                values = list(settings.values()) + [iso(), pipeline_id]
                conn.execute(
                    f"UPDATE auto_comment_settings SET {set_clause}, updated_at = ? WHERE pipeline_id = ?",
                    values
                )
            else:
                # Insert
                cols = ["pipeline_id"] + list(settings.keys()) + ["created_at", "updated_at"]
                placeholders = ", ".join(["?"] * len(cols))
                values = [pipeline_id] + list(settings.values()) + [now, now]
                conn.execute(
                    f"INSERT INTO auto_comment_settings ({', '.join(cols)}) VALUES ({placeholders})",
                    values
                )
        
        return self.load_settings()
    
    def is_within_active_hours(self, settings: AutoCommentSettings) -> bool:
        """Check if current time is within active hours."""
        try:
            import pytz
            tz = pytz.timezone(settings.timezone)
        except Exception:
            tz = datetime.now().astimezone().tzinfo
        
        now = datetime.now(tz)
        current_hour = now.hour
        current_weekday = now.weekday()  # Mon=0
        
        # Check active days
        if current_weekday not in settings.active_days:
            return False
        
        # Check hours (handle overnight ranges)
        start = settings.start_hour
        end = settings.end_hour
        
        if start <= end:
            return start <= current_hour < end
        else:
            # Overnight range (e.g., 22:00 - 06:00)
            return current_hour >= start or current_hour < end
    
    def get_account_limits(self, account_id: int) -> AccountDailyLimits:
        """Get today's limits for an account, counted in the workspace day."""
        today = _workspace_day(self.pipeline_id)
        
        with db() as conn:
            row = conn.execute(
                """SELECT * FROM auto_comment_account_limits
                   WHERE account_id = ? AND date = ?""",
                (account_id, today)
            ).fetchone()
            
            if row:
                return AccountDailyLimits(**dict(row))
            
            return AccountDailyLimits(
                account_id=account_id,
                date=today,
                comments_posted=0,
                last_comment_at=None,
            )
    
    def can_account_post(self, settings: AutoCommentSettings, account_id: int, mode: Mode) -> tuple[bool, str]:
        """Check if an account can post a comment now."""
        # Check if account is in allowed list for this mode
        if mode == "group" and settings.group_account_ids:
            if account_id not in settings.group_account_ids:
                return False, "account_not_in_group_list"
        elif mode == "feed" and settings.feed_account_ids:
            if account_id not in settings.feed_account_ids:
                return False, "account_not_in_feed_list"
        
        limits = self.get_account_limits(account_id)
        
        # Daily limit per account
        if limits.comments_posted >= settings.max_comments_per_account_per_day:
            return False, "daily_account_limit_reached"
        
        # Min interval since last comment
        if limits.last_comment_at:
            try:
                last = datetime.fromisoformat(limits.last_comment_at)
                elapsed = (datetime.now() - last).total_seconds()
                if elapsed < settings.min_interval_seconds:
                    return False, f"min_interval_not_met ({elapsed:.0f}s < {settings.min_interval_seconds}s)"
            except Exception:
                pass
        
        return True, "OK"
    
    def get_pipeline_daily_count(self) -> int:
        """Get total comments posted today for this pipeline, in the workspace day."""
        today = _workspace_day(self.pipeline_id)
        start, end = _day_bounds_utc(self.pipeline_id, today)
        
        with db() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM auto_comment_jobs
                   WHERE pipeline_id = ? AND status = 'posted'
                     AND datetime(posted_at) >= datetime(?)
                     AND datetime(posted_at) < datetime(?)""",
                (self.pipeline_id, start, end)
            ).fetchone()
            return row["cnt"] if row else 0
    
    def can_pipeline_post(self, settings: AutoCommentSettings) -> tuple[bool, str]:
        """Check if pipeline can post more comments today."""
        if self.get_pipeline_daily_count() >= settings.max_total_comments_per_day:
            return False, "daily_pipeline_limit_reached"
        return True, "OK"
    
    def get_eligible_accounts(self, settings: AutoCommentSettings, mode: Mode) -> list[int]:
        """Get list of accounts eligible for commenting in this mode."""
        with db() as conn:
            if mode == "group" and settings.group_account_ids:
                account_ids = settings.group_account_ids
            elif mode == "feed" and settings.feed_account_ids:
                account_ids = settings.feed_account_ids
            else:
                # All accounts in pipeline
                rows = conn.execute(
                    "SELECT id FROM accounts WHERE pipeline_id = ? AND status = 'active'",
                    (self.pipeline_id,)
                ).fetchall()
                account_ids = [r["id"] for r in rows]
        
        # Filter by daily limits and interval
        eligible = []
        for acc_id in account_ids:
            ok, _ = self.can_account_post(settings, acc_id, mode)
            if ok:
                eligible.append(acc_id)
        
        return eligible
    
    def pick_account(self, settings: AutoCommentSettings, mode: Mode,
                     eligible_accounts: list[int]) -> int | None:
        """Pick the best account for next comment (least recently used)."""
        if not eligible_accounts:
            return None
        
        with db() as conn:
            # Get last comment time for each account
            best_account = None
            best_time = None
            
            for acc_id in eligible_accounts:
                limits = self.get_account_limits(acc_id)
                if limits.last_comment_at:
                    try:
                        t = datetime.fromisoformat(limits.last_comment_at)
                    except Exception:
                        t = datetime.min
                else:
                    t = datetime.min
                
                if best_time is None or t < best_time:
                    best_time = t
                    best_account = acc_id
            
            return best_account or eligible_accounts[0]


def create_comment_job(conn, pipeline_id: int, mode: Mode, source_id: int,
                       fb_post_id: str, account_id: int, comment_text: str,
                       fingerprint_id: int, scheduled_for: str) -> int:
    """Create a new comment job."""
    from app import iso
    
    idempotency_key = hashlib.sha256(
        f"{pipeline_id}:{mode}:{fb_post_id}:{account_id}:{comment_text}".encode()
    ).hexdigest()[:32]
    
    # Check idempotency
    existing = conn.execute(
        "SELECT id FROM auto_comment_jobs WHERE idempotency_key = ?",
        (idempotency_key,)
    ).fetchone()
    if existing:
        return existing["id"]
    
    cur = conn.execute(
        """INSERT INTO auto_comment_jobs
           (pipeline_id, mode, source_id, fb_post_id, account_id, comment_text,
            status, scheduled_for, fingerprint_id, idempotency_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?)""",
        (pipeline_id, mode, source_id, fb_post_id, account_id, comment_text,
         scheduled_for, fingerprint_id, idempotency_key, iso(), iso())
    )
    return cur.lastrowid


def get_ready_jobs(conn, pipeline_id: int, limit: int = 10) -> list[CommentJob]:
    """Get jobs ready to be posted (status=scheduled and scheduled_for <= now)."""
    now = iso()
    rows = conn.execute(
        """SELECT * FROM auto_comment_jobs
           WHERE pipeline_id = ? AND status = 'scheduled' AND scheduled_for <= ?
           ORDER BY scheduled_for LIMIT ?""",
        (pipeline_id, now, limit)
    ).fetchall()
    return [CommentJob(**dict(row)) for row in rows]


def update_job_status(conn, job_id: int, status: JobStatus, **extra) -> None:
    """Update job status."""
    from app import iso
    
    fields = {"status": status, "updated_at": iso()}
    fields.update(extra)
    
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    conn.execute(
        f"UPDATE auto_comment_jobs SET {set_clause} WHERE id = ?",
        values
    )


def mark_job_posted(conn, job_id: int, fb_comment_id: str) -> None:
    """Mark a job as successfully posted."""
    from app import iso
    update_job_status(conn, job_id, "posted", fb_comment_id=fb_comment_id, posted_at=iso())


def mark_job_failed(conn, job_id: int, error: str) -> None:
    """Mark a job as failed."""
    update_job_status(conn, job_id, "failed", error=error[:500])


def mark_job_submitted_unverified(conn, job_id: int) -> None:
    """Mark a job as submitted but not yet verified by Facebook."""
    update_job_status(conn, job_id, "submitted_unverified")


def increment_account_limit(conn, account_id: int, pipeline_id: int | None = None) -> None:
    """Increment daily comment count for an account, in the workspace day."""
    from app import iso
    today = (_workspace_day(pipeline_id) if pipeline_id
             else datetime.now().date().isoformat())
    
    conn.execute(
        """INSERT INTO auto_comment_account_limits (account_id, date, comments_posted, last_comment_at)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(account_id, date) DO UPDATE SET
             comments_posted = comments_posted + 1,
             last_comment_at = ?""",
        (account_id, today, iso(), iso())
    )


def check_account_restricted(conn, account_id: int) -> tuple[bool, str]:
    """Check if account is restricted (spam, checkpoint, comment limit)."""
    # This would check Facebook account status
    # For now, return not restricted
    return False, "OK"


import hashlib