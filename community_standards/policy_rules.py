"""Policy rules for the 12 Community Standards risk categories.

Each category is a dataclass that describes:
- the human-readable label,
- the severity tier (violation / distribution_risk / copyright),
- the default action this tool recommends (block / review),
- and a short policy reference.
"""
from __future__ import annotations

from dataclasses import dataclass

POLICY_VERSION = "meta-cs-v2026-10-20"

# Severity tiers — distinct from the check status.
SEVERITY_VIOLATION = "violation"
SEVERITY_DISTRIBUTION_RISK = "distribution_risk"
SEVERITY_COPYRIGHT = "copyright"

# Actions the policy evaluator can recommend.
ACTION_BLOCK = "block"
ACTION_REVIEW = "review"

# Final outcome tiers.
OUTCOME_PASS = "pass"
OUTCOME_REVIEW = "needs_review"
OUTCOME_BLOCK = "blocked"
OUTCOME_CHECK_FAILED = "check_failed"


@dataclass(frozen=True)
class CategoryRule:
    """One community-standards category."""

    key: str
    label: str
    severity: str
    action: str
    policy_ref: str
    description: str


def _categories() -> tuple[CategoryRule, ...]:
    return (
        CategoryRule(
            key="violence_threats",
            label="Bạo lực, đe dọa và kích động hành vi nguy hiểm",
            severity=SEVERITY_VIOLATION,
            action=ACTION_BLOCK,
            policy_ref="Community Standards - Violence & Incitement",
            description="Hình ảnh hoặc lời gây bạo lực, đe dọa tính mạng, kích động giết người hoặc hành vi nguy hiểm.",
        ),
        CategoryRule(
            key="graphic_content",
            label="Hình ảnh máu me, thương tích",
            severity=SEVERITY_VIOLATION,
            action=ACTION_BLOCK,
            policy_ref="Community Standards - Graphic Violence & Physical Injury",
            description="Nội dung máu me, vết thương, thương tích rõ rệt, hoặc hình ảnh gây sốc.",
        ),
        CategoryRule(
            key="nudity_sexual",
            label="Khỏa thân và nội dung tình dực không phù hợp",
            severity=SEVERITY_VIOLATION,
            action=ACTION_BLOCK,
            policy_ref="Community Standards - Nudity & Sexual Activity",
            description="Khoe khẩn, nội dung tình dực, khiêu dâm, hoặc lợi dụng tình dục.",
        ),
        CategoryRule(
            key="child_safety",
            label="Nội dung liên quan đến bóc lột hoặc xâm hại trẻ em",
            severity=SEVERITY_VIOLATION,
            action=ACTION_BLOCK,
            policy_ref="Community Standards - Child Exploitation & Abuse",
            description="Bất kỳ nội dung nào gợi đề tới bóc lột, xâm hại, khai thác trẻ em.",
        ),
        CategoryRule(
            key="hate_speech",
            label="Ngôn từ thù ghét, xúc phạm, bắt nạt và quấy rối",
            severity=SEVERITY_VIOLATION,
            action=ACTION_REVIEW,
            policy_ref="Community Standards - Hate Speech & Bullying",
            description="Lời nói thù hại dựa trên nhóm bảo vệ, ngôn từ xúc phạm, bắt nạt.",
        ),
        CategoryRule(
            key="suicide_self_harm",
            label="Tự gây thương tích, tự tử",
            severity=SEVERITY_VIOLATION,
            action=ACTION_REVIEW,
            policy_ref="Community Standards - Self-Harm & Suicide",
            description="Nội dung thúc đẩy hoặc hướng dẫn tự làm hại, tự tử.",
        ),
        CategoryRule(
            key="fraud_deception",
            label="Lừa đảo và nội dung gây hiểu nhầm",
            severity=SEVERITY_VIOLATION,
            action=ACTION_BLOCK,
            policy_ref="Community Standards - Fraud & Deception",
            description="Lừa đảo, gây hiểu nhầm, quảng cáo sai sự thật, lừa tài chính.",
        ),
        CategoryRule(
            key="restricted_goods",
            label="Mua bán hoặc quảng bá hàng hóa, dịch vụ bị hạn chế",
            severity=SEVERITY_DISTRIBUTION_RISK,
            action=ACTION_REVIEW,
            policy_ref="Commerce Policies - Restricted Products",
            description="Hàng hóa/dịch vụ bị hạn chế như rượu bia, thuốc, vũ khí, sản phẩm cấm.",
        ),
        CategoryRule(
            key="personal_info",
            label="Tiết lộ thông tin cá nhân trái phép",
            severity=SEVERITY_VIOLATION,
            action=ACTION_BLOCK,
            policy_ref="Privacy Policy - Unauthorized Data Sharing",
            description="Tiết lộ CCCD, số tài khoản, thông tin cá nhân mà người khác không chia sẻ.",
        ),
        CategoryRule(
            key="spam_duplicate",
            label="Spam, nội dung trùng lặp, tương tác giả",
            severity=SEVERITY_DISTRIBUTION_RISK,
            action=ACTION_REVIEW,
            policy_ref="Community Standards - Spam & Fake Engagement",
            description="Nội dung trùng lặp, spam, tương tác giả, bot-like hành vi.",
        ),
        CategoryRule(
            key="copyright",
            label="Nội dung vi phạm bản quyền",
            severity=SEVERITY_COPYRIGHT,
            action=ACTION_REVIEW,
            policy_ref="Intellectual Property Policy",
            description="Nội dung dùng âm nhạc, hình ảnh, video mà người dùng không có quyền sử dụng.",
        ),
        CategoryRule(
            key="non_original_low_quality",
            label="Nội dung không nguyên bản, chất lượng thấp",
            severity=SEVERITY_DISTRIBUTION_RISK,
            action=ACTION_REVIEW,
            policy_ref="Reels Recommendation Guidelines",
            description="Nội dung tái đăng, chất lượng kém, watermark, không tạo giá trị mới.",
        ),
    )


_CATEGORIES_TUPLE = _categories()
CATEGORIES: tuple[CategoryRule, ...] = _CATEGORIES_TUPLE
CATEGORY_BY_KEY: dict[str, CategoryRule] = {rule.key: rule for rule in _CATEGORIES_TUPLE}


def get_rule(key: str) -> CategoryRule | None:
    """Look up a rule by its category key."""
    return CATEGORY_BY_KEY.get(key)


def evaluate_category(
    category_key: str,
    evidence: str = "",
    confidence: str = "medium",
) -> dict | None:
    """Return a structured finding for one category, or None if no issue.

    ``evidence`` is a short human text describing what was seen.
    ``confidence`` is one of ``high``, ``medium``, ``low``.
    """
    rule = CATEGORY_BY_KEY.get(category_key)
    if rule is None:
        return None
    return {
        "category": category_key,
        "label": rule.label,
        "severity": rule.severity,
        "action": rule.action,
        "policy_ref": rule.policy_ref,
        "confidence": confidence,
        "evidence": evidence[:400],
        "remediation": _remediation_for(category_key),
    }


def _remediation_for(key: str) -> str:
    """A short, actionable fix suggestion per category."""
    return {
        "violence_threats": "Loại bỏ hoặc chỉnh sửa cảnh quay bạo lực, đe dọa.",
        "graphic_content": "Cắt bỏ cảnh máu me, thương tích; thêm lời cảnh báo nếu là tin tức/đời thực.",
        "nudity_sexual": "Làm mờ hoặc cắt phần khỏa thân; chuyển thành nội dung giáo dục nếu cần.",
        "child_safety": "NGƯỜI LỚN NÊN ĐƯỢC BẢO VỆ. Xóa nội dung liên quan đến trẻ em ngay.",
        "hate_speech": "Thay đổi ngôn ngữ, không dùng từ ngữ thù hại hay xúc phạm nhóm bảo vệ.",
        "suicide_self_harm": "Loại bỏ lời kêu gọi tự làm hại; thêm liên kết hỗ trợ nếu là nội dung giáo dục.",
        "fraud_deception": "Xóa lời hứa kiểu ‘100%’, ‘cam kết’, ‘lãi không rủi ro’; không được lừa đảo.",
        "restricted_goods": "Xác minh hàng hóa không thuộc danh sách cấm; cung cấp giấy tờ pháp lý.",
        "personal_info": "Xóa thông tin cá nhân (số CMND, tài khoản, số thẻ, v.v.).",
        "spam_duplicate": "Nhấn mạnh sự sáng tạo; tránh nội dung trùng lặp, hashtag lạm dụng.",
        "copyright": "Sử dụng nhạc/ảnh/video có bản quyền hoặc nền tảng tự sở hữu; ghi rõ nguồn.",
        "non_original_low_quality": "Tạo nội dung gốc, cải thiện chất lượng hình ảnh, bỏ watermark.",
    }.get(key, "Xem xét lại nội dung theo hướng dẫn cộng đồng.")
