"""Lớp tương thích: hạn mức đăng nằm trong :mod:`scheduler.account_limits`.

Tên hàm cũ được giữ nguyên để các module đã có không phải thay đổi.
"""
from scheduler.account_limits import (  # noqa: F401
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MIN_GAP_MINUTES,
    DEFAULT_SETTINGS,
    DEFAULT_TIMEZONE,
    account_state,
    confirm,
    day_of,
    effective_gap,
    effective_limit,
    is_paused,
    last_post_at,
    limits_of,
    now_in,
    pipeline_usage_of,
    reconcile,
    release,
    reserve,
    settings_of,
    usage_of,
    group_usage_of,
)
