"""Chính sách thử lại: chỉ lỗi tạm thời được thử lại, có giới hạn và backoff.

Lỗi vĩnh viễn (checkpoint, phiên hết hạn, quyền không đủ, selectors đã đổi)
không bao giờ được thử lại tự động. Lỗi sau khi đã nhấn nút Đăng nhưng chưa
xác minh kết quả cũng không được thử lại, để tránh đăng trùng.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RetryPolicy:
    """Giới hạn số lần thử và khoảng cách giữa các lần thử.

    ``base_delay_seconds`` là khoảng cách ban đầu, mỗi lần thử sau bị nhân đôi
    (exponential backoff) nhưng không vượt quá ``max_delay_seconds``. Có jitter
    để các tác vụ chạy song song không cùng lúc thử lại.
    """
    max_attempts: int = 3
    base_delay_seconds: float = 0.75
    max_delay_seconds: float = 4.0
    jitter_seconds: float = 0.25


class AmbiguousPublishStateError(RuntimeError):
    """Submit đã có thể xảy ra nhưng thành công chưa được xác nhận an toàn.

    Không bao giờ được retry tự động, vì bài có thể đã đăng rồi.
    """


class TransientError(RuntimeError):
    """Lỗi tạm thời (mạng, gateway, timeout) có thể thử lại."""


class PermanentError(RuntimeError):
    """Lỗi vĩnh viễn (checkpoint, phiên, quyền, selector) không thử lại."""


#: Dấu hiệu chắc chắn không nên thử lại.
NON_RETRYABLE_MARKERS = (
    "captcha", "checkpoint", "permission", "not authorized", "unauthorized",
    "forbidden", "đăng nhập", "login", "quyền đăng", "không tìm thấy",
    "không mở được khung", "không bấm được nút đăng", "không tìm thấy nút đăng",
    "publish control not found", "composer not found",
)

#: Dấu hiệu lỗi mạng tạm thời có thể thử lại.
TRANSIENT_MARKERS = (
    "net::err_", "connection reset", "connection aborted", "connection refused",
    "network changed", "temporarily unavailable", "temporary failure",
    "service unavailable", "bad gateway", "gateway timeout", "timeout",
    "timed out", "quá thời gian",
)


def classify_error(error: Exception) -> str:
    """Phân loại lỗi: ``transient``, ``permanent`` hay ``publish_ambiguous``.

    Kết quả quyết định việc có thử lại hay không.
    """
    if isinstance(error, AmbiguousPublishStateError):
        return "publish_ambiguous"
    if isinstance(error, PermanentError):
        return "permanent"
    if isinstance(error, TransientError):
        return "transient"

    text = str(error).lower()
    if any(marker in text for marker in NON_RETRYABLE_MARKERS):
        return "permanent"
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


def retry_delay_seconds(policy: RetryPolicy, retry_number: int,
                        *, random_value: float | None = None) -> float:
    """Khoảng chờ trước lần thử tiếp theo, có giới hạn và jitter."""
    exponent = max(retry_number - 1, 0)
    base = min(policy.max_delay_seconds,
               policy.base_delay_seconds * (2 ** exponent))
    jitter_source = random.random() if random_value is None else min(
        max(random_value, 0.0), 1.0)
    return min(policy.max_delay_seconds, base + (policy.jitter_seconds * jitter_source))


async def run_with_bounded_retry(operation, *, policy: RetryPolicy | None = None,
                           stop_fn: Callable[[], bool] | None = None,
                           sleep_fn: Callable[[float], None] | None = None,
                           random_fn: Callable[[], float] | None = None,
                           on_retry: Callable[[int, str, float], None] | None = None,
                           should_retry: Callable[[Exception], bool] | None = None):
    """Thử lại chỉ những lỗi được phép, trong số lần cố định.

    ``should_retry`` nếu được cung cấp sẽ thay thế phân loại mặc định. Hàm
    không bao giờ tự retry sau khi đã nhấn nút Đăng (đó là trách nhiệm của
    gọi lệnh: ném ``AmbiguousPublishStateError``).
    """
    active_policy = policy or RetryPolicy()
    attempts = max(1, active_policy.max_attempts)
    sleep_fn = sleep_fn or asyncio.sleep
    random_fn = random_fn or random.random

    for attempt in range(1, attempts + 1):
        if stop_fn is not None and stop_fn():
            raise InterruptedError("Đã dừng theo yêu cầu người dùng")
        try:
            result = operation()
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as error:
            error_class = classify_error(error)
            if should_retry is not None:
                allowed = should_retry(error)
            else:
                allowed = error_class == "transient"
            if not allowed or attempt >= attempts:
                raise
            delay = retry_delay_seconds(active_policy, attempt,
                                        random_value=random_fn())
            if on_retry is not None:
                on_retry(attempt + 1, error_class, delay)
            if stop_fn is not None and stop_fn():
                raise InterruptedError("Đã dừng theo yêu cầu người dùng") from error
            await sleep_fn(delay)

    raise RuntimeError("Vòng lặp retry kết thúc ngoài dự kiến")
