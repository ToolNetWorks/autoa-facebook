"""Khoảng nghỉ giữa hai lần đăng vào cùng một group.

Một group không nên nhận hai bài liên tiếp. Khoảng nghỉ tính theo lần đăng
gần nhất của group đó, kể cả bài do tài khoản khác đăng.
"""
from __future__ import annotations

from datetime import datetime, timedelta

DEFAULT_GROUP_COOLDOWN = 120


def last_group_post_at(conn, group_id: int) -> str | None:
    """Thời điểm đăng gần nhất vào group này."""
    row = conn.execute("""SELECT MAX(COALESCE(published_at, updated_at)) moment FROM jobs
                           WHERE group_id=?""", (int(group_id),)).fetchone()
    return row["moment"] if row and row["moment"] else None


def group_usage_today(conn, group_id: int, day: str) -> int:
    """Số bài đã đăng vào group hôm nay."""
    row = conn.execute("""SELECT COUNT(*) FROM jobs WHERE group_id=?
                           AND date(COALESCE(published_at, updated_at))=date(?)""",
                        (int(group_id), day)).fetchone()
    return int(row[0] or 0)


def cooldown_active(conn, group_id: int, moment: datetime, cooldown_minutes: int) -> bool:
    """True when group này còn đang nghỉ."""
    if not cooldown_minutes:
        return False
    last = last_group_post_at(conn, group_id)
    if not last:
        return False
    try:
        last_moment = datetime.fromisoformat(last)
    except ValueError:
        return False
    if last_moment.tzinfo is None:
        last_moment = last_moment.replace(tzinfo=moment.tzinfo)
    return moment < last_moment + timedelta(minutes=int(cooldown_minutes))


def next_available_at(conn, group_id: int, moment: datetime,
                      cooldown_minutes: int) -> datetime | None:
    """Thời điểm group sẵn sàng nhận bài tiếp theo."""
    last = last_group_post_at(conn, group_id)
    if not last:
        return moment
    try:
        last_moment = datetime.fromisoformat(last)
    except ValueError:
        return moment
    if last_moment.tzinfo is None:
        last_moment = last_moment.replace(tzinfo=moment.tzinfo)
    ready = last_moment + timedelta(minutes=int(cooldown_minutes or 0))
    return max(moment, ready)


def describe(conn, group_id: int, moment: datetime, cooldown_minutes: int) -> dict:
    """Trạng thái nghỉ của một group, dùng cho giao diện."""
    last = last_group_post_at(conn, group_id)
    ready = next_available_at(conn, group_id, moment, cooldown_minutes)
    return {
        "group_id": int(group_id),
        "last_post_at": last,
        "cooldown_minutes": int(cooldown_minutes or 0),
        "available_at": ready.isoformat() if ready else None,
        "cooling_down": cooldown_active(conn, group_id, moment, cooldown_minutes),
    }
