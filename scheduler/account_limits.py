"""Hạn mức đăng của từng tài khoản và của cả Pipeline.

Phần này nằm trong package ``scheduler`` để mọi quy tắc về lịch và hạn mức
nằm cùng một chỗ. Module ``services.posting_limits`` vẫn giữ tên cũ để các
phần khác không phải đổi.

Bộ đếm số bài đã dùng hôm nay nằm trong bảng ``posting_counters``. Một bài đã
đăng và xác minh được tính vào ``published``; một bài đã bấm đăng nhưng chưa
xác minh vẫn giữ một suất dự phòng trong ``reserved`` cho tới khi được xác
minh, để không gửi trùng. Bài thất bại trả lại suất dự phòng.

Các hạn mức do người dùng đặt là quy tắc nội bộ của hệ thống, không phải
ngưỡng do Facebook công bố.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from scheduler import time_windows

try:  # pragma: no cover - múi giờ có sẵn trong Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_DAILY_LIMIT = 3
DEFAULT_MIN_GAP_MINUTES = 60
#: So bai cho group "can duyet" duoc chay cung luc.
DEFAULT_MAX_PENDING_APPROVALS = 1


def now_in(timezone_name: str) -> datetime:
    """The current time of the timezone of the workspace."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(timezone_name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def day_of(moment: datetime) -> str:
    """The day of a moment, in the timezone of the workspace."""
    return moment.strftime("%Y-%m-%d")


DEFAULT_SETTINGS = {
    "pipeline_id": None,
    "enabled": False,
    "scope": "all",
    "account_ids": [],
    "group_ids": [],
    "daily_limit_per_account": DEFAULT_DAILY_LIMIT,
    "min_gap_minutes": DEFAULT_MIN_GAP_MINUTES,
    "pipeline_daily_limit": 0,
    "topic": "",
    "ai_caption": True,
    "anti_duplicate": True,
    "require_approval": True,
    "apply_to_new_accounts": True,
    "timezone": DEFAULT_TIMEZONE,
    "max_pending_approvals": DEFAULT_MAX_PENDING_APPROVALS,
}


def settings_of(conn, pipeline_id: int) -> dict:
    """The shared configuration of a pipeline, with the default values."""
    row = conn.execute("SELECT * FROM pipeline_auto_post_settings WHERE pipeline_id=?",
                       (int(pipeline_id),)).fetchone()
    if row is None:
        return {**DEFAULT_SETTINGS, "pipeline_id": int(pipeline_id)}
    settings = dict(row)
    settings["enabled"] = bool(settings.get("enabled"))
    settings["ai_caption"] = bool(settings.get("ai_caption"))
    settings["anti_duplicate"] = bool(settings.get("anti_duplicate"))
    settings["require_approval"] = bool(settings.get("require_approval"))
    settings["apply_to_new_accounts"] = bool(settings.get("apply_to_new_accounts"))
    for field, fallback in (("account_ids", []), ("group_ids", [])):
        raw = settings.get(f"{field}_json")
        try:
            settings[field] = json.loads(raw) if raw else list(fallback)
        except (TypeError, ValueError):
            settings[field] = list(fallback)
    settings.pop("account_ids_json", None)
    settings.pop("group_ids_json", None)
    return settings


def limits_of(conn, account_id: int) -> dict:
    """The limits that belong to one account."""
    row = conn.execute("SELECT * FROM account_posting_limits WHERE account_id=?",
                       (int(account_id),)).fetchone()
    if row is None:
        return {"account_id": int(account_id), "daily_limit": None, "min_gap_minutes": None,
                "group_daily_limit": None, "paused": False}
    item = dict(row)
    item["paused"] = bool(item.get("paused"))
    return item


def effective_limit(conn, account_id: int, settings: dict) -> int:
    """How many posts this account may make today."""
    limits = limits_of(conn, account_id)
    if limits["daily_limit"] is not None:
        return int(limits["daily_limit"])
    return int(settings.get("daily_limit_per_account") or DEFAULT_DAILY_LIMIT)


def effective_gap(conn, account_id: int, settings: dict) -> int:
    """The smallest number of minutes between two posts of this account."""
    limits = limits_of(conn, account_id)
    if limits["min_gap_minutes"] is not None:
        return int(limits["min_gap_minutes"])
    return int(settings.get("min_gap_minutes") or DEFAULT_MIN_GAP_MINUTES)


def is_paused(conn, account_id: int) -> bool:
    """True when the administrator paused this account."""
    return bool(limits_of(conn, account_id)["paused"])


def usage_of(conn, account_id: int, day: str, moment: datetime | None = None) -> dict:
    """How many posts this account used today.

    Bộ đếm và bảng ``jobs`` được đối chiếu với nhau: bộ đếm giữ suất dự phòng
    cho bài đã bấm đăng nhưng chưa xác minh, còn bảng ``jobs`` là sự thật của
    những bài đã đăng. Số lớn hơn được lấy, nên một worker bị gián đoạn không
    làm sai hạn mức.

    Ngày được tính theo múi giờ của Workspace. Vì vậy khoảng thời gian lấy từ
    ``jobs`` được so sánh bằng hai mốc UTC của ngày đó, chứ không dùng
    ``date()`` của SQLite vì hàm đó đổi sang UTC và làm lệch ngày quanh nửa
    đêm.
    """
    row = conn.execute("""SELECT COALESCE(SUM(published),0) published,
                                  COALESCE(SUM(reserved),0) reserved
                           FROM posting_counters WHERE account_id=? AND day=?""",
                        (int(account_id), day)).fetchone()
    published = int(row["published"] or 0)
    reserved = int(row["reserved"] or 0)
    real = conn.execute("""SELECT COUNT(*) FROM jobs
                            WHERE account_id=? AND state IN
                              ('posted','submitted_unverified','pending_approval')
                              AND datetime(COALESCE(published_at, updated_at)) >= datetime(?)
                              AND datetime(COALESCE(published_at, updated_at)) < datetime(?)""",
                         (int(account_id), *_day_bounds(moment, day))).fetchone()[0]
    published = max(published, int(real))
    return {"published": published, "reserved": reserved, "used": published + reserved}


def _day_bounds(moment: datetime | None, day: str) -> tuple[str, str]:
    """Hai mốc UTC của một ngày theo múi giờ của Workspace."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    return time_windows.day_bounds_utc(moment, day)


def group_usage_of(conn, group_id: int, day: str) -> int:
    """How many posts went to this group today."""
    row = conn.execute("""SELECT COALESCE(SUM(published),0) + COALESCE(SUM(reserved),0) used
                           FROM posting_counters WHERE group_id=? AND day=?""",
                        (int(group_id), day)).fetchone()
    return int(row["used"] or 0)


def pipeline_usage_of(conn, pipeline_id: int, day: str) -> int:
    """How many posts the whole pipeline used today."""
    row = conn.execute("""SELECT COALESCE(SUM(published),0) + COALESCE(SUM(reserved),0) used
                           FROM posting_counters c JOIN accounts a ON a.id=c.account_id
                           WHERE a.pipeline_id=? AND c.day=?""",
                        (int(pipeline_id), day)).fetchone()
    return int(row["used"] or 0)


def last_post_at(conn, account_id: int) -> str | None:
    """When this account published something the last time."""
    row = conn.execute("""SELECT MAX(updated_at) moment FROM jobs
                           WHERE account_id=? AND state IN ('posted','submitted_unverified')""",
                        (int(account_id),)).fetchone()
    return row["moment"] if row and row["moment"] else None


def reserve(conn, account_id: int, group_id: int, day: str) -> None:
    """Hold one slot for a job that was just created."""
    conn.execute("""INSERT INTO posting_counters(account_id,group_id,day,published,reserved,updated_at)
                    VALUES(?,?,?,0,1,?)
                    ON CONFLICT(account_id,group_id,day) DO UPDATE SET
                        reserved=reserved+1, updated_at=excluded.updated_at""",
                 (int(account_id), int(group_id or 0), day, datetime.now(timezone.utc).isoformat()))


def confirm(conn, account_id: int, group_id: int, day: str) -> None:
    """Turn a reservation into a published post."""
    conn.execute("""UPDATE posting_counters SET published=published+1,
                        reserved=MAX(reserved-1,0), updated_at=?
                    WHERE account_id=? AND group_id=? AND day=?""",
                 (datetime.now(timezone.utc).isoformat(), int(account_id), int(group_id or 0), day))


def release(conn, account_id: int, group_id: int, day: str) -> None:
    """Give the slot of a job that failed back to the account."""
    conn.execute("""UPDATE posting_counters SET reserved=MAX(reserved-1,0), updated_at=?
                    WHERE account_id=? AND group_id=? AND day=?""",
                 (datetime.now(timezone.utc).isoformat(), int(account_id), int(group_id or 0), day))


def reconcile(conn, pipeline_id: int) -> None:
    """Rebuild the counters of a pipeline from the jobs that really exist.

    A crash of the worker must never leave a wrong counter behind.
    """
    timezone_name = pipeline_timezone(conn, pipeline_id)
    zone = time_windows.zone(timezone_name)
    rows = conn.execute("""SELECT j.account_id, j.group_id, j.posting_status, j.state,
                                  COALESCE(j.published_at, j.updated_at) moment
                             FROM jobs j WHERE j.pipeline_id=?""", (int(pipeline_id),)).fetchall()
    conn.execute("DELETE FROM posting_counters WHERE account_id IN"
                 " (SELECT id FROM accounts WHERE pipeline_id=?)", (int(pipeline_id),))
    for row in rows:
        day = _local_day(row["moment"], zone)
        if not day:
            continue
        status = row["posting_status"] or row["state"]
        if status == "posted":
            confirm(conn, row["account_id"], row["group_id"], day)
        elif status == "submitted_unverified":
            reserve(conn, row["account_id"], row["group_id"], day)


def pipeline_timezone(conn, pipeline_id: int) -> str:
    """Múi giờ của Workspace, dùng để tính ngày của hạn mức."""
    row = conn.execute("""SELECT timezone FROM pipeline_auto_post_settings
                          WHERE pipeline_id=?""", (int(pipeline_id),)).fetchone()
    if row is None or not row["timezone"]:
        row = conn.execute("""SELECT timezone FROM pipeline_posting_schedule
                              WHERE pipeline_id=?""", (int(pipeline_id),)).fetchone()
    if row is not None and row["timezone"]:
        return row["timezone"]
    return DEFAULT_TIMEZONE


def _local_day(value: str | None, zone) -> str:
    """Ngày theo múi giờ của Workspace của một mốc thời gian."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone).strftime("%Y-%m-%d")


def account_state(conn, account_id: int, settings: dict, moment: datetime | None = None) -> dict:
    """Everything the dashboard needs to know about one account."""
    moment = moment or now_in(settings.get("timezone", DEFAULT_TIMEZONE))
    day = day_of(moment)
    usage = usage_of(conn, account_id, day, moment)
    limit = effective_limit(conn, account_id, settings)
    gap = effective_gap(conn, account_id, settings)
    last = last_post_at(conn, account_id)
    next_allowed = None
    if last:
        try:
            last_moment = datetime.fromisoformat(last)
            if last_moment.tzinfo is None:
                last_moment = last_moment.replace(tzinfo=moment.tzinfo)
            candidate = last_moment + timedelta(minutes=gap)
            if candidate > moment:
                next_allowed = candidate.isoformat()
        except ValueError:
            next_allowed = None
    return {
        "account_id": int(account_id),
        "daily_limit": limit,
        "min_gap_minutes": gap,
        "paused": is_paused(conn, account_id),
        "published": usage["published"],
        "reserved": usage["reserved"],
        "used": usage["used"],
        "remaining": max(0, limit - usage["used"]),
        "at_limit": usage["used"] >= limit,
        "last_post_at": last,
        "next_allowed_at": next_allowed,
    }
