"""Strict import of operator-provided Facebook session cookies.

Never log or persist raw JSON uploads; only Chromium's private browser profile
stores the resulting cookies. Possession of a cookie file grants account access.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_COOKIE_BYTES = 256 * 1024
MAX_COOKIES = 250


class InvalidCookies(ValueError):
    pass


def facebook_domain(value: str) -> bool:
    domain = value.lstrip(".").lower()
    return domain == "facebook.com" or domain.endswith(".facebook.com")


def parse_cookie_file(content: bytes) -> tuple[list[dict[str, Any]], str]:
    if not content or len(content) > MAX_COOKIE_BYTES:
        raise InvalidCookies("File JSON trống hoặc vượt 256 KB")
    try:
        document = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InvalidCookies("File không phải JSON UTF-8 hợp lệ") from error
    items = document.get("cookies") if isinstance(document, dict) else document
    if not isinstance(items, list) or not (1 <= len(items) <= MAX_COOKIES):
        raise InvalidCookies("JSON cần là mảng cookies hoặc {cookies: [...]} (tối đa 250)")

    cleaned: list[dict[str, Any]] = []
    found: dict[str, str] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise InvalidCookies(f"Cookie #{index+1} không hợp lệ")
        name, value, domain = item.get("name"), item.get("value"), item.get("domain", ".facebook.com")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]{1,128}", name):
            raise InvalidCookies("Tên cookie không hợp lệ")
        if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
            raise InvalidCookies("Giá trị cookie không hợp lệ")
        if not isinstance(domain, str) or not facebook_domain(domain):
            raise InvalidCookies("File chứa cookie ngoài miền facebook.com")
        path = item.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
            raise InvalidCookies("Đường dẫn cookie không hợp lệ")
        cookie: dict[str, Any] = {
            "name": name, "value": value, "domain": domain.lower(), "path": path,
            "secure": bool(item.get("secure", True)), "httpOnly": bool(item.get("httpOnly", False)),
        }
        same_site = item.get("sameSite")
        if isinstance(same_site, str):
            mapped = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None"}.get(same_site.lower())
            if mapped:
                cookie["sameSite"] = mapped
        expiration = next((item[k] for k in ("expires", "expirationDate", "expiry", "expiresDate") if k in item), None)
        if isinstance(expiration, (int, float)) and math.isfinite(expiration) and expiration > 0:
            # Chromium's 'expires' is seconds since epoch, not milliseconds.
            cookie["expires"] = float(expiration / 1000 if expiration > 1e11 else expiration)
        cleaned.append(cookie)
        if name in ("c_user", "xs") and domain.lstrip(".").lower() == "facebook.com":
            found[name] = value
    if not found.get("c_user") or not found.get("xs"):
        raise InvalidCookies("Thiếu cookie c_user hoặc xs trên facebook.com")
    if not found["c_user"].isdigit():
        raise InvalidCookies("c_user không hợp lệ")
    return cleaned, found["c_user"]
