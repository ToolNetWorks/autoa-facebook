"""ToolNet pre-submit retry contract. Publication is never a retryable operation."""
from __future__ import annotations

import asyncio
import inspect
import math
import random
import re
from dataclasses import dataclass


class AmbiguousPublishStateError(RuntimeError):
    """An action may have reached Facebook; reconciliation is required."""


class TransientError(RuntimeError):
    pass


class PermanentError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    # Existing public defaults retained for callers; no upstream implementation.
    max_attempts: int = 3
    base_delay_seconds: float = 0.75
    max_delay_seconds: float = 4.0
    jitter_seconds: float = 0.25

    def __post_init__(self):
        if not 1 <= self.max_attempts <= 10:
            raise ValueError('Retry attempts must be between 1 and 10')
        if any(not math.isfinite(v) or v < 0 for v in
               (self.base_delay_seconds, self.max_delay_seconds, self.jitter_seconds)):
            raise ValueError('Retry delays must be finite and nonnegative')


def classify_error(error: Exception) -> str:
    if isinstance(error, AmbiguousPublishStateError):
        return 'publish_ambiguous'
    if isinstance(error, PermanentError):
        return 'permanent'
    message = str(error).casefold()
    if re.search(r'captcha|checkpoint|login|đăng nhập|permission|unauthori[sz]ed|forbidden|'
                 r'quyền đăng|không tìm thấy|không mở được khung|không bấm được nút đăng|'
                 r'(?:publish control|composer) not found', message):
        return 'permanent'
    if isinstance(error, (TransientError, TimeoutError, ConnectionError)) or re.search(
            r'net::err_|connection (?:reset|aborted|refused)|network changed|'
            r'temporar(?:ily unavailable|y failure)|service unavailable|bad gateway|'
            r'timeout|timed out|quá thời gian', message):
        return 'transient'
    return 'unknown'


def retry_delay_seconds(policy: RetryPolicy, retry_number: int, *, random_value=None) -> float:
    noise = random.random() if random_value is None else random_value
    step = min(30, max(0, retry_number - 1))
    delay = math.ldexp(policy.base_delay_seconds, step)
    return min(policy.max_delay_seconds, delay + policy.jitter_seconds * max(0, min(1, noise)))


async def run_with_bounded_retry(operation, *, policy=None, stop_fn=None,
                                 sleep_fn=None, random_fn=None, on_retry=None,
                                 should_retry=None):
    limits = policy or RetryPolicy()
    pause = sleep_fn or asyncio.sleep
    sample = random_fn or random.random
    used = 0
    while used < limits.max_attempts:
        if stop_fn and stop_fn():
            raise InterruptedError('Operation cancelled before next attempt')
        used += 1
        try:
            value = operation()
            return await value if inspect.isawaitable(value) else value
        except Exception as error:
            classification = classify_error(error)
            # Even a caller override cannot authorize replay of an uncertain Submit
            # or bypass a security/permission stop.
            if classification in ('publish_ambiguous', 'permanent') or used == limits.max_attempts:
                raise
            permitted = should_retry(error) if should_retry else classification == 'transient'
            if not permitted:
                raise
            if stop_fn and stop_fn():
                raise InterruptedError('Operation cancelled before retry') from error
            delay = retry_delay_seconds(limits, used, random_value=sample())
            if on_retry:
                on_retry(used + 1, classification, delay)
            await pause(delay)
