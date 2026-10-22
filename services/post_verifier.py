"""Xác minh kết quả bài đăng sau khi nhấn nút Đăng.

Không bao giờ kết luận thành công chỉ vì đã nhấn nút. Ba kết quả có thể:

* ``published`` — Facebook xác nhận bài đã đăng (thông báo hoặc hộp thoại
  composer đóng).
* ``pending_approval`` — Facebook xác nhận bài đang chờ quản trị group duyệt.
* ``rejected`` — Facebook từ chối bài đăng.
* ``ambiguous`` — không xác nhận được. Bài **không** được gửi lại, vì có thể
  đã đăng rồi.
"""
from __future__ import annotations

from playwright.async_api import Page

#: Thông báo Facebook khi bài đã đăng công khai.
PUBLISHED_NOTICES = (
    "text=Your post is now published",
    "text=Bài viết của bạn hiện đã được đăng",
    "text=Đã đăng bài viết của bạn",
    "text=Post published",
)
#: Thông báo Facebook khi bài đang chờ quản trị group duyệt.
PENDING_NOTICES = (
    "text=Sent for approval",
    "text=Đã gửi để phê duyệt",
    "text=Đang chờ quản trị viên duyệt",
    "text=Đang chờ duyệt",
    "text=Đã gửi để duyệt",
)
#: Thông báo Facebook khi bài bị từ chối.
REJECTED_NOTICES = (
    "text=You can't post in this group",
    "text=Bạn không thể đăng bài trong nhóm này",
    "text=Your post was rejected",
    "text=Bài viết của bạn đã bị từ chối",
)


async def _first_notice_visible(page: Page, notices: tuple[str, ...]) -> bool:
    for selector in notices:
        try:
            target = page.locator(selector).first
            await target.wait_for(state="visible", timeout=2500)
            return True
        except Exception:
            continue
    return False


async def wait_post_outcome(page: Page, body: str = "") -> dict:
    """Quan sát kết quả thật sau khi bấm Đăng.

    Trả về dict có ``state`` (published/pending_approval/rejected/ambiguous),
    ``details`` và ``permalink``. Không có kết quả nào được rút ra từ việc đã
    bấm nút.
    """
    outcome = {"state": "ambiguous", "details": "", "permalink": ""}
    try:
        if await _first_notice_visible(page, REJECTED_NOTICES):
            outcome["state"] = "rejected"
            outcome["details"] = "Facebook từ chối bài đăng trong group này."
            return outcome
        if await _first_notice_visible(page, PENDING_NOTICES):
            outcome["state"] = "pending_approval"
            outcome["details"] = "Facebook đã nhận bài và đang chờ quản trị group duyệt."
            return outcome
        if await _first_notice_visible(page, PUBLISHED_NOTICES):
            outcome["state"] = "published"
            outcome["details"] = "Facebook xác nhận bài đã đăng."
            return outcome
    except Exception:
        pass
    # Hộp thoại composer đóng lại là dấu hiệu Facebook đã nhận bài.
    try:
        dialog = page.get_by_role("dialog").last
        await dialog.wait_for(state="hidden", timeout=12000)
        outcome["state"] = "published"
        outcome["details"] = "Hộp thoại tạo bài đã đóng, Facebook đã nhận bài."
    except Exception:
        outcome["details"] = ("Không xác nhận được kết quả sau khi bấm Đăng; "
                              "không tự gửi lại để tránh đăng trùng.")
    return outcome


async def newest_permalink(page: Page, body: str = "") -> str:
    """Liên kết của bài vừa viết, khi trang hiển thị nó.

    Là thêm vào: khi không tìm thấy kết quả giữ nguyên. Khi biết văn bản bài
    chỉ tính liên kết bên cạnh văn bản đó, để không nhầm với bài gắn hoặc cũ.
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
