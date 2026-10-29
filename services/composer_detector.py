"""Composer controls resolved inside the active dialog; never page-wide Submit."""
from __future__ import annotations

import re
from typing import Any
from services.facebook_selectors import current_registry, active_composer_dialog


def composer_regex() -> re.Pattern:
    return re.compile(r"Write something|Viết gì đó|Bạn viết gì|Hãy viết gì đó|Bạn đang nghĩ gì|What's on your mind", re.I)


def publish_label_regex() -> re.Pattern:
    return re.compile(r'^(Đăng|Post|Gửi|Send|Đăng bài|Publish)$', re.I)


async def find_composer(page: Any, timeout: int = 2500, *, posting_mode: str = "") -> Any | None:
    registry = current_registry()
    if posting_mode == "anonymous":
        dedicated = await registry.resolve(page, 'anonymous_composer', timeout=200)
        if dedicated is not None:
            return dedicated
    return await registry.resolve(page, 'composer', timeout=timeout)


async def find_editor(page: Any, dialog: Any, timeout: int = 7000) -> Any | None:
    if dialog is None:
        return None
    return await current_registry().resolve(dialog, 'editor', timeout=timeout)


async def find_publish_button(page: Any, dialog: Any, timeout: int = 2500) -> Any | None:
    if dialog is None:
        return None
    return await current_registry().resolve(dialog, 'publish', timeout=timeout)


async def find_media_button(page: Any, dialog: Any, timeout: int = 5000) -> Any | None:
    if dialog is None:
        return None
    return await current_registry().resolve(dialog, 'media', timeout=timeout)
