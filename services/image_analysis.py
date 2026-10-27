"""Analysis of an uploaded album image.

The analysis answers three questions: what is on the picture, is it safe to
publish, and has it been used before. Moderation is performed by AWS
Rekognition (DetectModerationLabels). A lightweight description/label pass uses
the AI provider only when AWS has already cleared the content.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from services import ai_provider
from services.ai_provider import ProviderError
from services.aws_rekognition import moderation as aws_moderation
from services.aws_rekognition.exceptions import (
    AccessDeniedError,
    ProviderTimeoutError,
    ThrottlingError,
)
from services.aws_rekognition.mapper import map_moderation_labels
from services.media_fingerprint import file_sha256, hamming_distance, perceptual_signature

LOG = logging.getLogger("facebook-pipeline.media-analysis")

STATUS_PASS = "pass"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_BLOCKED = "blocked"
STATUS_UNKNOWN = "unknown"
STATUS_CHECK_FAILED = "check_failed"

HIGH_RISK = {"high", "blocked"}
BLOCK_LABELS = {"explicit_nudity", "sexual_content", "violence", "gore",
                "torture", "threat", "hate_symbols", "child_safety",
                "violence_depicts", "sexual_acts", "suggestive", "partial_nudity"}
REVIEW_LABELS = {"revealing_swimwear", "male_swimwear", "female_swimwear",
                 "male_underwear", "female_underwear", "female_bodily_regions",
                 "male_bodily_regions", "skateboard", "drugs", "tobacco",
                 "alcohol", "rude_gestures", "crude_gestures", "gambling",
                 "hate_groups", "nazi_symbol", "white_supremacist",
                 "confederate_flag", "extremist_flags", "middle_finger",
                 "offensive_gestures", "medical_instruments", "blood",
                 "corpse_violence", "skeletal_dissection", "surgery",
                 "medical_procedures", "inner_body", "skeletal_material",
                 "body_part", "buttocks_showing", "female_breast_showing",
                 "male_breast_showing", "female_genital_showing",
                 "male_genital_showing"}

ANALYSIS_PROMPT = """Bạn mô tả một bức ảnh để dùng cho bài đăng Facebook.
Trả về đúng một đối tượng JSON:
{"description": "mô tả ngắn, chỉ những gì thực sự thấy, không bịa sản phẩm, giá hay cam kết",
 "tags": ["từ khóa nội bộ", "..."],
 "text_in_image": "nội dung chữ thấy được hoặc rỗng"}
Nếu ảnh không cho thấy gì thì dùng rỗng."""


def _error_text(error: Exception) -> str:
    """Một mô tả lỗi ngắn, không lộ chi tiết nội bộ ra giao diện."""
    if isinstance(error, ProviderError):
        return "Lỗi nhà cung cấp dịch vụ"
    if isinstance(error, ProviderTimeoutError):
        return "Dịch vụ phân tích ảnh quá thời gian chờ."
    if isinstance(error, ThrottlingError):
        return "Dịch vụ bị giới hạn tạm thời; vui lòng thử lại."
    if isinstance(error, AccessDeniedError):
        return "Dịch vụ phân tích ảnh chưa được cấu hình."
    return "Lỗi kiểm tra ảnh"


def _resize_for_rekognition(path: Path, max_bytes: int = 5 * 1024 * 1024) -> bytes:
    """Read image bytes, downscaling large images to Rekognition's JPEG limit."""
    from PIL import Image
    import io
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return data
    try:
        with Image.open(io.BytesIO(data)) as img:
            ratio = min(1.0, (max_bytes / len(data)) ** 0.5)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            buffer = io.BytesIO()
            img.resize(new_size, Image.Resampling.LANCZOS).save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()
    except Exception:
        return data


def _reko_moderation(path: Path):
    """Run AWS Rekognition moderation on image bytes.

    Returns ``(labels, error)`` where *labels* is the list of normalized
    findings and *error* is an internal error class name or ``""``.
    """
    try:
        from services.aws_rekognition.client import client_from_env
        from services.aws_rekognition.frame_processor import JPEG_MAX_BYTES
        client = client_from_env()
        if client is None:
            return [], "AWS_REKOGNITION_NOT_ENABLED"
        image_bytes = _resize_for_rekognition(path, JPEG_MAX_BYTES)
        labels = aws_moderation.analyze_frame_moderation(client, image_bytes)
        findings = map_moderation_labels(labels)
        return findings, ""
    except AccessDeniedError:
        LOG.warning("AWS AccessDenied for image moderation: %s", path.name)
        return [], "AccessDeniedError"
    except ThrottlingError:
        LOG.warning("AWS throttle for image moderation: %s", path.name)
        return [], "ThrottlingError"
    except ProviderTimeoutError:
        LOG.warning("AWS timeout for image moderation: %s", path.name)
        return [], "TimeoutError"
    except Exception as error:
        LOG.warning("AWS moderation error for %s: %s", path.name, type(error).__name__)
        return [], type(error).__name__


def analyze_image(path: Path, provider_config=None, known_media: list[dict] | None = None) -> dict:
    """Describe the image, judge the risk and look for duplicates.

    Moderation is performed by AWS Rekognition. A lightweight description
    is only generated when AWS reports the image as safe.
    """
    result = {
        "status": STATUS_UNKNOWN, "description": "", "tags": [], "risk": "none",
        "category": "none", "note": "", "people": False, "text_in_image": "",
        "duplicate_of": None, "near_duplicate_of": None, "evidence": "",
        "model_version": "", "error": "", "checked": False,
    }
    try:
        result["sha256"] = file_sha256(path)
        result["signature"] = perceptual_signature(path)
    except OSError as error:
        result["error"] = _error_text(error)
        result["checked"] = True
        return result

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

    findings, reko_error = _reko_moderation(path)
    if reko_error:
        if reko_error == "AWS_REKOGNITION_NOT_ENABLED":
            result["error"] = "Dịch vụ kiểm tra an toàn chưa được bật"
        else:
            result["error"] = _error_text(RuntimeError(reko_error))
        result["status"] = STATUS_CHECK_FAILED
        result["checked"] = True
        return result

    if findings:
        highest = max(findings, key=lambda f: f.get("confidence", 0))
        category = highest.get("label", "").lower()
        note = highest.get("evidence", "")
        result.update({
            "risk": "high",
            "category": category,
            "note": note[:400],
            "evidence": json.dumps(findings, ensure_ascii=False),
            "status": STATUS_BLOCKED,
            "checked": True,
        })
        return result

    result.update({
        "risk": "none",
        "status": STATUS_PASS,
        "checked": True,
    })

    provider_config = provider_config or ai_provider.load_config()
    if provider_config.vision_configured:
        try:
            answer = ai_provider.vision_json(
                "Bạn phân tích ảnh để chuẩn bị bài đăng Facebook.", ANALYSIS_PROMPT, [path], provider_config)
            result.update({
                "description": str(answer.get("description", ""))[:600],
                "tags": [str(tag)[:40] for tag in (answer.get("tags") or [])][:12],
                "text_in_image": str(answer.get("text_in_image", ""))[:400],
                "model_version": provider_config.vision_model,
            })
        except Exception as error:
            LOG.warning("AI description failed for %s: %s", path.name, type(error).__name__)
    else:
        result["description"] = "Ảnh không có nội dung cần mô tả"
        result["tags"] = []

    return result
