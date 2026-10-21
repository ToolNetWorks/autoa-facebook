"""Automatic group screening and the priority engine.

A group is classified from evidence that can actually be observed:

* what the browser saw in the group (access, membership, composer, approval
  notices, the group rules when they are readable),
* what happened to the posts this account made in that group.

Nothing is ever published to find out. A single successful post is evidence,
not a guarantee, so the confidence never reaches certainty and the priority
only changes the order of the jobs that are allowed to run anyway.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

STATUS_PRIORITY = "priority"
STATUS_REVIEW = "review"
STATUS_UNKNOWN = "unknown"
STATUS_RESTRICTED = "restricted"
STATUS_FAILED = "failed"

SCREENING_STATUSES = (STATUS_PRIORITY, STATUS_REVIEW, STATUS_UNKNOWN, STATUS_RESTRICTED, STATUS_FAILED)

STATUS_LABELS = {
    STATUS_PRIORITY: "Đã xác minh đăng trực tiếp",
    STATUS_REVIEW: "Cần Admin group duyệt",
    STATUS_UNKNOWN: "Chưa xác định",
    STATUS_RESTRICTED: "Nhóm hạn chế",
    STATUS_FAILED: "Không kiểm tra được",
}

# The order in which eligible groups are served. Restricted and failed groups
# are never served automatically.
PRIORITY_ORDER = {
    STATUS_PRIORITY: 0,
    STATUS_REVIEW: 1,
    STATUS_UNKNOWN: 2,
    STATUS_RESTRICTED: 3,
    STATUS_FAILED: 4,
}

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
    """Return (status, source, confidence) for the collected evidence.

    The rules are deliberately conservative: anything that cannot be confirmed
    stays unknown, and a priority is only given to a group with recent, direct
    evidence that its posts go out without approval.
    """
    evidence = evidence or {}
    if evidence.get("manual_review"):
        return STATUS_UNKNOWN, "manual", 1.0

    check_status = str(evidence.get("check_status") or "")
    if check_status in ("error", "busy", "failed"):
        return STATUS_FAILED, "browser_check", 0.0
    if evidence.get("accessible") is False:
        return STATUS_RESTRICTED, "browser_check", 0.9
    if evidence.get("is_member") is False:
        return STATUS_RESTRICTED, "browser_check", 0.9
    if evidence.get("can_post") is False:
        return STATUS_RESTRICTED, "browser_check", 0.9

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
        "group_name": check.get("group_name", ""),
        "check_details": check.get("details", ""),
    })
    return evidence


def evidence_summary(evidence: dict) -> str:
    """A short human readable reason for the classification."""
    parts = []
    if evidence.get("manual_review"):
        return "Người dùng chuyển sang kiểm tra thủ công."
    if evidence.get("check_status") in ("error", "busy", "failed"):
        return "Không kiểm tra được group: " + str(evidence.get("check_details", ""))[:200]
    if evidence.get("accessible") is False:
        return "Tài khoản không truy cập được group."
    if evidence.get("is_member") is False:
        return "Tài khoản chưa tham gia group."
    if evidence.get("can_post") is False:
        return "Tài khoản không có quyền tạo bài viết trong group."
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
    return "Chưa đủ bằng chứng để phân loại."


def screening_payload(row) -> dict:
    """The API shape of one screening result."""
    evidence = json.loads(row["evidence_json"] or "{}")
    item = dict(row)
    item["evidence"] = evidence
    item["evidence_summary"] = evidence_summary(evidence)
    item["status_label"] = STATUS_LABELS.get(row["moderation_status"], row["moderation_status"])
    item["priority"] = priority_index(row["moderation_status"])
    item["fresh"] = is_fresh(row["last_direct_post_at"])
    return item
