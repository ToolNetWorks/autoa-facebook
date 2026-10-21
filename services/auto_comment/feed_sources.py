"""Auto Comment: News Feed sources management."""
from __future__ import annotations

import logging
from dataclasses import dataclass

LOG = logging.getLogger("facebook-pipeline.auto-comment.feed")


@dataclass(frozen=True)
class FeedSource:
    """A News Feed source configuration for auto-comment."""
    id: int
    pipeline_id: int
    name: str
    source_type: str  # 'profile', 'page', 'keyword', 'saved'
    target_id: str    # Facebook ID of profile/page or keyword
    topics: list[str] # Filter topics
    excluded_keywords: list[str]
    excluded_authors: list[str]
    allowed_domains: list[str]
    is_active: bool
    created_at: str
    updated_at: str


def load_feed_sources(conn, pipeline_id: int) -> list[FeedSource]:
    """Load active feed sources for a pipeline."""
    rows = conn.execute(
        """SELECT * FROM auto_comment_feed_sources
           WHERE pipeline_id = ? AND is_active = 1
           ORDER BY id""",
        (pipeline_id,)
    ).fetchall()
    
    sources = []
    for row in rows:
        d = dict(row)
        # Parse JSON fields
        import json
        d["topics"] = json.loads(d.get("topics") or "[]")
        d["excluded_keywords"] = json.loads(d.get("excluded_keywords") or "[]")
        d["excluded_authors"] = json.loads(d.get("excluded_authors") or "[]")
        d["allowed_domains"] = json.loads(d.get("allowed_domains") or "[]")
        sources.append(FeedSource(**d))
    return sources


def add_feed_source(conn, pipeline_id: int, name: str, source_type: str,
                    target_id: str, topics: list[str] = None,
                    excluded_keywords: list[str] = None,
                    excluded_authors: list[str] = None,
                    allowed_domains: list[str] = None) -> int:
    """Add a new feed source."""
    import json
    from app import iso
    
    cur = conn.execute(
        """INSERT INTO auto_comment_feed_sources
           (pipeline_id, name, source_type, target_id, topics, excluded_keywords,
            excluded_authors, allowed_domains, is_active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (pipeline_id, name, source_type, target_id,
         json.dumps(topics or []),
         json.dumps(excluded_keywords or []),
         json.dumps(excluded_authors or []),
         json.dumps(allowed_domains or []),
         iso(), iso())
    )
    return cur.lastrowid


def update_feed_source(conn, source_id: int, **fields) -> bool:
    """Update a feed source."""
    if not fields:
        return False
    import json
    from app import iso
    
    # Convert list fields to JSON
    for key in ("topics", "excluded_keywords", "excluded_authors", "allowed_domains"):
        if key in fields and isinstance(fields[key], list):
            fields[key] = json.dumps(fields[key])
    
    fields["updated_at"] = iso()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [source_id]
    conn.execute(
        f"UPDATE auto_comment_feed_sources SET {set_clause} WHERE id = ?",
        values
    )
    return True


def delete_feed_source(conn, source_id: int) -> bool:
    """Soft delete a feed source."""
    from app import iso
    conn.execute(
        "UPDATE auto_comment_feed_sources SET is_active = 0, updated_at = ? WHERE id = ?",
        (iso(), source_id)
    )
    return True


def matches_feed_filters(post: dict, feed_source: FeedSource) -> tuple[bool, str]:
    """Check if a post matches the feed source filters.
    
    Returns (matches, reason).
    """
    content = (post.get("content") or "").lower()
    author = (post.get("author_name") or "").lower()
    url = (post.get("url") or "").lower()
    
    # Excluded keywords
    for kw in feed_source.excluded_keywords:
        if kw.lower() in content:
            return False, f"excluded_keyword: {kw}"
    
    # Excluded authors
    if author in [a.lower() for a in feed_source.excluded_authors]:
        return False, f"excluded_author: {author}"
    
    # Allowed domains (if specified)
    if feed_source.allowed_domains:
        domain_ok = any(d.lower() in url for d in feed_source.allowed_domains)
        if not domain_ok:
            return False, "domain_not_allowed"
    
    # Topics (if specified, at least one must match)
    if feed_source.topics:
        topic_ok = any(t.lower() in content for t in feed_source.topics)
        if not topic_ok:
            return False, "no_matching_topic"
    
    return True, "OK"


def fetch_news_feed_posts(conn, feed_source: FeedSource, account_cookies: dict,
                          since_timestamp: int | None = None, limit: int = 10) -> list[dict]:
    """Fetch latest posts from News Feed matching the source.
    
    Returns list of dicts with: fb_post_id, content, author_id, author_name,
    posted_at, url, media_type, media_urls, comment_count, reaction_count.
    """
    # This would use the Facebook browser session to fetch News Feed
    # For now, return empty list as placeholder
    return []