"""Auto Comment: Background worker for processing scheduled comment jobs."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from app import db, iso, STOP_EVENT

from services.auto_comment.scheduler import (
    AutoCommentSettings,
    AutoCommentScheduler,
    CommentJob,
    JobStatus,
    Mode,
    create_comment_job,
    get_ready_jobs,
    mark_job_failed,
    mark_job_posted,
    mark_job_submitted_unverified,
    increment_account_limit,
    check_account_restricted,
)
from services.auto_comment.group_sources import (
    load_group_sources,
    fetch_latest_posts as fetch_group_posts,
)
from services.auto_comment.feed_sources import (
    load_feed_sources,
    fetch_news_feed_posts,
    matches_feed_filters,
)
from services.auto_comment.comment_generator import (
    generate_single_comment,
)
from services.auto_comment.policy_checker import (
    check_comment_policy,
    basic_spam_check,
)
from services.auto_comment.duplicate_guard import (
    check_duplicate_limit,
    register_comment_use,
)
from services import ai_provider

LOG = logging.getLogger("facebook-pipeline.auto-comment.worker")


class AutoCommentWorker:
    """Background worker that processes auto-comment jobs."""
    
    def __init__(self, pipeline_id: int):
        self.pipeline_id = pipeline_id
        self.scheduler = AutoCommentScheduler(pipeline_id)
        self._stop_event = threading.Event()
        self._worker_thread = None
    
    def start(self):
        """Start the worker thread."""
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._run_loop,
            name=f"auto-comment-worker-{self.pipeline_id}",
            daemon=True,
        )
        self._worker_thread.start()
        LOG.info("Auto comment worker started for pipeline %d", self.pipeline_id)
    
    def stop(self):
        """Stop the worker thread."""
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=10)
        LOG.info("Auto comment worker stopped for pipeline %d", self.pipeline_id)
    
    def _run_loop(self):
        """Main worker loop."""
        while not self._stop_event.is_set() and not STOP_EVENT.is_set():
            try:
                self._process_cycle()
            except Exception as exc:
                LOG.error("Auto comment worker error: %s", type(exc).__name__)
            
            # Sleep between cycles
            self._stop_event.wait(30)
    
    def _process_cycle(self):
        """Single processing cycle."""
        with db() as conn:
            settings = self.scheduler.load_settings()
            if not settings:
                return
            
            if settings.status != "enabled":
                return
            
            if not self.scheduler.is_within_active_hours(settings):
                return
            
            # Check pipeline daily limit
            ok, reason = self.scheduler.can_pipeline_post(settings)
            if not ok:
                LOG.info("Pipeline daily limit reached: %s", reason)
                return
            
            # Process ready jobs
            ready_jobs = self._get_ready_jobs(conn)
            if not ready_jobs:
                # No ready jobs, try to create new ones
                self._create_new_jobs(conn, settings)
                return
            
            # Process each ready job
            for job in ready_jobs:
                if self._stop_event.is_set():
                    break
                self._process_job(conn, job, settings)
    
    def _get_ready_jobs(self, conn) -> list:
        """Get ready-to-post jobs."""
        return get_ready_jobs(conn, self.pipeline_id, limit=5)
    
    def _process_job(self, conn, job: CommentJob, settings: AutoCommentSettings):
        """Process a single comment job."""
        LOG.info("Processing comment job %d for pipeline %d", job.id, self.pipeline_id)
        
        # Check account restrictions
        restricted, reason = check_account_restricted(conn, job.account_id)
        if restricted:
            mark_job_failed(conn, job.id, f"Account restricted: {reason}")
            return
        
        # Check if account can still post
        ok, reason = self.scheduler.can_account_post(settings, job.account_id, job.mode)
        if not ok:
            # Reschedule for later
            new_time = iso_plus_seconds(settings.min_interval_seconds)
            with db() as c:
                update_job_status(c, job.id, "scheduled", scheduled_for=new_time)
            LOG.info("Job %d rescheduled due to account limit: %s", job.id, reason)
            return
        
        # Verify post still exists and is accessible
        post_data = self._fetch_post_data(conn, job)
        if not post_data:
            mark_job_failed(conn, job.id, "Post no longer accessible")
            return
        
        # Check if we should still comment (post might have changed)
        should_comment, skip_reason = self._should_comment_on_post(post_data, job, settings)
        if not should_comment:
            mark_job_failed(conn, job.id, f"Skipped: {skip_reason}")
            return
        
        # Generate comment if not already generated
        comment_text = job.comment_text
        if not comment_text:
            fingerprint_check = lambda text: check_duplicate_limit(
                conn, self.pipeline_id, text, job.fb_post_id, job.account_id
            )
            comment_text, error = self._generate_comment_for_job(
                post_data, job, settings, fingerprint_check
            )
            if error:
                mark_job_failed(conn, job.id, f"Comment generation failed: {error}")
                return
            
            # Update job with generated comment
            with db() as c:
                update_job_status(c, job.id, "scheduled", comment_text=comment_text)
        
        # Final policy check
        policy_result = check_comment_policy(
            comment_text=comment_text,
            post_content=post_data.get("content", ""),
            post_context={"description": post_data.get("description", "")},
            group_rules=settings.group_default_rules if job.mode == "group" else None,
            provider_config=ai_provider.load_config(),
            brand_guidelines=settings.brand_guidelines,
        )
        
        if policy_result.verdict.value == "BLOCKED":
            mark_job_failed(conn, job.id, f"Policy blocked: {policy_result.reason}")
            return
        
        if policy_result.verdict.value == "NEEDS_REVIEW":
            # Mark as needing review - don't auto-post
            with db() as c:
                update_job_status(conn, job.id, "submitted_unverified", error="Needs review: " + policy_result.reason)
            LOG.info("Job %d needs review: %s", job.id, policy_result.reason)
            return
        
        # Final duplicate check
        ok, reason = check_duplicate_limit(
            conn, self.pipeline_id, comment_text, job.fb_post_id, job.account_id
        )
        if not ok:
            mark_job_failed(conn, job.id, f"Duplicate limit: {reason}")
            return
        
        # Attempt to post comment
        fb_comment_id, error = self._post_comment(job.account_id, job.fb_post_id, comment_text)
        
        if error:
            if "spam" in error.lower() or "limit" in error.lower() or "checkpoint" in error.lower():
                # Account likely restricted
                mark_job_failed(conn, job.id, f"Account may be restricted: {error}")
                # Could trigger account restriction check here
            else:
                # Retry once with backoff
                new_time = iso_plus_seconds(60)
                with db() as c:
                    update_job_status(conn, job.id, "scheduled", scheduled_for=new_time, error=error[:200])
            return
        
        # Success!
        register_comment_use(
            conn, self.pipeline_id, comment_text, job.fb_post_id,
            job.account_id, job.id
        )
        increment_account_limit(conn, job.account_id)
        mark_job_posted(conn, job.id, fb_comment_id)
        
        LOG.info("Comment job %d posted successfully (fb_comment_id=%s)", job.id, fb_comment_id)
    
    def _generate_comment_for_job(self, post_data, job, settings, fingerprint_check):
        """Generate a comment for a job."""
        group_rules = settings.group_default_rules if job.mode == "group" else None
        brand_guidelines = settings.brand_guidelines
        
        # Get feed source topic hint if applicable
        topic_hint = None
        if job.mode == "feed":
            with db() as conn:
                row = conn.execute(
                    "SELECT topics FROM auto_comment_feed_sources WHERE id = ?",
                    (job.source_id,)
                ).fetchone()
                if row and row["topics"]:
                    import json
                    topics = json.loads(row["topics"])
                    topic_hint = ", ".join(topics[:3])
        
        comment_text, error = generate_single_comment(
            post_content=post_data.get("content", ""),
            post_context={"description": post_data.get("description", "")},
            group_rules=group_rules,
            brand_guidelines=brand_guidelines,
            provider_config=ai_provider.load_config(),
            fingerprint_check_fn=fingerprint_check,
            topic_hint=topic_hint,
        )
        
        return comment_text, error
    
    def _fetch_post_data(self, conn, job):
        """Fetch current post data from Facebook."""
        # This would use the Facebook browser session
        # For now, return mock data
        return {
            "fb_post_id": job.fb_post_id,
            "content": "Sample post content",
            "description": "Sample post description",
            "author_id": "123",
            "author_name": "Author",
            "posted_at": iso(),
            "url": f"https://facebook.com/{job.fb_post_id}",
        }
    
    def _should_comment_on_post(self, post_data, job, settings) -> tuple[bool, str]:
        """Check if we should still comment on this post."""
        # Could check: post age, comment count, engagement, etc.
        return True, "OK"
    
    def _post_comment(self, account_id: int, fb_post_id: str, comment_text: str) -> tuple[str | None, str | None]:
        """Post a comment to Facebook.
        
        Returns (fb_comment_id, error).
        """
        # This would use the Facebook browser session to post the comment
        # For now, return success with fake ID
        import uuid
        return f"comment_{uuid.uuid4().hex[:16]}", None


def iso_plus_seconds(seconds: int) -> str:
    """Return ISO timestamp for now + seconds."""
    return (datetime.now() + timedelta(seconds=seconds)).isoformat()


def start_worker(pipeline_id: int) -> AutoCommentWorker:
    """Start the auto comment worker for a pipeline."""
    worker = AutoCommentWorker(pipeline_id)
    worker.start()
    return worker


def stop_worker(worker: AutoCommentWorker):
    """Stop the auto comment worker."""
    worker.stop()


# Convenience function for creating jobs from source scanning
def scan_and_create_jobs(pipeline_id: int):
    """Scan sources and create comment jobs. Called by scheduler or manually."""
    # This would be called periodically to find new posts and create jobs
    pass