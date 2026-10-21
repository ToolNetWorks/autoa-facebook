"""Auto Comment: Group sources management."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("facebook-pipeline.auto-comment.group")


@dataclass(frozen=True)
class GroupSource:
    """A Facebook Group configured as a source for auto-comment."""
    id: int
    pipeline_id: int
    fb_group_id: str
    name: str
    url: str
    is_active: bool
    allow_auto_comment: bool
    created_at: str
    updated_at: str


def load_group_sources(conn, pipeline_id: int) -> list[GroupSource]:
    """Load active group sources for a pipeline."""
    rows = conn.execute(
        """SELECT * FROM auto_comment_group_sources
           WHERE pipeline_id = ? AND is_active = 1 AND allow_auto_comment = 1
           ORDER BY id""",
        (pipeline_id,)
    ).fetchall()
    return [GroupSource(**dict(row)) for row in rows]


def add_group_source(conn, pipeline_id: int, fb_group_id: str, name: str,
                     url: str, allow_auto_comment: bool = True) -> int:
    """Add a new group source."""
    from app import iso
    cur = conn.execute(
        """INSERT INTO auto_comment_group_sources
           (pipeline_id, fb_group_id, name, url, is_active, allow_auto_comment, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
        (pipeline_id, fb_group_id, name, url, allow_auto_comment, iso(), iso())
    )
    return cur.lastrowid


def update_group_source(conn, source_id: int, **fields) -> bool:
    """Update a group source."""
    if not fields:
        return False
    from app import iso
    fields["updated_at"] = iso()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [source_id]
    conn.execute(
        f"UPDATE auto_comment_group_sources SET {set_clause} WHERE id = ?",
        values
    )
    return True


def delete_group_source(conn, source_id: int) -> bool:
    """Soft delete a group source."""
    from app import iso
    conn.execute(
        "UPDATE auto_comment_group_sources SET is_active = 0, updated_at = ? WHERE id = ?",
        (iso(), source_id)
    )
    return True


def validate_group_accessible(fb_group_id: str, account_cookies: dict) -> tuple[bool, str]:
    """Check if a group is accessible with given cookies.
    
    Returns (is_accessible, reason).
    """
    # This would use the Facebook browser session to check group access
    # For now, return True as placeholder
    return True, "OK"


def fetch_latest_posts(conn, group_source: GroupSource, account_cookies: dict,
                       since_timestamp: int | None = None, limit: int = 10) -> list[dict]:
    """Fetch latest posts from a group.
    
    Returns list of dicts with: fb_post_id, content, author_id, author_name,
    posted_at, url, media_type, media_urls, comment_count, reaction_count.
    """
    # This would use the Facebook browser session to fetch posts
    # For now, return empty list as placeholder
    return []