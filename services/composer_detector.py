"""Phát hiện ô soạn thảo (composer) trên nhóm Facebook.

Mô-đun này tách logic nhận diện composer khỏi trình duyệt, giúp fb_browser.py
không chứa quá nhiều selectors của giao diện Facebook. Mọi bộ chọn đều là
best-effort: nếu Facebook đổi giao diện, hàm trả về None và gọi lệnh phải dừng
thay vì đoán hoặc gửi bài vào chỗ sai.
"""
from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page

#: Nhãn của nút mở trình soạn thảo, từ cụ thể đến phổ biến.
COMPOSER_LABELS = (
    "Bạn đang nghĩ gì",
    "Bạn viết gì đi",
    "Viết gì đó",
    "Hãy viết gì đó",
    "Write something",
    "What's on your mind",
    "Tạo bài viết",
    "Create post",
)

COMPOSER_SELECTORS = (
    "div[role='button']:has-text('Bạn đang nghĩ gì')",
    "div[role='button']:has-text('Bạn viết gì đi')",
    "div[role='button']:has-text('Viết gì đó')",
    "div[role='button']:has-text('Hãy viết gì đó')",
    "div[role='button']:has-text('Write something')",
    "div[role='button']:has-text(\"What's on your mind\")",
    "div[aria-label='Tạo bài viết']",
    "div[aria-label='Create post']",
)

EDITOR_SELECTORS = (
    "[contenteditable='true'][role='textbox']",
    "[contenteditable='true']",
)

PUBLISH_LABELS = (
    "Đăng", "Post", "Gửi", "Send", "Đăng bài",
)

PUBLISH_SELECTORS = (
    "span:text-is('Đăng')",
    "span:text-is('Post')",
    "span:text-is('Gửi')",
    "span:text-is('Send')",
    "span:text-is('Đăng bài')",
    "div[aria-label='Đăng']",
    "div[aria-label='Post']",
    "div[aria-label='Gửi']",
    "div[aria-label='Send']",
    "div[role='button']:has-text('Đăng')",
    "div[role='button']:has-text('Post')",
)

MEDIA_LABELS = (
    "Ảnh/video", "Ảnh / Video", "Photo/video", "Photos/videos", "Ảnh",
)

MEDIA_UPLOAD_LABELS = (
    "Đính kèm một ảnh hoặc video",
    "Attach a photo or video",
    "Chọn ảnh",
    "Upload photos?",
    "Chọn từ máy tính",
)


def composer_regex() -> re.Pattern:
    """Biểu thức chính quy nhận nhãn mở trình soạn thảo."""
    return re.compile(
        r"Write something|Viết gì đó|Bạn viết gì|Hãy viết gì đó|"
        r"Bạn đang nghĩ gì|What's on your mind",
        re.I,
    )


async def find_composer(page: Page, timeout: int = 2500) -> Any | None:
    """Tìm nút mở trình soạn thảo trên trang group.

    Trả về locator đầu tiên hiển thị và bấm được, hoặc None nếu không tìm
    thấy. Không bao giờ bấm bên trong hàm này.
    """
    for selector in COMPOSER_SELECTORS:
        try:
            target = page.locator(selector).first
            await target.wait_for(state="visible", timeout=timeout)
            if await target.is_enabled():
                return target
        except Exception:
            continue
    # Fallback: tìm theo vai trò + nhãn khuôn dạng.
    try:
        target = page.get_by_role("button", name=composer_regex()).first
        await target.wait_for(state="visible", timeout=timeout)
        if await target.is_enabled():
            return target
    except Exception:
        pass
    return None


async def find_editor(page: Page, dialog: Any, timeout: int = 7000) -> Any | None:
    """Tìm ô nhập nội dung bên trong hộp thoại composer đang mở."""
    for selector in EDITOR_SELECTORS:
        try:
            target = dialog.locator(selector).first
            await target.wait_for(state="visible", timeout=timeout)
            if await target.is_enabled():
                return target
        except Exception:
            continue
    try:
        target = page.locator(EDITOR_SELECTORS[0]).first
        await target.wait_for(state="visible", timeout=timeout)
        if await target.is_enabled():
            return target
    except Exception:
        pass
    return None


async def find_publish_button(page: Page, dialog: Any, timeout: int = 2500) -> Any | None:
    """Tìm nút đăng/bấmublish trong hộp thoại composer đang mở."""
    for selector in PUBLISH_SELECTORS:
        try:
            target = dialog.locator(selector).first
            await target.wait_for(state="visible", timeout=timeout)
            if await target.is_enabled():
                return target
        except Exception:
            continue
    try:
        target = page.get_by_role("button", name=re.compile(r"^(Đăng|Post|Gửi|Send)$", re.I)).first
        await target.wait_for(state="visible", timeout=timeout)
        if await target.is_enabled():
            return target
    except Exception:
        pass
    return None


async def find_media_button(page: Page, dialog: Any, timeout: int = 5000) -> Any | None:
    """Tìm nút thêm ảnh/video trong hộp thoại composer."""
    for label in MEDIA_LABELS:
        try:
            target = dialog.locator(f'[aria-label="{label}"]').first
            if await target.count() and await target.is_visible():
                return target
        except Exception:
            continue
    try:
        target = page.get_by_role("button", name=re.compile(r"^(Photo|Photos)$", re.I)).first
        if await target.count() and await target.is_visible():
            return target
    except Exception:
        pass
    return None


def publish_label_regex() -> re.Pattern:
    return re.compile(r"^(Đăng|Post|Gửi|Send|Đăng bài)$", re.I)
