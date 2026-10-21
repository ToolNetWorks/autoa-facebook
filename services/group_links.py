"""Normalisation of Facebook group references.

Operators paste all kinds of links: a full URL with tracking parameters, a
group id, a group slug or a mobile link. They all have to end up as one
canonical URL and one comparable key so the same group cannot be added twice.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse, urlunparse

FACEBOOK_HOSTS = ("facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "fb.watch")
GROUP_PATH = re.compile(r"^/groups/([A-Za-z0-9._-]+)/?$")
SHARE_GROUP_PATH = re.compile(r"^/share/g/([A-Za-z0-9._-]+)/?$")

# Parameters that only carry a campaign, never the identity of the group.
TRACKING_PARAMS = {
    "fbclid", "ref", "ref_src", "ref_url", "referrer", "comment_tracking", "mibextid",
    "sharing", "rdr", "sfnsn", "extid", "v", "t", "e", "hc_ref", "hc_[a-z]+_fb",
    "__tn__", "notif_t", "notif_id", "fb_source", "source", "utm_source", "utm_medium",
    "utm_campaign", "utm_term", "utm_content", "utm_id", "utm_name", "utm_reader",
    "utm_brand", "utm_social", "utm_social-type", "fb_action_ids", "fb_action_types",
    "fb_source_ids", "fb_ref", "fb_ref_type", "action_object_map", "action_type_map",
    "action_id_map", "original_subdomain", "igshid", "igsh",
}


class InvalidGroupLink(ValueError):
    """Raised when a group reference cannot be understood."""


def normalize_group_url(raw: str) -> tuple[str, str]:
    """Return the canonical URL and the comparable group key.

    Accepted: a full HTTPS facebook.com group URL, a bare group id, a group
    slug or a mobile link. Tracking parameters and fragments are removed.
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidGroupLink("Link group rỗng")
    if len(text) > 500:
        raise InvalidGroupLink("Link group quá dài")

    if "://" not in text:
        # A bare identifier, for example 123456789 or my-group-name
        key = text.strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", key):
            raise InvalidGroupLink("Chỉ nhận được ID hoặc tên group, hoặc link https://www.facebook.com/groups/...")
        return f"https://www.facebook.com/groups/{key}/", key.lower()

    parsed = urlparse(text)
    if parsed.scheme != "https":
        raise InvalidGroupLink("Chỉ chấp nhận link HTTPS của facebook.com")
    host = (parsed.hostname or "").lower()
    if host not in FACEBOOK_HOSTS:
        raise InvalidGroupLink("Link không thuộc facebook.com")

    path = parsed.path or ""
    matched = GROUP_PATH.match(path)
    if not matched:
        # Facebook shares a group as /share/g/<id>; the identifier is the same.
        shared = SHARE_GROUP_PATH.match(path)
        if not shared:
            raise InvalidGroupLink("Link phải trỏ tới một group: https://www.facebook.com/groups/GROUP_ID")
        key = shared.group(1)
    else:
        key = matched.group(1)

    # A group link is fully identified by its path. Every query parameter of a
    # shared group link is a tracking artefact, so the canonical form has none.
    canonical = urlunparse(("https", "www.facebook.com", f"/groups/{key}/", "", "", ""))
    return canonical, key.lower()


SHARE_CODE_PATTERN = re.compile(r"/share/g/([A-Za-z0-9._-]+)")


def group_alias_keys(raw: str) -> list[str]:
    """Other keys that address the same group.

    Facebook shares a group as /share/g/<code> while the group itself lives
    under /groups/<id>. A shared numeric link carries the share code inside its
    share_url parameter, so both forms can be linked to the same group.
    """
    keys: list[str] = []
    text = unquote(unquote(raw or ""))
    for match in SHARE_CODE_PATTERN.finditer(text):
        code = match.group(1).lower()
        if code not in keys:
            keys.append(code)
    return keys


def sql_like_escape(value: str) -> str:
    """Escape a value for a LIKE pattern."""
    return (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def group_display_name(url: str, fallback: str = "") -> str:
    """A readable label for a group link."""
    parsed = urlparse(url)
    matched = GROUP_PATH.match(parsed.path or "")
    key = matched.group(1) if matched else ""
    if not key:
        return fallback or "Group"
    if key.isdigit():
        return fallback or f"Group {key}"
    return fallback or key.replace("-", " ").replace("_", " ").strip().title()
