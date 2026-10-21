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
                     approved_by=None, published_by=None) -> None:
    """Ghi lại ai tạo, ai duyệt và tài khoản nào đã đăng một bài."""
    row = conn.execute("""SELECT pipeline_id FROM jobs WHERE id=? AND ?='group_post'""",
                        (post_id, content_type)).fetchone()
    if row is None:
        row = conn.execute("""SELECT pipeline_id FROM manual_tasks WHERE id=? AND ?='reel'""",
                            (post_id, content_type)).fetchone()
    if row is None:
        return
    pipeline_id = row["pipeline_id"]
    conn.execute("""INSERT INTO post_attribution
        (content_type,post_id,pipeline_id,created_by_ctv_id,approved_by_user_id,
         published_by_account_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(content_type,post_id) DO UPDATE SET
            created_by_ctv_id=COALESCE(excluded.created_by_ctv_id, post_attribution.created_by_ctv_id),
            approved_by_user_id=COALESCE(excluded.approved_by_user_id, post_attribution.approved_by_user_id),
            published_by_account_id=COALESCE(excluded.published_by_account_id, post_attribution.published_by_account_id),
            updated_at=excluded.updated_at""",
                 (content_type, post_id, pipeline_id, created_by, approved_by, published_by,
                  _now(), _now()))


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
            item = conn.execute("SELECT account_id, permalink FROM jobs WHERE id=?", (post_id,)).fetchone()
        else:
            item = conn.execute("SELECT account_id, permalink FROM manual_tasks WHERE id=?", (post_id,)).fetchone()
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
