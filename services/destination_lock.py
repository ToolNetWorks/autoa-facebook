"""Destination Lock Service: quản lý địa chỉ liên hệ cố định cho từng Pipeline.

Mỗi Pipeline có một địa chỉ liên hệ cố định (username) được khóa trên một nền tảng
(Telegram, WhatsApp, v.v.). AI Caption Service sử dụng service này để gắn dòng cuối
bắt buộc vào caption, đảm bảo không bị mất, bị sửa hoặc tự thay đổi.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, List

if TYPE_CHECKING:
    from services.ai_caption import CaptionOptimization

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List

# Pattern nhận diện username Telegram/Telegram-like
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{3,32}$")

# Các nhãn hiển thị cho Telegram (có thể cấu hình)
DEFAULT_TELEGRAM_LABELS = ["Telegram", "Telegram", "Té Lè", "T3L3"]


@dataclass(frozen=True)
class DestinationLock:
    """Cấu hình khóa địa chỉ cho một Pipeline."""
    pipeline_id: int
    platform: str                    # "telegram", "whatsapp", etc.
    destination_username: str        # Username thực tế (ví dụ: "@hiu9h")
    display_label: str               # Nhãn hiển thị mặc định (ví dụ: "Telegram")
    allowed_labels: List[str]        # Các nhãn được phép dùng ("Telegram", "Telegram", "Té Lè", "T3L3")
    is_locked: bool                  # True = không cho CTV sửa
    is_enabled: bool                 # Bật/tắt khóa

    def get_footer_line(self, label: Optional[str] = None) -> str:
        """Tạo dòng footer chuẩn: 'Label @username'."""
        label = label or self.platform.capitalize()
        return f"{label} {self.destination_username}"

    def format_footer(self, label: Optional[str] = None) -> str:
        """Tạo dòng footer chuẩn để gắn vào caption."""
        label = label or self.platform.capitalize()
        return f"{self.destination_username}"

    def validate_username(self, username: str) -> bool:
        """Kiểm tra username có đúng định dạng."""
        return bool(USERNAME_RE.match(username))

    def normalize_username(self, username: str) -> str:
        """Chuẩn hóa username: đảm bảo có @ prefix."""
        username = username.strip()
        if not username.startswith("@"):
            return f"@{username}"
        return username


@dataclass
class DestinationLockService:
    """Service quản lý Destination Lock cho các Pipeline."""

    def __init__(self, db_path: str = "data/manager.sqlite3"):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_lock(self, pipeline_id: int) -> Optional[dict]:
        """Lấy cấu hình Destination Lock của một Pipeline."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM destination_locks WHERE pipeline_id = ? AND is_enabled = 1",
                (pipeline_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_lock(self, pipeline_id: int, platform: str, destination_username: str,
                    display_label: str = "", allowed_labels: Optional[List[str]] = None,
                    is_locked: bool = True, is_enabled: bool = True, updated_by: Optional[int] = None) -> dict:
        """Tạo Destination Lock cho một Pipeline."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            now = datetime.now(timezone.utc).isoformat()
            labels = allowed_labels or [self.platform_label(platform)]
            conn.execute("""
                INSERT INTO destination_locks
                    (pipeline_id, platform, destination_username, display_label,
                     allowed_labels_json, is_locked, is_enabled, updated_by,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pipeline_id, platform, destination_username, display_label or self.platform_label(platform),
                  json.dumps(labels), int(is_locked), int(is_enabled), updated_by,
                  datetime.now(timezone.utc).isoformat(),
                  datetime.now(timezone.utc).isoformat()))
            conn.commit()
            return self.get_lock(pipeline_id)
        finally:
            conn.close()

    def update_lock(self, pipeline_id: int, **kwargs) -> Optional[dict]:
        """Cập nhật Destination Lock."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            now = datetime.now(timezone.utc).isoformat()
            allowed_fields = {
                "platform", "destination_username", "display_label",
                "allowed_labels_json", "is_locked", "is_enabled", "updated_by"
            }
            updates = []
            params = []
            for key, value in kwargs.items():
                if key in allowed_fields:
                    updates.append(f"{key} = ?")
                    params.append(value)
            if not updates:
                return self.get_lock(pipeline_id)
            params.append(datetime.now(timezone.utc).isoformat())
            params.append(pipeline_id)
            conn.execute(
                f"UPDATE destination_locks SET {', '.join(updates)}, updated_at = ? WHERE pipeline_id = ?",
                params
            )
            conn.commit()
            return self.get_lock(pipeline_id)
        finally:
            conn.close()

    def get_lock_by_id(self, lock_id: int) -> Optional[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM destination_locks WHERE id = ?", (lock_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def delete_lock(self, pipeline_id: int) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM destination_locks WHERE pipeline_id = ?", (pipeline_id,))
            conn.commit()
            return conn.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def platform_label(platform: str) -> str:
        """Nhãn hiển thị mặc định cho nền tảng."""
        labels = {
            "telegram": "Telegram",
            "whatsapp": "WhatsApp",
            "zalo": "Zalo",
            "messenger": "Messenger",
        }
        return labels.get(platform.lower(), platform.capitalize())

    def apply_destination_lock(self, caption: str, pipeline_id: int,
                                preferred_label: Optional[str] = None) -> tuple[str, bool, str]:
        """Gắn Destination Lock vào caption.

        Returns:
            tuple: (caption_with_lock, was_modified, message)
        """
        lock = self.get_lock(pipeline_id)
        if not lock:
            return caption, False, "Không có Destination Lock cho Pipeline này"

        # Chuẩn hóa username
        username = lock["destination_username"].strip()
        if not username.startswith("@"):
            username = f"@{username}"

        # Tạo footer line
        label = lock["display_label"] or "Telegram"
        footer = f"{lock['display_label']} {username}"

        # Tạo footer line
        footer_line = f"{lock['display_label']} @{lock['destination_username'].lstrip('@')}"

        # Chuẩn hóa caption
        caption = caption.rstrip()

        # Kiểm tra xem footer đã có chưa (kiểm tra xem dòng cuối có chứa username không)
        lines = caption.split('\n')
        last_line = lines[-1].strip() if lines else ""

        # Kiểm tra xem username đã có trong dòng cuối chưa
        username_clean = lock["destination_username"].lstrip("@")
        has_footer = username_clean in last_line and lock["display_label"].lower() in last_line.lower()

        if has_footer:
            # Đã có footer đúng -> không thêm
            return caption, False, "Caption đã có Destination Lock"

        # Kiểm tra xem có footer cũ không (các biến thể khác)
        # Nếu có -> loại bỏ footer cũ
        footer_pattern = re.compile(
            r'\n?\s*(Telegram|Telegram|Té Lè|T3L3)\s*@\w+\s*$', re.IGNORECASE
        )
        if footer_pattern.search(caption):
            # Loại bỏ footer cũ
            caption = re.sub(footer_pattern, '', caption).rstrip()

        # Thêm footer mới
        new_caption = f"{caption}\n\n{lock['display_label']} @{lock['destination_username'].lstrip('@')}"

        return new_caption.strip(), True, "Đã gắn Destination Lock"

    def apply_lock_to_caption(self, caption: str, pipeline_id: int,
                               preferred_label: Optional[str] = None) -> tuple[str, bool, str]:
        """Alias cho backward compatibility."""
        return self.apply_destination_lock(caption, pipeline_id, preferred_label)

    def validate_caption_with_lock(self, caption: str, pipeline_id: int) -> tuple[bool, str]:
        """Kiểm tra caption có đúng Destination Lock không."""
        lock = self.get_lock(pipeline_id)
        if not lock:
            return True, "Không có Destination Lock"

        username_clean = lock["destination_username"].lstrip("@")
        lines = caption.strip().split('\n')
        last_line = lines[-1].strip() if lines else ""

        # Kiểm tra chính xác: dòng cuối phải chứa username và label
        # Sử dụng regex để match chính xác pattern "Label @username" ở cuối caption
        expected_footer_pattern = re.compile(
            rf"{re.escape(lock['display_label'])}\s+{re.escape(lock['destination_username'])}\s*$",
            re.IGNORECASE
        )
        if expected_footer_pattern.search(last_line):
            return True, "Footer hợp lệ"

        return False, f"Thiếu Destination Lock: {lock['display_label']} @{lock['destination_username'].lstrip('@')}"