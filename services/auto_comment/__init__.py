"""Auto Comment system for Facebook Group and News Feed.

This package provides a complete auto-comment system with:
- Group and News Feed source management
- AI-powered comment generation
- Community Standards policy checking
- Duplicate detection with fingerprinting
- Smart scheduling with per-account limits
- Background worker for job processing

All state is stored in the Pipeline database (Turso/SQLite).
"""
from __future__ import annotations

from services.auto_comment.group_sources import (
    GroupSource,
    load_group_sources,
    add_group_source,
    update_group_source,
    delete_group_source,
    validate_group_accessible,
    fetch_latest_posts as fetch_group_posts,
)

from services.auto_comment.client import (
    RekognitionClient,
    RekognitionConfig,
    load_config as load_aws_config,
    client_from_env,
)

from services.auto_comment.feed_sources import (
    FeedSource,
    load_feed_sources,
    add_feed_source,
    update_feed_source,
    delete_feed_source,
    matches_feed_filters,
    fetch_news_feed_posts,
)

from services.auto_comment.comment_generator import (
    CommentCandidate,
    GenerationResult,
    generate_comment_candidates,
    select_best_candidate,
    generate_single_comment,
)

from services.auto_comment.policy_checker import (
    PolicyVerdict,
    PolicyCheckResult,
    check_comment_policy,
    basic_spam_check,
)

from services.auto_comment.duplicate_guard import (
    CommentFingerprint,
    MAX_USES_PER_COMMENT,
    normalize_comment,
    compute_content_hash,
    is_similar,
    check_duplicate_limit,
    register_comment_use,
    get_fingerprint_stats,
)

from services.auto_comment.scheduler import (
    AutoCommentSettings,
    AutoCommentScheduler,
    CommentJob,
    AccountDailyLimits,
    DEFAULT_MIN_INTERVAL_SECONDS,
    Status,
    JobStatus,
    Mode,
    create_comment_job,
    get_ready_jobs,
    update_job_status,
    mark_job_posted,
    mark_job_failed,
    mark_job_submitted_unverified,
    increment_account_limit,
    check_account_restricted,
)

from services.auto_comment.worker import (
    AutoCommentWorker,
    start_worker,
    stop_worker,
    scan_and_create_jobs,
)

__all__ = [
    # Group sources
    "GroupSource",
    "load_group_sources",
    "add_group_source",
    "update_group_source",
    "delete_group_source",
    "validate_group_accessible",
    "fetch_group_posts",
    
    # Client
    "RekognitionClient",
    "RekognitionConfig",
    "load_aws_config",
    "client_from_env",
    
    # Feed sources
    "FeedSource",
    "load_feed_sources",
    "add_feed_source",
    "update_feed_source",
    "delete_feed_source",
    "matches_feed_filters",
    "fetch_news_feed_posts",
    
    # Comment generation
    "CommentCandidate",
    "GenerationResult",
    "generate_comment_candidates",
    "select_best_candidate",
    "generate_single_comment",
    
    # Policy checking
    "PolicyVerdict",
    "PolicyCheckResult",
    "check_comment_policy",
    "basic_spam_check",
    
    # Duplicate guard
    "CommentFingerprint",
    "MAX_USES_PER_COMMENT",
    "normalize_comment",
    "compute_content_hash",
    "is_similar",
    "check_duplicate_limit",
    "register_comment_use",
    "get_fingerprint_stats",
    
    # Scheduler
    "AutoCommentSettings",
    "AutoCommentScheduler",
    "CommentJob",
    "AccountDailyLimits",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "Status",
    "JobStatus",
    "Mode",
    "create_comment_job",
    "get_ready_jobs",
    "update_job_status",
    "mark_job_posted",
    "mark_job_failed",
    "mark_job_submitted_unverified",
    "increment_account_limit",
    "check_account_restricted",
    
    # Worker
    "AutoCommentWorker",
    "start_worker",
    "stop_worker",
    "scan_and_create_jobs",
]