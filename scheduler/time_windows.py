"""Khung giờ được phép đăng và cách rải bài trong ngày.

Một Pipeline có thể dùng một khung giờ liên tục (ví dụ 08:00 đến 22:00) hoặc
nhiều khung giờ rời nhau. Khung giờ được phép vượt qua nửa đêm, ví dụ 22:00 đến
02:00.

Mọi phép tính đều theo múi giờ của Workspace, không theo giờ máy chủ.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

try:  # pragma: no cover
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_START = "08:00"
DEFAULT_END = "22:00"
DEFAULT_MIN_INTERVAL = 90
DEFAULT_MAX_POSTS_PER_DAY = 3
DEFAULT_DAYS = [1, 2, 3, 4, 5, 6, 0]
DEFAULT_GROUP_COOLDOWN = 120

# Giao diện dùng 1 = thứ hai ... 6 = thứ bảy, 0 = chủ nhật; phần tính
# dùng isoweekday() nên chủ nhật là 7.
WEEKDAY_ALIASES = {0: 7, 7: 7}


def zone(timezone_name: str):
    """Múi giờ của Workspace."""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(timezone_name or DEFAULT_TIMEZONE)
        except Exception:
            pass
    return timezone.utc


def now_in(timezone_name: str) -> datetime:
    """Giờ hiện tại theo múi giờ của Workspace."""
    return datetime.now(zone(timezone_name))


def parse_time(value: str, fallback: time) -> time:
    """Đọc "HH:MM" thành giờ."""
    try:
        parts = str(value or "").strip().split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return fallback


def parse_windows(windows_json: str | None) -> list[tuple[time, time]]:
    """Các khung giờ bổ sung, dạng "08:00-12:00"."""
    try:
        raw = json.loads(windows_json or "[]")
    except (TypeError, ValueError):
        raw = []
    result = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            start = parse_time(item.get("start", ""), time(8, 0))
            end = parse_time(item.get("end", ""), time(12, 0))
        else:
            text = str(item)
            if "-" not in text:
                continue
            left, right = text.split("-", 1)
            start = parse_time(left, time(8, 0))
            end = parse_time(right, time(12, 0))
        result.append((start, end))
    return result


def parse_days(days_json: str | None) -> list[int]:
    """Các ngày trong tuần được phép đăng (thứ hai = 1, chủ nhật = 0 hoặc 7)."""
    try:
        raw = json.loads(days_json or "[]")
    except (TypeError, ValueError):
        raw = []
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_DAYS)
    days = []
    for value in raw:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        days.append(WEEKDAY_ALIASES.get(number, number))
    return sorted(set(days))


def day_enabled(moment: datetime, days: list[int]) -> bool:
    """True when this day of the week may post."""
    # isoweekday(): thứ hai = 1 ... chủ nhật = 7
    return moment.isoweekday() in days


def window_bounds(start: time, end: time, moment: datetime) -> tuple[datetime, datetime]:
    """Khung giờ của ngày chứa ``moment``.

    Khung giờ vượt qua nửa đêm bắt đầu ở ngày hôm trước và kết thúc ở nửa
    đêm hôm nay.
    """
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    begin = datetime.combine(day_start.date(), start, tzinfo=moment.tzinfo)
    finish = datetime.combine(day_start.date(), end, tzinfo=moment.tzinfo)
    if end <= start:
        # Khung giờ qua nửa đêm: 22:00 hôm nay đến 02:00 hôm mai.
        if moment.timetz().replace(tzinfo=None) >= start:
            finish = finish + timedelta(days=1)
        else:
            begin = begin - timedelta(days=1)
    return begin, finish


def windows_of_day(moment: datetime, settings: dict) -> list[tuple[datetime, datetime]]:
    """Tất cả khung giờ được phép của ngày đó."""
    timezone_name = settings.get("timezone") or DEFAULT_TIMEZONE
    start = parse_time(settings.get("start_time", DEFAULT_START), time(8, 0))
    end = parse_time(settings.get("end_time", DEFAULT_END), time(22, 0))
    result = [window_bounds(start, end, moment)]
    for extra_start, extra_end in parse_windows(settings.get("windows_json")):
        result.append(window_bounds(extra_start, extra_end, moment))
    result.sort(key=lambda item: item[0])
    return result


def is_inside_window(moment: datetime, settings: dict) -> bool:
    """True when the moment is inside a khung giờ được phép."""
    for begin, finish in windows_of_day(moment, settings):
        if begin <= moment < finish:
            return True
    return False


def next_allowed_moment(moment: datetime, settings: dict) -> datetime:
    """Thời điểm sớm nhất được phép đăng, kể từ ``moment``."""
    days = parse_days(settings.get("days_enabled_json"))
    cursor = moment
    for _ in range(400):
        for begin, finish in windows_of_day(cursor, settings):
            if cursor < begin:
                return begin
            if begin <= cursor < finish:
                return cursor
        # sang ngày tiếp theo
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if not day_enabled(cursor, days):
            continue
    return moment


def spread_slots(windows: list[tuple[datetime, datetime]], quotas: dict[int, int],
                 min_interval: int, day_start: datetime) -> list[tuple[int, datetime]]:
    """Rải các bài của mọi tài khoản đều trong khung giờ.

    Các tài khoản luân phiên nên không có hai bài của hai tài khoản khác nhau
    trùng phút, và mỗi tài khoản có khoảng cách tối thiểu giữa hai bài của
    nó. Lịch là tất định: cùng dữ liệu cho cùng kết quả.
    """
    total = sum(quotas.values())
    if total <= 0:
        return []
    remaining = {account_id: int(count) for account_id, count in quotas.items() if count > 0}
    if not remaining:
        return []
    total_minutes = 0
    for begin, finish in windows:
        total_minutes += max(0, int((finish - begin).total_seconds() // 60))
    gap = max(int(min_interval or DEFAULT_MIN_INTERVAL), 1)
    if total_minutes > 0:
        gap = max(gap, total_minutes // max(1, total + 1))
    slots: list[tuple[int, datetime]] = []
    account_ids = sorted(remaining)
    cursor = day_start
    for begin, finish in windows:
        cursor = max(cursor, begin)
        while any(remaining.values()):
            for account_id in account_ids:
                if remaining.get(account_id, 0) <= 0:
                    continue
                slots.append((account_id, cursor))
                remaining[account_id] -= 1
                if not any(remaining.values()):
                    break
                cursor = cursor + timedelta(minutes=gap)
                if cursor >= finish:
                    break
            if cursor >= finish:
                break
    return slots


def day_of(moment: datetime) -> str:
    """Ngày theo múi giờ của Workspace, dạng YYYY-MM-DD."""
    return moment.strftime("%Y-%m-%d")


def day_bounds_utc(moment: datetime, day: str | None = None) -> tuple[str, str]:
    """Đầu ngày và đầu ngày tiếp theo, theo múi giờ Workspace, ở dạng UTC.

    Hạn mức bài mỗi ngày phải tính theo ngày của Workspace, không theo ngày
    UTC của máy chủ. Hai mốc này quyết định mọi truy vấn đếm bài trong ngày,
    nên chúng được tính một chỗ và dùng lại.

    Việc cộng một ngày giữ nguyên giờ tường nên đúng cả khi múi giờ có giờ
    tiết kiệm ban ngày: đầu ngày tiếp theo vẫn là nửa đêm địa phương.
    """
    local_day = day or moment.strftime("%Y-%m-%d")
    try:
        start_local = datetime.fromisoformat(f"{local_day}T00:00:00").replace(tzinfo=moment.tzinfo)
    except ValueError:
        start_local = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end_local.astimezone(timezone.utc).isoformat(timespec="seconds"))


def describe(settings: dict, moment: datetime | None = None) -> dict:
    """Mô tả ngắn cấu hình lịch, dùng cho giao diện."""
    moment = moment or now_in(settings.get("timezone", DEFAULT_TIMEZONE))
    windows = windows_of_day(moment, settings)
    return {
        "timezone": settings.get("timezone", DEFAULT_TIMEZONE),
        "mode": settings.get("mode", "spread"),
        "start_time": settings.get("start_time", DEFAULT_START),
        "end_time": settings.get("end_time", DEFAULT_END),
        "windows": [{"start": begin.strftime("%H:%M"), "end": finish.strftime("%H:%M")}
                    for begin, finish in windows],
        "min_interval_minutes": int(settings.get("min_interval_minutes") or DEFAULT_MIN_INTERVAL),
        "max_posts_per_account_per_day": int(
            settings.get("max_posts_per_account_per_day") or DEFAULT_MAX_POSTS_PER_DAY),
        "days_enabled": parse_days(settings.get("days_enabled_json")),
        "group_cooldown_minutes": int(
            settings.get("group_cooldown_minutes") or DEFAULT_GROUP_COOLDOWN),
        "paused": bool(settings.get("paused")),
    }
