"""Chọn và xác minh danh tính đăng (ẩn danh / biệt danh).

Mô-đun tách biệt logic xác định chế độ đăng khỏi trình duyệt. Không bao giờ
tự chuyển sang tên thật nếu chế độ yêu cầu không khả dụng: trả về lỗi và để
gọi lệnh dừng lại.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from playwright.async_api import Page
from services.facebook_selectors import active_composer_dialog, current_registry
from services import nickname_repository

MODE_ANONYMOUS = "anonymous"
MODE_NICKNAME = "nickname"

# Trạng thái biệt danh Facebook
NICKNAME_PENDING = "pending"      # Đang chờ Facebook xác nhận
NICKNAME_CONFIRMED = "confirmed"  # Đã được Facebook xác nhận
NICKNAME_FAILED = "failed"        # Facebook từ chối hoặc lỗi
NICKNAME_EXPIRED = "expired"      # Biệt danh đã hết hạn/không còn hợp lệ

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

# Pattern để nhận diện biệt danh do Facebook đề xuất
FACEBOOK_SUGGESTED_NICKNAME_RE = re.compile(
    r"^(Leo\d+|[A-Z][a-z]+\d+|User\d+|[A-Z][a-z]+[0-9]{2,})$",
    re.I,
)

def same_identity(left: str, right: str) -> bool:
    """So sánh hai nhãn danh tính không phân hoa thường và khoảng trắng."""
    normalise = lambda value: re.sub(r"\s+", " ", (value or "").strip()).casefold()
    return normalise(left) == normalise(right)

async def get_saved_facebook_nickname(conn, pipeline_id: int, account_id: int, group_id: int) -> Optional[str]:
    return nickname_repository.confirmed(conn, pipeline_id, account_id, group_id)


async def save_facebook_nickname(conn, pipeline_id: int, account_id: int, group_id: int,
                                 nickname: str, status: str = "confirmed") -> None:
    nickname_repository.save(conn, pipeline_id, account_id, group_id, nickname, status)


async def update_nickname_verification(conn, pipeline_id: int, account_id: int, group_id: int,
                                       status: str, nickname: Optional[str] = None) -> None:
    if nickname:
        nickname_repository.save(conn, pipeline_id, account_id, group_id, nickname, status)
    else:
        nickname_repository.update_status(conn, pipeline_id, account_id, group_id, status)


async def get_saved_facebook_nickname_async(pipeline_id: int, account_id: int, group_id: int) -> Optional[str]:
    import sqlite3
    from app import DB_PATH
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=25)
    try:
        return nickname_repository.confirmed(conn, pipeline_id, account_id, group_id)
    finally:
        conn.close()


async def anonymous_mode_available(page: Page) -> bool | None:
    """True khi composer cung cấp chế độ ẩn danh.

    ``None`` có nghĩa không đọc được điều khiển, khác với việc không hỗ trợ.
    """
    for locator in [current_registry().all(page, 'anonymous_switch'),
                    current_registry().all(page, 'anonymous_checkbox'),
                    current_registry().all(page, 'anonymous_button')]:
        try:
            if await locator.count() and await locator.first.is_visible():
                return True
        except Exception:
            continue
    return None


async def dismiss_anonymous_notice(page: Page) -> bool:
    """Continue the information-only anonymous notice, never a Publish button."""
    for dialog in await current_registry().all(page, 'dialogs').all():
        if not await dialog.is_visible() or await current_registry().all(dialog, 'editable').count():
            continue
        title = current_registry().all(dialog, 'anonymous_notice_title')
        if not await title.count():
            continue
        control = await current_registry().resolve(dialog, 'anonymous_notice_continue', timeout=500)
        if control is not None:
            await control.click(timeout=5000)
            return True
    return False


async def anonymous_mode_verified(page: Page) -> bool:
    """Only active composer controls/author labels prove anonymous identity."""
    dialog = await active_composer_dialog(page)
    if dialog is None:
        return False
    for role in ("switch", "checkbox"):
        controls = current_registry().all(dialog, 'anonymous_' + role)
        visible = [c for c in await controls.all() if await c.is_visible()]
        if len(visible) > 1:
            return False
        if visible:
            if await visible[0].get_attribute("aria-checked") == "true":
                return True
            try:
                if await visible[0].is_checked():
                    return True
            except Exception:
                pass
    for label in ("Anonymous member", "Anonymous participant", "Thành viên ẩn danh", "Người tham gia ẩn danh"):
        if await identity_shown(page, label):
            return True
    return False


async def require_anonymous_mode(page: Page) -> bool:
    await dismiss_anonymous_notice(page)
    if await anonymous_mode_verified(page):
        return True
    dialog = await active_composer_dialog(page)
    if dialog is None:
        return False
    for role in ("switch", "checkbox", "button"):
        visible = [c for c in await current_registry().all(dialog, 'anonymous_' + role).all()
                   if await c.is_visible()]
        if len(visible) > 1:
            return False
        if visible:
            await visible[0].click(timeout=5000)
            await dismiss_anonymous_notice(page)
            return await anonymous_mode_verified(page)
    return False


async def open_identity_menu(page: Page) -> Any | None:
    dialog = await active_composer_dialog(page)
    if dialog is None:
        return None
    controls = current_registry().all(dialog, 'identity_control')
    visible = [c for c in await controls.all() if await c.is_visible()]
    if len(visible) != 1:
        return None
    await visible[0].click(timeout=5000)
    menus = [m for m in await current_registry().all(page, 'menus').all() if await m.is_visible()]
    return menus[0] if len(menus) == 1 else None


async def identity_names(menu: Any) -> list[str]:
    """Danh sách danh tính Facebook đang ofer trong menu đã mở."""
    names: list[str] = []
    try:
        for item in await current_registry().all(menu, 'menu_items').all():
            try:
                text = (await item.inner_text()).strip()
                if text:
                    names.append(text)
            except Exception:
                continue
        if not names:
            for item in await current_registry().all(menu, 'menu_items').all():
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
    """Exact visible author label outside caption, options and notice dialogs."""
    dialog = await active_composer_dialog(page)
    if dialog is None or not wanted.strip():
        return False
    for label in await current_registry().text(dialog, wanted).all():
        if await label.is_visible() and await label.evaluate("(el, selector) => !el.closest(selector)", current_registry().css("identity_exclusions")):
            return True
    return False

async def is_facebook_suggested_nickname(name: str) -> bool:
    """Kiểm tra xem tên có phải là biệt danh do Facebook đề xuất (ngẫu nhiên)."""
    # Pattern nhận diện biệt danh ngẫu nhiên do Facebook đề xuất
    # Ví dụ: Leo3363, User123, JohnDoe123, etc.
    return bool(FACEBOOK_SUGGESTED_NICKNAME_RE.match(name.strip()))

async def select_nickname_from_facebook_suggestions(page: Page) -> tuple[str, list[str]]:
    """Compatibility API: inspect options, but never choose an unsolicited identity."""
    menu = await open_identity_menu(page)
    return "", await identity_names(menu) if menu is not None else []


async def save_facebook_nickname_async(pipeline_id: int, account_id: int, group_id: int,
                                       nickname: str, status: str = "confirmed") -> None:
    import sqlite3
    from app import DB_PATH
    with sqlite3.connect(str(DB_PATH), timeout=25) as conn:
        nickname_repository.save(conn, pipeline_id, account_id, group_id, nickname, status)


async def require_nickname_identity(page: Page, pipeline_id: int, account_id: int, group_id: int,
                                     configured_nickname: str) -> tuple[str, list[str], str]:
    """Reuse the confirmed identity; never choose a new random nickname."""
    saved = await get_saved_facebook_nickname_async(pipeline_id, account_id, group_id)
    wanted = saved or configured_nickname.strip()
    if not wanted:
        return "", [], "not_found"
    if await identity_shown(page, wanted):
        if not saved:
            await save_facebook_nickname_async(pipeline_id, account_id, group_id, wanted)
        return wanted, [wanted], "saved" if saved else "configured"
    menu = await open_identity_menu(page)
    if menu is None:
        return "", [], "not_found"
    names = await identity_names(menu)
    if not any(same_identity(name, wanted) for name in names):
        return "", names, "not_found"
    item = current_registry().text(menu, wanted)
    if await item.count() != 1 or not await item.is_visible():
        return "", names, "not_found"
    await item.click(timeout=5000)
    await page.wait_for_timeout(300)
    if not await identity_shown(page, wanted):
        return "", names, "not_found"
    if not saved:
        await save_facebook_nickname_async(pipeline_id, account_id, group_id, wanted)
    return wanted, names, "saved" if saved else "configured"

async def enforce_posting_identity(page: Page, pipeline_id: int, account_id: int, group_id: int,
                                   mode: str, configured_nickname: str) -> tuple[str, str, str]:
    """Buộc composer dùng danh tính được yêu cầu.

    Logic mới:
    - Nếu mode = nickname: Dùng đúng biệt danh đã xác nhận hoặc đã cấu hình
    - Không chọn biệt danh khác khi danh tính yêu cầu không khả dụng
    - KHÔNG dùng tên thật làm thay thế
    - Trả về (danh tính, xác minh, lỗi)
    """
    mode = (mode or "anonymous").strip().lower()
    configured_nickname = (configured_nickname or "").strip()
    
    if mode == MODE_NICKNAME:
        # Thử chọn biệt danh từ Facebook (ưu tiên đã lưu, sau đó Facebook đề xuất)
        try:
            selected, available, status = await require_nickname_identity(page, pipeline_id, account_id, group_id, configured_nickname)
        except nickname_repository.NicknameSchemaRequired:
            return "", "not_verified", "Chế độ biệt danh cần được quản trị viên cập nhật trước khi sử dụng; chưa gửi bài."
        
        if not selected:
            known = ", ".join(available[:3]) if available else "Facebook không hiển thị danh tính nào"
            error_msg = f"Không thể chọn biệt danh trên Facebook ({status}): {known}"
            return "", "not_verified", error_msg
        
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
