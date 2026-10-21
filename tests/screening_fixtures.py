"""Helper để ghi lại bằng chứng xác minh của một cặp Facebook–Group.

Từ V13, quyết định đưa một group vào lịch đăng tự động dựa trên **bằng chứng
đăng bài thật** trong bảng ``group_post_verifications``, không dựa trên một
quan sát của trình duyệt. Bài kiểm thử dùng helper này để mô phỏng một cặp
đã được xác minh.
"""
import json

import app as m
from services import group_verification

#: Quan sát của trình duyệt về một group đã xác minh.
VERIFIED_OBSERVATION = {
    "check_status": "ok",
    "accessible": True,
    "is_member": True,
    "can_post": True,
    "requires_approval": False,
    "rules_mention_approval": False,
    "supports_anonymous": True,
    "supports_nickname": True,
    "nickname_available": True,
    "direct_posts": 1,
    "pending_posts": 0,
    "rejected_posts": 0,
    "last_direct_post_at": None,
    "last_pending_post_at": None,
    "last_rejected_post_at": None,
    "manual_review": False,
    "check_details": "",
    "group_name": "",
}

#: Những trạng thái kiểm tra cũ được ánh xạ sang bằng chứng đăng bài thật.
SCREENING_TO_VERIFICATION = {
    "priority": group_verification.VERIFIED_POSTED,
    "review": group_verification.PENDING_GROUP_APPROVAL,
    "restricted": group_verification.RESTRICTED_CONFIRMED,
    "rejected": group_verification.REJECTED,
    "failed": group_verification.CHECK_FAILED,
    "submitted_unverified": group_verification.SUBMITTED_UNVERIFIED,
    "not_member": group_verification.NOT_TESTED,
    "unknown": group_verification.NOT_TESTED,
    "not_accessible": group_verification.NOT_TESTED,
}


def verify_pair(conn, pipeline_id, account_id, group_id, status="priority",
                source="browser_check", evidence=None, confidence=0.9,
                posting_mode="anonymous", permalink=""):
    """Ghi lại bằng chứng xác minh cho đúng một cặp Facebook–Group."""
    observation = dict(VERIFIED_OBSERVATION)
    if evidence:
        observation.update(evidence)
    verification = SCREENING_TO_VERIFICATION.get(status, group_verification.NOT_TESTED)
    stamp = m.iso()
    direct_at = observation.get("last_direct_post_at") or stamp
    conn.execute("""INSERT INTO group_screening_results
        (pipeline_id,account_id,group_id,moderation_status,verification_source,confidence,
         evidence_json,last_checked_at,expires_at,last_direct_post_at,last_pending_post_at,
         last_rejected_post_at,direct_posts,pending_posts,rejected_posts,notes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id,group_id) DO UPDATE SET
            moderation_status=excluded.moderation_status,
            verification_source=excluded.verification_source,
            confidence=excluded.confidence,
            evidence_json=excluded.evidence_json,
            last_checked_at=excluded.last_checked_at,
            updated_at=excluded.updated_at""",
                 (pipeline_id, account_id, group_id, status, source, confidence,
                  json.dumps(observation, ensure_ascii=False), stamp, stamp, direct_at,
                  observation.get("last_pending_post_at"), observation.get("last_rejected_post_at"),
                  int(observation.get("direct_posts") or 0), int(observation.get("pending_posts") or 0),
                  int(observation.get("rejected_posts") or 0), "", stamp, stamp))
    group_verification.record_outcome(
        conn, pipeline_id, account_id, group_id, verification,
        post_url=permalink, posting_mode=posting_mode,
        evidence_reference="test-evidence", error_code="")
    return group_id
