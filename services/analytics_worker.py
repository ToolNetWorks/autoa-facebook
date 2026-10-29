"""Bộ thu thập chỉ số tương tác và hàng đợi của nó.

Hàng đợi có một dòng cho mỗi bài đã đăng, nên bộ thu thập chạy lại nhiều lần
vẫn không làm trùng công việc. Mỗi lần đo thành công ghi một snapshot; bảng
điều khiển chỉ cộng snapshot mới nhất của mỗi bài.
"""
from __future__ import annotations

import logging

from services import analytics
from services.engagement_collector import collect_one

LOG = logging.getLogger("facebook-pipeline")


def _now() -> str:
    return analytics._now().isoformat()


def sync_attribution(conn, content_type: str, post_id: int, created_by=None,
                     approved_by=None, published_by=None, *, pipeline_id=None) -> bool:
    """Attribute an execution ID (jobs.id for Groups), never a source posts.id.

    Invalid legacy associations are left untouched for an approved repair. Refusal
    must not roll back a Facebook result already observed by the caller.
    """
    if content_type == "group_post":
        row = conn.execute("""SELECT j.pipeline_id,j.account_id,j.state status,
            j.verification_status,j.permalink FROM jobs j
            JOIN posts p ON p.id=j.post_id AND p.pipeline_id=j.pipeline_id
            JOIN groups g ON g.id=j.group_id AND g.pipeline_id=j.pipeline_id
            JOIN accounts a ON a.id=j.account_id AND a.pipeline_id=j.pipeline_id
            WHERE j.id=?""", (post_id,)).fetchone()
    elif content_type == "reel":
        row = conn.execute("""SELECT t.pipeline_id,t.account_id,t.status,
            'unverified' verification_status,t.permalink FROM manual_tasks t
            JOIN accounts a ON a.id=t.account_id AND a.pipeline_id=t.pipeline_id
            WHERE t.id=? AND t.task_type='reel'""", (post_id,)).fetchone()
    else:
        return False
    if row is None or (pipeline_id is not None and row["pipeline_id"] != pipeline_id):
        return False
    tenant = row["pipeline_id"]
    if created_by is not None and not conn.execute("""SELECT 1 FROM pipeline_memberships
        WHERE user_id=? AND pipeline_id=?""", (created_by, tenant)).fetchone():
        return False
    if approved_by is not None and not conn.execute("""SELECT 1 FROM users
        WHERE id=? AND role='admin'""", (approved_by,)).fetchone():
        return False
    if published_by is not None:
        if (published_by != row["account_id"] or row["status"] != "posted"
                or row["verification_status"] != "verified" or not row["permalink"]):
            return False
    existing = conn.execute("""SELECT pipeline_id,published_by_account_id FROM post_attribution
        WHERE content_type=? AND post_id=?""", (content_type, post_id)).fetchone()
    if existing and (existing["pipeline_id"] != tenant or
            (existing["published_by_account_id"] is not None and
             existing["published_by_account_id"] != row["account_id"])):
        LOG.warning("attribution_conflict content_type=%s execution_id=%s", content_type, post_id)
        return False
    conn.execute("""INSERT INTO post_attribution
        (content_type,post_id,pipeline_id,created_by_ctv_id,approved_by_user_id,
         published_by_account_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(content_type,post_id) DO UPDATE SET
            created_by_ctv_id=COALESCE(excluded.created_by_ctv_id,post_attribution.created_by_ctv_id),
            approved_by_user_id=COALESCE(excluded.approved_by_user_id,post_attribution.approved_by_user_id),
            published_by_account_id=COALESCE(excluded.published_by_account_id,post_attribution.published_by_account_id),
            updated_at=excluded.updated_at""",
        (content_type,post_id,tenant,created_by,approved_by,published_by,_now(),_now()))
    return True


def enqueue_published(conn) -> int:
    """Đưa mọi bài đã đăng có permalink vào hàng đợi đo."""
    queued = 0
    rows = conn.execute("""SELECT j.id post_id, 'group_post' content_type, j.pipeline_id, j.account_id,
                                   j.permalink, COALESCE(j.posting_status, j.state) status
                              FROM jobs j
                             WHERE j.permalink <> ''
                               AND COALESCE(j.posting_status, j.state) IN ('posted','submitted_unverified')
            UNION ALL
            SELECT t.id post_id, 'reel' content_type, t.pipeline_id, t.account_id,
                   t.permalink, t.status
              FROM manual_tasks t
             WHERE t.task_type='reel' AND t.permalink <> ''
               AND t.status IN ('posted','submitted_unverified')""").fetchall()
    for row in rows:
        conn.execute("""INSERT INTO analytics_collection_jobs
            (pipeline_id,content_type,post_id,status,last_attempt_at,error_code,created_at,updated_at)
            VALUES(?,?,?,'queued',NULL,'',?,?)
            ON CONFLICT(content_type,post_id) DO UPDATE SET
                status=CASE WHEN analytics_collection_jobs.status='done' THEN 'done' ELSE 'queued' END,
                updated_at=excluded.updated_at""",
                     (row["pipeline_id"], row["content_type"], row["post_id"], _now(), _now()))
        queued += 1
    return queued


def collect_pending(conn, limit: int = 5) -> int:
    """Đo một lô bài đang chờ và ghi snapshot."""
    rows = conn.execute("""SELECT * FROM analytics_collection_jobs
                            WHERE status <> 'done'
                            ORDER BY id LIMIT ?""", (int(limit),)).fetchall()
    done = 0
    for row in rows:
        content_type = row["content_type"]
        post_id = row["post_id"]
        if content_type == "group_post":
            item = conn.execute("SELECT account_id, permalink FROM jobs WHERE id=? AND pipeline_id=?", (post_id, row["pipeline_id"])).fetchone()
        else:
            item = conn.execute("SELECT account_id, permalink FROM manual_tasks WHERE id=? AND pipeline_id=?", (post_id, row["pipeline_id"])).fetchone()
        if item is None:
            conn.execute("""UPDATE analytics_collection_jobs SET status='skipped', error_code='no_post',
                            updated_at=? WHERE id=?""", (_now(), row["id"]))
            continue
        outcome = collect_one(item["account_id"], item["permalink"] or "", str(post_id))
        status = outcome.get("verification_status")
        source = outcome.get("source") or ""
        if status == "measured":
            conn.execute("""INSERT OR IGNORE INTO post_engagement_snapshots
                (pipeline_id,content_type,post_id,account_id,reactions_count,comments_count,
                 shares_count,views_count,reach_count,source,collected_at,verification_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (row["pipeline_id"], content_type, post_id, item["account_id"],
                          outcome.get("reactions"), outcome.get("comments"), outcome.get("shares"),
                          outcome.get("views"), outcome.get("reach"), source, _now(), status))
            conn.execute("""UPDATE analytics_collection_jobs SET status='done', last_attempt_at=?,
                            error_code='', updated_at=? WHERE id=?""",
                         (_now(), _now(), row["id"]))
            done += 1
        else:
            # Không đo được thì giữ hàng đợi, nhưng ghi lại mã lỗi.
            conn.execute("""UPDATE analytics_collection_jobs SET status='queued', last_attempt_at=?,
                            error_code=?, updated_at=? WHERE id=?""",
                         (_now(), outcome.get("error_code", "")[:40], _now(), row["id"]))
            LOG.info("Chưa đo được chỉ số của %s %s: %s", content_type, post_id,
                     outcome.get("error_code", ""))
    return done


def run_once(limit: int = 5) -> int:
    """Một vòng của bộ thu thập, dùng bởi worker và bởi việc kiểm thử."""
    import app as m

    with m.db() as conn:
        enqueue_published(conn)
    with m.db() as conn:
        return collect_pending(conn, limit)
