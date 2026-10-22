"""Tạo chiến dịch đăng group: một bài, nhiều job được quản lý riêng.

Mỗi group nhận **một job riêng** với danh tính và chế độ đăng của riêng nó.
Không có một tác vụ Playwright khổng lồ cho tất cả group, và ảnh được dùng
chung nên không tải lại nhiều lần.

Idempotency: mỗi job có một khoá theo cặp group–tài khoản–ngày, nên gọi lại
cùng một chiến dịch không tạo job trùng. Job của một group đã có bài đang
chờ thì group đó được báo riêng, các group khác vẫn được tạo.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from services import group_post_validator

#: Tiền tố của khoá idempotency.
JOB_KEY_PREFIX = "campaign"


def job_key(pipeline_id: int, group_id: int, account_id: int, day: str) -> str:
    """Khoá idempotency của một job."""
    return f"{JOB_KEY_PREFIX}:{int(pipeline_id)}:{int(group_id)}:{int(account_id)}:{day}"


def create_campaign(conn, pipeline_id: int, group_ids: list[int], principal, caption: str,
                    media_paths: list[str], scheduled_at: datetime,
                    ai_record_id: int | None = None, ai_caption: str = "",
                    may_publish: bool = True, lookup_group=None, lookup_account=None,
                    resource_owner=None) -> dict:
    """Tạo bài và các job cho một chiến dịch.

    Trả về số job đã tạo, các group bị bỏ qua kèm lý do, và các group đã
    có job nên không tạo lại.
    """
    from services import analytics_worker

    if lookup_group is None or lookup_account is None:
        raise ValueError("create_campaign cần hàm tra cứu group và account")

    plan = group_post_validator.validate_selection(
        conn, pipeline_id, group_ids, principal, lookup_group, lookup_account, may_publish,
        scheduled=scheduled_at)

    if not plan["ready"]:
        # Không group nào đủ điều kiện: không tạo bài rỗng, chỉ báo lý do.
        return {"post_id": None, "jobs": [], "skipped": plan["skipped"],
                "deferred": plan.get("deferred") or [], "duplicates": [],
                "selected": plan["selected"], "limit": plan["limit"], "published": False}

    owner = resource_owner(principal) if resource_owner else None
    post = conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only,
                                           ai_record_id,owner_user_id)
                            VALUES(?,?,?,?,1,?,?)""",
                         (int(pipeline_id), caption, json.dumps(media_paths), iso(),
                          ai_record_id, owner))
    post_id = post.lastrowid
    analytics_worker.sync_attribution(conn, "group_post", post_id, created_by=owner)

    day = scheduled_at.strftime("%Y-%m-%d")
    created: list[dict] = []
    skipped: list[dict] = list(plan["skipped"])
    deferred: list[dict] = list(plan.get("deferred") or [])
    duplicates: list[dict] = []
    for item in plan["ready"]:
        key = job_key(pipeline_id, item["group_id"], item["account_id"], day)
        exists = conn.execute("""SELECT id FROM jobs WHERE pipeline_id=? AND idempotency_key=?""",
                              (int(pipeline_id), key)).fetchone()
        if exists:
            duplicates.append({"group_id": item["group_id"], "label": item["label"],
                               "reason": "Bài này đã có trong hàng đợi"})
            skipped.append({"group_id": item["group_id"], "label": item["label"],
                            "reason": "Bài này đã có trong hàng đợi"})
            continue
        state = "queued" if may_publish else "pending_approval"
        details = "" if may_publish else "Bài nháp: CTV chưa có quyền đăng, cần Chủ hệ thống duyệt"
        if ai_record_id and not ai_caption:
            state = "pending_approval"
            details = "Caption AI chưa được duyệt hoặc bị chặn, bài chờ người duyệt"
        cursor = conn.execute("""INSERT INTO jobs
            (post_id,group_id,account_id,pipeline_id,state,details,scheduled_at,updated_at,
             requested_posting_mode,requested_nickname,posting_status,verification_status,
             idempotency_key)
            VALUES(?,?,?,?,?,?,?,?,?,?,'pending','unverified',?)""",
            (post_id, item["group_id"], item["account_id"], int(pipeline_id), state, details,
             iso(scheduled_at), iso(), item["posting_mode"], item["nickname"], key))
        created.append({"job_id": cursor.lastrowid, "group_id": item["group_id"],
                        "label": item["label"], "account_id": item["account_id"],
                        "posting_mode": item["posting_mode"], "nickname": item["nickname"],
                        "decision": item.get("decision", "")})
    return {"post_id": post_id, "jobs": created, "skipped": skipped,
            "deferred": deferred, "duplicates": duplicates, "selected": plan["selected"],
            "limit": plan["limit"], "published": may_publish and not (ai_record_id and not ai_caption)}


def iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat(timespec="seconds")
