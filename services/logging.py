"""Ghi nhật ký sự kiện có cấu trúc, không lộ thông tin nhạy cảm.

Mỗi lượt chạy (run) có một ``run_id`` duy nhất để truy vết, không dùng để
gắn liên với dữ liệu người dùng hay phiên đăng nhập. Trường nhạy cảm
(cookie, token, session, secret, password...) bị lược bỏ trước khi ghi ra
file JSONL, chứ không chỉ được ẩn trên màn hình.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|token|secret|cookie|authorization|session|storage[_-]?state|profile|access[_-]?token|refresh[_-]?token)",
    re.I,
)
_SENSITIVE_TEXT_PATTERNS = [
    (re.compile(r"(?i)\b(Bearer)\s+[^\s,;]+"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\b(token|password|passwd|cookie|secret|session)\s*[:=]\s*[^\s,;]+"),
     r"\1=[REDACTED]"),
    (re.compile(r"c_user=[^;]+"), "c_user=[REDACTED]"),
    (re.compile(r"xs=[^;]+"), "xs=[REDACTED]"),
    (re.compile(r"datr=[^;]+"), "datr=[REDACTED]"),
]


def new_run_id() -> str:
    """Mã lần chạy duy nhất, không chứa thông tin cá nhân."""
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    """Lược bỏ thông tin nhạy cảm khỏi một giá trị trước khi ghi log."""
    if key and _SENSITIVE_KEY_RE.search(str(key)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_sensitive(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    return value


class JsonlEventLogger:
    """Ghi sự kiện theo định dạng JSONL, mỗi dòng một sự kiện.

    ``path`` là file đích. Ghi nguyên tử bằng tệp tạm rồi ``os.replace`` để
    tránh hỏng file khi quá trình bị gián đoạn giữa lúc ghi.
    """

    def __init__(self, path: str | Path, *, run_id: str | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.run_id = run_id or new_run_id()
        self._closed = False

    def emit(self, event: str, **fields: Any) -> bool:
        if self._closed:
            return False
        payload: dict[str, Any] = {
            "ts": now_iso(),
            "run_id": self.run_id,
            "event": event,
        }
        payload.update(fields)
        safe_payload = redact_sensitive(payload)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(
                f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            with temp_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(safe_payload, ensure_ascii=False,
                                        separators=(",", ":")))
                handle.write("\n")
            os.replace(temp_path, self.path)
            return True
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def close(self) -> None:
        self._closed = True


class RunLogger:
    """Vỏ xung quanh ``JsonlEventLogger`` cho một lần chạy tác vụ."""

    def __init__(self, log_dir: str | Path, run_id: str | None = None,
                 name: str = "run") -> None:
        self.log_dir = Path(log_dir).expanduser().resolve()
        self.run_id = run_id or new_run_id()
        self.logger = JsonlEventLogger(self.log_dir / f"{name}.jsonl",
                                       run_id=self.run_id)

    def emit(self, event: str, **fields: Any) -> bool:
        return self.logger.emit(event, **fields)

    def close(self) -> None:
        self.logger.close()


def default_log_dir(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser().resolve() / "logs"
