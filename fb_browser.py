"""Opt-in browser adapter for accounts personally controlled by the operator.

No CAPTCHA bypass, fingerprint spoofing, credential extraction or auto-retries.
Facebook DOM changes often; every attempted submission is unverified until checked.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from profile_lock import ProfileBusy, ProfileLock
from services import group_links
from services.browser_session import atomic_storage_state
from services.composer_detector import (find_composer, find_editor, find_media_button,
                                        find_publish_button)
from services.engagement_collector import counters_from_text
from services.logging import (JsonlEventLogger, default_log_dir, new_run_id)
from services.media_upload import upload_media
from services.post_verifier import newest_permalink, wait_post_outcome
from services.posting_mode import (enforce_posting_identity, require_anonymous_mode,
                                   require_nickname_identity)
from services.retry_policy import (AmbiguousPublishStateError, RetryPolicy,
                                   classify_error, run_with_bounded_retry)
from services.destination_lock import DestinationLockService

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
PROFILE_DIR = DATA / "profiles"
EVIDENCE_DIR = DATA / "evidence"
SESSIONS_DIR = DATA / "sessions"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


async def context_for(playwright, account_id: int):
    """Open this account's persistent Chromium profile under an exclusive lock.

    The lock is keyed to the account, so a login browser, a cookie import and a
    posting worker can never drive the same profile at the same time.
    """
    lock = ProfileLock(account_id, DATA)
    lock.acquire()
    try:
        profile = PROFILE_DIR / f"account-{account_id}"
        profile.mkdir(parents=True, exist_ok=True)
        profile.chmod(0o700)
        context = await playwright.chromium.launch_persistent_context(
            str(profile), headless=False, viewport={"width": 1280, "height": 850},
            locale="vi-VN", accept_downloads=False,
            args=["--disable-dev-shm-usage", "--no-first-run"],
        )
    except BaseException:
        lock.release()
        raise
    context.fpm_profile_lock = lock
    context.fpm_storage_destination = DATA / "sessions" / f"account-{account_id}.json"
    return context


async def close_context(context):
    """Close a Chromium context and release the profile lock it holds.

    Trước khi đóng, lưu trạng thái trình duyệt (cookie, localStorage) ra
    tệp hồ sơ theo cách nguyên atomic: ghi tệp tạm rồi ``os.replace``, để
    không bao giờ để lại tệp session bị hỏng khi tiến trình bị gián đoạn.
    """
    if context is None:
        return
    lock = getattr(context, "fpm_profile_lock", None)
    destination = getattr(context, "fpm_storage_destination", None)
    try:
        pages = context.pages
        if pages and destination is not None:
            try:
                await pages[0].goto("https://www.facebook.com/",
                                    wait_until="domcontentloaded",
                                    timeout=30000)
            except Exception:
                # Cookie đã được thiết lập trong bộ nhớ; trang lỗi không
                # được làm mất chúng.
                pass
            await atomic_storage_state(context, destination)
    except Exception:
        pass
    finally:
        try:
            await context.close()
        except Exception:
            pass
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


async def import_cookie_session(account_id: int, cookies: list[dict]) -> None:
    """Write supplied cookies into only this account's Chromium persistent profile.

    Chromium keeps cookies that were injected through the protocol in memory
    and only writes them to the profile database once they have been used on
    the domain. The browser therefore has to visit facebook.com before it is
    closed, otherwise the import is lost when the profile is opened again.
    """
    async with async_playwright() as playwright:
        context = await context_for(playwright, account_id)
        try:
            await context.add_cookies(cookies)
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)
            except Exception:
                # The cookies are already set; a page that fails to load must
                # not discard them.
                pass
        finally:
            await close_context(context)


async def manual_login(account_id: int):
    stop_marker = DATA / f"login-stop-{account_id}.flag"
    stop_marker.unlink(missing_ok=True)
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
                print(f"ACCOUNT {account_id}: log in manually in noVNC, then use Close login browser in dashboard", flush=True)
                while not stop_marker.exists():
                    await asyncio.sleep(1)
            finally:
                await close_context(context)
    finally:
        stop_marker.unlink(missing_ok=True)


async def first_visible(page, locators, timeout=2500):
    for locator in locators:
        try:
            await locator.first.wait_for(state="visible", timeout=timeout)
            return locator.first
        except Exception:
            pass
    return None


def result(status: str, details: str, evidence: str = "", identity: str = "",
           verification: str = "", permalink: str = ""):
    return {"state": status, "details": details, "evidence": evidence,
            "identity": identity, "verification": verification,
            "permalink": permalink}


LOGIN_TEXT = re.compile(r"^(Log into Facebook|Log In|Đăng nhập vào Facebook|Đăng nhập Facebook|Đăng nhập)$", re.I)
CHECKPOINT_URL = ("/checkpoint", "two_step_verification", "/recovery", "login/device", "checkpoint/")
PROFILE_BUTTON = re.compile(
    r"Your profile|Your profile picture|Tài khoản của bạn|Ảnh đại diện của bạn|Hồ sơ của bạn|Your account", re.I)


def safe_avatar(url: str) -> str:
    """Accept only plain https image URLs; never render a javascript: source."""
    if not url:
        return ""
    if not url.lower().startswith("https://"):
        return ""
    return url[:2000]


async def facebook_identity(page, uid: str) -> tuple[str, str]:
    """Best-effort Facebook display name and avatar. Never raises."""
    name, avatar = "", ""
    try:
        button = page.get_by_role("button", name=PROFILE_BUTTON).first
        if await button.count() and await button.is_visible():
            label = (await button.get_attribute("aria-label")) or (await button.inner_text())
            if label:
                name = label.strip()[:120]
            image = button.locator("img").first
            if await image.count():
                avatar = safe_avatar(await image.get_attribute("src") or "")
    except Exception:
        pass
    if not name or not avatar:
        try:
            await page.goto(f"https://www.facebook.com/profile.php?id={uid}",
                            wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            heading = page.locator("h1").first
            if await heading.count():
                text = (await heading.inner_text()).strip()
                if text and text.lower() not in ("facebook", "trang chủ", "home"):
                    name = name or text[:120]
            picture = page.locator("image[role=img], [data-testid='profile-picture'] img, img[alt*='profile' i], img[alt*='ảo' i]").first
            if await picture.count():
                avatar = avatar or safe_avatar(await picture.get_attribute("src") or "")
        except Exception:
            pass
    if not name:
        try:
            title = (await page.title()).strip()
            if title and "Facebook" not in title:
                name = title[:120]
        except Exception:
            pass
    return name, avatar


async def check_session(account_id: int) -> dict:
    """Report whether this account's stored browser profile is still usable.

    Returns one of: connected, expired, checkpoint, busy, error, unknown.
    Never attempts to solve a CAPTCHA, a checkpoint or a 2FA challenge.
    """
    outcome = {"status": "unknown", "name": "", "uid": "", "avatar": "", "details": ""}
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)
                url = page.url.lower()
                if any(marker in url for marker in CHECKPOINT_URL):
                    outcome.update(status="checkpoint",
                                   details="Facebook yêu cầu xác minh bảo mật (checkpoint hoặc 2FA). "
                                           "Hãy dùng 'Đăng nhập lại' và tự xử lý trên trình duyệt.")
                    return outcome
                if any(marker in url for marker in ("/login", "/logout")):
                    outcome.update(status="expired",
                                   details="Hồ sơ trình duyệt chưa đăng nhập hoặc phiên đã hết hạn. "
                                           "Hãy tải cookie JSON mới hoặc đăng nhập lại.")
                    return outcome
                try:
                    login_form = page.get_by_text(LOGIN_TEXT).first
                    if await login_form.count() and await login_form.is_visible():
                        outcome.update(status="expired",
                                       details="Facebook hiển thị màn hình đăng nhập; phiên không còn hiệu lực.")
                        return outcome
                except Exception:
                    pass
                uid = ""
                try:
                    for cookie in await context.cookies("https://www.facebook.com"):
                        domain = str(cookie.get("domain", "")).lstrip(".").lower()
                        value = str(cookie.get("value", ""))
                        if cookie.get("name") == "c_user" and domain == "facebook.com" and value.isdigit():
                            uid = value
                            break
                except Exception:
                    pass
                if not uid:
                    outcome.update(status="expired",
                                   details="Không tìm thấy cookie phiên (c_user) trong hồ sơ trình duyệt. "
                                           "Hãy tải cookie JSON mới hoặc đăng nhập lại.")
                    return outcome
                name, avatar = await facebook_identity(page, uid)
                outcome.update(status="connected", uid=uid, name=name, avatar=avatar,
                               details="Phiên đăng nhập còn hiệu lực trên hồ sơ trình duyệt.")
                return outcome
            finally:
                await close_context(context)
    except ProfileBusy as error:
        outcome.update(status="busy", details=str(error))
    except Exception as error:
        outcome.update(status="error", details=f"Lỗi kiểm tra trình duyệt: {type(error).__name__}")
    return outcome


ANONYMOUS_RE = re.compile(r"Post anonymously|Anonymous post|Anonymous member|Anonymous participant|Đăng ẩn danh|Bài viết ẩn danh|Thành viên ẩn danh|Người tham gia ẩn danh", re.I)


#: Facebook shows an information dialog when the anonymous identity is turned
#: on. It has to be closed before the identity of the composer can be read.
ANONYMOUS_NOTICE_RE = re.compile(r"^(Bài viết ẩn danh|Post anonymously)$", re.I)
ANONYMOUS_NOTICE_OK_RE = re.compile(r"^(OK|Đồng ý|Got it|Đã hiểu|Tôi hiểu)$", re.I)
#: The identity a composer shows when the post goes out anonymously. Only the
#: author badge counts: the label "Bài viết ẩn danh" also appears on a
#: control that is present while the account is still posting under its real
#: name, so it is not proof of anything.
ANONYMOUS_IDENTITY_RE = re.compile(
    r"^(Anonymous member|Anonymous participant|Thành viên ẩn danh|Người tham gia ẩn danh)$", re.I)


async def dismiss_anonymous_notice(page) -> bool:
    """Đóng hộp thoại giải thích về bài viết ẩn danh nếu nó đang mở.

    Facebook mở hộp thoại này ngay khi bật chế độ ẩn danh. Hộp thoại nằm
    bên trong composer nên không phải lúc nào nó cũng là hộp thoại cuối cùng,
    vì vậy mọi hộp thoại đều được dò.
    """
    try:
        dialogs = await page.get_by_role("dialog").all()
    except Exception:
        return False
    for dialog in dialogs:
        try:
            title = dialog.get_by_text(ANONYMOUS_NOTICE_RE)
            if not await title.count() or not await title.first.is_visible():
                continue
            button = await first_visible(page, [
                dialog.get_by_role("button", name=ANONYMOUS_NOTICE_OK_RE),
            ], timeout=1500)
            if button is None:
                button = await first_visible(page, [
                    page.get_by_role("button", name=ANONYMOUS_NOTICE_OK_RE),
                ], timeout=1500)
            if button is None:
                continue
            if not await click_element(button, timeout=5000):
                continue
            await page.wait_for_timeout(800)
            return True
        except Exception:
            continue
    return False


async def click_element(element, timeout: int = 10000) -> bool:
    """Bấm một phần tử, ép bấm khi có lớp phủ của Facebook chắn.

    Facebook hay đặt một lớp phủ vô hình lên nút. Phần tử đã được kiểm tra là
    hiện ra và bật, nên bấm trực tiếp vào nó vẫn đúng ý định.
    """
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


async def anonymous_mode_verified(page) -> bool:
    """Strictly verify a checked toggle or an active anonymous composer identity."""
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
    # An information dialog headed "Post anonymously" is not proof the
    # author identity has changed. Only accept a checked control or an
    # explicit anonymous AUTHOR badge in the active composer.
    try:
        dialog = page.get_by_role("dialog").last
        identity = dialog.get_by_text(ANONYMOUS_IDENTITY_RE)
        if await identity.count() and await identity.first.is_visible():
            return True
    except Exception:
        pass
    return False


async def require_anonymous_mode(page) -> bool:
    await dismiss_anonymous_notice(page)
    if await anonymous_mode_verified(page):
        return True
    for locator in [page.get_by_role("switch", name=ANONYMOUS_RE),
                     page.get_by_role("checkbox", name=ANONYMOUS_RE)]:
        try:
            if await locator.count() and await locator.first.is_visible():
                await click_element(locator.first, timeout=5000)
                break
        except Exception:
            pass
    else:
        # Some Facebook UIs expose a text button instead of a switch.
        # Never trust the click alone: verify the resulting composer separately.
        control = await first_visible(page, [
            page.get_by_role("button", name=ANONYMOUS_RE),
            page.get_by_text(re.compile(r"^Post anonymously$|^Đăng ẩn danh$", re.I)),
        ], timeout=1000)
        if control is None:
            return False
        await click_element(control, timeout=5000)
    # Bật chế độ ẩn danh sẽ mở hộp thoại giải thích; đóng nó trước khi
    # đọc danh tính của composer.
    await page.wait_for_timeout(600)
    await dismiss_anonymous_notice(page)
    okay = await first_visible(page, [
        page.get_by_role("button", name=re.compile(r"^(Got it|Đã hiểu|Tôi hiểu)$", re.I)),
    ], timeout=1000)
    if okay:
        await click_element(okay, timeout=5000)
    await page.wait_for_timeout(800)
    await dismiss_anonymous_notice(page)
    return await anonymous_mode_verified(page)


#: Nút thêm ảnh hoặc video trong composer. Facebook tiếng Việt dùng nhãn
#: "Đính kèm một ảnh hoặc video", tiếng Anh dùng "Attach a photo or video".
#: Nhãn của nút thêm ảnh. Dấu "/" không dùng được trong biểu thức chọn
#: của Playwright, nên nhãn tiếng Việt được tra bằng bộ chọn thuộc tính.
PHOTO_BUTTON_LABELS = ("Ảnh/video", "Ảnh / Video", "Photo/video", "Photos/videos", "Ảnh")

#: Nút đăng bài. Facebook dùng cả thẻ ``button`` lẫn ``div`` có
#: ``role="button"``, nhãn nằm trong một ``span`` đơn giản, và khi đăng ẩn danh
#: nút lại là **Gửi** chứ không phải **Đăng**. Các lựa chọn đặt từ cụ thể nhất
#: đến rộng nhất.
SUBMIT_BUTTON_RE = re.compile(r"^(Post|Đăng|Đăng bài|Gửi|Send)$", re.I)
SUBMIT_BUTTON_SELECTORS = (
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
PHOTO_BUTTON_RE = re.compile(r"^(Photo|Photos)", re.I)
#: Nút tải ảnh lên máy, một số giao diện dùng nhãn này thay cho nút trên.
PHOTO_UPLOAD_RE = re.compile(
    r"Đính kèm một ảnh hoặc video|Attach a photo or video|"
    r"Chọn ảnh|Upload photos?|Chọn từ máy tính", re.I)

GROUP_JOIN_RE = re.compile(r"^(Join Group|Tham gia nhóm|Tham gia)$", re.I)
#: Notices that Facebook shows after a join request was sent.
GROUP_JOIN_PENDING_RE = re.compile(
    r"your request to join|request sent|joining request|awaiting approval|"
    r"đã gửi yêu cầu tham gia|yêu cầu tham gia đã được gửi|đang chờ.*duyệt|"
    r"chờ quản trị viên.*duyệt|pending approval", re.I)
#: Notices that a join request was refused.
GROUP_JOIN_REJECTED_RE = re.compile(
    r"you (?:can'?t|could ?not) join|not allowed to join|join request (?:was )?declined|"
    r"bị từ chối|không được phép tham gia", re.I)
#: The question a group asks before letting someone in.
GROUP_JOIN_QUESTION_RE = re.compile(
    r"answer.{0,20}question|câu hỏi|question(s)?\s*\(?required|trả lời", re.I)
GROUP_COMPOSER_RE = re.compile(r"Write something|Viết gì đó|Bạn viết gì|Hãy viết gì đó|Viết bài", re.I)
GROUP_PENDING_RE = re.compile(r"pending approval|awaiting approval|chờ duyệt|chờ phê duyệt|"
                              r"đang chờ.*duyệt|your post.*review", re.I)
IDENTITY_CONTROL_RE = re.compile(r"Chỉnh sửa hồ sơ|Đổi danh tính|Đăng.*với tư cách|Edit profile|"
                                 r"Post as|Use another identity|Đăng như", re.I)
GROUP_NO_ACCESS_RE = re.compile(
    r"không xem được nội dung này|không thể xem nội dung này|nội dung này không khả dụng|"
    r"chỉ chia sẻ nội dung với một nhóm nhỏ|"
    r"you.{0,3}re not able to view this content|you currently can.{0,3}t view this content|"
    r"this content isn.{0,3}t available", re.I)
# A notice that says the account may not post here. This is the only browser
# evidence that counts as a real restriction; a missing composer is not.
GROUP_CANNOT_POST_RE = re.compile(
    r"bạn không thể đăng bài trong nhóm này|không thể đăng bài trong nhóm này|"
    r"chỉ quản trị viên được đăng bài|chỉ quản trị viên nhóm mới được đăng|"
    r"bạn không có quyền đăng bài trong nhóm này|đăng bài trong nhóm này đã bị tắt|"
    r"you can.{0,3}t post in this group|only admins can post|"
    r"posting.{0,3}s been turned off|you.{0,3}re not allowed to post", re.I)


def same_identity(left: str, right: str) -> bool:
    """Compare two identity labels without case and spacing differences."""
    normalise = lambda value: re.sub(r"\s+", " ", (value or "").strip()).casefold()
    return normalise(left) == normalise(right)


async def anonymous_mode_available(page) -> bool | None:
    """True when the composer offers the anonymous identity.

    ``None`` means the identity control could not be read at all, which is
    not the same as the identity being unavailable.
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


async def open_identity_menu(page):
    """Open the identity control of the composer. Never creates an identity."""
    dialog = page.get_by_role("dialog").last
    control = await first_visible(page, [
        dialog.get_by_role("button", name=IDENTITY_CONTROL_RE),
        page.get_by_role("button", name=IDENTITY_CONTROL_RE),
        dialog.locator('[aria-label*="tư cách" i], [aria-label*="identity" i], '
                       '[aria-label*="hồ sơ" i], [aria-label*="profile" i]'),
    ], timeout=1500)
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


async def identity_names(menu) -> list[str]:
    """The identities Facebook offers in the open menu."""
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


async def identity_shown(page, wanted: str) -> bool:
    """True when the composer shows the expected identity."""
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


async def check_group(data: dict) -> dict:
    """Check what Facebook allows this account to do in a group.

    Nothing is ever posted here and no challenge is bypassed. Every answer
    that cannot be confirmed is reported as unknown.

    ``accessible``, ``is_member`` and ``can_post`` are three valued: ``True``
    when the browser saw it, ``False`` only when the browser saw a clear sign
    of the opposite, and ``None`` when there was not enough to tell. A missing
    selector, a timeout or a browser error is never a sign that Facebook
    restricts the account.

    Chưa tham gia group không mặc định nghĩa không được đăng: một số group
    công khai cho phépVisitor Posting (viết trên tường group mà không phải
    thành viên). Nếu thấy nút tham gia nhưng vẫn tìm thấy ô soạn bài, hệ thống
    coi như có thể đăng (visitor posting), chứ không chặn ngay lập tức.
    """
    account_id = int(data["account_id"])
    url = str(data["group_url"])
    wanted_nickname = str(data.get("nickname") or "").strip()
    outcome = {
        "status": "unknown", "logged_in": False, "accessible": None, "is_member": None,
        "can_post": None, "supports_anonymous": None, "supports_nickname": None,
        "nickname_available": None, "requires_approval": False, "group_name": "",
        "resolved_url": "", "details": "", "evidence": "",
        "visitor_posting": None,
    }
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                # Chuẩn hóa URL group trước khi mở.
                try:
                    canonical_url, _ = group_links.normalize_group_url(url)
                    url = canonical_url
                except Exception:
                    pass
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=65000)
                except Exception:
                    outcome.update(status="error", accessible=None, is_member=None, can_post=None,
                                   details="Không mở được trang group; chưa biết tài khoản có bị "
                                           "hạn chế hay không.")
                    return outcome
                await page.wait_for_timeout(4000)
                if any(part in page.url for part in ("/login", "/checkpoint", "/two_step_verification",
                                                    "/recover", "/recoveraccount")):
                    outcome.update(status="error",
                                   details="Facebook yêu cầu đăng nhập hoặc xác minh; không kiểm tra được group.")
                    return outcome
                outcome["logged_in"] = True
                if "/groups/" not in page.url:
                    outcome.update(status="not_accessible", accessible=False, is_member=None,
                                   can_post=None,
                                   details="Facebook không mở trang group; chưa xác định được quyền "
                                           "đăng trong group này.")
                    return outcome
                outcome["accessible"] = True
                try:
                    resolved = str(page.url or "")
                    if "/groups/" in resolved:
                        outcome["resolved_url"] = resolved
                except Exception:
                    pass
                try:
                    body = (await page.locator("body").inner_text(timeout=10000))[:8000]
                except Exception:
                    body = ""
                try:
                    heading = page.locator("h1").first
                    if await heading.count():
                        outcome["group_name"] = (await heading.inner_text()).strip()[:200]
                except Exception:
                    pass
                if GROUP_NO_ACCESS_RE.search(body):
                    outcome.update(status="not_accessible", accessible=False, is_member=None,
                                   can_post=None,
                                   details="Tài khoản không xem được nội dung của group; group có thể là "
                                           "riêng tư, đã thay đổi người được xem hoặc nội dung đã bị xóa. "
                                           "Đây không phải dấu hiệu Facebook hạn chế đăng bài.")
                    return outcome
                if GROUP_CANNOT_POST_RE.search(body):
                    outcome.update(status="restricted", accessible=True, is_member=None,
                                   can_post=False,
                                   details="Facebook thông báo tài khoản không được đăng bài trong "
                                           "group này.")
                    return outcome
                outcome["requires_approval"] = bool(GROUP_PENDING_RE.search(body))
                join = await first_visible(page, [
                    page.get_by_role("button", name=GROUP_JOIN_RE),
                    page.get_by_text(GROUP_JOIN_RE),
                ], timeout=1500)
                composer = await first_visible(page, [
                    page.get_by_text(GROUP_COMPOSER_RE),
                    page.get_by_role("button", name=GROUP_COMPOSER_RE),
                ], timeout=1500)
                if join is not None:
                    # Có nút tham gia: chưa chắc là không được đăng. Một số group
                    # công khai hỗ trợ Visitor Posting, cho phép viết bài ngay
                    # trên tường group trước khi tham gia. Chỉ chặn khi composer
                    # cũng không có sẵn.
                    if composer is not None:
                        outcome["visitor_posting"] = True
                        outcome["is_member"] = False
                        outcome["can_post"] = True
                        outcome["status"] = "ok"
                        outcome["details"] = ("Chưa tham gia group nhưng ô soạn bài có sẵn: "
                                              "Facebook cho phép viết bài công khai (Visitor Posting).")
                        # Không mở composer ở đây để không thay đổi trạng thái;
                        # kiểm tra chế độ đăng sẽ được thực hiện khi đăng thật.
                        return outcome
                    outcome.update(status="not_member", is_member=False, can_post=None,
                                   details="Tài khoản chưa tham gia group này và không thấy ô soạn bài; "
                                           "không thể đăng mà không tham gia.")
                    return outcome
                if composer is None:
                    # Không tìm thấy ô soạn bài không có nghĩa là bị hạn chế:
                    # có thể Facebook đã đổi giao diện, hoặc tài khoản bị chặn
                    # ở một bước nào đó mà trình duyệt không nhìn thấy.
                    outcome.update(status="needs_review", is_member=None, can_post=None,
                                   details="Không thấy ô soạn bài; chưa xác minh được quyền đăng bài.")
                    return outcome
                outcome["is_member"] = True
                try:
                    await composer.click(timeout=10000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    outcome.update(status="needs_review", can_post=None,
                                   details="Không mở được ô soạn bài; chưa xác minh được chế độ đăng.")
                    return outcome
                outcome["can_post"] = True
                outcome["supports_anonymous"] = await anonymous_mode_available(page)
                menu = await open_identity_menu(page)
                if menu is not None:
                    names = await identity_names(menu)
                    outcome["supports_nickname"] = bool(names)
                    if wanted_nickname:
                        outcome["nickname_available"] = any(same_identity(name, wanted_nickname)
                                                           for name in names)
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                else:
                    # Không đọc được danh sách danh tính: chưa biết group có hỗ trợ
                    # biệt danh hay không.
                    outcome["supports_nickname"] = None
                    outcome["nickname_available"] = None
                if wanted_nickname and outcome["nickname_available"] is False:
                    outcome.update(status="needs_review",
                                   details="Không thấy biệt danh đã cấu hình trong danh sách danh tính của Facebook.")
                    return outcome
                outcome["status"] = "ok"
                outcome["details"] = "Đã kiểm tra được quyền đăng của group."
                return outcome
            finally:
                await close_context(context)
    except ProfileBusy as error:
        outcome.update(status="busy", details=str(error))
    except Exception as error:
        outcome.update(status="error", details=f"Lỗi kiểm tra trình duyệt: {type(error).__name__}")
    return outcome


async def join_group(data: dict) -> dict:
    """Join a group when Facebook allows it.

    Nothing is bypassed here. A challenge, a checkpoint or a captcha stops the
    action and asks the collaborator to handle it by hand. When the group asks
    questions before joining, the questions are returned unanswered: the
    system never invents an answer.
    """
    account_id = int(data["account_id"])
    url = str(data["group_url"])
    answers = data.get("answers") or {}
    outcome = {"state": "unknown", "status": "unknown", "details": "", "questions": [],
               "evidence": "", "membership": "unknown"}
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=65000)
                await page.wait_for_timeout(4000)
                if any(part in page.url for part in ("/login", "/checkpoint", "/two_step_verification",
                                                    "/recover", "/recoveraccount")):
                    outcome.update(state="failed", status="checkpoint",
                                   details="Facebook yêu cầu đăng nhập hoặc xác minh; cần bạn xử lý.")
                    return outcome
                if "/groups/" not in page.url:
                    outcome.update(state="failed", status="no_access",
                                   details="Facebook không mở trang group; không thể tham gia.")
                    return outcome
                try:
                    body = (await page.locator("body").inner_text(timeout=10000))[:8000]
                except Exception:
                    body = ""
                if GROUP_JOIN_REJECTED_RE.search(body):
                    outcome.update(state="failed", status="rejected",
                                   details="Facebook nói tài khoản không được phép tham gia group này.")
                    return outcome
                if GROUP_JOIN_PENDING_RE.search(body):
                    outcome.update(state="ok", status="pending_approval", membership="pending_approval",
                                   details="Yêu cầu tham gia đã được gửi và đang chờ group duyệt.")
                    return outcome
                join = await first_visible(page, [
                    page.get_by_role("button", name=GROUP_JOIN_RE),
                    page.get_by_text(GROUP_JOIN_RE),
                ], timeout=1500)
                if join is None:
                    # Không có nút tham gia: đã là thành viên, hoặc giao diện đã đổi.
                    composer = await first_visible(page, [
                        page.get_by_text(GROUP_COMPOSER_RE),
                        page.get_by_role("button", name=GROUP_COMPOSER_RE),
                    ], timeout=1500)
                    if composer is not None:
                        outcome.update(state="ok", status="joined", membership="joined",
                                       details="Tài khoản đã là thành viên group.")
                    else:
                        outcome.update(state="failed", status="unknown",
                                       details="Không thấy nút tham gia cũng không thấy ô soạn bài; "
                                               "chưa xác định được tư cách thành viên.")
                    return outcome
                await join.click(timeout=10000)
                await page.wait_for_timeout(2000)
                questions = await join_questions(page)
                if questions and not answers:
                    outcome.update(state="needs_answers", status="needs_answers",
                                   questions=questions,
                                   details="Group yêu cầu trả lời câu hỏi trước khi tham gia.")
                    return outcome
                if questions:
                    filled = await fill_join_answers(page, questions, answers)
                    if not filled:
                        outcome.update(state="needs_answers", status="needs_answers",
                                       questions=questions,
                                       details="Câu trả lời chưa đủ; cần bạn xem lại các câu hỏi.")
                        return outcome
                    await page.wait_for_timeout(1200)
                submit = await first_visible(page, [
                    page.get_by_role("button", name=re.compile(r"^(Join|Tham gia|Submit|Gửi)$", re.I)),
                    page.get_by_text(re.compile(r"^(Join|Tham gia|Submit|Gửi)$", re.I)),
                ], timeout=2000)
                if submit is not None:
                    await click_element(submit, timeout=10000)
                    await page.wait_for_timeout(3000)
                try:
                    body = (await page.locator("body").inner_text(timeout=8000))[:8000]
                except Exception:
                    body = ""
                if GROUP_JOIN_REJECTED_RE.search(body):
                    outcome.update(state="failed", status="rejected",
                                   details="Group từ chối yêu cầu tham gia.")
                    return outcome
                if GROUP_JOIN_PENDING_RE.search(body):
                    outcome.update(state="ok", status="pending_approval", membership="pending_approval",
                                   details="Yêu cầu tham gia đã được gửi và đang chờ group duyệt.")
                    return outcome
                composer = await first_visible(page, [
                    page.get_by_text(GROUP_COMPOSER_RE),
                    page.get_by_role("button", name=GROUP_COMPOSER_RE),
                ], timeout=2000)
                if composer is not None:
                    outcome.update(state="ok", status="joined", membership="joined",
                                   details="Tài khoản đã là thành viên group.")
                else:
                    outcome.update(state="failed", status="unknown",
                                   details="Đã bấm tham gia nhưng chưa xác nhận được kết quả.")
                return outcome
            finally:
                await close_context(context)
    except ProfileBusy as error:
        outcome.update(state="failed", status="busy", details=str(error))
    except Exception as error:
        outcome.update(state="failed", status="error",
                       details=f"Lỗi trình duyệt khi tham gia: {type(error).__name__}")
    return outcome


async def join_questions(page) -> list[str]:
    """Các câu hỏi group hỏi trước khi cho vào."""
    found: list[str] = []
    try:
        dialog = page.get_by_role("dialog").last
        for locator in (dialog.locator("label"), page.locator("label"),
                        dialog.get_by_role("textbox"), page.get_by_role("textbox")):
            try:
                for item in await locator.all():
                    text = (await item.inner_text()).strip()
                    if text and text not in found and len(text) < 300:
                        found.append(text)
            except Exception:
                continue
    except Exception:
        pass
    return found[:10]


async def fill_join_answers(page, questions: list[str], answers: dict) -> bool:
    """Điền câu trả lời của CTV vào form tham gia group."""
    filled = True
    try:
        dialog = page.get_by_role("dialog").last
        fields = []
        for locator in (dialog.get_by_role("textbox"), page.get_by_role("textbox"),
                        dialog.locator("textarea"), page.locator("textarea")):
            try:
                fields.extend(await locator.all())
            except Exception:
                continue
        for index, field in enumerate(fields):
            question = questions[index] if index < len(questions) else ""
            answer = str(answers.get(question) or "").strip()
            if not answer:
                filled = False
                continue
            try:
                await field.fill(answer, timeout=8000)
            except Exception:
                filled = False
    except Exception:
        return False
    return filled


async def active_composer_dialog(page):
    """Hop thoai composer dang mo, tuc la hop thoai co o nhap noi dung.

    Facebook nhiet ba hop thoai: hop thoi goc, hop thoi composer va cac hop
    thoai thong bao long nhau. Nut cua composer nam o hop thoi dung, khong
    phai hop thoi cuoi cung.
    """
    try:
        dialogs = await page.get_by_role("dialog").all()
    except Exception:
        return page.get_by_role("dialog").last
    for dialog in dialogs:
        try:
            if await dialog.locator('[contenteditable="true"]').count():
                return dialog
        except Exception:
            continue
    return page.get_by_role("dialog").last


async def attempt_post(data: dict):
    account_id, job_id = int(data["account_id"]), int(data["job_id"])
    expected = str(data["group_url"])
    evidence_name = f"job-{job_id}.png"
    evidence_path = EVIDENCE_DIR / evidence_name
    was_clicked = False
    run_id = new_run_id()
    logger = JsonlEventLogger(default_log_dir(DATA) / "posting.jsonl", run_id=run_id)
    context = None
    try:
        logger.emit("post_started", job_id=job_id, account_id=account_id,
                    group_url=expected, run_id=run_id)
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            page = context.pages[0] if context.pages else await context.new_page()

            # Chuẩn hóa URL group trước khi mở, tránh đăng nhầm group vì
            # tham số truy vết hay viết tắt khác nhau trỏ cùng một group.
            try:
                canonical_url, _ = group_links.normalize_group_url(expected)
                expected = canonical_url
            except Exception:
                pass

            def go_to_group():
                return page.goto(expected, wait_until="domcontentloaded", timeout=65000)

            # Chỉ retry lỗi mạng tạm thời khi điều hướng; không bao giờ
            # retry sau khi đã nhấn nút Đăng (để tránh đăng trùng).
            await run_with_bounded_retry(go_to_group, policy=RetryPolicy(),
                                   stop_fn=lambda: False)
            await page.wait_for_timeout(5000)
            if any(part in page.url for part in ("/login", "/checkpoint", "/two_step_verification")):
                logger.emit("post_failed", job_id=job_id, reason="login_or_checkpoint",
                            run_id=run_id)
                return result("failed", "Login or checkpoint required; open login browser and handle manually")
            if "/groups/" not in page.url:
                logger.emit("post_failed", job_id=job_id, reason="unexpected_destination",
                            run_id=run_id)
                return result("failed", "Unexpected destination; refusing to post")
            body_text = (await page.locator("body").inner_text(timeout=10000))[:6500]
            if any(s in body_text for s in ("Join Group", "Tham gia nhóm")) and not any(s in body_text for s in ("Write something", "Bạn viết gì", "Viết gì")):
                logger.emit("post_failed", job_id=job_id, reason="not_member_or_no_composer",
                            run_id=run_id)
                return result("failed", "Not a member or group permissions prevent posting")

            mode = str(data.get("posting_mode") or "anonymous").lower()
            nickname = str(data.get("nickname") or "").strip()
            if mode not in ("anonymous", "nickname"):
                return result("needs_review", f"Unknown posting mode '{mode}'; no post submitted")
            if not data.get("media"):
                return result("failed", "Group posts must contain an image")
            await dismiss_common_popups(page)
            compose = await first_visible(page, [
                page.get_by_text(re.compile(r"Write something|Viết gì đó|Bạn viết gì|Hãy viết gì đó", re.I)),
                page.get_by_role("button", name=re.compile(r"Write something|Viết gì đó|Bạn viết gì", re.I)),
                page.locator("div[role='button']:has-text('Bạn viết gì đi')"),
                page.locator("div[role='button']:has-text('Bạn đang nghĩ gì')"),
                page.locator("div[aria-label='Tạo bài viết']"),
            ])
            if compose is None:
                logger.emit("post_failed", job_id=job_id, reason="composer_not_found",
                            run_id=run_id)
                return result("failed", "Composer not found. Facebook interface may have changed; no post submitted")
            if not await click_element(compose, timeout=10000):
                logger.emit("post_failed", job_id=job_id, reason="composer_not_clickable",
                            run_id=run_id)
                return result("failed", "Không mở được ô soạn bài; không đăng bài")
            await page.wait_for_timeout(1500)
            actual_identity, verification, identity_error = await enforce_posting_identity(
                page, data["pipeline_id"], data["account_id"], data["group_id"], mode, nickname)
            if identity_error:
                await page.screenshot(path=str(evidence_path), full_page=False, timeout=10000)
                logger.emit("post_needs_review", job_id=job_id, reason=identity_error,
                            run_id=run_id)
                return result("needs_review", identity_error, evidence_name, "", verification)
            dialog = await active_composer_dialog(page)
            editor = await first_visible(page, [
                dialog.locator('[contenteditable="true"][role="textbox"]'),
                dialog.locator('[contenteditable="true"]'),
                page.locator('[contenteditable="true"][role="textbox"]'),
            ])
            if editor is None:
                logger.emit("post_failed", job_id=job_id, reason="editor_not_found",
                            run_id=run_id)
                return result("failed", "Text editor unavailable; no post submitted")
            await editor.fill(str(data["body"]), timeout=15000)

            media = list(data.get("media") or [])
            if media:
                # Fallback upload: thử nhiều chiến lược, không ép khi giao diện đổi.
                uploaded = await upload_media(page, media,
                                              dialog_selector="div[role='dialog']")
                if not uploaded:
                    logger.emit("post_failed", job_id=job_id, reason="media_upload_failed",
                                run_id=run_id)
                    return result("failed", "Media upload control not found; no post submitted")
                await page.wait_for_timeout(3000)

            # Require a visible publish button in the active post dialog.
            submit = await first_visible(page, [
                dialog.get_by_role("button", name=SUBMIT_BUTTON_RE),
                dialog.get_by_text(SUBMIT_BUTTON_RE),
                *[dialog.locator(selector) for selector in SUBMIT_BUTTON_SELECTORS],
                page.get_by_role("button", name=SUBMIT_BUTTON_RE),
                page.get_by_text(SUBMIT_BUTTON_RE),
                *[page.locator(selector) for selector in SUBMIT_BUTTON_SELECTORS],
            ], timeout=2500)
            if submit is None:
                await page.screenshot(path=str(evidence_path), full_page=False, timeout=10000)
                labels = await describe_composer(page)
                logger.emit("post_failed", job_id=job_id, reason="submit_button_not_found",
                            run_id=run_id)
                return result("failed",
                              "Không tìm thấy nút đăng trong ô soạn bài; không đăng bài"
                              + (f" (có: {labels})" if labels else ""),
                              evidence_name)
            if mode == "nickname":
                if not await identity_shown(page, actual_identity):
                    logger.emit("post_needs_review", job_id=job_id,
                                reason="nickname_not_verified", run_id=run_id)
                    return result("needs_review",
                                  "Không xác minh được biệt danh trước khi gửi; không đăng bài",
                                  evidence_name, actual_identity, "not_verified")
            else:
                # Hộp thoại giải thích có thể xuất hiện lại; đóng trước khi
                # kiểm tra danh tính lần cuối.
                await dismiss_anonymous_notice(page)
                if not await anonymous_mode_verified(page):
                    logger.emit("post_needs_review", job_id=job_id,
                                reason="anonymous_not_verified", run_id=run_id)
                    return result("needs_review",
                                  "Chế độ ẩn danh mất trước khi gửi; không đăng bài",
                                  evidence_name, actual_identity, "not_verified")

            # Final Destination Lock validation before Submit
            pipeline_id = data.get("pipeline_id")
            if pipeline_id is not None:
                lock_service = DestinationLockService()
                # Read the current caption from the editor
                try:
                    current_caption = await editor.inner_text()
                except Exception:
                    current_caption = str(data.get("body") or "")
                
                is_valid, validation_msg = lock_service.validate_caption_with_lock(current_caption, pipeline_id)
                if not is_valid:
                    # Try to apply the lock to the editor
                    try:
                        locked_caption, was_modified, lock_msg = lock_service.apply_destination_lock(current_caption, pipeline_id)
                        if was_modified:
                            await editor.fill(locked_caption, timeout=10000)
                            await page.wait_for_timeout(500)
                            # Re-validate
                            is_valid, validation_msg = lock_service.validate_caption_with_lock(locked_caption, pipeline_id)
                            if not is_valid:
                                logger.emit("post_needs_review", job_id=job_id,
                                            reason="destination_lock_failed", run_id=run_id)
                                return result("needs_review",
                                              f"Không thể áp dụng Destination Lock: {validation_msg}",
                                              evidence_name, actual_identity, "not_verified")
                        else:
                            # Lock is enabled but couldn't be applied
                            logger.emit("post_needs_review", job_id=job_id,
                                        reason="destination_lock_failed", run_id=run_id)
                            return result("needs_review",
                                          f"Destination Lock bắt buộc nhưng không thể áp dụng: {validation_msg}",
                                          evidence_name, actual_identity, "not_verified")
                    except Exception as e:
                        logger.emit("post_needs_review", job_id=job_id,
                                    reason="destination_lock_error", run_id=run_id)
                        return result("needs_review",
                                      f"Lỗi Destination Lock: {e}",
                                      evidence_name, actual_identity, "not_verified")

            if not await submit.is_enabled():
                logger.emit("post_failed", job_id=job_id, reason="submit_disabled",
                            run_id=run_id)
                return result("failed", "Publish control disabled; no post submitted")
            was_clicked = True
            if not await click_element(submit, timeout=15000):
                logger.emit("post_failed", job_id=job_id, reason="submit_click_failed",
                            run_id=run_id)
                return result("failed", "Không bấm được nút đăng; không đăng bài")
            # Quan sát kết quả thật. Không có kết luận nào được rút ra từ
            # việc đã bấm nút. Không tự retry sau khi nhấn Đăng.
            outcome = await wait_post_outcome(page, str(data.get("body") or ""))
            permalink = outcome["permalink"] or await newest_permalink(
                page, str(data.get("body") or ""))
            await page.screenshot(path=str(evidence_path), full_page=False, timeout=15000)
            if outcome["state"] == "published":
                logger.emit("post_posted", job_id=job_id, identity=actual_identity,
                            permalink=permalink, run_id=run_id)
                return result("posted",
                              "Facebook đã xác nhận bài đã đăng",
                              evidence_name, actual_identity, "verified", permalink)
            if outcome["state"] == "pending_approval":
                logger.emit("post_pending_approval", job_id=job_id, identity=actual_identity,
                            permalink=permalink, run_id=run_id)
                return result("pending_approval",
                              "Facebook đã nhận bài và đang chờ quản trị group duyệt",
                              evidence_name, actual_identity, "verified", permalink)
            if outcome["state"] == "rejected":
                logger.emit("post_rejected", job_id=job_id, identity=actual_identity,
                            permalink=permalink, run_id=run_id)
                return result("failed",
                              "Facebook từ chối bài đăng trong group này",
                              evidence_name, actual_identity, "not_verified", permalink)
            logger.emit("post_submitted_unverified", job_id=job_id, identity=actual_identity,
                        permalink=permalink, run_id=run_id)
            return result("submitted_unverified", outcome["details"], evidence_name,
                          actual_identity, "not_verified", permalink)
    except AmbiguousPublishStateError as error:
        # Kết quả đăng chưa rõ: không retry, không coi là thành công.
        logger.emit("post_ambiguous", job_id=job_id, reason=str(error), run_id=run_id)
        return result("submitted_unverified", str(error)[:500], evidence_name)
    except Exception as error:
        error_class = classify_error(error)
        logger.emit("post_exception", job_id=job_id, error_class=error_class,
                    error=type(error).__name__, run_id=run_id)
        label = "needs_review" if error_class == "publish_ambiguous" else "needs_review"
        if context:
            try:
                pages = context.pages
                if pages:
                    await pages[0].screenshot(path=str(evidence_path), timeout=5000)
            except Exception:
                pass
        return result(label, f"Browser exception: {type(error).__name__}: {str(error)[:500]}",
                      evidence_name if evidence_path.is_file() else "")
    finally:
        logger.close()
        if context:
            try:
                await close_context(context)
            except Exception:
                pass


async def attempt_reel(data: dict):
    """Best-effort Facebook profile Reel via visible desktop UI, fail-closed.

    Only publish after reaching an explicit Reel composer. Never convert the
    video into a normal feed upload when the Reel composer isn't available.
    """
    account_id, task_id = int(data["account_id"]), int(data["task_id"])
    media = list(data.get("media") or [])
    evidence_name = f"reel-{task_id}.png"
    evidence_path = EVIDENCE_DIR / evidence_name
    if len(media) != 1 or not Path(media[0]).is_file():
        return result("failed", "Exactly one existing video is required")
    was_clicked = False
    context = None
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.facebook.com/reels/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
            if any(x in page.url for x in ("/login", "/checkpoint", "/two_step_verification")):
                return result("failed", "Facebook account requires login/checkpoint")
            create = await first_visible(page, [
                page.get_by_role("button", name=re.compile(r"Create reel|Tạo thước phim|Tạo reel", re.I)),
                page.get_by_role("link", name=re.compile(r"Create reel|Tạo thước phim|Tạo reel", re.I)),
            ], timeout=2000)
            if create is None:
                return result("failed", "Reel creation control unavailable; do not upload as a normal profile post")
            await create.click(timeout=10000)
            await page.wait_for_timeout(1200)
            if not ("reel" in page.url.lower() or await page.get_by_text(re.compile(r"Create reel|Tạo thước phim|Tạo reel", re.I)).count()):
                return result("failed", "Could not verify Reel composer; no post submitted")
            add_video = await first_visible(page, [
                page.get_by_role("button", name=re.compile(r"Add video|Thêm video|Chọn video|Upload video", re.I)),
                page.get_by_text(re.compile(r"^Add video$|^Thêm video$|^Chọn video$", re.I)),
            ], timeout=3000)
            if add_video is None:
                return result("failed", "Reel video upload control unavailable")
            # Browser UI usually opens a file chooser. Do not upload to hidden
            # unrelated inputs on the home/feed page.
            async with page.expect_file_chooser(timeout=15000) as chooser_wait:
                await add_video.click(timeout=10000)
            chooser = await chooser_wait.value
            await chooser.set_files(media[0])
            await page.wait_for_timeout(5000)
            # Review steps; descriptions can be introduced after one or two Next clicks.
            for _ in range(3):
                description = await first_visible(page, [
                    page.get_by_role("textbox", name=re.compile(r"Describe your reel|Mô tả thước phim|Mô tả reel", re.I)),
                    page.get_by_placeholder(re.compile(r"Describe your reel|Mô tả thước phim|Mô tả reel", re.I)),
                ], timeout=1500)
                if description:
                    if data.get("body"):
                        await description.fill(str(data["body"])[:20000], timeout=10000)
                    break
                next_button = await first_visible(page, [page.get_by_role("button", name=re.compile(r"^(Next|Tiếp|Tiếp theo)$", re.I))], timeout=1800)
                if not next_button:
                    return result("failed", "Reel preview/Next not available; no post submitted")
                await next_button.click(timeout=12000)
                await page.wait_for_timeout(2000)
            else:
                return result("failed", "Reel description stage unavailable; no post submitted")
            # Require unmistakable Reel-specific form before clicking publish.
            reel_context = await page.get_by_text(re.compile(r"Your reel|Describe your reel|Reel của bạn|Mô tả reel|thước phim",re.I)).count()
            if reel_context == 0:
                return result("failed", "Reel identity not confirmed at publish stage")
            publish = await first_visible(page, [
                page.get_by_role("button", name=re.compile(r"^(Publish|Publish reel|Đăng thước phim|Đăng reel)$", re.I)),
            ], timeout=2000)
            if publish is None or not await publish.is_enabled():
                return result("failed", "Reel publish control disabled or absent")
            was_clicked = True
            await publish.click(timeout=15000)
            await page.wait_for_timeout(5000)
            await page.screenshot(path=str(evidence_path), full_page=False, timeout=15000)
            return result("submitted_unverified", "Reel publish clicked; confirm video exists on personal profile", evidence_name)
    except Exception as error:
        state = "needs_review" if was_clicked else "failed"
        if context:
            try:
                if context.pages:
                    await context.pages[0].screenshot(path=str(evidence_path),timeout=5000)
            except Exception:
                pass
        return result(state, f"Reel browser error {type(error).__name__}; {'check profile before retrying' if was_clicked else 'no publish click observed'}",
                      evidence_name if evidence_path.exists() else "")
    finally:
        if context:
            try:
                await close_context(context)
            except Exception:
                pass


# --- the counters Facebook shows on the page of a post -------------------------
# --- the counters Facebook shows on the page of a post -------------------------
# --- the counters Facebook shows on the page of a post -------------------------
NUMBER = r"([0-9][0-9.,]*)(?:\s*([KkMm]))?"
LABELS = {
    "reactions": r"reactions?|likes?|lượt thích|thích",
    "comments": r"comments?|bình luận",
    "shares": r"shares?|lượt chia sẻ|chia sẻ",
    "views": r"views?|lượt xem",
}


def scale_count(raw: str, suffix: str = "") -> int | None:
    """Read "1.2K", "3,4K", "12 345" as a number."""
    if not raw:
        return None
    text = raw.strip().replace(" ", "").replace(".", ",")
    if not text:
        return None
    if "," in text:
        head, _, tail = text.partition(",")
        text = head + ("." + tail if len(tail) in (1, 2) else "")
    try:
        number = int(round(float(text)))
    except ValueError:
        return None
    if (suffix or "").lower() == "k":
        return number * 1000
    if (suffix or "").lower() == "m":
        return number * 1000000
    return number


def counters_from_text(text: str) -> dict:
    """The counters of a post page.

    Facebook writes the number before or after the label depending on the
    language, so every number goes to the label that is closest to it. A
    counter that is not on the page keeps the value None: it means "chưa có
    dữ liệu", never zero.
    """
    content = text or ""
    marks = []
    for metric, pattern in LABELS.items():
        for match in re.finditer(rf"({pattern})", content, re.I):
            marks.append((match.start(), match.end(), metric))
    marks.sort()
    numbers = [(match.start(), match.end(), match.group(1), match.group(2) or "")
               for match in re.finditer(NUMBER, content)]
    candidates = []
    for index, (start, end, _metric) in enumerate(marks):
        previous_end = marks[index - 1][1] if index else 0
        next_start = marks[index + 1][0] if index + 1 < len(marks) else len(content)
        for number_start, number_end, raw, suffix in numbers:
            if number_end <= previous_end or number_start >= next_start:
                continue
            if number_start >= end:
                distance = number_start - end
            elif number_end <= start:
                distance = start - number_end
            else:
                continue
            if distance > 14:
                continue
            candidates.append((distance, index, number_start, raw, suffix))
    candidates.sort()
    result = {metric: None for metric in LABELS}
    taken_labels = set()
    taken_numbers = set()
    for _distance, index, number_start, raw, suffix in candidates:
        metric = marks[index][2]
        if metric in taken_labels or number_start in taken_numbers:
            continue
        value = scale_count(raw, suffix)
        if value is None:
            continue
        result[metric] = value
        taken_labels.add(metric)
        taken_numbers.add(number_start)
    return result


REMOVED_TEXT = re.compile(
    r"This content isn.t available|content is no longer available|Bài viết này không còn khả dụng|"
    r"nội dung này không còn khả dụng|This post isn.t available", re.I)


def engagement_result(counters: dict, status: str, error_code: str = ""):
    return {"counters": counters, "verification_status": status, "error_code": error_code}


async def dismiss_common_popups(page) -> bool:
    """Đóng các hộp thoại phủ lên composer: cookie, quảng cáo, thông báo.

    Hộp thoại này chắn thao tác chuột, khiến nút bấm bị báo lỗi dù đã tìm
    thấy đúng phần tử.
    """
    candidates = [
        "div[aria-label='Đóng']",
        "div[aria-label='Close']",
        "div[role='button']:has-text('Để sau')",
        "div[role='button']:has-text('Not now')",
        "div[role='button']:has-text('Bỏ qua')",
        "div[role='button']:has-text('Skip')",
    ]
    closed = False
    for selector in candidates:
        try:
            target = page.locator(selector).first
            await target.wait_for(state="visible", timeout=400)
            if await target.is_enabled():
                if await click_element(target, timeout=1500):
                    closed = True
        except Exception:
            continue
    return closed


async def describe_composer(page) -> str:
    """Mô tả ngắn các nút đang hiện trong composer, để đọc lỗi dễ hơn."""
    try:
        dialog = page.get_by_role("dialog").last
        labels = []
        for element in await dialog.locator("span, div[role='button']").all():
            try:
                text = (await element.inner_text()).strip()
                if text and len(text) <= 40 and text not in labels:
                    labels.append(text)
            except Exception:
                continue
            if len(labels) >= 12:
                break
        return ", ".join(labels)[:400]
    except Exception:
        return ""


async def attempt_engagement(data: dict):
    """Read the public counters of a published post.

    Nothing is guessed: a counter that is not on the page keeps the state
    "chưa có dữ liệu". A checkpoint stops the reader at once.
    """
    account_id = int(data["account_id"])
    permalink = str(data.get("permalink") or "").strip()
    counters = {"reactions": None, "comments": None, "shares": None, "views": None}
    if not permalink:
        return engagement_result(counters, "unavailable", "no_permalink")
    context = None
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(permalink, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3500)
            if any(part in page.url for part in CHECKPOINT_URL):
                return engagement_result(counters, "unavailable", "checkpoint")
            body_text = ""
            try:
                body_text = (await page.locator("body").inner_text(timeout=10000))[:12000]
            except Exception:
                body_text = ""
            if REMOVED_TEXT.search(body_text):
                return engagement_result(counters, "removed", "content_removed")
            counters = counters_from_text(body_text)
            if all(value is None for value in counters.values()):
                return engagement_result(counters, "unavailable", "no_counter_on_page")
            return engagement_result(counters, "measured")
    except Exception as error:
        return engagement_result(counters, "error", type(error).__name__.lower()[:40])
    finally:
        if context:
            try:
                await close_context(context)
            except Exception:
                pass


async def _cli_check_composer(payload: dict) -> dict:
    """Kiểm tra xem ô soạn bài có sẵn trên nhóm hay không (dùng cho CLI debug)."""
    account_id = int(payload["account_id"])
    url = str(payload["group_url"])
    outcome = {"status": "unknown", "composer": False, "editor": False,
               "publish": False, "media": False, "details": "", "url": url}
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=65000)
                except Exception:
                    outcome.update(status="error", details="Không mở được trang group")
                    return outcome
                await page.wait_for_timeout(4000)
                if any(part in page.url for part in ("/login", "/checkpoint", "/two_step_verification")):
                    outcome.update(status="checkpoint", details="Yêu cầu đăng nhập hoặc xác minh")
                    return outcome
                composer = await find_composer(page, timeout=3000)
                outcome["composer"] = composer is not None
                if composer is None:
                    outcome.update(status="no_composer", details="Không tìm thấy ô soạn bài")
                    return outcome
                if await click_element(composer, timeout=10000):
                    await page.wait_for_timeout(1500)
                    dialog = await active_composer_dialog(page)
                    editor = await find_editor(page, dialog, timeout=5000)
                    outcome["editor"] = editor is not None
                    publish = await find_publish_button(page, dialog, timeout=3000)
                    outcome["publish"] = publish is not None
                    media = await find_media_button(page, dialog, timeout=3000)
                    outcome["media"] = media is not None
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    outcome.update(status="ok", details="Ô soạn bài có sẵn")
                else:
                    outcome.update(status="composer_not_clickable", details="Không bấm được ô soạn bài")
            finally:
                await close_context(context)
    except Exception as error:
        outcome.update(status="error", details=f"Lỗi: {type(error).__name__}: {str(error)[:300]}")
    return outcome


async def _cli_check_identity(payload: dict) -> dict:
    """Kiểm tra chế độ đăng ẩn danh / biệt danh có sẵn hay không (CLI debug)."""
    account_id = int(payload["account_id"])
    url = str(payload["group_url"])
    mode = str(payload.get("posting_mode") or "anonymous").lower()
    nickname = str(payload.get("nickname") or "").strip()
    outcome = {"status": "unknown", "mode": mode, "available": False,
               "details": "", "url": url}
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=65000)
                except Exception:
                    outcome.update(status="error", details="Không mở được trang group")
                    return outcome
                await page.wait_for_timeout(4000)
                if any(part in page.url for part in ("/login", "/checkpoint", "/two_step_verification")):
                    outcome.update(status="checkpoint", details="Yêu cầu đăng nhập hoặc xác minh")
                    return outcome
                composer = await find_composer(page, timeout=3000)
                if composer is None:
                    outcome.update(status="no_composer", details="Không tìm thấy ô soạn bài")
                    return outcome
                if await click_element(composer, timeout=10000):
                    await page.wait_for_timeout(1500)
                    if mode == "anonymous":
                        available = await require_anonymous_mode(page)
                        outcome["available"] = available
                        outcome["details"] = ("Chế độ ẩn danh có sẵn" if available
                                              else "Chế độ ẩn danh không khả dụng")
                    elif mode == "nickname":
                        if not nickname:
                            outcome.update(status="needs_nickname", details="Chưa cấu hình biệt danh")
                        else:
                            selected, names, status = await require_nickname_identity(
                                page, data["pipeline_id"], data["account_id"], data["group_id"], nickname)
                            outcome["available"] = bool(selected)
                            outcome["details"] = (f"Đã chọn '{selected}'" if selected
                                                  else f"Biệt danh không có sẵn ({', '.join(names[:3]) if names else 'không có'})")
                        try:
                            await page.keyboard.press("Escape")
                        except Exception:
                            pass
                    else:
                        outcome.update(status="unknown_mode", details=f"Chế độ không hợp lệ: {mode}")
                    outcome.update(status="ok" if outcome["available"] else "unavailable")
                else:
                    outcome.update(status="composer_not_clickable", details="Không bấm được ô soạn bài")
            finally:
                await close_context(context)
    except Exception as error:
        outcome.update(status="error", details=f"Lỗi: {type(error).__name__}: {str(error)[:300]}")
    return outcome


def _cli_dry_run(payload: dict) -> dict:
    """Xác thực yêu cầu đăng mà không thực sự đăng (CLI debug, Dry Run).

    Không mở trình duyệt, không nhấn nút Đăng. Chỉ kiểm tra tính hợp lệ của
    dữ liệu đầu vào.
    """
    errors = []
    account_id = payload.get("account_id")
    group_url = payload.get("group_url")
    body = payload.get("body") or ""
    media = payload.get("media") or []
    mode = str(payload.get("posting_mode") or "anonymous").lower()
    nickname = str(payload.get("nickname") or "").strip()

    if not account_id:
        errors.append("Thiếu account_id")
    if not group_url:
        errors.append("Thiếu group_url")
    else:
        try:
            group_links.normalize_group_url(group_url)
        except Exception as error:
            errors.append(f"Group URL không hợp lệ: {error}")
    if not body:
        errors.append("Thiếu caption (body)")
    if not media:
        errors.append("Phải có ít nhất một tệp ảnh")
    if mode not in ("anonymous", "nickname"):
        errors.append(f"Chế độ đăng không hợp lệ: {mode}")
    if mode == "nickname" and not nickname:
        errors.append("Chế độ biệt danh cần tên biệt danh")

    return {
        "status": "ok" if not errors else "invalid",
        "dry_run": True,
        "account_id": account_id,
        "group_url": group_url,
        "mode": mode,
        "nickname": nickname,
        "media_count": len(media),
        "errors": errors,
        "details": "Dry Run: không nhấn nút Đăng, không đăng bài thật" if not errors
        else "; ".join(errors),
    }


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "login":
        asyncio.run(manual_login(int(sys.argv[2])))
        return
    if len(sys.argv) == 4 and sys.argv[1] == "check":
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        outcome = asyncio.run(check_session(int(payload["account_id"])))
        Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
        print(outcome["status"])
        return
    if len(sys.argv) == 4 and sys.argv[1] == "check-group":
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        outcome = asyncio.run(check_group(payload))
        Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
        print(outcome.get("status", "unknown"))
        return
    if len(sys.argv) == 4 and sys.argv[1] == "join-group":
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        outcome = asyncio.run(join_group(payload))
        Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
        print(outcome.get("status", "unknown"))
        return
    if len(sys.argv) == 4 and sys.argv[1] == "engagement":
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        outcome = asyncio.run(attempt_engagement(payload))
        Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
        print(outcome.get("verification_status", "unavailable"))
        return
    if len(sys.argv) != 4 or sys.argv[1] not in ("post", "reel"):
        print("Usage: fb_browser.py login <account_id> | check|check-group|check-composer|check-identity|dry-run|post|reel|engagement <request.json> <result.json>", file=sys.stderr)
        raise SystemExit(2)
    payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    if sys.argv[1] == "check-composer":
        outcome = asyncio.run(_cli_check_composer(payload))
        Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
        print(outcome.get("status", "unknown"))
        return
    if sys.argv[1] == "check-identity":
        outcome = asyncio.run(_cli_check_identity(payload))
        Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
        print(outcome.get("status", "unknown"))
        return
    if sys.argv[1] == "dry-run":
        outcome = _cli_dry_run(payload)
        Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
        print(outcome.get("status", "unknown"))
        return
    outcome = asyncio.run(attempt_post(payload) if sys.argv[1] == "post" else attempt_reel(payload))
    Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
    print(outcome["state"])


if __name__ == "__main__":
    main()
