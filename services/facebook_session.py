"""Observe authentication without logging cookie values or solving challenges."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

MESSAGES = {
    'connected': 'Facebook đã xác minh phiên đăng nhập.',
    'expired': 'Cần đăng nhập lại Facebook.',
    'checkpoint': 'Facebook yêu cầu xác minh bảo mật; chủ tài khoản cần xử lý thủ công.',
    'restricted': 'Facebook đang hạn chế thao tác; vui lòng kiểm tra trên Facebook.',
    'unknown': 'Chưa xác minh được phiên Facebook. Vui lòng kiểm tra lại.',
}
ACCOUNT_CONTROL = re.compile(r'Tài khoản.*trang cá nhân|Your profile|Your account|Tài khoản của bạn|Ảnh đại diện của bạn|Hồ sơ của bạn', re.I)


async def inspect_session(page: Any, context: Any) -> dict:
    path = urlsplit(page.url).path.lower()
    status = ''
    if any(x in path for x in ('/checkpoint', 'two_step_verification', '/captcha', '/recovery', '/recover')):
        status = 'checkpoint'
    elif any(x in path for x in ('/login', '/logout')):
        status = 'expired'
    if not status:
        inputs = page.locator('input[name="email"], input[name="pass"]')
        if any([await item.is_visible() for item in await inputs.all()]):
            status = 'expired'
    if not status:
        security = page.get_by_role('dialog').filter(has_text=re.compile(
            r'confirm your identity|security check|CAPTCHA|xác minh danh tính|kiểm tra bảo mật', re.I))
        if any([await item.is_visible() for item in await security.all()]):
            status = 'checkpoint'
    if not status:
        blocked = page.locator('[role="dialog"], [role="alert"]').filter(has_text=re.compile(
            r'action blocked|you can.t post right now|temporarily blocked|try again later|tạm thời bị chặn|bạn không thể đăng', re.I))
        if any([await item.is_visible() for item in await blocked.all()]):
            status = 'restricted'
    uid = ''
    if not status:
        cookies = await context.cookies('https://www.facebook.com')
        uid = next((str(c.get('value', '')) for c in cookies if c.get('name') == 'c_user'
                    and str(c.get('value', '')).isdigit()
                    and str(c.get('domain', '')).lstrip('.') == 'facebook.com'), '')
        if not uid:
            status = 'expired'
        else:
            controls = page.get_by_role('button', name=ACCOUNT_CONTROL)
            try:
                await controls.first.wait_for(state='visible', timeout=3000)
                status = 'connected'
            except Exception:
                status = 'unknown'
    return {'status': status, 'details': MESSAGES[status], 'uid': uid if status == 'connected' else ''}
