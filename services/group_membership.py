"""Tự động yêu cầu tham gia group và theo dõi tư cách thành viên.

Mỗi cặp Facebook–Group có một trạng thái thành viên riêng. Bảy trạng thái:

``NOT_MEMBER``
    Tài khoản chưa tham gia group.
``JOIN_REQUESTING``
    Đang gửi yêu cầu tham gia.
``PENDING_APPROVAL``
    Đã gửi yêu cầu, đang chờ group duyệt.
``JOINED``
    Đã là thành viên, có bằng chứng quan sát được.
``JOIN_REJECTED``
    Yêu cầu bị từ chối.
``JOIN_FAILED``
    Có lỗi khi thực hiện, không xác định được kết quả.
``UNKNOWN``
    Không đủ dữ liệu để kết luận.

Một yêu cầu đang chờ duyệt không bao giờ được coi là ``JOINED``. Một lần thử
không xác định được kết quả cũng không được coi là thất bại, và không được gửi
lại khi chưa rõ.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

NOT_MEMBER = "not_member"
JOIN_REQUESTING = "join_requesting"
PENDING_APPROVAL = "pending_approval"
JOINED = "joined"
JOIN_REJECTED = "join_rejected"
JOIN_FAILED = "join_failed"
UNKNOWN = "unknown"

STATUSES = (NOT_MEMBER, JOIN_REQUESTING, PENDING_APPROVAL, JOINED, JOIN_REJECTED,
            JOIN_FAILED, UNKNOWN)

LABELS = {
    NOT_MEMBER: "Chưa tham gia",
    JOIN_REQUESTING: "Đang gửi yêu cầu",
    PENDING_APPROVAL: "Đang chờ group duyệt",
    JOINED: "Đã là thành viên",
    JOIN_REJECTED: "Bị từ chối",
    JOIN_FAILED: "Tham gia lỗi",
    UNKNOWN: "Chưa xác định",
}

TONES = {
    NOT_MEMBER: "warn",
    JOIN_REQUESTING: "info",
    PENDING_APPROVAL: "warn",
    JOINED: "ok",
    JOIN_REJECTED: "bad",
    JOIN_FAILED: "bad",
    UNKNOWN: "neutral",
}

#: Những trạng thái đã rõ: không gửi lại yêu cầu.
SETTLED_STATUSES = (JOINED, JOIN_REJECTED, PENDING_APPROVAL)

TABLE = "group_memberships"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def label_of(status: str) -> str:
    return LABELS.get(status, LABELS[UNKNOWN])


def tone_of(status: str) -> str:
    return TONES.get(status, TONES[UNKNOWN])


def ensure_schema(conn) -> None:
    """Tạo bảng tư cách thành viên nếu chưa có."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            membership_status TEXT NOT NULL DEFAULT '{UNKNOWN}',
            join_requested_at TEXT,
            join_answered_at TEXT,
            join_questions_json TEXT NOT NULL DEFAULT '[]',
            join_answers_json TEXT NOT NULL DEFAULT '{{}}',
            last_error_code TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT,
            updated_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(pipeline_id, account_id, group_id)
        )""")
    conn.execute(f"""CREATE INDEX IF NOT EXISTS idx_group_memberships_pair
                     ON {TABLE}(pipeline_id, account_id, group_id)""")


def row_of(conn, pipeline_id: int, account_id: int, group_id: int):
    return conn.execute(f"""SELECT * FROM {TABLE}
                            WHERE pipeline_id=? AND account_id=? AND group_id=?""",
                        (int(pipeline_id), int(account_id), int(group_id))).fetchone()


def status_of(conn, pipeline_id: int, account_id: int, group_id: int) -> str:
    row = row_of(conn, pipeline_id, account_id, group_id)
    return row["membership_status"] if row else UNKNOWN


def status_from_outcome(outcome: dict) -> tuple[str, str]:
    """Trạng thái thành viên từ kết quả quan sát được của trình duyệt.

    Trả về (trạng thái, mã lỗi). Một lần thử không xác định được kết quả thì
    là ``UNKNOWN``, không phải là từ chối.
    """
    outcome = outcome or {}
    state = str(outcome.get("state") or "failed")
    status = str(outcome.get("status") or "error")
    if state == "ok" and status == "joined":
        return JOINED, ""
    if state == "ok" and status == "pending_approval":
        return PENDING_APPROVAL, ""
    if state == "needs_answers":
        return NOT_MEMBER, "needs_answers"
    if status == "rejected":
        return JOIN_REJECTED, status
    if status in ("checkpoint", "unknown"):
        return UNKNOWN, status
    return JOIN_FAILED, status


def record(conn, pipeline_id: int, account_id: int, group_id: int, status: str,
           questions: list | None = None, answers: dict | None = None,
           error_code: str = "") -> None:
    """Ghi lại trạng thái thành viên của một cặp."""
    if status not in STATUSES:
        status = UNKNOWN
    stamp = now_iso()
    current = row_of(conn, pipeline_id, account_id, group_id)
    questions_json = json.dumps(questions or [], ensure_ascii=False)
    answers_json = json.dumps(answers or {}, ensure_ascii=False)
    requested = current["join_requested_at"] if current else None
    if status == JOIN_REQUESTING and not requested:
        requested = stamp
    answered = current["join_answered_at"] if current else None
    if answers:
        answered = stamp
    conn.execute(f"""INSERT INTO {TABLE}
        (pipeline_id,account_id,group_id,membership_status,join_requested_at,join_answered_at,
         join_questions_json,join_answers_json,last_error_code,last_checked_at,updated_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pipeline_id,account_id,group_id) DO UPDATE SET
            membership_status=excluded.membership_status,
            join_requested_at=excluded.join_requested_at,
            join_answered_at=excluded.join_answered_at,
            join_questions_json=excluded.join_questions_json,
            join_answers_json=excluded.join_answers_json,
            last_error_code=excluded.last_error_code,
            last_checked_at=excluded.last_checked_at,
            updated_at=excluded.updated_at""",
                 (int(pipeline_id), int(account_id), int(group_id), status, requested, answered,
                  questions_json, answers_json, error_code, stamp, stamp, stamp))


def payload(row) -> dict:
    """Dạng API của trạng thái thành viên."""
    if row is None:
        return {"membership_status": UNKNOWN, "status_label": label_of(UNKNOWN),
                "status_tone": tone_of(UNKNOWN), "join_questions": [],
                "join_answers": {}, "last_checked_at": None, "join_requested_at": None,
                "last_error_code": ""}
    item = dict(row)
    status = item.get("membership_status") or UNKNOWN
    item["membership_status"] = status
    item["status_label"] = label_of(status)
    item["status_tone"] = tone_of(status)
    try:
        item["join_questions"] = json.loads(item.get("join_questions_json") or "[]")
    except (TypeError, ValueError):
        item["join_questions"] = []
    try:
        item["join_answers"] = json.loads(item.get("join_answers_json") or "{}")
    except (TypeError, ValueError):
        item["join_answers"] = {}
    item.pop("join_questions_json", None)
    item.pop("join_answers_json", None)
    return item


def pairs_of(conn, pipeline_id: int) -> dict:
    """Trạng thái thành viên theo cặp, để tra nhanh."""
    result = {}
    for row in conn.execute(f"""SELECT * FROM {TABLE} WHERE pipeline_id=?""",
                            (int(pipeline_id),)).fetchall():
        result[(int(row["account_id"]), int(row["group_id"]))] = payload(row)
    return result


def pending_requests(conn, pipeline_id: int, limit: int = 5) -> list[dict]:
    """Các cặp đang cần gửi yêu cầu tham gia.

    Chỉ những group mà CTV đã bật tự động tham gia, và chỉ khi trạng thái
    chưa rõ ràng: đang chờ duyệt hoặc đã thành viên thì không gửi lại.
    """
    rows = conn.execute(f"""SELECT m.*, g.url group_url, g.label group_label, g.auto_join,
                                  g.join_answers_json, g.posting_mode, g.nickname,
                                  a.label account_label
                           FROM {TABLE} m JOIN groups g ON g.id=m.group_id
                           JOIN accounts a ON a.id=m.account_id
                           WHERE m.pipeline_id=? AND g.auto_join=1 AND g.enabled=1
                             AND m.membership_status NOT IN ({','.join('?' * len(SETTLED_STATUSES))})
                           ORDER BY m.id LIMIT ?""",
                        (int(pipeline_id), *SETTLED_STATUSES, int(limit))).fetchall()
    return [dict(row) for row in rows]
