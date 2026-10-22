"""Xác minh kết quả bài đăng sau khi nhấn nút Đăng.

Không bao giờ kết luận thành công chỉ vì đã nhấn nút. Các kết quả có thể:

* ``published`` — Facebook xác nhận bài đã đăng (thông báo rõ ràng).
* ``pending_approval`` — Facebook xác nhận bài đang chờ quản trị group duyệt.
* ``rejected`` — Facebook từ chối bài đăng.
* ``ambiguous`` — không xác nhận được. Bài **không** được gửi lại, vì có thể
  đã đăng rồi.
"""
from __future__ import annotations

import asyncio
import logging
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

_LOGGER = logging.getLogger(__name__)

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
    bấm nút. Hộp thoại đóng KHÔNG tự động nghĩa là bài đã đăng.

    Quan sát đồng thời các trạng thái PUBLISHED/PENDING/REJECTED để không bỏ lỡ
    thông báo xuất hiện ngắn. Quan sát bắt đầu trước hoặc đồng thời với Submit.
    """
    outcome = {"state": "ambiguous", "details": "", "permalink": ""}
    
    # Tạo các task quan sát đồng thời
    async def watch_notices(notices: tuple[str, ...], state: str, details: str):
        for selector in notices:
            try:
                target = page.locator(selector).first
                await target.wait_for(state="visible", timeout=2500)
                return {"state": state, "details": details}
            except Exception:
                continue
        return None
    
    # Khởi động quan sát đồng thời trước khi/dồng thời với Submit
    published_task = asyncio.create_task(watch_notices(PUBLISHED_NOTICES, "published", "Facebook xác nhận bài đã đăng."))
    pending_task = asyncio.create_task(watch_notices(PENDING_NOTICES, "pending_approval", "Facebook đã nhận bài và đang chờ quản trị group duyệt."))
    rejected_task = asyncio.create_task(watch_notices(REJECTED_NOTICES, "rejected", "Facebook từ chối bài đăng trong group này."))
    
    # Chờ kết quả từ bất kỳ task nào hoàn thành trước
    done, pending = await asyncio.wait(
        [published_task, pending_task, rejected_task],
        return_when=asyncio.FIRST_COMPLETED,
        timeout=15000  # Timeout chung hợp lý
    )
    
    # Hủy các task còn lại
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    
    # Xử lý kết quả
    for task in done:
        try:
            result = task.result()
            if result:
                return result
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    
    # Không có notice rõ ràng -> quan sát dialog đóng (KHÔNG = published)
    try:
        dialog = page.get_by_role("dialog").last
        await dialog.wait_for(state="hidden", timeout=12000)
        return {"state": "ambiguous", "details": ("Hộp thoại tạo bài đã đóng nhưng không có thông báo xác nhận; "
                              "kết quả không xác định, không tự gửi lại."), "permalink": ""}
    except Exception:
        return {"state": "ambiguous", "details": ("Không xác nhận được kết quả sau khi bấm Đăng; "
                              "không tự gửi lại để tránh đăng trùng."), "permalink": ""}


async def newest_permalink(page: Page, body: str = "") -> str:
    """Liên kết của bài vừa viết, khi trang hiển thị nó.

    Chỉ tìm liên kết trong ngữ cảnh Group (feed, timeline, articles).
    Không lấy liên kết từ profile cá nhân, sidebar, header, v.v.
    Nếu không tìm thấy link khớp nội dung trong Group, trả về rỗng.
    """
    try:
        result = await page.evaluate("""(body) => {
            const wanted = (body || '').trim();
            if (!wanted) return '';
            
            // Chỉ tìm trong vùng nội dung chính của Group (feed, timeline)
            const mainContent = document.querySelector('[role="main"], [data-pagelet="FeedUnit_0"], [role="feed"], div[data-pagelet^="FeedUnit"]');
            if (!mainContent) return '';
            
            const linksInScope = [...mainContent.querySelectorAll('a[href]')];
            const post = (link) => {
                const href = link.getAttribute('href') || '';
                if (!href.includes('/posts/') && !href.includes('/permalink/')) return '';
                return href.startsWith('http') ? href : new URL(href, location.origin).href;
            };
            
            // Tìm link khớp nội dung caption (ưu tiên bài mới - thường ở đầu)
            for (const link of [...linksInScope].reverse()) {
                const href = post(link);
                if (!href) continue;
                const holder = link.closest('[data-testid="post_message"], [role="article"], article, [data-pagelet^="FeedUnit"]') || link.parentElement;
                if (!holder) continue;
                const text = holder.innerText || '';
                if (text.includes(wanted.slice(0, 80))) {
                    return href;
                }
            }
            // Không tìm thấy bài khớp nội dung -> trả rỗng (KHÔNG fallback)
            return '';
        }""", body)
    except Exception as e:
        _LOGGER.warning("newest_permalink failed: %s", e)
        return ""
    return result