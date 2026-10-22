"""Xác minh quyền đăng của một cặp Facebook–Group bằng bằng chứng thật.

Không có suy đoán nào ở đây. Một group chỉ được coi là đã xác minh khi đã
có **một lần gửi bài thật** vào đúng group bằng đúng tài khoản Facebook, và
kết quả của lần gửi đó đã được quan sát.

Tám trạng thái:

``NOT_TESTED``
    Chưa có bài đăng thực tế để xác minh.
``TEST_IN_PROGRESS``
    Đang thực hiện xác minh.
``SUBMITTED_UNVERIFIED``
    Đã gửi thao tác nhưng chưa xác nhận được Facebook tiếp nhận.
``PENDING_GROUP_APPROVAL``
    Facebook xác nhận bài đang chờ quản trị viên group duyệt.
``VERIFIED_POSTED``
    Đã xác minh bài xuất hiện trong group theo đúng tài khoản và chế độ đăng.
``REJECTED``
    Bài bị từ chối, có bằng chứng thực tế.
``RESTRICTED_CONFIRMED``
    Facebook thông báo hạn chế đăng rõ ràng, có bằng chứng.
``CHECK_FAILED``
    Không xác định được kết quả vì lỗi hệ thống.

Một lần kiểm tra bằng trình duyệt, một timeout hay một giao diện đổi **không
bao giờ** dẫn tới ``RESTRICTED_CONFIRMED`` hay ``VERIFIED_POSTED``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

NOT_TESTED = "not_tested"
TEST_IN_PROGRESS = "test_in_progress"
SUBMITTED_UNVERIFIED = "submitted_unverified"
PENDING_GROUP_APPROVAL = "pending_group_approval"
VERIFIED_POSTED = "verified_posted"
REJECTED = "rejected"
RESTRICTED_CONFIRMED = "restricted_confirmed"
CHECK_FAILED = "check_failed"

STATUSES = (NOT_TESTED, TEST_IN_PROGRESS, SUBMITTED_UNVERIFIED, PENDING_GROUP_APPROVAL,
            VERIFIED_POSTED, REJECTED, RESTRICTED_CONFIRMED, CHECK_FAILED)

LABELS = {
    NOT_TESTED: "Chưa xác minh",
    TEST_IN_PROGRESS: "Đang xác minh",
    SUBMITTED_UNVERIFIED: "Đã gửi, chưa xác nhận được",
    PENDING_GROUP_APPROVAL: "Đang chờ Admin group duyệt",
    VERIFIED_POSTED: "Đã xác minh đăng thành công",
    REJECTED: "Bài bị từ chối",
    RESTRICTED_CONFIRMED: "Facebook hạn chế quyền đăng",
    CHECK_FAILED: "Không xác minh được",
}

TONES = {
    NOT_TESTED: "neutral",
    TEST_IN_PROGRESS: "info",
    SUBMITTED_UNVERIFIED: "warn",
    PENDING_GROUP_APPROVAL: "warn",
    VERIFIED_POSTED: "ok",
    REJECTED: "bad",
    RESTRICTED_CONFIRMED: "bad",
    CHECK_FAILED: "bad",
}

#: Những trạng thái cho thấy đã có bằng chứng gửi bài thật.
HAS_POST_EVIDENCE = (VERIFIED_POSTED, PENDING_GROUP_APPROVAL)

#: Những trạng thái cho thấy tài khoản đã chứng minh được nó đăng được vào
#: group này.
SERVABLE_STATUSES = (VERIFIED_POSTED, PENDING_GROUP_APPROVAL)

#: Những trạng thái cho phép bộ xếp lịch phục vụ cặp này.
#:
#: ``VERIFIED_POSTED`` là bài đã hiện trong group. ``PENDING_GROUP_APPROVAL`` là
#: bài đã gửi và đang chờ quản trị group duyệt: group đó vẫn đăng được, nhưng
#: ở mức ưu tiên thấp và số bài chờ duyệt được kiểm soát.
AUTO_POSTABLE_STATUSES = (VERIFIED_POSTED, PENDING_GROUP_APPROVAL)

#: Những trạng thái chặn bộ xếp lịch.
BLOCKING_STATUSES = (NOT_TESTED, TEST_IN_PROGRESS, SUBMITTED_UNVERIFIED, REJECTED,
                     RESTRICTED_CONFIRMED, CHECK_FAILED)

TABLE = "group_post_verifications"

#: Bằng chứng của một lần bị Facebook từ chối.
REJECTED_MARKERS = ("từ chối", "rejected", "bị gỡ", "removed", "không được duyệt",
                    "xóa bài", "bị chặn", "không thể đăng", "restricted")
#: Bằng chứng rõ ràng của một lần bị hạn chế quyền đăng.
RESTRICTED_MARKERS = ("hạn chế quyền đăng", "bị hạn chế", "không được đăng bài trong nhóm",
                      "you cannot post", "you can't post", "posting has been turned off",
                      "only admins can post", "not allowed to post")

#: Bằng chứng rõ ràng của một lần bị từ chối.
REJECTION_MARKERS = ("bị từ chối", "đã bị từ chối", "không được duyệt", "bị gỡ", "đã bị xóa")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def label_of(status: str) -> str:
    return LABELS.get(status, LABELS[NOT_TESTED])


def tone_of(status: str) -> str:
    return TONES.get(status, TONES[NOT_TESTED])


def is_servable(status: str) -> bool:
    """Bộ xếp lịch có phục vụ cặp này không.

    Cả hai đều là bằng chứng thật: bài đã hiện trong group, hoặc đã được
    Facebook tiếp nhận và đang chờ quản trị group duyệt. Thứ tự ưu tiên
    giữa hai loại do phân loại quyết định, không phải ở đây.
    """
    return str(status or NOT_TESTED) in AUTO_POSTABLE_STATUSES


def status_from_job(state: str, details: str) -> str:
    """Trạng thái xác minh suy ra từ kết quả thật của một lần gửi bài.

    Chỉ một lần gửi bài thật mới cho ra trạng thái nào ngoài ``CHECK_FAILED``.
    """
    text = (details or "").lower()
    lowered_state = str(state or "").lower()
    if any(marker in text for marker in RESTRICTED_MARKERS):
        return RESTRICTED_CONFIRMED
    if lowered_state == "posted":
        return VERIFIED_POSTED
    if lowered_state == "pending_approval":
        return PENDING_GROUP_APPROVAL
    if lowered_state == "submitted_unverified":
        return SUBMITTED_UNVERIFIED
    if lowered_state in ("failed", "needs_review", "cancelled"):
        if any(marker in text for marker in REJECTION_MARKERS):
            return REJECTED
        return CHECK_FAILED
    return CHECK_FAILED


def ensure_schema(conn) -> None:
    """Tạo bảng bằng chứng xác minh nếu chưa có. Không đụng dữ liệu cũ."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            verification_status TEXT NOT NULL DEFAULT '{NOT_TESTED}',
            verified_post_id INTEGER,
            verified_post_url TEXT NOT NULL DEFAULT '',
            verified_posting_mode TEXT NOT NULL DEFAULT '',
            submitted_at TEXT,
            verified_at TEXT,
            evidence_reference TEXT NOT NULL DEFAULT '',
            last_error_code TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(pipeline_id, account_id, group_id)
        )""")
    conn.execute(f"""CREATE INDEX IF NOT EXISTS idx_group_post_verifications_pair
                     ON {TABLE}(pipeline_id, account_id, group_id)""")
    conn.execute(f"""CREATE INDEX IF NOT EXISTS idx_group_post_verifications_status
                     ON {TABLE}(pipeline_id, verification_status)""")


def row_of(conn, pipeline_id: int, account_id: int, group_id: int):
    """Bằng chứng xác minh của đúng một cặp Facebook–Group."""
    return conn.execute(f"""SELECT * FROM {TABLE}
                            WHERE pipeline_id=? AND account_id=? AND group_id=?""",
                        (int(pipeline_id), int(account_id), int(group_id))).fetchone()


def status_of(conn, pipeline_id: int, account_id: int, group_id: int) -> str:
    """Trạng thái xác minh của một cặp, ``NOT_TESTED`` nếu chưa có bằng chứng."""
    row = row_of(conn, pipeline_id, account_id, group_id)
    if row is None:
        return NOT_TESTED
    return row["verification_status"]


def servable_status_of(conn, pipeline_id: int, account_id: int, group_id: int,
                       current_mode: str = "anonymous") -> str:
    """Trạng thái xác minh, chỉ tính khi bằng chứng còn phù hợp."""
    row = row_of(conn, pipeline_id, account_id, group_id)
    if row is None:
        return NOT_TESTED
    if row["verification_status"] not in HAS_POST_EVIDENCE:
        return row["verification_status"]
    if row["verified_posting_mode"] and row["verified_posting_mode"] != current_mode:
        # Chế độ đăng đã xác minh khác chế độ đang dùng thì bằng chứng không
        # còn phù hợp.
        return NOT_TESTED
    return row["verification_status"]


def start_attempt(conn, pipeline_id: int, account_id: int, group_id: int,
                  post_id: int | None = None, posting_mode: str = "",
                  evidence_reference: str = "") -> None:
    """Ghi nhận một lần xác minh đang thực hiện."""
    stamp = now_iso()
    conn.execute(f"""INSERT INTO {TABLE}
        (pipeline_id,account_id,group_id,verification_status,verified_post_id,
         verified_posting_mode,submitted_at,evidence_reference,last_error_code,updated_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,'',?,?)
        ON CONFLICT(pipeline_id,account_id,group_id) DO UPDATE SET
            verification_status=excluded.verification_status,
            verified_post_id=excluded.verified_post_id,
            verified_posting_mode=excluded.verified_posting_mode,
            submitted_at=excluded.submitted_at,
            evidence_reference=excluded.evidence_reference,
            last_error_code='',
            updated_at=excluded.updated_at""",
                 (int(pipeline_id), int(account_id), int(group_id), TEST_IN_PROGRESS,
                  post_id, posting_mode, stamp, evidence_reference, stamp, stamp))
    sync_group_status(conn, pipeline_id, group_id)


def record_outcome(conn, pipeline_id: int, account_id: int, group_id: int, status: str,
                   post_id: int | None = None, post_url: str = "",
                   posting_mode: str = "", evidence_reference: str = "",
                   error_code: str = "") -> None:
    """Ghi lại kết quả thật của một lần gửi bài."""
    if status not in STATUSES:
        status = CHECK_FAILED
    stamp = now_iso()
    verified_at = stamp if status in (VERIFIED_POSTED, PENDING_GROUP_APPROVAL) else None
    conn.execute(f"""INSERT INTO {TABLE}
        (pipeline_id,account_id,group_id,verification_status,verified_post_id,verified_post_url,
         verified_posting_mode,submitted_at,verified_at,evidence_reference,last_error_code,
         updated_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pipeline_id,account_id,group_id) DO UPDATE SET
            verification_status=excluded.verification_status,
            verified_post_id=excluded.verified_post_id,
            verified_post_url=excluded.verified_post_url,
            verified_posting_mode=excluded.verified_posting_mode,
            verified_at=excluded.verified_at,
            evidence_reference=excluded.evidence_reference,
            last_error_code=excluded.last_error_code,
            updated_at=excluded.updated_at""",
                 (int(pipeline_id), int(account_id), int(group_id), status, post_id, post_url,
                  posting_mode, stamp, verified_at, evidence_reference, error_code, stamp, stamp))
    sync_group_status(conn, pipeline_id, group_id)


def sync_group_status(conn, pipeline_id: int, group_id: int) -> None:
    """Cập nhật cột hiển thị trên group theo bằng chứng của các cặp của group."""
    rows = conn.execute(f"""SELECT verification_status FROM {TABLE}
                            WHERE pipeline_id=? AND group_id=?""",
                        (int(pipeline_id), int(group_id))).fetchall()
    statuses = [row["verification_status"] for row in rows]
    if not statuses:
        status = NOT_TESTED
    elif any(item == VERIFIED_POSTED for item in statuses):
        status = VERIFIED_POSTED
    elif any(item == PENDING_GROUP_APPROVAL for item in statuses):
        status = PENDING_GROUP_APPROVAL
    elif any(item == SUBMITTED_UNVERIFIED for item in statuses):
        status = SUBMITTED_UNVERIFIED
    elif any(item == TEST_IN_PROGRESS for item in statuses):
        status = TEST_IN_PROGRESS
    elif any(item == REJECTED for item in statuses):
        status = REJECTED
    elif any(item == RESTRICTED_CONFIRMED for item in statuses):
        status = RESTRICTED_CONFIRMED
    elif any(item == CHECK_FAILED for item in statuses):
        status = CHECK_FAILED
    else:
        status = NOT_TESTED
    conn.execute("""UPDATE groups SET screening_status=?, screening_source='post_evidence',
                    screening_checked_at=? WHERE id=?""",
                 (status, now_iso(), int(group_id)))


def payload(row) -> dict:
    """Dạng API của một bằng chứng xác minh."""
    if row is None:
        return {"verification_status": NOT_TESTED, "status_label": label_of(NOT_TESTED),
                "status_tone": tone_of(NOT_TESTED), "has_evidence": False,
                "auto_postable": False, "verified_post_url": "", "verified_posting_mode": "",
                "submitted_at": None, "verified_at": None, "last_error_code": ""}
    item = dict(row)
    status = item.get("verification_status") or NOT_TESTED
    item["verification_status"] = status
    item["status_label"] = label_of(status)
    item["status_tone"] = tone_of(status)
    item["has_evidence"] = status in HAS_POST_EVIDENCE
    item["auto_postable"] = status in AUTO_POSTABLE_STATUSES
    item["evidence_reference"] = item.get("evidence_reference") or ""
    return item


def list_of(conn, pipeline_id: int) -> list[dict]:
    """Mọi bằng chứng xác minh của một Workspace."""
    rows = conn.execute(f"""SELECT * FROM {TABLE} WHERE pipeline_id=?
                            ORDER BY group_id, account_id""", (int(pipeline_id),)).fetchall()
    return [payload(row) for row in rows]


def pairs_of(conn, pipeline_id: int) -> dict:
    """Bằng chứng theo cặp, để tra nhanh."""
    result = {}
    for row in conn.execute(f"""SELECT * FROM {TABLE} WHERE pipeline_id=?""",
                            (int(pipeline_id),)).fetchall():
        result[(int(row["account_id"]), int(row["group_id"]))] = payload(row)
    return result


#: Lỗi kỹ thuật: có thể thử lại. Lỗi quyền hoặc phiên đăng nhập thì không.
TRANSIENT_FAILURE_MARKERS = (
    "timeout", "timed out", "quá thời gian", "không xác nhận được", "not found",
    "không tìm thấy", "không bấm được", "không mở được", "không thấy",
    "publish control", "browser", "exception", "không đọc được",
)
PERMANENT_FAILURE_MARKERS = (
    "login", "checkpoint", "đăng nhập", "session", "phiên", "quyền", "permission",
    "not a member", "bị hạn chế", "restricted", "từ chối", "rejected", "spam",
)


def is_transient_failure(details: str) -> bool:
    """Lỗi kỹ thuật thì được thử lại, lỗi quyền hoặc phiên thì không."""
    text = str(details or "").lower()
    if any(marker in text for marker in PERMANENT_FAILURE_MARKERS):
        return False
    return any(marker in text for marker in TRANSIENT_FAILURE_MARKERS)


def sync_from_job(conn, job_id: int) -> None:
    """Cập nhật bằng chứng xác minh từ kết quả thật của một lần gửi bài.

    Một lần gửi bài thất bại vì lý do không liên quan (tài khoản bận, bài đã
    có) không được xóa bằng chứng tốt đã có. Chỉ một kết quả rõ ràng mới
    thay đổi trạng thái.
    """
    job = conn.execute("""SELECT j.id, j.post_id, j.group_id, j.account_id, j.pipeline_id, j.state,
                                j.details, j.permalink, j.evidence, j.actual_posting_identity,
                                j.requested_posting_mode, j.requested_nickname
                         FROM jobs j WHERE j.id=?""", (int(job_id),)).fetchone()
    if job is None:
        return
    pipeline_id = job["pipeline_id"]
    if pipeline_id is None:
        return
    status = status_from_job(job["state"], job["details"])
    current = row_of(conn, pipeline_id, job["account_id"], job["group_id"])
    previous = current["verification_status"] if current else NOT_TESTED
    if status == CHECK_FAILED and previous in (VERIFIED_POSTED, PENDING_GROUP_APPROVAL):
        # Bằng chứng tốt đã có, giữ nguyên; chỉ ghi lại lỗi.
        record_outcome(conn, pipeline_id, job["account_id"], job["group_id"], previous,
                       post_id=job["post_id"], post_url=current["verified_post_url"] if current else "",
                       posting_mode=current["verified_posting_mode"] if current else "",
                       evidence_reference=current["evidence_reference"] if current else "",
                       error_code=str(job["state"])[:40])
        return
    permalink = str(job["permalink"] or "")[:500]
    mode = str(job["actual_posting_identity"] or job["requested_posting_mode"] or "")[:40]
    record_outcome(conn, pipeline_id, job["account_id"], job["group_id"], status,
                   post_id=job["post_id"], post_url=permalink, posting_mode=mode,
                   evidence_reference=str(job["evidence"] or "")[:200],
                   error_code="" if status in HAS_POST_EVIDENCE else str(job["state"])[:40])


def questions_json(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
