"""Observe submission evidence. Closing a composer never proves publication."""
from __future__ import annotations

import asyncio
from typing import Any

PUBLISHED_NOTICES = (
    'text=Your post is now published', 'text=Bài viết của bạn hiện đã được đăng',
    'text=Đã đăng bài viết của bạn', 'text=Post published',
)
PENDING_NOTICES = (
    'text=Sent for approval', 'text=Đã gửi để phê duyệt',
    'text=Đang chờ quản trị viên duyệt', 'text=Đang chờ duyệt',
    'text=Đã gửi để duyệt', 'text=Hệ thống đã gửi bài viết cho quản trị viên nhóm',
    'text=Your post is pending approval',
)
REJECTED_NOTICES = (
    "text=You can't post in this group", 'text=Bạn không thể đăng bài trong nhóm này',
    'text=Your post was rejected', 'text=Bài viết của bạn đã bị từ chối',
)


def notice_target(page: Any, selector: str) -> Any:
    return page.locator(selector).and_(page.locator(
        '[role="status"], [role="status"] *, [role="alert"], [role="alert"] *, '
        '[role="dialog"], [role="dialog"] *'))


async def visible_notices(page: Any) -> set[str]:
    visible = set()
    for selector in PUBLISHED_NOTICES + PENDING_NOTICES + REJECTED_NOTICES:
        target = notice_target(page, selector)
        if await target.count() and await target.first.is_visible():
            visible.add(selector)
    return visible


async def wait_post_outcome(page: Any, body: str = '', *, timeout: int = 15000,
                            prior_notices: set[str] | None = None) -> dict:
    """Watch all labels concurrently; an absent label cannot cancel a later one.

    Caller must start this before Submit. A published notice is only a signal:
    the runner also requires a new content-matched Group permalink for POSTED.
    """
    async def watch(selector: str, state: str) -> dict | None:
        try:
            if selector in (prior_notices or set()):
                return None
            target = notice_target(page, selector)
            await target.first.wait_for(state='visible', timeout=timeout)
            return {'state': state, 'details': {
                'published': 'Facebook hiển thị thông báo đã đăng; cần đối chiếu bài viết.',
                'pending_approval': 'Facebook đã nhận bài và đang chờ quản trị group duyệt.',
                'rejected': 'Facebook từ chối bài đăng trong group này.',
            }[state], 'permalink': ''}
        except Exception:
            return None

    tasks = [asyncio.create_task(watch(selector, state))
             for selectors, state in ((PUBLISHED_NOTICES, 'published'),
                                      (PENDING_NOTICES, 'pending_approval'),
                                      (REJECTED_NOTICES, 'rejected')) for selector in selectors]
    try:
        for future in asyncio.as_completed(tasks, timeout=timeout / 1000):
            outcome = await future
            if outcome:
                return outcome
    except asyncio.TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {'state': 'ambiguous', 'details': 'Chưa xác minh được kết quả; không tự gửi lại để tránh đăng trùng.',
            'permalink': ''}


async def group_post_links(page: Any) -> list[str]:
    """Snapshot links before Submit so old posts cannot verify this attempt."""
    return await page.locator('a[href]').evaluate_all("""links => links.map(a => a.href)
        .filter(h => /\\/groups\\/[^/]+\\/(posts|permalink|pending_posts)\\//.test(h))""")


async def newest_permalink(page: Any, body: str = '', *, expected_group: str = '',
                           exclude: list[str] | None = None) -> str:
    """Require full caption, image and new same-group URL in one visible article.

    /posts/, /permalink/ and /pending_posts/ are distinct evidence types.
    """
    return await page.evaluate("""({body, expectedGroup, exclude}) => {
        const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
        const wanted = norm(body);
        if (!wanted) return '';
        const target = new URL(expectedGroup || location.href);
        const group = target.pathname.match(/^\\/groups\\/([^/]+)/)?.[1];
        if (!group) return '';
        const old = new Set(exclude.map(h => { const u = new URL(h); return u.origin + u.pathname; }));
        const matches = new Set();
        for (const article of document.querySelectorAll('[role="main"] [role="article"], [role="feed"] [role="article"], main article')) {
            if (!article.getClientRects().length || !norm(article.innerText).includes(wanted)) continue;
            if (![...article.querySelectorAll('img')].some(img => img.complete && img.naturalWidth > 0)) continue;
            for (const link of article.querySelectorAll('a[href]')) {
                const u = new URL(link.href);
                if (!['www.facebook.com','web.facebook.com','m.facebook.com','facebook.com'].includes(u.hostname)) continue;
                const m = u.pathname.match(/^\\/groups\\/([^/]+)\\/(posts|permalink|pending_posts)\\/([^/]+)\\/?$/);
                if (!m || m[1] !== group || old.has(u.origin + u.pathname)) continue;
                matches.add(u.origin + u.pathname);
            }
        }
        return matches.size === 1 ? [...matches][0] : '';
    }""", {'body': body, 'expectedGroup': expected_group, 'exclude': exclude or []})
