"""Bộ xếp lịch riêng cho từng CTV trong một Pipeline.

Một Pipeline có thể có nhiều cấu hình tự động đăng cùng lúc: cấu hình chung
của quản trị và cấu hình riêng của từng CTV. Mỗi cấu hình có chủ sở hữu và
lịch riêng, nhưng tất cả phải chịu các quy tắc an toàn của **cả Pipeline**:

1. Một Facebook không được có hai job của hai chủ sở hữu khác nhau cùng lúc.
2. Hạn mức bài mỗi ngày của một Facebook tính tổng từ mọi CTV.
3. Hai CTV không được nhắm vào cùng một group.
4. Ảnh và caption trùng bị chặn trên toàn Pipeline.
5. Giới hạn riêng của CTV không được vượt giới hạn chung của quản trị.
6. Một job luôn giữ đúng tài khoản đã chọn lúc xếp lịch, không bao giờ đổi
   tài khoản để né hạn mức.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from scheduler import account_limits, group_cooldowns, time_windows
from services import ai_provider, ctv_auto_post, duplicate_guard
from services import group_eligibility
from services.auto_posting import create_caption
from services.destination_lock import DestinationLockService
from services.image_selection import select_media

LOG = logging.getLogger("facebook-pipeline.ctv-allocator")

OPEN_STATUSES = ("pending", "approved", "queued")


# --- helpers -----------------------------------------------------------------

def _schedule_of(settings: dict) -> dict:
    """Lịch của một cấu hình CTV, theo đúng cấu trúc mà time_windows dùng."""
    rules = settings.get("schedule_rules") or {}
    days = rules.get("days_enabled") or list(ctv_auto_post.DEFAULT_DAYS)
    return {
        "mode": "spread",
        "timezone": settings.get("timezone") or ctv_auto_post.DEFAULT_TIMEZONE,
        "start_time": settings.get("start_time") or ctv_auto_post.DEFAULT_START_TIME,
        "end_time": settings.get("end_time") or ctv_auto_post.DEFAULT_END_TIME,
        "windows_json": "[]",
        "min_interval_minutes": int(rules.get("min_gap_minutes")
                                    or ctv_auto_post.DEFAULT_MIN_GAP_MINUTES),
        "max_posts_per_account_per_day": int(settings.get("max_posts_per_account_daily")
                                             or ctv_auto_post.DEFAULT_MAX_POSTS),
        "days_enabled_json": json.dumps(list(days)),
        "group_cooldown_minutes": 0,
        "paused": False,
    }


def _account_rows(conn, pipeline_id: int, settings: dict) -> list[dict]:
    """Các Facebook mà cấu hình của CTV được phép dùng."""
    rows = conn.execute("""SELECT a.* FROM accounts a WHERE a.pipeline_id=? ORDER BY a.id""",
                        (int(pipeline_id),)).fetchall()
    chosen = settings.get("selected_account_ids") or []
    result = []
    for row in rows:
        item = dict(row)
        if (settings.get("account_scope") == ctv_auto_post.SCOPE_SELECTED
                and chosen and item["id"] not in chosen):
            continue
        if not item.get("enabled"):
            continue
        result.append(item)
    return result


def _group_rows(conn, pipeline_id: int, settings: dict) -> list[dict]:
    """Các group mà cấu hình của CTV được phép dùng."""
    rows = conn.execute("""SELECT g.*, a.label account_label, a.session_status account_session,
                                  a.enabled account_enabled
                             FROM groups g LEFT JOIN accounts a ON a.id=g.account_id
                            WHERE g.pipeline_id=? ORDER BY g.id""",
                        (int(pipeline_id),)).fetchall()
    chosen = settings.get("selected_group_ids") or []
    result = []
    for row in rows:
        item = dict(row)
        if chosen and item["id"] not in chosen:
            continue
        result.append(item)
    return result


def _media_rows(conn, pipeline_id: int) -> list[dict]:
    """Ảnh của album kèm dấu vết chống trùng."""
    rows = conn.execute("""SELECT id, description, tags_json, safety_status, analysis_status,
                                  usable, locked, sha256, signature, usage_count, last_used_at
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


def busy_accounts(conn, pipeline_id: int, owner_id: int | None) -> set[int]:
    """Facebook đang có job của một chủ sở hữu khác.

    Một Facebook chỉ được một chủ sở hữu dùng tại một thời điểm, nên hai CTV
    không thể cùng nhận job trên cùng một tài khoản Facebook.
    """
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    if owner_id is None:
        rows = conn.execute(f"""SELECT DISTINCT account_id FROM auto_post_jobs
                                WHERE pipeline_id=? AND status IN ({placeholders})
                                  AND created_by_ctv_id IS NOT NULL""",
                            (int(pipeline_id), *OPEN_STATUSES)).fetchall()
    else:
        rows = conn.execute(f"""SELECT DISTINCT account_id FROM auto_post_jobs
                                WHERE pipeline_id=? AND status IN ({placeholders})
                                  AND (created_by_ctv_id IS NULL OR created_by_ctv_id<>?)""",
                            (int(pipeline_id), *OPEN_STATUSES, int(owner_id))).fetchall()
    return {int(row["account_id"]) for row in rows if row["account_id"]}


def claimed_groups(conn, pipeline_id: int, owner_id: int | None) -> set[int]:
    """Group đang bị một chủ sở hữu khác nhắm tới."""
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    if owner_id is None:
        rows = conn.execute(f"""SELECT DISTINCT group_id FROM auto_post_jobs
                                WHERE pipeline_id=? AND status IN ({placeholders})
                                  AND created_by_ctv_id IS NOT NULL""",
                            (int(pipeline_id), *OPEN_STATUSES)).fetchall()
    else:
        rows = conn.execute(f"""SELECT DISTINCT group_id FROM auto_post_jobs
                                WHERE pipeline_id=? AND status IN ({placeholders})
                                  AND (created_by_ctv_id IS NULL OR created_by_ctv_id<>?)""",
                            (int(pipeline_id), *OPEN_STATUSES, int(owner_id))).fetchall()
    return {int(row["group_id"]) for row in rows if row["group_id"]}


def _job_key(pipeline_id: int, owner_id: int | None, account_id: int, group_id: int,
             media_id: int, day: str) -> str:
    """Khóa chống tạo trùng job, có phân biệt chủ sở hữu."""
    owner = "shared" if owner_id is None else int(owner_id)
    return f"{int(pipeline_id)}:{owner}:{int(account_id)}:{int(group_id)}:{int(media_id)}:{day}"


def _order_media(candidates: list[dict], rule: str, seed: str) -> list[dict]:
    """Sắp xếp ảnh theo quy tắc chọn ảnh của cấu hình."""
    if rule == ctv_auto_post.RULE_UNUSED_FIRST:
        return sorted(candidates, key=lambda item: (int(item.get("usage_count") or 0),
                                                    str(item.get("last_used_at") or ""),
                                                    int(item.get("id") or 0)))
    if rule == ctv_auto_post.RULE_RANDOM:
        return sorted(candidates, key=lambda item: _stable_hash(f"{seed}:{item.get('id')}"))
    return candidates


def _stable_hash(text: str) -> int:
    value = 0
    for char in str(text):
        value = (value * 131 + ord(char)) % 1000003
    return value


# --- planning ----------------------------------------------------------------

def plan_for_owner(conn, pipeline_id: int, settings: dict, moment: datetime | None = None,
                   provider_config=None) -> dict:
    """Lịch của một chủ sở hữu trong ngày, chưa ghi vào database."""
    owner_id = settings.get("ctv_user_id")
    schedule = _schedule_of(settings)
    moment = moment or time_windows.now_in(schedule["timezone"])
    day = moment.strftime("%Y-%m-%d")
    if not settings.get("enabled"):
        return {"enabled": False, "reason": "Tự động đăng group đang tắt",
                "jobs": [], "notes": [], "day": day}
    days = time_windows.parse_days(schedule.get("days_enabled_json"))
    if not time_windows.day_enabled(moment, days):
        return {"enabled": True, "reason": "Hôm nay không nằm trong các ngày được phép đăng",
                "jobs": [], "notes": [], "day": day}
    if not time_windows.is_inside_window(moment, schedule):
        return {"enabled": True, "reason": "Ngoài khung giờ được phép đăng",
                "jobs": [], "notes": [], "day": day,
                "next_allowed": time_windows.next_allowed_moment(moment, schedule).isoformat()}

    accounts = _account_rows(conn, pipeline_id, settings)
    groups = _group_rows(conn, pipeline_id, settings)
    eligible = group_eligibility.eligible_pairs(conn, pipeline_id, groups, for_scheduling=True)
    media = _media_rows(conn, pipeline_id)
    provider_config = provider_config or ai_provider.load_config()
    rules = settings.get("schedule_rules") or {}
    group_daily_limit = int(rules.get("group_daily_limit") or 0)
    policy = ctv_auto_post.pipeline_policy(conn, pipeline_id)

    taken_accounts = busy_accounts(conn, pipeline_id, owner_id)
    taken_groups = claimed_groups(conn, pipeline_id, owner_id)

    notes: list[str] = []
    quotas: dict[int, int] = {}
    for account in accounts:
        if account["id"] in taken_accounts:
            notes.append(f"{account['label']}: đang có lịch của CTV khác trên Facebook này")
            continue
        state = account_limits.account_state(conn, account["id"], settings, moment)
        if state["paused"]:
            notes.append(f"{account['label']}: đang tạm dừng")
            continue
        if state["at_limit"]:
            notes.append(f"{account['label']}: đã đủ {state['used']}/{state['daily_limit']} bài hôm nay")
            continue
        # Giới hạn riêng của CTV, không vượt quá giới hạn chung của quản trị.
        own_limit = int(settings.get("max_posts_per_account_daily")
                        or ctv_auto_post.DEFAULT_MAX_POSTS)
        account_policy = ctv_auto_post.account_policy(conn, account["id"])
        if account_policy.get("daily_limit") is not None:
            own_limit = min(own_limit, int(account_policy["daily_limit"]))
        if policy.get("daily_limit_per_account"):
            own_limit = min(own_limit, int(policy["daily_limit_per_account"]))
        own_limit = max(0, min(own_limit, int(state["remaining"])))
        used_by_owner = ctv_auto_post.usage_of(conn, pipeline_id, owner_id, account["id"], day)
        own_limit = max(0, min(own_limit, own_limit - used_by_owner))
        if own_limit <= 0:
            notes.append(f"{account['label']}: đã đủ {own_limit} bài/ngày của riêng CTV này")
            continue
        quotas[account["id"]] = own_limit
    if not quotas:
        return {"enabled": True, "reason": "Không có tài khoản nào còn hạn mức hôm nay",
                "jobs": [], "notes": notes, "day": day}

    windows = time_windows.windows_of_day(moment, schedule)
    slots = time_windows.spread_slots(windows, quotas, int(schedule["min_interval_minutes"]),
                                      moment.replace(hour=0, minute=0, second=0, microsecond=0))
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
        chosen_group = None
        ready_at = moment_slot
        for group in own_groups:
            if group["id"] in taken_groups:
                continue
            if group_daily_limit and _group_usage_today(conn, group["id"], day, moment) >= group_daily_limit:
                continue
            available = group_cooldowns.next_available_at(conn, group["id"], moment_slot, 0)
            if available is None:
                available = moment_slot
            if chosen_group is None or available < ready_at:
                chosen_group = group
                ready_at = available
        if chosen_group is None:
            notes.append(f"{account['label']}: mọi group đủ điều kiện đều đang có lịch của CTV khác")
            continue
        scheduled_for = max(ready_at, moment_slot)
        if not time_windows.is_inside_window(scheduled_for, schedule):
            notes.append(f"{chosen_group.get('label')}: thời điểm đăng nằm ngoài khung giờ")
            continue
        if not media:
            notes.append("Album chưa có ảnh đủ điều kiện an toàn")
            break
        candidates = [item for item in media if item["id"] not in used_media]
        if not candidates:
            candidates = list(media)
        candidates = _order_media(candidates, rules.get("image_selection_rule", ""),
                                  f"{pipeline_id}:{owner_id}:{day}")
        chosen = select_media(candidates, settings.get("topic", ""))
        picture = chosen.get("media")
        if not picture and len(candidates) < len(media):
            fallback = _order_media(list(media), rules.get("image_selection_rule", ""),
                                    f"{pipeline_id}:{owner_id}:{day}")
            picture = select_media(fallback, settings.get("topic", "")).get("media")
        if not picture:
            notes.append(f"{chosen_group.get('label')}: chưa có ảnh phù hợp")
            continue
        caption_result = create_caption(picture.get("description") or "",
                                        list(picture.get("tags") or []),
                                        settings.get("topic", ""), provider_config,
                                        posting_mode=chosen_group.get("posting_mode") or "anonymous")
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
        if settings.get("anti_duplicate", True) and verdict["blocked"]:
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
            "ctv_user_id": owner_id,
            "key": _job_key(pipeline_id, owner_id, account_id, chosen_group["id"], picture["id"],
                            scheduled_for.strftime("%Y-%m-%d")),
        })
        used_media.add(picture["id"])
    return {"enabled": True, "jobs": jobs, "notes": notes, "day": day,
            "accounts": accounts, "quota": quotas}


def _group_usage_today(conn, group_id: int, day: str, moment=None) -> int:
    """Số bài đã nhắm vào group này trong ngày, tính cả của CTV khác.

    Ngày theo múi giờ của Workspace, nên hai mốc UTC được dùng để so sánh.
    """
    start, end = time_windows.day_bounds_utc(moment or datetime.now().astimezone(), day)
    row = conn.execute("""SELECT COUNT(*) FROM auto_post_jobs
                           WHERE group_id=?
                             AND datetime(COALESCE(scheduled_for, created_at)) >= datetime(?)
                             AND datetime(COALESCE(scheduled_for, created_at)) < datetime(?)
                             AND status IN ('pending','approved','queued','published')""",
                        (int(group_id), start, end)).fetchone()
    return int(row[0] or 0)


# --- allocating --------------------------------------------------------------

def allocate(conn, pipeline_id: int, moment: datetime | None = None, provider_config=None,
             max_jobs: int = 20) -> dict:
    """Tạo job cho mọi cấu hình CTV đang bật trong Pipeline.

    Việc ghi job, giữ chỗ và ghi dấu vết nằm trong cùng một giao dịch, và mỗi
    job có một khóa idempotent, nên hai worker chạy cùng lúc không thể tạo ra
    hai job giống nhau.
    """
    configs = ctv_auto_post.active_configs(conn, pipeline_id)
    if not configs:
        return {"created": [], "skipped": [], "owners": 0}
    config_ids = ctv_auto_post.config_ids_of(conn, pipeline_id)
    created: list[dict] = []
    skipped: list[str] = []
    for settings in configs:
        owner_id = int(settings["ctv_user_id"])
        moment_local = moment or time_windows.now_in(settings.get("timezone")
                                                     or ctv_auto_post.DEFAULT_TIMEZONE)
        day = moment_local.strftime("%Y-%m-%d")
        outcome = plan_for_owner(conn, pipeline_id, settings, moment_local, provider_config)
        skipped.extend(outcome.get("notes", []))
        for job in outcome.get("jobs", [])[:max_jobs]:
            stamp = datetime.now(timezone.utc).isoformat()
            approval = bool(settings.get("approval_required", True))
            try:
                cursor = conn.execute("""INSERT INTO auto_post_jobs
                    (pipeline_id,config_id,ctv_config_id,created_by_ctv_id,media_id,account_id,
                     group_id,caption,posting_mode,nickname,status,needs_approval,
                     idempotency_key,scheduled_for,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(pipeline_id), None, config_ids.get(owner_id), owner_id,
                     job["media_id"], job["account_id"], job["group_id"], job["caption"],
                     job["posting_mode"], job["nickname"],
                     "pending" if approval else "approved", int(approval),
                     job["key"], job["scheduled_for"], stamp, stamp))
            except Exception as error:  # noqa: BLE001 - một job hỏng không dừng cả lượt
                LOG.info("Không tạo được job của CTV %s: %s", owner_id, type(error).__name__)
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
            conn.execute("""INSERT INTO media_usage_history
                            (pipeline_id,media_id,account_id,group_id,job_id,used_at,note)
                            VALUES(?,?,?,?,?,?,?)""",
                         (int(pipeline_id), job["media_id"], job["account_id"], job["group_id"],
                          job_id, stamp, f"tự động đăng CTV {owner_id}"))
            created.append({**job, "id": job_id, "ctv_user_id": owner_id})
    return {"created": created, "skipped": skipped, "owners": len(configs)}


# --- preview -----------------------------------------------------------------

def preview(conn, pipeline_id: int, ctv_user_id: int, moment: datetime | None = None) -> dict:
    """Lịch dự kiến của một CTV, dùng cho giao diện."""
    settings = ctv_auto_post.settings_of(conn, pipeline_id, ctv_user_id)
    if settings is None:
        settings = ctv_auto_post.defaults_for(pipeline_id, ctv_user_id)
    outcome = plan_for_owner(conn, pipeline_id, settings, moment)
    by_account: dict[int, list[dict]] = {}
    for job in outcome.get("jobs", []):
        by_account.setdefault(job["account_id"], []).append(job)
    rows = []
    for account in outcome.get("accounts", []):
        jobs = by_account.get(account["id"], [])
        state = account_limits.account_state(conn, account["id"], settings, moment)
        rows.append({
            "account_id": account["id"], "label": account["label"],
            "daily_limit": state["daily_limit"], "used": state["used"],
            "remaining": state["remaining"], "at_limit": state["at_limit"],
            "paused": state["paused"], "planned": len(jobs),
            "slots": [{"group": job["group_label"], "media_id": job["media_id"],
                       "scheduled_for": job["scheduled_for"]} for job in jobs],
        })
    return {"ok": True, "enabled": bool(settings.get("enabled")),
            "schedule": time_windows.describe(_schedule_of(settings), moment),
            "accounts": rows, "notes": outcome.get("notes", []),
            "reason": outcome.get("reason", ""),
            "expected_jobs": len(outcome.get("jobs", [])),
            "next_allowed": outcome.get("next_allowed")}
