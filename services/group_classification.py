"""Phân loại group và khả năng đăng của một cặp Facebook–Group.

Hệ thống tự học từ kết quả đăng bài thật:

``no_approval``
    Group Đăng Không Cần Duyệt — bài đã xuất hiện trong group mà không qua
    bước duyệt của quản trị group. Ưu tiên **HIGH**.

``needs_approval``
    Group Cần Duyệt — Facebook xác nhận bài đang chờ quản trị group duyệt.
    Ưu tiên **LOW**.

``unclassified``
    Chưa có lần đăng đầu tiên nào, nên chưa có bằng chứng để phân loại.

Bốn thứ được giữ riêng biệt, không trộn vào một trường:

* **Phân loại** — kết quả về cơ chế duyệt bài.
* **Khả năng đăng** — các danh tính mà group thực sự hỗ trợ.
* **Chế độ đã chọn** — chế độ CTV muốn dùng (nằm ở ``group_accounts``).
* **Bằng chứng** — kết quả đăng bài (nằm ở ``group_post_verifications``).

Một bài chờ duyệt rồi được quản trị group chấp nhận vẫn thuộc nhóm *Cần
Duyệt*, vì cách gửi bài thực tế của group đó vẫn phải qua bước duyệt.
"""
from __future__ import annotations

from datetime import datetime, timezone

NO_APPROVAL = "no_approval"
NEEDS_APPROVAL = "needs_approval"
UNCLASSIFIED = "unclassified"

CLASSIFICATIONS = (NO_APPROVAL, NEEDS_APPROVAL, UNCLASSIFIED)

PRIORITY_HIGH = "high"
PRIORITY_LOW = "low"
PRIORITY_NONE = "none"

#: Thứ tự ưu tiên của bộ xếp lịch.
PRIORITY_ORDER = {NO_APPROVAL: 0, UNCLASSIFIED: 1, NEEDS_APPROVAL: 2}

LABELS = {
    NO_APPROVAL: "Group Đăng Không Cần Duyệt",
    NEEDS_APPROVAL: "Group Cần Duyệt",
    UNCLASSIFIED: "Chưa phân loại",
}

TONES = {NO_APPROVAL: "ok", NEEDS_APPROVAL: "warn", UNCLASSIFIED: "neutral"}

#: Trạng thái của khả năng đăng.
CAPABILITY_UNKNOWN = "unknown"
CAPABILITY_KNOWN = "known"

TABLE = "group_classifications"
HISTORY_TABLE = "group_classification_history"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn) -> None:
    """Tạo bảng phân loại và lịch sử thay đổi, nếu chưa có."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            classification TEXT NOT NULL DEFAULT '{UNCLASSIFIED}',
            priority TEXT NOT NULL DEFAULT '{PRIORITY_NONE}',
            supports_anonymous INTEGER NOT NULL DEFAULT 0,
            supports_nickname INTEGER NOT NULL DEFAULT 0,
            capability_state TEXT NOT NULL DEFAULT '{CAPABILITY_UNKNOWN}',
            last_verified_at TEXT,
            last_post_status TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(pipeline_id, account_id, group_id)
        )""")
    conn.execute(f"""CREATE INDEX IF NOT EXISTS idx_group_classifications_pipeline
                     ON {TABLE}(pipeline_id, classification)""")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            from_classification TEXT NOT NULL DEFAULT '',
            to_classification TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            evidence_reference TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )""")
    conn.execute(f"""CREATE INDEX IF NOT EXISTS idx_group_classification_history_pair
                     ON {HISTORY_TABLE}(pipeline_id, account_id, group_id, id)""")


def classification_of(conn, pipeline_id: int, account_id: int, group_id: int) -> dict:
    """Phân loại và khả năng đăng của một cặp, theo đúng cặp đó."""
    row = conn.execute(f"""SELECT * FROM {TABLE}
                           WHERE pipeline_id=? AND account_id=? AND group_id=?""",
                        (int(pipeline_id), int(account_id), int(group_id))).fetchone()
    if row is None:
        return {
            "classification": UNCLASSIFIED, "label": LABELS[UNCLASSIFIED], "tone": TONES[UNCLASSIFIED],
            "priority": PRIORITY_NONE, "priority_rank": PRIORITY_ORDER[UNCLASSIFIED],
            "supports_anonymous": False, "supports_nickname": False,
            "capability_state": CAPABILITY_UNKNOWN, "last_verified_at": None,
            "last_post_status": "", "configured": False,
        }
    item = dict(row)
    classification = item["classification"] or UNCLASSIFIED
    item["classification"] = classification
    item["label"] = LABELS.get(classification, LABELS[UNCLASSIFIED])
    item["tone"] = TONES.get(classification, TONES[UNCLASSIFIED])
    item["priority_rank"] = PRIORITY_ORDER.get(classification, PRIORITY_ORDER[UNCLASSIFIED])
    item["supports_anonymous"] = bool(item["supports_anonymous"])
    item["supports_nickname"] = bool(item["supports_nickname"])
    item["configured"] = True
    return item


def pairs_of(conn, pipeline_id: int) -> dict:
    """Phân loại của mọi cặp trong Workspace, tra nhanh theo (account, group)."""
    result = {}
    for row in conn.execute(f"""SELECT * FROM {TABLE} WHERE pipeline_id=?""",
                            (int(pipeline_id),)).fetchall():
        item = dict(row)
        classification = item["classification"] or UNCLASSIFIED
        item["classification"] = classification
        item["label"] = LABELS.get(classification, LABELS[UNCLASSIFIED])
        item["tone"] = TONES.get(classification, TONES[UNCLASSIFIED])
        item["priority_rank"] = PRIORITY_ORDER.get(classification, PRIORITY_ORDER[UNCLASSIFIED])
        item["supports_anonymous"] = bool(item["supports_anonymous"])
        item["supports_nickname"] = bool(item["supports_nickname"])
        item["configured"] = True
        result[(int(item["account_id"]), int(item["group_id"]))] = item
    return result


def capabilities_from_evidence(evidence: dict) -> dict:
    """Khả năng đăng mà group thực sự hỗ trợ, theo bằng chứng đã thấy.

    Chưa có bằng chứng thì để ở trạng thái chưa xác định, không khẳng định là
    group không hỗ trợ.
    """
    evidence = evidence or {}
    anonymous = evidence.get("supports_anonymous")
    nickname = evidence.get("supports_nickname")
    if anonymous is None and nickname is None:
        return {"supports_anonymous": False, "supports_nickname": False,
                "capability_state": CAPABILITY_UNKNOWN}
    return {"supports_anonymous": bool(anonymous), "supports_nickname": bool(nickname),
            "capability_state": CAPABILITY_KNOWN}


def classify_outcome(status: str, group_requires_approval: bool) -> tuple[str, str]:
    """Phân loại của một cặp, từ kết quả đăng bài thật.

    * Bài xuất hiện trong group mà group không cần duyệt → đăng không cần duyệt.
    * Bài xuất hiện trong group mà group cần duyệt → vẫn là *cần duyệt*.
    * Bài đang chờ quản trị group duyệt → *cần duyệt*.
    * Chưa có kết quả nào → *chưa phân loại*.
    """
    if status == "verified_posted":
        classification = NEEDS_APPROVAL if group_requires_approval else NO_APPROVAL
    elif status == "pending_group_approval":
        classification = NEEDS_APPROVAL
    else:
        classification = UNCLASSIFIED
    priority = {NO_APPROVAL: PRIORITY_HIGH, NEEDS_APPROVAL: PRIORITY_LOW,
                UNCLASSIFIED: PRIORITY_NONE}[classification]
    return classification, priority


def record_outcome(conn, pipeline_id: int, account_id: int, group_id: int, status: str,
                   group_requires_approval: bool = False, evidence: dict | None = None,
                   evidence_reference: str = "") -> dict:
    """Cập nhật phân loại sau một lần đăng có kết quả thật.

    Kết quả thật mới được ghi lại. Một lần kiểm tra lỗi hoặc một bài chưa
    rõ kết quả không làm đổi phân loại.
    """
    evidence = evidence or {}
    capabilities = capabilities_from_evidence(evidence)
    current = classification_of(conn, pipeline_id, account_id, group_id)
    if status not in ("verified_posted", "pending_group_approval"):
        # Chưa có kết quả đăng rõ ràng: giữ nguyên phân loại, chỉ cập nhật
        # khả năng đăng nếu có bằng chứng mới.
        _upsert(conn, pipeline_id, account_id, group_id, current["classification"],
                current["priority"], capabilities, current.get("last_verified_at"),
                current.get("last_post_status", ""), status)
        return classification_of(conn, pipeline_id, account_id, group_id)
    classification, priority = classify_outcome(status, group_requires_approval)
    changed = classification != current["classification"]
    _upsert(conn, pipeline_id, account_id, group_id, classification, priority, capabilities,
            now_iso(), status, status)
    if changed:
        conn.execute(f"""INSERT INTO {HISTORY_TABLE}
            (pipeline_id,account_id,group_id,from_classification,to_classification,reason,
             evidence_reference,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                     (int(pipeline_id), int(account_id), int(group_id),
                      current["classification"], classification,
                      "Bằng chứng đăng bài mới", evidence_reference, now_iso()))
    return classification_of(conn, pipeline_id, account_id, group_id)


def _upsert(conn, pipeline_id: int, account_id: int, group_id: int, classification: str,
            priority: str, capabilities: dict, last_verified_at, last_post_status: str,
            status: str) -> None:
    stamp = now_iso()
    conn.execute(f"""INSERT INTO {TABLE}
        (pipeline_id,account_id,group_id,classification,priority,supports_anonymous,
         supports_nickname,capability_state,last_verified_at,last_post_status,updated_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pipeline_id,account_id,group_id) DO UPDATE SET
            classification=excluded.classification,
            priority=excluded.priority,
            supports_anonymous=excluded.supports_anonymous,
            supports_nickname=excluded.supports_nickname,
            capability_state=excluded.capability_state,
            last_verified_at=COALESCE(excluded.last_verified_at, {TABLE}.last_verified_at),
            last_post_status=excluded.last_post_status,
            updated_at=excluded.updated_at""",
                 (int(pipeline_id), int(account_id), int(group_id), classification, priority,
                  int(bool(capabilities.get("supports_anonymous"))),
                  int(bool(capabilities.get("supports_nickname"))),
                  capabilities.get("capability_state", CAPABILITY_UNKNOWN),
                  last_verified_at, last_post_status, stamp, stamp))


def counts_of(conn, pipeline_id: int) -> dict:
    """Số group theo phân loại, để bảng điều khiển và giao diện dùng chung.

    Mọi cặp Facebook–Group của Workspace đều được tính: cặp chưa có bằng
    chứng thì thuộc nhóm *chưa phân loại*. Nhờ vậy số liệu khớp với những
    gì bảng điều khiển và bộ xếp lịch dùng.
    """
    counts = {name: 0 for name in CLASSIFICATIONS}
    saved = pairs_of(conn, pipeline_id)
    groups = conn.execute("""SELECT g.id group_id, g.account_id FROM groups g
                              WHERE g.pipeline_id=?""", (int(pipeline_id),)).fetchall()
    for group in groups:
        candidates = [int(group["account_id"] or 0)]
        for row in conn.execute("""SELECT account_id FROM group_accounts
                                   WHERE group_id=? AND status='active'""",
                                (group["group_id"],)).fetchall():
            if int(row["account_id"]) not in candidates:
                candidates.append(int(row["account_id"]))
        for account_id in candidates:
            entry = saved.get((account_id, int(group["group_id"])))
            name = entry["classification"] if entry else UNCLASSIFIED
            counts[name] = counts.get(name, 0) + 1
    counts["total"] = sum(counts[name] for name in CLASSIFICATIONS)
    return counts


def pending_approval_count(conn, pipeline_id: int) -> int:
    """Số bài đang chờ quản trị group duyệt trong Workspace.

    Số này được giới hạn, để hệ thống không gửi liên tục vào group mà mỗi
    bài đều phải chờ người duyệt.
    """
    row = conn.execute(f"""SELECT COUNT(*) n FROM auto_post_jobs j
                           JOIN {TABLE} c ON c.group_id=j.group_id AND c.account_id=j.account_id
                           WHERE j.pipeline_id=? AND j.status='pending'
                             AND c.classification=?""",
                        (int(pipeline_id), NEEDS_APPROVAL)).fetchone()
    return int(row["n"] or 0)


def history_of(conn, pipeline_id: int, account_id: int, group_id: int,
               limit: int = 20) -> list[dict]:
    """Lịch sử thay đổi phân loại, để biết vì sao nó đổi."""
    rows = conn.execute(f"""SELECT * FROM {HISTORY_TABLE}
                            WHERE pipeline_id=? AND account_id=? AND group_id=?
                            ORDER BY id DESC LIMIT ?""",
                        (int(pipeline_id), int(account_id), int(group_id), int(limit))).fetchall()
    return [dict(row) for row in rows]
