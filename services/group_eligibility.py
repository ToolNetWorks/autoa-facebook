"""Một cặp Facebook–Group có được đưa vào lịch đăng tự động không.

Quyết định luôn gắn với đúng một cặp ``account_id`` + ``group_id``. Trạng
thái của một Facebook không bao giờ được dùng cho Facebook khác, và trạng
thái của một group không bao giờ được dùng cho group khác.

Hai loại điều kiện được kiểm tra, theo thứ tự:

A. FIRST POST ELIGIBILITY

Dành cho Group chưa từng có bằng chứng đăng bài.

Trạng thái NOT_TESTED không phải lý do chặn bài đầu tiên.

Nếu tài khoản và Group đáp ứng các điều kiện có thể kiểm chứng:

* Cùng Pipeline.
* Facebook được CTV chọn.
* Group được CTV chọn.
* Phiên Facebook hợp lệ.
* Có quyền truy cập phù hợp.
* Đã được xác nhận là thành viên nếu nhóm yêu cầu.
* Không có hạn chế đã được xác nhận.
* Chế độ đăng yêu cầu khả dụng.
* Ảnh/caption đáp ứng Safety Engine.
* Tác vụ sử dụng phương thức xuất bản được phép.

Thì cho phép đưa bài đầu tiên vào luồng xuất bản.

Không yêu cầu VERIFIED_POSTED trước đó.

B. RECURRING POST ELIGIBILITY

Dành cho những cặp đã có kết quả đăng bài thực tế.

Đánh giá quyền đăng dựa trên kết quả đã quan sát được.

Giữ kiểm tra tình trạng tài khoản, nhóm, danh tính đăng, hạn mức, nội dung và quyền truy cập trước các lần tiếp theo.

Không coi một lần thành công là bảo đảm vĩnh viễn.

Những điều kiện được kiểm tra, theo thứ tự:

1. Group thuộc Workspace, đang bật, hoạt động và được duyệt làm đích.
2. Facebook thuộc Workspace và phiên đăng nhập còn hiệu lực.
3. Cặp này **đã có bằng chứng đăng bài thật**: đã đăng thành công, hoặc đã
   gửi và đang chờ group duyệt. Không có bằng chứng thì không xếp lịch.
4. Chế độ đăng của group được hỗ trợ bởi tài khoản này.
5. Chế độ biệt danh đã có tên.

Không có điều kiện nào được bỏ qua, và không có điều kiện nào được suy đoán.
"""
from __future__ import annotations

from services import (group_classification, group_membership, group_screening,
                     group_verification)

#: Trạng thái nghiệp vụ của một cặp Facebook–Group. Đây là cách nói chung
#: cho giao diện, không thay thế trạng thái bằng chứng đã lưu.
FIRST_POST_READY = "first_post_ready"
VERIFIED_READY = "verified_ready"
WAITING_MEMBERSHIP = "waiting_membership"
WAITING_APPROVAL = "waiting_approval"
NEEDS_ACTION = "needs_action"
CONFIRMED_RESTRICTED = "confirmed_restricted"
SAFETY_BLOCKED = "safety_blocked"

#: Những trạng thái mà một group có thể được phục vụ tự động.
ELIGIBLE_STATUSES = (group_verification.VERIFIED_POSTED, group_verification.PENDING_GROUP_APPROVAL)

#: Những trạng thái phiên Facebook không dùng được để đăng.
#: Một phiên chưa được kiểm tra (unknown) chưa phải là phiên chết: lần kiểm tra
#: group sẽ cho biết tài khoản còn dùng được hay không.
DEAD_SESSIONS = ("expired", "checkpoint", "restricted", "error", "invalid", "revoked")

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
                  group=None, for_scheduling: bool = False) -> dict:
    """Quyết định cho một cặp Facebook–Group, kèm lý do và hướng xử lý.

    Hàm luôn trả về một quyết định. Khi không đủ điều kiện thì ``eligible``
    là ``False`` và ``reason`` nói rõ vì sao.
    """
    group_id = int(group_id)
    account_id = int(account_id)
    if group is None:
        group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if group is None or group["pipeline_id"] != int(pipeline_id):
        return _no(group_verification.NOT_TESTED, "Group không thuộc Workspace này.", "",
                    MODE_ANONYMOUS)

    account = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if account is None or account["pipeline_id"] != int(pipeline_id):
        return _no(group_verification.NOT_TESTED, "Facebook không thuộc Workspace này.", "",
                    MODE_ANONYMOUS)

    mode, nickname = mode_of(conn, account_id, group_id, group)
    from services.auto_posting_cycles.repository import global_state
    if global_state(conn,pipeline_id,group_id)=='DISABLED':
        return _no(group_verification.NOT_TESTED,'Group Bị Vô Hiệu Hoá',
                   'Chờ xác minh lại trạng thái Group.',mode)
    if for_scheduling:
        return _schedule_decision(group, mode)

    # 1. Group phải dùng được.
    if not group["enabled"]:
        return _no(group_verification.NOT_TESTED, "Group đang tạm dừng.",
                    "Bật lại group trong cấu hình.", mode)
    if group["status"] != "active":
        return _no(group_verification.NOT_TESTED, "Group không hoạt động.",
                    "Kiểm tra lại group.", mode)
    if not group["approved_target"]:
        return _no(group_verification.NOT_TESTED, "Group chưa được duyệt làm group đích.",
                    "Đánh dấu group là đích được phép đăng.", mode)

    # 2. Phiên Facebook phải còn hiệu lực.
    session = (account["session_status"] or "").strip().lower()
    if session in DEAD_SESSIONS:
        return _no(group_verification.CHECK_FAILED, "Phiên đăng nhập Facebook chưa hợp lệ.",
                    "Đăng nhập lại Facebook rồi xác minh lại.", mode)

    # 3. Chế độ đăng phải khả dụng. Điều này áp dụng cho cả bài đầu tiên lẫn
    #    các bài sau, vì phương thức xuất bản phải được Facebook hỗ trợ.
    evidence = _observation(conn, account_id, group_id)
    if mode == MODE_NICKNAME:
        if not nickname.strip():
            return _no(group_verification.NOT_TESTED, "Chế độ biệt danh chưa có tên.",
                        "Chọn tên biệt danh cho group này.", mode)
        if evidence.get("nickname_available") is False:
            return _no(group_verification.NOT_TESTED,
                        "Facebook không có biệt danh đã cấu hình trong danh sách danh tính.",
                        "Xác minh lại chế độ đăng hoặc chọn tên biệt danh khác.", mode)
    elif mode == MODE_ANONYMOUS:
        if evidence.get("supports_anonymous") is False:
            return _no(group_verification.NOT_TESTED, "Group không hỗ trợ đăng ẩn danh.",
                        "Xác minh chế độ ẩn danh, hoặc chọn chế độ biệt danh.", mode)
    else:
        return _no(group_verification.NOT_TESTED, f"Chế độ đăng '{mode}' không hợp lệ.",
                    "Chọn lại chế độ đăng của group.", mode)

    # 4. Tài khoản chưa tham gia không mặc định chặn đăng: một số group công
    #    khai hỗ trợ Visitor Posting (viết bài trên tường group mà không phải
    #    thành viên). Chỉ chặn khi bằng chứng rõ ràng là không thể đăng.
    membership = group_membership.status_of(conn, pipeline_id, account_id, group_id)
    if membership == group_membership.NOT_MEMBER:
        evidence = _observation(conn, account_id, group_id)
        if evidence.get("can_post") is True:
            # Visitor Posting được Facebook hỗ trợ: cho phép bài đầu tiên làm
            # lần xác minh thực tế.
            pass
        else:
            return _no(group_verification.NOT_TESTED,
                        "Tài khoản chưa tham gia group này và chưa có bằng chứng "
                        "Facebook cho phép viết bài mà không tham gia.",
                        "Xác minh quyền đăng bằng bài đăng thật, hoặc tham gia group.",
                        mode, decision=WAITING_MEMBERSHIP)
    if membership == group_membership.PENDING_APPROVAL:
        return _no(group_verification.NOT_TESTED,
                    "Yêu cầu tham gia group đang chờ group duyệt.",
                    "Chờ group duyệt rồi thử lại.", mode,
                    decision=WAITING_MEMBERSHIP)
    if membership == group_membership.JOIN_REJECTED:
        # Yêu cầu tham gia bị từ chối KHÔNG có nghĩa Facebook hạn chế quyền
        # đăng. Tài khoản có thể vẫn đăng được nếu đã vào group từ đường khác,
        # nên trạng thái thành viên giữ riêng và không suy ra hạn chế đăng.
        return _no(group_verification.NOT_TESTED,
                    "Group đã từ chối yêu cầu tham gia của tài khoản này.",
                    "Dùng tài khoản khác cho group này, hoặc tham gia bằng tài khoản đó.",
                    mode, decision=NEEDS_ACTION)

    # 5. Xác định loại kiểm tra: FIRST POST hay RECURRING POST.
    status = group_verification.servable_status_of(conn, pipeline_id, account_id, group_id, mode)

    # FIRST POST FLOW: Group chưa từng có bằng chứng đăng bài. Bài đầu tiên
    # chính là lần xác minh thực tế, nên NOT_TESTED và SUBMITTED_UNVERIFIED
    # không phải lý do chặn khi đang lưu lịch (for_scheduling=True).
    is_first_post_status = (status == group_verification.NOT_TESTED or
                            (for_scheduling and status == group_verification.SUBMITTED_UNVERIFIED))
    if is_first_post_status:
        return {"eligible": True, "status": status,
                "reason": "Group chưa từng có bài đăng thật. Bài đầu tiên là lần xác minh.",
                "action": "Hệ thống sẽ chuẩn bị bài đầu tiên khi bạn bấm Xếp lịch ngay.",
                "mode": mode, "first_post_eligible": True, "recurring_eligible": False,
                "decision": FIRST_POST_READY}

    # RECURRING POST FLOW: cặp này phải có bằng chứng đăng bài thật.
    if not group_verification.is_servable(status):
        return _no(status, group_screening.guidance_of(evidence, status),
                    _action_of(status, evidence), mode,
                    decision=_decision_of(status))

    # Trạng thái nghiệp vụ lấy từ phân loại đã học được từ bằng chứng.
    classification = group_classification.classification_of(conn, pipeline_id, account_id, group_id)
    decision = _decision_from_classification(classification, status)
    return {"eligible": True, "status": status,
            "reason": group_screening.guidance_of(evidence, status),
            "action": _action_of(status, evidence), "mode": mode,
            "first_post_eligible": False, "recurring_eligible": True,
            "decision": decision}

def _schedule_decision(group, mode: str) -> dict:
    """Quyết định khi LƯU LỊCH (SCHEDULE_VALIDATION).

    Chỉ dùng dữ liệu cấu hình của Workspace: group đang bật, đang hoạt động và
    được đánh dấu là group đích. Không dùng phiên Facebook, thành viên group,
    quyền đăng, biệt danh có sẵn hay kết quả kiểm tra (NOT_TESTED,
    CHECK_FAILED...): những điều đó chỉ được kiểm tra lúc worker chuẩn bị đăng.
    Tenant và cặp Facebook–Group đã được kiểm tra trước khi gọi hàm này.
    """
    group = dict(group)
    if not group.get("enabled"):
        return _no(group_verification.NOT_TESTED, "Group đang tạm dừng.",
                   "Bật lại group trong cấu hình.", mode)
    if group["status"] != "active":
        return _no(group_verification.NOT_TESTED, "Group không hoạt động.",
                   "Kiểm tra lại group.", mode)
    if not group["approved_target"]:
        return _no(group_verification.NOT_TESTED, "Group chưa được chọn làm group đích.",
                   "Đánh dấu group là đích được phép đăng.", mode)
    screening = (group.get("screening_status") or "").lower()
    if screening in (group_verification.RESTRICTED_CONFIRMED, "restricted", "rejected"):
        return _no(group_verification.RESTRICTED_CONFIRMED, "Facebook hạn chế quyền đăng trong group này.",
                   "Dùng tài khoản khác hoặc kiểm tra lại quyền của tài khoản trong group.", mode)
    return {"eligible": True, "status": group_verification.NOT_TESTED,
            "reason": "Có thể xếp lịch. Điều kiện đăng được kiểm tra lúc đến giờ đăng.",
            "action": "", "mode": mode, "first_post_eligible": True,
            "recurring_eligible": False, "decision": FIRST_POST_READY}


def _no(status: str, reason: str, action: str, mode: str, decision: str = NEEDS_ACTION) -> dict:
    return {"eligible": False, "status": status, "reason": reason, "action": action,
            "mode": mode, "recurring_eligible": False, "first_post_eligible": False,
            "decision": decision}


def _decision_from_classification(classification: dict, status: str) -> str:
    """Trạng thái nghiệp vụ từ phân loại, nếu chưa có thì từ bằng chứng."""
    if classification.get("configured"):
        name = classification.get("classification")
        if name == group_classification.NO_APPROVAL:
            return VERIFIED_READY
        if name == group_classification.NEEDS_APPROVAL:
            return WAITING_APPROVAL
        return FIRST_POST_READY
    if status == group_verification.PENDING_GROUP_APPROVAL:
        return WAITING_APPROVAL
    return VERIFIED_READY


def _decision_of(status: str) -> str:
    """Trạng thái nghiệp vụ của một cặp đã có bằng chứng."""
    if status == group_verification.PENDING_GROUP_APPROVAL:
        return WAITING_APPROVAL
    if status == group_verification.RESTRICTED_CONFIRMED:
        return CONFIRMED_RESTRICTED
    if status == group_verification.REJECTED:
        return NEEDS_ACTION
    if status == group_verification.SUBMITTED_UNVERIFIED:
        return WAITING_APPROVAL
    if status == group_verification.CHECK_FAILED:
        return NEEDS_ACTION
    if status == group_verification.TEST_IN_PROGRESS:
        return WAITING_APPROVAL
    return NEEDS_ACTION


def _action_of(status: str, evidence: dict) -> str:
    if status == group_verification.VERIFIED_POSTED:
        return "Đã xác minh, có thể xếp lịch tự động."
    if status == group_verification.PENDING_GROUP_APPROVAL:
        return "Bài sẽ cần Admin group duyệt trước khi hiện công khai."
    if status == group_verification.NOT_TESTED:
        return "Bài đầu tiên sẽ là lần xác minh thực tế của group này."
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
                   account_id: int | None = None, first_post: bool = True,
                   for_scheduling: bool = False) -> list[dict]:
    """Các cặp Facebook–Group đủ điều kiện đăng.

    Kết quả là một danh sách group, mỗi group ghi kèm các tài khoản được phép
    đăng vào nó.

    Với ``first_post`` (mặc định) thì group chưa từng có bằng chứng đăng bài
    cũng được phép, vì bài đầu tiên chính là lần xác minh. Bộ xếp lịch đăng
    lặp lại thì truyền ``first_post=False`` để chỉ phục vụ group đã có bằng
    chứng.

    Khi ``for_scheduling=True``, không lọc theo trạng thái screening - cho phép
    tất cả group được CTV chọn làm đích.
    """
    result = []
    for group in groups:
        candidates = _candidate_accounts(conn, pipeline_id, group)
        if account_id is not None:
            candidates = [item for item in candidates if item == int(account_id)]
        allowed = []
        for candidate in candidates:
            decision = pair_decision(conn, pipeline_id, candidate, group["id"], group,
                                     for_scheduling=for_scheduling)
            if not decision["eligible"]:
                continue
            if not first_post and not decision.get("recurring_eligible", True):
                continue
            allowed.append(candidate)
        if allowed:
            item = dict(group)
            item["eligible_account_ids"] = allowed
            result.append(item)
    # Thứ tự phục vụ: đã xác minh đăng trực tiếp, rồi chưa phân loại (bài
    # đầu tiên), rồi group cần quản trị duyệt.
    classifications = group_classification.pairs_of(conn, pipeline_id)
    result.sort(key=lambda item: _priority_rank(classifications, item))
    return result


def _priority_rank(classifications: dict, group: dict) -> tuple:
    key = (int(group.get("account_id") or 0), int(group.get("id") or 0))
    entry = classifications.get(key)
    rank = entry["priority_rank"] if entry else group_classification.PRIORITY_ORDER[
        group_classification.UNCLASSIFIED]
    return (rank, int(group.get("id") or 0))


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
