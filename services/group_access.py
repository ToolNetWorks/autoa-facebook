"""Group navigation evidence; inaccessible content is not a selector failure."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from services.facebook_session import inspect_session
from services.facebook_selectors import current_registry
from services.group_links import FACEBOOK_HOSTS, InvalidGroupLink, normalize_group_url
from services.posting_preflight import same_group


def public_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.hostname or '', parsed.path, '', ''))


def verified_redirect(expected: str, actual: str, response: Any) -> str:
    """Trust only an observed HTTPS Facebook redirect chain, never query aliases."""
    try:
        target, key = normalize_group_url(actual)
        if key.startswith('share:'):
            return ''
        chain = []
        request = response.request if response else None
        while request:
            chain.append(request.url)
            request = request.redirected_from
        if len(chain) < 2 or normalize_group_url(chain[-1])[1] != normalize_group_url(expected)[1]:
            return ''
        if any(urlsplit(url).scheme != 'https' or urlsplit(url).hostname not in FACEBOOK_HOSTS
               or urlsplit(url).username or urlsplit(url).password for url in chain):
            return ''
        if public_url(chain[0]) != public_url(actual):
            return ''
        return target
    except (InvalidGroupLink, ValueError):
        return ''


async def inspect_group_access(page: Any, context: Any, expected: str, response: Any = None) -> dict:
    session = await inspect_session(page, context)
    result = {'code': 'UNKNOWN', 'checked_at': datetime.now(timezone.utc).isoformat(),
              'requested_url': public_url(expected), 'final_url': public_url(page.url),
              'session_status': session['status'], 'accessible': None, 'is_member': None,
              'resolved_url': '', 'resolution_verified': False, 'details': session['details']}
    if session['status'] != 'connected':
        result['code'] = {'expired': 'SESSION_EXPIRED', 'checkpoint': 'CHECKPOINT',
                          'restricted': 'RESTRICTED'}.get(session['status'], 'SESSION_UNKNOWN')
        return result
    text = await current_registry().all(page, 'body').inner_text(timeout=10000)
    if current_registry().pattern('unavailable').search(text):
        result.update(code='CONTENT_UNAVAILABLE', accessible=False,
                      details='Facebook không cho xem nội dung tại URL này. Chưa xác định Group còn tồn tại hoặc quyền truy cập.')
        return result
    redirect = verified_redirect(expected, page.url, response)
    if not same_group(page.url, expected) and not redirect:
        result.update(code='UNVERIFIED_DESTINATION', details='Chưa xác minh được Group sau chuyển hướng; chưa gửi bài.')
        return result
    joined = current_registry().all(page, 'group_joined')
    join = current_registry().all(page, 'group_join')
    if any([await c.is_visible() for c in await joined.all()]):
        result['is_member'] = True
    elif any([await c.is_visible() for c in await join.all()]):
        result['is_member'] = False
    heading = current_registry().all(page, 'heading')
    if not any([await h.is_visible() for h in await heading.all()]):
        result.update(code='GROUP_PAGE_UNCONFIRMED', details='Chưa xác minh được trang Group; vui lòng kiểm tra lại.')
        return result
    result.update(code='ACCESSIBLE', accessible=True, resolved_url=redirect or normalize_group_url(expected)[0],
                  resolution_verified=True, details='Đã mở đúng trang Group; quyền gửi và danh tính cần kiểm tra trong composer.')
    return result
