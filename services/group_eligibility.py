"""Một cặp Facebook–Group có được đưa vào lịch đăng tự động không.

Quyết định luôn gắn với đúng một cặp ``account_id`` + ``group_id``. Trạng
thái của một Facebook không bao giờ được dùng cho Facebook khác, và trạng
thái của một group không bao giờ được dùng cho group khác.

Những điều kiện được kiểm tra, theo thứ tự:

1. Group thuộc Workspace, đang bật, hoạt động và được duyệt làm đích.
2. Facebook thuộc Workspace và phiên đăng nhập còn hiệu lực.
3. Cặp này **đã có bằng chứng đăng bài thật**: đã đăng thành công, hoặc đã
   gửi và đang chờ group duyệt. Không có bằng chứng thì không xếp lịch.
4. Chế độ đăng của group được hỗ trợ bởi tài khoản này.
5. Chế độ biệt danh đã có tên.

Quan sát của trình duyệt chỉ dùng để hướng dẫn CTV, không dùng để kết luận.
"""
from __future__ import annotations

from services import group_screening, group_verification

#: Những trạng thái phiên Facebook không dùng được để đăng.
#: Một phiên chưa được kiểm tra (unknown) chưa phải là phiên chết: lần kiểm tra
#: group sẽ cho biết tài khoản còn dùng được hay không.
DEAD_SESSIONS = ("expired", "error", "invalid", "revoked")

MODE_ANONYMOUS = "anonymous"
MODE_NICKNAME = "nickname"


def screening_of(conn, account_id: int, group_id: int):
    """Kết quả kiểm tra của đúng cặp Facebook–Group này."""
    return conn.execute("""SELECT * FROM group_screening_results
                           WHERE account_id=? AND group_id=?""",
                        (int(account_id), int(group_id))).fetchone()


def mode_of(conn, account_id: int, group_id: int, group=None) -> tuple[str, str]:
    """Chế độ đăng và tên biệt danh của cặp này."""
    row = conn.execute("""SELECT posting_mode, nickname FROM group_accounts
                           WHERE account_id=? AND group_id=? AND status='active'""",
                        (int(account_id), int(group_id))).fetchone()
    if row is not None:
        return (row["posting_mode"] or MODE_ANONYMOUS), (row["nickname"] or "")
    if group is not None:
        return (group["posting_mode"] or MODE_ANONYMOUS), (group["nickname"] or "")
    return MODE_ANONYMOUS, ""


def pair_decision(conn, pipeline_id: int, account_id: int, group_id: int,
                  group=None) -> dict:
    """Quyết định cho một cặp Facebook–Group, kèm lý do và hướng xử lý.

    Hàm luôn trả về một quyết định. Khi không đủ điều kiện thì ``eligible``
    là ``False`` và ``reason`` nói rõ vì sao.
    """
    group_id = int(group_id)
    account_id = int(account_id)
    if group is None:
        group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if group is None or group["pipeline_id"] != int(pipeline_id):
        return _no(group_screening.STATUS_UNKNOWN, "Group không thuộc Workspace này.", "",
                    MODE_ANONYMOUS)

    account = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if account is None or account["pipeline_id"] != int(pipeline_id):
        return _no(group_screening.STATUS_UNKNOWN, "Facebook không thuộc Workspace này.", "",
                    MODE_ANONYMOUS)

    mode, nickname = mode_of(conn, account_id, group_id, group)

    # 1. Group phải dùng được.
    if not group["enabled"]:
        return _no(group_screening.STATUS_UNKNOWN, "Group đang tạm dừng.",
                    "Bật lại group trong cấu hình.", mode)
    if group["status"] != "active":
        return _no(group_screening.STATUS_UNKNOWN, "Group không hoạt động.",
                    "Kiểm tra lại group.", mode)
    if not group["approved_target"]:
        return _no(group_screening.STATUS_UNKNOWN, "Group chưa được duyệt làm group đích.",
                    "Đánh dấu group là đích được phép đăng.", mode)

    # 2. Phiên Facebook phải còn hiệu lực.
    session = (account["session_status"] or "").strip().lower()
    if session in DEAD_SESSIONS:
        return _no(group_verification.CHECK_FAILED, "Phiên đăng nhập Facebook chưa hợp lệ.",
                    "Đăng nhập lại Facebook rồi xác minh lại.", mode)

    # 3. Cặp này phải có bằng chứng đăng bài thật.
    status = group_verification.servable_status_of(conn, pipeline_id, account_id, group_id)
    evidence = _observation(conn, account_id, group_id)
    if not group_verification.is_servable(status):
        return _no(status, group_screening.guidance_of(evidence, status),
                    _action_of(status, evidence), mode)

    # 4. Chế độ đăng phải được hỗ trợ.
    if mode == MODE_NICKNAME:
        if not nickname.strip():
            return _no(status, "Chế độ biệt danh chưa có tên.",
                        "Chọn tên biệt danh cho group này.", mode)
        if evidence.get("nickname_available") is False:
            return _no(status, "Facebook không có biệt danh đã cấu hình trong danh sách danh tính.",
                        "Xác minh lại chế độ đăng hoặc chọn tên biệt danh khác.", mode)
    elif mode == MODE_ANONYMOUS:
        if evidence.get("supports_anonymous") is False:
            return _no(status, "Group không hỗ trợ đăng ẩn danh.",
                        "Xác minh chế độ ẩn danh, hoặc chọn chế độ biệt danh.", mode)
    else:
        return _no(status, f"Chế độ đăng '{mode}' không hợp lệ.",
                    "Chọn lại chế độ đăng của group.", mode)

    return {"eligible": True, "status": status,
            "reason": group_screening.guidance_of(evidence, status),
            "action": _action_of(status, evidence), "mode": mode}


def _no(status: str, reason: str, action: str, mode: str) -> dict:
    return {"eligible": False, "status": status, "reason": reason, "action": action,
            "mode": mode}


def _action_of(status: str, evidence: dict) -> str:
    if status == group_verification.VERIFIED_POSTED:
        return "Đã xác minh, có thể xếp lịch tự động."
    if status == group_verification.PENDING_GROUP_APPROVAL:
        return "Bài sẽ cần Admin group duyệt trước khi hiện công khai."
    if status == group_verification.NOT_TESTED:
        return "Bấm “Xác minh bằng bài đăng” để kiểm chứng quyền đăng."
    if status == group_verification.SUBMITTED_UNVERIFIED:
        return "Đang chờ xác nhận của Facebook; kiểm tra lại sau."
    if status == group_verification.TEST_IN_PROGRESS:
        return "Đang thực hiện xác minh."
    if status == group_verification.CHECK_FAILED:
        return "Kiểm tra lại, hoặc mở group để kiểm tra thủ công."
    if status == group_verification.REJECTED:
        return "Mở group để xem lại nội dung và quyền của tài khoản."
    if status == group_verification.RESTRICTED_CONFIRMED:
        return "Tài khoản này không được đăng trong group. Dùng tài khoản khác."
    return group_screening.next_action(evidence)


def _observation(conn, account_id: int, group_id: int) -> dict:
    """Quan sát của trình duyệt về cặp này, dùng để hướng dẫn CTV."""
    row = conn.execute("""SELECT evidence_json FROM group_screening_results
                          WHERE account_id=? AND group_id=?""",
                       (int(account_id), int(group_id))).fetchone()
    if row is None:
        return {}
    return _evidence(row)


def _evidence(row) -> dict:
    import json
    try:
        evidence = json.loads(row["evidence_json"] or "{}")
    except (TypeError, ValueError):
        evidence = {}
    return evidence if isinstance(evidence, dict) else {}


def eligible_pairs(conn, pipeline_id: int, groups: list[dict],
                   account_id: int | None = None) -> list[dict]:
    """Các cặp Facebook–Group đủ điều kiện đăng tự động.

    Kết quả là một danh sách group, mỗi group ghi kèm các tài khoản được phép
    đăng vào nó.
    """
    result = []
    for group in groups:
        candidates = _candidate_accounts(conn, pipeline_id, group)
        if account_id is not None:
            candidates = [item for item in candidates if item == int(account_id)]
        allowed = []
        for candidate in candidates:
            decision = pair_decision(conn, pipeline_id, candidate, group["id"], group)
            if decision["eligible"]:
                allowed.append(candidate)
        if allowed:
            item = dict(group)
            item["eligible_account_ids"] = allowed
            result.append(item)
    result.sort(key=lambda item: (0 if item.get("screening_status") == group_screening.STATUS_PRIORITY
                                  else 1, item.get("id", 0)))
    return result


def _candidate_accounts(conn, pipeline_id: int, group: dict) -> list[int]:
    """Các Facebook có thể đăng vào group này."""
    found: list[int] = []
    if group.get("account_id"):
        found.append(int(group["account_id"]))
    for row in conn.execute("""SELECT account_id FROM group_accounts
                               WHERE group_id=? AND status='active'""",
                            (group["id"],)).fetchall():
        if int(row["account_id"]) not in found:
            found.append(int(row["account_id"]))
    return found


def accounts_of(conn, pipeline_id: int, group: dict) -> list[int]:
    """Public alias so the schedulers can ask the same question."""
    return _candidate_accounts(conn, pipeline_id, group)


def groups_for_account(groups: list[dict], account_id: int) -> list[dict]:
    """Các group mà tài khoản này được phép đăng, theo quyết định của từng cặp."""
    return [group for group in groups
            if int(account_id) in (group.get("eligible_account_ids") or [])]
