"""Observe authentication without logging cookie values or solving challenges."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit
from services.facebook_selectors import current_registry

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
        inputs = current_registry().all(page, 'login_inputs')
        if any([await item.is_visible() for item in await inputs.all()]):
            status = 'expired'
    if not status:
        security = current_registry().all(page, 'dialogs').filter(has_text=current_registry().pattern('security'))
        if any([await item.is_visible() for item in await security.all()]):
            status = 'checkpoint'
    if not status:
        blocked = current_registry().all(page, 'restriction_scope').filter(has_text=current_registry().pattern('restriction'))
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
            controls = current_registry().all(page, 'account_control')
            try:
                await controls.first.wait_for(state='visible', timeout=3000)
                status = 'connected'
            except Exception:
                status = 'unknown'
    return {'status': status, 'details': MESSAGES[status], 'uid': uid if status == 'connected' else ''}
