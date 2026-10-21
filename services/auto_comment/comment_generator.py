"""Auto Comment: AI-powered comment generation."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from services import ai_provider

LOG = logging.getLogger("facebook-pipeline.auto-comment.generator")


@dataclass(frozen=True)
class CommentCandidate:
    """A generated comment candidate."""
    text: str
    confidence: str  # 'high' | 'medium' | 'low'
    tone: str  # 'friendly' | 'professional' | 'casual' | 'enthusiastic'
    reasoning: str  # Why this comment fits


@dataclass(frozen=True)
class GenerationResult:
    """Result of comment generation."""
    candidates: list[CommentCandidate]
    error: str | None
    needs_review: bool


COMMENT_GENERATION_PROMPT = """Bạn là trợ lý tạo bình luận tự nhiên cho Facebook.
Tạo bình luận liên quan, tự nhiên, không spam, không quảng cáo.
Trả về JSON array các ứng cử viên:
[
  {"text": "nội dung bình luận", "confidence": "high|medium|low", "tone": "friendly|professional|casual|enthusiastic", "reasoning": "tại sao bình luận này phù hợp"}
]

Quy tắc:
- Tiếng Việt tự nhiên, không máy móc
- Liên quan trực tiếp đến nội dung bài viết
- Không chèn link, SĐT, Telegram, CTA quảng cáo
- Không tự nhận đã dùng sản phẩm khi không có bằng chứng
- Không tạo thông tin sai sự thật
- Tôn trọng Tiêu chuẩn cộng đồng Facebook
- Độ dài 20-300 ký tự
- Tối đa 3 ứng cử viên"""


def generate_comment_candidates(
    post_content: str,
    post_context: dict,
    group_rules: str | None,
    brand_guidelines: str | None,
    provider_config: ai_provider.ProviderConfig,
    topic_hint: str | None = None,
) -> GenerationResult:
    """Generate comment candidates for a post."""
    if not provider_config.configured:
        return GenerationResult(
            candidates=[],
            error="AI provider not configured",
            needs_review=True,
        )
    
    brand_section = f"\nHƯỚNG DẪN THƯƠNG HIỆU:\n{brand_guidelines}\n" if brand_guidelines else ""
    group_rules_section = f"\nQUY ĐỊNH GROUP:\n{group_rules}\n" if group_rules else ""
    topic_section = f"\nGỢI Ý CHỦ ĐỀ:\n{topic_hint}\n" if topic_hint else ""
    
    user_prompt = f"""NỘI DUNG BÀI VIẾT:
{post_content[:3000]}

NGỮ CẢNH:
{post_context.get('description', 'Không có')}{group_rules_section}{brand_section}{topic_section}

Hãy tạo 3 bình luận phù hợp."""

    try:
        result = ai_provider.chat_json(
            system_prompt=COMMENT_GENERATION_PROMPT,
            user_prompt=user_prompt,
            config=provider_config,
            timeout=45,
        )
        
        if not isinstance(result, list):
            LOG.error("Comment generator returned non-list: %s", type(result))
            return GenerationResult(candidates=[], error="Invalid AI response format", needs_review=True)
        
        candidates = []
        for item in result[:3]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text or len(text) < 10:
                continue
            if len(text) > 500:
                text = text[:500]
            
            candidates.append(CommentCandidate(
                text=text,
                confidence=str(item.get("confidence", "medium")),
                tone=str(item.get("tone", "friendly")),
                reasoning=str(item.get("reasoning", "")),
            ))
        
        if not candidates:
            return GenerationResult(candidates=[], error="No valid candidates generated", needs_review=True)
        
        return GenerationResult(
            candidates=candidates,
            error=None,
            needs_review=False,
        )
    
    except ai_provider.ProviderError as exc:
        LOG.error("Comment generation AI error: %s", exc)
        return GenerationResult(candidates=[], error=f"AI provider error: {exc}", needs_review=True)
    except Exception as exc:
        LOG.error("Comment generation unexpected error: %s", type(exc).__name__)
        return GenerationResult(candidates=[], error=f"Unexpected error: {type(exc).__name__}", needs_review=True)


def select_best_candidate(
    candidates: list[CommentCandidate],
    fingerprint_check_fn,
) -> CommentCandidate | None:
    """Select the best candidate that passes fingerprint check.
    
    fingerprint_check_fn(text) -> (is_allowed, reason)
    """
    # Sort by confidence: high > medium > low
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    sorted_candidates = sorted(candidates, key=lambda c: confidence_order.get(c.confidence, 3))
    
    for candidate in sorted_candidates:
        is_allowed, reason = fingerprint_check_fn(candidate.text)
        if is_allowed:
            return candidate
        LOG.debug("Candidate rejected by fingerprint: %s", reason)
    
    return None


def generate_single_comment(
    post_content: str,
    post_context: dict,
    group_rules: str | None,
    brand_guidelines: str | None,
    provider_config: ai_provider.ProviderConfig,
    fingerprint_check_fn,
    topic_hint: str | None = None,
) -> tuple[str | None, str | None]:
    """Generate a single approved comment.
    
    Returns (comment_text, error).
    """
    result = generate_comment_candidates(
        post_content=post_content,
        post_context=post_context,
        group_rules=group_rules,
        brand_guidelines=brand_guidelines,
        provider_config=provider_config,
        topic_hint=topic_hint,
    )
    
    if result.error:
        return None, result.error
    
    best = select_best_candidate(result.candidates, fingerprint_check_fn)
    if not best:
        return None, "All candidates failed duplicate/spam check"
    
    return best.text, None