"""Chọn và xác minh danh tính đăng (ẩn danh / biệt danh).

Mô-đun tách biệt logic xác định chế độ đăng khỏi trình duyệt. Không bao giờ
tự chuyển sang tên thật nếu chế độ yêu cầu không khả dụng: trả về lỗi và để
gọi lệnh dừng lại.
"""
from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page

MODE_ANONYMOUS = "anonymous"
MODE_NICKNAME = "nickname"

ANONYMOUS_RE = re.compile(
    r"Post anonymously|Anonymous post|Anonymous member|Anonymous participant|"
    r"Đăng ẩn danh|Bài viết ẩn danh|Thành viên ẩn danh|Người tham gia ẩn danh",
    re.I,
)
ANONYMOUS_NOTICE_RE = re.compile(r"^(Bài viết ẩn danh|Post anonymously)$", re.I)
ANONYMOUS_NOTICE_OK_RE = re.compile(r"^(OK|Đồng ý|Got it|Đã hiểu|Tôi hiểu)$", re.I)
ANONYMOUS_IDENTITY_RE = re.compile(
    r"^(Anonymous member|Anonymous participant|Thành viên ẩn danh|Người tham gia ẩn danh)$",
    re.I,
)
IDENTITY_CONTROL_RE = re.compile(
    r"Chỉnh sửa hồ sơ|Đổi danh tính|Đăng.*với tư cách|Edit profile|"
    r"Post as|Use another identity|Đăng như", re.I,
)


def same_identity(left: str, right: str) -> bool:
    """So sánh hai nhãn danh tính không phân hoa thường và khoảng trắng."""
    normalise = lambda value: re.sub(r"\s+", " ", (value or "").strip()).casefold()
    return normalise(left) == normalise(right)


async def anonymous_mode_available(page: Page) -> bool | None:
    """True khi composer cung cấp chế độ ẩn danh.

    ``None`` có nghĩa không đọc được điều khiển, khác với việc không hỗ trợ.
    """
    for locator in [page.get_by_role("switch", name=ANONYMOUS_RE),
                    page.get_by_role("checkbox", name=ANONYMOUS_RE),
                    page.get_by_role("button", name=ANONYMOUS_RE)]:
        try:
            if await locator.count() and await locator.first.is_visible():
                return True
        except Exception:
            continue
    return None


async def dismiss_anonymous_notice(page: Page) -> bool:
    """Đóng hộp thoại giải thích về bài viết ẩn danh nếu đang mở."""
    try:
        dialogs = await page.get_by_role("dialog").all()
    except Exception:
        return False
    for dialog in dialogs:
        try:
            title = dialog.get_by_text(ANONYMOUS_NOTICE_RE)
            if not await title.count() or not await title.first.is_visible():
                continue
            ok = await page.get_by_role("button", name=ANONYMOUS_NOTICE_OK_RE).first
            if await ok.count() and await ok.is_visible():
                if await _click(ok, timeout=5000):
                    await page.wait_for_timeout(800)
                    return True
        except Exception:
            continue
    return False


async def anonymous_mode_verified(page: Page) -> bool:
    """Xác minh nghiêm ngặt chế độ ẩn danh đang bật.

    Chỉ chấp nhận điều khiển đã được bật hoặc nhãn tác giả ẩn danh rõ ràng.
    Hộp thoại thông tin không được coi là bằng chứng.
    """
    for locator in [page.get_by_role("switch", name=ANONYMOUS_RE),
                    page.get_by_role("checkbox", name=ANONYMOUS_RE)]:
        try:
            if await locator.count() and await locator.first.is_visible():
                if await locator.first.get_attribute("aria-checked") == "true":
                    return True
                if await locator.first.is_checked():
                    return True
        except Exception:
            pass
    try:
        dialog = page.get_by_role("dialog").last
        identity = dialog.get_by_text(ANONYMOUS_IDENTITY_RE)
        if await identity.count() and await identity.first.is_visible():
            return True
    except Exception:
        pass
    return False


async def require_anonymous_mode(page: Page) -> bool:
    """Bật chế độ ẩn danh nếu cần và xác minh kết quả.

    Không bao giờ coi việc bấm nút là thành công nếu danh tính sau đó chưa
    được xác minh.
    """
    await dismiss_anonymous_notice(page)
    if await anonymous_mode_verified(page):
        return True
    for locator in [page.get_by_role("switch", name=ANONYMOUS_RE),
                    page.get_by_role("checkbox", name=ANONYMOUS_RE)]:
        try:
            if await locator.count() and await locator.first.is_visible():
                if await _click(locator.first, timeout=5000):
                    break
        except Exception:
            pass
    else:
        control = await page.get_by_role("button", name=ANONYMOUS_RE).first
        if not (await control.count() and await control.is_visible()):
            return False
        if not await _click(control, timeout=5000):
            return False
    await page.wait_for_timeout(600)
    await dismiss_anonymous_notice(page)
    okay = await page.get_by_role("button", name=ANONYMOUS_NOTICE_OK_RE).first
    if await okay.count() and await okay.is_visible():
        await _click(okay, timeout=5000)
    await page.wait_for_timeout(800)
    await dismiss_anonymous_notice(page)
    return await anonymous_mode_verified(page)


async def open_identity_menu(page: Page) -> Any | None:
    """Mở điều khiển danh tính của composer. Không bao giờ tạo danh tính mới."""
    dialog = page.get_by_role("dialog").last
    control = None
    try:
        control = await dialog.get_by_role("button", name=IDENTITY_CONTROL_RE).first
        if not (await control.count() and await control.is_visible()):
            control = None
    except Exception:
        control = None
    if control is None:
        try:
            control = await page.get_by_role("button", name=IDENTITY_CONTROL_RE).first
            if not (await control.count() and await control.is_visible()):
                control = None
        except Exception:
            control = None
    if control is None:
        return None
    try:
        await control.click(timeout=5000)
        await page.wait_for_timeout(1200)
    except Exception:
        return None
    menu = page.get_by_role("menu").last
    if await menu.count():
        return menu
    return None


async def identity_names(menu: Any) -> list[str]:
    """Danh sách danh tính Facebook đang ofer trong menu đã mở."""
    names: list[str] = []
    try:
        for item in await menu.get_by_role("menuitem").all():
            try:
                text = (await item.inner_text()).strip()
                if text:
                    names.append(text)
            except Exception:
                continue
        if not names:
            for item in await menu.locator("li, [role=option]").all():
                try:
                    text = (await item.inner_text()).strip()
                    if text:
                        names.append(text)
                except Exception:
                    continue
    except Exception:
        pass
    return [name for name in names if name]


async def identity_shown(page: Page, wanted: str) -> bool:
    """True khi composer hiển thị danh tính mong muốn."""
    dialog = page.get_by_role("dialog").last
    for locator in [dialog.locator(f'[aria-label*="{wanted}" i]'),
                    page.locator(f'[aria-label*="{wanted}" i]')]:
        try:
            if await locator.count():
                return True
        except Exception:
            continue
    try:
        text = await dialog.inner_text(timeout=5000)
        return wanted.casefold() in text.casefold()
    except Exception:
        return False


async def require_nickname_identity(page: Page, nickname: str) -> tuple[str, list[str]]:
    """Chọn biệt danh đã cấu hình. Trả về (tên đã chọn, danh sách tên có sẵn).

    Trả về ("", []) nếu không chọn được. Không bao giờ dùng tên thật làm
    thay thế.
    """
    wanted = (nickname or "").strip()
    if not wanted:
        return "", []
    menu = await open_identity_menu(page)
    if menu is None:
        return "", []
    names = await identity_names(menu)
    chosen = next((name for name in names if same_identity(name, wanted)), "")
    if not chosen:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return "", names
    try:
        item = menu.get_by_text(re.compile(rf"^{re.escape(chosen)}$", re.I)).first
        await item.click(timeout=5000)
        await page.wait_for_timeout(1200)
    except Exception:
        return "", names
    if not await identity_shown(page, chosen):
        return "", names
    return chosen, names


async def enforce_posting_identity(page: Page, mode: str, nickname: str) -> tuple[str, str, str]:
    """Buộc composer dùng chính xác danh tính được yêu cầu.

    Trả về (danh tính, xác minh, lỗi). Danh tính rỗng khi yêu cầu không thể
    hoàn thành; gọi lệnh phải dừng không đăng bài dưới tên thật.
    """
    mode = (mode or "anonymous").strip().lower()
    nickname = (nickname or "").strip()
    if mode == MODE_NICKNAME:
        if not nickname:
            return "", "unverified", ("Chưa cấu hình biệt danh cho group này; không đăng bài")
        selected, available = await require_nickname_identity(page, nickname)
        if not selected:
            known = ", ".join(available[:3]) if available else "Facebook không hiển thị danh tính nào"
            return "", "not_verified", (
                f"Biệt danh đã cấu hình không có trong danh sách danh tính của Facebook ({known}); "
                "không đăng bài dưới tên thật")
        return selected, "verified", ""
    if mode != MODE_ANONYMOUS:
        return "", "unverified", f"Chế độ đăng không hợp lệ '{mode}'; không đăng bài"
    activated = await require_anonymous_mode(page)
    if not activated:
        return "", "not_verified", ("Chế độ ẩn danh không khả năng hoặc không xác minh được; "
                                    "không đăng bài dưới tên thật")
    return "anonymous", "verified", ""


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
