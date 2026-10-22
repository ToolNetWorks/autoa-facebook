"""Tải ảnh lên trình soạn thảo với nhiều chiến lược dự phòng.

Facebook đôi khi đổi giao diện: một lúc nút ảnh mở hộp chọn tệp, một lúc
lại là input ẩn. Module này thử theo thứ tự từ ổn định nhất đến dự phòng,
không bao giờ ép upload khi không tìm thấy điều khiển.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from playwright.async_api import Page


async def upload_media(page: Page, media_paths: Sequence[str | Path],
                       dialog_selector: str = "div[role='dialog']") -> bool:
    """Tải danh sách tệp ảnh vào trình soạn thảo đang mở.

    Trả về True khi ít nhất một tệp đã được chọn thành công. Thử các chiến
    lược sau theo thứ tự:

    1. Nút ảnh trong dialog → ``expect_file_chooser`` → ``set_files``.
    2. Input ẩn ``input[type='file']`` trong dialog → ``set_input_files``.
    3. Nút ảnh trên trang chính → ``expect_file_chooser`` → ``set_files``.
    4. Input ẩn trên trang chính → ``set_input_files``.

    Không bao giờ giả tạo kết quả: nếu không tìm thấy điều khiển nào, trả về
    False và gọi lệnh phải dừng thay vì đăng bài thiếu ảnh.
    """
    paths = [str(p) for p in media_paths if p]
    if not paths:
        return False

    photo_labels = ("Ảnh/video", "Ảnh / Video", "Photo/video", "Photos/videos", "Ảnh")
    upload_re = r"Đính kèm một ảnh hoặc video|Attach a photo or video|Chọn ảnh|Upload photos?|Chọn từ máy tính"

    contexts = [page.locator(dialog_selector), page]

    for ctx in contexts:
        photo_button = None
        for label in photo_labels:
            try:
                candidate = ctx.locator(f'[aria-label="{label}"]').first
                if await candidate.count() and await candidate.is_visible():
                    photo_button = candidate
                    break
            except Exception:
                continue
        if photo_button is None:
            try:
                candidate = ctx.get_by_role("button", name=__import__("re").compile(
                    r"^(Photo|Photos)$", __import__("re").I)).first
                if await candidate.count() and await candidate.is_visible():
                    photo_button = candidate
            except Exception:
                pass

        if photo_button is not None:
            try:
                async with page.expect_file_chooser(timeout=15000) as chooser_info:
                    if await _click(photo_button, timeout=8000):
                        chooser = await chooser_info.value
                        await chooser.set_files(paths)
                        return True
            except Exception:
                pass

        upload = None
        try:
            candidate = ctx.get_by_role("button", name=__import__("re").compile(upload_re, __import__("re").I)).first
            if await candidate.count() and await candidate.is_visible():
                upload = candidate
        except Exception:
            pass
        if upload is None:
            try:
                candidate = ctx.locator(f'[aria-label*="{upload_re}" i]').first
                if await candidate.count() and await candidate.is_visible():
                    upload = candidate
            except Exception:
                pass

        if upload is not None:
            try:
                async with page.expect_file_chooser(timeout=15000) as chooser_info:
                    if await _click(upload, timeout=8000):
                        chooser = await chooser_info.value
                        await chooser.set_files(paths)
                        return True
            except Exception:
                pass

        try:
            hidden = ctx.locator("input[type='file']").first
            if await hidden.count() and await hidden.is_visible():
                await hidden.set_input_files(paths)
                return True
        except Exception:
            pass

    return False


async def _click(element: Any, timeout: int = 10000) -> bool:
    try:
        await element.click(timeout=timeout)
        return True
    except Exception:
        pass
    try:
        await element.click(timeout=timeout, force=True)
        return True
    except Exception:
        return False
