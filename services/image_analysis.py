"""Analysis of an uploaded album image.

The analysis answers three questions: what is on the picture, is it safe to
publish, and has it been used before. The description is written for the
machine, it never invents a product, a price or a promise that the picture
does not show.
"""
from __future__ import annotations

import logging
from pathlib import Path

from services import ai_provider
from services.media_fingerprint import file_sha256, hamming_distance, perceptual_signature

LOG = logging.getLogger("facebook-pipeline.media-analysis")

STATUS_PASS = "pass"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_BLOCKED = "blocked"
STATUS_UNKNOWN = "unknown"

ANALYSIS_PROMPT = """Bạn mô tả một bức ảnh để dùng cho bài đăng Facebook.
Trả về đúng một đối tượng JSON:
{"description": "mô tả ngắn, chỉ những gì thực sự thấy, không bịa sản phẩm, giá hay cam kết",
 "tags": ["từ khóa nội bộ", "..."],
 "risk": "none|low|medium|high",
 "category": "none|violence|blood|sexual|harassment|dangerous|offensive_text|misleading|watermark|low_quality|none",
 "note": "giải thích ngắn nếu có rủi ro",
 "people": true, "text_in_image": "nội dung chữ thấy được hoặc rỗng"}
Nếu ảnh không cho thấy gì thì dùng "none"."""

RISK_STATUS = {"none": STATUS_PASS, "low": STATUS_PASS, "medium": STATUS_NEEDS_REVIEW,
               "high": STATUS_BLOCKED}
RISK_CATEGORY = {"violence", "blood", "sexual", "harassment", "dangerous", "misleading"}


def analyze_image(path: Path, provider_config=None, known_media: list[dict] | None = None) -> dict:
    """Describe the image, judge the risk and look for duplicates."""
    provider_config = provider_config or ai_provider.load_config()
    result = {
        "status": STATUS_UNKNOWN, "description": "", "tags": [], "risk": "none",
        "category": "none", "note": "", "people": False, "text_in_image": "",
        "duplicate_of": None, "near_duplicate_of": None, "model_version": "",
        "error": "", "checked": False,
    }
    try:
        result["sha256"] = file_sha256(path)
        result["signature"] = perceptual_signature(path)
    except OSError as error:
        result["error"] = type(error).__name__
        return result

    # a picture that is already in the album is a duplicate
    for item in known_media or []:
        if item.get("sha256") and item["sha256"] == result["sha256"]:
            result["duplicate_of"] = item.get("storage_key") or item.get("id")
            result["status"] = STATUS_NEEDS_REVIEW
            result["note"] = "Ảnh đã có trong Album"
            result["checked"] = True
            return result
        if (item.get("signature") and result.get("signature")
                and hamming_distance(result["signature"], item["signature"]) <= 10):
            result["near_duplicate_of"] = item.get("storage_key") or item.get("id")

    if not provider_config.vision_configured:
        result["status"] = STATUS_UNKNOWN
        result["error"] = "Chưa cấu hình model phân tích hình ảnh (AI_VISION_MODEL)"
        result["checked"] = True
        return result
    try:
        answer = ai_provider.vision_json(
            "Bạn phân tích ảnh để chuẩn bị bài đăng Facebook.", ANALYSIS_PROMPT, [path], provider_config)
    except Exception as error:
        result["status"] = STATUS_UNKNOWN
        result["error"] = type(error).__name__
        result["checked"] = True
        return result

    risk = str(answer.get("risk", "none")).lower()
    category = str(answer.get("category", "none")).lower()
    result.update({
        "description": str(answer.get("description", ""))[:600],
        "tags": [str(tag)[:40] for tag in (answer.get("tags") or [])][:12],
        "risk": risk if risk in RISK_STATUS else "low",
        "category": category,
        "note": str(answer.get("note", ""))[:400],
        "people": bool(answer.get("people")),
        "text_in_image": str(answer.get("text_in_image", ""))[:400],
        "model_version": provider_config.vision_model,
        "checked": True,
    })
    if category in RISK_CATEGORY:
        result["status"] = STATUS_BLOCKED
    elif risk == "medium" or result["near_duplicate_of"]:
        result["status"] = STATUS_NEEDS_REVIEW
    else:
        result["status"] = STATUS_PASS
    return result
