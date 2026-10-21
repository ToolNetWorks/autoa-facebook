"""Trạng thái của một ảnh trong Album.

Sáu trạng thái, không bao giờ gộp làm một:

``UPLOADED``
    Ảnh đã tải lên, chờ hàng đợi kiểm tra.
``ANALYZING``
    Đang được kiểm tra tự động.
``READY``
    Kiểm tra đạt, ảnh tự động dùng được cho lịch đăng.
``NEEDS_REVIEW``
    Có bằng chứng rủi ro, cần người xem xét.
``BLOCKED``
    Vi phạm quy tắc bắt buộc.
``CHECK_FAILED``
    Bộ kiểm tra lỗi, không kết luận được. Ảnh này không phải an toàn và
    cũng không phải vi phạm.

Một ảnh chỉ được dùng cho bài đăng tự động khi nó ``READY``, hoặc khi
chính CTV của Workspace đã xem xét và đánh dấu được dùng. Ảnh ``BLOCKED`` và
``CHECK_FAILED`` không bao giờ được bỏ qua để đăng.
"""
from __future__ import annotations

import json

UPLOADED = "uploaded"
ANALYZING = "analyzing"
READY = "ready"
NEEDS_REVIEW = "needs_review"
BLOCKED = "blocked"
CHECK_FAILED = "check_failed"

STATES = (UPLOADED, ANALYZING, READY, NEEDS_REVIEW, BLOCKED, CHECK_FAILED)

LABEL = {
    UPLOADED: "Đã tải lên, chờ kiểm tra",
    ANALYZING: "Đang kiểm tra",
    READY: "Khả dụng",
    NEEDS_REVIEW: "Cần xem xét",
    BLOCKED: "Bị chặn",
    CHECK_FAILED: "Kiểm tra thất bại",
}

TONE = {
    UPLOADED: "neutral",
    ANALYZING: "info",
    READY: "ok",
    NEEDS_REVIEW: "warn",
    BLOCKED: "bad",
    CHECK_FAILED: "bad",
}

#: The analysis states of the database.
ANALYSIS_QUEUED = "pending"
ANALYSIS_RUNNING = "analyzing"
ANALYSIS_DONE = "done"
ANALYSIS_ERROR = "error"

#: The safety verdicts of the database.
SAFETY_PASS = "pass"
SAFETY_REVIEW = "needs_review"
SAFETY_BLOCKED = "blocked"
SAFETY_UNKNOWN = "unknown"

CATEGORY_TEXT = {
    "violence": "hình ảnh bạo lực",
    "blood": "hình ảnh có máu",
    "sexual": "hình ảnh nhạy cảm",
    "harassment": "nội dung quấy rối",
    "dangerous": "nội dung nguy hiểm",
    "misleading": "hình ảnh gây hiểu nhầm",
    "offensive_text": "chữ gây khó chịu",
    "watermark": "hình ảnh có watermark",
    "low_quality": "hình ảnh chất lượng thấp",
}

ERROR_TEXT = {
    "ProviderError": "Dịch vụ phân tích ảnh không phản hồi.",
    "Chưa cấu hình model phân tích hình ảnh (AI_VISION_MODEL)":
        "Chưa cấu hình model phân tích hình ảnh.",
    "StorageError": "Ảnh không còn tồn tại trên bộ nhớ.",
    "FileNotFoundError": "Không tìm thấy tệp ảnh.",
    "OSError": "Không đọc được tệp ảnh.",
    "TimeoutError": "Dịch vụ phân tích ảnh quá thời gian chờ.",
}


def state_of(analysis_status: str | None, safety_status: str | None) -> str:
    """Trạng thái của một ảnh, từ hai cột của database."""
    analysis = (analysis_status or "").strip().lower()
    safety = (safety_status or "").strip().lower()
    if analysis == ANALYSIS_RUNNING:
        return ANALYZING
    if analysis == ANALYSIS_ERROR:
        return CHECK_FAILED
    if analysis != ANALYSIS_DONE:
        return UPLOADED
    if safety == SAFETY_PASS:
        return READY
    if safety == SAFETY_REVIEW:
        return NEEDS_REVIEW
    if safety == SAFETY_BLOCKED:
        return BLOCKED
    # Một lần kiểm tra đã xong mà không có kết luận là một lần kiểm tra hỏng.
    return CHECK_FAILED


def label_of(state: str) -> str:
    return LABEL.get(state, LABEL[CHECK_FAILED])


def tone_of(state: str) -> str:
    return TONE.get(state, TONE[CHECK_FAILED])


def _safety_of(row) -> dict:
    try:
        safety = json.loads(row.get("safety_json") or "{}")
    except (TypeError, ValueError):
        safety = {}
    return safety if isinstance(safety, dict) else {}


def reason_of(row) -> str:
    """Một câu ngắn giải thích trạng thái, dành cho CTV đọc."""
    state = state_of(row.get("analysis_status"), row.get("safety_status"))
    safety = _safety_of(row)
    note = str(safety.get("note") or "").strip()
    category = str(safety.get("category") or "").strip()
    error = str(safety.get("error") or "").strip()
    if state == CHECK_FAILED:
        if error:
            return ERROR_TEXT.get(error, "Không kiểm tra được ảnh. Bấm “Kiểm tra lại” để thử lại.")
        return "Không kiểm tra được ảnh. Bấm “Kiểm tra lại” để thử lại."
    if state == BLOCKED:
        what = CATEGORY_TEXT.get(category)
        if note:
            return note
        if what:
            return f"Ảnh chứa nội dung bị cấm: {what}."
        return "Ảnh vi phạm quy tắc an toàn, không được dùng để đăng."
    if state == NEEDS_REVIEW:
        if safety.get("duplicate_of"):
            return "Ảnh này đã có trong Album."
        what = CATEGORY_TEXT.get(category)
        if note:
            return note
        if what:
            return f"Ảnh có dấu hiệu cần xem xét: {what}."
        return "Hệ thống không xác định được rủi ro cụ thể, cần bạn xem xét."
    if state == READY:
        return "Kiểm tra đạt, ảnh dùng được cho lịch đăng."
    if state == ANALYZING:
        return "Hệ thống đang kiểm tra ảnh."
    return "Ảnh đã tải lên, hệ thống sẽ kiểm tra trong ít phút."


def can_be_usable(state: str) -> bool:
    """Ảnh chưa có kết luận thì không được dùng để đăng tự động."""
    return state in (READY, NEEDS_REVIEW)


def can_retry(state: str) -> bool:
    """Trạng thái mà CTV có thể bấm “Kiểm tra lại”."""
    return state == CHECK_FAILED


def counts_of(items: list[dict]) -> dict:
    """Bộ đếm theo trạng thái, tách riêng từng trạng thái."""
    counts = {"total": len(items), "usable": 0, "needs_review": 0, "blocked": 0,
              "failed": 0, "waiting": 0, "locked": 0, "saved": 0}
    for item in items:
        state = item.get("state") or state_of(item.get("analysis_status"), item.get("safety_status"))
        if state == READY:
            counts["usable"] += 1
        elif state == NEEDS_REVIEW:
            counts["needs_review"] += 1
        elif state == BLOCKED:
            counts["blocked"] += 1
        elif state == CHECK_FAILED:
            counts["failed"] += 1
        else:
            counts["waiting"] += 1
        if item.get("locked"):
            counts["locked"] += 1
        if item.get("saved"):
            counts["saved"] += 1
    return counts
