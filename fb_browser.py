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
from services.engagement_collector import counters_from_text

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
PROFILE_DIR = DATA / "profiles"
EVIDENCE_DIR = DATA / "evidence"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


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
    return context


async def close_context(context):
    """Close a Chromium context and release the profile lock it holds."""
    if context is None:
        return
    lock = getattr(context, "fpm_profile_lock", None)
    try:
        await context.close()
    finally:
        if lock is not None:
            lock.release()


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
#: Nut dang bai. Khong chua dau "/" nen dung duoc trong bieu thuc chon.
SUBMIT_BUTTON_RE = re.compile(r"^(Post|Đăng|Đăng bài)$", re.I)
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


async def require_nickname_identity(page, nickname: str):
    """Select the configured nickname. Returns (selected, available names)."""
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


async def check_group(data: dict) -> dict:
    """Check what Facebook allows this account to do in a group.

    Nothing is ever posted here and no challenge is bypassed. Every answer
    that cannot be confirmed is reported as unknown.

    ``accessible``, ``is_member`` and ``can_post`` are three valued: ``True``
    when the browser saw it, ``False`` only when the browser saw a clear sign
    of the opposite, and ``None`` when there was not enough to tell. A missing
    selector, a timeout or a browser error is never a sign that Facebook
    restricts the account.
    """
    account_id = int(data["account_id"])
    url = str(data["group_url"])
    wanted_nickname = str(data.get("nickname") or "").strip()
    outcome = {
        "status": "unknown", "logged_in": False, "accessible": None, "is_member": None,
        "can_post": None, "supports_anonymous": None, "supports_nickname": None,
        "nickname_available": None, "requires_approval": False, "group_name": "",
        "resolved_url": "", "details": "", "evidence": "",
    }
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
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
                    outcome.update(status="not_member", is_member=False, can_post=None,
                                   details="Tài khoản chưa tham gia group này.")
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


async def enforce_posting_identity(page, mode: str, nickname: str):
    """Make the composer use exactly the requested identity.

    Returns (identity, verification, error). The identity is empty when the
    request could not be honoured, and the caller must then stop without
    posting. The profile name is never used as a substitute.
    """
    mode = (mode or "anonymous").strip().lower()
    nickname = (nickname or "").strip()
    if mode == "nickname":
        if not nickname:
            return "", "unverified", "Chưa cấu hình biệt danh cho group này; không đăng bài"
        selected, available = await require_nickname_identity(page, nickname)
        if not selected:
            known = ", ".join(available[:3]) if available else "Facebook không hiển thị danh tính nào"
            return "", "not_verified", (f"Biệt danh đã cấu hình không có trong danh sách danh tính của "
                                        f"Facebook ({known}); không đăng bài dưới tên thật")
        return selected, "verified", ""
    if mode != "anonymous":
        return "", "unverified", f"Chế độ đăng không hợp lệ '{mode}'; không đăng bài"
    activated = await require_anonymous_mode(page)
    if not activated:
        return "", "not_verified", ("Chế độ ẩn danh không khả dụng hoặc không xác minh được; "
                                    "không đăng bài dưới tên thật")
    return "anonymous", "verified", ""


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
    context = None
    try:
        async with async_playwright() as playwright:
            context = await context_for(playwright, account_id)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(expected, wait_until="domcontentloaded", timeout=65000)
            await page.wait_for_timeout(5000)
            if any(part in page.url for part in ("/login", "/checkpoint", "/two_step_verification")):
                return result("failed", "Login or checkpoint required; open login browser and handle manually")
            if "/groups/" not in page.url:
                return result("failed", "Unexpected destination; refusing to post")
            body_text = (await page.locator("body").inner_text(timeout=10000))[:6500]
            if any(s in body_text for s in ("Join Group", "Tham gia nhóm")) and not any(s in body_text for s in ("Write something", "Bạn viết gì", "Viết gì")):
                return result("failed", "Not a member or group permissions prevent posting")

            mode = str(data.get("posting_mode") or "anonymous").lower()
            nickname = str(data.get("nickname") or "").strip()
            if mode not in ("anonymous", "nickname"):
                return result("needs_review", f"Unknown posting mode '{mode}'; no post submitted")
            if not data.get("media"):
                return result("failed", "Group posts must contain an image")
            compose = await first_visible(page, [
                page.get_by_text(re.compile(r"Write something|Viết gì đó|Bạn viết gì|Hãy viết gì đó", re.I)),
                page.get_by_role("button", name=re.compile(r"Write something|Viết gì đó|Bạn viết gì", re.I)),
            ])
            if compose is None:
                return result("failed", "Composer not found. Facebook interface may have changed; no post submitted")
            if not await click_element(compose, timeout=10000):
                return result("failed", "Không mở được ô soạn bài; không đăng bài")
            await page.wait_for_timeout(1500)
            actual_identity, verification, identity_error = await enforce_posting_identity(
                page, mode, nickname)
            if identity_error:
                await page.screenshot(path=str(evidence_path), full_page=False, timeout=10000)
                return result("needs_review", identity_error, evidence_name, "", verification)
            dialog = await active_composer_dialog(page)
            editor = await first_visible(page, [
                dialog.locator('[contenteditable="true"][role="textbox"]'),
                dialog.locator('[contenteditable="true"]'),
                page.locator('[contenteditable="true"][role="textbox"]'),
            ])
            if editor is None:
                return result("failed", "Text editor unavailable; no post submitted")
            await editor.fill(str(data["body"]), timeout=15000)

            media = list(data.get("media") or [])
            if media:
                photo_button = await first_visible(page, [
                    *[dialog.locator(f'[aria-label="{label}"]') for label in PHOTO_BUTTON_LABELS],
                    *[page.locator(f'[aria-label="{label}"]') for label in PHOTO_BUTTON_LABELS],
                    dialog.get_by_role("button", name=PHOTO_BUTTON_RE),
                    page.get_by_role("button", name=PHOTO_BUTTON_RE),
                ])
                if photo_button is None:
                    return result("failed", "Media upload control not found; no post submitted")
                chooser = None
                try:
                    async with page.expect_file_chooser(timeout=15000) as chooser_info:
                        if not await click_element(photo_button, timeout=8000):
                            return result("failed", "Không bấm được nút thêm ảnh; không đăng bài")
                    chooser = await chooser_info.value
                except Exception:
                    # Một số giao diện mở hộp chọn ảnh thay vì hộp chọn tệp.
                    upload = await first_visible(page, [
                        dialog.get_by_role("button", name=PHOTO_UPLOAD_RE),
                        page.get_by_role("button", name=PHOTO_UPLOAD_RE),
                    ], timeout=3000)
                    if upload is None:
                        return result("failed", "Không mở được hộp chọn ảnh; không đăng bài")
                    try:
                        async with page.expect_file_chooser(timeout=15000) as chooser_info:
                            if not await click_element(upload, timeout=8000):
                                return result("failed", "Không mở được hộp chọn tệp; không đăng bài")
                        chooser = await chooser_info.value
                    except Exception:
                        return result("failed", "Không mở được hộp chọn tệp; không đăng bài")
                await chooser.set_files(media)
                await page.wait_for_timeout(3000)

            # Require a visible publish button in the active post dialog.
            submit = await first_visible(page, [
                dialog.get_by_role("button", name=SUBMIT_BUTTON_RE),
                dialog.get_by_text(SUBMIT_BUTTON_RE),
                page.get_by_role("button", name=SUBMIT_BUTTON_RE),
                page.get_by_text(SUBMIT_BUTTON_RE),
            ], timeout=1500)
            if submit is None:
                return result("failed", "Publish control not found; no post submitted")
            if mode == "nickname":
                if not await identity_shown(page, actual_identity):
                    return result("needs_review",
                                  "Không xác minh được biệt danh trước khi gửi; không đăng bài",
                                  evidence_name, actual_identity, "not_verified")
            else:
                # Hộp thoại giải thích có thể xuất hiện lại; đóng trước khi
                # kiểm tra danh tính lần cuối.
                await dismiss_anonymous_notice(page)
                if not await anonymous_mode_verified(page):
                    return result("needs_review",
                                  "Chế độ ẩn danh mất trước khi gửi; không đăng bài",
                                  evidence_name, actual_identity, "not_verified")
            if not await submit.is_enabled():
                return result("failed", "Publish control disabled; no post submitted")
            was_clicked = True
            if not await click_element(submit, timeout=15000):
                return result("failed", "Không bấm được nút đăng; không đăng bài")
            await page.wait_for_timeout(5000)
            permalink = await newest_permalink(page, str(data.get("body") or ""))
            await page.screenshot(path=str(evidence_path), full_page=False, timeout=15000)
            # Meta may queue the post for moderation, reject it or publish it.
            # Never report definitive success based just on a button click.
            return result("submitted_unverified",
                          f"Đã bấm đăng với danh tính '{actual_identity}'; hãy kiểm tra hàng đợi duyệt của group "
                          "và danh tính hiển thị trên bài", evidence_name, actual_identity, verification,
                          permalink)
    except Exception as error:
        label = "needs_review"
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


async def newest_permalink(page, body: str = "") -> str:
    """The link of the post that was just written, when the page shows it.

    The link is a bonus: when it cannot be found the posting result stays
    exactly as it was. When the text of the post is known, only a link next to
    that text counts, so a pinned or older post is never mistaken for it.
    """
    try:
        return await page.evaluate("""(body) => {
            const links = [...document.querySelectorAll('a[href]')];
            const post = (link) => {
                const href = link.getAttribute('href') || '';
                if (!href.includes('/posts/') && !href.includes('/permalink/')) return '';
                return href.startsWith('http') ? href : new URL(href, location.origin).href;
            };
            const wanted = (body || '').trim();
            if (wanted) {
                for (const link of links) {
                    const href = post(link);
                    if (!href) continue;
                    const holder = link.closest('div[data-testid="post_message"], div[role="article"], article') || link.parentElement;
                    const text = holder ? (holder.innerText || '') : '';
                    if (text.includes(wanted.slice(0, 40))) return href;
                }
            }
            for (const link of links) {
                const href = post(link);
                if (href) return href;
            }
            return '';
        }""", body)
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
        print("Usage: fb_browser.py login <account_id> | check|check-group|post|reel|engagement <request.json> <result.json>", file=sys.stderr)
        raise SystemExit(2)
    payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    outcome = asyncio.run(attempt_post(payload) if sys.argv[1] == "post" else attempt_reel(payload))
    Path(sys.argv[3]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
    print(outcome["state"])


if __name__ == "__main__":
    main()
