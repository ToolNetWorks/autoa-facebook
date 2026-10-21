"""Số liệu phân tích của Workspace và của từng CTV.

Nguyên tắc của module này: chỉ đưa ra con số có thể kiểm chứng.

* Một chỉ số chưa được thu thập được trả về ``None`` chứ không phải 0. Giao
  diện hiện "Chưa có dữ liệu" cho những trường hợp đó.
* Tổng tương tác chỉ cộng snapshot **mới nhất** của mỗi bài, nên một bài được
  đo nhiều lần vẫn chỉ tính một lần.
* Lượt xem, reach và tương tác là ba nhóm số riêng, không cộng chung.
* Bài chờ duyệt, bài đăng và bài bị gỡ là ba trạng thái khác nhau.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

GROUP_POST = "group_post"
REEL = "reel"

CONTENT_LABELS = {GROUP_POST: "Bài Group", REEL: "Reel"}


# --- helpers -----------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_day(value: str | None) -> datetime | None:
    """The first second of a day, given as YYYY-MM-DD."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def day_range(days: int | None, custom_from: str | None = None,
              custom_to: str | None = None) -> tuple[datetime | None, datetime | None]:
    """The window of the report."""
    if custom_from or custom_to:
        start = parse_day(custom_from)
        end = parse_day(custom_to)
        if end is not None:
            end = end + timedelta(days=1)
        return start, end
    if not days:
        return None, None
    end = _now()
    start = (end - timedelta(days=max(1, int(days)))).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def _within(value: str | None, start: datetime | None, end: datetime | None) -> bool:
    if not value:
        return False
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if start and moment < start:
        return False
    if end and moment >= end:
        return False
    return True


# --- the content of a workspace ----------------------------------------------

CONTENT_UNION = """
SELECT j.id post_id, 'group_post' content_type, j.pipeline_id, j.account_id,
       COALESCE(j.posting_status, j.state) posting_status, j.state,
       COALESCE(j.published_at, j.started_at, j.updated_at) created_at,
       j.scheduled_at, j.updated_at, j.published_at, j.permalink,
       p.body, p.owner_user_id created_by, p.id source_post_id,
       g.label group_label, g.url group_url, a.label account_label,
       'group' content_kind
  FROM jobs j JOIN posts p ON p.id = j.post_id
  LEFT JOIN groups g ON g.id = j.group_id
  LEFT JOIN accounts a ON a.id = j.account_id
UNION ALL
SELECT t.id post_id, 'reel' content_type, t.pipeline_id, t.account_id,
       t.status posting_status, NULL state,
       t.created_at, t.scheduled_at, t.updated_at, t.published_at, t.permalink,
       t.body, t.owner_user_id created_by, NULL source_post_id,
       NULL group_label, NULL group_url, a.label account_label,
       'reel' content_kind
  FROM manual_tasks t LEFT JOIN accounts a ON a.id = t.account_id
 WHERE t.task_type = 'reel'
"""

CONTENT_SQL = "SELECT * FROM (" + CONTENT_UNION + ")"


def latest_snapshots(conn) -> dict:
    """The newest snapshot of every post.

    Only this row is used, so a post that was measured five times still
    counts once.
    """
    rows = conn.execute("""
        SELECT s.* FROM post_engagement_snapshots s
        JOIN (SELECT content_type, post_id, MAX(id) newest
                FROM post_engagement_snapshots GROUP BY content_type, post_id) t
          ON s.id = t.newest""").fetchall()
    return {(row["content_type"], row["post_id"]): dict(row) for row in rows}


def content_rows(conn, pipeline_id: int | None = None, account_id: int | None = None,
                 ctv_id: int | None = None, content_type: str | None = None,
                 status: str | None = None, start: datetime | None = None,
                 end: datetime | None = None, include_reels: bool = True) -> list[dict]:
    """Every piece of content with its attribution and its newest snapshot."""
    sql = CONTENT_SQL
    conditions = []
    params: list = []
    if not include_reels:
        conditions.append("content_type='group_post'")
    if pipeline_id is not None:
        conditions.append("pipeline_id=?")
        params.append(int(pipeline_id))
    if account_id:
        conditions.append("account_id=?")
        params.append(int(account_id))
    if ctv_id:
        conditions.append("created_by=?")
        params.append(int(ctv_id))
    if content_type:
        conditions.append("content_type=?")
        params.append(content_type)
    if status:
        conditions.append("posting_status=?")
        params.append(status)
    if start or end:
        conditions.append("(created_at >= ? OR created_at IS NULL)")
        params.append(start.isoformat() if start else "1970-01-01T00:00:00+00:00")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC"

    snapshots = latest_snapshots(conn)
    rows = []
    for raw in conn.execute(sql, params).fetchall():
        row = dict(raw)
        moment = row.get("published_at") or row.get("created_at") or ""
        if (start or end) and not _within(moment, start, end):
            continue
        snapshot = snapshots.get((row["content_type"], row["post_id"]))
        row["snapshot"] = snapshot
        row["engagement"] = engagement_of(snapshot)
        row["views"] = snapshot.get("views_count") if snapshot else None
        row["reach"] = snapshot.get("reach_count") if snapshot else None
        row["measured"] = snapshot is not None
        row["removed"] = bool(snapshot and snapshot.get("verification_status") == "removed")
        rows.append(row)
    return rows


def engagement_of(snapshot: dict | None) -> dict | None:
    """Reactions, comments and shares of one post.

    ``None`` means "chưa có dữ liệu"; a post without any measurement never
    turns into a zero.
    """
    if not snapshot:
        return None
    reactions = snapshot.get("reactions_count")
    comments = snapshot.get("comments_count")
    shares = snapshot.get("shares_count")
    if reactions is None and comments is None and shares is None:
        return None
    return {
        "reactions": reactions,
        "comments": comments,
        "shares": shares,
        "total": sum(value for value in (reactions, comments, shares) if value is not None),
    }


# --- aggregation --------------------------------------------------------------

def _sum_measured(rows: list[dict], field: str) -> int | None:
    """The sum of a metric over the posts that really have it."""
    total = 0
    seen = False
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        total += int(value)
        seen = True
    return total if seen else None


def engagement_total(rows: list[dict]) -> int | None:
    """The measured interactions of a set of posts."""
    total = 0
    seen = False
    for row in rows:
        engagement = row.get("engagement")
        if not engagement or engagement.get("total") is None:
            continue
        total += int(engagement["total"])
        seen = True
    return total if seen else None


def count_states(rows: list[dict]) -> dict:
    """How many posts are published, waiting, refused or removed."""
    counts = {"posted": 0, "submitted_unverified": 0, "pending_approval": 0,
              "awaiting_manual": 0, "queued_experimental": 0, "needs_review": 0,
              "failed": 0, "cancelled": 0, "removed": 0, "other": 0}
    for row in rows:
        status = (row.get("posting_status") or "").strip()
        if row.get("removed"):
            counts["removed"] += 1
        if status in counts:
            counts[status] += 1
        elif status:
            counts["other"] += 1
    counts["published"] = counts["posted"]
    counts["waiting"] = (counts["pending_approval"] + counts["awaiting_manual"]
                          + counts["queued_experimental"])
    counts["refused"] = counts["failed"] + counts["needs_review"]
    return counts


def summarize(rows: list[dict]) -> dict:
    """The numbers of one pipeline, one collaborator or the whole system."""
    counts = count_states(rows)
    reels = [row for row in rows if row["content_type"] == REEL]
    group_rows = [row for row in rows if row["content_type"] == GROUP_POST]
    return {
        "total": len(rows),
        "posted": counts["published"],
        "waiting": counts["waiting"],
        "refused": counts["refused"],
        "removed": counts["removed"],
        "submitted_unverified": counts["submitted_unverified"],
        "group_posts": len(group_rows),
        "group_posts_posted": count_states(group_rows)["published"],
        "reels": len(reels),
        "reels_posted": count_states(reels)["published"],
        "engagement": engagement_total(rows),
        "reactions": _sum_measured(rows, "engagement_reactions"),
        "comments": _sum_measured(rows, "engagement_comments"),
        "shares": _sum_measured(rows, "engagement_shares"),
        "views": _sum_measured(rows, "views"),
        "reach": _sum_measured(rows, "reach"),
        "measured_posts": sum(1 for row in rows if row.get("measured")),
        "unmeasured_posts": sum(1 for row in rows if not row.get("measured")),
    }


def _attach_metric_sums(rows: list[dict]) -> None:
    """Fill the per metric sums so that they can be summed separately."""
    for row in rows:
        engagement = row.get("engagement") or {}
        row["engagement_reactions"] = engagement.get("reactions")
        row["engagement_comments"] = engagement.get("comments")
        row["engagement_shares"] = engagement.get("shares")


def summarize_rows(rows: list[dict]) -> dict:
    """Like :func:`summarize` but keeps reactions, comments and shares apart."""
    _attach_metric_sums(rows)
    return summarize(rows)


def timeseries(rows: list[dict], days: int = 30) -> list[dict]:
    """One point per day with the published posts and the measured interaction."""
    buckets: dict[str, dict] = {}
    for row in rows:
        moment = row.get("published_at") or row.get("created_at") or ""
        if not moment:
            continue
        day = moment[:10]
        bucket = buckets.setdefault(day, {"day": day, "posts": 0, "reels": 0,
                                          "engagement": 0, "measured": 0, "views": 0})
        if row["content_type"] == REEL:
            bucket["reels"] += 1
        else:
            bucket["posts"] += 1
        engagement = row.get("engagement")
        if engagement and engagement.get("total") is not None:
            bucket["engagement"] += int(engagement["total"])
            bucket["measured"] += 1
        if row.get("views") is not None:
            bucket["views"] += int(row["views"])
    ordered = [buckets[key] for key in sorted(buckets)]
    limit = max(1, int(days or 30))
    return ordered[-limit:]
