"""Facebook Pipeline Manager: independently managed authorized group workflows.

Facebook discontinued Groups API. Browser jobs are best-effort, not a guarantee
of a published post; uncertain results require human verification.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from cookie_import import InvalidCookies, MAX_COOKIE_BYTES, parse_cookie_file
from profile_lock import ProfileBusy, profile_busy
from session_crypto import DecryptError, decrypt, encrypt
from schemas.ai_caption import (
    STATUS_APPROVED, STATUS_BLOCKED, STATUS_NEEDS_REVIEW, SEVERITY_HIGH,
    CaptionOptimizeRequest, CaptionApproveRequest, CaptionSettings, CaptionSettingsUpdate,
)
from services.ai_caption import optimize_caption
from services.caption_validator import analyze_caption
from services.ai_provider import ProviderError, load_config as load_ai_config
from services.auto_posting import (
    MODE_ANONYMOUS, MODE_NICKNAME, create_caption as build_caption, eligible_groups,
    idempotency_key, plan_post,
)
from services.image_analysis import analyze_image
from services.image_selection import score_candidate
from services.media_store import (
    album_root, pipeline_folder, safe_join,
)
from services import analytics as analytics_service
from services import posting_limits
from services import ctv_auto_post
from services import pipeline_scope
from services import media_service
from services import media_status
from scheduler import job_allocator
from scheduler import ctv_allocator
from scheduler import time_windows
from services import post_scheduler
from services import analytics_worker
from services import media_cloud
from api import ctv_auto_post as ctv_auto_post_api
from api import media as media_api
from api import groups as groups_api
from api import group_posts as group_posts_api
from api import verification as verification_api
from api import auto_cycles as auto_cycles_api
from services.auto_posting_cycles import engine as cycle_engine, runtime as cycle_runtime, repository as cycle_repository
from services.auto_posting_cycles import refresh as cycle_refresh
from services.account_identity import claim as claim_facebook_identity, DuplicateFacebookIdentity
from services.media_fingerprint import file_sha256, hamming_distance, perceptual_signature
from services.media_probe import ReelPolicy
from services.video_precheck import (
    CATEGORIES as PRECHECK_CATEGORIES, FINAL_BLOCKED, FINAL_CHECK_FAILED,
    FINAL_NEEDS_REVIEW, FINAL_PASS, run_precheck,
)
from services import group_screening
from services import group_verification
from services import group_membership
from services import group_classification
from services import group_eligibility
from services import group_targets
from services import group_join_worker
from services import destination_lock
from services.group_screening import (
    FRESHNESS_DAYS, MIN_CHECK_INTERVAL_SECONDS, RECHECK_COOLDOWN_SECONDS,
    build_evidence, classify, evidence_summary,
)
from services import account_session_state, group_job_lifecycle
from services.pipeline_membership_policy import MembershipConflict, require_available, unique_active_membership
from services.group_links import (
    InvalidGroupLink, group_alias_keys, group_display_name, normalize_group_url, sql_like_escape,
)
from auth import (
    DEFAULT_CTV_PERMISSIONS, KEY_TTL_DAYS, LOGIN_THROTTLE, PERMISSIONS, Principal, ROLE_ADMIN, ROLE_CTV,
    STATUS_ACTIVE, STATUS_SUSPENDED, create_session, hash_secret, hash_session_token, iso as auth_iso,
    key_prefix, needs_rehash, new_pipeline_key, normalise_permissions, purge_expired_sessions,
    resolve_principal, revoke_key_sessions, revoke_session, revoke_user_sessions, secure_equals,
    utcnow, verify_secret,
)

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
UPLOADS = DATA / "uploads"
EVIDENCE = DATA / "evidence"
PROFILES = DATA / "profiles"
DB_PATH = DATA / "manager.sqlite3"
for path in (DATA, UPLOADS, EVIDENCE, PROFILES):
    path.mkdir(parents=True, exist_ok=True)
    if path == PROFILES:
        path.chmod(0o700)

ADMIN_USER = os.getenv("APP_USERNAME", "admin")
ADMIN_PASS = os.getenv("APP_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET", "")
COOKIE_SECURE = os.getenv("APP_COOKIE_SECURE", "false").lower() == "true"
MAX_DAILY_POSTS = max(1, int(os.getenv("MAX_DAILY_POSTS", "5")))
MIN_ACCOUNT_GAP_SECONDS = max(60, int(os.getenv("MIN_ACCOUNT_GAP_SECONDS", "600")))
MIN_GROUP_GAP_HOURS = max(1, int(os.getenv("MIN_GROUP_GAP_HOURS", "24")))
SESSION_COOKIE = "fpm_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CURRENT_PRINCIPAL: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("fpm_principal", default=None)
LOG = logging.getLogger("facebook-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
STOP_EVENT = threading.Event()
IMPORT_LOCK = threading.Lock()
IMPORT_IN_PROGRESS: set[int] = set()


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds")


def parse_date(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Missing timezone")
        return result.astimezone(timezone.utc)
    except ValueError as error:
        raise HTTPException(422, "scheduled_at must be ISO 8601 with timezone") from error


@contextmanager
def db():
    connection = sqlite3.connect(str(DB_PATH), timeout=25)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=25000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS pipelines (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY, pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            label TEXT NOT NULL, profile_url TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, login_pid INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY, pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            label TEXT NOT NULL, url TEXT NOT NULL, group_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, approved_target INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, UNIQUE(pipeline_id, group_key)
        );
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY, pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            body TEXT NOT NULL, media_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            state TEXT NOT NULL DEFAULT 'queued', scheduled_at TEXT NOT NULL,
            started_at TEXT, updated_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, details TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            UNIQUE(post_id, group_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(state,scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_account ON jobs(account_id,updated_at);
        CREATE TABLE IF NOT EXISTS audits (
            id INTEGER PRIMARY KEY, category TEXT NOT NULL, entity_id INTEGER,
            message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_tasks (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
                task_type TEXT NOT NULL CHECK (task_type IN ('reel', 'video', 'comment')),
                body TEXT NOT NULL DEFAULT '',
                media_json TEXT NOT NULL DEFAULT '[]',
                target_url TEXT NOT NULL DEFAULT '',
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'awaiting_manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_pipeline ON manual_tasks(pipeline_id,id)")
        # --- multi-tenant authentication and per-pipeline authorisation ---
        # Every table below is additive. Existing pipelines, accounts, groups,
        # posts, jobs and manual tasks are left untouched.
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin','ctv')),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pipeline_memberships (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            permissions_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, pipeline_id)
        );
        CREATE TABLE IF NOT EXISTS pipeline_access_keys (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            key_prefix TEXT NOT NULL UNIQUE,
            key_hash TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            expires_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            pipeline_id INTEGER REFERENCES pipelines(id) ON DELETE CASCADE,
            key_id INTEGER REFERENCES pipeline_access_keys(id) ON DELETE SET NULL,
            session_hash TEXT NOT NULL UNIQUE,
            csrf_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY,
            actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL DEFAULT '',
            pipeline_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_caption_settings (
            pipeline_id INTEGER PRIMARY KEY REFERENCES pipelines(id) ON DELETE CASCADE,
            tone TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'vi',
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_membership_user ON pipeline_memberships(user_id,status);
        CREATE INDEX IF NOT EXISTS idx_membership_pipeline ON pipeline_memberships(pipeline_id,status);
        CREATE INDEX IF NOT EXISTS idx_keys_pipeline ON pipeline_access_keys(pipeline_id,revoked_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id,revoked_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_pipeline ON user_sessions(pipeline_id,revoked_at);
        CREATE INDEX IF NOT EXISTS idx_audit_pipeline ON audit_logs(pipeline_id,id);
        """)
        # Carry the legacy audit trail into the new table without losing rows.
        legacy = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        if legacy == 0:
            for row in conn.execute("SELECT category,entity_id,message,created_at FROM audits").fetchall():
                conn.execute("""INSERT INTO audit_logs(actor_id,actor_username,pipeline_id,action,detail,created_at)
                                VALUES(NULL,'',NULL,?,?,?)""",
                             (f"legacy.{row['category']}", str(row["message"])[:500], row["created_at"]))
        # Ownership of a job is now stored on the job itself, so every row can
        # be checked without joining across tables. Existing rows are backfilled
        # from their post; nothing is deleted.
        job_columns = {column[1] for column in conn.execute("PRAGMA table_info(jobs)")}
        if "pipeline_id" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN pipeline_id INTEGER")
        # Backfill on every start so rows written by older code keep an owner.
        conn.execute("""UPDATE jobs SET pipeline_id=
                        (SELECT pipeline_id FROM posts WHERE posts.id=jobs.post_id)
                        WHERE pipeline_id IS NULL""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pipeline ON jobs(pipeline_id,id)")
        # --- AI Caption Automation -------------------------------------------
        # The per-workspace configuration is extended in place so workspaces
        # created before this upgrade keep their settings.
        caption_columns = {column[1] for column in conn.execute("PRAGMA table_info(ai_caption_settings)")}
        for name, ddl in (
            ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("model", "TEXT NOT NULL DEFAULT ''"),
            ("target", "TEXT NOT NULL DEFAULT 'group'"),
            ("add_hashtags", "INTEGER NOT NULL DEFAULT 0"),
            ("preserve_meaning", "INTEGER NOT NULL DEFAULT 1"),
            ("optimize_for_reel", "INTEGER NOT NULL DEFAULT 0"),
            ("optimize_for_group", "INTEGER NOT NULL DEFAULT 0"),
            ("auto_use_when_safe", "INTEGER NOT NULL DEFAULT 0"),
            ("require_manual_review", "INTEGER NOT NULL DEFAULT 1"),
            ("max_hashtags", "INTEGER NOT NULL DEFAULT 5"),
        ):
            if name not in caption_columns:
                conn.execute(f"ALTER TABLE ai_caption_settings ADD COLUMN {name} {ddl}")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_caption_records (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            original_caption TEXT NOT NULL,
            optimized_caption TEXT NOT NULL DEFAULT '',
            ai_status TEXT NOT NULL DEFAULT 'PENDING',
            risk_level TEXT NOT NULL DEFAULT 'unknown',
            meaning_preserved INTEGER NOT NULL DEFAULT 1,
            approved INTEGER NOT NULL DEFAULT 0,
            approved_at TEXT,
            ai_model TEXT NOT NULL DEFAULT '',
            ai_provider TEXT NOT NULL DEFAULT 'local',
            ai_engine TEXT NOT NULL DEFAULT 'local',
            target TEXT NOT NULL DEFAULT 'group',
            topic TEXT NOT NULL DEFAULT '',
            intent TEXT NOT NULL DEFAULT '',
            issues_json TEXT NOT NULL DEFAULT '[]',
            changes_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_caption_records_pipeline ON ai_caption_records(pipeline_id,id);
        """)
        for table in ("posts", "manual_tasks"):
            columns = {column[1] for column in conn.execute(f"PRAGMA table_info({table})")}
            if "ai_record_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN ai_record_id INTEGER")
        # --- group configuration and posting identity ------------------------
        # Everything below is additive, existing groups and jobs keep working
        # and are given the safe default of anonymous posting.
        group_columns = {column[1] for column in conn.execute("PRAGMA table_info(groups)")}
        for name, ddl in (
            ("group_name", "TEXT NOT NULL DEFAULT ''"),
            ("source_key", "TEXT NOT NULL DEFAULT ''"),
            ("posting_mode", "TEXT NOT NULL DEFAULT 'anonymous'"),
            ("nickname", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("check_status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("check_details_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("last_checked_at", "TEXT"),
            ("can_post", "INTEGER NOT NULL DEFAULT 0"),
            ("is_member", "INTEGER NOT NULL DEFAULT 0"),
            ("supports_anonymous", "INTEGER NOT NULL DEFAULT 0"),
            ("supports_nickname", "INTEGER NOT NULL DEFAULT 0"),
            ("nickname_available", "INTEGER NOT NULL DEFAULT 0"),
            ("requires_approval", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in group_columns:
                conn.execute(f"ALTER TABLE groups ADD COLUMN {name} {ddl}")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS group_accounts (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            posting_mode TEXT NOT NULL DEFAULT 'anonymous',
            nickname TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            facebook_nickname TEXT NOT NULL DEFAULT '',
            nickname_status TEXT NOT NULL DEFAULT '',
            confirmed_at TEXT NOT NULL DEFAULT '',
            last_verified_at TEXT NOT NULL DEFAULT '',
            UNIQUE(account_id, group_id)
        );
        CREATE INDEX IF NOT EXISTS idx_group_accounts_group ON group_accounts(group_id,status);
        CREATE INDEX IF NOT EXISTS idx_group_accounts_pipeline ON group_accounts(pipeline_id,account_id);
        """)
        job_columns = {column[1] for column in conn.execute("PRAGMA table_info(jobs)")}
        for name, ddl in (
            ("requested_posting_mode", "TEXT NOT NULL DEFAULT 'anonymous'"),
            ("requested_nickname", "TEXT NOT NULL DEFAULT ''"),
            ("actual_posting_identity", "TEXT NOT NULL DEFAULT ''"),
            ("verification_status", "TEXT NOT NULL DEFAULT 'unverified'"),
            ("posting_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("failure_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in job_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        # A group that already exists keeps its account; the assignment table
        # is filled from that legacy column so both agree.
        conn.execute("""INSERT OR IGNORE INTO group_accounts
            (pipeline_id,account_id,group_id,posting_mode,nickname,status,created_at,updated_at)
            SELECT g.pipeline_id, g.account_id, g.id, g.posting_mode, g.nickname, 'active', g.created_at, g.created_at
            FROM groups g WHERE g.account_id IS NOT NULL""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_group ON jobs(group_id,account_id,state)")
        # --- AI video pre-check ------------------------------------------------
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS video_prechecks (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            media_id TEXT NOT NULL DEFAULT '',
            media_path TEXT NOT NULL DEFAULT '',
            video_sha256 TEXT NOT NULL DEFAULT '',
            video_signature TEXT NOT NULL DEFAULT '',
            caption_checked TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            progress TEXT NOT NULL DEFAULT '',
            progress_step INTEGER NOT NULL DEFAULT 0,
            total_steps INTEGER NOT NULL DEFAULT 9,
            technical_result_json TEXT NOT NULL DEFAULT '{}',
            visual_result_json TEXT NOT NULL DEFAULT '{}',
            audio_result_json TEXT NOT NULL DEFAULT '{}',
            ocr_result_json TEXT NOT NULL DEFAULT '{}',
            caption_result_json TEXT NOT NULL DEFAULT '{}',
            copyright_result_json TEXT NOT NULL DEFAULT '{}',
            issues_json TEXT NOT NULL DEFAULT '[]',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            recommendations_json TEXT NOT NULL DEFAULT '[]',
            score REAL NOT NULL DEFAULT 0,
            model_version TEXT NOT NULL DEFAULT '',
            vision_model TEXT NOT NULL DEFAULT '',
            policy_version TEXT NOT NULL DEFAULT '',
            copyright_status TEXT NOT NULL DEFAULT '',
            checked_scope TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            approved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            approved_at TEXT,
            approval_note TEXT NOT NULL DEFAULT '',
            checked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_fingerprints (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER REFERENCES pipelines(id) ON DELETE CASCADE,
            sha256 TEXT NOT NULL,
            signature TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'known',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(sha256, path)
        );
        CREATE INDEX IF NOT EXISTS idx_prechecks_pipeline ON video_prechecks(pipeline_id,id);
        CREATE INDEX IF NOT EXISTS idx_prechecks_status ON video_prechecks(status,pipeline_id);
        CREATE INDEX IF NOT EXISTS idx_fingerprints_pipeline ON media_fingerprints(pipeline_id,sha256);

        CREATE TABLE IF NOT EXISTS media_assets (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            uploader_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            storage_key TEXT NOT NULL UNIQUE,
            thumbnail_key TEXT NOT NULL DEFAULT '',
            original_name TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT NOT NULL DEFAULT '',
            signature TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            description TEXT NOT NULL DEFAULT '',
            safety_status TEXT NOT NULL DEFAULT 'unknown',
            safety_json TEXT NOT NULL DEFAULT '{}',
            analysis_status TEXT NOT NULL DEFAULT 'pending',
            usable INTEGER NOT NULL DEFAULT 0,
            locked INTEGER NOT NULL DEFAULT 0,
            saved INTEGER NOT NULL DEFAULT 0,
            usage_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_tags (
            id INTEGER PRIMARY KEY,
            media_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(media_id, tag)
        );
        CREATE TABLE IF NOT EXISTS media_usage_history (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            media_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
            job_id INTEGER,
            used_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS media_analysis_results (
            id INTEGER PRIMARY KEY,
            media_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'unknown',
            description TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            risk TEXT NOT NULL DEFAULT 'none',
            findings_json TEXT NOT NULL DEFAULT '[]',
            model_version TEXT NOT NULL DEFAULT '',
            checked_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auto_post_configs (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL UNIQUE REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            group_ids_json TEXT NOT NULL DEFAULT '[]',
            topic TEXT NOT NULL DEFAULT '',
            ai_caption INTEGER NOT NULL DEFAULT 1,
            daily_limit INTEGER NOT NULL DEFAULT 3,
            require_approval INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 0,
            tone TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auto_post_jobs (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            config_id INTEGER REFERENCES auto_post_configs(id) ON DELETE SET NULL,
            media_id INTEGER REFERENCES media_assets(id) ON DELETE SET NULL,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
            caption TEXT NOT NULL DEFAULT '',
            posting_mode TEXT NOT NULL DEFAULT 'anonymous',
            nickname TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            needs_approval INTEGER NOT NULL DEFAULT 1,
            approved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            approved_at TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_media_pipeline ON media_assets(pipeline_id,id);
        CREATE INDEX IF NOT EXISTS idx_media_usable ON media_assets(pipeline_id,usable,safety_status);
        CREATE INDEX IF NOT EXISTS idx_media_sha ON media_assets(pipeline_id,sha256);
        CREATE INDEX IF NOT EXISTS idx_media_tags_tag ON media_tags(tag);
        CREATE INDEX IF NOT EXISTS idx_media_usage ON media_usage_history(media_id,used_at);
        CREATE INDEX IF NOT EXISTS idx_auto_jobs_pipeline ON auto_post_jobs(pipeline_id,status,id);
        CREATE INDEX IF NOT EXISTS idx_auto_jobs_media ON auto_post_jobs(media_id,group_id);

        -- Auto Comment tables
        CREATE TABLE IF NOT EXISTS auto_comment_settings (
            pipeline_id INTEGER PRIMARY KEY REFERENCES pipelines(id) ON DELETE CASCADE,
            mode_group_enabled INTEGER NOT NULL DEFAULT 0,
            mode_feed_enabled INTEGER NOT NULL DEFAULT 0,
            start_hour INTEGER NOT NULL DEFAULT 9,
            end_hour INTEGER NOT NULL DEFAULT 21,
            active_days_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
            timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
            max_comments_per_account_per_day INTEGER NOT NULL DEFAULT 5,
            min_interval_seconds INTEGER NOT NULL DEFAULT 300,
            max_comments_per_group_per_day INTEGER NOT NULL DEFAULT 3,
            max_total_comments_per_day INTEGER NOT NULL DEFAULT 50,
            status TEXT NOT NULL DEFAULT 'disabled',
            group_account_ids_json TEXT NOT NULL DEFAULT '[]',
            feed_account_ids_json TEXT NOT NULL DEFAULT '[]',
            brand_guidelines TEXT NOT NULL DEFAULT '',
            group_default_rules TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auto_comment_group_sources (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            fb_group_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            allow_auto_comment INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(pipeline_id, fb_group_id)
        );
        CREATE TABLE IF NOT EXISTS auto_comment_feed_sources (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            name TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'keyword',
            target_id TEXT NOT NULL DEFAULT '',
            topics_json TEXT NOT NULL DEFAULT '[]',
            excluded_keywords_json TEXT NOT NULL DEFAULT '[]',
            excluded_authors_json TEXT NOT NULL DEFAULT '[]',
            allowed_domains_json TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auto_comment_jobs (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK(mode IN ('group','feed')),
            source_id INTEGER NOT NULL,
            fb_post_id TEXT NOT NULL,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            comment_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('pending','scheduled','ready','posting','posted','failed','submitted_unverified','cancelled','duplicate_limit')),
            scheduled_for TEXT NOT NULL,
            posted_at TEXT,
            fb_comment_id TEXT,
            error TEXT NOT NULL DEFAULT '',
            fingerprint_id INTEGER,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auto_comment_job_fingerprints (
            job_id INTEGER NOT NULL REFERENCES auto_comment_jobs(id) ON DELETE CASCADE,
            fingerprint_id INTEGER NOT NULL REFERENCES comment_fingerprints(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (job_id, fingerprint_id)
        );
        CREATE TABLE IF NOT EXISTS comment_fingerprints (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            normalized_text TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            use_count INTEGER NOT NULL DEFAULT 0,
            first_used_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auto_comment_account_limits (
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            comments_posted INTEGER NOT NULL DEFAULT 0,
            last_comment_at TEXT,
            PRIMARY KEY (account_id, date)
        );
        CREATE TABLE IF NOT EXISTS auto_comment_activity_history (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            job_id INTEGER REFERENCES auto_comment_jobs(id) ON DELETE SET NULL,
            fb_post_id TEXT NOT NULL,
            comment_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            fb_comment_id TEXT,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ac_jobs_pipeline ON auto_comment_jobs(pipeline_id,status,scheduled_for);
        CREATE INDEX IF NOT EXISTS idx_ac_jobs_account ON auto_comment_jobs(account_id,status);
        CREATE INDEX IF NOT EXISTS idx_ac_jobs_post ON auto_comment_jobs(fb_post_id);
        CREATE INDEX IF NOT EXISTS idx_ac_fingerprints_pipeline ON comment_fingerprints(pipeline_id,content_hash);
        CREATE INDEX IF NOT EXISTS idx_ac_activity_pipeline ON auto_comment_activity_history(pipeline_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_ac_group_src_pipeline ON auto_comment_group_sources(pipeline_id,is_active);
        CREATE INDEX IF NOT EXISTS idx_ac_feed_src_pipeline ON auto_comment_feed_sources(pipeline_id,is_active);
        """)
        # --- automatic group screening -----------------------------------------
        group_columns = {column[1] for column in conn.execute("PRAGMA table_info(groups)")}
        for name, ddl in (
            ("manual_review", "INTEGER NOT NULL DEFAULT 0"),
            ("screening_status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("screening_source", "TEXT NOT NULL DEFAULT 'none'"),
            ("screening_confidence", "REAL NOT NULL DEFAULT 0"),
            ("screening_checked_at", "TEXT"),
        ):
            if name not in group_columns:
                conn.execute(f"ALTER TABLE groups ADD COLUMN {name} {ddl}")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS group_screening_results (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            moderation_status TEXT NOT NULL DEFAULT 'unknown',
            verification_source TEXT NOT NULL DEFAULT 'none',
            confidence REAL NOT NULL DEFAULT 0,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            last_checked_at TEXT,
            expires_at TEXT,
            last_direct_post_at TEXT,
            last_pending_post_at TEXT,
            last_rejected_post_at TEXT,
            direct_posts INTEGER NOT NULL DEFAULT 0,
            pending_posts INTEGER NOT NULL DEFAULT 0,
            rejected_posts INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id, group_id)
        );
        CREATE TABLE IF NOT EXISTS screening_queue (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            state TEXT NOT NULL DEFAULT 'queued',
            force INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
         CREATE INDEX IF NOT EXISTS idx_screening_queue_state ON screening_queue(state,id);
         CREATE INDEX IF NOT EXISTS idx_screening_results_pipeline ON group_screening_results(pipeline_id,group_id);
         """)
        cycle_refresh.recover_probes(conn,iso())
        # V2026-10-20: add OCR/category/issues/policy columns to video_prechecks
        _pc = {c[1] for c in conn.execute("PRAGMA table_info(video_prechecks)")}
        for _col, _ddl in [
            ("ocr_result_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("issues_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("vision_model", "TEXT NOT NULL DEFAULT ''"),
            ("policy_version", "TEXT NOT NULL DEFAULT ''"),
            ("copyright_status", "TEXT NOT NULL DEFAULT ''"),
            ("checked_scope", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if _col not in _pc:
                conn.execute(f"ALTER TABLE video_prechecks ADD COLUMN {_col} {_ddl}")
        # New V4 jobs must never fall back from anonymous to named posting.
        post_columns = {column[1] for column in conn.execute("PRAGMA table_info(posts)")}
        if "anonymous_only" not in post_columns:
            conn.execute("ALTER TABLE posts ADD COLUMN anonymous_only INTEGER NOT NULL DEFAULT 0")
        task_columns = {column[1] for column in conn.execute("PRAGMA table_info(manual_tasks)")}
        if "auto_publish" not in task_columns:
            conn.execute("ALTER TABLE manual_tasks ADD COLUMN auto_publish INTEGER NOT NULL DEFAULT 0")
        if "details" not in task_columns:
            conn.execute("ALTER TABLE manual_tasks ADD COLUMN details TEXT NOT NULL DEFAULT ''")
        if "evidence" not in task_columns:
            conn.execute("ALTER TABLE manual_tasks ADD COLUMN evidence TEXT NOT NULL DEFAULT ''")
        # Existing jobs may have been scheduled as named or text-only posts.
        # Preserve the records but stop them; never silently change their privacy.
        conn.execute("""UPDATE jobs SET state='needs_review',
            details='Legacy job: anonymous posting was not explicitly required; not automatically published',
            updated_at=? WHERE state='queued' AND post_id IN
            (SELECT id FROM posts WHERE anonymous_only=0)""", (iso(),))
        conn.execute("""UPDATE manual_tasks SET status='needs_review',
            details='Reel worker interrupted; verify on Facebook before retrying',updated_at=?
            WHERE status='running'""", (iso(),))
        # Add non-sensitive account metadata without touching existing profiles or jobs.
        columns = {column[1] for column in conn.execute("PRAGMA table_info(accounts)")}
        if "auth_method" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'browser'")
        if "cookie_updated_at" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN cookie_updated_at TEXT")
        if "fb_uid_fingerprint" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN fb_uid_fingerprint TEXT")
        # Session check results. Every column is additive with a default, so
        # accounts created before this upgrade keep working untouched.
        if "session_status" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN session_status TEXT NOT NULL DEFAULT 'unknown'")
        if "session_checked_at" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN session_checked_at TEXT")
        if "session_details" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN session_details TEXT NOT NULL DEFAULT ''")
        if "fb_name" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN fb_name TEXT NOT NULL DEFAULT ''")
        if "fb_avatar" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN fb_avatar TEXT NOT NULL DEFAULT ''")
        if "fb_uid_enc" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN fb_uid_enc TEXT NOT NULL DEFAULT ''")
        if "updated_at" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN updated_at TEXT")
        # Jobs abandoned by a crashed worker must never be blindly reposted.
        conn.execute("UPDATE jobs SET state='needs_review', details='Worker interrupted; verify manually before retrying', updated_at=? WHERE state='running'", (iso(),))
    migrate_v6_ownership()
    migrate_v7_analytics()
    migrate_v8_pipeline_auto_post()
    migrate_v9_posting_schedule()
    migrate_v10_auto_comment()
    migrate_v11_ctv_auto_post()
    migrate_v12_screening_classification()
    migrate_v13_post_verification()
    migrate_v14_workspace_config()
    migrate_v15_group_classification()
    migrate_v16_pending_approval_limit()
    migrate_v17_campaign_idempotency()
    migrate_v18_destination_lock()


def migrate_v17_campaign_idempotency():
    """V17: khoá idempotency cho job cua chien dich.

    Mot chien dich tao nhieu job, nen can mot khoa de goi lai cung chien dich
    khong tao trung job. Cot moi nen khong co du lieu nao bi mat.
    """
    with db() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "idempotency_key" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_jobs_idempotency
                            ON jobs(pipeline_id, idempotency_key)""")


def migrate_v18_destination_lock():
    """V18: tạo bảng destination_locks nếu chưa tồn tại.

    DestinationLockService được dùng bởi ctv_allocator và job_allocator để gắn
    footer contact vào caption. Bảng mới nên không có dữ liệu nào bị mất.
    """
    with db() as conn:
        destination_lock.ensure_schema(conn)


def migrate_v16_pending_approval_limit():
    """V16: giới hạn số bài chờ quản trị group duyệt được chạy cùng lúc."""
    with db() as conn:
        for table in ("pipeline_auto_post_settings", "ctv_auto_post_settings"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "max_pending_approvals" not in columns:
                conn.execute(f"""ALTER TABLE {table} ADD COLUMN max_pending_approvals INTEGER
                                  NOT NULL DEFAULT 1""")


def migrate_v15_group_classification():
    """V15: phân loại group và khả năng đăng, tách khỏi bằng chứng.

    Bảng mới nên không có dữ liệu nào bị mất. Phân loại của các cặp đã có
    bằng chứng đăng bài được dựng lại từ chính bằng chứng đó, không suy đoán.
    """
    with db() as conn:
        group_classification.ensure_schema(conn)
        rows = conn.execute("""SELECT r.pipeline_id, r.account_id, r.group_id, r.verification_status,
                                     r.evidence_reference, g.requires_approval
                              FROM group_post_verifications r
                              JOIN groups g ON g.id=r.group_id""").fetchall()
        for row in rows:
            evidence = {}
            screening = conn.execute("""SELECT evidence_json FROM group_screening_results
                                        WHERE account_id=? AND group_id=?""",
                                     (row["account_id"], row["group_id"])).fetchone()
            if screening is not None:
                try:
                    evidence = json.loads(screening["evidence_json"] or "{}")
                except (TypeError, ValueError):
                    evidence = {}
            group_classification.record_outcome(
                conn, row["pipeline_id"], row["account_id"], row["group_id"],
                row["verification_status"], group_requires_approval=bool(row["requires_approval"]),
                evidence=evidence, evidence_reference=str(row["evidence_reference"] or ""))


def migrate_v14_workspace_config():
    """V14: một Workspace có đúng một cấu hình tự động đăng.

    Mô hình là 1 Pipeline = 1 CTV = 1 Workspace, nên cấu hình tự động đăng
    thuộc về Workspace chứ không thuộc về từng tài khoản. Cấu hình cũ được
    giữ nguyên; nếu một Workspace từng có nhiều dòng thì dòng cập nhật gần
    nhất được giữ, các dòng còn lại được chuyển sang bảng lưu trữ chứ không
    bị xoá.
    """
    with db() as conn:
        ctv_auto_post.ensure_schema(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(ctv_auto_post_settings)")}
        if not columns:
            return
        has_pipeline_unique = False
        for index in conn.execute("PRAGMA index_list(ctv_auto_post_settings)").fetchall():
            if not index["unique"]:
                continue
            names = [row["name"] for row in conn.execute(
                f"PRAGMA index_info('{index['name']}')").fetchall()]
            if names == ["pipeline_id"]:
                has_pipeline_unique = True
        if has_pipeline_unique:
            return

        keep = {row["pipeline_id"]: row["id"] for row in conn.execute(
            """SELECT pipeline_id, MAX(id) id FROM ctv_auto_post_settings
               GROUP BY pipeline_id""").fetchall()}
        conn.execute("""CREATE TABLE IF NOT EXISTS ctv_auto_post_settings_archive (
            id INTEGER PRIMARY KEY, pipeline_id INTEGER NOT NULL, ctv_user_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL, archived_at TEXT NOT NULL)""")
        conn.execute("DROP TABLE IF EXISTS ctv_auto_post_settings_keep")
        conn.executescript(ctv_auto_post.TABLE_DDL.replace(
            "CREATE TABLE IF NOT EXISTS ctv_auto_post_settings",
            "CREATE TABLE ctv_auto_post_settings_keep"))
        for row in conn.execute("SELECT * FROM ctv_auto_post_settings").fetchall():
            if keep.get(row["pipeline_id"]) == row["id"]:
                conn.execute(f"""INSERT INTO ctv_auto_post_settings_keep
                    ({",".join(row.keys())}) VALUES ({",".join("?" for _ in row.keys())})""",
                             tuple(row.values()))
            else:
                conn.execute("""INSERT INTO ctv_auto_post_settings_archive
                                (pipeline_id,ctv_user_id,payload_json,archived_at) VALUES(?,?,?,?)""",
                             (row["pipeline_id"], row["ctv_user_id"],
                              json.dumps(dict(row), ensure_ascii=False), iso()))
        conn.execute("DROP TABLE ctv_auto_post_settings")
        conn.execute("ALTER TABLE ctv_auto_post_settings_keep RENAME TO ctv_auto_post_settings")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_ctv_auto_post_owner
                        ON ctv_auto_post_settings(pipeline_id, ctv_user_id)""")


def migrate_v13_post_verification():
    """V13: xác minh quyền đăng bằng bằng chứng đăng bài thật.

    Bảng bằng chứng và bảng tư cách thành viên là mới, nên không có dữ liệu
    nào bị mất. Kết quả kiểm tra cũ **không** được biến thành đã xác minh: một
    lần kiểm tra bằng trình duyệt không phải là bằng chứng đăng bài.
    """
    with db() as conn:
        group_verification.ensure_schema(conn)
        group_membership.ensure_schema(conn)
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "is_verification" not in job_columns:
            # Bài xác minh là bài đang tự kiểm chứng, nên nó được phép đi
            # trước khi cặp Facebook–Group có bằng chứng.
            conn.execute("ALTER TABLE jobs ADD COLUMN is_verification INTEGER NOT NULL DEFAULT 0")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(groups)")}
        if "auto_join" not in columns:
            conn.execute("ALTER TABLE groups ADD COLUMN auto_join INTEGER NOT NULL DEFAULT 0")
        if "join_answers_json" not in columns:
            conn.execute("""ALTER TABLE groups ADD COLUMN join_answers_json TEXT NOT NULL
                            DEFAULT '{}'""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_groups_auto_join
                        ON groups(pipeline_id, auto_join)""")
        # Group chưa có bằng chứng đăng bài thì bắt đầu ở trạng thái chưa xác minh.
        conn.execute("""UPDATE groups SET screening_status=? WHERE screening_status NOT IN
                        ('verified_posted','pending_group_approval','submitted_unverified',
                         'test_in_progress','rejected','restricted_confirmed','check_failed')""",
                     (group_verification.NOT_TESTED,))


def migrate_v12_screening_classification():
    """V12: phân loại lại kết quả kiểm tra group theo bằng chứng đang có.

    Trước đây "chưa tham gia group" và "không thấy ô soạn bài" bị ghi thành
    "Facebook hạn chế quyền đăng". Bằng chứng không thay đổi, chỉ cách đọc
    nó, nên không có dữ liệu nào bị mất.
    """
    with db() as conn:
        changed = group_screening.reclassify_stored(conn)
    if changed:
        LOG.info("Phan loai lai %d ket qua kiem tra group", changed)


def migrate_v11_ctv_auto_post():
    """V11: mỗi CTV có cấu hình tự động đăng group riêng.

    Bảng mới hoàn toàn độc lập với cấu hình chung của Pipeline, nên cấu hình
    đang chạy của hệ thống được giữ nguyên. Job tự động đăng được ghi thêm
    chủ sở hữu để truy vết và thống kê đúng cho từng CTV.
    """
    with db() as conn:
        ctv_auto_post.ensure_schema(conn)
        # CTV đã có sẵn trong hệ thống cần quyền tự thiết lập lịch của riêng mình.
        # Quyền cấu hình chung của Pipeline vẫn phải được cấp riêng.
        rows = conn.execute("""SELECT m.id, m.permissions_json FROM pipeline_memberships m
                               JOIN users u ON u.id=m.user_id
                               WHERE m.status='active' AND u.role='ctv'""").fetchall()
        for row in rows:
            try:
                permissions = set(json.loads(row["permissions_json"] or "[]"))
            except (TypeError, ValueError):
                permissions = set()
            if "auto_post.self_configure" in permissions:
                continue
            permissions.add("auto_post.self_configure")
            conn.execute("UPDATE pipeline_memberships SET permissions_json=? WHERE id=?",
                         (json.dumps(sorted(permissions)), row["id"]))


def migrate_v10_auto_comment():
    """V10: Auto Comment system tables."""
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS auto_comment_settings (
                pipeline_id INTEGER PRIMARY KEY REFERENCES pipelines(id) ON DELETE CASCADE,
                mode_group_enabled INTEGER NOT NULL DEFAULT 0,
                mode_feed_enabled INTEGER NOT NULL DEFAULT 0,
                start_hour INTEGER NOT NULL DEFAULT 9,
                end_hour INTEGER NOT NULL DEFAULT 21,
                active_days_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
                timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
                max_comments_per_account_per_day INTEGER NOT NULL DEFAULT 5,
                min_interval_seconds INTEGER NOT NULL DEFAULT 300,
                max_comments_per_group_per_day INTEGER NOT NULL DEFAULT 3,
                max_total_comments_per_day INTEGER NOT NULL DEFAULT 50,
                status TEXT NOT NULL DEFAULT 'disabled',
                group_account_ids_json TEXT NOT NULL DEFAULT '[]',
                feed_account_ids_json TEXT NOT NULL DEFAULT '[]',
                brand_guidelines TEXT NOT NULL DEFAULT '',
                group_default_rules TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_comment_group_sources (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                fb_group_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                allow_auto_comment INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(pipeline_id, fb_group_id)
            );
            CREATE TABLE IF NOT EXISTS auto_comment_feed_sources (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                name TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'keyword',
                target_id TEXT NOT NULL DEFAULT '',
                topics_json TEXT NOT NULL DEFAULT '[]',
                excluded_keywords_json TEXT NOT NULL DEFAULT '[]',
                excluded_authors_json TEXT NOT NULL DEFAULT '[]',
                allowed_domains_json TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_comment_jobs (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                mode TEXT NOT NULL CHECK(mode IN ('group','feed')),
                source_id INTEGER NOT NULL,
                fb_post_id TEXT NOT NULL,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                comment_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('pending','scheduled','ready','posting','posted','failed','submitted_unverified','cancelled','duplicate_limit')),
                scheduled_for TEXT NOT NULL,
                posted_at TEXT,
                fb_comment_id TEXT,
                error TEXT NOT NULL DEFAULT '',
                fingerprint_id INTEGER,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_comment_job_fingerprints (
                job_id INTEGER NOT NULL REFERENCES auto_comment_jobs(id) ON DELETE CASCADE,
                fingerprint_id INTEGER NOT NULL REFERENCES comment_fingerprints(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (job_id, fingerprint_id)
            );
            CREATE TABLE IF NOT EXISTS comment_fingerprints (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                normalized_text TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                use_count INTEGER NOT NULL DEFAULT 0,
                first_used_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_comment_account_limits (
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                comments_posted INTEGER NOT NULL DEFAULT 0,
                last_comment_at TEXT,
                PRIMARY KEY (account_id, date)
            );
            CREATE TABLE IF NOT EXISTS auto_comment_activity_history (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                job_id INTEGER REFERENCES auto_comment_jobs(id) ON DELETE SET NULL,
                fb_post_id TEXT NOT NULL,
                comment_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                fb_comment_id TEXT,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ac_jobs_pipeline ON auto_comment_jobs(pipeline_id,status,scheduled_for);
            CREATE INDEX IF NOT EXISTS idx_ac_jobs_account ON auto_comment_jobs(account_id,status);
            CREATE INDEX IF NOT EXISTS idx_ac_jobs_post ON auto_comment_jobs(fb_post_id);
            CREATE INDEX IF NOT EXISTS idx_ac_fingerprints_pipeline ON comment_fingerprints(pipeline_id,content_hash);
            CREATE INDEX IF NOT EXISTS idx_ac_activity_pipeline ON auto_comment_activity_history(pipeline_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_ac_group_src_pipeline ON auto_comment_group_sources(pipeline_id,is_active);
            CREATE INDEX IF NOT EXISTS idx_ac_feed_src_pipeline ON auto_comment_feed_sources(pipeline_id,is_active);
        """)


def migrate_v9_posting_schedule():
    """V9: the schedule of a pipeline and its history.

    The table holds one configuration for the whole workspace: the windows,
    the days of the week, the smallest distance between two posts and the
    rest of a group. Every job that is allocated remembers the moment it is
    meant for, so the schedule survives a restart of the server.
    """
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_posting_schedule (
                pipeline_id INTEGER PRIMARY KEY REFERENCES pipelines(id) ON DELETE CASCADE,
                mode TEXT NOT NULL DEFAULT 'spread',
                timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
                start_time TEXT NOT NULL DEFAULT '08:00',
                end_time TEXT NOT NULL DEFAULT '22:00',
                windows_json TEXT NOT NULL DEFAULT '[]',
                min_interval_minutes INTEGER NOT NULL DEFAULT 90,
                max_posts_per_account_per_day INTEGER NOT NULL DEFAULT 3,
                days_enabled_json TEXT NOT NULL DEFAULT '[1,2,3,4,5,6,0]',
                group_cooldown_minutes INTEGER NOT NULL DEFAULT 120,
                paused INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS posting_schedule_history (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                job_id INTEGER,
                account_id INTEGER,
                group_id INTEGER,
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                scheduled_for TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_schedule_history_pipeline
                ON posting_schedule_history(pipeline_id, id);
            CREATE TABLE IF NOT EXISTS account_schedules (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                scheduled_at TEXT,
                paused INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(auto_post_jobs)")}
        if "scheduled_for" not in columns:
            conn.execute("ALTER TABLE auto_post_jobs ADD COLUMN scheduled_for TEXT")
        if "defer_reason" not in columns:
            conn.execute("ALTER TABLE auto_post_jobs ADD COLUMN defer_reason TEXT NOT NULL DEFAULT ''")
        if "evidence" not in columns:
            conn.execute("ALTER TABLE auto_post_jobs ADD COLUMN evidence TEXT NOT NULL DEFAULT ''")
        if "permalink" not in columns:
            conn.execute("ALTER TABLE auto_post_jobs ADD COLUMN permalink TEXT NOT NULL DEFAULT ''")
        if "attempts" not in columns:
            conn.execute("ALTER TABLE auto_post_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "post_id" not in columns:
            conn.execute("ALTER TABLE auto_post_jobs ADD COLUMN post_id INTEGER")


def migrate_v8_pipeline_auto_post():
    """V8: one configuration for the whole pipeline.

    The old table tied one configuration to one account. The new tables hold
    the shared configuration, the limits of every account, the fingerprints of
    the content that was already used and the counters of the day. The old
    row is copied, so nothing is lost.
    """
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_auto_post_settings (
                pipeline_id INTEGER PRIMARY KEY REFERENCES pipelines(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0,
                scope TEXT NOT NULL DEFAULT 'all',
                account_ids_json TEXT NOT NULL DEFAULT '[]',
                group_ids_json TEXT NOT NULL DEFAULT '[]',
                daily_limit_per_account INTEGER NOT NULL DEFAULT 3,
                min_gap_minutes INTEGER NOT NULL DEFAULT 60,
                pipeline_daily_limit INTEGER NOT NULL DEFAULT 0,
                topic TEXT NOT NULL DEFAULT '',
                ai_caption INTEGER NOT NULL DEFAULT 1,
                anti_duplicate INTEGER NOT NULL DEFAULT 1,
                require_approval INTEGER NOT NULL DEFAULT 1,
                apply_to_new_accounts INTEGER NOT NULL DEFAULT 1,
                timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_posting_limits (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                daily_limit INTEGER,
                min_gap_minutes INTEGER,
                group_daily_limit INTEGER,
                paused INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS content_fingerprints (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                media_id INTEGER,
                job_id INTEGER,
                group_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fingerprint_unique
                ON content_fingerprints(pipeline_id, kind, fingerprint);
            CREATE INDEX IF NOT EXISTS idx_fingerprint_pipeline
                ON content_fingerprints(pipeline_id, kind);
            CREATE TABLE IF NOT EXISTS posting_counters (
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                group_id INTEGER NOT NULL DEFAULT 0,
                day TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 0,
                reserved INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (account_id, group_id, day)
            );
            CREATE INDEX IF NOT EXISTS idx_counter_day ON posting_counters(day);
        """)
        # Chuyển cấu hình cũ sang cấu hình chung của Pipeline.
        for row in conn.execute("SELECT * FROM auto_post_configs").fetchall():
            already = conn.execute(
                "SELECT COUNT(*) FROM pipeline_auto_post_settings WHERE pipeline_id=?",
                (row["pipeline_id"],)).fetchone()[0]
            if already:
                continue
            account_ids = []
            if row["account_id"]:
                account_ids = [int(row["account_id"])]
            conn.execute("""INSERT INTO pipeline_auto_post_settings
                (pipeline_id,enabled,scope,account_ids_json,group_ids_json,daily_limit_per_account,
                 min_gap_minutes,pipeline_daily_limit,topic,ai_caption,anti_duplicate,
                 require_approval,apply_to_new_accounts,timezone,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (row["pipeline_id"], int(row["enabled"] or 0),
                          "selected" if account_ids else "all",
                          json.dumps(account_ids), row["group_ids_json"] or "[]",
                          int(row["daily_limit"] or 3), 60, 0, row["topic"] or "",
                          int(row["ai_caption"] or 1), 1, int(row["require_approval"] or 0),
                          1, "Asia/Ho_Chi_Minh", row["created_at"], row["updated_at"]))


def migrate_v7_analytics():
    """V7: the tables of the analytics.

    post_engagement_snapshots keeps one row per collection run. The dashboard
    only ever adds the newest snapshot of a post, so a metric is never counted
    twice.

    post_attribution remembers who created a piece of content, who approved it
    and which Facebook account published it.

    analytics_collection_jobs is the queue of the collector. One row per post,
    so a scheduler that runs twice does not double the work.
    """
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS post_engagement_snapshots (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                content_type TEXT NOT NULL DEFAULT 'group_post',
                post_id INTEGER NOT NULL,
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                reactions_count INTEGER,
                comments_count INTEGER,
                shares_count INTEGER,
                views_count INTEGER,
                reach_count INTEGER,
                source TEXT NOT NULL DEFAULT '',
                collected_at TEXT NOT NULL,
                verification_status TEXT NOT NULL DEFAULT 'unverified'
            );
            CREATE INDEX IF NOT EXISTS idx_engagement_pipeline
                ON post_engagement_snapshots(pipeline_id, collected_at);
            CREATE INDEX IF NOT EXISTS idx_engagement_post
                ON post_engagement_snapshots(content_type, post_id, collected_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_engagement_snapshot
                ON post_engagement_snapshots(content_type, post_id, source, collected_at);
            CREATE TABLE IF NOT EXISTS post_attribution (
                content_type TEXT NOT NULL DEFAULT 'group_post',
                post_id INTEGER NOT NULL,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                created_by_ctv_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                approved_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                published_by_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (content_type, post_id)
            );
            CREATE INDEX IF NOT EXISTS idx_attribution_pipeline
                ON post_attribution(pipeline_id);
            CREATE INDEX IF NOT EXISTS idx_attribution_ctv
                ON post_attribution(created_by_ctv_id);
            CREATE TABLE IF NOT EXISTS analytics_collection_jobs (
                id INTEGER PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                content_type TEXT NOT NULL DEFAULT 'group_post',
                post_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                last_attempt_at TEXT,
                error_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (content_type, post_id)
            );
            CREATE INDEX IF NOT EXISTS idx_collection_status
                ON analytics_collection_jobs(status, id);
        """)
        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "permalink" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN permalink TEXT NOT NULL DEFAULT ''")
        if "published_at" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN published_at TEXT")
        if "approved_by_user_id" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN approved_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(manual_tasks)")}
        if "permalink" not in task_columns:
            conn.execute("ALTER TABLE manual_tasks ADD COLUMN permalink TEXT NOT NULL DEFAULT ''")
        if "published_at" not in task_columns:
            conn.execute("ALTER TABLE manual_tasks ADD COLUMN published_at TEXT")


def migrate_v6_ownership():
    """V6: a collaborator owns the resources they create.

    Accounts, groups, pictures, posts, tasks and pre-checks carry an optional
    owner. A row without an owner stays in the shared pool of the pipeline and
    remains visible to every member. A row with an owner belongs to that
    collaborator alone; the administrators keep the full view.

    The old groups table carried UNIQUE(pipeline_id, group_key). The table is
    rebuilt with the same columns so the key becomes unique per owner and two
    collaborators may target the same group with their own identity.
    """
    connection = sqlite3.connect(str(DB_PATH), timeout=25, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        for table in ("accounts", "groups", "posts", "manual_tasks", "media_assets", "video_prechecks"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "owner_user_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN owner_user_id "
                                   "INTEGER REFERENCES users(id) ON DELETE SET NULL")
        rebuild_groups_table(connection)
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_groups_owner_key
                              ON groups(pipeline_id, COALESCE(owner_user_id, 0), group_key)""")
        grant_collaborator_ownership(connection)
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.close()


def rebuild_groups_table(connection) -> None:
    """Recreate the groups table without the pipeline wide unique key."""
    ddl = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='groups'").fetchone()
    if ddl is None or "UNIQUE(pipeline_id, group_key)" not in ddl[0]:
        return
    foreign_keys = {
        "pipeline_id": "REFERENCES pipelines(id) ON DELETE CASCADE",
        "account_id": "REFERENCES accounts(id) ON DELETE CASCADE",
    }
    info = connection.execute("PRAGMA table_info(groups)").fetchall()
    names = [row[1] for row in info]
    definitions = []
    for row in info:
        piece = f'"{row[1]}" {row[2]}'
        if row[1] in foreign_keys:
            piece += " " + foreign_keys[row[1]]
        if row[3]:
            piece += " NOT NULL"
        if row[4] is not None:
            piece += f" DEFAULT {row[4]}"
        if row[5]:
            piece += " PRIMARY KEY"
        definitions.append(piece)
    if "owner_user_id" not in names:
        definitions.append('"owner_user_id" INTEGER REFERENCES users(id) ON DELETE SET NULL')
    columns = ", ".join(f'"{name}"' for name in names)

    connection.execute("PRAGMA legacy_alter_table=ON")
    connection.execute("ALTER TABLE groups RENAME TO groups_v5")
    connection.execute("PRAGMA legacy_alter_table=OFF")
    connection.execute("CREATE TABLE groups (" + ", ".join(definitions) + ")")
    connection.execute(f"INSERT INTO groups ({columns}) SELECT {columns} FROM groups_v5")
    connection.execute("DROP TABLE groups_v5")


def grant_collaborator_ownership(connection) -> None:
    """Let the existing collaborators manage their own accounts and groups."""
    wanted = ("facebook.add", "groups.manage", "media.view", "media.upload", "media.edit",
              "media.delete")
    rows = connection.execute("""SELECT id, permissions_json FROM pipeline_memberships
                                  WHERE status='active'""").fetchall()
    for row in rows:
        try:
            granted = set(json.loads(row["permissions_json"] or "[]"))
        except (TypeError, ValueError):
            granted = set()
        missing = [permission for permission in wanted if permission not in granted]
        if not missing:
            continue
        granted.update(missing)
        connection.execute("UPDATE pipeline_memberships SET permissions_json=? WHERE id=?",
                           (json.dumps(sorted(granted)), row["id"]))


def album_scope_label(conn, row) -> str:
    """The folder of an album picture: the owner, or the shared pool."""
    return media_service.album_scope_label(conn, row)


def album_remote_key(conn, row, key: str = "") -> str:
    """The key of a picture inside the bucket of the album."""
    return media_service.album_remote_key(conn, row, key)


def album_bytes(conn, row, key: str = "") -> bytes | None:
    """Read a picture of the album, from the cloud or from the local album.

    A picture that was uploaded before the cloud was configured is still read
    from the local album, so nothing disappears while the bucket fills up.
    """
    return media_service.album_bytes(conn, row, DATA, key)


def audit(category: str, entity_id: int | None, message: str, pipeline_id: int | None = None):
    """Record an event in the legacy audits table and in the new audit log."""
    principal = CURRENT_PRINCIPAL.get()
    actor_id = principal.user_id if principal else None
    actor_name = principal.username if principal else ""
    owner = pipeline_id
    if owner is None and principal is not None:
        owner = principal.pipeline_id
    with db() as conn:
        conn.execute("INSERT INTO audits(category,entity_id,message,created_at) VALUES(?,?,?,?)",
                     (category, entity_id, message[:1000], iso()))
        conn.execute("""INSERT INTO audit_logs(actor_id,actor_username,pipeline_id,action,detail,created_at)
                        VALUES(?,?,?,?,?,?)""",
                     (actor_id, actor_name, owner, category, message[:1000], iso()))


def weak_password(password: str) -> bool:
    """Reject known-weak administrator passwords before public exposure."""
    if not password or len(password) < 12:
        return True
    lowered = password.lower()
    if lowered.startswith("admin") and lowered[5:].isdigit():
        return True
    return lowered in {"password1234", "123456789012", "facebook1234", "qwertyuiop12", "administrator"}


def seed_admin_user():
    """Create the first administrator from the .env credentials, exactly once.

    Later password changes are stored in the database and are never overwritten
    by a restart, so the .env APP_PASSWORD stops being the source of truth as
    soon as the admin sets a new one from the dashboard.
    """
    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
        if existing is not None:
            return
        if not ADMIN_PASS:
            LOG.warning("APP_PASSWORD is empty; no administrator account was created")
            return
        must_change = int(weak_password(ADMIN_PASS))
        conn.execute("""INSERT INTO users(username,password_hash,role,status,must_change_password,created_at,updated_at)
                        VALUES(?,?,'admin','active',?,?,?)""",
                     (ADMIN_USER, hash_secret(ADMIN_PASS), must_change, iso(), iso()))
        LOG.warning("Created administrator %s from .env credentials; password change required: %s",
                    ADMIN_USER, bool(must_change))


def require_auth(request: Request) -> Principal:
    """Resolve the caller from the session cookie on every single request.

    The principal is rebuilt from the database, so suspending a collaborator,
    changing their permissions or revoking a pipeline key takes effect at once.
    A pipeline identifier sent by the client is never trusted here.
    """
    token = request.cookies.get(SESSION_COOKIE, "")
    with db() as conn:
        principal = resolve_principal(conn, token)
    if principal is None:
        raise HTTPException(401, "Authentication required")
    CURRENT_PRINCIPAL.set(principal)
    if request.method.upper() not in SAFE_METHODS:
        supplied = request.headers.get("x-csrf-token", "")
        if not secure_equals(supplied, principal.csrf_token):
            raise HTTPException(403, "CSRF token thiếu hoặc không hợp lệ")
    return principal


def require_principal(principal: Principal = Depends(require_auth)) -> Principal:
    """Guard for every data endpoint: the password must not be pending rotation."""
    if principal.must_change_password:
        raise HTTPException(403, "PHẢI ĐỔI MẬT KHẨU: đổi mật khẩu trước khi dùng hệ thống")
    return principal


def require_admin(principal: Principal = Depends(require_auth)) -> Principal:
    if principal.must_change_password:
        raise HTTPException(403, "PHẢI ĐỔI MẬT KHẨU: đổi mật khẩu trước khi dùng hệ thống")
    if not principal.is_admin:
        raise HTTPException(403, "Chỉ Chủ hệ thống được thực hiện thao tác này")
    return principal


def scope_pipeline(principal: Principal, requested: int | None) -> int:
    """Return the pipeline the caller may act on.

    A collaborator is pinned to the pipeline of their membership; asking for a
    different one is reported as "not found" so no other tenant leaks.
    """
    if principal.is_admin:
        if requested is None:
            raise HTTPException(422, "Thiếu pipeline_id")
        return int(requested)
    if requested is not None and int(requested) != principal.pipeline_id:
        raise HTTPException(404, "Not found")
    return int(principal.pipeline_id)


def scoped_entry(conn, table: str, pk: int, principal: Principal):
    """Fetch a row, hiding it completely when it belongs to another pipeline.

    A row that carries an owner belongs to that collaborator alone. Another
    collaborator of the same pipeline gets "not found" as well, so nothing of
    a private resource leaks.
    """
    row = entry(conn, table, pk)
    if not principal.is_admin and row["pipeline_id"] != principal.pipeline_id:
        raise HTTPException(404, "Not found")
    if not principal.is_admin and "owner_user_id" in row.keys():
        owner = row["owner_user_id"]
        if owner is not None and owner != principal.user_id:
            raise HTTPException(404, "Not found")
    return row


def owner_condition(principal: Principal, alias: str = "") -> tuple[str, list]:
    """SQL fragment listing the rows the principal is allowed to see.

    Administrators keep the full view of their pipelines. A collaborator sees
    the shared pool of the workspace plus the resources they own themselves.
    """
    if principal.is_admin:
        return "1=1", []
    prefix = f"{alias}." if alias else ""
    return f"({prefix}owner_user_id IS NULL OR {prefix}owner_user_id=?)", [principal.user_id]


def resource_owner(principal: Principal) -> int | None:
    """Owner of a row that is being created.

    The administrator fills the shared pool of the workspace, a collaborator
    keeps what they create for themselves.
    """
    return None if principal.is_admin else principal.user_id


def require_permission(principal: Principal, permission: str) -> None:
    if not principal.can(permission):
        raise HTTPException(403, f"Thiếu quyền: {permission}")


def require_any_permission(principal: Principal, permissions) -> None:
    if not principal.can_any(permissions):
        raise HTTPException(403, "Thiếu quyền: " + ", ".join(permissions))


MEDIA_PATTERN = re.compile(r"^(?:uploads/)?(?:(\d+)/)?([0-9a-f]{32}\.(?:jpg|png|webp|mp4|mov))$")


def media_owner_pipeline(conn, name: str) -> int | None:
    """Find the pipeline that owns an uploaded file."""
    for table in ("posts", "manual_tasks"):
        row = conn.execute(f"SELECT pipeline_id FROM {table} WHERE media_json LIKE ? LIMIT 1",
                           (f"%{name}%",)).fetchone()
        if row is not None:
            return row["pipeline_id"]
    return None


def media_file(conn, filename: str, principal: Principal) -> tuple[Path, str]:
    """Resolve an upload reference to a real file owned by the caller's workspace.

    The path is rebuilt from a strict pattern, so no traversal is possible, and
    a file belonging to another pipeline is reported as "not found".
    """
    match = MEDIA_PATTERN.match(filename or "")
    if not match:
        raise HTTPException(404, "Not found")
    pipeline_part, name = match.group(1), match.group(2)
    if pipeline_part is not None:
        owner = int(pipeline_part)
        path = UPLOADS / pipeline_part / name
    else:
        owner = media_owner_pipeline(conn, name)
        path = UPLOADS / name
    if owner is None:
        raise HTTPException(404, "Not found")
    if not principal.is_admin and owner != principal.pipeline_id:
        raise HTTPException(404, "Not found")
    if not path.is_file():
        # Current layout keeps uploads in uploads/<pipeline_id>/<name>.
        alternative = UPLOADS / str(owner) / name
        if alternative.is_file():
            path = alternative
    if not path.is_file():
        raise HTTPException(404, "Not found")
    return path, name


def entry(conn, table: str, pk: int):
    if table not in ("pipelines", "accounts", "groups", "posts", "jobs", "manual_tasks"):
        raise HTTPException(400, "Invalid table")
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (pk,)).fetchone()
    if row is None:
        raise HTTPException(404, f"{table} not found")
    return row


def must_belong(conn, pipeline_id: int, table: str, item_id: int):
    row = entry(conn, table, item_id)
    if row["pipeline_id"] != pipeline_id:
        raise HTTPException(422, f"{table} belongs to another pipeline")
    return row


def group_link(link: str):
    parsed = urlparse(link.strip())
    if parsed.scheme != "https" or parsed.hostname not in ("facebook.com", "www.facebook.com", "m.facebook.com"):
        raise HTTPException(422, "Only HTTPS facebook.com group URLs are accepted")
    matched = re.fullmatch(r"/groups/([A-Za-z0-9._-]+)/?", parsed.path)
    if not matched:
        raise HTTPException(422, "Expected group URL: https://www.facebook.com/groups/GROUP_ID")
    group_id = matched.group(1)
    return f"https://www.facebook.com/groups/{group_id}/", group_id.lower()


class Login(BaseModel):
    username: str
    password: str


class PipelineCreate(BaseModel):
    name: str = Field(min_length=2, max_length=70, pattern=r"^@[A-Za-z0-9_.-]+$")
    description: str = Field(default="", max_length=500)


class AccountCreate(BaseModel):
    # A collaborator's workspace comes from the session, so the field is only
    # needed by an administrator who manages more than one workspace.
    pipeline_id: int | None = None
    label: str = Field(min_length=2, max_length=100)
    profile_url: str = Field(default="", max_length=300)


class GroupCreate(BaseModel):
    pipeline_id: int
    account_id: int
    label: str = Field(min_length=2, max_length=200)
    url: str
    approved_target: bool = False
    posting_mode: str = Field(default="anonymous", pattern=r"^(anonymous|nickname)$")
    nickname: str = Field(default="", max_length=120)
    account_ids: list[int] = Field(default_factory=list, max_length=10)


class GroupBulkCreate(BaseModel):
    urls: str = Field(min_length=1, max_length=20000)
    account_id: int | None = None
    label_prefix: str = Field(default="", max_length=60)
    posting_mode: str = Field(default="anonymous", pattern=r"^(anonymous|nickname)$")
    nickname: str = Field(default="", max_length=120)
    approved_target: bool = False


class GroupUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    account_id: int | None = None
    posting_mode: str | None = Field(default=None, pattern=r"^(anonymous|nickname)$")
    nickname: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, pattern=r"^(active|paused)$")
    enabled: bool | None = None
    account_ids: list[int] | None = Field(default=None, max_length=10)
    manual_review: bool | None = None
    screening_status: str | None = Field(default=None, pattern=r"^(priority|review|unknown|restricted|failed)$")


class AutoPostConfig(BaseModel):
    account_id: int | None = None
    group_ids: list[int] = Field(default_factory=list, max_length=50)
    topic: str = Field(default="", max_length=2000)
    ai_caption: bool = True
    daily_limit: int = Field(default=3, ge=1, le=50)
    require_approval: bool = True
    enabled: bool = False
    tone: str = Field(default="", max_length=200)


class MediaUpdate(BaseModel):
    tags: list[str] | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=600)
    usable: bool | None = None
    locked: bool | None = None
    saved: bool | None = None


class MediaAnalyzeRequest(BaseModel):
    media_ids: list[int] = Field(default_factory=list, max_length=200)


class PrecheckRequest(BaseModel):
    account_id: int
    media_path: str = Field(min_length=1, max_length=500)
    caption: str = Field(default="", max_length=20000)


class PrecheckApproval(BaseModel):
    note: str = Field(default="", max_length=500)


class ScreenRequest(BaseModel):
    group_ids: list[int] = Field(default_factory=list, max_length=200)
    force: bool = False


class GroupPreviewCheck(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    account_id: int
    posting_mode: str = Field(default="anonymous", pattern=r"^(anonymous|nickname)$")
    nickname: str = Field(default="", max_length=120)


class PostCreate(BaseModel):
    pipeline_id: int
    body: str = Field(min_length=1, max_length=20000)
    # So group cua mot chien dich duoc gioi han theo cau hinh, khong phai hang so
    # co dinh trong ma nguon.
    group_ids: list[int] = Field(min_length=1)
    media: list[str] = Field(min_length=1, max_length=4)
    anonymous_only: bool = True
    scheduled_at: str
    ai_record_id: int | None = None


class ManualTaskCreate(BaseModel):
    pipeline_id: int
    account_id: int
    group_id: int | None = None
    task_type: str = Field(pattern=r"^(reel|video|comment)$")
    body: str = Field(default="", max_length=20000)
    media: list[str] = Field(default_factory=list, max_length=1)
    target_url: str = Field(default="", max_length=2000)
    auto_publish: bool = False
    scheduled_at: str
    ai_record_id: int | None = None
    precheck_id: int | None = None


class ManualTaskUpdate(BaseModel):
    status: str = Field(pattern=r"^(awaiting_manual|completed|cancelled|posted|pending_approval|failed)$")


class EnabledUpdate(BaseModel):
    enabled: bool | None = None
    account_id: int | None = None
    posting_mode: str | None = None
    nickname: str | None = None
    description: str | None = None


class JobReview(BaseModel):
    state: str = Field(pattern=r"^(posted|pending_approval|failed|cancelled|queued)$")
    details: str = Field(default="", max_length=1000)


class PostUpdate(BaseModel):
    body: str | None = Field(default=None, max_length=20000)
    media_json: list[str] | None = Field(default=None, min_length=1, max_length=4)
    group_ids: list[int] | None = Field(default=None, min_length=0)


def pid_active(pid: int | None, account_id: int) -> bool:
    if not pid or pid < 2:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        return "fb_browser.py" in cmd and cmd.strip().split()[-2:] == ["login", str(account_id)]
    except OSError:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not ADMIN_PASS or len(ADMIN_PASS) < 12 or len(APP_SECRET) < 32:
        raise RuntimeError("Set APP_PASSWORD (>=12 chars) and APP_SECRET (>=32 chars) in .env")
    init_db()
    seed_admin_user()
    # Preserve session history; expiry/revocation is enforced by resolve_principal.
    # Deletion belongs to separately approved retention maintenance.
    STOP_EVENT.clear()
    worker = threading.Thread(target=scheduler, daemon=True, name="facebook-scheduler")
    worker.start()
    screener = threading.Thread(target=screening_worker, daemon=True, name="group-screening")
    screener.start()
    prechecker = threading.Thread(target=precheck_worker, daemon=True, name="video-precheck")
    prechecker.start()
    mediaworker = threading.Thread(target=media_worker, daemon=True, name="media-analysis")
    mediaworker.start()
    autoworker = threading.Thread(target=auto_post_worker, daemon=True, name="auto-posting")
    autoworker.start()
    analyticsthread = threading.Thread(target=analytics_thread, daemon=True, name="analytics")
    analyticsthread.start()
    joinworker = threading.Thread(target=group_join_worker_thread, daemon=True, name="group-join")
    joinworker.start()
    yield
    STOP_EVENT.set()
    worker.join(timeout=2)
    screener.join(timeout=2)
    prechecker.join(timeout=2)
    analyticsthread.join(timeout=2)
    mediaworker.join(timeout=2)
    autoworker.join(timeout=2)


app = FastAPI(title="Facebook Pipeline Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

# Cấu hình tự động đăng riêng của từng CTV nằm ở module riêng để ``app`` không
# phải chứa thêm phần xử lý. Các hàm bảo vệ được truyền vào qua ``Deps``.
app.include_router(ctv_auto_post_api.build_router(ctv_auto_post_api.Deps(
    db=db,
    require_principal=require_principal,
    require_admin=require_admin,
    require_permission=require_permission,
    scope_pipeline=scope_pipeline,
    scoped_entry=scoped_entry,
    entry=entry,
    audit=audit,
    load_ai_config=load_ai_config,
)))

app.include_router(auto_cycles_api.build_router(auto_cycles_api.Deps(
    db=db, require_principal=require_principal, require_admin=require_admin,
    require_permission=require_permission, scope_pipeline=scope_pipeline, audit=audit, now=lambda:now(),
    enqueue_screening=lambda *args:enqueue_screening(*args),
)))

# Album ảnh nằm ở module riêng để ``app`` không phải chứa thêm phần xử lý.
app.include_router(media_api.build_router(media_api.Deps(
    db=db,
    iso=iso,
    data_root=lambda: DATA,
    require_principal=require_principal,
    require_permission=require_permission,
    entry=entry,
    audit=audit,
    load_ai_config=load_ai_config,
)))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """A short answer instead of the technical report of the validator.

    The details stay in the log of the server, the customer only reads one
    sentence that says which field was wrong.
    """
    fields: list[str] = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path", "cookie", "header")]
        if location:
            fields.append(location[-1])
    LOG.warning("Dữ liệu gửi lên không hợp lệ tại %s: %s", request.url.path, exc.errors())
    unique = sorted(set(fields))
    detail = "Dữ liệu gửi lên không hợp lệ"
    if unique:
        detail += ": " + ", ".join(unique[:5])
    return JSONResponse(status_code=422, content={"detail": detail, "fields": unique})


@app.exception_handler(ProviderError)
async def provider_error_handler(request: Request, exc: ProviderError):
    LOG.warning("Lỗi dịch vụ AI tại %s: %s", request.url.path, str(exc))
    return JSONResponse(status_code=503, content={"detail": "Dịch vụ xử lý đang gặp sự cố, vui lòng thử lại sau."})


@app.exception_handler(ProviderError)
async def provider_error_handler(request: Request, exc: ProviderError):
    LOG.warning("Lỗi dịch vụ AI tại %s: %s", request.url.path, str(exc))
    return JSONResponse(status_code=503, content={"detail": "Dịch vụ xử lý đang gặp sự cố, vui lòng thử lại sau."})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    LOG.exception("Lỗi không xử lý được tại %s: %s", request.url.path, str(exc))
    return JSONResponse(status_code=500, content={"detail": "Máy chủ gặp sự cố, vui lòng thử lại."})


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "facebook-pipeline-manager"}


class CTVLogin(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    pipeline_key: str = Field(default="", max_length=128)


class PasswordChange(BaseModel):
    current_password: str | None = Field(default=None, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"


def set_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE,
                        samesite="strict", max_age=12 * 3600, path="/")


def principal_payload(principal: Principal, csrf: str) -> dict:
    return {
        "ok": True,
        "role": principal.role,
        "username": principal.username,
        "user_id": principal.user_id,
        "pipeline_id": principal.pipeline_id,
        "permissions": sorted(principal.permissions),
        "csrf_token": csrf,
        "must_change_password": principal.must_change_password,
    }


class AdminPipelineCreate(BaseModel):
    name: str = Field(min_length=2, max_length=70, pattern=r"^@[A-Za-z0-9_.-]+$")
    description: str = Field(default="", max_length=500)


class AdminPipelineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=70, pattern=r"^@[A-Za-z0-9_.-]+$")
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class CollaboratorCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    pipeline_id: int
    permissions: list[str] = Field(default_factory=list)
    status: str = Field(default=STATUS_ACTIVE, pattern=r"^(active|suspended)$")


class CollaboratorUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=12, max_length=256)
    pipeline_id: int | None = None
    permissions: list[str] | None = None
    status: str | None = Field(default=None, pattern=r"^(active|suspended)$")


@app.get("/api/admin/pipelines")
def admin_pipelines(_: Principal = Depends(require_admin)):
    """Every pipeline with its counters, for the administrator dashboard."""
    with db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.description, p.enabled, p.created_at,
              (SELECT COUNT(*) FROM accounts a WHERE a.pipeline_id=p.id) accounts_count,
              (SELECT COUNT(*) FROM groups g WHERE g.pipeline_id=p.id) groups_count,
              (SELECT COUNT(*) FROM pipeline_memberships m WHERE m.pipeline_id=p.id AND m.status='active') ctv_count,
              (SELECT COUNT(*) FROM jobs j JOIN posts po ON j.post_id=po.id
                 WHERE po.pipeline_id=p.id AND j.state='posted') posted_count,
              (SELECT COUNT(*) FROM jobs j JOIN posts po ON j.post_id=po.id
                 WHERE po.pipeline_id=p.id AND j.state IN ('queued','running','pending_approval')) pending_count,
              (SELECT COUNT(*) FROM jobs j JOIN posts po ON j.post_id=po.id
                 WHERE po.pipeline_id=p.id AND j.state IN ('failed','needs_review')) failed_count,
              (SELECT COUNT(*) FROM manual_tasks t WHERE t.pipeline_id=p.id) reels_count,
              (SELECT COUNT(*) FROM pipeline_access_keys k
                 WHERE k.pipeline_id=p.id AND k.revoked_at IS NULL) active_keys
            FROM pipelines p ORDER BY p.id""").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/pipelines", status_code=201)
def admin_create_pipeline(data: AdminPipelineCreate, principal: Principal = Depends(require_admin)):
    """Create a pipeline and issue its first access key."""
    with db() as conn:
        if conn.execute("SELECT id FROM pipelines WHERE name=?", (data.name,)).fetchone():
            raise HTTPException(409, "Pipeline đã tồn tại")
        cursor = conn.execute("INSERT INTO pipelines(name,description,enabled,created_at) VALUES(?,?,1,?)",
                              (data.name, data.description, iso()))
        pipeline_id = cursor.lastrowid
        key = new_pipeline_key()
        expires = utcnow() + timedelta(days=KEY_TTL_DAYS)
        conn.execute("""INSERT INTO pipeline_access_keys(pipeline_id,key_prefix,key_hash,created_by,expires_at,created_at)
                        VALUES(?,?,?,?,?,?)""",
                     (pipeline_id, key_prefix(key), hash_secret(key), principal.user_id, iso(expires), iso()))
    audit("admin.pipeline_created", pipeline_id, f"Tạo pipeline {data.name}", pipeline_id)
    return {"ok": True, "id": pipeline_id, "name": data.name, "key": key, "key_expires_at": iso(expires),
            "warning": "Pipeline Access Key chỉ hiển thị đúng một lần"}


@app.patch("/api/admin/pipelines/{pipeline_id}")
def admin_update_pipeline(pipeline_id: int, data: AdminPipelineUpdate, principal: Principal = Depends(require_admin)):
    """Rename, describe or lock/unlock a workspace."""
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        if data.name and data.name != row_name(conn, pipeline_id):
            if conn.execute("SELECT id FROM pipelines WHERE name=? AND id<>?", (data.name, pipeline_id)).fetchone():
                raise HTTPException(409, "Pipeline đã tồn tại")
            conn.execute("UPDATE pipelines SET name=? WHERE id=?", (data.name, pipeline_id))
        if data.description is not None:
            conn.execute("UPDATE pipelines SET description=? WHERE id=?", (data.description, pipeline_id))
        if data.enabled is not None:
            conn.execute("UPDATE pipelines SET enabled=? WHERE id=?", (int(data.enabled), pipeline_id))
    audit("admin.pipeline_updated", pipeline_id, "Cập nhật pipeline")
    return {"ok": True}


def row_name(conn, pipeline_id: int) -> str:
    row = conn.execute("SELECT name FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
    return row["name"] if row else ""


@app.get("/api/admin/pipelines/{pipeline_id}/key")
def admin_key_status(pipeline_id: int, _: Principal = Depends(require_admin)):
    """Key metadata only. The secret itself is never retrievable again."""
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        rows = conn.execute("""SELECT id, key_prefix, expires_at, revoked_at, created_at
                               FROM pipeline_access_keys WHERE pipeline_id=? ORDER BY id DESC""",
                            (pipeline_id,)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/pipelines/{pipeline_id}/key")
def admin_issue_key(pipeline_id: int, principal: Principal = Depends(require_admin)):
    """Issue a fresh access key. Any previous key of this pipeline is revoked."""
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        for row in conn.execute("SELECT id FROM pipeline_access_keys WHERE pipeline_id=? AND revoked_at IS NULL",
                                (pipeline_id,)).fetchall():
            revoke_key_sessions(conn, row["id"])
        conn.execute("UPDATE pipeline_access_keys SET revoked_at=? WHERE pipeline_id=? AND revoked_at IS NULL",
                     (iso(), pipeline_id))
        key = new_pipeline_key()
        expires = utcnow() + timedelta(days=KEY_TTL_DAYS)
        conn.execute("""INSERT INTO pipeline_access_keys(pipeline_id,key_prefix,key_hash,created_by,expires_at,created_at)
                        VALUES(?,?,?,?,?,?)""",
                     (pipeline_id, key_prefix(key), hash_secret(key), principal.user_id, iso(expires), iso()))
    audit("admin.key_issued", pipeline_id, f"Sinh Pipeline Access Key mới cho pipeline #{pipeline_id}", pipeline_id)
    return {"ok": True, "pipeline_id": pipeline_id, "key": key, "expires_at": iso(expires),
            "warning": "Hiển thị đúng một lần; hãy lưu lại an toàn"}


@app.delete("/api/admin/pipelines/{pipeline_id}/key")
def admin_revoke_keys(pipeline_id: int, principal: Principal = Depends(require_admin)):
    """Revoke every access key of a pipeline and end the sessions bound to them."""
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        for row in conn.execute("SELECT id FROM pipeline_access_keys WHERE pipeline_id=? AND revoked_at IS NULL",
                                (pipeline_id,)).fetchall():
            revoke_key_sessions(conn, row["id"])
        conn.execute("UPDATE pipeline_access_keys SET revoked_at=? WHERE pipeline_id=? AND revoked_at IS NULL",
                     (iso(), pipeline_id))
    audit("admin.key_revoked", pipeline_id, f"Thu hồi toàn bộ Pipeline Access Key của pipeline #{pipeline_id}",
          pipeline_id)
    return {"ok": True}


@app.get("/api/admin/users")
def admin_users(_: Principal = Depends(require_admin)):
    """All collaborators with their pipeline and granted permissions."""
    with db() as conn:
        rows = conn.execute("""
            SELECT u.id, u.username, u.role, u.status, u.must_change_password, u.created_at,
                   m.pipeline_id, m.permissions_json, m.status membership_status, p.name pipeline_name
            FROM users u
            LEFT JOIN pipeline_memberships m ON m.user_id=u.id
            LEFT JOIN pipelines p ON p.id=m.pipeline_id
            ORDER BY u.id""").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["permissions"] = sorted(normalise_permissions(item.pop("permissions_json", "[]")))
        result.append(item)
    return result


@app.post("/api/admin/users", status_code=201)
def admin_create_collaborator(data: CollaboratorCreate, principal: Principal = Depends(require_admin)):
    """Create a collaborator bound to exactly one pipeline."""
    if data.permissions:
        unknown = sorted(set(data.permissions) - set(PERMISSIONS))
        if unknown:
            raise HTTPException(422, "Quyền không hợp lệ: " + ", ".join(unknown))
        permissions = sorted(normalise_permissions(data.permissions))
    else:
        permissions = sorted(DEFAULT_CTV_PERMISSIONS)
    # Mỗi CTV luôn được tự thiết lập lịch đăng của riêng mình. Quyền này
    # không đụng tới cấu hình chung của Workspace, nên vẫn an toàn khi cấp sẵn.
    if "auto_post.self_configure" not in permissions:
        permissions = sorted(set(permissions) | {"auto_post.self_configure"})
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        entry(conn, "pipelines", data.pipeline_id)
        if conn.execute("SELECT id FROM users WHERE username=?", (data.username.strip(),)).fetchone():
            raise HTTPException(409, "Username đã tồn tại")
        if data.status == STATUS_ACTIVE:
            try:
                require_available(conn, data.pipeline_id)
            except MembershipConflict as error:
                raise HTTPException(409, {"code": error.code, "message": error.message}) from error
        try:
            cursor = conn.execute("""INSERT INTO users(username,password_hash,role,status,must_change_password,created_at,updated_at)
                                     VALUES(?,?,'ctv',?,0,?,?)""",
                                  (data.username.strip(), hash_secret(data.password), data.status, iso(), iso()))
        except sqlite3.IntegrityError as error:
            # Another request created the same username in the meantime.
            raise HTTPException(409, "Username đã tồn tại") from error
        user_id = cursor.lastrowid
        conn.execute("""INSERT INTO pipeline_memberships(user_id,pipeline_id,permissions_json,status,created_at,updated_at)
                        VALUES(?,?,?,?,?,?)""",
                     (user_id, data.pipeline_id, json.dumps(permissions), data.status, iso(), iso()))
    audit("admin.user_created", user_id, f"Tạo CTV {data.username} trong pipeline #{data.pipeline_id}", data.pipeline_id)
    return {"ok": True, "id": user_id, "username": data.username.strip(), "permissions": permissions}


@app.patch("/api/admin/users/{user_id}")
def admin_update_collaborator(user_id: int, data: CollaboratorUpdate, principal: Principal = Depends(require_admin)):
    """Change a collaborator's pipeline, permissions, status or password."""
    if data.permissions is not None:
        unknown = sorted(set(data.permissions) - set(PERMISSIONS))
        if unknown:
            raise HTTPException(422, "Quyền không hợp lệ: " + ", ".join(unknown))
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise HTTPException(404, "Không tìm thấy tài khoản")
        if user["role"] != ROLE_CTV:
            raise HTTPException(400, "Chỉ cập nhật được tài khoản CTV")
        memberships = conn.execute(
            "SELECT * FROM pipeline_memberships WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        active = [row for row in memberships if row["status"] == STATUS_ACTIVE]
        if len(active) > 1 or (not active and len(memberships) > 1):
            raise HTTPException(409, {"code": MembershipConflict.code, "message": MembershipConflict.message})
        membership = active[0] if active else (memberships[0] if memberships else None)
        target_pipeline = data.pipeline_id if data.pipeline_id is not None else (
            membership["pipeline_id"] if membership else None)
        if target_pipeline is None:
            raise HTTPException(422, "Chưa gán pipeline cho tài khoản này")
        entry(conn, "pipelines", target_pipeline)
        if data.permissions is not None:
            permissions = sorted(normalise_permissions(data.permissions))
        else:
            permissions = sorted(normalise_permissions(membership["permissions_json"])) if membership else []
        membership_status = data.status or (membership["status"] if membership else STATUS_ACTIVE)
        if membership_status == STATUS_ACTIVE:
            try:
                require_available(conn, target_pipeline, user_id, membership["id"] if membership else None)
            except MembershipConflict as error:
                raise HTTPException(409, {"code": error.code, "message": error.message}) from error
        if membership is None:
            conn.execute("""INSERT INTO pipeline_memberships(user_id,pipeline_id,permissions_json,status,created_at,updated_at)
                            VALUES(?,?,?,?,?,?)""",
                         (user_id, target_pipeline, json.dumps(permissions), membership_status, iso(), iso()))
        else:
            conn.execute("""UPDATE pipeline_memberships SET pipeline_id=?, permissions_json=?, status=?, updated_at=?
                            WHERE id=?""",
                         (target_pipeline, json.dumps(permissions), membership_status, iso(), membership["id"]))
        if data.password:
            conn.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                         (hash_secret(data.password), iso(), user_id))
        if data.status:
            conn.execute("UPDATE users SET status=?, updated_at=? WHERE id=?", (data.status, iso(), user_id))
        # A move, a suspension or a new password ends the live sessions;
        # a pure permission change already applies on the next request.
        if (data.pipeline_id is not None and target_pipeline != (membership["pipeline_id"] if membership else None)) \
                or (data.status and data.status != user["status"]) or data.password:
            revoke_user_sessions(conn, user_id)
    audit("admin.user_updated", user_id, f"Cập nhật CTV {user['username']}", target_pipeline)
    return {"ok": True, "permissions": permissions}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_collaborator(user_id: int, principal: Principal = Depends(require_admin)):
    """Remove a collaborator and all of their sessions."""
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise HTTPException(404, "Không tìm thấy tài khoản")
        if user["role"] != ROLE_CTV:
            raise HTTPException(400, "Không thể xóa tài khoản quản trị")
        revoke_user_sessions(conn, user_id)
        conn.execute("DELETE FROM pipeline_memberships WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    audit("admin.user_deleted", user_id, f"Xóa CTV {user['username']}")
    return {"ok": True}


@app.get("/api/admin/audit")
def admin_audit(limit: int = 200, _: Principal = Depends(require_admin)):
    limit = min(500, max(1, limit))
    with db() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))]


@app.get("/api/admin/permissions")
def admin_permission_catalogue(_: Principal = Depends(require_admin)):
    return {"permissions": list(PERMISSIONS), "defaults": list(DEFAULT_CTV_PERMISSIONS)}


def caption_settings_row(conn, pipeline_id: int) -> dict:
    """Read the workspace caption settings, creating the defaults on first use."""
    row = conn.execute("SELECT * FROM ai_caption_settings WHERE pipeline_id=?", (pipeline_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO ai_caption_settings(pipeline_id,updated_at) VALUES(?,?)",
                     (pipeline_id, iso()))
        row = conn.execute("SELECT * FROM ai_caption_settings WHERE pipeline_id=?", (pipeline_id,)).fetchone()
    return dict(row)


def caption_settings_payload(row) -> dict:
    return {
        "pipeline_id": row["pipeline_id"],
        "enabled": bool(row["enabled"]),
        "tone": row["tone"],
        "language": row["language"],
        "model": row["model"],
        "target": row["target"],
        "add_hashtags": bool(row["add_hashtags"]),
        "preserve_meaning": bool(row["preserve_meaning"]),
        "optimize_for_reel": bool(row["optimize_for_reel"]),
        "optimize_for_group": bool(row["optimize_for_group"]),
        "auto_use_when_safe": bool(row["auto_use_when_safe"]),
        "require_manual_review": bool(row["require_manual_review"]),
        "max_hashtags": row["max_hashtags"],
        "updated_at": row["updated_at"],
    }


def settings_to_schema(row, pipeline_id: int) -> CaptionSettings:
    return CaptionSettings(
        enabled=bool(row["enabled"]), tone=row["tone"], language=row["language"],
        model=row["model"], target=row["target"], add_hashtags=bool(row["add_hashtags"]),
        preserve_meaning=bool(row["preserve_meaning"]),
        optimize_for_reel=bool(row["optimize_for_reel"]),
        optimize_for_group=bool(row["optimize_for_group"]),
        auto_use_when_safe=bool(row["auto_use_when_safe"]),
        require_manual_review=bool(row["require_manual_review"]),
        max_hashtags=row["max_hashtags"],
        pipeline_id=pipeline_id)


def record_payload(row) -> dict:
    """The API shape of one AI caption record. Internal model/provider fields
    are stripped so end users never see raw AI metadata."""
    return {
        "id": row["id"],
        "pipeline_id": row["pipeline_id"],
        "original_caption": row["original_caption"],
        "optimized_caption": row["optimized_caption"],
        "ai_status": row["ai_status"],
        "meaning_preserved": bool(row["meaning_preserved"]),
        "approved": bool(row["approved"]),
        "approved_at": row["approved_at"],
        "target": row["target"],
        "issues": json.loads(row["issues_json"] or "[]"),
        "changes": json.loads(row["changes_json"] or "[]"),
        "reason": row["reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "usable": bool(row["approved"]) and row["ai_status"] != STATUS_BLOCKED,
    }


def usable_caption(conn, pipeline_id: int, record_id: int | None) -> tuple[str, int | None]:
    """Return the approved caption for a record, or the empty string.

    A record of another workspace, a record that was never approved and a
    blocked record all yield an empty string, so an unchecked caption can
    never reach the publishing queue.
    """
    if not record_id:
        return "", None
    row = conn.execute("SELECT * FROM ai_caption_records WHERE id=?", (record_id,)).fetchone()
    if row is None or row["pipeline_id"] != pipeline_id:
        raise HTTPException(404, "Not found")
    if not row["approved"] or row["ai_status"] == STATUS_BLOCKED:
        return "", row["id"]
    return row["optimized_caption"], row["id"]


@app.get("/api/pipelines/{pipeline_id}/caption/settings")
def get_caption_settings(pipeline_id: int, principal: Principal = Depends(require_principal)):
    require_permission(principal, "ai_caption.use")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        row = caption_settings_row(conn, pipeline_id)
    return caption_settings_payload(row)


@app.patch("/api/pipelines/{pipeline_id}/caption/settings")
def patch_caption_settings(pipeline_id: int, data: CaptionSettingsUpdate,
                           principal: Principal = Depends(require_principal)):
    require_permission(principal, "ai_caption.use")
    scope_pipeline(principal, pipeline_id)
    if not principal.is_admin and (data.enabled is not None or data.auto_use_when_safe is not None):
        raise HTTPException(403, "Chỉ Chủ hệ thống được bật/tắt chế độ tự động cho Workspace")
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        row = caption_settings_row(conn, pipeline_id)
        updates = data.model_dump(exclude_none=True)
        if not updates:
            return caption_settings_payload(row)
        assignments = ", ".join(f"{key}=?" for key in updates)
        conn.execute(f"UPDATE ai_caption_settings SET {assignments}, updated_at=? WHERE pipeline_id=?",
                     (*updates.values(), iso(), pipeline_id))
        row = conn.execute("SELECT * FROM ai_caption_settings WHERE pipeline_id=?", (pipeline_id,)).fetchone()
    audit("caption.settings", pipeline_id, "Cập nhật cấu hình AI Caption", pipeline_id)
    return caption_settings_payload(row)


@app.post("/api/pipelines/{pipeline_id}/caption/optimize")
def optimize_caption_endpoint(pipeline_id: int, data: CaptionOptimizeRequest,
                              principal: Principal = Depends(require_principal)):
    """Analyse, rewrite and validate a caption inside one workspace."""
    require_permission(principal, "ai_caption.use")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        row = caption_settings_row(conn, pipeline_id)
    settings = settings_to_schema(row, pipeline_id)
    if data.target:
        settings.target = data.target
    if data.tone:
        settings.tone = data.tone
    result = optimize_caption(data.caption, settings, row["model"])
    auto_approve = settings.auto_use_when_safe and result.status == STATUS_APPROVED
    with db() as conn:
        cursor = conn.execute("""INSERT INTO ai_caption_records
            (pipeline_id,original_caption,optimized_caption,ai_status,risk_level,meaning_preserved,
             approved,approved_at,ai_model,ai_provider,ai_engine,target,topic,intent,
             issues_json,changes_json,reason,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pipeline_id, result.original_caption, result.optimized_caption, result.status,
             result.risk_level, int(result.meaning_preserved), int(auto_approve),
             iso() if auto_approve else None, result.ai_model, result.provider, result.engine,
             settings.target, result.topic, result.intent,
             json.dumps([issue.model_dump() for issue in result.issues]),
             json.dumps([change.model_dump() for change in result.changes]),
             result.reason, iso(), iso()))
        record_id = cursor.lastrowid
    audit("caption.optimize", record_id, f"AI caption {result.status} (rủi ro {result.risk_level})", pipeline_id)
    payload = {
        "ok": True, "id": record_id, "status": result.status,
        "meaning_preserved": result.meaning_preserved, "approved": auto_approve,
        "original_caption": result.original_caption, "optimized_caption": result.optimized_caption,
        "issues": [issue.model_dump() for issue in result.issues],
        "changes": [change.model_dump() for change in result.changes],
        "reason": result.reason,
        "auto_approved": auto_approve,
        "note": "Kết quả AI không bảo đảm Facebook sẽ duyệt bài; trạng thái tài khoản và group cũng ảnh hưởng kiểm duyệt.",
    }
    return payload


@app.post("/api/pipelines/{pipeline_id}/caption/approve")
def approve_caption(pipeline_id: int, data: CaptionApproveRequest,
                    principal: Principal = Depends(require_principal)):
    """Accept an AI caption for use, after re-checking the edited text."""
    require_permission(principal, "ai_caption.use")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        record = conn.execute("SELECT * FROM ai_caption_records WHERE id=?", (data.record_id,)).fetchone()
        if record is None or record["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        if record["ai_status"] == STATUS_BLOCKED:
            raise HTTPException(409, "Nội dung bị chặn; hãy sửa lại bản gốc rồi tối ưu lại")
        optimized = data.optimized_caption.strip() or record["optimized_caption"]
        recheck = analyze_caption(optimized)
        blocking = [issue for issue in recheck.issues if issue.severity == SEVERITY_HIGH]
        if blocking:
            raise HTTPException(409, "Bản caption đã sửa vẫn vi phạm chính sách: "
                                     + "; ".join(issue.message for issue in blocking[:3]))
        conn.execute("""UPDATE ai_caption_records SET optimized_caption=?, approved=1, approved_at=?,
                        updated_at=? WHERE id=?""",
                     (optimized, iso(), iso(), data.record_id))
        updated = conn.execute("SELECT * FROM ai_caption_records WHERE id=?", (data.record_id,)).fetchone()
    audit("caption.approve", data.record_id, "Người dùng duyệt caption AI", pipeline_id)
    return {"ok": True, "record": record_payload(updated)}


@app.get("/api/pipelines/{pipeline_id}/caption/history")
def caption_history(pipeline_id: int, limit: int = 50, principal: Principal = Depends(require_principal)):
    require_permission(principal, "ai_caption.use")
    scope_pipeline(principal, pipeline_id)
    limit = min(200, max(1, limit))
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        rows = conn.execute("""SELECT * FROM ai_caption_records WHERE pipeline_id=?
                               ORDER BY id DESC LIMIT ?""", (pipeline_id, limit)).fetchall()
    return [record_payload(row) for row in rows]


@app.post("/api/auth/login")
def login(data: CTVLogin, request: Request):
    """Sign in as administrator, or as a collaborator with username, password and pipeline key."""
    ip = client_ip(request)
    username = data.username.strip()
    if LOGIN_THROTTLE.is_blocked(ip, username):
        raise HTTPException(429, f"Quá nhiều lần đăng nhập sai. Thử lại sau {LOGIN_THROTTLE.retry_after(ip, username)} giây.")
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user is None:
            LOGIN_THROTTLE.record_failure(ip, username)
            raise HTTPException(401, "Thông tin đăng nhập không hợp lệ")
        if not verify_secret(user["password_hash"], data.password):
            LOGIN_THROTTLE.record_failure(ip, username)
            raise HTTPException(401, "Thông tin đăng nhập không hợp lệ")
        if user["status"] != STATUS_ACTIVE:
            raise HTTPException(403, "Tài khoản đã bị vô hiệu hóa")
        pipeline_id = None
        key_id = None
        membership = None
        if user["role"] == ROLE_CTV:
            try:
                membership = unique_active_membership(conn, user["id"])
            except MembershipConflict as error:
                raise HTTPException(403, {"code": error.code, "message": error.message}) from error
            if membership is None:
                raise HTTPException(403, "Tài khoản chưa được gán cho pipeline nào")
            pipeline_id = membership["pipeline_id"]
            pipeline = conn.execute("SELECT * FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
            if pipeline is None or not pipeline["enabled"]:
                raise HTTPException(403, "Workspace đã bị khóa")
            # The key is a second factor for this exact membership, never a
            # stand-alone grant: it must belong to the pipeline the admin
            # assigned to this collaborator.
            if not data.pipeline_key.strip():
                raise HTTPException(401, "Thiếu Pipeline Access Key")
            candidate = data.pipeline_key.strip()
            key_row = conn.execute(
                "SELECT * FROM pipeline_access_keys WHERE key_prefix=?", (key_prefix(candidate),)).fetchone()
            if key_row is None or key_row["pipeline_id"] != pipeline_id:
                raise HTTPException(401, "Pipeline Access Key không hợp lệ cho tài khoản này")
            if key_row["revoked_at"]:
                raise HTTPException(401, "Pipeline Access Key đã bị thu hồi")
            if key_row["expires_at"] and key_row["expires_at"] < iso():
                raise HTTPException(401, "Pipeline Access Key đã hết hạn")
            if not verify_secret(key_row["key_hash"], candidate):
                raise HTTPException(401, "Pipeline Access Key không hợp lệ cho tài khoản này")
            key_id = key_row["id"]
        token, csrf, session_id = create_session(conn, user["id"], pipeline_id, key_id)
        conn.execute("UPDATE users SET updated_at=? WHERE id=?", (iso(), user["id"]))
    LOGIN_THROTTLE.clear(ip, username)
    granted = normalise_permissions(membership["permissions_json"]) if membership else set()
    principal = Principal(user_id=user["id"], username=user["username"], role=user["role"],
                          status=user["status"], pipeline_id=pipeline_id, session_id=session_id,
                          permissions=frozenset(granted),
                          must_change_password=bool(user["must_change_password"]), csrf_token=csrf)
    CURRENT_PRINCIPAL.set(principal)
    audit("auth.login", user["id"], f"{user['username']} đăng nhập ({user['role']})", pipeline_id)
    response = JSONResponse(principal_payload(principal, csrf))
    set_session_cookie(response, token)
    return response


@app.post("/api/auth/logout")
def logout(principal: Principal = Depends(require_auth)):
    with db() as conn:
        if principal.session_id:
            revoke_session(conn, principal.session_id)
    audit("auth.logout", principal.user_id, f"{principal.username} đăng xuất")
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
def auth_me(principal: Principal = Depends(require_auth)):
    """Who am I, which pipeline am I pinned to and what may I do."""
    with db() as conn:
        pipeline_name = None
        if principal.pipeline_id is not None:
            row = conn.execute("SELECT name FROM pipelines WHERE id=?", (principal.pipeline_id,)).fetchone()
            pipeline_name = row["name"] if row else None
    payload = principal_payload(principal, principal.csrf_token)
    payload["pipeline_name"] = pipeline_name
    return payload


@app.post("/api/auth/change-password")
def change_password(data: PasswordChange, principal: Principal = Depends(require_auth)):
    """Rotate the caller's own password. The old password must be supplied unless
    this is a forced first-time change (must_change_password is set)."""
    if weak_password(data.new_password):
        raise HTTPException(422, "Mật khẩu mới quá yếu hoặc quá dễ đoán; cần tối thiểu 12 ký tự đa dạng")
    with db() as conn:
        row = conn.execute("SELECT password_hash, must_change_password FROM users WHERE id=?", (principal.user_id,)).fetchone()
        is_forced_change = bool(row["must_change_password"])
        if not is_forced_change:
            if not data.current_password or not verify_secret(row["password_hash"], data.current_password):
                raise HTTPException(401, "Mật khẩu hiện tại không đúng")
        conn.execute("""UPDATE users SET password_hash=?, must_change_password=0, updated_at=? WHERE id=?""",
                     (hash_secret(data.new_password), iso(), principal.user_id))
        revoke_user_sessions(conn, principal.user_id)
    audit("auth.password_changed", principal.user_id, f"{principal.username} đổi mật khẩu")
    response = JSONResponse({"ok": True, "message": "Đã đổi mật khẩu; đăng nhập lại."})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.post("/api/auth/sessions/revoke")
def revoke_my_sessions(principal: Principal = Depends(require_auth)):
    """Sign this collaborator out of every other device."""
    with db() as conn:
        count = revoke_user_sessions(conn, principal.user_id)
    audit("auth.sessions_revoked", principal.user_id, f"{principal.username} thu hồi {count} phiên")
    response = JSONResponse({"ok": True, "revoked": count})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/summary")
def summary(principal: Principal = Depends(require_principal)):
    """Counters for the caller's own workspace only."""
    with db() as conn:
        if principal.is_admin:
            stats = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("pipelines", "accounts", "groups", "jobs")}
            stats["queued"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
            stats["review"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('needs_review','submitted_unverified','pending_approval')").fetchone()[0]
            stats["posted"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='posted'").fetchone()[0]
            return stats
        pipeline_id = principal.pipeline_id
        stats = {
            "pipelines": 1,
            "accounts": conn.execute("SELECT COUNT(*) FROM accounts WHERE pipeline_id=?", (pipeline_id,)).fetchone()[0],
            "groups": conn.execute("SELECT COUNT(*) FROM groups WHERE pipeline_id=?", (pipeline_id,)).fetchone()[0],
            "jobs": conn.execute("""SELECT COUNT(*) FROM jobs j JOIN posts o ON j.post_id=o.id
                                   WHERE o.pipeline_id=?""", (pipeline_id,)).fetchone()[0],
            "queued": conn.execute("""SELECT COUNT(*) FROM jobs j JOIN posts o ON j.post_id=o.id
                                     WHERE o.pipeline_id=? AND j.state='queued'""", (pipeline_id,)).fetchone()[0],
            "review": conn.execute("""SELECT COUNT(*) FROM jobs j JOIN posts o ON j.post_id=o.id
                                     WHERE o.pipeline_id=? AND j.state IN ('needs_review','submitted_unverified','pending_approval')""",
                                   (pipeline_id,)).fetchone()[0],
            "posted": conn.execute("""SELECT COUNT(*) FROM jobs j JOIN posts o ON j.post_id=o.id
                                     WHERE o.pipeline_id=? AND j.state='posted'""", (pipeline_id,)).fetchone()[0],
        }
        return stats


@app.get("/api/pipelines")
def pipelines(principal: Principal = Depends(require_principal)):
    """A collaborator only ever sees the single workspace granted to them."""
    with db() as conn:
        sql = """
            SELECT p.*, (SELECT COUNT(*) FROM accounts a WHERE a.pipeline_id=p.id) accounts_count,
                (SELECT COUNT(*) FROM groups g WHERE g.pipeline_id=p.id) groups_count,
                (SELECT COUNT(*) FROM jobs j JOIN posts o ON j.post_id=o.id WHERE o.pipeline_id=p.id AND j.state='queued') queued_count
            FROM pipelines p"""
        params: list = []
        if not principal.is_admin:
            sql += " WHERE p.id=?"
            params.append(principal.pipeline_id)
        return [dict(row) for row in conn.execute(sql + " ORDER BY p.id DESC", params)]


@app.post("/api/pipelines", status_code=201)
def create_pipeline(data: PipelineCreate, _: Principal = Depends(require_admin)):
    try:
        with db() as conn:
            result = conn.execute("INSERT INTO pipelines(name,description,created_at) VALUES(?,?,?)",
                                  (data.name, data.description, iso()))
            created_id = result.lastrowid
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "Pipeline name already exists") from error
    audit("pipeline", created_id, f"Created {data.name}", created_id)
    return {"id": created_id}


@app.patch("/api/pipelines/{pipeline_id}")
def patch_pipeline(pipeline_id: int, data: EnabledUpdate, principal: Principal = Depends(require_admin)):
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        updates = []
        params = []
        if data.enabled is not None:
            updates.append("enabled=?")
            params.append(int(data.enabled))
        if data.description is not None:
            updates.append("description=?")
            params.append(data.description)
        if updates:
            updates.append("updated_at=?")
            params.append(iso())
            params.append(pipeline_id)
            conn.execute(f"UPDATE pipelines SET {', '.join(updates)} WHERE id=?", params)
    return {"ok": True, "description_saved": data.description is not None}


def read_uid(token: str) -> str:
    """Decrypt a stored Facebook UID. Returns '' when absent or unreadable."""
    if not token:
        return ""
    try:
        return decrypt(token, APP_SECRET)
    except DecryptError:
        return ""


@app.get("/api/accounts")
def accounts(pipeline_id: int | None = None, principal: Principal = Depends(require_principal)):
    """The Facebook accounts of one workspace.

    A collaborator always reads their own workspace, taken from the session.
    A link that still points at another workspace must not empty their list,
    so the requested id is only honoured for an administrator.
    """
    require_permission(principal, "facebook.view")
    try:
        condition, params = pipeline_scope.scope_list(principal, pipeline_id, "a.pipeline_id",
                                                          "a.owner_user_id")
    except pipeline_scope.NoWorkspace:
        raise HTTPException(403, "Tài khoản chưa được gán Workspace")
    with db() as conn:
        sql = """SELECT a.id,a.pipeline_id,a.label,a.profile_url,a.enabled,a.login_pid,a.created_at,
                        a.auth_method,a.cookie_updated_at,a.session_status,a.session_checked_at,
                        a.session_details,a.fb_name,a.fb_avatar,a.fb_uid_enc,p.name pipeline_name,
                        a.owner_user_id,
                        (SELECT u.username FROM users u WHERE u.id=a.owner_user_id) owner_username
                 FROM accounts a JOIN pipelines p ON p.id=a.pipeline_id
                WHERE """ + condition + " ORDER BY a.id DESC"
        result = [dict(row) for row in conn.execute(sql, params)]
    for account in result:
        account.update(account_session_state.presentation(account))
        account["login_open"] = pid_active(account["login_pid"], account["id"])
        account["profile_busy"] = profile_busy(DATA, account["id"])
        account["fb_uid"] = read_uid(account.pop("fb_uid_enc", ""))
        account.pop("login_pid", None)
    return result


@app.post("/api/accounts", status_code=201)
def create_account(data: AccountCreate, principal: Principal = Depends(require_principal)):
    """Add one Facebook account to a workspace.

    A collaborator adds to their own workspace, decided by the session. The
    account stays visible to the whole workspace and to the administrator.
    """
    require_permission(principal, "facebook.add")
    try:
        workspace = pipeline_scope.workspace_for(principal, data.pipeline_id)
    except pipeline_scope.WorkspaceRequired:
        raise HTTPException(422, "Thiếu pipeline_id")
    except pipeline_scope.NoWorkspace:
        raise HTTPException(403, "Tài khoản chưa được gán Workspace")
    if data.profile_url:
        parsed = urlparse(data.profile_url)
        if parsed.scheme != "https" or parsed.hostname not in ("facebook.com", "www.facebook.com", "m.facebook.com"):
            raise HTTPException(422, "Profile link must use facebook.com HTTPS")
    with db() as conn:
        entry(conn, "pipelines", workspace)
        result = conn.execute("""INSERT INTO accounts(pipeline_id,label,profile_url,created_at,owner_user_id)
                                 VALUES(?,?,?,?,?)""",
                              (workspace, data.label, data.profile_url, iso(),
                               resource_owner(principal)))
        created_id = result.lastrowid
    audit("account", created_id, "Added account (local browser profile)", workspace)
    return {"id": created_id}


@app.patch("/api/accounts/{account_id}")
def patch_account(account_id: int, data: EnabledUpdate, principal: Principal = Depends(require_principal)):
    with db() as conn:
        account = scoped_entry(conn, "accounts", account_id, principal)
    require_permission(principal, "facebook.update_session")
    with db() as conn:
        conn.execute("UPDATE accounts SET enabled=? WHERE id=?", (int(data.enabled), account_id))
    audit("account", account_id, "Tắt/bật tài khoản", account["pipeline_id"])
    return {"ok": True}


@app.post("/api/accounts/{account_id}/cookies")
async def upload_account_cookies(account_id: int, file: UploadFile = File(...), principal: Principal = Depends(require_principal)):
    """Import cookies for one account; never store the uploaded JSON itself."""
    with db() as conn:
        account = scoped_entry(conn, "accounts", account_id, principal)
    require_permission(principal, "facebook.update_session")
    with db() as conn:
        if pid_active(account["login_pid"], account_id):
            raise HTTPException(409, "Đóng trình duyệt đăng nhập trước khi nhập cookie")
        running = conn.execute("SELECT 1 FROM jobs WHERE account_id=? AND state='running' LIMIT 1", (account_id,)).fetchone()
        if running:
            raise HTTPException(409, "Facebook đang đăng bài, thử lại sau")

    with IMPORT_LOCK:
        if account_id in IMPORT_IN_PROGRESS:
            raise HTTPException(409, "Tài khoản đang nhập cookie")
        IMPORT_IN_PROGRESS.add(account_id)
    try:
        raw = await file.read(MAX_COOKIE_BYTES + 1)
        try:
            cookies, uid = parse_cookie_file(raw)
        except InvalidCookies as error:
            raise HTTPException(422, str(error)) from error
        fingerprint = hmac.new(APP_SECRET.encode(), uid.encode(), hashlib.sha256).hexdigest()
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = scoped_entry(conn, "accounts", account_id, principal)
            if account["fb_uid_fingerprint"] and account["fb_uid_fingerprint"] != fingerprint:
                raise HTTPException(409, "File thuộc Facebook khác; hãy tạo tài khoản mới để tránh nhầm phiên")
            try:
                claim_facebook_identity(conn,account_id,account['pipeline_id'],fingerprint)
            except DuplicateFacebookIdentity as error:
                raise HTTPException(409, "Facebook này đã được liên kết; vui lòng dùng tài khoản hiện có.") from error
        try:
            from fb_browser import import_cookie_session
            await import_cookie_session(account_id, cookies)
        except ProfileBusy as error:
            LOG.warning("Account %s cookie import skipped: browser profile busy", account_id)
            raise HTTPException(409, "Hồ sơ trình duyệt đang được dùng; đóng trình duyệt đăng nhập rồi thử lại") from error
        except Exception as error:
            LOG.warning("Account %s cookie import failed: %s", account_id, type(error).__name__)
            raise HTTPException(503, "Không thể mở Chromium để nhập cookie. Kiểm tra fpm-desktop và Playwright") from error
        with db() as conn:
            conn.execute("""UPDATE accounts SET auth_method='cookie_file', cookie_updated_at=?, fb_uid_fingerprint=?,
                            session_status='unknown', session_checked_at=NULL,
                            session_details='Đã nạp cookie mới; cần kiểm tra lại phiên đăng nhập'
                            WHERE id=?""", (iso(), fingerprint, account_id))
        audit("account", account_id, "Imported Facebook cookie file into local browser profile (unverified)")
        return {"ok": True, "account_id": account_id, "cookie_count": len(cookies),
                "verified": False, "message": "Đã nhập JSON vào hồ sơ trình duyệt. Cần kiểm tra phiên trên Facebook."}
    finally:
        await file.close()
        with IMPORT_LOCK:
            IMPORT_IN_PROGRESS.discard(account_id)


@app.post("/api/accounts/{account_id}/connect")
def connect_account(account_id: int, principal: Principal = Depends(require_principal)):
    with db() as conn:
        account = scoped_entry(conn, "accounts", account_id, principal)
    require_permission(principal, "facebook.update_session")
    with IMPORT_LOCK:
        if account_id in IMPORT_IN_PROGRESS:
            raise HTTPException(409, "Đang nhập cookie; chờ hoàn tất")
    with db() as conn:
        if pid_active(account["login_pid"], account_id):
            return {"ok": True, "message": "Browser already open; use VNC"}
        if profile_busy(DATA, account_id):
            raise HTTPException(409, "Hồ sơ trình duyệt đang bận; thử lại sau")
        log_file = DATA / f"login-{account_id}.log"
        (DATA / f"login-stop-{account_id}.flag").unlink(missing_ok=True)
        with log_file.open("ab") as handle:
            proc = subprocess.Popen([sys.executable, str(ROOT / "fb_browser.py"), "login", str(account_id)],
                                    cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
                                    start_new_session=True, env={**os.environ, "DATA_DIR": str(DATA), "DISPLAY": ":99"})
        conn.execute("UPDATE accounts SET login_pid=? WHERE id=?", (proc.pid, account_id))
    audit("account", account_id, "Launched local browser for manual Facebook login")
    return {"ok": True, "pid": proc.pid, "message": "Open SSH-forwarded noVNC to log in; then close login window"}


@app.post("/api/accounts/{account_id}/close")
def close_account(account_id: int, principal: Principal = Depends(require_principal)):
    with db() as conn:
        account = scoped_entry(conn, "accounts", account_id, principal)
    require_permission(principal, "facebook.update_session")
    with db() as conn:
        if pid_active(account["login_pid"], account_id):
            (DATA / f"login-stop-{account_id}.flag").touch(mode=0o600)
            for _ in range(12):
                if not pid_active(account["login_pid"], account_id):
                    break
                time.sleep(0.25)
            if pid_active(account["login_pid"], account_id):
                os.killpg(account["login_pid"], signal.SIGTERM)
        conn.execute("UPDATE accounts SET login_pid=NULL WHERE id=?", (account_id,))
    return {"ok": True}


def wait_profile_free(account_id: int, timeout: float = 8.0) -> bool:
    """Wait until no other process drives this account's browser profile."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not profile_busy(DATA, account_id):
            return True
        time.sleep(0.4)
    return not profile_busy(DATA, account_id)


@app.post("/api/accounts/{account_id}/check")
def check_account(account_id: int, principal: Principal = Depends(require_principal)):
    """Verify this account's stored session against Facebook.

    Reports connected / expired / checkpoint / busy / error. Never bypasses a
    CAPTCHA, a checkpoint or a 2FA challenge, and never returns cookie values.
    """
    with db() as conn:
        account = scoped_entry(conn, "accounts", account_id, principal)
    require_permission(principal, "facebook.update_session")
    with db() as conn:
        if pid_active(account["login_pid"], account_id):
            raise HTTPException(409, "Trình duyệt đăng nhập đang mở; đóng nó rồi kiểm tra lại")
    if profile_busy(DATA, account_id):
        raise HTTPException(409, "Hồ sơ trình duyệt đang bận; thử lại sau")

    payload_file = DATA / f"check-{account_id}-{secrets.token_hex(6)}.json"
    result_file = DATA / f"check-result-{account_id}-{secrets.token_hex(6)}.json"
    payload_file.write_text(json.dumps({"account_id": account_id}), encoding="utf-8")
    payload_file.chmod(0o600)
    outcome = {"status": "error", "name": "", "uid": "", "avatar": "",
               "details": "Không thể chạy trình duyệt kiểm tra"}
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fb_browser.py"), "check", str(payload_file), str(result_file)],
            cwd=ROOT, env={**os.environ, "DATA_DIR": str(DATA), "DISPLAY": ":99"},
            capture_output=True, text=True, timeout=180)
        if result_file.exists():
            outcome = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            outcome["details"] = f"Worker exit {proc.returncode}: {proc.stderr[-200:]}"
    except subprocess.TimeoutExpired:
        outcome["details"] = "Kiểm tra quá thời gian; thử lại sau"
    except Exception as error:
        outcome["details"] = f"Lỗi kiểm tra: {type(error).__name__}"
    finally:
        payload_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)

    status = str(outcome.get("status", "error"))
    if status not in ("connected", "expired", "checkpoint", "restricted", "busy", "error", "unknown"):
        status = "error"
    details = str(outcome.get("details", ""))[:500]
    uid = str(outcome.get("uid", "")) if status == "connected" else ""
    name = str(outcome.get("name", ""))[:120] if status == "connected" else ""
    avatar = str(outcome.get("avatar", ""))[:2000] if status == "connected" else ""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT fb_name,fb_avatar,fb_uid_enc FROM accounts WHERE id=? AND pipeline_id=?",
                                (account_id, account["pipeline_id"])).fetchone()
        try:
            account_session_state.record_check(conn, account["pipeline_id"], account_id,
                                               {**outcome, "status": status}, iso(),
                                               encrypt_uid=lambda value: encrypt(value, APP_SECRET),
                                               fingerprint_uid=lambda value: hmac.new(APP_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest())
        except account_session_state.SessionIdentityMismatch:
            raise HTTPException(409, "Phiên trình duyệt không khớp tài khoản Facebook đã lưu.")
    audit("account", account_id, f"Session check: {status}")
    return {"ok": True, "account_id": account_id, "status": status, "details": details,
            "name": name or previous["fb_name"], "uid": uid, "avatar": avatar,
            "checked_at": iso()}


@app.post("/api/accounts/{account_id}/relogin")
def relogin_account(account_id: int, principal: Principal = Depends(require_principal)):
    """Close any running login browser and open a fresh one for this account."""
    close_account(account_id, principal)
    wait_profile_free(account_id)
    return connect_account(account_id, principal)


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, principal: Principal = Depends(require_admin)):
    """Remove one account and its isolated browser profile.

    Only this account's own rows are affected; other pipelines are untouched.
    """
    with db() as conn:
        account = scoped_entry(conn, "accounts", account_id, principal)
        if pid_active(account["login_pid"], account_id):
            raise HTTPException(409, "Đóng trình duyệt đăng nhập trước khi xóa tài khoản")
        if profile_busy(DATA, account_id):
            raise HTTPException(409, "Hồ sơ trình duyệt đang bận; thử lại sau")
        busy = conn.execute("SELECT COUNT(*) FROM jobs WHERE account_id=? AND state='running'", (account_id,)).fetchone()[0]
        busy += conn.execute("SELECT COUNT(*) FROM manual_tasks WHERE account_id=? AND status='running'", (account_id,)).fetchone()[0]
        if busy:
            raise HTTPException(409, "Tài khoản đang chạy công việc; thử lại sau")
        groups_count = conn.execute("SELECT COUNT(*) FROM groups WHERE account_id=?", (account_id,)).fetchone()[0]
        jobs_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE account_id=?", (account_id,)).fetchone()[0]
        tasks_count = conn.execute("SELECT COUNT(*) FROM manual_tasks WHERE account_id=?", (account_id,)).fetchone()[0]
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    profile = PROFILES / f"account-{account_id}"
    if profile.is_dir():
        shutil.rmtree(profile, ignore_errors=True)
    audit("account", account_id,
          f"Deleted account with {groups_count} groups, {jobs_count} jobs, {tasks_count} manual tasks")
    return {"ok": True, "deleted_groups": groups_count, "deleted_jobs": jobs_count,
            "deleted_manual_tasks": tasks_count}


def group_already_present(conn, pipeline_id: int, group_key: str, aliases=None,
                           owner_user_id: int | None = None) -> bool:
    """True when this owner already holds the group under any of its keys."""
    for candidate in [group_key] + list(aliases or []):
        escaped = sql_like_escape(candidate)
        if cycle_repository.available(conn) and conn.execute("SELECT 1 FROM groups WHERE pipeline_id=? AND group_key=?",(pipeline_id,candidate)).fetchone():
            return True
        row = conn.execute("""SELECT id FROM groups WHERE pipeline_id=? AND owner_user_id IS ?
                              AND (group_key=? OR ',' || source_key || ',' LIKE '%,' || ? || ',%' ESCAPE '\\')""",
                            (pipeline_id, owner_user_id, candidate, escaped)).fetchone()
        if row is not None:
            return True
    return False


def group_config(conn, group_id: int, principal: Principal | None = None) -> dict:
    """The effective account, posting mode and nickname of a group.

    A group may be linked to several accounts. The assignment row wins over the
    group default, so a group can be posted by a different identity without
    changing the other accounts. A collaborator always gets the identity of an
    account they own, never the identity of another collaborator.
    """
    row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Not found")
    assignments = conn.execute(
        "SELECT * FROM group_accounts WHERE group_id=? AND status='active' ORDER BY id",
        (group_id,)).fetchall()
    assignment = None
    if principal is not None and not principal.is_admin:
        for candidate in assignments:
            account = conn.execute("SELECT owner_user_id FROM accounts WHERE id=?",
                                    (candidate["account_id"],)).fetchone()
            if account is None:
                continue
            if account["owner_user_id"] is None or account["owner_user_id"] == principal.user_id:
                assignment = candidate
                break
    if assignment is None:
        assignment = assignments[0] if assignments else None
    if assignment is not None:
        account_id = assignment["account_id"]
        mode = assignment["posting_mode"]
        nickname = assignment["nickname"]
    else:
        account_id = row["account_id"]
        mode = row["posting_mode"]
        nickname = row["nickname"]
    return {"group_id": group_id, "pipeline_id": row["pipeline_id"], "account_id": account_id,
            "posting_mode": mode, "nickname": nickname, "row": row}


def upsert_group_account(conn, pipeline_id: int, group_id: int, account_id: int,
                         mode: str, nickname: str) -> None:
    conn.execute("""INSERT INTO group_accounts
        (pipeline_id,account_id,group_id,posting_mode,nickname,status,created_at,updated_at)
        VALUES(?,?,?,?,?,'active',?,?)
        ON CONFLICT(account_id,group_id) DO UPDATE SET
            posting_mode=excluded.posting_mode, nickname=excluded.nickname,
            status='active', updated_at=excluded.updated_at""",
                 (pipeline_id, account_id, group_id, mode, nickname, iso(), iso()))


def group_payload(conn, row, principal: Principal | None = None) -> dict:
    """A group with its counters and its effective configuration."""
    item = dict(row)
    item["shared"] = row["owner_user_id"] is None if "owner_user_id" in row.keys() else True
    posted = conn.execute("SELECT COUNT(*) FROM jobs WHERE group_id=? AND state='posted'",
                           (row["id"],)).fetchone()[0]
    pending = conn.execute("""SELECT COUNT(*) FROM jobs WHERE group_id=?
                              AND state IN ('queued','running','pending_approval')""",
                           (row["id"],)).fetchone()[0]
    failed = conn.execute("""SELECT COUNT(*) FROM jobs WHERE group_id=?
                              AND state IN ('failed','needs_review')""",
                          (row["id"],)).fetchone()[0]
    config = group_config(conn, row["id"], principal)
    accounts = [dict(entry) for entry in conn.execute(
        """SELECT ga.id,ga.account_id,ga.posting_mode,ga.nickname,ga.status,a.label account_label
           FROM group_accounts ga JOIN accounts a ON a.id=ga.account_id
           WHERE ga.group_id=? AND ga.status='active' ORDER BY ga.id""", (row["id"],)).fetchall()]
    item.update({
        "posted_count": posted, "pending_count": pending, "failed_count": failed,
        "account_id": config["account_id"], "posting_mode": config["posting_mode"],
        "nickname": config["nickname"], "accounts": accounts,
        "check_details": json.loads(row["check_details_json"] or "{}"),
    })
    return item


def screening_row(conn, account_id: int, group_id: int):
    return conn.execute("""SELECT * FROM group_screening_results
                           WHERE account_id=? AND group_id=?""",
                        (account_id, group_id)).fetchone()


def store_screening_result(conn, pipeline_id: int, account_id: int, group_id: int,
                           check: dict | None = None, source_override: str = "",
                           status_override: str = "") -> dict:
    """Lưu quan sát của trình duyệt về một cặp Facebook–Group.

    Kết luận về quyền đăng không nằm ở đây. Nó nằm ở bảng bằng chứng
    ``group_post_verifications`` và chỉ được đưa ra sau một lần gửi bài thật.
    """
    cycle_repository.record_pair_observation(conn,pipeline_id,account_id,group_id,check)
    evidence = build_evidence(conn, pipeline_id, account_id, group_id, check)
    status, source, confidence = classify(evidence)
    if status_override:
        status = status_override
        source = source_override or "manual"
        confidence = 1.0
    else:
        # A decision an administrator made by hand is not overwritten by a later
        # automatic classification, only the evidence is refreshed.
        previous = screening_row(conn, account_id, group_id)
        if previous is not None and previous["verification_source"] == "manual":
            status = previous["moderation_status"]
            source = "manual"
            confidence = previous["confidence"]
    now = iso()
    expires = (datetime.now(timezone.utc) + timedelta(days=FRESHNESS_DAYS)).isoformat(timespec="seconds")
    conn.execute("""INSERT INTO group_screening_results
        (pipeline_id,account_id,group_id,moderation_status,verification_source,confidence,
         evidence_json,last_checked_at,expires_at,last_direct_post_at,last_pending_post_at,
         last_rejected_post_at,direct_posts,pending_posts,rejected_posts,notes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id,group_id) DO UPDATE SET
            moderation_status=excluded.moderation_status,
            verification_source=excluded.verification_source,
            confidence=excluded.confidence,
            evidence_json=excluded.evidence_json,
            last_checked_at=excluded.last_checked_at,
            expires_at=excluded.expires_at,
            last_direct_post_at=excluded.last_direct_post_at,
            last_pending_post_at=excluded.last_pending_post_at,
            last_rejected_post_at=excluded.last_rejected_post_at,
            direct_posts=excluded.direct_posts,
            pending_posts=excluded.pending_posts,
            rejected_posts=excluded.rejected_posts,
            notes=excluded.notes,
            updated_at=excluded.updated_at""",
                 (pipeline_id, account_id, group_id, status, source, confidence,
                  json.dumps(evidence, ensure_ascii=False), now, expires,
                  evidence.get("last_direct_post_at"), evidence.get("last_pending_post_at"),
                  evidence.get("last_rejected_post_at"), evidence.get("direct_posts", 0),
                  evidence.get("pending_posts", 0), evidence.get("rejected_posts", 0),
                  evidence_summary(evidence), now, now))
    # Trạng thái hiển thị trên group đến từ bằng chứng đăng bài, không phải từ
    # quan sát của trình duyệt.
    group_verification.sync_group_status(conn, pipeline_id, group_id)
    return {"status": status, "source": source, "confidence": confidence, "evidence": evidence}


def stage_album_media(conn, pipeline_id: int, picture) -> str:
    """Chuẩn bị ảnh của Album cho worker đăng bài.

    Worker đọc ảnh từ thư mục upload, nên ảnh trong Album được chép sang đó
    và trả về đường dẫn tương đối mà worker hiểu.
    """
    media_path = f"uploads/{int(pipeline_id)}/{Path(picture['storage_key']).name}"
    target = UPLOADS / str(int(pipeline_id)) / Path(picture["storage_key"]).name
    target.parent.mkdir(parents=True, exist_ok=True)
    if media_cloud.enabled():
        payload = album_bytes(conn, picture)
        if payload is None:
            raise HTTPException(409, "Tệp ảnh không còn tồn tại trên bộ nhớ")
        target.write_bytes(payload)
    else:
        source = safe_join(DATA, picture["storage_key"])
        if not source.is_file():
            raise HTTPException(409, "Tệp ảnh không còn tồn tại")
        shutil.copyfile(source, target)
    target.chmod(0o600)
    return media_path


def prepare_verification_post(conn, pipeline_id: int, account_id: int, group_id: int) -> dict:
    """Nội dung bài xác minh để CTV xem trước. Hàm này không ghi gì.

    Bài dùng ảnh đã qua kiểm tra trong Album, đúng tài khoản và đúng chế độ
    đăng mà CTV đã chọn.
    """
    group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    account = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if group is None or account is None:
        raise HTTPException(404, "Not found")
    mode, nickname = group_eligibility.mode_of(conn, account_id, group_id, group)
    picture = conn.execute("""SELECT * FROM media_assets
                               WHERE pipeline_id=? AND usable=1 AND locked=0
                                 AND analysis_status='done'
                               ORDER BY usage_count ASC, id DESC LIMIT 1""",
                            (int(pipeline_id),)).fetchone()
    if picture is None:
        raise HTTPException(409, "Album chưa có ảnh đã kiểm tra để dùng cho bài xác minh")
    return {"body": "Bài kiểm tra quyền đăng của nhóm.",
            "media_description": picture["description"] or "",
            "tags": [],
            "media": [picture["storage_key"]],
            "media_name": picture["description"] or "",
            "media_id": picture["id"],
            "picture": picture,
            "account_id": account_id, "account_label": account["label"],
            "group_id": group_id, "group_label": group["label"],
            "posting_mode": mode, "nickname": nickname}


def create_verification_post(conn, pipeline_id: int, account_id: int, group_id: int) -> dict:
    """Tạo bài xác minh và xếp hàng gửi.

    Chỉ gọi sau khi CTV đã xem trước và xác nhận.
    """
    draft = prepare_verification_post(conn, pipeline_id, account_id, group_id)
    mode = draft["posting_mode"]
    nickname = draft["nickname"]
    # Caption theo AI Profile của Workspace, giống luồng đăng tự động.
    settings = posting_limits.settings_of(conn, pipeline_id)
    caption_result = build_caption(draft.get("media_description", ""),
                                   list(draft.get("tags") or []),
                                   settings.get("topic") or "sản phẩm",
                                   load_ai_config(),
                                   posting_mode=mode)
    body = caption_result.get("caption") or draft["body"]
    if caption_result.get("status") == "blocked":
        raise HTTPException(409, "Nội dung bị chặn bởi bộ kiểm tra an toàn")
    media_path = stage_album_media(conn, pipeline_id, draft["picture"])
    cursor = conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)
                             VALUES(?,?,?,?,1)""",
                          (int(pipeline_id), body, json.dumps([media_path]), iso()))
    post_id = cursor.lastrowid
    job = conn.execute("""INSERT INTO jobs
        (post_id,group_id,account_id,pipeline_id,state,details,scheduled_at,updated_at,
         requested_posting_mode,requested_nickname,posting_status,verification_status,
         is_verification)
        VALUES(?,?,?,?,'queued','Bài xác minh quyền đăng',?,?,?,?,'pending','unverified',1)""",
        (post_id, group_id, account_id, int(pipeline_id), iso(), iso(), mode, nickname))
    result = {key: value for key, value in draft.items() if key != "picture"}
    result["media"] = [media_path]
    return {"post_id": post_id, "job_id": job.lastrowid, **result}


def run_group_join(pipeline_id: int, account_id: int, group_id: int,
                   answers: dict | None = None) -> dict:
    """Chạy thao tác tham gia group bằng đúng tài khoản của group.

    Hàm chỉ **chạy** thao tác và trả về kết quả quan sát được. Việc ghi lại
    trạng thái do tầng API làm, để cùng một đường đi cho mọi nơi gọi.
    """
    with db() as conn:
        group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if group is None:
            raise HTTPException(404, "Not found")
        url = group["url"]
    context = group_join_worker.JoinContext(db=db, iso=iso, root=ROOT, data_dir=DATA,
                                            provider_config=load_ai_config)
    outcome = group_join_worker.run_join_in_browser(context, account_id, url, answers or {})
    outcome.setdefault("state", "failed")
    outcome.setdefault("status", "error")
    return outcome


def group_join_worker_thread() -> None:
    """Worker gửi yêu cầu tham gia cho các group CTV đã bật tự động tham gia."""
    context = group_join_worker.JoinContext(db=db, iso=iso, root=ROOT, data_dir=DATA,
                                            provider_config=load_ai_config)

    def pipeline_ids() -> list[int]:
        with db() as conn:
            rows = conn.execute("""SELECT DISTINCT pipeline_id FROM groups
                                   WHERE auto_join=1 AND enabled=1""").fetchall()
        return [int(row["pipeline_id"]) for row in rows]

    group_join_worker.join_worker(context, pipeline_ids, STOP_EVENT)


def refresh_screening_for_job(conn, job_id: int) -> None:
    """Cập nhật bằng chứng, phân loại và quan sát sau khi kết quả bài đổi."""
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        return
    # Bằng chứng đăng bài thật là nguồn kết luận duy nhất.
    group_verification.sync_from_job(conn, job_id)
    row = screening_row(conn, job["account_id"], job["group_id"])
    check = {}
    if row is not None:
        stored = json.loads(row["evidence_json"] or "{}")
        check = {"status": stored.get("check_status", ""),
                 "accessible": stored.get("accessible"),
                 "is_member": stored.get("is_member"),
                 "can_post": stored.get("can_post"),
                 "requires_approval": stored.get("requires_approval"),
                 "rules_mention_approval": stored.get("rules_mention_approval"),
                 "supports_anonymous": stored.get("supports_anonymous"),
                 "supports_nickname": stored.get("supports_nickname"),
                 "nickname_available": stored.get("nickname_available"),
                 "group_name": stored.get("group_name", ""),
                 "details": stored.get("check_details", "")}
    store_screening_result(conn, job["pipeline_id"], job["account_id"], job["group_id"], check)
    if row is not None:
        conn.execute('UPDATE group_screening_results SET last_checked_at=?,expires_at=? WHERE pipeline_id=? AND account_id=? AND group_id=?',
            (row['last_checked_at'],row['expires_at'],job['pipeline_id'],job['account_id'],job['group_id']))
    # Phân loại được học từ kết quả đăng bài thật của chính cặp này.
    verification = group_verification.row_of(conn, job["pipeline_id"], job["account_id"],
                                            job["group_id"])
    if verification is None:
        return
    group_row = conn.execute("SELECT requires_approval FROM groups WHERE id=?",
                             (job["group_id"],)).fetchone()
    group_requires_approval = bool(group_row["requires_approval"]) if group_row else False
    evidence = {}
    if row is not None:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            evidence = {}
    group_classification.record_outcome(
        conn, job["pipeline_id"], job["account_id"], job["group_id"],
        verification["verification_status"], group_requires_approval=group_requires_approval,
        evidence=evidence, evidence_reference=str(verification["evidence_reference"] or ""))


def enqueue_screening(conn, pipeline_id: int, account_id: int, group_id: int, force: bool) -> bool:
    """Put a group into the screening queue. Returns True when it was queued."""
    if not force:
        moments = []
        row = screening_row(conn, account_id, group_id)
        if row is not None and row["last_checked_at"]:
            moments.append(row["last_checked_at"])
        group = conn.execute("SELECT screening_checked_at FROM groups WHERE id=?", (group_id,)).fetchone()
        if group is not None and group["screening_checked_at"]:
            moments.append(group["screening_checked_at"])
        for value in moments:
            try:
                moment = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue
            if moment is not None and (now() - moment).total_seconds() < RECHECK_COOLDOWN_SECONDS:
                return False
    pending = conn.execute("""SELECT id FROM screening_queue WHERE group_id=? AND account_id=?
                               AND state IN ('queued','running')""", (group_id, account_id)).fetchone()
    if pending is not None:
        if force:
            conn.execute("UPDATE screening_queue SET force=1, updated_at=? WHERE id=?",
                         (iso(), pending["id"]))
        return False
    conn.execute("""INSERT INTO screening_queue
        (pipeline_id,account_id,group_id,state,force,attempts,created_at,updated_at)
        VALUES(?,?,?,'queued',?,0,?,?)""",
                 (pipeline_id, account_id, group_id, 1 if force else 0, iso(), iso()))
    return True


def claim_screening_item():
    """Take the next group out of the screening queue, one at a time."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("""SELECT * FROM screening_queue WHERE state='queued' ORDER BY id""").fetchall()
        for row in rows:
            if not row["force"]:
                group = conn.execute("SELECT screening_checked_at FROM groups WHERE id=?",
                                     (row["group_id"],)).fetchone()
                if group and group["screening_checked_at"]:
                    try:
                        moment = datetime.fromisoformat(group["screening_checked_at"])
                    except (TypeError, ValueError):
                        moment = None
                    if moment and (now() - moment).total_seconds() < RECHECK_COOLDOWN_SECONDS:
                        continue
            conn.execute("UPDATE screening_queue SET state='running', updated_at=? WHERE id=?",
                         (iso(), row["id"]))
            return dict(row)
    return None


def run_screening_item(item: dict) -> None:
    """Check one group with the account session and store the classification."""
    with db() as conn:
        group = conn.execute("SELECT * FROM groups WHERE id=?", (item["group_id"],)).fetchone()
        if group is None:
            conn.execute("UPDATE screening_queue SET state='failed', last_error=?, updated_at=? WHERE id=?",
                         ("Group không còn tồn tại", iso(), item["id"]))
            return
        config = group_config(conn, item["group_id"])
        url = group["url"]
        _,nickname = group_eligibility.mode_of(conn,item['account_id'],item['group_id'],group)
        account_id = item["account_id"]
        pipeline_id = item["pipeline_id"]
    try:
        check = run_group_probe(account_id, url, nickname)
    except Exception as error:
        with db() as conn:
            conn.execute("""UPDATE screening_queue SET state='failed', attempts=attempts+1,
                            last_error=?, updated_at=? WHERE id=?""",
                         (f"{type(error).__name__}", iso(), item["id"]))
        return
    with db() as conn:
        if not account_session_state.record_probe(conn,pipeline_id,account_id,check.pop('session_observation',None),iso()):
            check.update(status='error',accessible=None,is_member=None,can_post=None,details='Chưa xác minh được danh tính phiên Facebook; cần kiểm tra lại tài khoản.')
        store_screening_result(conn, pipeline_id, account_id, item["group_id"], check)
        conn.execute("""UPDATE screening_queue SET state='done', attempts=attempts+1,
                        last_error=?, updated_at=? WHERE id=?""",
                     (str(check.get("details", ""))[:400], iso(), item["id"]))
    audit("screening", item["group_id"], f"Kiểm tra group tự động: {check.get('status', 'unknown')}", pipeline_id)


def screening_worker():
    """Process the screening queue. Only one browser session runs at a time."""
    while not STOP_EVENT.is_set():
        try:
            item = claim_screening_item()
            if item is None:
                STOP_EVENT.wait(5)
                continue
            run_screening_item(item)
            STOP_EVENT.wait(MIN_CHECK_INTERVAL_SECONDS)
        except Exception as error:
            LOG.warning("Screening worker error: %s", type(error).__name__)
            STOP_EVENT.wait(5)


# Chiến dịch đăng group nằm ở module riêng: một bài, nhiều job, mỗi job
# một group, và mọi kiểm tra diễn ra trước khi ảnh được dàn.
app.include_router(group_posts_api.build_router(group_posts_api.Deps(
    db=db,
    require_principal=require_principal,
    require_permission=require_permission,
    scope_pipeline=scope_pipeline,
    scoped_entry=scoped_entry,
    entry=entry,
    audit=audit,
    group_config=group_config,
    resource_owner=resource_owner,
    usable_caption=usable_caption,
    parse_date=parse_date,
    media_file=media_file,
    uploads_root=lambda: UPLOADS,
    data_root=lambda: DATA,
    iso=iso,
)))


# Xác minh quyền đăng và tự động tham gia group nằm ở module riêng.
app.include_router(verification_api.build_router(verification_api.Deps(
    db=db,
    require_principal=require_principal,
    require_permission=require_permission,
    scope_pipeline=scope_pipeline,
    scoped_entry=scoped_entry,
    entry=entry,
    audit=audit,
    enqueue_screening=enqueue_screening,
    screening_row=screening_row,
    group_payload=group_payload,
    iso=iso,
    now=lambda: datetime.now(timezone.utc),
    create_post=lambda *args: create_verification_post(*args),
    prepare_post=lambda *args: prepare_verification_post(*args),
    run_join=lambda *args: run_group_join(*args),
)))



# Kiểm tra group nằm ở module riêng để ``app`` không phải chứa thêm phần xử lý.
app.include_router(groups_api.build_router(groups_api.Deps(
    db=db,
    require_principal=require_principal,
    require_permission=require_permission,
    scope_pipeline=scope_pipeline,
    scoped_entry=scoped_entry,
    entry=entry,
    audit=audit,
    enqueue_screening=enqueue_screening,
    screening_row=screening_row,
    group_payload=group_payload,
)))



PRECHECK_STEPS = [
    ("queued", "Đang chờ kiểm tra"),
    ("probing", "Đang đọc thông số kỹ thuật"),
    ("frames", "Đang lấy khung hình"),
    ("visual", "Đang phân tích hình ảnh"),
    ("audio", "Đang kiểm tra âm thanh"),
    ("caption", "Đang kiểm tra caption"),
    ("copyright", "Đang so sánh với media đã có"),
    ("done", "Đã hoàn tất"),
]


def media_fingerprint_rows(conn, pipeline_id: int) -> list[dict]:
    """The media of this workspace that a new video could duplicate."""
    rows = conn.execute("""SELECT sha256, signature, path, state FROM media_fingerprints
                           WHERE pipeline_id=? OR pipeline_id IS NULL""", (pipeline_id,)).fetchall()
    return [dict(row) for row in rows]


def remember_media(conn, pipeline_id: int, path: Path, sha256: str, signature: str) -> None:
    conn.execute("""INSERT INTO media_fingerprints
        (pipeline_id,sha256,signature,path,size_bytes,state,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?,'known',?,?)
        ON CONFLICT(sha256,path) DO UPDATE SET signature=excluded.signature, last_seen_at=excluded.last_seen_at""",
                 (pipeline_id, sha256, signature, str(path), path.stat().st_size if path.is_file() else 0,
                  iso(), iso()))


def precheck_payload(row) -> dict:
    """The API shape of one pre-check. Internal technical metadata is stripped
    so that end users never see AWS provider names, region, model identifiers,
    raw moderation labels, confidence percentages, or internal file paths.
    Only user-facing status, reasons, and suggestions are returned.
    """
    item = dict(row)
    for name in PRECHECK_CATEGORIES:
        try:
            item[name] = json.loads(item.get(f"{name}_result_json") or "{}")
        except json.JSONDecodeError:
            item[name] = {}
    try:
        item["warnings"] = json.loads(item.get("warnings_json") or "[]")
    except json.JSONDecodeError:
        item["warnings"] = []
    try:
        item["recommendations"] = json.loads(item.get("recommendations_json") or "[]")
    except json.JSONDecodeError:
        item["recommendations"] = []
    try:
        item["issues"] = json.loads(item.get("issues_json") or "[]")
    except json.JSONDecodeError:
        item["issues"] = []
    item["categories"] = {name: item.get(name, {}) for name in PRECHECK_CATEGORIES}
    item["approved"] = item.get("approved_at") is not None
    # Sanitize: remove internal technical fields from user-facing API response.
    # Full technical details remain stored in the database for administrators.
    item.pop("vision_model", None)
    item.pop("policy_version", None)
    item.pop("model_version", None)
    item.pop("video_sha256", None)
    item.pop("video_signature", None)
    item.pop("caption_checked", None)
    item.pop("error", None)  # detailed errors kept in DB; mapped to user message below
    item.pop("checked_scope", None)
    # Map internal error to a user-facing message if check failed
    if item.get("status") == "CHECK_FAILED":
        item["error"] = "Không thể hoàn tất kiểm tra"
    # Sanitize categories: remove raw provider labels/confidence from details
    for cat_name in item.get("categories", {}):
        cat = item["categories"][cat_name]
        if isinstance(cat, dict):
            cat.pop("details", None)
            cat.pop("raw_labels", None)
            cat.pop("model", None)
            cat.pop("provider", None)
            cat.pop("region", None)
    # Sanitize issues: keep only user-facing fields
    sanitized_issues = []
    for issue in item.get("issues", []):
        if isinstance(issue, dict):
            sanitized_issues.append({
                "policy_category": issue.get("policy_category", ""),
                "label": issue.get("label", issue.get("policy_category", "")),
                "action": issue.get("action", ""),
                "reason": issue.get("reason", ""),
                "remediation": issue.get("remediation", ""),
            })
    item["issues"] = sanitized_issues
    item["copyright_status"] = item.get("copyright_status", "")
    return item


def precheck_step(row, step: str, step_number: int) -> None:
    with db() as conn:
        conn.execute("""UPDATE video_prechecks SET progress=?, progress_step=?, updated_at=?
                        WHERE id=?""", (step, step_number, iso(), row["id"]))


def run_precheck_job(check_id: int) -> None:
    """Run the whole check in the background and store the result."""
    with db() as conn:
        row = conn.execute("SELECT * FROM video_prechecks WHERE id=?", (check_id,)).fetchone()
        if row is None:
            return
        record = dict(row)
    path = Path(record["media_path"])
    caption = record["caption_checked"]
    try:
        precheck_step(record, "Đang đọc thông số kỹ thuật", 1)
        known = []
        with db() as conn:
            known = media_fingerprint_rows(conn, record["pipeline_id"])
        report = run_precheck(path, caption, known, load_ai_config(), ReelPolicy())
    except Exception as error:
        with db() as conn:
            conn.execute("""UPDATE video_prechecks SET status=?, error=?, progress=?, updated_at=?
                            WHERE id=?""",
                         (FINAL_NEEDS_REVIEW, f"{type(error).__name__}: {str(error)[:300]}",
                          "Kiểm tra lỗi, cần xem lại", iso(), check_id))
        audit("precheck", check_id, "Kiểm tra video lỗi")
        return
    categories = report.get("categories", {})
    warnings: list[str] = []
    recommendations: list[str] = []
    for name in PRECHECK_CATEGORIES:
        category = categories.get(name, {})
        warnings.extend(f"{name}: {reason}" for reason in category.get("reasons", []))
        recommendations.extend(category.get("suggestions", []))
    try:
        issues = json.loads(json.dumps(report.get("issues", []), ensure_ascii=False))
    except (TypeError, ValueError):
        issues = []
    copyright_status = (
        report.get("categories", {}).get("copyright", {}).get("status", "")
    )
    with db() as conn:
        conn.execute("""UPDATE video_prechecks SET status=?, progress=?, progress_step=?, total_steps=?,
                        technical_result_json=?, visual_result_json=?, audio_result_json=?,
                        ocr_result_json=?, caption_result_json=?, copyright_result_json=?,
                        issues_json=?, warnings_json=?,
                        recommendations_json=?, score=?, model_version=?, vision_model=?,
                        policy_version=?, copyright_status=?, checked_scope=?, error='', checked_at=?, updated_at=?
                        WHERE id=?""",
                     (report["status"], "Đã hoàn tất", 8, 8,
                      json.dumps(categories.get("technical", {}), ensure_ascii=False),
                      json.dumps(categories.get("visual", {}), ensure_ascii=False),
                      json.dumps(categories.get("audio", {}), ensure_ascii=False),
                      json.dumps(categories.get("ocr", {}), ensure_ascii=False),
                      json.dumps(categories.get("caption", {}), ensure_ascii=False),
                      json.dumps(categories.get("copyright", {}), ensure_ascii=False),
                      json.dumps(issues, ensure_ascii=False),
                      json.dumps(warnings, ensure_ascii=False),
                      json.dumps(recommendations, ensure_ascii=False),
                      report.get("score", 0), report.get("model_version", ""),
                      report.get("vision_model", ""),
                      report.get("policy_version", ""),
                      copyright_status,
                      report.get("checked_scope", ""), iso(), iso(), check_id))
        try:
            remember_media(conn, record["pipeline_id"], path,
                           file_sha256(path) if path.is_file() else "", "")
        except OSError:
            pass
    audit("precheck", check_id, f"Kiểm tra video: {report['status']}", record["pipeline_id"])


def precheck_worker():
    """Run the queued pre-checks one at a time."""
    while not STOP_EVENT.is_set():
        try:
            with db() as conn:
                row = conn.execute("""SELECT id FROM video_prechecks WHERE status='queued'
                                      ORDER BY id LIMIT 1""").fetchone()
            if row is None:
                STOP_EVENT.wait(5)
                continue
            with db() as conn:
                conn.execute("UPDATE video_prechecks SET status='running', progress=?, updated_at=? WHERE id=?",
                             ("Đang kiểm tra", iso(), row["id"]))
            run_precheck_job(row["id"])
        except Exception as error:
            LOG.warning("Video pre-check worker error: %s", type(error).__name__)
            STOP_EVENT.wait(5)


def precheck_allows_publish(conn, pipeline_id: int, account_id: int, media_path: str,
                             caption: str, precheck_id: int | None) -> tuple[bool, str]:
    """The publish guard. A Reel may only go out with a valid pre-check."""
    if not precheck_id:
        return False, "Chưa kiểm tra video trước khi đăng"
    row = conn.execute("SELECT * FROM video_prechecks WHERE id=?", (precheck_id,)).fetchone()
    if row is None or row["pipeline_id"] != pipeline_id or row["account_id"] != account_id:
        return False, "Kết quả kiểm tra không thuộc Workspace hoặc tài khoản này"
    if row["status"] == FINAL_BLOCKED:
        return False, "Video bị chặn sau kiểm tra, cần sửa nội dung rồi kiểm tra lại"
    if row["status"] == FINAL_CHECK_FAILED:
        return False, "Kiểm tra video không thành công, cần chạy lại"
    if row["status"] == FINAL_NEEDS_REVIEW and not row["approved_at"]:
        return False, "Video cần người duyệt sau khi kiểm tra"
    if row["status"] not in (FINAL_PASS,) and not row["approved_at"]:
        return False, f"Video chưa đạt kiểm tra an toàn (trạng thái {row['status']})"
    path = Path(row["media_path"])
    if not path.is_file():
        return False, "Tệp video của lần kiểm tra không còn tồn tại"
    try:
        current = file_sha256(path)
    except OSError:
        return False, "Không đọc được tệp video để đối chiếu"
    if current != row["video_sha256"]:
        return False, "Video đã thay đổi sau khi kiểm tra, cần kiểm tra lại"
    if (caption or "").strip() != (row["caption_checked"] or "").strip():
        return False, "Caption đã thay đổi sau khi kiểm tra, cần kiểm tra lại"
    return True, ""


@app.post("/api/pipelines/{pipeline_id}/reels/precheck", status_code=202)
def create_precheck(pipeline_id: int, data: PrecheckRequest, principal: Principal = Depends(require_principal)):
    """Queue the pre-check of a video that is about to be published as a Reel."""
    require_permission(principal, "reels.create")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        account = scoped_entry(conn, "accounts", data.account_id, principal)
        if account["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        filename = Path(data.media_path).name
        path = UPLOADS / str(pipeline_id) / filename
        if not path.is_file():
            legacy = UPLOADS / filename
            path = legacy if legacy.is_file() else path
        if not path.is_file():
            raise HTTPException(422, "Không tìm thấy tệp video đã tải lên")
        sha = ""
        signature = ""
        try:
            sha = file_sha256(path)
            signature = perceptual_signature(path)
        except OSError:
            pass
        cursor = conn.execute("""INSERT INTO video_prechecks
            (pipeline_id,account_id,media_id,media_path,video_sha256,video_signature,caption_checked,
             status,progress,progress_step,total_steps,created_at,updated_at,owner_user_id)
            VALUES(?,?,?,?,?,?,?,'queued','Đang chờ kiểm tra',0,8,?,?,?)""",
            (pipeline_id, data.account_id, filename, str(path), sha, signature,
             (data.caption or "").strip(), iso(), iso(), resource_owner(principal)))
        check_id = cursor.lastrowid
    audit("precheck", check_id, "Xếp hàng kiểm tra video trước khi đăng", pipeline_id)
    return {"ok": True, "id": check_id, "status": "queued"}


@app.get("/api/pipelines/{pipeline_id}/reels/precheck/history")
def precheck_history(pipeline_id: int, limit: int = 30, principal: Principal = Depends(require_principal)):
    """The pre-checks of this workspace."""
    require_permission(principal, "history.view")
    scope_pipeline(principal, pipeline_id)
    limit = min(100, max(1, limit))
    with db() as conn:
        condition, values = owner_condition(principal, "v")
        rows = conn.execute(f"""SELECT v.* FROM video_prechecks v WHERE v.pipeline_id=?
                               AND {condition} ORDER BY v.id DESC LIMIT ?""",
                            (pipeline_id, *values, limit)).fetchall()
    return {"ok": True, "prechecks": [precheck_payload(row) for row in rows]}


@app.get("/api/pipelines/{pipeline_id}/reels/precheck/{check_id}")
def get_precheck(pipeline_id: int, check_id: int, principal: Principal = Depends(require_principal)):
    """The state and the report of one pre-check."""
    require_permission(principal, "history.view")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM video_prechecks WHERE id=?", (check_id,)).fetchone()
        if row is None or row["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        if not principal.is_admin and row["owner_user_id"] is not None \
                and row["owner_user_id"] != principal.user_id:
            raise HTTPException(404, "Not found")
    return {"ok": True, "precheck": precheck_payload(row)}


@app.post("/api/pipelines/{pipeline_id}/reels/precheck/{check_id}/retry")
def retry_precheck(pipeline_id: int, check_id: int, principal: Principal = Depends(require_principal)):
    """Run the check again."""
    require_permission(principal, "reels.create")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM video_prechecks WHERE id=?", (check_id,)).fetchone()
        if row is None or row["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        conn.execute("""UPDATE video_prechecks SET status='queued', progress='Đang chờ kiểm tra',
                        error='', approved_at=NULL, updated_at=? WHERE id=?""", (iso(), check_id))
    audit("precheck", check_id, "Kiểm tra lại video", pipeline_id)
    return {"ok": True, "id": check_id, "status": "queued"}


@app.post("/api/pipelines/{pipeline_id}/reels/precheck/{check_id}/approve")
def approve_precheck(pipeline_id: int, check_id: int, data: PrecheckApproval,
                     principal: Principal = Depends(require_admin)):
    """An administrator accepts a result that could not be cleared automatically."""
    with db() as conn:
        row = conn.execute("SELECT * FROM video_prechecks WHERE id=?", (check_id,)).fetchone()
        if row is None or row["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        if row["status"] == FINAL_BLOCKED:
            raise HTTPException(409, "Video bị chặn, phải sửa nội dung rồi kiểm tra lại")
        if row["status"] == FINAL_CHECK_FAILED:
            raise HTTPException(409, "Video lỗi kiểm tra, phải chạy lại rồi mới duyệt")
        conn.execute("""UPDATE video_prechecks SET approved_by=?, approved_at=?, approval_note=?,
                        updated_at=? WHERE id=?""",
                     (principal.user_id, iso(), (data.note or "")[:500], iso(), check_id))
        updated = conn.execute("SELECT * FROM video_prechecks WHERE id=?", (check_id,)).fetchone()
    audit("precheck", check_id, "Chủ hệ thống duyệt kết quả kiểm tra video", pipeline_id)
    return {"ok": True, "precheck": precheck_payload(updated)}



# --- media album -------------------------------------------------------------
# Cac endpoint cua Album nam trong ``api/media`` de ``app`` khong phai chua them
# phan xu ly. Worker va cac helper duoc dung boi phan con lai o lai day.


#: Một lần kiểm tra ảnh bị ngắt quá lâu được trả về hàng đợi.
MEDIA_ANALYSIS_TIMEOUT_MINUTES = 15


def media_context() -> media_service.MediaContext:
    """The infrastructure the album business needs."""
    return media_service.MediaContext(db=db, iso=iso, data_root=DATA,
                                      provider_config=load_ai_config)


def media_payload(row) -> dict:
    """The API shape of one album picture."""
    return media_service.media_payload(row)


def analyze_media_job(media_id: int) -> None:
    """Analyse one picture in the background."""
    media_service.run_analysis(media_context(), media_id)


def media_worker():
    """Analyse the uploaded pictures one at a time."""
    while not STOP_EVENT.is_set():
        try:
            with db() as conn:
                # Một lần kiểm tra bị ngắt giữa chừng được trả về hàng đợi.
                stale = (datetime.now(timezone.utc)
                         - timedelta(minutes=MEDIA_ANALYSIS_TIMEOUT_MINUTES)).isoformat()
                conn.execute("""UPDATE media_assets SET analysis_status=?
                                WHERE analysis_status=? AND updated_at < ?""",
                             (media_status.ANALYSIS_QUEUED, media_status.ANALYSIS_RUNNING, stale))
                row = conn.execute("""SELECT id FROM media_assets WHERE analysis_status=?
                                      ORDER BY id LIMIT 1""",
                                   (media_status.ANALYSIS_QUEUED,)).fetchone()
            if row is None:
                STOP_EVENT.wait(5)
                continue
            analyze_media_job(row["id"])
        except Exception as error:
            LOG.warning("Media analysis worker error: %s", type(error).__name__)
            STOP_EVENT.wait(5)


# --- automatic group posting -------------------------------------------------

def auto_post_config(conn, pipeline_id: int) -> dict:
    """The automatic posting configuration of a workspace."""
    row = conn.execute("SELECT * FROM auto_post_configs WHERE pipeline_id=?", (pipeline_id,)).fetchone()
    if row is None:
        return {"id": 0, "pipeline_id": pipeline_id, "account_id": None, "group_ids": [],
                "topic": "", "ai_caption": True, "daily_limit": 3, "require_approval": True,
                "enabled": False, "tone": ""}
    item = dict(row)
    try:
        item["group_ids"] = json.loads(item.get("group_ids_json") or "[]")
    except json.JSONDecodeError:
        item["group_ids"] = []
    item["ai_caption"] = bool(item.get("ai_caption"))
    item["require_approval"] = bool(item.get("require_approval"))
    item["enabled"] = bool(item.get("enabled"))
    return item


def workspace_groups(conn, pipeline_id: int) -> list[dict]:
    """The groups of a workspace with the state of the screening."""
    rows = conn.execute("""SELECT g.*, a.label account_label FROM groups g
        LEFT JOIN accounts a ON a.id=g.account_id WHERE g.pipeline_id=?""", (pipeline_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        assignment = conn.execute("""SELECT posting_mode, nickname FROM group_accounts
            WHERE group_id=? AND status='active' ORDER BY id LIMIT 1""", (row["id"],)).fetchone()
        item["posting_mode"] = assignment["posting_mode"] if assignment else row["posting_mode"]
        item["nickname"] = assignment["nickname"] if assignment else row["nickname"]
        result.append(item)
    return result


def album_candidates(conn, pipeline_id: int) -> list[dict]:
    """The pictures of the album that may be used."""
    rows = conn.execute("""SELECT id, description, tags_json, safety_status, analysis_status,
                                  usable, locked, saved, usage_count, last_used_at
                           FROM media_assets WHERE pipeline_id=?""", (pipeline_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.get("tags_json") or "[]")
        except json.JSONDecodeError:
            item["tags"] = []
        item["usable"] = bool(item["usable"])
        item["locked"] = bool(item["locked"])
        item["saved"] = bool(item["saved"])
        result.append(item)
    return result


def daily_posts_today(conn, account_id: int) -> int:
    """How many posts this account already made today."""
    return conn.execute("""SELECT COUNT(*) FROM jobs WHERE account_id=? AND state IN
        ('posted','submitted_unverified','pending_approval') AND date(updated_at)=date('now')""",
        (account_id,)).fetchone()[0]


def auto_post_settings_payload(conn, pipeline_id: int) -> dict:
    """The shared configuration of a workspace, with the values of the old table."""
    settings = posting_limits.settings_of(conn, pipeline_id)
    legacy = auto_post_config(conn, pipeline_id)
    settings["account_id"] = legacy.get("account_id")
    settings["tone"] = legacy.get("tone", "")
    return settings



def save_auto_post_settings(conn, pipeline_id: int, data: dict) -> dict:
    """Ghi cấu hình chung của Pipeline."""
    current = posting_limits.settings_of(conn, pipeline_id)
    merged = {**current, **{key: value for key, value in data.items() if value is not None}}
    conn.execute("""INSERT INTO pipeline_auto_post_settings
        (pipeline_id,enabled,scope,account_ids_json,group_ids_json,daily_limit_per_account,
         min_gap_minutes,pipeline_daily_limit,topic,ai_caption,anti_duplicate,require_approval,
         apply_to_new_accounts,timezone,max_pending_approvals,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pipeline_id) DO UPDATE SET
            enabled=excluded.enabled, scope=excluded.scope,
            account_ids_json=excluded.account_ids_json, group_ids_json=excluded.group_ids_json,
            daily_limit_per_account=excluded.daily_limit_per_account,
            min_gap_minutes=excluded.min_gap_minutes, pipeline_daily_limit=excluded.pipeline_daily_limit,
            topic=excluded.topic, ai_caption=excluded.ai_caption,
            anti_duplicate=excluded.anti_duplicate, require_approval=excluded.require_approval,
            apply_to_new_accounts=excluded.apply_to_new_accounts, timezone=excluded.timezone,
            max_pending_approvals=excluded.max_pending_approvals,
            updated_at=excluded.updated_at""",
                 (int(pipeline_id), int(bool(merged.get("enabled"))), merged.get("scope", "all"),
                  json.dumps([int(value) for value in merged.get("account_ids") or []]),
                  json.dumps([int(value) for value in merged.get("group_ids") or []]),
                  int(merged.get("daily_limit_per_account") or 3),
                  int(merged.get("min_gap_minutes") or 60),
                  int(merged.get("pipeline_daily_limit") or 0), merged.get("topic", ""),
                  int(bool(merged.get("ai_caption", True))),
                  int(bool(merged.get("anti_duplicate", True))),
                  int(bool(merged.get("require_approval", True))),
                  int(bool(merged.get("apply_to_new_accounts", True))),
                  merged.get("timezone") or "Asia/Ho_Chi_Minh",
                  int(max(0, int(merged.get("max_pending_approvals") or 0))),
                  iso(), iso()))
    return posting_limits.settings_of(conn, pipeline_id)


class AutoPostSettings(BaseModel):
    enabled: bool | None = None
    scope: str | None = None
    account_ids: list[int] | None = None
    group_ids: list[int] | None = None
    daily_limit_per_account: int | None = None
    min_gap_minutes: int | None = None
    pipeline_daily_limit: int | None = None
    topic: str | None = None
    ai_caption: bool | None = None
    anti_duplicate: bool | None = None
    require_approval: bool | None = None
    apply_to_new_accounts: bool | None = None
    timezone: str | None = None
    max_pending_approvals: int | None = None


class AccountLimit(BaseModel):
    daily_limit: int | None = None
    min_gap_minutes: int | None = None
    group_daily_limit: int | None = None
    paused: bool | None = None



class PostingSchedule(BaseModel):
    mode: str | None = None
    timezone: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    windows_json: str | None = None
    min_interval_minutes: int | None = None
    max_posts_per_account_per_day: int | None = None
    days_enabled_json: str | None = None
    group_cooldown_minutes: int | None = None
    paused: bool | None = None


@app.get("/api/pipelines/{pipeline_id}/schedule")
def get_posting_schedule(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """The schedule of the automatic posting of a workspace."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        schedule = job_allocator.schedule_of(conn, pipeline_id)
    return {"ok": True, "schedule": time_windows.describe(schedule)}


@app.put("/api/pipelines/{pipeline_id}/schedule")
def put_posting_schedule(pipeline_id: int, data: PostingSchedule,
                         principal: Principal = Depends(require_principal)):
    """Store the schedule of the automatic posting of a workspace."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    if data.mode not in (None, "spread", "windows"):
        raise HTTPException(422, "Chế độ lịch không hợp lệ")
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        payload = {key: value for key, value in data.model_dump().items() if value is not None}
        schedule = job_allocator.save_schedule(conn, pipeline_id, payload)
    audit("auto_post", None, "Cập nhật lịch đăng tự động", pipeline_id)
    return {"ok": True, "schedule": time_windows.describe(schedule)}


@app.get("/api/pipelines/{pipeline_id}/schedule/preview")
def get_schedule_preview(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """The schedule that each Facebook would follow."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        return job_allocator.preview(conn, pipeline_id)


@app.get("/api/pipelines/{pipeline_id}/schedule/history")
def get_schedule_history(pipeline_id: int, limit: int = 50,
                         principal: Principal = Depends(require_principal)):
    """What the scheduler did and why a job was postponed."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        rows = job_allocator.history_of(conn, pipeline_id, min(200, max(1, limit)))
    return {"ok": True, "history": rows}


@app.post("/api/pipelines/{pipeline_id}/schedule/allocate")
def post_schedule_allocate(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """Build the schedule of this workspace right now."""
    require_permission(principal, "auto_post.create")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        outcome = job_allocator.allocate(conn, pipeline_id, provider_config=load_ai_config(),
                                         max_jobs=20)
    audit("auto_post", None, f"Xếp lịch thông minh: {len(outcome['created'])} bài", pipeline_id)
    return {"ok": True, **outcome}



@app.get("/api/pipelines/{pipeline_id}/auto-post/settings")
def get_auto_post_settings(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """The shared configuration of the automatic posting of a workspace."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        settings = posting_limits.settings_of(conn, pipeline_id)
        settings["account_id"] = auto_post_config(conn, pipeline_id).get("account_id")
    return {"ok": True, "settings": settings}


@app.put("/api/pipelines/{pipeline_id}/auto-post/settings")
def put_auto_post_settings(pipeline_id: int, data: AutoPostSettings,
                            principal: Principal = Depends(require_principal)):
    """Store the shared configuration of the automatic posting of a workspace."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    if data.scope not in (None, "all", "selected"):
        raise HTTPException(422, "Phạm vi áp dụng không hợp lệ")
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        for account_id in data.account_ids or []:
            account = scoped_entry(conn, "accounts", account_id, principal)
            if account["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
        for group_id in data.group_ids or []:
            group = scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
        payload = {key: value for key, value in data.model_dump().items() if value is not None}
        # Group được chọn làm mục tiêu đăng thì được phép đặt làm đích. Việc
        # này không bỏ qua bất kỳ quy định nào của group.
        group_targets.sync_approved_targets(conn, pipeline_id, data.group_ids)
        # Đồng bộ group_accounts: liên kết các tài khoản được chọn với các group mục tiêu
        _sync_group_accounts(conn, pipeline_id, data.account_ids, data.group_ids)
        settings = save_auto_post_settings(conn, pipeline_id, payload)
    audit("auto_post", None, "Cập nhật cấu hình chung của tự động đăng group", pipeline_id)
    return {"ok": True, "settings": settings}


def _sync_group_accounts(conn, pipeline_id: int, account_ids: list[int] | None, group_ids: list[int] | None) -> None:
    """Tạo/ cập nhật group_accounts cho các tài khoản và group được chọn."""
    if not account_ids:
        return
    chosen_accounts = [int(a) for a in account_ids]
    if not chosen_accounts:
        return
    if group_ids:
        chosen_groups = [int(g) for g in group_ids]
    else:
        chosen_groups = [row[0] for row in conn.execute(
            "SELECT id FROM groups WHERE pipeline_id=? AND approved_target=1 AND enabled=1 AND status='active'",
            (int(pipeline_id),)
        ).fetchall()]
    if not chosen_groups:
        return
    placeholders_acc = ",".join("?" for _ in chosen_accounts)
    placeholders_grp = ",".join("?" for _ in chosen_groups)
    # Kiểm tra tài khoản và group hợp lệ
    valid_accounts = set(row[0] for row in conn.execute(
        f"SELECT id FROM accounts WHERE pipeline_id=? AND id IN ({placeholders_acc})",
        (int(pipeline_id), *chosen_accounts)
    ).fetchall())
    valid_groups = set(row[0] for row in conn.execute(
        f"SELECT id FROM groups WHERE pipeline_id=? AND id IN ({placeholders_grp}) AND approved_target=1 AND enabled=1 AND status='active'",
        (int(pipeline_id), *chosen_groups)
    ).fetchall())
    if not valid_accounts or not valid_groups:
        return
    stamp = datetime.now(timezone.utc).isoformat()
    for acc_id in valid_accounts:
        for grp_id in valid_groups:
            # Lấy posting_mode, nickname từ group
            row = conn.execute("SELECT posting_mode, nickname FROM groups WHERE id=?", (grp_id,)).fetchone()
            mode = (row["posting_mode"] or "anonymous") if row else "anonymous"
            nick = (row["nickname"] or "") if row else ""
            conn.execute("""INSERT INTO group_accounts(pipeline_id,group_id,account_id,posting_mode,nickname,status,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(account_id,group_id) DO UPDATE SET
                                posting_mode=excluded.posting_mode,
                                nickname=excluded.nickname,
                                status=excluded.status,
                                updated_at=excluded.updated_at""",
                         (int(pipeline_id), grp_id, acc_id, mode, nick, "active", stamp, stamp))


@app.get("/api/pipelines/{pipeline_id}/auto-post/limits")
def get_auto_post_limits(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """The limit of every Facebook account of the workspace."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        rows = conn.execute("""SELECT a.id, a.label, a.enabled, a.session_status FROM accounts a
                               WHERE a.pipeline_id=? ORDER BY a.id""", (pipeline_id,)).fetchall()
        items = []
        for row in rows:
            limits = posting_limits.limits_of(conn, row["id"])
            items.append({"account_id": row["id"], "label": row["label"],
                          "enabled": bool(row["enabled"]),
                          "session_status": row["session_status"], **limits})
    return {"ok": True, "limits": items}


@app.put("/api/pipelines/{pipeline_id}/auto-post/limits/{account_id}")
def put_auto_post_limit(pipeline_id: int, account_id: int, data: AccountLimit,
                        principal: Principal = Depends(require_principal)):
    """Store the limit of one Facebook account."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        account = scoped_entry(conn, "accounts", account_id, principal)
        if account["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        current = posting_limits.limits_of(conn, account_id)
        daily = data.daily_limit if data.daily_limit is not None else current["daily_limit"]
        gap = data.min_gap_minutes if data.min_gap_minutes is not None else current["min_gap_minutes"]
        group_limit = (data.group_daily_limit if data.group_daily_limit is not None
                       else current["group_daily_limit"])
        paused = data.paused if data.paused is not None else current["paused"]
        conn.execute("""INSERT INTO account_posting_limits
            (account_id,daily_limit,min_gap_minutes,group_daily_limit,paused,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET daily_limit=excluded.daily_limit,
                min_gap_minutes=excluded.min_gap_minutes, group_daily_limit=excluded.group_daily_limit,
                paused=excluded.paused, updated_at=excluded.updated_at""",
                     (account_id, daily, gap, group_limit, int(bool(paused)), iso()))
        limits = posting_limits.limits_of(conn, account_id)
    audit("auto_post", None, f"Cập nhật hạn mức đăng của tài khoản {account_id}", pipeline_id)
    return {"ok": True, "limits": limits}


@app.get("/api/pipelines/{pipeline_id}/auto-post/dashboard")
def get_auto_post_dashboard(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """The compact state of the automatic posting of a workspace."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        settings = posting_limits.settings_of(conn, pipeline_id)
        moment = posting_limits.now_in(settings.get("timezone"))
        day = posting_limits.day_of(moment)
        all_accounts = conn.execute("""SELECT a.* FROM accounts a WHERE a.pipeline_id=?
                                       ORDER BY a.id""", (int(pipeline_id),)).fetchall()
        groups = post_scheduler._group_rows(conn, pipeline_id, settings)
        eligible = eligible_groups(groups, first_post=True, conn=conn, pipeline_id=pipeline_id)
        decisions = post_scheduler.pair_decisions(conn, pipeline_id, groups)
        blockers = post_scheduler.group_blockers(groups, decisions)
        items = []
        active_count = 0
        chosen_accounts = settings.get("account_ids") or []
        for account in all_accounts:
            acc_dict = dict(account)
            state = posting_limits.account_state(conn, acc_dict["id"], settings, moment)
            own = [group for group in eligible if acc_dict["id"] in group.get("eligible_account_ids", [])]
            in_scope = (settings.get("scope") != "selected") or (not chosen_accounts) or (acc_dict["id"] in chosen_accounts)
            is_active = bool(acc_dict.get("enabled")) and in_scope
            if is_active:
                active_count += 1
            items.append({"account_id": acc_dict["id"], "label": acc_dict["label"],
                          "session_status": acc_dict.get("session_status"), "enabled": bool(acc_dict.get("enabled")),
                          **state, "eligible_groups": len(own)})
        from services.auto_job_history import confirmed_recent_count
        published_today = confirmed_recent_count(conn, pipeline_id)
        waiting = conn.execute("""SELECT COUNT(*) FROM auto_post_jobs
                                   WHERE pipeline_id=? AND status IN ('pending','approved')""",
                                (pipeline_id,)).fetchone()[0]
        blocked = conn.execute("""SELECT COUNT(*) FROM auto_post_jobs
                                   WHERE pipeline_id=? AND status='blocked'
                                     AND datetime(created_at) >= datetime('now', '-24 hours')""",
                                (pipeline_id,)).fetchone()[0]
    return {"ok": True, "settings": settings, "accounts": items, "blockers": blockers,
            "totals": {"accounts": active_count, "at_limit": sum(1 for item in items if item["at_limit"]),
                       "eligible_groups": len(eligible), "total_groups": len(groups),
                       "published_today": published_today, "waiting": waiting,
                       "blocked_duplicate": blocked}}


@app.post("/api/pipelines/{pipeline_id}/auto-post/distribute")
def distribute_auto_post(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """Chia công việc cho các tài khoản Facebook theo hạn mức."""
    require_permission(principal, "auto_post.create")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        outcome = post_scheduler.distribute(conn, pipeline_id, provider_config=load_ai_config())
    audit("auto_post", None, f"Xếp lịch tự động: tạo {len(outcome['created'])} bài", pipeline_id)
    return {"ok": True, **outcome}


@app.post("/api/pipelines/{pipeline_id}/auto-post/config")
def set_auto_post_config(pipeline_id: int, data: AutoPostConfig,
                         principal: Principal = Depends(require_principal)):
    """Store the automatic posting configuration of a workspace."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        if data.account_id is not None:
            account = scoped_entry(conn, "accounts", data.account_id, principal)
            if account["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
        for group_id in data.group_ids:
            group = scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
        conn.execute("""INSERT INTO auto_post_configs
            (pipeline_id,account_id,group_ids_json,topic,ai_caption,daily_limit,require_approval,
             enabled,tone,updated_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(pipeline_id) DO UPDATE SET
                account_id=excluded.account_id, group_ids_json=excluded.group_ids_json,
                topic=excluded.topic, ai_caption=excluded.ai_caption, daily_limit=excluded.daily_limit,
                require_approval=excluded.require_approval, enabled=excluded.enabled,
                tone=excluded.tone, updated_at=excluded.updated_at""",
            (pipeline_id, data.account_id, json.dumps(data.group_ids), data.topic,
             int(data.ai_caption), data.daily_limit, int(data.require_approval),
             int(data.enabled), data.tone, iso(), iso()))
        # Cấu hình chung của Pipeline cũng được cập nhật.
        account_ids = [int(data.account_id)] if data.account_id else []
        save_auto_post_settings(conn, pipeline_id, {
            "enabled": bool(data.enabled),
            "scope": "selected" if account_ids else "all",
            "account_ids": account_ids,
            "group_ids": [int(group_id) for group_id in data.group_ids],
            "daily_limit_per_account": int(data.daily_limit),
            "topic": data.topic,
            "ai_caption": bool(data.ai_caption),
            "require_approval": bool(data.require_approval),
        })
        config = auto_post_settings_payload(conn, pipeline_id)
    audit("auto_post", None, "Cập nhật cấu hình tự động đăng group", pipeline_id)
    return {"ok": True, "config": config}


@app.get("/api/pipelines/{pipeline_id}/auto-post/config")
def get_auto_post_config(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """The automatic posting configuration of a workspace."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        return {"ok": True, "config": auto_post_settings_payload(conn, pipeline_id)}


@app.post("/api/pipelines/{pipeline_id}/auto-post/preview")
def preview_auto_post(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """Show what the automatic posting would prepare right now."""
    require_permission(principal, "auto_post.create")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        settings = posting_limits.settings_of(conn, pipeline_id)
        outcome = post_scheduler.plan(conn, pipeline_id, provider_config=load_ai_config())
        groups = post_scheduler._group_rows(conn, pipeline_id, settings)
    jobs = outcome.get("jobs", [])
    first = jobs[0] if jobs else {}
    return {"ok": True, "preview": {
        "config": settings,
        "eligible_groups": outcome.get("eligible_groups", []),
        "skipped_groups": [g["id"] for g in groups if g["id"] not in outcome.get("eligible_groups", [])],
        "jobs": jobs,
        "notes": outcome.get("notes", []),
        "blockers": post_scheduler.group_blockers(groups),
        # các trường cũ vẫn còn để giao diện cũ dùng được
        "media_id": first.get("media_id"),
        "media_name": first.get("media_description", ""),
        "caption": first.get("caption", ""),
        "caption_status": first.get("caption_status", "unknown"),
        "posts": [{"group_id": job["group_id"], "group_name": job.get("group_label"),
                   "account_id": job["account_id"], "account_label": job.get("account_label"),
                   "posting_mode": job.get("posting_mode"), "nickname": job.get("nickname", ""),
                   "skipped": ""} for job in jobs],
    }}


@app.post("/api/pipelines/{pipeline_id}/auto-post/schedule", status_code=201)
def schedule_auto_post(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """Prepare the automatic posts of this workspace."""
    require_permission(principal, "auto_post.create")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        settings = posting_limits.settings_of(conn, pipeline_id)
        if not settings["enabled"]:
            raise HTTPException(409, "Tự động đăng group đang tắt")
        outcome = post_scheduler.distribute(conn, pipeline_id, provider_config=load_ai_config())
    if not outcome["created"] and outcome.get("no_media"):
        # Nói rõ vì sao không có bài nào, thay vì chỉ trả về con số không.
        reasons = [str(note) for note in (outcome.get("notes") or [])]
        reasons += [f"{item.get('label')}: {item.get('reason')}"
                    for item in (outcome.get("blockers") or [])]
        detail = "; ".join(dict.fromkeys(reasons))[:300]
        raise HTTPException(409, detail or "Album chưa có ảnh đủ điều kiện để đăng tự động")
    audit("auto_post", None, f"Chuẩn bị {len(outcome['created'])} bài tự động", pipeline_id)
    return {"ok": True, **outcome}


@app.post("/api/pipelines/{pipeline_id}/auto-post/{job_id}/approve")
def approve_auto_post(pipeline_id: int, job_id: int, principal: Principal = Depends(require_principal)):
    """An administrator or a collaborator with the right permission accepts a prepared post."""
    require_permission(principal, "auto_post.approve")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM auto_post_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        if row["status"] not in ("pending", "approved"):
            raise HTTPException(409, "Bài này không còn chờ duyệt")
        conn.execute("""UPDATE auto_post_jobs SET status='approved', approved_by=?, approved_at=?,
                        updated_at=? WHERE id=?""", (principal.user_id, iso(), iso(), job_id))
        updated = conn.execute("SELECT * FROM auto_post_jobs WHERE id=?", (job_id,)).fetchone()
    audit("auto_post", job_id, "Duyệt bài tự động", pipeline_id)
    return {"ok": True, "job": dict(updated)}


@app.get("/api/pipelines/{pipeline_id}/auto-post/history")
def auto_post_history(pipeline_id: int, status: str | None = None, limit: int = 200, principal: Principal = Depends(require_principal)):
    """The prepared and published automatic posts of this workspace."""
    require_permission(principal, "history.view")
    scope_pipeline(principal, pipeline_id)
    from services.auto_job_history import read_history
    with db() as conn:
        return read_history(conn, pipeline_id, status, limit)


def publish_auto_post_jobs() -> None:
    """Turn the approved automatic posts into real posting jobs."""
    with db() as conn:
        # Chỉ đăng khi đã tới giờ của lịch.
        rows = conn.execute("""SELECT * FROM auto_post_jobs
                              WHERE status='approved'
                                AND (scheduled_for IS NULL OR scheduled_for <= ?)
                              ORDER BY id LIMIT 5""", (iso(),)).fetchall()
    for row in rows:
        job = dict(row)
        try:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT status FROM auto_post_jobs WHERE id=? AND pipeline_id=?",
                                       (job["id"], job["pipeline_id"])).fetchone()
                if current is None or current["status"] != "approved":
                    continue
                if cycle_runtime.promote(conn,job,DATA,now(),album_bytes):
                    continue
                media = conn.execute("SELECT * FROM media_assets WHERE id=? AND pipeline_id=?",
                                     (job["media_id"], job["pipeline_id"])).fetchone()
                if media is None:
                    raise ValueError("Ảnh không còn tồn tại")
                if media["locked"] or not media["usable"]:
                    raise ValueError("Ảnh đã bị khóa hoặc không được phép sử dụng")
                if media["safety_status"] == "blocked":
                    raise ValueError("Ảnh bị chặn vì nội dung rủi ro")
                group = conn.execute("SELECT * FROM groups WHERE id=? AND pipeline_id=?",
                                     (job["group_id"], job["pipeline_id"])).fetchone()
                if group is None:
                    raise ValueError("Group không còn tồn tại")
                decision = group_eligibility.pair_decision(conn, job["pipeline_id"],
                                                            job["account_id"], job["group_id"], group)
                if not decision["eligible"]:
                    raise ValueError(decision["reason"] or "Group chưa được xác minh để đăng tự động")
                if group["status"] != "active" or not group["enabled"]:
                    raise ValueError("Group đang tạm dừng")
                if not group["approved_target"]:
                    raise ValueError("Chưa xác nhận quyền đăng của group")
                account = conn.execute("SELECT * FROM accounts WHERE id=? AND pipeline_id=?",
                                       (job["account_id"], job["pipeline_id"])).fetchone()
                if account is None or not account["enabled"]:
                    raise ValueError("Tài khoản Facebook không dùng được")
                if account["pipeline_id"] != group["pipeline_id"]:
                    raise ValueError("Tài khoản và group không cùng Workspace")
                settings = posting_limits.settings_of(conn, job["pipeline_id"])
                ctv_id = job.get("created_by_ctv_id")
                ctv_settings = None
                if ctv_id:
                    ctv_settings = ctv_auto_post.settings_of(conn, job["pipeline_id"], ctv_id)
                    if ctv_settings:
                        rules = ctv_settings.get("schedule_rules") or {}
                        settings = {**settings,
                                    "min_gap_minutes": max(
                                        int(settings.get("min_gap_minutes") or 0),
                                        int(rules.get("min_gap_minutes") or 0))}
                state = posting_limits.account_state(conn, account["id"], settings)
                if state["at_limit"]:
                    raise ValueError(f"Đã đạt giới hạn {state['daily_limit']} bài/ngày của tài khoản")
                if state["next_allowed_at"]:
                    raise ValueError("Chưa đủ khoảng cách tối thiểu giữa hai bài")
                if ctv_id and ctv_settings:
                    own_limit = int(ctv_settings.get("max_posts_per_account_daily") or 0)
                    day = posting_limits.day_of(posting_limits.now_in(
                        ctv_settings.get("timezone")))
                    if own_limit and ctv_auto_post.usage_of(
                            conn, job["pipeline_id"], ctv_id, account["id"], day) > own_limit:
                        raise ValueError(f"CTV đã đủ {own_limit} bài/ngày trên tài khoản này")
                if posting_limits.is_paused(conn, account["id"]):
                    raise ValueError("Tài khoản Facebook đang tạm dừng")
                duplicate = conn.execute("""SELECT COUNT(*) FROM jobs WHERE group_id=? AND account_id=?
                                           AND state IN ('queued','running')""",
                                         (group["id"], account["id"])).fetchone()[0]
                if duplicate:
                    raise ValueError("Đã có bài đang chờ cho group này")
                media_path = f"uploads/{job['pipeline_id']}/{Path(media['storage_key']).name}"
                # The posting worker reads the media from the upload folder, so
                # the picture of the album is copied there for this post.
                target = UPLOADS / str(job["pipeline_id"]) / Path(media["storage_key"]).name
                target.parent.mkdir(parents=True, exist_ok=True)
                if media_cloud.enabled():
                    payload = album_bytes(conn, media)
                    if payload is None:
                        raise ValueError("Tệp ảnh không còn tồn tại trên R2")
                    target.write_bytes(payload)
                else:
                    source = safe_join(DATA, media["storage_key"])
                    if not source.is_file():
                        raise ValueError("Tệp ảnh không còn tồn tại")
                    shutil.copyfile(source, target)
                target.chmod(0o600)
                caption = job["caption"]
                analysis = analyze_caption(caption)
                if any(issue.severity == "high" for issue in analysis.issues):
                    raise ValueError("Caption vi phạm chính sách")
                post = conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,
                                     anonymous_only,owner_user_id)
                    VALUES(?,?,?,?,1,?)""",
                    (job["pipeline_id"], caption, json.dumps([media_path]), iso(), ctv_id))
                post_id = post.lastrowid
                execution = conn.execute("""INSERT INTO jobs
                    (post_id,group_id,account_id,pipeline_id,state,details,scheduled_at,updated_at,
                     requested_posting_mode,requested_nickname,posting_status,verification_status,
                     created_by_ctv_id)
                    VALUES(?,?,?,?,'queued','',?,?,?,?,'pending','unverified',?)""",
                    (post_id, group["id"], account["id"], job["pipeline_id"], iso(), iso(),
                     job["posting_mode"], job["nickname"], ctv_id))
                analytics_worker.sync_attribution(conn, "group_post", execution.lastrowid,
                    created_by=ctv_id, approved_by=job.get("approved_by"), pipeline_id=job["pipeline_id"])
                conn.execute("""UPDATE auto_post_jobs SET status='queued', post_id=?, updated_at=? WHERE id=?""",
                             (post_id, iso(), job["id"]))
                conn.execute("""INSERT INTO media_usage_history
                    (pipeline_id,media_id,account_id,group_id,job_id,used_at,note)
                    VALUES(?,?,?,?,?,?,?)""",
                    (job["pipeline_id"], job["media_id"], account["id"], group["id"], job["id"], iso(),
                     "đăng tự động"))
                conn.execute("""UPDATE media_assets SET usage_count=usage_count+1, last_used_at=?,
                                updated_at=? WHERE id=?""", (iso(), iso(), job["media_id"]))
        except Exception as error:
            with db() as conn:
                conn.execute("""UPDATE auto_post_jobs SET status='failed', error=?, updated_at=?
                                WHERE id=?""", (str(error)[:400], iso(), job["id"]))


def auto_post_worker():
    """Publish the approved automatic posts and prepare new ones.

    Việc chuẩn bị chạy trong một giao dịch và có khóa idempotent, nên hai
    worker không thể tạo ra hai job giống nhau.
    """
    while not STOP_EVENT.is_set():
        try:
            publish_auto_post_jobs()
            prepare_auto_post_jobs()
            STOP_EVENT.wait(20)
        except Exception as error:
            LOG.warning("Auto posting worker error: %s", type(error).__name__)
            STOP_EVENT.wait(10)


def prepare_auto_post_jobs() -> None:
    """Chia công việc cho các tài khoản Facebook của các Workspace đang bật.

    Một Workspace có một CTV, nên chỉ một bộ cấu hình được dùng cho mỗi
    Workspace: nếu CTV đã tự cấu hình thì bộ xếp lịch của CTV chạy, bộ xếp lịch
    chung bỏ qua Workspace đó. Nhờ vậy một group không bị xếp lịch hai lần.
    """
    cycle_refresh.enqueue_due(db,now(),enqueue_screening)
    cycle_pipelines = cycle_engine.tick(db,now(),provider_config=load_ai_config())
    with db() as conn:
        ctv_pipelines = {int(row["pipeline_id"]) for row in conn.execute(
            """SELECT DISTINCT pipeline_id FROM ctv_auto_post_settings WHERE enabled=1""")}
    with db() as conn:
        pipelines = conn.execute("""SELECT pipeline_id FROM pipeline_auto_post_settings
                                   WHERE enabled=1""").fetchall()
    provider_config = load_ai_config()
    for row in pipelines:
        if int(row["pipeline_id"]) in ctv_pipelines | cycle_pipelines:
            continue
        with db() as conn:
            try:
                schedule = job_allocator.schedule_of(conn, row["pipeline_id"])
                if schedule.get("paused"):
                    continue
                outcome = job_allocator.allocate(conn, row["pipeline_id"],
                                                 provider_config=provider_config, max_jobs=10)
                if outcome.get("created"):
                    LOG.info("Pipeline %s: xếp lịch %d bài",
                             row["pipeline_id"], len(outcome["created"]))
            except Exception as error:
                LOG.warning("Không xếp lịch được pipeline %s: %s", row["pipeline_id"],
                            type(error).__name__)
    # Cấu hình riêng của CTV, không phụ thuộc cấu hình chung của quản trị.
    with db() as conn:
        ctv_pipelines = conn.execute("""SELECT DISTINCT pipeline_id FROM ctv_auto_post_settings
                                        WHERE enabled=1""").fetchall()
    for row in ctv_pipelines:
        if int(row["pipeline_id"]) in cycle_pipelines:
            continue
        with db() as conn:
            try:
                outcome = ctv_allocator.allocate(conn, row["pipeline_id"],
                                                 provider_config=provider_config, max_jobs=20)
                if outcome.get("created"):
                    LOG.info("Pipeline %s: xếp lịch %d bài cho CTV",
                             row["pipeline_id"], len(outcome["created"]))
            except Exception as error:
                LOG.warning("Không xếp lịch được CTV của pipeline %s: %s", row["pipeline_id"],
                            type(error).__name__)


# =============================================================================
# AUTO COMMENT API
# =============================================================================

@app.get("/api/pipelines/{pipeline_id}/auto-comment/settings")
def get_auto_comment_settings(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """Get auto comment settings for a pipeline."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM auto_comment_settings WHERE pipeline_id = ?",
            (pipeline_id,)
        ).fetchone()
        if not row:
            return {"ok": True, "settings": None}
        import json
        d = dict(row)
        d["active_days"] = json.loads(d.get("active_days_json") or "[0,1,2,3,4,5,6]")
        d["group_account_ids"] = json.loads(d.get("group_account_ids_json") or "[]")
        d["feed_account_ids"] = json.loads(d.get("feed_account_ids_json") or "[]")
        return {"ok": True, "settings": d}


@app.post("/api/pipelines/{pipeline_id}/auto-comment/settings")
def save_auto_comment_settings(pipeline_id: int, data: dict, principal: Principal = Depends(require_principal)):
    """Create or update auto comment settings for a pipeline."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    import json
    from app import iso
    
    allowed = {
        "mode_group_enabled", "mode_feed_enabled", "start_hour", "end_hour",
        "active_days", "timezone", "max_comments_per_account_per_day",
        "min_interval_seconds", "max_comments_per_group_per_day",
        "max_total_comments_per_day", "status", "group_account_ids",
        "feed_account_ids", "brand_guidelines", "group_default_rules",
    }
    settings = {k: v for k, v in data.items() if k in allowed}
    
    # Convert arrays to JSON
    if "active_days" in settings:
        settings["active_days_json"] = json.dumps(settings.pop("active_days"))
    if "group_account_ids" in settings:
        settings["group_account_ids_json"] = json.dumps(settings.pop("group_account_ids"))
    if "feed_account_ids" in settings:
        settings["feed_account_ids_json"] = json.dumps(settings.pop("feed_account_ids"))
    
    with db() as conn:
        row = conn.execute(
            "SELECT pipeline_id FROM auto_comment_settings WHERE pipeline_id = ?",
            (pipeline_id,)
        ).fetchone()
        
        if row:
            set_clause = ", ".join(f"{k} = ?" for k in settings)
            values = list(settings.values()) + [iso(), pipeline_id]
            conn.execute(
                f"UPDATE auto_comment_settings SET {set_clause}, updated_at = ? WHERE pipeline_id = ?",
                values
            )
        else:
            cols = ["pipeline_id"] + list(settings.keys()) + ["created_at", "updated_at"]
            placeholders = ", ".join(["?"] * len(cols))
            values = [pipeline_id] + list(settings.values()) + [iso(), iso()]
            conn.execute(
                f"INSERT INTO auto_comment_settings ({', '.join(cols)}) VALUES ({placeholders})",
                values
            )
    
    audit("auto_comment", None, "Cập nhật cấu hình bình luận tự động", pipeline_id)
    return {"ok": True}


# --- Group Sources ---

@app.get("/api/pipelines/{pipeline_id}/auto-comment/group-sources")
def list_group_sources(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """List group sources for auto comment."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM auto_comment_group_sources WHERE pipeline_id = ? AND is_active = 1 ORDER BY id",
            (pipeline_id,)
        ).fetchall()
    return {"ok": True, "sources": [dict(r) for r in rows]}


@app.post("/api/pipelines/{pipeline_id}/auto-comment/group-sources")
def add_group_source(pipeline_id: int, data: dict, principal: Principal = Depends(require_principal)):
    """Add a group source for auto comment."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    from app import iso
    
    fb_group_id = data.get("fb_group_id", "").strip()
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    allow_auto_comment = bool(data.get("allow_auto_comment", True))
    
    if not fb_group_id:
        raise HTTPException(400, "Thiếu fb_group_id")
    
    with db() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO auto_comment_group_sources
                   (pipeline_id, fb_group_id, name, url, is_active, allow_auto_comment, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                (pipeline_id, fb_group_id, name, url, allow_auto_comment, iso(), iso())
            )
            source_id = cur.lastrowid
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                raise HTTPException(409, "Group này đã được thêm")
            raise
    
    audit("auto_comment", source_id, "Thêm nguồn group bình luận", pipeline_id)
    return {"ok": True, "id": source_id}


@app.patch("/api/pipelines/{pipeline_id}/auto-comment/group-sources/{source_id}")
def update_group_source(pipeline_id: int, source_id: int, data: dict, principal: Principal = Depends(require_principal)):
    """Update a group source."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    from app import iso
    
    allowed = {"name", "url", "allow_auto_comment", "is_active"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "Không có trường hợp lệ để cập nhật")
    
    fields["updated_at"] = iso()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [source_id, pipeline_id]
    
    with db() as conn:
        conn.execute(
            f"UPDATE auto_comment_group_sources SET {set_clause} WHERE id = ? AND pipeline_id = ?",
            values
        )
    
    audit("auto_comment", source_id, "Cập nhật nguồn group bình luận", pipeline_id)
    return {"ok": True}


@app.delete("/api/pipelines/{pipeline_id}/auto-comment/group-sources/{source_id}")
def delete_group_source(pipeline_id: int, source_id: int, principal: Principal = Depends(require_principal)):
    """Delete a group source (soft delete)."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    from app import iso
    
    with db() as conn:
        conn.execute(
            "UPDATE auto_comment_group_sources SET is_active = 0, updated_at = ? WHERE id = ? AND pipeline_id = ?",
            (iso(), source_id, pipeline_id)
        )
    
    audit("auto_comment", source_id, "Xóa nguồn group bình luận", pipeline_id)
    return {"ok": True}


# --- Feed Sources ---

@app.get("/api/pipelines/{pipeline_id}/auto-comment/feed-sources")
def list_feed_sources(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """List feed sources for auto comment."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    import json
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM auto_comment_feed_sources WHERE pipeline_id = ? AND is_active = 1 ORDER BY id",
            (pipeline_id,)
        ).fetchall()
    sources = []
    for row in rows:
        d = dict(row)
        for key in ["topics_json", "excluded_keywords_json", "excluded_authors_json", "allowed_domains_json"]:
            d[key.replace("_json", "")] = json.loads(d.get(key) or "[]")
            del d[key]
        sources.append(d)
    return {"ok": True, "sources": sources}


@app.post("/api/pipelines/{pipeline_id}/auto-comment/feed-sources")
def add_feed_source(pipeline_id: int, data: dict, principal: Principal = Depends(require_principal)):
    """Add a feed source for auto comment."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    import json
    from app import iso
    
    name = data.get("name", "").strip()
    source_type = data.get("source_type", "keyword")
    target_id = data.get("target_id", "").strip()
    topics = data.get("topics", [])
    excluded_keywords = data.get("excluded_keywords", [])
    excluded_authors = data.get("excluded_authors", [])
    allowed_domains = data.get("allowed_domains", [])
    
    if not name or not target_id:
        raise HTTPException(400, "Thiếu name hoặc target_id")
    
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO auto_comment_feed_sources
               (pipeline_id, name, source_type, target_id, topics_json, excluded_keywords_json,
                excluded_authors_json, allowed_domains_json, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (pipeline_id, name, source_type, target_id,
             json.dumps(topics), json.dumps(excluded_keywords),
             json.dumps(excluded_authors), json.dumps(allowed_domains),
             iso(), iso())
        )
        source_id = cur.lastrowid
    
    audit("auto_comment", source_id, "Thêm nguồn feed bình luận", pipeline_id)
    return {"ok": True, "id": source_id}


@app.patch("/api/pipelines/{pipeline_id}/auto-comment/feed-sources/{source_id}")
def update_feed_source(pipeline_id: int, source_id: int, data: dict, principal: Principal = Depends(require_principal)):
    """Update a feed source."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    import json
    from app import iso
    
    allowed = {"name", "source_type", "target_id", "topics", "excluded_keywords", 
               "excluded_authors", "allowed_domains", "is_active"}
    fields = {}
    for k, v in data.items():
        if k in allowed:
            if k in ["topics", "excluded_keywords", "excluded_authors", "allowed_domains"] and isinstance(v, list):
                fields[k + "_json"] = json.dumps(v)
            else:
                fields[k] = v
    
    if not fields:
        raise HTTPException(400, "Không có trường hợp lệ để cập nhật")
    
    fields["updated_at"] = iso()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [source_id, pipeline_id]
    
    with db() as conn:
        conn.execute(
            f"UPDATE auto_comment_feed_sources SET {set_clause} WHERE id = ? AND pipeline_id = ?",
            values
        )
    
    audit("auto_comment", source_id, "Cập nhật nguồn feed bình luận", pipeline_id)
    return {"ok": True}


@app.delete("/api/pipelines/{pipeline_id}/auto-comment/feed-sources/{source_id}")
def delete_feed_source(pipeline_id: int, source_id: int, principal: Principal = Depends(require_principal)):
    """Delete a feed source (soft delete)."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    from app import iso
    
    with db() as conn:
        conn.execute(
            "UPDATE auto_comment_feed_sources SET is_active = 0, updated_at = ? WHERE id = ? AND pipeline_id = ?",
            (iso(), source_id, pipeline_id)
        )
    
    audit("auto_comment", source_id, "Xóa nguồn feed bình luận", pipeline_id)
    return {"ok": True}


# --- Auto Comment Jobs ---

@app.get("/api/pipelines/{pipeline_id}/auto-comment/jobs")
def list_auto_comment_jobs(pipeline_id: int, limit: int = 50, status: str = "", principal: Principal = Depends(require_principal)):
    """List auto comment jobs."""
    require_permission(principal, "history.view")
    scope_pipeline(principal, pipeline_id)
    limit = min(200, max(1, limit))
    
    with db() as conn:
        query = "SELECT * FROM auto_comment_jobs WHERE pipeline_id = ?"
        params = [pipeline_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    jobs = []
    for row in rows:
        job = dict(row)
        job.pop("fingerprint_id", None)
        job.pop("idempotency_key", None)
        job.pop("fb_comment_id", None)
        jobs.append(job)
    return {"ok": True, "jobs": jobs}


@app.post("/api/pipelines/{pipeline_id}/auto-comment/jobs/{job_id}/approve")
def approve_auto_comment_job(pipeline_id: int, job_id: int, principal: Principal = Depends(require_principal)):
    """Approve a comment job for posting."""
    require_permission(principal, "auto_post.approve")
    scope_pipeline(principal, pipeline_id)
    from app import iso
    
    with db() as conn:
        row = conn.execute("SELECT * FROM auto_comment_jobs WHERE id = ? AND pipeline_id = ?", (job_id, pipeline_id)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        if row["status"] not in ("pending", "scheduled", "submitted_unverified"):
            raise HTTPException(409, "Job này không thể duyệt")
        
        conn.execute(
            "UPDATE auto_comment_jobs SET status = 'scheduled', updated_at = ? WHERE id = ?",
            (iso(), job_id)
        )
    
    audit("auto_comment", job_id, "Duyệt bình luận tự động", pipeline_id)
    return {"ok": True}


@app.post("/api/pipelines/{pipeline_id}/auto-comment/jobs/{job_id}/cancel")
def cancel_auto_comment_job(pipeline_id: int, job_id: int, principal: Principal = Depends(require_principal)):
    """Cancel a comment job."""
    require_permission(principal, "auto_post.configure")
    scope_pipeline(principal, pipeline_id)
    from app import iso
    
    with db() as conn:
        row = conn.execute("SELECT * FROM auto_comment_jobs WHERE id = ? AND pipeline_id = ?", (job_id, pipeline_id)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        if row["status"] in ("posted", "failed", "cancelled"):
            raise HTTPException(409, "Job không thể hủy")
        
        conn.execute(
            "UPDATE auto_comment_jobs SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (iso(), job_id)
        )
    
    audit("auto_comment", job_id, "Hủy bình luận tự động", pipeline_id)
    return {"ok": True}


@app.get("/api/pipelines/{pipeline_id}/auto-comment/history")
def auto_comment_history(pipeline_id: int, limit: int = 50, principal: Principal = Depends(require_principal)):
    """Get auto comment activity history."""
    require_permission(principal, "history.view")
    scope_pipeline(principal, pipeline_id)
    limit = min(200, max(1, limit))
    with db() as conn:
        rows = conn.execute("""SELECT h.*, a.label account_label
                               FROM auto_comment_activity_history h
                               LEFT JOIN accounts a ON a.id = h.account_id
                               WHERE h.pipeline_id = ? ORDER BY h.id DESC LIMIT ?""",
                            (pipeline_id, limit)).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        item.pop("account_id", None)
        item.pop("job_id", None)
        item.pop("fb_comment_id", None)
        item.pop("pipeline_id", None)
        history.append(item)
    return {"ok": True, "history": history}


@app.get("/api/pipelines/{pipeline_id}/auto-comment/stats")
def auto_comment_stats(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """Get auto comment statistics."""
    require_permission(principal, "history.view")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        stats = {}
        stats["total_jobs"] = conn.execute("SELECT COUNT(*) FROM auto_comment_jobs WHERE pipeline_id = ?", (pipeline_id,)).fetchone()[0]
        stats["posted"] = conn.execute("SELECT COUNT(*) FROM auto_comment_jobs WHERE pipeline_id = ? AND status = 'posted'", (pipeline_id,)).fetchone()[0]
        stats["pending"] = conn.execute("SELECT COUNT(*) FROM auto_comment_jobs WHERE pipeline_id = ? AND status IN ('scheduled','pending')", (pipeline_id,)).fetchone()[0]
        stats["failed"] = conn.execute("SELECT COUNT(*) FROM auto_comment_jobs WHERE pipeline_id = ? AND status = 'failed'", (pipeline_id,)).fetchone()[0]
        stats["duplicate_limit"] = conn.execute("SELECT COUNT(*) FROM auto_comment_jobs WHERE pipeline_id = ? AND status = 'duplicate_limit'", (pipeline_id,)).fetchone()[0]
        
        # Fingerprint stats
        fp = conn.execute("""SELECT COUNT(*) as total, SUM(use_count) as uses, MAX(use_count) as max_uses
                             FROM comment_fingerprints WHERE pipeline_id = ?""", (pipeline_id,)).fetchone()
        stats["fingerprints"] = dict(fp) if fp else {"total": 0, "uses": 0, "max_uses": 0}
        
        # Today's count
        today = datetime.now().date().isoformat()
        stats["today_posted"] = conn.execute(
            "SELECT COUNT(*) FROM auto_comment_jobs WHERE pipeline_id = ? AND status = 'posted' AND DATE(posted_at) = ?",
            (pipeline_id, today)
        ).fetchone()[0]
    
    return {"ok": True, "stats": stats}


# =============================================================================
@app.get("/api/pipelines/{pipeline_id}/groups")
def pipeline_groups(pipeline_id: int, principal: Principal = Depends(require_principal)):
    """Every group of one workspace with its counters.

    A collaborator reads the groups of their own workspace, taken from the
    session, and sees exactly the rows the administrator sees there.
    """
    require_permission(principal, "groups.view")
    pipeline_id = scope_pipeline(principal, pipeline_id)
    try:
        condition, params = pipeline_scope.scope_list(principal, pipeline_id, "g.pipeline_id",
                                                      "g.owner_user_id")
    except pipeline_scope.NoWorkspace:
        raise HTTPException(403, "Tài khoản chưa được gán Workspace")
    with db() as conn:
        rows = conn.execute(f"""SELECT g.*, a.label account_label FROM groups g
                               LEFT JOIN accounts a ON a.id=g.account_id
                               WHERE {condition} ORDER BY g.id DESC""", params).fetchall()
        items = [group_payload(conn, row, principal) for row in rows]
        # Phân loại, khả năng đăng và chế độ đã chọn, theo đúng từng cặp
        # Facebook–Group.
        classifications = group_classification.pairs_of(conn, pipeline_id)
        for item in items:
            account_id = int(item.get("account_id") or 0)
            entry = classifications.get((account_id, int(item.get("id") or 0)))
            if entry is None:
                entry = group_classification.classification_of(conn, pipeline_id, account_id,
                                                              int(item.get("id") or 0))
            item["classification"] = entry["classification"]
            item["classification_label"] = entry["label"]
            item["classification_tone"] = entry["tone"]
            item["priority"] = entry["priority"]
            item["priority_rank"] = entry["priority_rank"]
            item["supports_anonymous"] = entry["supports_anonymous"]
            item["supports_nickname"] = entry["supports_nickname"]
            item["capability_state"] = entry["capability_state"]
            item["last_verified_at"] = entry.get("last_verified_at")
            item["last_post_status"] = entry.get("last_post_status", "")
            item["selected_posting_mode"] = item.get("posting_mode") or "anonymous"
        return items


@app.post("/api/pipelines/{pipeline_id}/groups", status_code=201)
def add_pipeline_group(pipeline_id: int, data: GroupCreate, principal: Principal = Depends(require_principal)):
    """Add one group to a workspace.

    A collaborator adds to the workspace of their session, so a link left over
    from another workspace cannot put their group in the wrong place.
    """
    require_permission(principal, "groups.manage")
    try:
        workspace = pipeline_scope.workspace_for(principal, pipeline_id)
    except pipeline_scope.WorkspaceRequired:
        raise HTTPException(422, "Thiếu pipeline_id")
    except pipeline_scope.NoWorkspace:
        raise HTTPException(403, "Tài khoản chưa được gán Workspace")
    if data.pipeline_id is not None and data.pipeline_id != workspace:
        raise HTTPException(404, "Not found")
    try:
        url, group_key = normalize_group_url(data.url)
    except InvalidGroupLink as error:
        raise HTTPException(422, str(error)) from error
    aliases = group_alias_keys(data.url)
    if data.posting_mode == "nickname" and not data.nickname.strip():
        raise HTTPException(422, "Chế độ biệt danh cần có tên biệt danh đã cấu hình")
    with db() as conn:
        entry(conn, "pipelines", workspace)
        account = scoped_entry(conn, "accounts", data.account_id, principal)
        if account["pipeline_id"] != workspace:
            raise HTTPException(404, "Not found")
        if group_already_present(conn, workspace, group_key, aliases):
            raise HTTPException(409, "Group này đã có trong Workspace")
        label = data.label.strip() or group_display_name(url)
        cursor = conn.execute("""INSERT INTO groups
            (pipeline_id,account_id,label,url,group_key,enabled,approved_target,created_at,
             group_name,posting_mode,nickname,status,source_key,owner_user_id)
            VALUES(?,?,?,?,?,1,?,?,?,?,?,'active',?,?)""",
            (workspace, data.account_id, label, url, group_key, int(data.approved_target),
             iso(), label, data.posting_mode, data.nickname.strip(),
             ",".join(sorted(set([group_key] + aliases))), resource_owner(principal)))
        group_id = cursor.lastrowid
        upsert_group_account(conn, workspace, group_id, data.account_id,
                             data.posting_mode, data.nickname.strip())
        for extra in data.account_ids:
            if extra == data.account_id:
                continue
            other = scoped_entry(conn, "accounts", extra, principal)
            if other["pipeline_id"] != workspace:
                raise HTTPException(404, "Not found")
            upsert_group_account(conn, workspace, group_id, extra, data.posting_mode,
                                 data.nickname.strip())
    audit("group", group_id, f"Thêm group {label}", workspace)
    with db() as conn:
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        return group_payload(conn, row)


@app.post("/api/pipelines/{pipeline_id}/groups/bulk", status_code=201)
def add_pipeline_groups_bulk(pipeline_id: int, data: GroupBulkCreate,
                              principal: Principal = Depends(require_principal)):
    """Add many groups from a list of links, one per line."""
    require_permission(principal, "groups.manage")
    scope_pipeline(principal, pipeline_id)
    if data.posting_mode == "nickname" and not data.nickname.strip():
        raise HTTPException(422, "Chế độ biệt danh cần có tên biệt danh đã cấu hình")
    lines = [line.strip() for line in (data.urls or "").splitlines() if line.strip()]
    if not lines:
        raise HTTPException(422, "Chưa có link group nào")
    if len(lines) > 100:
        raise HTTPException(422, "Tối đa 100 link mỗi lần")
    with db() as conn:
        entry(conn, "pipelines", pipeline_id)
        if data.account_id is not None:
            account = scoped_entry(conn, "accounts", data.account_id, principal)
            if account["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
        existing = set()
        owner = resource_owner(principal)
        for row in conn.execute("""SELECT group_key, source_key FROM groups
                                   WHERE pipeline_id=? AND owner_user_id IS ?""",
                                (pipeline_id, owner)).fetchall():
            existing.add(row["group_key"])
            if row["source_key"]:
                existing.add(row["source_key"])
    added, skipped, invalid, duplicates = [], [], [], []
    for line in lines:
        try:
            url, group_key = normalize_group_url(line)
        except InvalidGroupLink as error:
            invalid.append({"input": line, "reason": str(error)})
            continue
        aliases = group_alias_keys(line)
        if group_key in existing or any(alias in existing for alias in aliases):
            duplicates.append({"input": line, "reason": "Group đã có trong Workspace"})
            continue
        existing.add(group_key)
        for alias in aliases:
            existing.add(alias)
        try:
            with db() as conn:
                account_id = data.account_id
                if account_id is None:
                    condition, values = owner_condition(principal, "a")
                    found = conn.execute(
                        f"""SELECT id FROM accounts a WHERE a.pipeline_id=? AND a.enabled=1
                            AND {condition} ORDER BY a.id LIMIT 1""",
                        (pipeline_id, *values)).fetchone()
                    if found is None:
                        raise HTTPException(409, "Workspace chưa có tài khoản Facebook để gán")
                    account_id = found["id"]
                label = (data.label_prefix.strip() + " " if data.label_prefix.strip() else "") \
                    + group_display_name(url)
                cursor = conn.execute("""INSERT INTO groups
                    (pipeline_id,account_id,label,url,group_key,enabled,approved_target,created_at,
                     group_name,posting_mode,nickname,status,source_key,owner_user_id)
                    VALUES(?,?,?,?,?,1,?,?,?,?,?,'active',?,?)""",
                    (pipeline_id, account_id, label, url, group_key, int(data.approved_target),
                     iso(), label, data.posting_mode, data.nickname.strip(),
                     ",".join(sorted(set([group_key] + aliases))), owner))
                group_id = cursor.lastrowid
                upsert_group_account(conn, pipeline_id, group_id, account_id,
                                     data.posting_mode, data.nickname.strip())
            added.append({"id": group_id, "url": url, "label": label})
        except HTTPException as error:
            skipped.append({"input": line, "reason": str(error.detail)})
        except Exception as error:
            skipped.append({"input": line, "reason": f"{type(error).__name__}"})
    audit("group", None, f"Thêm hàng loạt {len(added)} group, {len(duplicates)} trùng, {len(invalid)} sai",
          pipeline_id)
    return {"ok": True, "added": added, "duplicates": duplicates, "invalid": invalid,
            "skipped": skipped, "added_count": len(added), "invalid_count": len(invalid),
            "duplicate_count": len(duplicates)}


@app.patch("/api/pipelines/{pipeline_id}/groups/{group_id}")
def update_pipeline_group(pipeline_id: int, group_id: int, data: GroupUpdate,
                          principal: Principal = Depends(require_principal)):
    """Change the account, the posting mode, the nickname or the state of a group."""
    require_permission(principal, "groups.manage")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        group = scoped_entry(conn, "groups", group_id, principal)
        if group["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        config = group_config(conn, group_id)
        mode = data.posting_mode if data.posting_mode is not None else config["posting_mode"]
        nickname = data.nickname.strip() if data.nickname is not None else config["nickname"]
        if mode == "nickname" and not nickname:
            raise HTTPException(422, "Chế độ biệt danh cần có tên biệt danh đã cấu hình")
        account_id = data.account_id if data.account_id is not None else config["account_id"]
        if account_id is not None:
            account = scoped_entry(conn, "accounts", account_id, principal)
            if account["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
        if data.label is not None:
            conn.execute("UPDATE groups SET label=? WHERE id=?", (data.label.strip(), group_id))
        conn.execute("UPDATE groups SET posting_mode=?, nickname=?, status=? WHERE id=?",
                     (mode, nickname, data.status or group["status"], group_id))
        if data.enabled is not None:
            conn.execute("UPDATE groups SET enabled=? WHERE id=?", (int(data.enabled), group_id))
        if account_id is not None:
            conn.execute("UPDATE groups SET account_id=? WHERE id=?", (account_id, group_id))
            upsert_group_account(conn, pipeline_id, group_id, account_id, mode, nickname)
        if data.account_ids is not None:
            conn.execute("DELETE FROM group_accounts WHERE group_id=? AND account_id<>?",
                         (group_id, account_id if account_id is not None else -1))
            for extra in data.account_ids:
                other = scoped_entry(conn, "accounts", extra, principal)
                if other["pipeline_id"] != pipeline_id:
                    raise HTTPException(404, "Not found")
                upsert_group_account(conn, pipeline_id, group_id, extra, mode, nickname)
        if data.manual_review is not None:
            conn.execute("UPDATE groups SET manual_review=? WHERE id=?",
                         (int(data.manual_review), group_id))
        if data.screening_status is not None:
            # Only an administrator may state the moderation state by hand, and
            # the result is marked as manual so it is never mixed with evidence.
            if not principal.is_admin:
                raise HTTPException(403, "Chỉ Chủ hệ thống được xác nhận trạng thái kiểm duyệt thủ công")
            target = account_id if account_id is not None else group["account_id"]
            store_screening_result(conn, pipeline_id, target, group_id, None,
                                   "manual", data.screening_status)
    audit("group", group_id, f"Cập nhật group: mode={mode}", pipeline_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        return group_payload(conn, row)


@app.delete("/api/pipelines/{pipeline_id}/groups/{group_id}")
def delete_pipeline_group(pipeline_id: int, group_id: int, principal: Principal = Depends(require_principal)):
    """Remove a group from the workspace."""
    require_permission(principal, "groups.manage")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        group = scoped_entry(conn, "groups", group_id, principal)
        if group["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        running = conn.execute("""SELECT COUNT(*) FROM jobs WHERE group_id=? AND state='running'""",
                               (group_id,)).fetchone()[0]
        if running:
            raise HTTPException(409, "Group đang đăng bài; thử lại sau")
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
    audit("group", group_id, "Xóa group khỏi Workspace", pipeline_id)
    return {"ok": True}


def run_group_probe(account_id: int, url: str, nickname: str) -> dict:
    """Run the group probe in a worker process and return its answer."""
    payload_file = DATA / f"group-probe-{account_id}-{secrets.token_hex(6)}.json"
    result_file = DATA / f"group-probe-result-{account_id}-{secrets.token_hex(6)}.json"
    payload_file.write_text(json.dumps({"account_id": account_id, "group_url": url,
                                        "nickname": nickname}), encoding="utf-8")
    payload_file.chmod(0o600)
    outcome = {"status": "error", "details": "Không chạy được trình duyệt kiểm tra",
               "logged_in": False, "accessible": None, "is_member": None, "can_post": None,
               "supports_anonymous": None, "supports_nickname": None,
               "nickname_available": None, "requires_approval": False, "group_name": ""}
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fb_browser.py"), "check-group", str(payload_file), str(result_file)],
            cwd=ROOT, env={**os.environ, "DATA_DIR": str(DATA), "DISPLAY": ":99"},
            capture_output=True, text=True, timeout=210)
        if result_file.exists():
            outcome = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            outcome["details"] = f"Worker exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        outcome["details"] = "Kiểm tra quá thời gian; thử lại sau"
    except Exception as error:
        outcome["details"] = f"Lỗi kiểm tra: {type(error).__name__}"
    finally:
        payload_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
    return outcome


@app.post("/api/pipelines/{pipeline_id}/groups/preview-check")
def preview_check_group(pipeline_id: int, data: GroupPreviewCheck,
                        principal: Principal = Depends(require_principal)):
    """Check a group link before it is saved."""
    require_permission(principal, "groups.manage")
    scope_pipeline(principal, pipeline_id)
    try:
        url, _ = normalize_group_url(data.url)
    except InvalidGroupLink as error:
        raise HTTPException(422, str(error)) from error
    with db() as conn:
        account = scoped_entry(conn, "accounts", data.account_id, principal)
        if account["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
    outcome = run_group_probe(data.account_id, url, data.nickname.strip())
    return {"ok": True, "status": outcome.get("status", "unknown"),
            "details": str(outcome.get("details", ""))[:500],
            "group": {key: outcome.get(key) for key in
                      ("logged_in", "accessible", "is_member", "can_post", "supports_anonymous",
                       "supports_nickname", "nickname_available", "requires_approval", "group_name")}}


@app.post("/api/pipelines/{pipeline_id}/groups/{group_id}/check")
def check_pipeline_group(pipeline_id: int, group_id: int, principal: Principal = Depends(require_principal)):
    """Check what this account may do in the group, using its own session."""
    require_permission(principal, "groups.manage")
    scope_pipeline(principal, pipeline_id)
    with db() as conn:
        group = scoped_entry(conn, "groups", group_id, principal)
        if group["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
        config = group_config(conn, group_id)
    if not config["account_id"]:
        raise HTTPException(409, "Group chưa gán tài khoản Facebook")
    payload_file = DATA / f"group-check-{group_id}-{secrets.token_hex(6)}.json"
    result_file = DATA / f"group-check-result-{group_id}-{secrets.token_hex(6)}.json"
    payload_file.write_text(json.dumps({"account_id": config["account_id"],
                                        "group_url": group["url"],
                                        "nickname": config["nickname"]}), encoding="utf-8")
    payload_file.chmod(0o600)
    outcome = {"status": "error", "details": "Không chạy được trình duyệt kiểm tra",
               "logged_in": False, "accessible": False, "is_member": False, "can_post": False,
               "supports_anonymous": False, "supports_nickname": False, "nickname_available": False,
               "requires_approval": False, "group_name": ""}
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fb_browser.py"), "check-group", str(payload_file), str(result_file)],
            cwd=ROOT, env={**os.environ, "DATA_DIR": str(DATA), "DISPLAY": ":99"},
            capture_output=True, text=True, timeout=210)
        if result_file.exists():
            outcome = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            outcome["details"] = f"Worker exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        outcome["details"] = "Kiểm tra quá thời gian; thử lại sau"
    except Exception as error:
        outcome["details"] = f"Lỗi kiểm tra: {type(error).__name__}"
    finally:
        payload_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
    with db() as conn:
        if not account_session_state.record_probe(conn,pipeline_id,config['account_id'],outcome.pop('session_observation',None),iso()):
            outcome.update(status='error',accessible=None,is_member=None,can_post=None,details='Cần kiểm tra lại danh tính phiên Facebook.')
    status = str(outcome.get("status", "unknown"))
    if status not in ("ok", "unknown", "needs_review", "not_member", "no_access", "error", "busy"):
        status = "unknown"
    # Facebook redirects a shared link to the real group. Keep that address so
    # the same group is never added twice under two different keys.
    resolved = str(outcome.get("resolved_url", "") or "")
    with db() as conn:
        if resolved and outcome.get("resolution_verified") is True and outcome.get("accessible") is True:
            try:
                resolved_url, resolved_key = normalize_group_url(resolved)
            except InvalidGroupLink:
                resolved_url, resolved_key = "", ""
            if resolved_key and resolved_key != group["group_key"]:
                conn.execute("UPDATE groups SET url=?, group_key=? WHERE id=?",
                             (resolved_url, resolved_key, group_id))
        conn.execute("""UPDATE groups SET check_status=?, check_details_json=?, last_checked_at=?,
                        can_post=?, is_member=?, supports_anonymous=?, supports_nickname=?,
                        nickname_available=?, requires_approval=?, group_name=? WHERE id=?""",
                     (status, json.dumps(outcome), iso(), int(bool(outcome.get("can_post"))),
                      int(bool(outcome.get("is_member"))), int(bool(outcome.get("supports_anonymous"))),
                      int(bool(outcome.get("supports_nickname"))),
                      int(bool(outcome.get("nickname_available"))),
                      int(bool(outcome.get("requires_approval"))),
                      str(outcome.get("group_name", ""))[:200], group_id))
    audit("group", group_id, f"Kiểm tra group: {status}", pipeline_id)
    return {"ok": True, "status": status, "details": str(outcome.get("details", ""))[:500],
            "group": {key: outcome.get(key) for key in
                      ("logged_in", "accessible", "is_member", "can_post", "supports_anonymous",
                       "supports_nickname", "nickname_available", "requires_approval", "group_name")},
            "last_checked_at": iso()}


@app.get("/api/groups")
def groups(pipeline_id: int | None = None, principal: Principal = Depends(require_principal)):
    """The groups of one workspace.

    A collaborator reads the groups of their own workspace, taken from the
    session, and sees exactly the rows the administrator sees there.
    """
    require_permission(principal, "groups.view")
    try:
        condition, params = pipeline_scope.scope_list(principal, pipeline_id, "g.pipeline_id",
                                                          "g.owner_user_id")
    except pipeline_scope.NoWorkspace:
        raise HTTPException(403, "Tài khoản chưa được gán Workspace")
    with db() as conn:
        sql = """SELECT g.*, a.label account_label, p.name pipeline_name,
                        (SELECT u.username FROM users u WHERE u.id=g.owner_user_id) owner_username
                 FROM groups g JOIN accounts a ON a.id=g.account_id JOIN pipelines p ON p.id=g.pipeline_id
                WHERE """ + condition + " ORDER BY g.id DESC"
        return [dict(row) for row in conn.execute(sql, params)]


@app.post("/api/groups", status_code=201)
def create_group(data: GroupCreate, principal: Principal = Depends(require_principal)):
    """Add one group to the workspace of the caller."""
    require_permission(principal, "groups.manage")
    try:
        workspace = pipeline_scope.workspace_for(principal, data.pipeline_id)
    except pipeline_scope.WorkspaceRequired:
        raise HTTPException(422, "Thiếu pipeline_id")
    except pipeline_scope.NoWorkspace:
        raise HTTPException(403, "Tài khoản chưa được gán Workspace")
    link, key = group_link(data.url)
    with db() as conn:
        entry(conn, "pipelines", workspace)
        account = scoped_entry(conn, "accounts", data.account_id, principal)
        if account["pipeline_id"] != workspace:
            raise HTTPException(404, "Not found")
        try:
            result = conn.execute("""INSERT INTO groups(pipeline_id,account_id,label,url,group_key,approved_target,created_at,owner_user_id)
                        VALUES(?,?,?,?,?,?,?,?)""", (workspace, data.account_id, data.label, link,
                                                      key, int(data.approved_target), iso(),
                                                      resource_owner(principal)))
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Group already exists in this pipeline") from error
        created_id = result.lastrowid
    audit("group", created_id, f"Added target {link}; permission confirmed={data.approved_target}")
    return {"id": created_id}


@app.patch("/api/groups/{group_id}")
def patch_group(group_id: int, data: EnabledUpdate, principal: Principal = Depends(require_principal)):
    """Bật/tắt group, và chọn tài khoản cùng chế độ đăng cho group đó."""
    with db() as conn:
        group = scoped_entry(conn, "groups", group_id, principal)
    require_permission(principal, "groups.manage")
    changes = []
    with db() as conn:
        if data.enabled is not None:
            conn.execute("UPDATE groups SET enabled=? WHERE id=?", (int(data.enabled), group_id))
            changes.append("bật/tắt")
        if data.account_id is not None:
            account = scoped_entry(conn, "accounts", int(data.account_id), principal)
            if account["pipeline_id"] != group["pipeline_id"]:
                raise HTTPException(404, "Not found")
            conn.execute("UPDATE groups SET account_id=? WHERE id=?",
                         (int(data.account_id), group_id))
            upsert_group_account(conn, group["pipeline_id"], group_id, int(data.account_id),
                                 group["posting_mode"] or "anonymous", group["nickname"] or "")
            changes.append("đổi tài khoản")
        if data.posting_mode is not None:
            mode = str(data.posting_mode)
            if mode not in (MODE_ANONYMOUS, MODE_NICKNAME):
                raise HTTPException(422, "Chế độ đăng không hợp lệ")
            conn.execute("UPDATE groups SET posting_mode=? WHERE id=?", (mode, group_id))
            upsert_group_account(conn, group["pipeline_id"], group_id, group["account_id"],
                                 mode, data.nickname if data.nickname is not None
                                 else (group["nickname"] or ""))
            changes.append("đổi chế độ đăng")
        elif data.nickname is not None:
            conn.execute("UPDATE groups SET nickname=? WHERE id=?", (data.nickname, group_id))
            upsert_group_account(conn, group["pipeline_id"], group_id, group["account_id"],
                                 group["posting_mode"] or "anonymous", data.nickname)
            changes.append("đổi biệt danh")
    if changes:
        audit("group", group_id, ", ".join(changes), group["pipeline_id"])
    return {"ok": True}


@app.post("/api/uploads")
async def upload(pipeline_id: int | None = None, file: UploadFile = File(...), principal: Principal = Depends(require_principal)):
    require_permission(principal, "group_posts.create")
    scope = scope_pipeline(principal, pipeline_id)
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    suffix = allowed.get(file.content_type or "")
    if not suffix:
        raise HTTPException(415, "Only JPG, PNG or WebP images allowed")
    content = await file.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "Maximum 8 MB per image")
    headers = {".jpg": b"\xff\xd8\xff", ".png": b"\x89PNG\r\n\x1a\n", ".webp": b"RIFF"}
    if not content.startswith(headers[suffix]):
        raise HTTPException(415, "Invalid image signature")
    if suffix == ".webp" and content[8:12] != b"WEBP":
        raise HTTPException(415, "Invalid WebP")
    filename = secrets.token_hex(16) + suffix
    folder = UPLOADS / str(scope)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    dest = folder / filename
    dest.write_bytes(content)
    dest.chmod(0o600)
    return {"path": f"uploads/{scope}/{filename}", "name": file.filename}


@app.post("/api/video-uploads")
async def upload_video(pipeline_id: int | None = None, file: UploadFile = File(...), principal: Principal = Depends(require_principal)):
    require_permission(principal, "reels.create")
    scope = scope_pipeline(principal, pipeline_id)
    allowed = {"video/mp4": ".mp4", "video/quicktime": ".mov"}
    suffix = allowed.get(file.content_type or "")
    if not suffix:
        raise HTTPException(415, "Only MP4 or MOV video allowed")
    filename = secrets.token_hex(16) + suffix
    folder = UPLOADS / str(scope)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    file_path = folder / filename
    maximum_bytes = 200 * 1024 * 1024
    byte_count = 0
    prefix = b""
    try:
        with file_path.open("xb") as handle:
            while chunk := await file.read(1024 * 1024):
                byte_count += len(chunk)
                if byte_count > maximum_bytes:
                    raise HTTPException(413, "Video exceeds 200 MB")
                if len(prefix) < 12:
                    prefix += chunk[:12 - len(prefix)]
                handle.write(chunk)
        if len(prefix) < 12 or prefix[4:8] != b"ftyp":
            raise HTTPException(415, "Invalid MP4/MOV signature")
        file_path.chmod(0o600)
    except Exception:
        file_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {"path": f"uploads/{scope}/{filename}", "size": byte_count}


@app.get("/api/media/{filename:path}")
def serve_upload(filename: str, principal: Principal = Depends(require_principal)):
    require_any_permission(principal, ("group_posts.create", "reels.create", "history.view", "facebook.view"))
    with db() as conn:
        path, _ = media_file(conn, filename, principal)
    return FileResponse(path)


@app.post("/api/posts", status_code=201)
def create_post(data: PostCreate, principal: Principal = Depends(require_principal)):
    scope_pipeline(principal, data.pipeline_id)
    require_permission(principal, "group_posts.create")
    scheduled = parse_date(data.scheduled_at)
    if scheduled < now() - timedelta(minutes=5):
        raise HTTPException(422, "Schedule cannot be in the past")
    if not data.anonymous_only:
        raise HTTPException(422, "Group posting must be anonymous; named fallback prohibited")
    if not data.media:
        raise HTTPException(422, "Anonymous group posts require one or more images")
    if len(set(data.group_ids)) != len(data.group_ids):
        raise HTTPException(422, "Duplicate groups in request")
    media_paths = []
    with db() as conn:
        for filename in data.media:
            try:
                media_file(conn, filename, principal)
            except HTTPException as error:
                raise HTTPException(422, "Invalid image reference") from error
            media_paths.append(filename)
        pipeline = entry(conn, "pipelines", data.pipeline_id)
        if not pipeline["enabled"]:
            raise HTTPException(409, "Pipeline is paused")
        selected_groups = []
        for group_id in data.group_ids:
            group = scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != data.pipeline_id:
                raise HTTPException(404, "Not found")
            account = scoped_entry(conn, "accounts", group["account_id"], principal)
            if account["pipeline_id"] != data.pipeline_id:
                raise HTTPException(404, "Not found")
            if not group["enabled"] or not account["enabled"]:
                raise HTTPException(409, f"Group {group_id} or account disabled")
            if not group["approved_target"]:
                raise HTTPException(409, f"Confirm permission to publish in group {group_id} first")
            selected_groups.append(group)
        # An AI caption is used only when it was approved and not blocked.
        ai_caption, ai_record = usable_caption(conn, data.pipeline_id, data.ai_record_id)
        caption = ai_caption or data.body
        result = conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only,ai_record_id)
                                 VALUES(?,?,?,?,?,?)""",
                              (data.pipeline_id, caption, json.dumps(media_paths), iso(), 1, ai_record))
        post_id = result.lastrowid
        # Without the publish permission the post is only a draft: it waits for
        # an administrator instead of reaching Facebook. A caption that has not
        # passed the AI check keeps the job out of the publishing queue as well.
        may_publish = principal.can("group_posts.publish")
        if data.ai_record_id and not ai_caption:
            may_publish = False
            job_details = "Caption AI chưa được duyệt hoặc bị chặn, bài chờ người duyệt"
        elif not may_publish:
            job_details = "Bài nháp: CTV chưa có quyền đăng, cần Chủ hệ thống duyệt"
        else:
            job_details = ""
        job_state = "queued" if may_publish else "pending_approval"
        for group in selected_groups:
            conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,scheduled_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         (post_id, group["id"], group["account_id"], data.pipeline_id, job_state,
                          job_details, iso(scheduled), iso()))
    audit("post", post_id, f"Scheduled for {len(selected_groups)} groups", data.pipeline_id)
    return {"id": post_id, "jobs_created": len(selected_groups), "state": job_state,
            "published": may_publish}


@app.get("/api/jobs")
def get_jobs(pipeline_id: int | None = None, limit: int = 100, principal: Principal = Depends(require_principal)):
    require_permission(principal, "history.view")
    limit = min(300, max(1, limit))
    with db() as conn:
        sql = """SELECT j.*, g.label group_name, g.url group_url, a.label account_name,
                        p.body, p.media_json, p.anonymous_only, p.pipeline_id, l.name pipeline_name
                 FROM jobs j JOIN groups g ON j.group_id=g.id JOIN accounts a ON j.account_id=a.id
                 JOIN posts p ON j.post_id=p.id JOIN pipelines l ON p.pipeline_id=l.id"""
        params = []
        conditions = []
        if pipeline_id:
            scope_pipeline(principal, pipeline_id)
            conditions.append("p.pipeline_id=?")
            params.append(pipeline_id)
        elif not principal.is_admin:
            conditions.append("p.pipeline_id=?")
            params.append(principal.pipeline_id)
        condition, values = owner_condition(principal, "p")
        conditions.append(condition)
        params.extend(values)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY j.id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in conn.execute(sql, params)]


@app.get("/api/pipelines/{pipeline_id}/posts")
def list_posts(pipeline_id: int, state: str | None = None, limit: int = 100,
               search: str | None = None, principal: Principal = Depends(require_principal)):
    """List posts (campaigns) with aggregate job info for the scheduled-posts tab.

    Optional ``state`` filter narrows to posts that have at least one job in
    the given state.  Optional ``search`` filters on the post body / group names.
    """
    require_permission(principal, "history.view")
    scope_pipeline(principal, pipeline_id)
    limit = min(300, max(1, limit))
    with db() as conn:
        sql = """
            SELECT p.id, p.pipeline_id, p.body, p.media_json, p.created_at, p.anonymous_only,
                   p.ai_record_id,
                   (SELECT COUNT(*) FROM jobs WHERE post_id=p.id) AS job_count,
                   (SELECT COUNT(*) FROM jobs WHERE post_id=p.id AND state IN ('queued','running','submitted_unverified')) AS pending_count,
                   (SELECT COUNT(*) FROM jobs WHERE post_id=p.id AND state='posted') AS posted_count,
                   (SELECT COUNT(*) FROM jobs WHERE post_id=p.id AND state IN ('failed','needs_review','cancelled')) AS failed_count,
                   (SELECT MIN(scheduled_at) FROM jobs WHERE post_id=p.id) AS first_scheduled,
                   (SELECT MAX(scheduled_at) FROM jobs WHERE post_id=p.id) AS last_scheduled
            FROM posts p
            WHERE p.pipeline_id=?
        """
        params = [pipeline_id]
        conditions = []
        if state:
            if state == "scheduled":
                conditions.append("(SELECT COUNT(*) FROM jobs WHERE post_id=p.id AND state IN ('queued','pending_approval')) > 0")
            elif state == "needs_review":
                conditions.append("EXISTS (SELECT 1 FROM jobs WHERE post_id=p.id AND state IN ('needs_review'))")
            elif state == "processing":
                conditions.append("EXISTS (SELECT 1 FROM jobs WHERE post_id=p.id AND state IN ('running','submitted_unverified'))")
            elif state == "posted":
                conditions.append("NOT EXISTS (SELECT 1 FROM jobs WHERE post_id=p.id AND state NOT IN ('posted','cancelled'))")
            elif state == "failed":
                conditions.append("EXISTS (SELECT 1 FROM jobs WHERE post_id=p.id AND state='failed')")
            elif state == "cancelled":
                conditions.append("EXISTS (SELECT 1 FROM jobs WHERE post_id=p.id AND state='cancelled')")
        if search:
            conditions.append("(p.body LIKE ? OR EXISTS (SELECT 1 FROM jobs j JOIN groups g ON g.id=j.group_id WHERE j.post_id=p.id AND (g.label LIKE ? OR g.url LIKE ?)))")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
        if conditions:
            sql += " AND " + " AND ".join(conditions)
        sql += " ORDER BY first_scheduled DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in conn.execute(sql, params)]


@app.get("/api/posts/{post_id}/jobs")
def list_post_jobs(post_id: int, principal: Principal = Depends(require_principal)):
    """List all jobs (per-group) belonging to one post, scoped to the caller's pipeline."""
    require_permission(principal, "history.view")
    with db() as conn:
        post = scoped_entry(conn, "posts", post_id, principal)
        if post is None:
            raise HTTPException(404, "Not found")
        scope_pipeline(principal, post["pipeline_id"])
        sql = """SELECT j.*, g.label group_name, g.url group_url, g.posting_mode group_mode,
                        g.nickname group_nickname, a.label account_name, a.fb_name
                 FROM jobs j JOIN groups g ON j.group_id=g.id JOIN accounts a ON j.account_id=a.id
                 WHERE j.post_id=? ORDER BY j.id"""
        return [dict(row) for row in conn.execute(sql, (post_id,))]


@app.patch("/api/posts/{post_id}")
def update_post(post_id: int, data: PostUpdate, principal: Principal = Depends(require_principal)):
    """Edit caption, media, or target groups of a post whose jobs are all still editable.

    Only jobs in 'queued' or 'pending_approval' state may be updated. Once a job
    has started (running, submitted_unverified, posted, failed) the post is locked.
    """
    require_permission(principal, "group_posts.create")
    with db() as conn:
        post = scoped_entry(conn, "posts", post_id, principal)
        if post is None:
            raise HTTPException(404, "Not found")
        scope_pipeline(principal, post["pipeline_id"])
        locked = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE post_id=? AND state NOT IN ('queued','pending_approval')",
            (post_id,)).fetchone()[0]
        if locked > 0:
            raise HTTPException(409, "Bài đã bắt đầu xử lý; không thể sửa")
        updates = []
        params = []
        if data.body is not None:
            updates.append("body=?")
            params.append(data.body)
        if data.media_json is not None:
            for filename in data.media_json:
                try:
                    media_file(conn, filename, principal)
                except HTTPException as error:
                    raise HTTPException(422, f"Ảnh không hợp lệ: {filename}") from error
            updates.append("media_json=?")
            params.append(json.dumps(data.media_json))
        if data.group_ids is not None:
            existing = {row["group_id"] for row in conn.execute("SELECT group_id FROM jobs WHERE post_id=?", (post_id,))}
            new_groups = set(data.group_ids)
            for removed in existing - new_groups:
                conn.execute("DELETE FROM jobs WHERE post_id=? AND group_id=?", (post_id, removed))
                audit("job", None, f"Removed group {removed}", post["pipeline_id"])
            for added in sorted(new_groups - existing):
                group = scoped_entry(conn, "groups", added, principal)
                if group is None or int(group["pipeline_id"]) != int(post["pipeline_id"]):
                    raise HTTPException(404, f"Group {added} not found")
                account = scoped_entry(conn, "accounts", group["account_id"], principal)
                if account is None or int(account["pipeline_id"]) != int(post["pipeline_id"]):
                    raise HTTPException(404, f"Account for group {added} not found")
                if not group["enabled"] or not account["enabled"]:
                    raise HTTPException(409, f"Group or account disabled: {added}")
                if not group["approved_target"]:
                    raise HTTPException(409, f"Confirm permission to publish in group {added} first")
                conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,scheduled_at,updated_at)
                                VALUES(?,?,?,?,?,?,?,?)""",
                             (post_id, added, account["id"], post["pipeline_id"], "queued", "",
                              iso(), iso()))
                audit("job", None, f"Added group {added}", post["pipeline_id"])
        if updates:
            updates.append("updated_at=?")
            params.append(iso())
            params.append(post_id)
            conn.execute(f"UPDATE posts SET {', '.join(updates)} WHERE id=?", params)
    audit("post", post_id, "Updated post", post["pipeline_id"])
    return {"ok": True, "post_id": post_id}


@app.patch("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, principal: Principal = Depends(require_principal)):
    """Soft-cancel a job that has not started yet."""
    require_permission(principal, "group_posts.publish")
    with db() as conn:
        job = scoped_entry(conn, "jobs", job_id, principal)
        if job is None:
            raise HTTPException(404, "Not found")
        scope_pipeline(principal, job["pipeline_id"])
        if job["state"] in ("running", "posted"):
            raise HTTPException(409, "Không thể hủy bài đã đăng hoặc đang xử lý")
        if job["state"] == "cancelled":
            raise HTTPException(409, "Bài này đã được hủy trước đó")
        conn.execute("""UPDATE jobs SET state='cancelled', details=?, updated_at=? WHERE id=?""",
                     (f"Đã hủy bởi {principal.username} lúc {iso()}", iso(), job_id))
    audit("job", job_id, f"Cancelled by {principal.username}", job["pipeline_id"])
    return {"ok": True}


@app.post("/api/jobs/{job_id}/review")
def review_job(job_id: int, data: JobReview, principal: Principal = Depends(require_principal)):
    with db() as conn:
        job = scoped_entry(conn, "jobs", job_id, principal)
    require_permission(principal, "group_posts.publish")
    with db() as conn:
        if job["state"] == "running":
            raise HTTPException(409, "Cannot modify a running job")
        if data.state=='queued' and job['state']=='skipped':
            raise HTTPException(409,'Đích này đã được bỏ qua; chỉ xem xét lại trong chu kỳ mới.')
        uncertain = {"submitted_unverified", "needs_review", "posted"}
        if job["attempts"]:
            uncertain.add("pending_approval")
        if data.state == "queued" and (job["state"] in uncertain or job["posting_status"] in uncertain):
            raise HTTPException(409, "Potentially published: do not blindly retry; create a fresh post only after verification")
        target_group = conn.execute("SELECT url FROM groups WHERE id=? AND pipeline_id=?",
                                    (job["group_id"],job["pipeline_id"])).fetchone()
        if data.state == "posted" and (job["verification_status"] != "verified" or not target_group
                or not group_job_lifecycle.verified_group_post_link(job["permalink"],target_group["url"])):
            raise HTTPException(409, {"code": "POST_EVIDENCE_REQUIRED", "message": "Chưa có bằng chứng xác minh bài đã đăng."})
        if data.state == "queued" and not principal.is_admin:
            raise HTTPException(403, "Chỉ Chủ hệ thống được duyệt bài để đăng tự động")
        conn.execute("""UPDATE jobs SET state=?, details=?, updated_at=?,
                        approved_by_user_id=COALESCE(approved_by_user_id,?) WHERE id=?""",
                     (data.state, data.details or f"Manually set to {data.state}", iso(),
                      principal.user_id, job_id))
        analytics_worker.sync_attribution(conn, "group_post", job["id"],
                                          approved_by=principal.user_id, pipeline_id=job["pipeline_id"])
        refresh_screening_for_job(conn, job_id)
    audit("job", job_id, f"Manual review: {data.state}", job["pipeline_id"])
    return {"ok": True}


@app.get("/api/manual-tasks")
def list_manual_tasks(pipeline_id: int | None = None, principal: Principal = Depends(require_principal)):
    require_permission(principal, "history.view")
    with db() as conn:
        statement = """SELECT t.*, a.label account_label, g.label group_label, p.name pipeline_name
                       FROM manual_tasks t JOIN accounts a ON a.id=t.account_id
                       LEFT JOIN groups g ON g.id=t.group_id
                       JOIN pipelines p ON p.id=t.pipeline_id"""
        args = []
        conditions = []
        if pipeline_id is not None:
            scope_pipeline(principal, pipeline_id)
            conditions.append("t.pipeline_id=?")
            args.append(pipeline_id)
        elif not principal.is_admin:
            conditions.append("t.pipeline_id=?")
            args.append(principal.pipeline_id)
        condition, values = owner_condition(principal, "t")
        conditions.append(condition)
        args.extend(values)
        if conditions:
            statement += " WHERE " + " AND ".join(conditions)
        return [dict(row) for row in conn.execute(statement + " ORDER BY t.id DESC LIMIT 300", args)]


@app.post("/api/manual-tasks", status_code=201)
def create_manual_task(data: ManualTaskCreate, principal: Principal = Depends(require_principal)):
    scope_pipeline(principal, data.pipeline_id)
    require_permission(principal, "comments.manage" if data.task_type == "comment" else "reels.create")
    scheduled = parse_date(data.scheduled_at)
    if scheduled < now() - timedelta(minutes=5):
        raise HTTPException(422, "Schedule cannot be in the past")
    media = []
    if data.task_type in ("video", "reel"):
        if len(data.media) != 1:
            raise HTTPException(422, "Upload exactly one video for Reel/Video")
        filename = data.media[0]
        try:
            with db() as conn:
                media_file(conn, filename, principal)
        except HTTPException as error:
            raise HTTPException(422, "Video not found") from error
        media = [filename]
    if data.auto_publish and data.task_type != "reel":
        raise HTTPException(422, "Experimental automatic browser posting is available only for personal Reels")
    if data.task_type == "reel" and data.group_id is not None:
        raise HTTPException(422, "Personal Reel cannot target a Facebook group")
    if data.task_type == "comment":
        if not data.body.strip():
            raise HTTPException(422, "Comment text is required")
        parsed = urlparse(data.target_url.strip())
        if parsed.scheme != "https" or parsed.hostname not in ("facebook.com", "www.facebook.com", "m.facebook.com"):
            raise HTTPException(422, "Comment link must be HTTPS facebook.com")
        if parsed.username or parsed.password or not parsed.path or parsed.path == "/":
            raise HTTPException(422, "Link to a specific Facebook post or comment")
    if data.task_type in ("reel", "video"):
        # The safety engine is not optional: a Reel is only scheduled when its
        # pre-check is still valid for this file and this caption.
        with db() as conn:
            allowed, reason = precheck_allows_publish(conn, data.pipeline_id, data.account_id,
                                                       media[0], data.body, data.precheck_id)
        if not allowed:
            raise HTTPException(409, reason)

    with db() as conn:
        pipeline = entry(conn, "pipelines", data.pipeline_id)
        if not pipeline["enabled"]:
            raise HTTPException(409, "Pipeline paused")
        account = scoped_entry(conn, "accounts", data.account_id, principal)
        if account["pipeline_id"] != data.pipeline_id:
            raise HTTPException(404, "Not found")
        if not account["enabled"]:
            raise HTTPException(409, "Facebook account disabled")
        if data.group_id is not None:
            group = scoped_entry(conn, "groups", data.group_id, principal)
            if group["pipeline_id"] != data.pipeline_id:
                raise HTTPException(404, "Not found")
            if group["account_id"] != data.account_id:
                raise HTTPException(422, "This group is assigned to another Facebook account")
            if not group["enabled"] or not group["approved_target"]:
                raise HTTPException(409, "Group disabled or not permissioned")
        # An AI caption is used only when it was approved and not blocked.
        ai_caption, ai_record = usable_caption(conn, data.pipeline_id, data.ai_record_id)
        caption = ai_caption or data.body.strip()
        # Without the publish permission the task stays a manual draft.
        auto_publish = bool(data.auto_publish) and principal.can("reels.publish")
        if data.ai_record_id and not ai_caption:
            auto_publish = False
            ai_reason = "Caption AI chưa được duyệt hoặc bị chặn, tác vụ chờ người duyệt"
        elif not auto_publish:
            ai_reason = "Bản nháp: CTV chưa có quyền đăng tự động"
        else:
            ai_reason = ""
        result = conn.execute("""INSERT INTO manual_tasks
            (pipeline_id,account_id,group_id,task_type,body,media_json,target_url,scheduled_at,
             created_at,updated_at,status,auto_publish,details,ai_record_id,owner_user_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (data.pipeline_id, data.account_id, data.group_id,
            data.task_type, caption, json.dumps(media), data.target_url.strip(),
            iso(scheduled), iso(), iso(), "queued_experimental" if auto_publish else "awaiting_manual",
            int(auto_publish), ai_reason, ai_record, resource_owner(principal)))
        task_id = result.lastrowid
    audit("manual_task", task_id, f"Created {data.task_type}; mode={'experimental browser' if data.auto_publish else 'manual'}")
    return {"id": task_id,
            "status": "queued_experimental" if auto_publish else "awaiting_manual",
            "manual_only": not auto_publish}


@app.patch("/api/manual-tasks/{task_id}")
def update_manual_task(task_id: int, data: ManualTaskUpdate, principal: Principal = Depends(require_principal)):
    with db() as conn:
        row = scoped_entry(conn, "manual_tasks", task_id, principal)
        require_permission(principal, "comments.manage" if row["task_type"] == "comment" else "reels.publish")
        allowed = {"awaiting_manual": {"completed", "cancelled"},
                   "queued_experimental": {"cancelled"},
                   "submitted_unverified": {"posted", "pending_approval", "failed"},
                   "needs_review": {"posted", "pending_approval", "failed"},
                   "failed": {"completed"},
                   "pending_approval": {"posted", "failed"}}
        if data.status not in allowed.get(row["status"], set()):
            raise HTTPException(409, "Invalid task status transition; uncertain jobs must not auto-retry")
        conn.execute("UPDATE manual_tasks SET status=?, updated_at=? WHERE id=?",
                     (data.status, iso(), task_id))
    audit("manual_task", task_id, f"Updated task status to {data.status}")
    return {"ok": True}


@app.get("/api/manual-media/{filename:path}")
def serve_manual_video(filename: str, principal: Principal = Depends(require_principal)):
    require_any_permission(principal, ("reels.create", "history.view"))
    with db() as conn:
        file_path, name = media_file(conn, filename, principal)
    return FileResponse(file_path, media_type="video/mp4" if name.endswith(".mp4") else "video/quicktime")


@app.get("/api/audits")
def audits(_: Principal = Depends(require_admin)):
    with db() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM audits ORDER BY id DESC LIMIT 100")]


@app.get("/api/evidence/{filename}")
def serve_evidence(filename: str, principal: Principal = Depends(require_principal)):
    require_permission(principal, "history.view")
    matched = re.fullmatch(r"job-([0-9]+)\.png", filename)
    if not matched:
        raise HTTPException(404, "Not found")
    with db() as conn:
        scoped_entry(conn, "jobs", int(matched.group(1)), principal)
    path = EVIDENCE / filename
    if not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


def claim_next_job():
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cycle_job = cycle_runtime.claim(conn,now(),lambda aid: profile_busy(DATA,aid) or aid in IMPORT_IN_PROGRESS)
        if cycle_job is not None:
            return cycle_job
        cycle_exclusion = ("AND NOT EXISTS (SELECT 1 FROM auto_cycle_targets t WHERE t.job_id=j.id AND t.pipeline_id=j.pipeline_id)"
                           if cycle_repository.available(conn) else "")
        row = conn.execute(f"""SELECT j.*, g.url group_url, g.enabled group_enabled,
                g.group_key, g.approved_target, g.requires_approval, g.status group_status,
                p.body, p.media_json, p.anonymous_only, p.pipeline_id effective_pipeline_id,
                l.enabled pipeline_enabled, a.enabled account_enabled, a.login_pid
            FROM jobs j JOIN groups g ON j.group_id=g.id JOIN posts p ON j.post_id=p.id
            JOIN pipelines l ON p.pipeline_id=l.id JOIN accounts a ON a.id=j.account_id
            WHERE j.state='queued' AND j.scheduled_at<=?
              {cycle_exclusion}
              AND p.pipeline_id=g.pipeline_id AND p.pipeline_id=a.pipeline_id
              AND (j.pipeline_id IS NULL OR j.pipeline_id=p.pipeline_id)
              AND (j.is_verification=1
                   OR g.screening_status NOT IN ('restricted_confirmed','rejected'))
              AND g.manual_review=0
            ORDER BY (CASE g.screening_status WHEN 'verified_posted' THEN 0
                                               WHEN 'pending_group_approval' THEN 1
                                               ELSE 2 END),
                     j.scheduled_at, j.id LIMIT 1""", (iso(),)).fetchone()
        if not row:
            return None
        job = dict(row)
        job["pipeline_id"] = job.pop("effective_pipeline_id")
        if not job["anonymous_only"] or not json.loads(job["media_json"]):
            group_job_lifecycle.mark_blocked(conn, job, "needs_review", "Anonymous photo-only policy not met; not posted", iso())
            return None
        # Quyết định theo đúng cặp Facebook–Group, không dựa trên cột chung
        # của group.
        decision = group_eligibility.pair_decision(conn, job["pipeline_id"],
                                                    job["account_id"], job["group_id"])
        if not decision["eligible"] and not job.get("is_verification"):
            group_job_lifecycle.mark_blocked(conn, job, "needs_review",
                                              f"Chưa xác minh được quyền đăng: {decision['reason']}"[:500], iso())
            return None
        if not job["pipeline_enabled"] or not job["account_enabled"] or not job["group_enabled"] \
                or not job["approved_target"] or job.get("group_status") != "active":
            conn.execute("UPDATE jobs SET state='cancelled',details='Target disabled or permission not confirmed',updated_at=? WHERE id=?", (iso(), job["id"]))
            return None
        with IMPORT_LOCK:
            importing = job["account_id"] in IMPORT_IN_PROGRESS
        if importing or pid_active(job["login_pid"], job["account_id"]):
            conn.execute("UPDATE jobs SET scheduled_at=?,details='Account browser busy',updated_at=? WHERE id=?",
                         (iso(now() + timedelta(minutes=5)), iso(), job["id"]))
            return None
        since = iso(now() - timedelta(days=1))
        daily = conn.execute("""SELECT COUNT(*) FROM jobs WHERE account_id=? AND id<>? AND state IN
                ('posted','submitted_unverified','pending_approval','needs_review') AND updated_at>=?""",
                (job["account_id"], job["id"], since)).fetchone()[0]
        reel_daily = conn.execute("""SELECT COUNT(*) FROM manual_tasks
            WHERE account_id=? AND auto_publish=1 AND status IN
            ('posted','submitted_unverified','pending_approval','needs_review') AND updated_at>=?""",
            (job["account_id"], since)).fetchone()[0]
        if daily + reel_daily >= MAX_DAILY_POSTS:
            conn.execute("UPDATE jobs SET scheduled_at=?,details='Daily account limit',updated_at=? WHERE id=?",
                         (iso(now() + timedelta(hours=6)), iso(), job["id"]))
            return None
        last_account = conn.execute("""SELECT updated_at FROM jobs WHERE account_id=? AND id<>? AND state IN
            ('posted','submitted_unverified','pending_approval','needs_review','running') ORDER BY updated_at DESC LIMIT 1""",
            (job["account_id"], job["id"])).fetchone()
        last_reel = conn.execute("""SELECT updated_at FROM manual_tasks
            WHERE account_id=? AND auto_publish=1 AND status IN
            ('posted','submitted_unverified','pending_approval','needs_review','running')
            ORDER BY updated_at DESC LIMIT 1""", (job["account_id"],)).fetchone()
        recent_times = [parse_date(item["updated_at"]) for item in (last_account, last_reel) if item]
        if recent_times:
            earliest = max(recent_times) + timedelta(seconds=MIN_ACCOUNT_GAP_SECONDS)
            if earliest > now():
                conn.execute("UPDATE jobs SET scheduled_at=?,details='Account interval',updated_at=? WHERE id=?",
                             (iso(earliest), iso(), job["id"]))
                return None
        last_group = conn.execute("""SELECT j.updated_at FROM jobs j JOIN groups g ON j.group_id=g.id
            WHERE g.group_key=? AND j.id<>? AND j.state IN
            ('posted','submitted_unverified','pending_approval','needs_review','running') ORDER BY j.updated_at DESC LIMIT 1""",
            (job["group_key"], job["id"])).fetchone()
        if last_group:
            earliest = parse_date(last_group["updated_at"]) + timedelta(hours=MIN_GROUP_GAP_HOURS)
            if earliest > now():
                conn.execute("UPDATE jobs SET scheduled_at=?,details='Group frequency limit',updated_at=? WHERE id=?",
                             (iso(earliest), iso(), job["id"]))
                return None
        conn.execute("UPDATE jobs SET state='running',attempts=attempts+1,started_at=?,updated_at=? WHERE id=?",
                     (iso(), iso(), job["id"]))
        conn.execute("UPDATE auto_post_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE post_id=? AND pipeline_id=?",
                     (iso(), job["post_id"], job["pipeline_id"]))
        return job


def job_ownership_ok(job_id: int) -> bool:
    """The post, group, account and job must all belong to one pipeline."""
    with db() as conn:
        row = conn.execute("""SELECT p.pipeline_id post_pipeline, g.pipeline_id group_pipeline,
                a.pipeline_id account_pipeline, j.pipeline_id job_pipeline
            FROM jobs j JOIN posts p ON j.post_id=p.id JOIN groups g ON j.group_id=g.id
            JOIN accounts a ON a.id=j.account_id WHERE j.id=?""", (job_id,)).fetchone()
    if row is None:
        return False
    owners = {row["post_pipeline"], row["group_pipeline"], row["account_pipeline"]}
    if row["job_pipeline"] is not None:
        owners.add(row["job_pipeline"])
    return len(owners) == 1


def _needs_login(details: str) -> bool:
    """True when Facebook asked the account to log in again."""
    text = str(details or "").lower()
    return ("login or checkpoint" in text or "yêu cầu đăng nhập" in text
            or "màn hình đăng nhập" in text or "checkpoint" in text)


def execute_job(job: dict):
    with db() as conn:
        if not cycle_runtime.before_execute(conn,job,now()):
            return
    if not job_ownership_ok(job["id"]):
        with db() as conn:
            conn.execute("""UPDATE jobs SET state='needs_review',
                            details='Refused: job, post, group and account belong to different pipelines',
                            updated_at=? WHERE id=?""", (iso(), job["id"]))
        audit("job", job["id"], "Refused cross-pipeline job")
        return
    result_file = DATA / f"job-result-{job['id']}-{secrets.token_hex(6)}.json"
    payload_file = DATA / f"job-request-{job['id']}-{secrets.token_hex(6)}.json"
    try:
        payload = group_job_lifecycle.worker_payload(job, DATA)
    except (ValueError, KeyError, TypeError):
        with db() as conn:
            group_job_lifecycle.mark_blocked(conn, job, "needs_review", "Không xác minh được ảnh thuộc Workspace; chưa gửi bài.", iso())
        return
    payload_file.write_text(json.dumps(payload), encoding="utf-8")
    payload_file.chmod(0o600)
    result = {"state": "needs_review", "details": "Worker result unavailable; check Facebook before retrying", "evidence": ""}
    try:
        proc = subprocess.run([sys.executable, str(ROOT / "fb_browser.py"), "post", str(payload_file), str(result_file)],
                              cwd=ROOT, env={**os.environ, "DATA_DIR": str(DATA), "DISPLAY": ":99"},
                              capture_output=True, text=True, timeout=210)
        if result_file.exists():
            result = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            result["details"] = f"Worker exit {proc.returncode}: {proc.stderr[-300:]}"
    except subprocess.TimeoutExpired:
        result["details"] = "Browser timed out; verify group for duplicate before creating new post"
    except Exception as error:
        result["details"] = f"Worker exception: {type(error).__name__}: {error}"
    finally:
        payload_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
    status = group_job_lifecycle.validated_result_state(result, job)
    identity = str(result.get("identity", ""))[:120]
    verification = str(result.get("verification", ""))[:40] or "unverified"
    posting_status = {"posted": "posted", "submitted_unverified": "submitted_unverified",
                      "pending_approval": "pending_approval",
                      "needs_review": "needs_review", "failed": "failed"}.get(status, "pending")
    permalink = str(result.get("permalink", "") or "")[:500]
    published_at = iso() if posting_status == "posted" else None
    with db() as conn:
        conn.execute("""UPDATE jobs SET state=?,details=?,evidence=?,updated_at=?,
                        actual_posting_identity=?,verification_status=?,posting_status=?,failure_reason=?,
                        permalink=?,published_at=COALESCE(published_at,?)
                        WHERE id=?""",
                     (status, str(result.get("details", ""))[:1000], str(result.get("evidence", ""))[:400],
                      iso(), identity, verification, posting_status,
                      str(result.get("details", ""))[:500] if status in ("failed", "needs_review") else "",
                      permalink, published_at, job["id"]))
        conn.execute("""UPDATE auto_post_jobs SET
                        status=?, error=?, defer_reason=?, evidence=?, permalink=?, updated_at=?
                        WHERE post_id=? AND pipeline_id=? AND account_id=? AND group_id=?""",
                     (status, str(result.get("details", ""))[:500] if status in ("failed", "needs_review") else "",
                      str(result.get("details", ""))[:500] if status == "needs_review" else "",
                      str(result.get("evidence", ""))[:400],
                      permalink, iso(), job["post_id"], job["pipeline_id"], job["account_id"], job["group_id"]))
        account_session_state.record_worker_session(conn, job["pipeline_id"], job["account_id"], result, iso())
        analytics_worker.sync_attribution(conn, "group_post", job["id"],
                                          published_by=job.get("account_id") if status == "posted" else None,
                                          pipeline_id=job["pipeline_id"])
        settings = posting_limits.settings_of(conn, job["pipeline_id"])
        day = posting_limits.day_of(posting_limits.now_in(settings.get("timezone")))
        if posting_status == "posted":
            posting_limits.confirm(conn, job["account_id"], job["group_id"], day)
        elif posting_status in ("failed", "needs_review"):
            posting_limits.release(conn, job["account_id"], job["group_id"], day)
        cycle_runtime.reconcile(conn)
    with db() as conn:
        refresh_screening_for_job(conn, job["id"])
    audit("job", job["id"], f"Browser job result {status}: {str(result.get('details', ''))[:160]}")


@app.get("/api/reel-evidence/{task_id}")
def reel_evidence(task_id: int, principal: Principal = Depends(require_principal)):
    require_permission(principal, "history.view")
    with db() as conn:
        task = scoped_entry(conn, "manual_tasks", task_id, principal)
        if task["evidence"] != f"reel-{task_id}.png":
            raise HTTPException(404, "Evidence not found")
    path = EVIDENCE / task["evidence"]
    if not path.is_file():
        raise HTTPException(404, "Evidence not found")
    return FileResponse(path)


def claim_next_reel():
    """Claim one due reel; no auto-retry after any uncertain publication attempt."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""SELECT t.*,p.enabled pipeline_enabled,a.enabled account_enabled,
            a.login_pid FROM manual_tasks t
            JOIN pipelines p ON p.id=t.pipeline_id
            JOIN accounts a ON a.id=t.account_id
            WHERE t.task_type='reel' AND t.auto_publish=1 AND t.status='queued_experimental'
              AND t.scheduled_at<=? AND t.pipeline_id=p.id AND t.pipeline_id=a.pipeline_id
              AND (t.group_id IS NULL OR t.group_id IN (SELECT id FROM groups WHERE pipeline_id=t.pipeline_id))
              ORDER BY t.scheduled_at,t.id LIMIT 1""", (iso(),)).fetchone()
        if row is None:
            return None
        task = dict(row)
        if not task["pipeline_enabled"] or not task["account_enabled"]:
            conn.execute("UPDATE manual_tasks SET status='cancelled',details='Pipeline or account disabled',updated_at=? WHERE id=?", (iso(), task["id"]))
            return None
        with IMPORT_LOCK:
            importing = task["account_id"] in IMPORT_IN_PROGRESS
        if importing or pid_active(task["login_pid"], task["account_id"]):
            conn.execute("UPDATE manual_tasks SET scheduled_at=?,details='Account browser busy',updated_at=? WHERE id=?", (iso(now()+timedelta(minutes=5)), iso(), task["id"]))
            return None
        since = iso(now()-timedelta(days=1))
        group_count = conn.execute("""SELECT COUNT(*) FROM jobs WHERE account_id=? AND state IN
            ('posted','submitted_unverified','pending_approval','needs_review') AND updated_at>=?""",
            (task["account_id"],since)).fetchone()[0]
        reels_count = conn.execute("""SELECT COUNT(*) FROM manual_tasks WHERE account_id=? AND id<>?
            AND auto_publish=1 AND status IN ('submitted_unverified','needs_review','posted','pending_approval')
            AND updated_at>=?""",(task["account_id"],task["id"],since)).fetchone()[0]
        if group_count + reels_count >= MAX_DAILY_POSTS:
            conn.execute("UPDATE manual_tasks SET scheduled_at=?,details='Daily account cap',updated_at=? WHERE id=?", (iso(now()+timedelta(hours=6)), iso(), task["id"]))
            return None
        # Serialize all browser jobs and apply minimum account gap.
        prev_group=conn.execute("""SELECT updated_at FROM jobs WHERE account_id=? AND state IN
            ('posted','submitted_unverified','pending_approval','needs_review','running')
            ORDER BY updated_at DESC LIMIT 1""",(task["account_id"],)).fetchone()
        prev_reel=conn.execute("""SELECT updated_at FROM manual_tasks WHERE account_id=? AND id<>?
            AND auto_publish=1 AND status IN ('submitted_unverified','needs_review','posted','pending_approval','running')
            ORDER BY updated_at DESC LIMIT 1""",(task["account_id"],task["id"])).fetchone()
        previous=[parse_date(r["updated_at"]) for r in (prev_group,prev_reel) if r]
        if previous:
            earliest=max(previous)+timedelta(seconds=MIN_ACCOUNT_GAP_SECONDS)
            if earliest>now():
                conn.execute("UPDATE manual_tasks SET scheduled_at=?,details='Account interval',updated_at=? WHERE id=?", (iso(earliest),iso(),task["id"]))
                return None
        conn.execute("UPDATE manual_tasks SET status='running',updated_at=? WHERE id=?",(iso(),task["id"]))
        return task


def execute_reel(task: dict):
    task_id=task["id"]
    payload_file=DATA / f"reel-request-{task_id}-{secrets.token_hex(6)}.json"
    result_file=DATA / f"reel-result-{task_id}-{secrets.token_hex(6)}.json"
    videos=json.loads(task["media_json"])
    payload={"account_id":task["account_id"], "task_id":task_id,
             "body":task["body"],"media":[str(DATA / path) for path in videos]}
    payload_file.write_text(json.dumps(payload),encoding="utf-8")
    payload_file.chmod(0o600)
    result={"state":"needs_review","details":"Worker result unavailable; verify profile before retrying","evidence":""}
    try:
        proc=subprocess.run([sys.executable,str(ROOT / "fb_browser.py"),"reel",str(payload_file),str(result_file)],
            cwd=ROOT,env={**os.environ,"DATA_DIR":str(DATA),"DISPLAY":":99"},capture_output=True,text=True,timeout=240)
        if result_file.exists():
            result=json.loads(result_file.read_text(encoding="utf-8"))
        else:
            result["details"]=f"Worker exited {proc.returncode}; inspect profile before retrying"
    except subprocess.TimeoutExpired:
        result["details"]="Reel worker timed out; verify Facebook before retrying"
    except Exception as error:
        result["details"]=f"Worker {type(error).__name__}; verify Facebook before retrying"
    finally:
        payload_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
    status=result.get("state","needs_review")
    if status not in ("failed","needs_review","submitted_unverified"):
        status="needs_review"
    evidence=result.get("evidence","")
    if evidence != f"reel-{task_id}.png":
        evidence=""
    permalink = str(result.get("permalink", "") or "")[:500]
    with db() as conn:
        conn.execute("""UPDATE manual_tasks SET status=?,details=?,evidence=?,updated_at=?,
                        permalink=?,published_at=COALESCE(published_at,?) WHERE id=?""",
            (status,str(result.get("details",""))[:1000],evidence,iso(),permalink,
             iso() if status in ("posted", "submitted_unverified") else None,task_id))
        analytics_worker.sync_attribution(conn, "reel", task_id, published_by=task.get("account_id"))
    audit("reel",task_id,f"Browser result {status}; unverified until checked")


# --- admin analytics -----------------------------------------------------------
def analytics_filters(days: int | None = None, custom_from: str | None = None,
                      custom_to: str | None = None, account_id: int | None = None,
                      ctv_id: int | None = None, content_type: str | None = None,
                      status: str | None = None):
    """Read the filters of a report."""
    start, end = analytics_service.day_range(days, custom_from, custom_to)
    return {
        "days": days, "from": custom_from, "to": custom_to,
        "account_id": account_id, "ctv_id": ctv_id,
        "content_type": content_type, "status": status,
        "start": start, "end": end,
    }


def analytics_rows(conn, filters, pipeline_id: int | None = None, only_ctv: int | None = None):
    """The content of a report with its attribution."""
    rows = analytics_service.content_rows(
        conn, pipeline_id=pipeline_id, account_id=filters["account_id"],
        ctv_id=filters["ctv_id"] or only_ctv, content_type=filters["content_type"],
        status=filters["status"], start=filters["start"], end=filters["end"])
    analytics_service._attach_metric_sums(rows)
    return rows


def analytics_top_posts(rows, limit: int = 10) -> list[dict]:
    """The posts with the best measured result."""
    ranked = [row for row in rows if row.get("engagement") and row["engagement"].get("total") is not None]
    ranked.sort(key=lambda row: row["engagement"]["total"], reverse=True)
    items = []
    for row in ranked[:limit]:
        items.append(analytics_post_payload(row))
    if not items:
        # Chưa có chỉ số nào thì vẫn hiện những bài mới nhất, ở trạng thái
        # "chưa có dữ liệu" thay vì số 0.
        for row in rows[:limit]:
            items.append(analytics_post_payload(row))
    return items


def analytics_post_payload(row) -> dict:
    """One post of a report."""
    engagement = row.get("engagement") or {}
    body = (row.get("body") or "").strip()
    return {
        "content_type": row["content_type"],
        "post_id": row["post_id"],
        "title": (body[:120] or f"Bài #{row['post_id']}"),
        "group_label": row.get("group_label"),
        "account_label": row.get("account_label"),
        "permalink": row.get("permalink") or "",
        "published_at": row.get("published_at") or row.get("created_at"),
        "created_at": row.get("created_at"),
        "status": row.get("posting_status"),
        "created_by": row.get("created_by"),
        "reactions": engagement.get("reactions"),
        "comments": engagement.get("comments"),
        "shares": engagement.get("shares"),
        "views": row.get("views"),
        "reach": row.get("reach"),
        "engagement": engagement.get("total"),
        "measured": bool(row.get("measured")),
        "removed": bool(row.get("removed")),
    }


def analytics_kpis(rows) -> dict:
    """The numbers of the top of a report."""
    summary = analytics_service.summarize_rows(rows)
    return {
        "total": summary["total"],
        "posts_published": summary["posted"],
        "posts_waiting": summary["waiting"],
        "posts_refused": summary["refused"],
        "posts_removed": summary["removed"],
        "submitted_unverified": summary["submitted_unverified"],
        "group_posts": summary["group_posts"],
        "group_posts_posted": summary["group_posts_posted"],
        "reels": summary["reels"],
        "reels_posted": summary["reels_posted"],
        "engagement": summary["engagement"],
        "reactions": summary["reactions"],
        "comments": summary["comments"],
        "shares": summary["shares"],
        "views": summary["views"],
        "reach": summary["reach"],
        "measured_posts": summary["measured_posts"],
        "unmeasured_posts": summary["unmeasured_posts"],
        "engagement_available": summary["engagement"] is not None,
        "views_available": summary["views"] is not None,
    }


@app.get("/api/admin/analytics/overview")
def admin_analytics_overview(principal: Principal = Depends(require_admin)):
    """The numbers of the whole system for the dashboard of the administrator."""
    filters = analytics_filters(days=90)
    with db() as conn:
        rows = analytics_rows(conn, filters)
        active_pipelines = conn.execute(
            "SELECT COUNT(*) FROM pipelines WHERE enabled=1").fetchone()[0]
        active_collaborators = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='ctv' AND status='active'").fetchone()[0]
    kpis = analytics_kpis(rows)
    kpis["pipelines_active"] = active_pipelines
    kpis["collaborators_active"] = active_collaborators
    return {"ok": True, "kpis": kpis, "range": {"days": filters["days"]},
            "sources": analytics_sources()}


def analytics_sources() -> dict:
    """Which sources of measurement are available."""
    from services import engagement_collector

    return {"graph_api": engagement_collector.graph_configured(),
            "facebook_session": engagement_collector.browser_configured()}


@app.get("/api/admin/analytics/pipelines")
def admin_analytics_pipelines(days: int = 30, principal: Principal = Depends(require_admin)):
    """One line per pipeline with its numbers."""
    filters = analytics_filters(days=days)
    with db() as conn:
        pipelines = conn.execute("SELECT id,name,description,enabled,created_at FROM pipelines"
                                  " ORDER BY id").fetchall()
        rows = analytics_rows(conn, filters)
        items = []
        for pipeline in pipelines:
            own = [row for row in rows if row["pipeline_id"] == pipeline["id"]]
            summary = analytics_service.summarize_rows(own)
            collaborators = conn.execute(
                """SELECT COUNT(*) FROM pipeline_memberships m JOIN users u ON u.id=m.user_id
                   WHERE m.pipeline_id=? AND u.status='active'""", (pipeline["id"],)).fetchone()[0]
            last = max((row.get("published_at") or row.get("created_at") or "" for row in own),
                       default="")
            items.append({
                "pipeline_id": pipeline["id"], "name": pipeline["name"],
                "description": pipeline["description"], "enabled": bool(pipeline["enabled"]),
                "collaborators": collaborators,
                "posts_published": summary["posted"],
                "posts_waiting": summary["waiting"],
                "posts_refused": summary["refused"],
                "posts_removed": summary["removed"],
                "engagement": summary["engagement"],
                "views": summary["views"],
                "measured_posts": summary["measured_posts"],
                "last_activity": last or None,
            })
    return {"ok": True, "pipelines": items, "range": {"days": filters["days"]}}


@app.get("/api/admin/analytics/pipelines/{pipeline_id}")
def admin_analytics_pipeline(pipeline_id: int, days: int = 30, custom_from: str | None = None,
                              custom_to: str | None = None, account_id: int | None = None,
                              ctv_id: int | None = None, content_type: str | None = None,
                              status: str | None = None,
                              principal: Principal = Depends(require_admin)):
    """The report of one pipeline."""
    filters = analytics_filters(days, custom_from, custom_to, account_id, ctv_id, content_type, status)
    with db() as conn:
        pipeline = conn.execute("SELECT id,name,description,enabled,created_at FROM pipelines WHERE id=?",
                                (pipeline_id,)).fetchone()
        if pipeline is None:
            raise HTTPException(404, "Not found")
        rows = analytics_rows(conn, filters, pipeline_id=pipeline_id)
        collaborators = []
        for member in conn.execute("""SELECT u.id, u.username, u.status FROM pipeline_memberships m
                                       JOIN users u ON u.id=m.user_id WHERE m.pipeline_id=?
                                       ORDER BY u.username""", (pipeline_id,)).fetchall():
            own = [row for row in rows if row.get("created_by") == member["id"]]
            summary = analytics_service.summarize_rows(own)
            collaborators.append({
                "user_id": member["id"], "username": member["username"], "status": member["status"],
                "posts_created": summary["total"], "posts_published": summary["posted"],
                "posts_waiting": summary["waiting"], "posts_refused": summary["refused"],
                "engagement": summary["engagement"], "views": summary["views"],
                "measured_posts": summary["measured_posts"],
            })
        accounts = conn.execute("""SELECT a.id,a.label FROM accounts a WHERE a.pipeline_id=?
                                    ORDER BY a.label""", (pipeline_id,)).fetchall()
    return {
        "ok": True,
        "pipeline": {"id": pipeline["id"], "name": pipeline["name"],
                     "description": pipeline["description"], "enabled": bool(pipeline["enabled"])},
        "kpis": analytics_kpis(rows),
        "timeseries": analytics_service.timeseries(rows, filters["days"] or 30),
        "top_posts": analytics_top_posts(rows, 10),
        "collaborators": collaborators,
        "accounts": [{"id": row["id"], "label": row["label"]} for row in accounts],
        "filters": {"days": filters["days"], "from": filters["from"], "to": filters["to"],
                    "account_id": filters["account_id"], "ctv_id": filters["ctv_id"],
                    "content_type": filters["content_type"], "status": filters["status"]},
        "sources": analytics_sources(),
    }


@app.get("/api/admin/analytics/collaborators")
def admin_analytics_collaborators(days: int = 30, pipeline_id: int | None = None,
                                   principal: Principal = Depends(require_admin)):
    """The ranking of the collaborators."""
    filters = analytics_filters(days=days)
    with db() as conn:
        rows = analytics_rows(conn, filters, pipeline_id=pipeline_id)
        members = conn.execute("""SELECT u.id,u.username,u.status,m.pipeline_id,p.name pipeline_name
                                    FROM users u LEFT JOIN pipeline_memberships m ON m.user_id=u.id
                                    LEFT JOIN pipelines p ON p.id=m.pipeline_id
                                   WHERE u.role='ctv' ORDER BY u.username""").fetchall()
        items = []
        for member in members:
            own = [row for row in rows if row.get("created_by") == member["id"]]
            summary = analytics_service.summarize_rows(own)
            created = summary["total"]
            items.append({
                "user_id": member["id"], "username": member["username"], "status": member["status"],
                "pipeline_id": member["pipeline_id"], "pipeline_name": member["pipeline_name"],
                "posts_created": created, "posts_published": summary["posted"],
                "posts_waiting": summary["waiting"], "posts_refused": summary["refused"],
                "posts_removed": summary["removed"],
                "engagement": summary["engagement"], "views": summary["views"],
                "measured_posts": summary["measured_posts"],
                "success_rate": (round(summary["posted"] / created * 100, 1)
                                 if created else None),
                "last_activity": max((row.get("published_at") or row.get("created_at") or "")
                                     for row in own) if own else None,
            })
    items.sort(key=lambda item: (item["posts_published"], item["engagement"] or 0), reverse=True)
    return {"ok": True, "collaborators": items, "range": {"days": filters["days"]}}


@app.get("/api/admin/analytics/collaborators/{user_id}")
def admin_analytics_collaborator(user_id: int, days: int = 30, custom_from: str | None = None,
                                  custom_to: str | None = None, content_type: str | None = None,
                                  status: str | None = None,
                                  principal: Principal = Depends(require_admin)):
    """The report of one collaborator."""
    filters = analytics_filters(days, custom_from, custom_to, None, None, content_type, status)
    with db() as conn:
        member = conn.execute("""SELECT u.id,u.username,u.status,m.pipeline_id,p.name pipeline_name
                                    FROM users u LEFT JOIN pipeline_memberships m ON m.user_id=u.id
                                    LEFT JOIN pipelines p ON p.id=m.pipeline_id
                                   WHERE u.id=? AND u.role='ctv'""", (user_id,)).fetchone()
        if member is None:
            raise HTTPException(404, "Not found")
        rows = analytics_rows(conn, filters, pipeline_id=member["pipeline_id"], only_ctv=user_id)
    return {
        "ok": True,
        "collaborator": {"user_id": member["id"], "username": member["username"],
                         "status": member["status"], "pipeline_id": member["pipeline_id"],
                         "pipeline_name": member["pipeline_name"]},
        "kpis": analytics_kpis(rows),
        "timeseries": analytics_service.timeseries(rows, filters["days"] or 30),
        "top_posts": analytics_top_posts(rows, 10),
        "sources": analytics_sources(),
    }


@app.get("/api/admin/analytics/top-posts")
def admin_analytics_top_posts(days: int = 30, pipeline_id: int | None = None,
                               limit: int = 10, content_type: str | None = None,
                               principal: Principal = Depends(require_admin)):
    """The posts with the best measured result."""
    filters = analytics_filters(days=days, content_type=content_type)
    with db() as conn:
        rows = analytics_rows(conn, filters, pipeline_id=pipeline_id)
    return {"ok": True, "posts": analytics_top_posts(rows, min(50, max(1, limit))),
            "range": {"days": filters["days"]}}


@app.get("/api/admin/analytics/timeseries")
def admin_analytics_timeseries(days: int = 30, pipeline_id: int | None = None,
                                ctv_id: int | None = None, content_type: str | None = None,
                                principal: Principal = Depends(require_admin)):
    """The trend of the published posts and the measured interaction."""
    filters = analytics_filters(days=days, ctv_id=ctv_id, content_type=content_type)
    with db() as conn:
        rows = analytics_rows(conn, filters, pipeline_id=pipeline_id)
    return {"ok": True, "series": analytics_service.timeseries(rows, filters["days"] or 30),
            "range": {"days": filters["days"]}}


@app.get("/api/analytics/me")
def analytics_me(days: int = 30, principal: Principal = Depends(require_principal)):
    """The numbers of the collaborator who is signed in.

    Chỉ Workspace của CTV đó và chỉ những bài họ tạo; báo cáo của Workspace
    khác không bao giờ trả lời.
    """
    require_permission(principal, "analytics.view")
    filters = analytics_filters(days=days)
    with db() as conn:
        rows = analytics_rows(conn, filters, pipeline_id=principal.pipeline_id,
                              only_ctv=principal.user_id)
        pipeline = conn.execute("SELECT id,name FROM pipelines WHERE id=?",
                                (principal.pipeline_id,)).fetchone()
    return {"ok": True, "pipeline": {"id": pipeline["id"], "name": pipeline["name"]}
            if pipeline else None,
            "kpis": analytics_kpis(rows),
            "timeseries": analytics_service.timeseries(rows, filters["days"] or 30),
            "top_posts": analytics_top_posts(rows, 10)}


@app.post("/api/admin/analytics/collect")
def admin_analytics_collect(principal: Principal = Depends(require_admin)):
    """Chạy bộ thu thập ngay, không đợi đến lượt hẹn."""
    done = analytics_worker.run_once()
    return {"ok": True, "collected": done}


def analytics_thread():
    """Measure the published posts again and again.

    The queue holds one line per post, so a run that repeats itself never
    doubles the work. A post that cannot be measured keeps its line and the
    reason; it is never counted as zero.
    """
    from services import engagement_collector

    while not STOP_EVENT.is_set():
        try:
            analytics_worker_run()
        except Exception:
            LOG.exception("Analytics worker unexpected failure")
        STOP_EVENT.wait(max(60, engagement_collector.INTERVAL_MINUTES * 60))


def analytics_worker_run():
    """One round of the analytics worker."""
    from services import engagement_collector

    with db() as conn:
        analytics_worker.enqueue_published(conn)
    with db() as conn:
        return analytics_worker.collect_pending(conn, engagement_collector.BATCH_SIZE)


def scheduler():
    while not STOP_EVENT.is_set():
        try:
            job = claim_next_job()
            if job:
                execute_job(job)
                continue
            reel = claim_next_reel()
            if reel:
                execute_reel(reel)
                continue
        except Exception:
            LOG.exception("Scheduler unexpected failure")
        STOP_EVENT.wait(10)
