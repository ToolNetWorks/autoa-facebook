"""Read-only checks on the current browser page before any submission."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse
from services.group_links import normalize_group_url


def same_group(actual: str, expected: str) -> bool:
    try:
        parsed = urlparse(actual)
        _, expected_key = normalize_group_url(expected)
        path = re.match(r'^(/groups/[^/]+)(?:/|$)', parsed.path)
        if not path:
            return False
        actual_url = parsed._replace(path=path[1] + '/', params='', query='', fragment='').geturl()
        _, actual_key = normalize_group_url(actual_url)
        return actual_key == expected_key and not expected_key.startswith('share:')
    except ValueError:
        return False


async def session_problem(page: Any, context: Any) -> str:
    from services.facebook_session import inspect_session
    result = await inspect_session(page, context)
    return "" if result["status"] == "connected" else result["details"]
