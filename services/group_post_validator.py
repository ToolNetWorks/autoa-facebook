"""Kiểm tra trước khi tạo chiến dịch đăng group.

Một chiến dịch có thể chọn rất nhiều group, nên việc kiểm tra phải chạy
**trước** khi ảnh được dàn và trước khi job nào được tạo. Nếu không, một
danh sách group không hợp lệ sẽ để lại ảnh tạm và job dang dở.

Kết quả kiểm tra tách hai loại:

* **Lỗi xác thực** — yêu cầu sai (quá giới hạn, group thuộc Workspace
  khác, thiếu ảnh...). Yêu cầu bị từ chối, không có gì được ghi.
* **Group chưa đủ điều kiện** — group đó không được xếp lịch, nhưng các group
  còn lại vẫn được xếp lịch, và mỗi group có một lý do riêng.
"""
from __future__ import annotations

from datetime import datetime

from services import group_eligibility, group_verification

#: Số group tối đa trong một chiến dịch. Đây là giới hạn **cấu hình**, không
#: phải hằng số viết cứng trong mã nguồn.
DEFAULT_MAX_GROUPS_PER_CAMPAIGN = 200

#: Số job tối đa tạo trong một lần gọi, để không một lần tạo quá nhiều job.
MAX_JOBS_PER_CAMPAIGN = 200


def max_groups_per_campaign() -> int:
    """Giới hạn số group cho một chiến dịch, lấy từ cấu hình."""
    import os
    try:
        value = int(os.getenv("MAX_GROUPS_PER_CAMPAIGN", DEFAULT_MAX_GROUPS_PER_CAMPAIGN))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_GROUPS_PER_CAMPAIGN
    return max(1, value)


class ValidationError(Exception):
    """Yêu cầu không hợp lệ. Không có gì được ghi vào database."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def full_message(self) -> str:
        """Thông báo đầy đủ, gồm cả gợi ý sửa."""
        return f"{self.message}. {self.detail}".strip() if self.detail else self.message


def unique_ids(group_ids: list[int]) -> list[int]:
    """Bỏ id lặp, giữ nguyên thứ tự người dùng chọn."""
    seen: set[int] = set()
    result: list[int] = []
    for value in group_ids or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number in seen:
            continue
        seen.add(number)
        result.append(number)
    return result


def validate_selection(conn, pipeline_id: int, group_ids: list[int], principal,
                       lookup_group, lookup_account, may_publish,
                       scheduled: datetime | None = None) -> dict:
    """Kiểm tra danh sách group của một chiến dịch.

    ``lookup_group`` và ``lookup_account`` là hai hàm tra cứu theo tài khoản
    đang đăng nhập, nên group của Workspace khác trả về ``None`` và bị từ chối
    ngay.
    """
    from scheduler import account_limits

    limit = max_groups_per_campaign()
    chosen = unique_ids(group_ids)
    if not chosen:
        raise ValidationError("Chọn ít nhất một group", "Chưa có group nào được chọn.")
    if len(chosen) > limit:
        raise ValidationError(
            f"Chiến dịch chỉ nhận tối đa {limit} group mỗi lần",
            f"Đang chọn {len(chosen)} group. Hãy chia thành nhiều chiến dịch.")
    if len(chosen) > MAX_JOBS_PER_CAMPAIGN:
        raise ValidationError(
            f"Chiến dịch chỉ nhận tối đa {MAX_JOBS_PER_CAMPAIGN} group mỗi lần",
            f"Đang chọn {len(chosen)} group.")

    settings = account_limits.settings_of(conn, pipeline_id)
    moment = scheduled or account_limits.now_in(settings.get("timezone"))
    day = account_limits.day_of(moment)
    planned: dict[int, int] = {}
    ready: list[dict] = []
    skipped: list[dict] = []
    deferred: list[dict] = []
    for group_id in chosen:
        group = lookup_group(group_id)
        if group is None or int(group["pipeline_id"]) != int(pipeline_id):
            raise ValidationError("Group không thuộc Workspace này", f"Group {group_id}.")
        config = group["config"]
        if not config.get("account_id"):
            skipped.append({"group_id": group_id, "label": group["label"],
                            "reason": "Group chưa gán tài khoản Facebook"})
            continue
        account = lookup_account(config["account_id"])
        if account is None or int(account["pipeline_id"]) != int(pipeline_id):
            raise ValidationError("Tài khoản Facebook không thuộc Workspace này",
                                  f"Group {group_id}.")
        if not account["enabled"]:
            skipped.append({"group_id": group_id, "label": group["label"],
                            "reason": "Tài khoản Facebook đang tạm dừng"})
            continue
        if group["status"] != "active" or not group["enabled"]:
            skipped.append({"group_id": group_id, "label": group["label"],
                            "reason": "Group đang tạm dừng"})
            continue
        if config.get("posting_mode") == "nickname" and not (config.get("nickname") or "").strip():
            skipped.append({"group_id": group_id, "label": group["label"],
                            "reason": "Chế độ biệt danh chưa có tên"})
            continue
        if not may_publish:
            skipped.append({"group_id": group_id, "label": group["label"],
                            "reason": "Tài khoản chưa có quyền đăng, cần quản trị duyệt"})
            continue
        # Hạn mức bài mỗi ngày của tài khoản. Bài vẫn được lên lịch, nhưng
        # phần vượt hạn mức sẽ chờ đến ngày sau: worker giữ nhịp đăng, nên
        # không group nào bị mất.
        account_id = int(config["account_id"])
        daily_limit = account_limits.effective_limit(conn, account_id, settings)
        used = account_limits.usage_of(conn, account_id, day, moment)["used"]
        if used + planned.get(account_id, 0) >= daily_limit:
            deferred.append({"group_id": group_id, "label": group["label"],
                             "reason": f"Tài khoản đã đủ {daily_limit} bài/ngày, bài sẽ chờ ngày sau"})
        decision = group_eligibility.pair_decision(conn, pipeline_id, account_id,
                                                   group_id, group)
        if not decision["eligible"]:
            skipped.append({"group_id": group_id, "label": group["label"],
                            "reason": decision["reason"] or "Group chưa đủ điều kiện đăng"})
            continue
        planned[account_id] = planned.get(account_id, 0) + 1
        ready.append({"group_id": group_id, "label": group["label"],
                      "account_id": account_id,
                      "posting_mode": config.get("posting_mode") or "anonymous",
                      "nickname": config.get("nickname") or "",
                      "decision": decision.get("decision", "")})
    # Không group nào đủ điều kiện không phải lỗi xác thực: các group đó
    # được báo riêng, còn các group khác vẫn được lên lịch.
    return {"selected": chosen, "ready": ready, "skipped": skipped,
            "deferred": deferred, "limit": limit}


def duplicate_groups(conn, group_ids: list[int], account_ids: list[int]) -> set[int]:
    """Các group đã có bài đang chờ, theo cặp group–tài khoản."""
    if not group_ids:
        return set()
    placeholders = ",".join("?" for _ in group_ids)
    rows = conn.execute(f"""SELECT group_id FROM jobs
                            WHERE group_id IN ({placeholders})
                              AND state IN ('queued','running')""",
                         list(group_ids)).fetchall()
    return {int(row["group_id"]) for row in rows}
