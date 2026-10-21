"""Auto Comment: Community Standards policy checking for comments."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from services import ai_provider

LOG = logging.getLogger("facebook-pipeline.auto-comment.policy")


class PolicyVerdict(Enum):
    APPROVED = "APPROVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class PolicyCheckResult:
    """Result of a comment policy check."""
    verdict: PolicyVerdict
    reason: str
    confidence: str  # 'high' | 'medium' | 'low'
    categories: list[str]  # e.g., ['hate_speech', 'harassment']
    suggestions: list[str]


def check_comment_policy(
    comment_text: str,
    post_content: str,
    post_context: dict,
    group_rules: str | None,
    provider_config: ai_provider.ProviderConfig,
    brand_guidelines: str | None = None,
) -> PolicyCheckResult:
    """Check a comment against Community Standards and brand guidelines.
    
    Uses AI to evaluate:
    - Hate speech, harassment, bullying
    - Violence, self-harm, regulated goods
    - Spam, deception, misleading content
    - Brand guideline violations
    - Spammy/repetitive patterns
    - Self-promotion, unauthorized links
    """
    if not provider_config.configured:
        return PolicyCheckResult(
            verdict=PolicyVerdict.NEEDS_REVIEW,
            reason="AI provider not configured",
            confidence="low",
            categories=[],
            suggestions=["Configure AI provider for automated policy check"],
        )
    
    # Build system prompt
    system_prompt = """Bạn là bộ kiểm duyệt bình luận cho Facebook. 
Kiểm tra bình luận theo Tiêu chuẩn cộng đồng Facebook và hướng dẫn thương hiệu.
Trả về JSON:
{
  "verdict": "APPROVED|NEEDS_REVIEW|BLOCKED",
  "reason": "lý do ngắn gọn",
  "confidence": "high|medium|low",
  "categories": ["hate_speech", "harassment", "violence", "spam", "deception", "self_promotion"],
  "suggestions": ["đề xuất sửa đổi nếu có"]
}

Chỉ trả về JSON. Không thêm văn bản khác."""

    # Build user prompt
    brand_section = f"\nHƯỚNG DẪN THƯƠNG HIỆU:\n{brand_guidelines}\n" if brand_guidelines else ""
    group_rules_section = f"\nQUY ĐỊNH GROUP:\n{group_rules}\n" if group_rules else ""
    
    user_prompt = f"""NỘI DUNG BÀI VIẾT:
{post_content[:2000]}

NGỮ CẢNH BÀI VIẾT:
{post_context.get('description', 'Không có')}{group_rules_section}{brand_section}

BÌNH LUẬN CẦN KIỂM TRA:
{comment_text}

Hãy kiểm tra bình luận này."""

    try:
        result = ai_provider.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            config=provider_config,
            timeout=30,
        )
        
        verdict_str = str(result.get("verdict", "")).upper()
        verdict = PolicyVerdict[verdict_str] if verdict_str in PolicyVerdict.__members__ else PolicyVerdict.NEEDS_REVIEW
        
        return PolicyCheckResult(
            verdict=verdict,
            reason=str(result.get("reason", "Không có lý do"))[:500],
            confidence=str(result.get("confidence", "medium")),
            categories=[str(c) for c in result.get("categories", [])],
            suggestions=[str(s) for s in result.get("suggestions", [])],
        )
    
    except ai_provider.ProviderError as exc:
        LOG.error("AI policy check failed: %s", exc)
        return PolicyCheckResult(
            verdict=PolicyVerdict.NEEDS_REVIEW,
            reason=f"AI provider error: {exc}",
            confidence="low",
            categories=[],
            suggestions=["Kiểm tra AI provider và thử lại"],
        )
    except Exception as exc:
        LOG.error("Policy check unexpected error: %s", type(exc).__name__)
        return PolicyCheckResult(
            verdict=PolicyVerdict.NEEDS_REVIEW,
            reason=f"Lỗi không xác định: {type(exc).__name__}",
            confidence="low",
            categories=[],
            suggestions=[],
        )


def basic_spam_check(comment_text: str) -> tuple[bool, str]:
    """Fast heuristic spam check without AI.
    
    Returns (is_suspicious, reason).
    """
    text = comment_text.lower()
    
    # Excessive repetition
    words = comment_text.split()
    if len(words) > 10:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < 0.3:
            return True, "excessive_word_repetition"
    
    # All caps (shouting)
    if len(comment_text) > 20 and comment_text.isupper():
        return True, "all_caps"
    
    # Excessive emojis
    emoji_count = sum(1 for c in comment_text if ord(c) > 0x1F300)
    if emoji_count > 10:
        return True, "excessive_emojis"
    
    # Common spam patterns
    spam_patterns = [
        r'k\.?m\.?k\.?\s*\d+',  # kmk + number (VN spam)
        r'liên hệ|lien he',      # contact prompts
        r' zalo | fb | messenger ',  # external contact
        r'https?://',            # links
        r'\b(sdt|sđt|phone|số điện thoại)\b',  # phone numbers
    ]
    
    for pattern in spam_patterns:
        if re.search(pattern, text):
            return True, f"spam_pattern:{pattern}"
    
    return False, "OK"