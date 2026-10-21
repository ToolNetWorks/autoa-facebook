"""The automatic group posting of a workspace.

The rule of the module is simple and never broken: one post is one picture,
one caption and the mode that was configured for the group. The picture comes
from the album of the workspace, the caption is written or optimised with the
help of the model, and every post has to pass the safety checks before it is
queued.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from services import ai_provider
from services.caption_validator import analyze_caption
from services.image_selection import select_media

LOG = logging.getLogger("facebook-pipeline.auto-post")

MODE_ANONYMOUS = "anonymous"
MODE_NICKNAME = "nickname"

CAPTION_PROMPT = """Viết một caption Facebook bằng tiếng Việt dựa trên mô tả ảnh và chủ đề.
Bắt buộc:
- Chỉ dùng những gì có trong mô tả ảnh. Không bịa tên sản phẩm, giá, chính sách hay cam kết.
- Không dùng ký tự đặc biệt, mã hóa hay biến thể chính tả để né bộ lọc kiểm duyệt.
- Caption tự nhiên, ngắn gọn, đúng chủ đề.
Trả về đúng một đối tượng JSON: {"caption": "...", "hashtags": ["..."], "reason": "..."}"""


def eligible_groups(groups: list[dict], account_id: int | None = None) -> list[dict]:
    """The groups of a workspace that may be served automatically.

    Only a group that was verified as publishing directly or as needing an
    approval qualifies, and it has to be active with a confirmed permission.
    """
    result = []
    for group in groups:
        if group.get("screening_status") not in ("priority", "review"):
            continue
        if group.get("status") != "active" or not group.get("enabled"):
            continue
        if not group.get("approved_target"):
            continue
        if account_id is not None and group.get("account_id") != account_id:
            continue
        result.append(group)
    result.sort(key=lambda item: (0 if item.get("screening_status") == "priority" else 1, item.get("id", 0)))
    return result


def create_caption(description: str, tags: list[str], topic: str,
                   provider_config=None) -> dict:
    """Write a caption for the picture and check it."""
    provider_config = provider_config or ai_provider.load_config()
    result = {"caption": "", "source": "topic", "status": "pass", "reasons": [], "hashtags": []}
    subject = (topic or "").strip()
    if not subject and description:
        subject = description[:120]
    if not subject:
        result["status"] = "needs_review"
        result["reasons"].append("Chưa có chủ đề và ảnh chưa được mô tả")
        return result
    if provider_config.configured:
        try:
            answer = ai_provider.chat_json(
                "Bạn viết caption Facebook bằng tiếng Việt.",
                CAPTION_PROMPT + f"\nMô tả ảnh: {description[:800]}\n"
                f"Thẻ: {', '.join(tags[:10])}\nChủ đề: {subject}\n"
                f"Chế độ đăng: {'ẩn danh' if MODE_ANONYMOUS else 'biệt danh'}",
                provider_config)
            caption = str(answer.get("caption", "")).strip()
            hashtags = [str(tag)[:40] for tag in (answer.get("hashtags") or [])][:8]
            if caption:
                result["caption"] = caption
                result["source"] = "ai"
                result["hashtags"] = hashtags
            if answer.get("reason"):
                result["reasons"].append(str(answer["reason"])[:200])
        except Exception as error:
            result["reasons"].append(f"Không viết được caption bằng AI: {type(error).__name__}")
            result["status"] = "unknown"
    if not result["caption"]:
        result["caption"] = subject
    analysis = analyze_caption(result["caption"])
    if any(issue.severity == "high" for issue in analysis.issues):
        result["status"] = "blocked"
    elif analysis.issues and result["status"] == "pass":
        result["status"] = "needs_review"
    for issue in analysis.issues:
        result["reasons"].append(f"{issue.category}: {issue.message}")
    if not result["reasons"]:
        result["reasons"].append("Caption không có dấu hiệu spam hay vi phạm")
    return result


def plan_post(groups: list[dict], candidates: list[dict], topic: str, provider_config=None,
              ai_caption: bool = True) -> dict:
    """Build the preview of one post per eligible group."""
    provider_config = provider_config or ai_provider.load_config()
    chosen = select_media(candidates, topic)
    plan = {
        "media": chosen["media"], "selection_score": chosen["score"],
        "selection_reasons": chosen["reasons"], "candidates": chosen["candidates"],
        "caption": "", "caption_status": "unknown", "caption_reasons": [],
        "posts": [], "notes": [],
    }
    if not chosen["media"]:
        plan["notes"].append("Album chưa có ảnh phù hợp, cần bổ sung ảnh")
        return plan
    media = chosen["media"]
    description = media.get("description") or ""
    tags = media.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = []
    caption_result = create_caption(description, list(tags), topic, provider_config) if ai_caption else {
        "caption": topic or description[:200], "source": "topic", "status": "pass",
        "reasons": ["Dùng chủ đề người dùng nhập"], "hashtags": []}
    plan["caption"] = caption_result["caption"]
    plan["caption_status"] = caption_result["status"]
    plan["caption_reasons"] = caption_result["reasons"]
    plan["caption_source"] = caption_result["source"]
    for group in groups:
        mode = group.get("posting_mode") or MODE_ANONYMOUS
        if mode == MODE_NICKNAME and not (group.get("nickname") or "").strip():
            plan["posts"].append({"group_id": group.get("id"), "group_name": group.get("label"),
                                  "posting_mode": mode, "skipped": "Chưa cấu hình biệt danh"})
            continue
        if caption_result["status"] == "blocked":
            plan["posts"].append({"group_id": group.get("id"), "group_name": group.get("label"),
                                  "posting_mode": mode, "skipped": "Caption bị chặn vì nội dung rủi ro"})
            continue
        plan["posts"].append({"group_id": group.get("id"), "group_name": group.get("label"),
                               "account_id": group.get("account_id"), "account_label": group.get("account_label"),
                               "posting_mode": mode, "nickname": group.get("nickname") or "",
                               "screening_status": group.get("screening_status"),
                               "skipped": ""})
    return plan


def idempotency_key(pipeline_id: int, config_id: int, media_id: int, group_id: int,
                     day: str) -> str:
    """The key that keeps the scheduler from creating the same post twice."""
    return f"{int(pipeline_id)}:{int(config_id)}:{int(media_id)}:{int(group_id)}:{day}"


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
