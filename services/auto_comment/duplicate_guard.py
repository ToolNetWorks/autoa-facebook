"""Auto Comment: Duplicate detection and fingerprinting."""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass

LOG = logging.getLogger("facebook-pipeline.auto-comment.duplicate")

MAX_USES_PER_COMMENT = 3
SIMILARITY_THRESHOLD = 0.85  # For near-duplicate detection


@dataclass(frozen=True)
class CommentFingerprint:
    """A unique comment content fingerprint."""
    id: int
    pipeline_id: int
    normalized_text: str
    content_hash: str
    use_count: int
    first_used_at: str
    last_used_at: str
    created_at: str


def normalize_comment(text: str) -> str:
    """Normalize comment text for comparison.
    
    - Unicode normalization (NFC)
    - Case fold
    - Collapse whitespace
    - Remove punctuation except essential
    - Remove emojis (optional)
    """
    if not text:
        return ""
    
    # Unicode normalization
    text = unicodedata.normalize("NFC", text)
    
    # Case fold
    text = text.casefold()
    
    # Replace emojis with placeholder
    text = re.sub(r'[\U0001F300-\U0001F9FF\U00002600-\U000027BF]', ' ', text)
    
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)
    
    # Remove punctuation except . , ? ! (keep sentence structure)
    text = re.sub(r'[^\w\s\.\,\?\!]', ' ', text)
    
    # Collapse whitespace again
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()


def compute_content_hash(text: str) -> str:
    """Compute SHA256 hash of normalized text."""
    normalized = normalize_comment(text)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def is_similar(text1: str, text2: str, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    """Check if two texts are similar using Jaccard similarity on word n-grams."""
    if not text1 or not text2:
        return False
    
    # Get word 3-grams
    def get_trigrams(text):
        words = text.split()
        trigrams = set()
        for i in range(len(words) - 2):
            trigrams.add(" ".join(words[i:i+3]))
        return trigrams
    
    t1 = get_trigrams(text1)
    t2 = get_trigrams(text2)
    
    if not t1 or not t2:
        return False
    
    intersection = len(t1 & t2)
    union = len(t1 | t2)
    
    if union == 0:
        return False
    
    return (intersection / union) >= threshold


def check_duplicate_limit(conn, pipeline_id: int, comment_text: str,
                          fb_post_id: str, account_id: int) -> tuple[bool, str]:
    """Check if a comment would exceed duplicate limits.
    
    Returns (is_allowed, reason).
    """
    normalized = normalize_comment(comment_text)
    content_hash = compute_content_hash(comment_text)
    
    # Check exact hash matches (same comment content)
    row = conn.execute(
        """SELECT use_count FROM comment_fingerprints
           WHERE pipeline_id = ? AND content_hash = ?""",
        (pipeline_id, content_hash)
    ).fetchone()
    
    if row and row["use_count"] >= MAX_USES_PER_COMMENT:
        return False, f"DUPLICATE_LIMIT: comment used {row['use_count']} times (max {MAX_USES_PER_COMMENT})"
    
    # Check near-duplicate fingerprints
    similar_rows = conn.execute(
        """SELECT normalized_text, use_count FROM comment_fingerprints
           WHERE pipeline_id = ? AND content_hash != ?""",
        (pipeline_id, content_hash)
    ).fetchall()
    
    for row in similar_rows:
        if is_similar(normalized, row["normalized_text"]):
            total_uses = row["use_count"] + 1  # +1 for this proposed use
            if total_uses > MAX_USES_PER_COMMENT:
                return False, f"NEAR_DUPLICATE_LIMIT: similar comment used {row['use_count']} times"
    
    # Check if same account already commented on this post
    row = conn.execute(
        """SELECT id FROM auto_comment_jobs
           WHERE pipeline_id = ? AND account_id = ? AND fb_post_id = ?
             AND status IN ('pending', 'scheduled', 'posted', 'submitted_unverified')""",
        (pipeline_id, account_id, fb_post_id)
    ).fetchone()
    
    if row:
        return False, "ACCOUNT_ALREADY_COMMENTED: this account already has a comment job for this post"
    
    # Check if ANY account already used this exact comment on this post
    row = conn.execute(
        """SELECT account_id FROM auto_comment_jobs
           WHERE pipeline_id = ? AND fb_post_id = ? AND comment_text = ?
             AND status IN ('pending', 'scheduled', 'posted', 'submitted_unverified')""",
        (pipeline_id, fb_post_id, comment_text)
    ).fetchone()
    
    if row:
        return False, "DUPLICATE_ON_POST: another account already used this comment on this post"
    
    return True, "OK"


def register_comment_use(conn, pipeline_id: int, comment_text: str,
                         fb_post_id: str, account_id: int, job_id: int) -> int:
    """Register a comment use and return fingerprint ID."""
    from app import iso
    
    normalized = normalize_comment(comment_text)
    content_hash = compute_content_hash(comment_text)
    
    # Try to insert new fingerprint or update existing
    row = conn.execute(
        """SELECT id, use_count FROM comment_fingerprints
           WHERE pipeline_id = ? AND content_hash = ?""",
        (pipeline_id, content_hash)
    ).fetchone()
    
    if row:
        conn.execute(
            """UPDATE comment_fingerprints SET use_count = use_count + 1,
               last_used_at = ? WHERE id = ?""",
            (iso(), row["id"])
        )
        fingerprint_id = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO comment_fingerprints
               (pipeline_id, normalized_text, content_hash, use_count, first_used_at, last_used_at, created_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (pipeline_id, normalize_comment(comment_text), content_hash, iso(), iso(), iso())
        )
        fingerprint_id = cur.lastrowid
    
    # Link fingerprint to job
    conn.execute(
        """INSERT INTO auto_comment_job_fingerprints
           (job_id, fingerprint_id, created_at) VALUES (?, ?, ?)""",
        (job_id, fingerprint_id, iso())
    )
    
    return fingerprint_id


def get_fingerprint_stats(conn, pipeline_id: int) -> dict:
    """Get duplicate guard statistics for a pipeline."""
    row = conn.execute(
        """SELECT COUNT(*) as total_fingerprints,
                  SUM(use_count) as total_uses,
                  MAX(use_count) as max_uses
           FROM comment_fingerprints WHERE pipeline_id = ?""",
        (pipeline_id,)
    ).fetchone()
    
    return dict(row) if row else {"total_fingerprints": 0, "total_uses": 0, "max_uses": 0}