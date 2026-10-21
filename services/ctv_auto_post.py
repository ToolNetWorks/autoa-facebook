"""Cấu hình tự động đăng group riêng của từng CTV.

Mỗi CTV có một cấu hình riêng, thuộc phạm vi ``pipeline_id`` + ``ctv_user_id``.
Cấu hình chung của Pipeline (``pipeline_auto_post_settings``) vẫn còn nguyên và
được xem là **chính sách chung**: hạn mức mà CTV không được vượt quá, hạn mức
tổng của cả Pipeline và các quy tắc chống trùng lặp.

Ba nguyên tắc được giữ vững ở đây:

* Cấu hình chỉ được ghi khi chính CTV bấm lưu. Một CTV chưa cấu hình thì
  **không** có bản ghi nào trong ``ctv_auto_post_settings``.
* Cấu hình của một CTV không bao giờ được dùng làm mặc định cho CTV khác.
* CTV chỉ dùng được tài nguyên của Pipeline mà tài khoản đó được cấp quyền.

Bảng cũ ``pipeline_auto_post_settings`` và ``auto_post_configs`` không bị đụng
tới, nên một hệ thống đang chạy vẫn giữ nguyên cấu hình chung của mình.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

LOG = logging.getLogger("facebook-pipeline.ctv-auto-post")

TABLE = "ctv_auto_post_settings"

SCOPE_ALL = "all"
SCOPE_SELECTED = "selected"

RULE_ROTATE = "rotate"
RULE_UNUSED_FIRST = "unused_first"
RULE_RANDOM = "random"
IMAGE_RULES = (RULE_ROTATE, RULE_UNUSED_FIRST, RULE_RANDOM)

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_START_TIME = "08:00"
DEFAULT_END_TIME = "20:00"
DEFAULT_MAX_POSTS = 3
DEFAULT_MIN_GAP_MINUTES = 60
DEFAULT_DAYS = [1, 2, 3, 4, 5, 6, 0]
MAX_MAX_POSTS = 50
MAX_MIN_GAP_MINUTES = 1440

DEFAULT_SCHEDULE_RULES = {
    "days_enabled": list(DEFAULT_DAYS),
    "min_gap_minutes": DEFAULT_MIN_GAP_MINUTES,
    "group_daily_limit": 0,
    "image_selection_rule": RULE_ROTATE,
}

DEFAULT_SETTINGS = {
    "pipeline_id": None,
    "ctv_user_id": None,
    "enabled": False,
    "account_scope": SCOPE_ALL,
    "selected_account_ids": [],
    "selected_group_ids": [],
    "topic": "",
    "caption_template": "",
    "max_posts_per_account_daily": DEFAULT_MAX_POSTS,
    "start_time": DEFAULT_START_TIME,
    "end_time": DEFAULT_END_TIME,
    "timezone": DEFAULT_TIMEZONE,
    "schedule_rules": dict(DEFAULT_SCHEDULE_RULES),
    "ai_caption_enabled": True,
    "approval_required": True,
    "anti_duplicate": True,
    "apply_to_new_accounts": True,
}

BOOL_FIELDS = ("enabled", "ai_caption_enabled", "approval_required", "anti_duplicate",
               "apply_to_new_accounts")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- schema ------------------------------------------------------------------

TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    ctv_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    account_scope TEXT NOT NULL DEFAULT '{SCOPE_ALL}',
    selected_account_ids_json TEXT NOT NULL DEFAULT '[]',
    selected_group_ids_json TEXT NOT NULL DEFAULT '[]',
    topic TEXT NOT NULL DEFAULT '',
    caption_template TEXT NOT NULL DEFAULT '',
    max_posts_per_account_daily INTEGER NOT NULL DEFAULT {DEFAULT_MAX_POSTS},
    start_time TEXT NOT NULL DEFAULT '{DEFAULT_START_TIME}',
    end_time TEXT NOT NULL DEFAULT '{DEFAULT_END_TIME}',
    timezone TEXT NOT NULL DEFAULT '{DEFAULT_TIMEZONE}',
    schedule_rules_json TEXT NOT NULL DEFAULT '{{}}',
    ai_caption_enabled INTEGER NOT NULL DEFAULT 1,
    approval_required INTEGER NOT NULL DEFAULT 1,
    anti_duplicate INTEGER NOT NULL DEFAULT 1,
    apply_to_new_accounts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(pipeline_id)
)
"""


def ensure_schema(conn) -> None:
    """Tạo bảng cấu hình riêng của CTV và các cột truy vết job.

    Hàm này chạy lại được nhiều lần và không bao giờ xóa hay ghi đè dữ liệu cũ.
    """
    conn.execute(TABLE_DDL)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_ctv_auto_post_owner "
                 f"ON {TABLE}(pipeline_id, ctv_user_id)")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(auto_post_jobs)")}
    if "created_by_ctv_id" not in columns:
        conn.execute("""ALTER TABLE auto_post_jobs ADD COLUMN created_by_ctv_id INTEGER
                        REFERENCES users(id) ON DELETE SET NULL""")
    if "ctv_config_id" not in columns:
        conn.execute(f"""ALTER TABLE auto_post_jobs ADD COLUMN ctv_config_id INTEGER
                         REFERENCES {TABLE}(id) ON DELETE SET NULL""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_auto_jobs_owner
                    ON auto_post_jobs(pipeline_id, created_by_ctv_id, status)""")
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "created_by_ctv_id" not in job_columns:
        conn.execute("""ALTER TABLE jobs ADD COLUMN created_by_ctv_id INTEGER
                        REFERENCES users(id) ON DELETE SET NULL""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_jobs_owner
                        ON jobs(pipeline_id, created_by_ctv_id)""")


# --- reading -----------------------------------------------------------------

def _decode(value: str | None, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return list(fallback) if isinstance(fallback, list) else dict(fallback)
    if isinstance(fallback, list):
        return parsed if isinstance(parsed, list) else list(fallback)
    return parsed if isinstance(parsed, dict) else dict(fallback)


def _row_to_settings(row) -> dict:
    """Đổi một dòng của database thành cấu hình dùng được trong code."""
    settings = dict(row)
    for field in BOOL_FIELDS:
        settings[field] = bool(settings.get(field))
    settings["selected_account_ids"] = _decode(settings.get("selected_account_ids_json"), [])
    settings["selected_group_ids"] = _decode(settings.get("selected_group_ids_json"), [])
    rules = _decode(settings.get("schedule_rules_json"), {})
    merged_rules = dict(DEFAULT_SCHEDULE_RULES)
    if isinstance(rules, dict):
        merged_rules.update({key: rules[key] for key in rules if key in DEFAULT_SCHEDULE_RULES})
    settings["schedule_rules"] = merged_rules
    for column in ("selected_account_ids_json", "selected_group_ids_json", "schedule_rules_json"):
        settings.pop(column, None)
    return settings


def settings_of(conn, pipeline_id: int, ctv_user_id: int) -> dict | None:
    """Cấu hình tự động đăng của Workspace, hoặc ``None`` nếu chưa lưu lần nào.

    Một Workspace có đúng một CTV, nên Workspace chỉ có một cấu hình. ``ctv_user_id``
    là người đã lưu cấu hình, dùng để truy vết.

    ``None`` và một cấu hình mặc định là hai trạng thái khác nhau: trạng thái
    ``None`` nghĩa là chưa có hành động lưu nào.
    """
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE pipeline_id=?",
                       (int(pipeline_id),)).fetchone()
    if row is None:
        return None
    return _row_to_settings(row)


def defaults_for(pipeline_id: int, ctv_user_id: int) -> dict:
    """Cấu hình mặc định của một CTV, dùng cho lần đầu mở giao diện."""
    return {**DEFAULT_SETTINGS,
            "pipeline_id": int(pipeline_id),
            "ctv_user_id": int(ctv_user_id),
            "schedule_rules": dict(DEFAULT_SCHEDULE_RULES)}


def payload_of(conn, pipeline_id: int, ctv_user_id: int) -> dict:
    """Cấu hình của CTV kèm cờ cho biết đã lưu hay chưa."""
    settings = settings_of(conn, pipeline_id, ctv_user_id)
    if settings is None:
        return {"configured": False, "settings": defaults_for(pipeline_id, ctv_user_id)}
    return {"configured": True, "settings": settings}


def owner_of(conn, pipeline_id: int, ctv_user_id: int) -> dict | None:
    """Thông tin chủ sở hữu của một cấu hình, phục vụ giao diện quản trị."""
    row = conn.execute("""SELECT u.id user_id, u.username, u.role, u.status,
                                  m.status membership_status
                           FROM users u
                           LEFT JOIN pipeline_memberships m
                             ON m.user_id=u.id AND m.pipeline_id=?
                           WHERE u.id=?""", (int(pipeline_id), int(ctv_user_id))).fetchone()
    if row is None:
        return None
    owner = dict(row)
    owner["is_member"] = owner.get("membership_status") == "active"
    return owner


def list_configs(conn, pipeline_id: int) -> list[dict]:
    """Cấu hình tự động đăng của Workspace, dùng cho quản trị.

    Một Workspace có một CTV nên chỉ có một cấu hình. Nếu Workspace chưa lưu
    cấu hình nào thì trả về một bản mặc định, để quản trị thấy rõ.
    """
    rows = conn.execute(f"""SELECT s.*, u.username, u.status user_status
                            FROM {TABLE} s JOIN users u ON u.id=s.ctv_user_id
                            WHERE s.pipeline_id=? ORDER BY u.username""",
                        (int(pipeline_id),)).fetchall()
    if not rows:
        owner = conn.execute("""SELECT u.id user_id, u.username, u.status user_status
                                FROM pipeline_memberships m JOIN users u ON u.id=m.user_id
                                WHERE m.pipeline_id=? AND m.status='active' AND u.role='ctv'
                                ORDER BY u.username LIMIT 1""", (int(pipeline_id),)).fetchone()
        settings = defaults_for(pipeline_id, int(owner["user_id"]) if owner else 0)
        settings["username"] = owner["username"] if owner else ""
        settings["user_status"] = owner["user_status"] if owner else ""
        settings["configured"] = False
        return [settings]
    result = []
    for row in rows:
        settings = _row_to_settings(row)
        settings["username"] = row["username"]
        settings["user_status"] = row["user_status"]
        settings["configured"] = True
        result.append(settings)
    return result


def list_collaborators(conn, pipeline_id: int) -> list[dict]:
    """Các cộng tác viên của Workspace, để quản trị thấy đủ ai có mặt."""
    rows = conn.execute("""SELECT u.id user_id, u.username, u.status user_status
                            FROM pipeline_memberships m JOIN users u ON u.id=m.user_id
                            WHERE m.pipeline_id=? AND m.status='active' AND u.role='ctv'
                            ORDER BY u.username""", (int(pipeline_id),)).fetchall()
    saved = {int(row["ctv_user_id"]) for row in conn.execute(
        f"SELECT ctv_user_id FROM {TABLE} WHERE pipeline_id=?", (int(pipeline_id),)).fetchall()}
    return [{"user_id": int(row["user_id"]), "username": row["username"],
             "user_status": row["user_status"], "configured": int(row["user_id"]) in saved}
            for row in rows]


def active_configs(conn, pipeline_id: int) -> list[dict]:
    """Các cấu hình đang bật, dùng cho bộ xếp lịch."""
    rows = conn.execute(f"SELECT * FROM {TABLE} WHERE pipeline_id=? AND enabled=1",
                        (int(pipeline_id),)).fetchall()
    return [_row_to_settings(row) for row in rows]


def config_ids_of(conn, pipeline_id: int) -> dict[int, int]:
    """Bản đồ ``ctv_user_id`` → ``id`` của bảng cấu hình."""
    rows = conn.execute(f"SELECT id, ctv_user_id FROM {TABLE} WHERE pipeline_id=?",
                        (int(pipeline_id),)).fetchall()
    return {int(row["ctv_user_id"]): int(row["id"]) for row in rows}


# --- writing -----------------------------------------------------------------

def _clean_ids(values) -> list[int]:
    result: list[int] = []
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _clean_clock(value: str, fallback: str) -> str:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return fallback
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def _clean_rules(raw) -> dict:
    rules = dict(DEFAULT_SCHEDULE_RULES)
    if not isinstance(raw, dict):
        return rules
    days = raw.get("days_enabled")
    if isinstance(days, list):
        cleaned = []
        for value in days:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= number <= 7 and number not in cleaned:
                cleaned.append(number)
        if cleaned:
            rules["days_enabled"] = sorted(cleaned)
    try:
        gap = int(raw.get("min_gap_minutes", rules["min_gap_minutes"]))
        rules["min_gap_minutes"] = max(0, min(MAX_MIN_GAP_MINUTES, gap))
    except (TypeError, ValueError):
        pass
    try:
        limit = int(raw.get("group_daily_limit", rules["group_daily_limit"]))
        rules["group_daily_limit"] = max(0, min(MAX_MAX_POSTS, limit))
    except (TypeError, ValueError):
        pass
    rule = str(raw.get("image_selection_rule") or rules["image_selection_rule"])
    if rule in IMAGE_RULES:
        rules["image_selection_rule"] = rule
    return rules


def pipeline_policy(conn, pipeline_id: int) -> dict:
    """Chính sách chung của Pipeline mà cấu hình CTV không được vượt quá."""
    row = conn.execute("""SELECT daily_limit_per_account, min_gap_minutes, pipeline_daily_limit
                           FROM pipeline_auto_post_settings WHERE pipeline_id=?""",
                        (int(pipeline_id),)).fetchone()
    if row is None:
        return {"daily_limit_per_account": DEFAULT_MAX_POSTS,
                "min_gap_minutes": DEFAULT_MIN_GAP_MINUTES,
                "pipeline_daily_limit": 0}
    return {"daily_limit_per_account": int(row["daily_limit_per_account"] or DEFAULT_MAX_POSTS),
            "min_gap_minutes": int(row["min_gap_minutes"] or DEFAULT_MIN_GAP_MINUTES),
            "pipeline_daily_limit": int(row["pipeline_daily_limit"] or 0)}


def account_policy(conn, account_id: int) -> dict:
    """Hạn mức mà quản trị đặt riêng cho một tài khoản Facebook."""
    row = conn.execute("""SELECT daily_limit, min_gap_minutes, group_daily_limit, paused
                           FROM account_posting_limits WHERE account_id=?""",
                        (int(account_id),)).fetchone()
    if row is None:
        return {"daily_limit": None, "min_gap_minutes": None, "group_daily_limit": None,
                "paused": False}
    policy = dict(row)
    policy["paused"] = bool(policy.get("paused"))
    return policy


def clamp_to_policy(conn, pipeline_id: int, settings: dict) -> dict:
    """Giới hạn riêng của CTV không được vượt giới hạn chung của quản trị."""
    policy = pipeline_policy(conn, pipeline_id)
    capped = dict(settings)
    limit = int(capped.get("max_posts_per_account_daily") or DEFAULT_MAX_POSTS)
    limit = max(1, min(MAX_MAX_POSTS, limit))
    rules = dict(capped.get("schedule_rules") or {})
    account_cap = policy["daily_limit_per_account"]
    if account_cap > 0:
        limit = min(limit, account_cap)
    capped["max_posts_per_account_daily"] = limit
    gap = int(rules.get("min_gap_minutes", DEFAULT_MIN_GAP_MINUTES))
    if policy["min_gap_minutes"] > 0:
        gap = max(gap, policy["min_gap_minutes"])
    rules["min_gap_minutes"] = max(0, min(MAX_MIN_GAP_MINUTES, gap))
    capped["schedule_rules"] = rules
    return capped


def save_settings(conn, pipeline_id: int, ctv_user_id: int, data: dict) -> dict:
    """Tạo hoặc cập nhật cấu hình tự động đăng của Workspace.

    Một Workspace có một CTV, nên chỉ có một cấu hình. Người lưu cấu hình
    được ghi lại để truy vết.
    """
    current = settings_of(conn, pipeline_id, ctv_user_id) or defaults_for(pipeline_id, ctv_user_id)
    merged = {**current}
    for key in ("enabled", "topic", "caption_template", "timezone", "start_time", "end_time",
                "ai_caption_enabled", "approval_required", "anti_duplicate",
                "apply_to_new_accounts"):
        if key in data and data[key] is not None:
            merged[key] = data[key]
    if data.get("account_scope") is not None:
        scope = str(data["account_scope"])
        merged["account_scope"] = scope if scope in (SCOPE_ALL, SCOPE_SELECTED) else SCOPE_ALL
    if data.get("selected_account_ids") is not None:
        merged["selected_account_ids"] = _clean_ids(data["selected_account_ids"])
    if data.get("selected_group_ids") is not None:
        merged["selected_group_ids"] = _clean_ids(data["selected_group_ids"])
    if data.get("max_posts_per_account_daily") is not None:
        try:
            merged["max_posts_per_account_daily"] = max(
                1, min(MAX_MAX_POSTS, int(data["max_posts_per_account_daily"])))
        except (TypeError, ValueError):
            pass
    if data.get("schedule_rules") is not None:
        merged["schedule_rules"] = _clean_rules(data["schedule_rules"])
    merged["start_time"] = _clean_clock(merged.get("start_time"), DEFAULT_START_TIME)
    merged["end_time"] = _clean_clock(merged.get("end_time"), DEFAULT_END_TIME)
    for key in BOOL_FIELDS:
        merged[key] = bool(merged.get(key))
    merged = clamp_to_policy(conn, pipeline_id, merged)

    stamp = now_iso()
    conn.execute(f"""INSERT INTO {TABLE}
        (pipeline_id,ctv_user_id,enabled,account_scope,selected_account_ids_json,
         selected_group_ids_json,topic,caption_template,max_posts_per_account_daily,
         start_time,end_time,timezone,schedule_rules_json,ai_caption_enabled,
         approval_required,anti_duplicate,apply_to_new_accounts,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pipeline_id) DO UPDATE SET
            ctv_user_id=excluded.ctv_user_id,
            enabled=excluded.enabled, account_scope=excluded.account_scope,
            selected_account_ids_json=excluded.selected_account_ids_json,
            selected_group_ids_json=excluded.selected_group_ids_json,
            topic=excluded.topic, caption_template=excluded.caption_template,
            max_posts_per_account_daily=excluded.max_posts_per_account_daily,
            start_time=excluded.start_time, end_time=excluded.end_time,
            timezone=excluded.timezone, schedule_rules_json=excluded.schedule_rules_json,
            ai_caption_enabled=excluded.ai_caption_enabled,
            approval_required=excluded.approval_required,
            anti_duplicate=excluded.anti_duplicate,
            apply_to_new_accounts=excluded.apply_to_new_accounts,
            updated_at=excluded.updated_at""",
                 (int(pipeline_id), int(ctv_user_id),
                  int(bool(merged["enabled"])), merged["account_scope"],
                  json.dumps(merged["selected_account_ids"]),
                  json.dumps(merged["selected_group_ids"]),
                  str(merged.get("topic") or ""),
                  str(merged.get("caption_template") or ""),
                  int(merged["max_posts_per_account_daily"]),
                  merged["start_time"], merged["end_time"], merged.get("timezone") or DEFAULT_TIMEZONE,
                  json.dumps(merged["schedule_rules"]),
                  int(bool(merged["ai_caption_enabled"])),
                  int(bool(merged["approval_required"])),
                  int(bool(merged["anti_duplicate"])),
                  int(bool(merged["apply_to_new_accounts"])),
                  stamp, stamp))
    return settings_of(conn, pipeline_id, ctv_user_id) or defaults_for(pipeline_id, ctv_user_id)


def set_enabled(conn, pipeline_id: int, ctv_user_id: int, enabled: bool) -> dict:
    """Bật hoặc tắt hệ thống đăng bài của một CTV."""
    stamp = now_iso()
    cursor = conn.execute(f"""UPDATE {TABLE} SET enabled=?, updated_at=?
                              WHERE pipeline_id=? AND ctv_user_id=?""",
                           (int(bool(enabled)), stamp, int(pipeline_id), int(ctv_user_id)))
    if cursor.rowcount == 0:
        # CTV chưa có cấu hình: tạo một bản ghi tắt để lần sau vẫn nhớ trạng thái.
        save_settings(conn, pipeline_id, ctv_user_id, {"enabled": bool(enabled)})
    return settings_of(conn, pipeline_id, ctv_user_id) or defaults_for(pipeline_id, ctv_user_id)


# --- jobs --------------------------------------------------------------------

def jobs_of(conn, pipeline_id: int, ctv_user_id: int, limit: int = 50) -> list[dict]:
    """Các job do một CTV tạo, theo thứ tự mới nhất."""
    rows = conn.execute("""SELECT j.id, j.pipeline_id, j.account_id, j.group_id, j.media_id,
                                   j.status, j.caption, j.scheduled_for, j.created_at,
                                   j.updated_at, j.idempotency_key, j.created_by_ctv_id,
                                   g.label group_label, a.label account_label
                            FROM auto_post_jobs j
                            LEFT JOIN groups g ON g.id=j.group_id
                            LEFT JOIN accounts a ON a.id=j.account_id
                            WHERE j.pipeline_id=? AND j.created_by_ctv_id=?
                            ORDER BY j.id DESC LIMIT ?""",
                        (int(pipeline_id), int(ctv_user_id), int(limit))).fetchall()
    return [dict(row) for row in rows]


def usage_of(conn, pipeline_id: int, ctv_user_id: int, account_id: int, day: str) -> int:
    """Số bài một CTV đã tạo cho một Facebook trong ngày."""
    row = conn.execute("""SELECT COUNT(*) FROM auto_post_jobs
                           WHERE pipeline_id=? AND created_by_ctv_id=? AND account_id=?
                             AND date(COALESCE(scheduled_for, created_at))=date(?)
                             AND status IN ('pending','approved','queued','published')""",
                        (int(pipeline_id), int(ctv_user_id), int(account_id), day)).fetchone()
    return int(row[0] or 0)
