"""Observe submission evidence. Closing a composer never proves publication."""
from __future__ import annotations

import asyncio
from typing import Any

from services.facebook_selectors import current_registry


def notice_target(page: Any, selector: str) -> Any:
    return page.locator(selector).and_(current_registry().all(page, 'notice_scope'))


async def visible_notices(page: Any) -> set[str]:
    visible = set()
    for selector in sum((current_registry().notices[key] for key in ('published','pending','rejected')), []):
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
             for selectors, state in ((current_registry().notices['published'], 'published'),
                                      (current_registry().notices['pending'], 'pending_approval'),
                                      (current_registry().notices['rejected'], 'rejected')) for selector in selectors]
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
    return await current_registry().all(page, 'links').evaluate_all("""links => links.map(a => a.href)
        .filter(h => /\\/groups\\/[^/]+\\/(posts|permalink|pending_posts)\\//.test(h))""")


async def newest_permalink(page: Any, body: str = '', *, expected_group: str = '',
                           exclude: list[str] | None = None, expected_image=None,
                           submitted_at: float | None = None, expected_identity: str = '') -> str:
    """Compatibility adapter; missing target evidence intentionally returns no URL."""
    from services.post_evidence import verify_post
    evidence = await verify_post(page, body, expected_group=expected_group,
        expected_image=expected_image, submitted_at=submitted_at,
        expected_identity=expected_identity, exclude=exclude)
    return evidence['permalink']


def __getattr__(name):
    """Legacy exports resolve through the pinned registry, not a second rule set."""
    mapping = {'PUBLISHED_NOTICES':'published', 'PENDING_NOTICES':'pending', 'REJECTED_NOTICES':'rejected'}
    if name in mapping:
        return tuple(current_registry().notices[mapping[name]])
    raise AttributeError(name)
