"""Vision analysis: image description, flagging and OCR text extraction.

Delegates to :mod:`services.ai_provider` for the actual HTTP calls.
"""
from __future__ import annotations

import logging
from pathlib import Path

from services import ai_provider

LOG = logging.getLogger("facebook-pipeline.vision")


VISION_SYSTEM_PROMPT = """Bạn là bộ kiểm duyệt nội dung video ngắn cho Facebook Reels.
Phân tích các khung hình được cung cấp và trả về JSON:
{"overall": "none|low|medium|high",
 "reason": "mô tả ngắn gì thực sự thấy được",
 "frames": [{"index": 0, "time": 0.0, "category": "none|violence|blood|sexual|harassment|dangerous|offensive_text|misleading|watermark|low_quality", "note": "mô tả ngắn"}],
 "text_in_frames": [{"time": 0.0, "text": "bất kỳ văn bản nào thấy trong khung hình"}]}

Chỉ nêu những gì thực sự nhìn thấy. Nếu không có gì thì dùng "none".
Nếu thấy văn bản trong ảnh, hãy trích xuất chính xác nội dung văn bản đó.
Phân tích bối cảnh: phim hư cấp, tin tức, giáo dục hay nội dung thực tế — không tự động kết luận vi phạm chỉ vì chứa hình ảnh hoặc từ ngữ nhạy cảm."""


def analyze_frames(
    frames: list[Path],
    provider_config: ai_provider.ProviderConfig,
    system_prompt: str = VISION_SYSTEM_PROMPT,
    timeout: int = 0,
) -> dict | None:
    """Call the vision model with a list of key frames.

    Returns the model's JSON response, or ``None`` when the model is not
    configured.  Raises :class:`ProviderError` on provider errors so the caller
    can distinguish a technical failure from a missing configuration.
    """
    if not frames:
        return None
    if not provider_config.vision_configured:
        return None
    # Verify the model advertises vision capability before calling.
    caps = ai_provider.discover_models(provider_config)
    model_name = provider_config.vision_model
    if model_name and model_name in caps:
        info = caps[model_name]
        if not info.get("vision", False):
            LOG.error("Configured vision model %s does not advertise vision capability", model_name)
            raise ai_provider.ProviderError(f"Vision model '{model_name}' does not support image input")
    # Exceptions propagate to the caller.
    return ai_provider.vision_json(
        "Bạn là bộ kiểm duyệt nội dung video ngắn.",
        system_prompt,
        frames,
        provider_config,
        timeout=timeout,
    )


def extract_ocr_text(answer: dict | None) -> str:
    """Pull the concatenated OCR text out of a vision-model answer."""
    if not answer or not isinstance(answer, dict):
        return ""
    chunks: list[str] = []
    for entry in answer.get("text_in_frames") or []:
        if isinstance(entry, dict):
            text = str(entry.get("text") or "").strip()
            if text:
                chunks.append(text)
    return "\n".join(chunks) if chunks else ""
