"""Đồng bộ quyền đặt group làm mục tiêu đăng.

Trong mô hình 1 Pipeline = 1 CTV = 1 Workspace, CTV là người quản lý các
group của Workspace mình, nên việc CTV **chọn** một group làm mục tiêu đăng
cũng là việc xác nhận group đó được phép đăng. Vì vậy cờ
``approved_target`` được đồng bộ theo lựa chọn đó.

Không bỏ qua quy định nào của group: group vẫn phải bật, hoạt động, và mọi
bài đăng vẫn phải qua Safety Engine. Việc đồng bộ chỉ nói rõ group nào được
phép làm đích, không nói rõ group nào đăng được.
"""
from __future__ import annotations


def sync_approved_targets(conn, pipeline_id: int, group_ids) -> list[int]:
    """Đánh dấu các group được chọn là được phép đặt làm mục tiêu đăng.

    Trả về danh sách id group đã được đồng bộ. Group không thuộc Workspace
    hoặc không tồn tại thì bị bỏ qua, không báo lỗi: danh sách lựa chọn có
    thể đã cũ.
    """
    chosen: list[int] = []
    for group_id in group_ids or []:
        try:
            value = int(group_id)
        except (TypeError, ValueError):
            continue
        if value not in chosen:
            chosen.append(value)
    if not chosen:
        return []
    placeholders = ",".join("?" for _ in chosen)
    conn.execute(f"""UPDATE groups SET approved_target=1
                     WHERE pipeline_id=? AND id IN ({placeholders})""",
                 (int(pipeline_id), *chosen))
    return chosen
