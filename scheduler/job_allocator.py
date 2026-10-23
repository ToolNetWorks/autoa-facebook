"""Bộ phân bổ lịch đăng của một Pipeline.

Bộ phân bổ làm đúng thứ tự kiểm tra mà hệ thống phải tuân theo:

1. Tài khoản đang hoạt động.
2. Hạn mức bài mỗi ngày của tài khoản.
3. Khung giờ được phép và ngày trong tuần.
4. Khoảng cách tối thiểu giữa hai bài của cùng tài khoản.
5. Group đủ điều kiện và đang hết thời gian nghỉ.
6. Bài trùng lặp.
7. Kiểm tra an toàn của ảnh và caption.
8. Chỗ đứng thời gian hợp lệ.

Mỗi job luôn có sẵn tài khoản, group, ảnh và caption, nên không bao giờ phải
đổi tài khoản lúc chạy. Khi không đủ khung giờ hoặc hạn mức, job được ghi
lại lý do trì hoãn thay vì bị bỏ mất.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from scheduler import account_limits, group_cooldowns, time_windows
from services import ai_provider, duplicate_guard
from services import group_eligibility
from services.auto_posting import create_caption
from services.caption_validator import analyze_caption
from services.destination_lock import DestinationLockService
from services.image_selection import select_media

LOG = logging.getLogger("facebook-pipeline.scheduler")

MODE_SPREAD = "spread"
MODE_WINDOWS = "windows"

DEFAULT_SCHEDULE = {
    "pipeline_id": None,
    "mode": MODE_SPREAD,
    "timezone": time_windows.DEFAULT_TIMEZONE,
    "start_time": time_windows.DEFAULT_START,
    "end_time": time_windows.DEFAULT_END,
    "windows_json": "[]",
    "min_interval_minutes": time_windows.DEFAULT_MIN_INTERVAL,
    "max_posts_per_account_per_day": time_windows.DEFAULT_MAX_POSTS_PER_DAY,
    "days_enabled_json": json.dumps(time_windows.DEFAULT_DAYS),
    "group_cooldown_minutes": time_windows.DEFAULT_GROUP_COOLDOWN,
    "paused": False,
}


def schedule_of(conn, pipeline_id: int) -> dict:
    """Cấu hình lịch đăng của một Pipeline."""
    row = conn.execute("SELECT * FROM pipeline_posting_schedule WHERE pipeline_id=?",
                       (int(pipeline_id),)).fetchone()
    if row is None:
        return {**DEFAULT_SCHEDULE, "pipeline_id": int(pipeline_id)}
    schedule = dict(row)
    schedule["paused"] = bool(schedule.get("paused"))
    return schedule


def save_schedule(conn, pipeline_id: int, data: dict) -> dict:
    """Ghi cấu hình lịch đăng."""
    current = schedule_of(conn, pipeline_id)
    merged = {**current, **{key: value for key, value in data.items() if value is not None}}
    conn.execute("""INSERT INTO pipeline_posting_schedule
        (pipeline_id,mode,timezone,start_time,end_time,windows_json,min_interval_minutes,
         max_posts_per_account_per_day,days_enabled_json,group_cooldown_minutes,paused,
         created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pipeline_id) DO UPDATE SET mode=excluded.mode, timezone=excluded.timezone,
            start_time=excluded.start_time, end_time=excluded.end_time,
            windows_json=excluded.windows_json, min_interval_minutes=excluded.min_interval_minutes,
            max_posts_per_account_per_day=excluded.max_posts_per_account_per_day,
            days_enabled_json=excluded.days_enabled_json,
            group_cooldown_minutes=excluded.group_cooldown_minutes, paused=excluded.paused,
            updated_at=excluded.updated_at""",
                 (int(pipeline_id), merged.get("mode", MODE_SPREAD),
                  merged.get("timezone", time_windows.DEFAULT_TIMEZONE),
                  merged.get("start_time", time_windows.DEFAULT_START),
                  merged.get("end_time", time_windows.DEFAULT_END),
                  merged.get("windows_json", "[]"),
                  int(merged.get("min_interval_minutes") or time_windows.DEFAULT_MIN_INTERVAL),
                  int(merged.get("max_posts_per_account_per_day")
                      or time_windows.DEFAULT_MAX_POSTS_PER_DAY),
                  merged.get("days_enabled_json") or json.dumps(time_windows.DEFAULT_DAYS),
                  int(merged.get("group_cooldown_minutes")
                      or time_windows.DEFAULT_GROUP_COOLDOWN),
                  int(bool(merged.get("paused"))),
                  datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
    return schedule_of(conn, pipeline_id)


def account_overrides(conn, pipeline_id: int) -> dict:
    """Ngoại lệ riêng của từng tài khoản."""
    rows = conn.execute("""SELECT s.* FROM account_schedules s JOIN accounts a ON a.id=s.account_id
                           WHERE a.pipeline_id=?""", (int(pipeline_id),)).fetchall()
    return {row["account_id"]: dict(row) for row in rows}


def record_history(conn, pipeline_id: int, action: str, reason: str = "",
                   job_id: int | None = None, account_id: int | None = None,
                   group_id: int | None = None, scheduled_for: str | None = None) -> None:
    """Ghi lại lịch sử và lý do trì hoãn của job."""
    conn.execute("""INSERT INTO posting_schedule_history
        (pipeline_id,job_id,account_id,group_id,action,reason,scheduled_for,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
                 (int(pipeline_id), job_id, account_id, group_id, action, str(reason)[:400],
                  scheduled_for, datetime.now(timezone.utc).isoformat()))


def _account_rows(conn, pipeline_id: int, settings: dict) -> list[dict]:
    """Các tài khoản Facebook được phép đăng trong Workspace."""
    rows = conn.execute("""SELECT a.* FROM accounts a WHERE a.pipeline_id=? ORDER BY a.id""",
                        (int(pipeline_id),)).fetchall()
    chosen = settings.get("account_ids") or []
    accounts = []
    for row in rows:
        item = dict(row)
        if settings.get("scope") == "selected" and chosen and item["id"] not in chosen:
            continue
        if not item.get("enabled"):
            continue
        if (item.get("session_status") or "") in ("expired", "error", "invalid", "revoked"):
            continue
        accounts.append(item)
    return accounts


def _group_rows(conn, pipeline_id: int, settings: dict) -> list[dict]:
    """Các group của Workspace kèm thông tin tài khoản."""
    rows = conn.execute("""SELECT g.*, a.label account_label, a.session_status account_session,
                                  a.enabled account_enabled
                             FROM groups g LEFT JOIN accounts a ON a.id=g.account_id
                            WHERE g.pipeline_id=? ORDER BY g.id""", (int(pipeline_id),)).fetchall()
    groups = []
    chosen = settings.get("group_ids") or []
    for row in rows:
        item = dict(row)
        if chosen and item["id"] not in chosen:
            continue
        groups.append(item)
    return groups


def _media_rows(conn, pipeline_id: int) -> list[dict]:
    """Ảnh của Album kèm dấu vết chống trùng."""
    rows = conn.execute("""SELECT id, description, tags_json, safety_status, analysis_status,
                                   usable, locked, sha256, signature
                              FROM media_assets WHERE pipeline_id=? ORDER BY id""",
                        (int(pipeline_id),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.get("tags_json") or "[]")
        except (TypeError, ValueError):
            item["tags"] = []
        item["usable"] = bool(item.get("usable"))
        item["locked"] = bool(item.get("locked"))
        result.append(item)
    return result


def _job_key(pipeline_id: int, account_id: int, group_id: int, media_id: int, day: str) -> str:
    return f"{int(pipeline_id)}:{int(account_id)}:{int(group_id)}:{int(media_id)}:{day}"


def plan(conn, pipeline_id: int, moment: datetime | None = None,
         provider_config=None) -> dict:
    """Lịch sẽ tạo hôm nay, chưa ghi vào database."""
    settings = account_limits.settings_of(conn, pipeline_id)
    schedule = schedule_of(conn, pipeline_id)
    moment = moment or time_windows.now_in(schedule["timezone"])
    if not settings.get("enabled"):
        return {"enabled": False, "reason": "Tự động đăng group đang tắt", "jobs": [], "notes": []}
    if schedule.get("paused"):
        return {"enabled": True, "paused": True, "reason": "Lịch đăng đang tạm dừng",
                "jobs": [], "notes": []}
    days = time_windows.parse_days(schedule.get("days_enabled_json"))
    if not time_windows.day_enabled(moment, days):
        return {"enabled": True, "reason": "Hôm nay không nằm trong các ngày được phép đăng",
                "jobs": [], "notes": []}
    if not time_windows.is_inside_window(moment, schedule):
        return {"enabled": True, "reason": "Ngoài khung giờ được phép đăng",
                "jobs": [], "notes": [], "next_allowed": time_windows.next_allowed_moment(
                    moment, schedule).isoformat()}

    accounts = _account_rows(conn, pipeline_id, settings)
    groups = _group_rows(conn, pipeline_id, settings)
    eligible = group_eligibility.eligible_pairs(conn, pipeline_id, groups, for_scheduling=True)
    media = _media_rows(conn, pipeline_id)
    overrides = account_overrides(conn, pipeline_id)

    quotas: dict[int, int] = {}
    notes: list[str] = []
    for account in accounts:
        override = overrides.get(account["id"], {})
        if override.get("paused"):
            notes.append(f"{account['label']}: lịch đang tạm dừng")
            continue
        state = account_limits.account_state(conn, account["id"], settings, moment)
        if state["at_limit"]:
            notes.append(f"{account['label']}: đã đủ {state['used']}/{state['daily_limit']} bài hôm nay")
            continue
        quota = int(schedule.get("max_posts_per_account_per_day")
                    or time_windows.DEFAULT_MAX_POSTS_PER_DAY)
        quota = min(quota, max(0, state["remaining"]))
        if quota <= 0:
            continue
        quotas[account["id"]] = quota
    if not quotas:
        return {"enabled": True, "reason": "Không có tài khoản nào còn hạn mức hôm nay",
                "jobs": [], "notes": notes}

    windows = time_windows.windows_of_day(moment, schedule)
    slots = time_windows.spread_slots(windows, quotas,
                                      int(schedule.get("min_interval_minutes")
                                          or time_windows.DEFAULT_MIN_INTERVAL),
                                      moment.replace(hour=0, minute=0, second=0, microsecond=0))
    provider_config = provider_config or ai_provider.load_config()

    jobs: list[dict] = []
    used_media: set[int] = set()
    for account_id, moment_slot in slots:
        account = next((item for item in accounts if item["id"] == account_id), None)
        if account is None:
            continue
        own_groups = group_eligibility.groups_for_account(eligible, account_id)
        if not own_groups:
            notes.append(f"{account['label']}: chưa có group đủ điều kiện")
            continue
        # Chọn group sớm nhất nào sẵn sàng nhận bài.
        chosen_group = None
        ready_at = moment_slot
        for group in own_groups:
            available = group_cooldowns.next_available_at(
                conn, group["id"], moment_slot,
                int(schedule.get("group_cooldown_minutes")
                    or time_windows.DEFAULT_GROUP_COOLDOWN))
            if available is None:
                available = moment_slot
            if chosen_group is None or available < ready_at:
                chosen_group = group
                ready_at = available
        if chosen_group is None:
            continue
        if ready_at > moment_slot:
            notes.append(f"{chosen_group.get('label')}: chờ hết thời gian nghỉ của group")
        scheduled_for = max(ready_at, moment_slot)
        if not time_windows.is_inside_window(scheduled_for, schedule):
            notes.append(f"{chosen_group.get('label')}: thời điểm đăng nằm ngoài khung giờ")
            continue
        candidates = [item for item in media if item["id"] not in used_media]
        chosen = select_media(candidates, settings.get("topic", ""))
        picture = chosen.get("media")
        if not picture:
            notes.append("Album chưa có ảnh đủ điều kiện an toàn")
            break
        caption_result = create_caption(picture.get("description") or "",
                                        list(picture.get("tags") or []),
                                        settings.get("topic", ""), provider_config)
        if caption_result.get("status") == "blocked":
            notes.append("Caption bị chặn vì nội dung rủi ro")
            continue
        # Apply Destination Lock if configured for this pipeline
        lock_service = DestinationLockService()
        caption_with_lock, was_locked, lock_msg = lock_service.apply_destination_lock(
            caption_result.get("caption", ""), pipeline_id)
        if was_locked:
            caption_result["caption"] = caption_with_lock
            caption_result.setdefault("reasons", []).append(f"Destination Lock: {lock_msg}")
        verdict = duplicate_guard.check(conn, pipeline_id, picture,
                                        caption_result.get("caption", ""),
                                        chosen_group["id"], account_id)
        if settings.get("anti_duplicate") and verdict["blocked"]:
            notes.append(f"{chosen_group.get('label')}: {verdict['reason']}")
            continue
        jobs.append({
            "account_id": account_id, "account_label": account.get("label"),
            "group_id": chosen_group["id"], "group_label": chosen_group.get("label"),
            "media_id": picture["id"], "media_description": (picture.get("description") or "")[:120],
            "caption": caption_result.get("caption", ""),
            "caption_status": caption_result.get("status"),
            "posting_mode": chosen_group.get("posting_mode") or "anonymous",
            "nickname": chosen_group.get("nickname") or "",
            "scheduled_for": scheduled_for.isoformat(),
            "key": _job_key(pipeline_id, account_id, chosen_group["id"], picture["id"],
                            scheduled_for.strftime("%Y-%m-%d")),
        })
        used_media.add(picture["id"])
    return {"enabled": True, "jobs": jobs, "notes": notes, "day": moment.strftime("%Y-%m-%d"),
            "accounts": accounts, "quota": quotas}


def allocate(conn, pipeline_id: int, moment: datetime | None = None,
             provider_config=None, max_jobs: int = 20) -> dict:
    """Tạo job theo lịch đã tính.

    Lịch là idempotent: mỗi job có một khóa, nên chạy lại nhiều lần không tạo
    thêm job trùng. Việc ghi job, giữ chỗ và ghi lịch sử nằm trong cùng một
    giao dịch.
    """
    schedule = schedule_of(conn, pipeline_id)
    moment = moment or time_windows.now_in(schedule["timezone"])
    day = moment.strftime("%Y-%m-%d")
    start, end = time_windows.day_bounds_utc(moment, day)
    existing = conn.execute("""SELECT COUNT(*) FROM auto_post_jobs
                               WHERE pipeline_id=?
                                 AND datetime(scheduled_for) >= datetime(?)
                                 AND datetime(scheduled_for) < datetime(?)""",
                            (int(pipeline_id), start, end)).fetchone()[0]
    outcome = plan(conn, pipeline_id, moment, provider_config)
    jobs = outcome.get("jobs", [])
    if existing >= len(jobs):
        return {"created": [], "skipped": outcome.get("notes", []), "day": day,
                "planned": len(jobs), "already": existing,
                "reason": outcome.get("reason", "")}
    settings = account_limits.settings_of(conn, pipeline_id)
    created = []
    for job in jobs[:max_jobs]:
        try:
            cursor = conn.execute("""INSERT INTO auto_post_jobs
                (pipeline_id,config_id,media_id,account_id,group_id,caption,posting_mode,nickname,
                 status,needs_approval,idempotency_key,scheduled_for,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(pipeline_id), None, job["media_id"], job["account_id"], job["group_id"],
                 job["caption"], job["posting_mode"], job["nickname"],
                 "pending" if settings.get("require_approval", True) else "approved",
                 int(settings.get("require_approval", True)), job["key"],
                 job["scheduled_for"], datetime.now(timezone.utc).isoformat(),
                 datetime.now(timezone.utc).isoformat()))
        except Exception as error:
            LOG.info("Không tạo được job: %s", type(error).__name__)
            continue
        job_id = cursor.lastrowid
        account_limits.reserve(conn, job["account_id"], job["group_id"], day)
        media = conn.execute("SELECT sha256, signature FROM media_assets WHERE id=?",
                             (job["media_id"],)).fetchone()
        sha = media["sha256"] if media else ""
        signature = media["signature"] if media else ""
        duplicate_guard.record(conn, pipeline_id, duplicate_guard.KIND_MEDIA_EXACT,
                               duplicate_guard.media_fingerprint(sha), job["media_id"], job_id)
        duplicate_guard.record(conn, pipeline_id, duplicate_guard.KIND_MEDIA_NEAR,
                               duplicate_guard.near_fingerprint(signature), job["media_id"], job_id)
        duplicate_guard.record(conn, pipeline_id, duplicate_guard.KIND_CAPTION,
                               duplicate_guard.caption_fingerprint(job["caption"]),
                               job["media_id"], job_id, job["group_id"])
        duplicate_guard.record(conn, pipeline_id, duplicate_guard.KIND_CONTENT,
                               duplicate_guard.content_fingerprint(
                                   duplicate_guard.media_fingerprint(sha), job["caption"]),
                               job["media_id"], job_id, job["group_id"])
        conn.execute("""INSERT INTO media_usage_history(pipeline_id,media_id,account_id,group_id,job_id,used_at,note)
                        VALUES(?,?,?,?,?,?,?)""",
                     (int(pipeline_id), job["media_id"], job["account_id"], job["group_id"],
                      job_id, datetime.now(timezone.utc).isoformat(), "lịch thông minh"))
        record_history(conn, pipeline_id, "allocated", "Đã xếp lịch", job_id,
                       job["account_id"], job["group_id"], job["scheduled_for"])
        created.append({**job, "id": job_id})
    for note in outcome.get("notes", []):
        record_history(conn, pipeline_id, "postponed", note)
    return {"created": created, "skipped": outcome.get("notes", []), "day": day,
            "planned": len(jobs), "already": existing}


def history_of(conn, pipeline_id: int, limit: int = 50) -> list[dict]:
    """Lịch sử xếp lịch và lý do trì hoãn."""
    rows = conn.execute("""SELECT * FROM posting_schedule_history WHERE pipeline_id=?
                           ORDER BY id DESC LIMIT ?""", (int(pipeline_id), int(limit))).fetchall()
    return [dict(row) for row in rows]


def preview(conn, pipeline_id: int, moment: datetime | None = None) -> dict:
    """Lịch dự kiến của từng Facebook, dùng cho giao diện."""
    schedule = schedule_of(conn, pipeline_id)
    moment = moment or time_windows.now_in(schedule["timezone"])
    outcome = plan(conn, pipeline_id, moment)
    by_account: dict[int, list[dict]] = {}
    for job in outcome.get("jobs", []):
        by_account.setdefault(job["account_id"], []).append(job)
    accounts = outcome.get("accounts", [])
    rows = []
    for account in accounts:
        jobs = by_account.get(account["id"], [])
        state = account_limits.account_state(conn, account["id"],
                                             account_limits.settings_of(conn, pipeline_id), moment)
        rows.append({
            "account_id": account["id"], "label": account["label"],
            "daily_limit": state["daily_limit"], "used": state["used"],
            "remaining": state["remaining"], "at_limit": state["at_limit"],
            "paused": state["paused"],
            "planned": len(jobs),
            "slots": [{"group": job["group_label"], "media_id": job["media_id"],
                       "scheduled_for": job["scheduled_for"]} for job in jobs],
        })
    return {"ok": True, "settings": time_windows.describe(schedule, moment),
            "accounts": rows, "notes": outcome.get("notes", []),
            "reason": outcome.get("reason", ""),
            "expected_jobs": len(outcome.get("jobs", [])),
            "next_allowed": outcome.get("next_allowed")}
