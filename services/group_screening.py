"""Quan sát của trình duyệt về một group, và cách đọc nó.

Phần này chỉ **quan sát**: trình duyệt thấy gì khi mở group. Những quan sát
này dùng để hướng dẫn CTV, không dùng để kết luận quyền đăng.

Kết luận về quyền đăng nằm ở :mod:`services.group_verification`, và chỉ được
đưa ra khi đã có bằng chứng từ một lần gửi bài thật.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

STATUS_PRIORITY = "priority"
STATUS_REVIEW = "review"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_MEMBER = "not_member"
STATUS_NOT_ACCESSIBLE = "not_accessible"
STATUS_RESTRICTED = "restricted"
STATUS_FAILED = "failed"

SCREENING_STATUSES = (STATUS_PRIORITY, STATUS_REVIEW, STATUS_UNKNOWN, STATUS_NOT_MEMBER,
                      STATUS_NOT_ACCESSIBLE, STATUS_RESTRICTED, STATUS_FAILED)

STATUS_LABELS = {
    STATUS_PRIORITY: "Đủ điều kiện đăng trực tiếp",
    STATUS_REVIEW: "Cần Admin group duyệt bài",
    STATUS_UNKNOWN: "Chưa xác minh",
    STATUS_NOT_MEMBER: "Chưa tham gia group",
    STATUS_NOT_ACCESSIBLE: "Không xem được nội dung group",
    STATUS_RESTRICTED: "Facebook hạn chế quyền đăng",
    STATUS_FAILED: "Không kiểm tra được",
}

# The order in which eligible groups are served. Restricted and failed groups
# are never served automatically.
PRIORITY_ORDER = {
    STATUS_PRIORITY: 0,
    STATUS_REVIEW: 1,
    STATUS_UNKNOWN: 2,
    STATUS_NOT_MEMBER: 3,
    STATUS_NOT_ACCESSIBLE: 4,
    STATUS_RESTRICTED: 5,
    STATUS_FAILED: 6,
}

# The browser statuses that mean the check could not be carried out.
CHECK_FAILED_STATUSES = ("error", "busy", "failed", "timeout")
# The browser statuses that mean the browser saw a clear answer.
CHECK_RESTRICTED = "restricted"
CHECK_NOT_MEMBER = "not_member"
CHECK_NOT_ACCESSIBLE = "not_accessible"
CHECK_NEEDS_REVIEW = "needs_review"

# A group that has not shown a direct post for this long loses its priority.
FRESHNESS_DAYS = 30
# At most one screening run per group inside this window unless it is forced.
RECHECK_COOLDOWN_SECONDS = 3600
# Minimum pause between two screening runs of the whole system.
MIN_CHECK_INTERVAL_SECONDS = 20
# The confidence of a group never reaches certainty on a single observation.
MAX_CONFIDENCE = 0.95


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def is_fresh(value: str | None, days: int = FRESHNESS_DAYS, now: datetime | None = None) -> bool:
    moment = _parse(value)
    if moment is None:
        return False
    reference = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return reference - moment <= timedelta(days=days)


def _newer(first: str | None, second: str | None) -> bool:
    """True when first is later than second."""
    left, right = _parse(first), _parse(second)
    if left is None:
        return False
    if right is None:
        return True
    return left > right


def classify(evidence: dict) -> tuple[str, str, float]:
    """Quan sát của trình duyệt về group, dưới dạng (trạng thái, nguồn, độ tin cậy).

    Đây **không phải** kết luận về quyền đăng. Nó chỉ cho biết trình duyệt
    nhìn thấy gì, để hướng dẫn CTV:

    * một lần kiểm tra không thực hiện được là một lỗi, không phải hạn chế;
    * không tìm thấy selector, timeout hay lỗi trình duyệt không bao giờ là
      hạn chế;
    * chưa tham gia group không phải là hạn chế, tài khoản chỉ chưa vào thôi;
    * chỉ một thông báo rõ ràng từ Facebook mới được coi là hạn chế.
    """
    evidence = evidence or {}
    if evidence.get("manual_review"):
        return STATUS_UNKNOWN, "manual", 1.0

    check_status = str(evidence.get("check_status") or "")

    # 1. The check could not be carried out. Nothing was learned about the
    #    right to post, so this is a failure and not a restriction.
    if check_status in CHECK_FAILED_STATUSES:
        return STATUS_FAILED, "browser_check", 0.0

    # 2. The browser saw a clear answer.
    if check_status == CHECK_RESTRICTED:
        # Bằng chứng của phiên bản cũ: trình duyệt không xem được nội dung
        # group, nên đây không phải hạn chế đăng mà là không có quyền xem.
        if evidence.get("accessible") is False and evidence.get("is_member") is False:
            return STATUS_NOT_ACCESSIBLE, "browser_check", 0.9
        return STATUS_RESTRICTED, "browser_check", 0.95
    if check_status == CHECK_NOT_ACCESSIBLE:
        return STATUS_NOT_ACCESSIBLE, "browser_check", 0.9
    if check_status == CHECK_NOT_MEMBER:
        return STATUS_NOT_MEMBER, "browser_check", 0.9

    # 3. The three valued answers, for evidence that was collected without a
    #    browser run. ``None`` means there was not enough to tell.
    if evidence.get("accessible") is False:
        return STATUS_NOT_ACCESSIBLE, "browser_check", 0.9
    if evidence.get("is_member") is False:
        return STATUS_NOT_MEMBER, "browser_check", 0.9
    if evidence.get("can_post") is False:
        return STATUS_RESTRICTED, "browser_check", 0.95

    direct = int(evidence.get("direct_posts") or 0)
    pending = int(evidence.get("pending_posts") or 0)
    rejected = int(evidence.get("rejected_posts") or 0)
    last_direct = evidence.get("last_direct_post_at")
    last_pending = evidence.get("last_pending_post_at")
    last_rejected = evidence.get("last_rejected_post_at")

    # A rejection that came after the last good post means the right to post
    # is gone. An older rejection does not.
    if rejected and _newer(last_rejected, last_direct):
        return STATUS_RESTRICTED, "post_evidence", 0.8

    approval_notice = bool(evidence.get("requires_approval"))
    rules_notice = bool(evidence.get("rules_mention_approval"))
    if approval_notice or rules_notice:
        return STATUS_REVIEW, "browser_check", 0.75
    if pending and _newer(last_pending, last_direct):
        return STATUS_REVIEW, "post_evidence", 0.7

    if direct and is_fresh(last_direct):
        # One direct post is a hint, not a guarantee, so the confidence stays
        # clearly below certainty and grows with the amount of evidence.
        confidence = 0.5 + 0.1 * min(direct, 4)
        if check_status == "ok" and not approval_notice:
            confidence += 0.15
        return STATUS_PRIORITY, "post_evidence", round(min(confidence, MAX_CONFIDENCE), 2)

    return STATUS_UNKNOWN, "none", 0.0


def guidance_of(evidence: dict, verification_status: str = "") -> str:
    """Một câu ngắn hướng dẫn CTV, dựa trên quan sát và bằng chứng đăng bài."""
    if verification_status == "verified_posted":
        return "Đã có bài đăng thật trong group bằng tài khoản này."
    if verification_status == "pending_group_approval":
        return "Bài đã gửi và đang chờ Admin group duyệt."
    if verification_status == "submitted_unverified":
        return "Đã gửi bài nhưng chưa xác nhận được Facebook tiếp nhận."
    if verification_status == "rejected":
        return "Bài đăng bị từ chối. Cần xem lại nội dung hoặc quyền của tài khoản."
    if verification_status == "restricted_confirmed":
        return "Facebook đã báo tài khoản không được đăng trong group này."
    if verification_status == "check_failed":
        return "Chưa xác minh được. Bấm “Xác minh bằng bài đăng” để thử lại."
    if verification_status == "test_in_progress":
        return "Đang thực hiện xác minh."
    if not (evidence or {}).get("check_status"):
        return "Chưa có bằng chứng đăng bài cho cặp Facebook–Group này."
    return evidence_summary(evidence)


def priority_index(status: str | None) -> int:
    return PRIORITY_ORDER.get(str(status or STATUS_UNKNOWN), PRIORITY_ORDER[STATUS_UNKNOWN])


def is_publishable(status: str | None) -> bool:
    """Only a verified group or a group that asks for approval is served."""
    return str(status or STATUS_UNKNOWN) in (STATUS_PRIORITY, STATUS_REVIEW)


def job_outcome(state: str, details: str, group_requires_approval: bool) -> str:
    """Classify the result of a posting job as evidence."""
    text = (details or "").lower()
    if state == "posted":
        return "direct"
    if state in ("failed", "needs_review", "cancelled"):
        if any(marker in text for marker in ("từ chối", "rejected", "bị gỡ", "removed", "không được duyệt",
                                            "xóa bài", "bị chặn", "restricted")):
            return "rejected"
        return "neutral"
    if state in ("submitted_unverified", "pending_approval"):
        return "pending"
    return "neutral"


def evidence_from_rows(conn, account_id: int, group_id: int) -> dict:
    """Collect the posting history of one account in one group."""
    rows = conn.execute("""SELECT state, details, updated_at FROM jobs
                           WHERE account_id=? AND group_id=? ORDER BY id""",
                        (account_id, group_id)).fetchall()
    group = conn.execute("SELECT requires_approval FROM groups WHERE id=?", (group_id,)).fetchone()
    requires_approval = bool(group["requires_approval"]) if group else False
    direct = pending = rejected = 0
    last_direct = last_pending = last_rejected = None
    for row in rows:
        outcome = job_outcome(row["state"], row["details"], requires_approval)
        if outcome == "direct":
            direct += 1
            last_direct = row["updated_at"]
        elif outcome == "pending":
            pending += 1
            last_pending = row["updated_at"]
        elif outcome == "rejected":
            rejected += 1
            last_rejected = row["updated_at"]
    return {"direct_posts": direct, "pending_posts": pending, "rejected_posts": rejected,
            "last_direct_post_at": last_direct, "last_pending_post_at": last_pending,
            "last_rejected_post_at": last_rejected}


def build_evidence(conn, pipeline_id: int, account_id: int, group_id: int,
                   check: dict | None = None) -> dict:
    """Merge the browser check with the posting history of the pair."""
    evidence = evidence_from_rows(conn, account_id, group_id)
    check = check or {}
    group = conn.execute("SELECT manual_review FROM groups WHERE id=?", (group_id,)).fetchone()
    evidence.update({
        "manual_review": bool(group["manual_review"]) if group else False,
        "check_status": check.get("status", ""),
        "accessible": check.get("accessible"),
        "is_member": check.get("is_member"),
        "can_post": check.get("can_post"),
        "requires_approval": check.get("requires_approval"),
        "rules_mention_approval": check.get("rules_mention_approval"),
        "supports_anonymous": check.get("supports_anonymous"),
        "supports_nickname": check.get("supports_nickname"),
        "nickname_available": check.get("nickname_available"),
        "group_name": check.get("group_name", ""),
        "check_details": check.get("details", ""),
    })
    return evidence


def evidence_summary(evidence: dict) -> str:
    """A short human readable reason for the classification."""
    if evidence.get("manual_review"):
        return "Người dùng chuyển sang kiểm tra thủ công."
    check_status = str(evidence.get("check_status") or "")
    if check_status in CHECK_FAILED_STATUSES:
        return "Không kiểm tra được group: " + str(evidence.get("check_details", ""))[:200]
    if check_status == CHECK_RESTRICTED or evidence.get("can_post") is False:
        return str(evidence.get("check_details") or
                   "Facebook thông báo tài khoản không được đăng bài trong group này.")
    if check_status == CHECK_NOT_ACCESSIBLE or evidence.get("accessible") is False:
        return str(evidence.get("check_details") or
                   "Tài khoản không xem được nội dung của group.")
    if check_status == CHECK_NOT_MEMBER or evidence.get("is_member") is False:
        return "Tài khoản chưa tham gia group."
    if evidence.get("rejected_posts") and _newer(evidence.get("last_rejected_post_at"),
                                                 evidence.get("last_direct_post_at")):
        return "Bài gần nhất bị từ chối hoặc bị gỡ; tài khoản có thể đã mất quyền đăng."
    if evidence.get("requires_approval"):
        return "Group thông báo bài viết cần quản trị viên duyệt."
    if evidence.get("rules_mention_approval"):
        return "Quy định group có nhắc tới việc duyệt bài."
    if evidence.get("pending_posts"):
        return "Bài gần nhây còn chờ duyệt nên chưa xác nhận được đăng trực tiếp."
    if evidence.get("direct_posts") and is_fresh(evidence.get("last_direct_post_at")):
        return (f"{evidence['direct_posts']} bài gần đây đã đăng trực tiếp, "
                "không thấy thông báo cần duyệt.")
    if check_status == CHECK_NEEDS_REVIEW:
        return str(evidence.get("check_details") or
                   "Chưa xác minh được quyền đăng bài trong group này.")
    return "Chưa đủ bằng chứng để phân loại."


def next_action(evidence: dict) -> str:
    """Một hướng xử lý ngắn gọn cho CTV."""
    check_status = str(evidence.get("check_status") or "")
    if evidence.get("manual_review"):
        return "Mở group để kiểm tra thủ công."
    if check_status in CHECK_FAILED_STATUSES:
        return "Kiểm tra lại, hoặc đăng nhập lại Facebook nếu phiên đã hết hạn."
    if check_status == CHECK_RESTRICTED or evidence.get("can_post") is False:
        return "Mở group để kiểm tra thủ công, tài khoản này không được đăng bài ở đây."
    if check_status == CHECK_NOT_ACCESSIBLE or evidence.get("accessible") is False:
        return "Kiểm tra quyền xem group: group có thể riêng tư hoặc đã đổi người được xem."
    if check_status == CHECK_NOT_MEMBER or evidence.get("is_member") is False:
        return "Tham gia group bằng tài khoản này rồi kiểm tra lại."
    if evidence.get("requires_approval") or evidence.get("rules_mention_approval"):
        return "Bài sẽ cần Admin group duyệt trước khi hiện công khai."
    if check_status == CHECK_NEEDS_REVIEW:
        return "Kiểm tra lại, hoặc mở group để kiểm tra thủ công."
    if evidence.get("direct_posts") and is_fresh(evidence.get("last_direct_post_at")):
        return "Đã xác minh đăng trực tiếp, có thể xếp lịch tự động."
    return "Kiểm tra lại để xác minh quyền đăng."


def summary_of(conn, pipeline_id: int) -> dict:
    """Bộ đếm trạng thái kiểm tra của một Workspace."""
    counts = {status: 0 for status in SCREENING_STATUSES}
    latest = ""
    for row in conn.execute("""SELECT moderation_status, last_checked_at FROM group_screening_results
                               WHERE pipeline_id=?""", (int(pipeline_id),)).fetchall():
        counts[row["moderation_status"]] = counts.get(row["moderation_status"], 0) + 1
        if row["last_checked_at"] and row["last_checked_at"] > latest:
            latest = row["last_checked_at"]
    total = sum(counts.values())
    return {"total": total, "counts": counts, "last_checked_at": latest,
            "eligible": counts.get(STATUS_PRIORITY, 0) + counts.get(STATUS_REVIEW, 0)}


def screening_payload(row) -> dict:
    """The API shape of one screening result."""
    evidence = json.loads(row["evidence_json"] or "{}")
    item = dict(row)
    item["evidence"] = evidence
    item["evidence_summary"] = evidence_summary(evidence)
    item["next_action"] = next_action(evidence)
    item["status_label"] = STATUS_LABELS.get(row["moderation_status"], row["moderation_status"])
    item["priority"] = priority_index(row["moderation_status"])
    item["fresh"] = is_fresh(row["last_direct_post_at"])
    item["publishable"] = is_publishable(row["moderation_status"])
    return item


def reclassify_stored(conn, pipeline_id: int | None = None) -> int:
    """Phân loại lại các kết quả đã lưu theo đúng bằng chứng đang có.

    Bằng chứng không thay đổi, chỉ cách phân loại. Kết quả do người quyết định
    bằng tay thì giữ nguyên. Trả về số dòng đã phân loại lại.
    """
    sql = "SELECT * FROM group_screening_results"
    params: list = []
    if pipeline_id is not None:
        sql += " WHERE pipeline_id=?"
        params.append(int(pipeline_id))
    changed = 0
    for row in conn.execute(sql, params).fetchall():
        if row["verification_source"] == "manual":
            continue
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        status, source, confidence = classify(evidence)
        if status == row["moderation_status"]:
            continue
        conn.execute("""UPDATE group_screening_results
                         SET moderation_status=?, verification_source=?, confidence=?,
                             notes=?, updated_at=? WHERE id=?""",
                     (status, source, confidence, evidence_summary(evidence),
                      datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"]))
        conn.execute("""UPDATE groups SET screening_status=?, screening_source=?,
                        screening_confidence=? WHERE id=?""",
                     (status, source, confidence, row["group_id"]))
        changed += 1
    return changed
