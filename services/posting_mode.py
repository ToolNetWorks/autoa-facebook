"""Chọn và xác minh danh tính đăng (ẩn danh / biệt danh).

Mô-đun tách biệt logic xác định chế độ đăng khỏi trình duyệt. Không bao giờ
tự chuyển sang tên thật nếu chế độ yêu cầu không khả dụng: trả về lỗi và để
gọi lệnh dừng lại.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from playwright.async_api import Page

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
    """Lấy biệt danh Facebook đã lưu cho cặp account-group."""
    row = conn.execute("""SELECT facebook_nickname, nickname_status, confirmed_at
                           FROM group_accounts
                           WHERE pipeline_id=? AND account_id=? AND group_id=? 
                           AND nickname_status = 'confirmed'""",
                       (pipeline_id, account_id, group_id)).fetchone()
    if row and row["facebook_nickname"]:
        return row["facebook_nickname"]
    return None

async def save_facebook_nickname(conn, pipeline_id: int, account_id: int, group_id: int,
                                  nickname: str, status: str = "confirmed") -> None:
    """Lưu biệt danh Facebook đã xác nhận."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""UPDATE group_accounts SET
        facebook_nickname=?, nickname_status=?, confirmed_at=?, last_verified_at=?, updated_at=?
        WHERE pipeline_id=? AND account_id=? AND group_id=?""",
        (nickname, status, now, now, now, pipeline_id, account_id, group_id))

async def update_nickname_verification(conn, pipeline_id: int, account_id: int, group_id: int,
                                        status: str, nickname: Optional[str] = None) -> None:
    """Cập nhật trạng thái xác minh biệt danh."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if nickname:
        conn.execute("""UPDATE group_accounts SET
            facebook_nickname=?, nickname_status=?, confirmed_at=?, last_verified_at=?, updated_at=?
            WHERE pipeline_id=? AND account_id=? AND group_id=?""",
            (nickname, status, now, now, now, pipeline_id, account_id, group_id))
    else:
        conn.execute("""UPDATE group_accounts SET
            nickname_status=?, last_verified_at=?, updated_at=?
            WHERE pipeline_id=? AND account_id=? AND group_id=?""",
            (status, now, now, pipeline_id, account_id, group_id))

async def get_saved_facebook_nickname_async(pipeline_id: int, account_id: int, group_id: int) -> Optional[str]:
    """Lấy biệt danh Facebook đã lưu (async version cho fb_browser.py)."""
    import sqlite3
    from app import DB_PATH
    conn = sqlite3.connect(str(DB_PATH), timeout=25)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""SELECT facebook_nickname, nickname_status, confirmed_at
                               FROM group_accounts
                               WHERE pipeline_id=? AND account_id=? AND group_id=? 
                               AND nickname_status = 'confirmed'""",
                           (pipeline_id, account_id, group_id)).fetchone()
        if row and row["facebook_nickname"]:
            return row["facebook_nickname"]
        return None
    finally:
        conn.close()


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
            ok = page.get_by_role("button", name=ANONYMOUS_NOTICE_OK_RE).first
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
            control = page.get_by_role("button", name=ANONYMOUS_RE).first
            if not (await control.count() and await control.is_visible()):
                return False
            if not await _click(control, timeout=5000):
                return False
    await page.wait_for_timeout(600)
    await dismiss_anonymous_notice(page)
    okay = page.get_by_role("button", name=ANONYMOUS_NOTICE_OK_RE).first
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
        control = dialog.get_by_role("button", name=IDENTITY_CONTROL_RE).first
        if not (await control.count() and await control.is_visible()):
            control = None
    except Exception:
        control = None
    if control is None:
        try:
            control = page.get_by_role("button", name=IDENTITY_CONTROL_RE).first
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

async def is_facebook_suggested_nickname(name: str) -> bool:
    """Kiểm tra xem tên có phải là biệt danh do Facebook đề xuất (ngẫu nhiên)."""
    # Pattern nhận diện biệt danh ngẫu nhiên do Facebook đề xuất
    # Ví dụ: Leo3363, User123, JohnDoe123, etc.
    return bool(FACEBOOK_SUGGESTED_NICKNAME_RE.match(name.strip()))

async def select_nickname_from_facebook_suggestions(page: Page) -> tuple[str, list[str]]:
    """Chọn biệt danh từ danh sách Facebook đề xuất. Trả về (tên đã chọn, danh sách tên)."""
    menu = await open_identity_menu(page)
    if menu is None:
        return "", []
    
    names = await identity_names(menu)
    if not names:
        return "", []
    
    # Tìm biệt danh do Facebook đề xuất (thường là tên ngẫu nhiên như Leo3363, User123, etc.)
    suggested = []
    for name in names:
        if await is_facebook_suggested_nickname(name):
            suggested.append(name)
    
    if suggested:
        # Chọn biệt danh đầu tiên được đề xuất
        chosen = suggested[0]
        try:
            item = menu.get_by_text(re.compile(rf"^{re.escape(chosen)}$", re.I)).first
            await item.click(timeout=5000)
            await page.wait_for_timeout(1200)
        except Exception:
            return "", names
        
        if await identity_shown(page, chosen):
            return chosen, names
        return "", names
    
    # Nếu không có biệt danh do Facebook đề xuất, thử tìm biệt danh trong danh sách
    # (có thể là biệt danh đã được Facebook cấp trước đó)
    return "", names

async def save_facebook_nickname_async(pipeline_id: int, account_id: int, group_id: int,
                                        nickname: str, status: str = "confirmed") -> None:
    """Lưu biệt danh Facebook đã xác nhận (async version cho fb_browser.py)."""
    import sqlite3
    from datetime import datetime, timezone
    from app import DB_PATH
    conn = sqlite3.connect(str(DB_PATH), timeout=25)
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""UPDATE group_accounts SET
            facebook_nickname=?, nickname_status=?, confirmed_at=?, last_verified_at=?, updated_at=?
            WHERE pipeline_id=? AND account_id=? AND group_id=?""",
            (nickname, status, now, now, now, pipeline_id, account_id, group_id))
        conn.commit()
    finally:
        conn.close()


async def require_nickname_identity(page: Page, pipeline_id: int, account_id: int, group_id: int,
                                     configured_nickname: str) -> tuple[str, list[str], str]:
    """Chọn biệt danh cho việc đăng bài.

    Logic:
    1. Kiểm tra xem đã có biệt danh Facebook đã lưu trong DB chưa (facebook_nickname + status=confirmed)
    2. Nếu có -> sử dụng biệt danh đã lưu
    3. Nếu chưa có -> mở menu danh tính, tìm biệt danh do Facebook đề xuất
    4. Nếu Facebook đề xuất biệt danh mới -> chấp nhận, lưu vào DB
    4. Nếu không có biệt danh nào -> trả về lỗi
    6. KHÔNG so sánh với nickname đã cấu hình (chỉ dùng làm gợi ý)

    Trả về: (tên đã chọn, danh sách tên có sẵn, trạng thái: "saved"|"facebook_suggested"|"not_found")
    """
    import sqlite3
    from datetime import datetime, timezone
    
    # 1. Kiểm tra biệt danh đã lưu trong DB
    saved_nickname = await get_saved_facebook_nickname_async(pipeline_id, account_id, group_id)
    if saved_nickname:
        # Kiểm tra xem biệt danh đã lưu có trong danh sách hiện tại không
        menu = await open_identity_menu(page)
        if menu is not None:
            names = await identity_names(menu)
            if any(same_identity(name, saved_nickname) for name in names):
                # Biệt danh đã lưu vẫn còn trong danh sách -> sử dụng
                try:
                    item = menu.get_by_text(re.compile(rf"^{re.escape(saved_nickname)}$", re.I)).first
                    await item.click(timeout=5000)
                    await page.wait_for_timeout(1200)
                except Exception:
                    pass
                if await identity_shown(page, saved_nickname):
                    return saved_nickname, names, "saved"
    
    # 2. Không có biệt danh đã lưu hoặc không còn trong danh sách -> tìm biệt danh do Facebook đề xuất
    menu = await open_identity_menu(page)
    if menu is None:
        return "", [], "not_found"
    
    names = await identity_names(menu)
    if not names:
        return "", [], "not_found"
    
    # Tìm biệt danh do Facebook đề xuất (ngẫu nhiên như Leo3363, User123, etc.)
    suggested = []
    for name in names:
        if await is_facebook_suggested_nickname(name):
            suggested.append(name)
    
    if suggested:
        # Facebook đề xuất biệt danh mới -> chấp nhận và lưu
        chosen = suggested[0]
        try:
            item = menu.get_by_text(re.compile(rf"^{re.escape(chosen)}$", re.I)).first
            await item.click(timeout=5000)
            await page.wait_for_timeout(1200)
        except Exception:
            return "", names, "facebook_suggested"
        
        if await identity_shown(page, chosen):
            # Lưu biệt danh mới vào database
            await save_facebook_nickname_async(pipeline_id, account_id, group_id, chosen, "confirmed")
            return chosen, names, "facebook_suggested"
        return "", names, "facebook_suggested"
    
    # Không có biệt danh do Facebook đề xuất
    return "", names, "not_found"

async def enforce_posting_identity(page: Page, pipeline_id: int, account_id: int, group_id: int,
                                   mode: str, configured_nickname: str) -> tuple[str, str, str]:
    """Buộc composer dùng danh tính được yêu cầu.

    Logic mới:
    - Nếu mode = nickname: Tự động chọn biệt danh từ Facebook (ưu tiên đã lưu, sau đó Facebook đề xuất)
    - KHÔNG bắt buộc khớp với configured_nickname
    - KHÔNG dùng tên thật làm thay thế
    - Trả về (danh tính, xác minh, lỗi)
    """
    mode = (mode or "anonymous").strip().lower()
    configured_nickname = (configured_nickname or "").strip()
    
    if mode == MODE_NICKNAME:
        # Thử chọn biệt danh từ Facebook (ưu tiên đã lưu, sau đó Facebook đề xuất)
        selected, available, status = await require_nickname_identity(page, pipeline_id, account_id, group_id, configured_nickname)
        
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
