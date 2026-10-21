"""Map AWS Rekognition labels to Community Standards risk categories."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RiskLevel = Literal["none", "low", "medium", "high", "blocked"]


@dataclass(frozen=True)
class ModerationMapping:
    aws_category: str
    aws_parent: str
    cs_category: str
    risk_level: RiskLevel
    action: str
    policy_ref: str


MODERATION_MAPPINGS: list[ModerationMapping] = [
    # Explicit Nudity
    ModerationMapping(
        aws_category="Explicit Nudity",
        aws_parent="Explicit Nudity",
        cs_category="sexual_content",
        risk_level="high",
        action="block",
        policy_ref="Community Standards - Adult Nudity & Sexual Activity",
    ),
    ModerationMapping(
        aws_category="Suggestive",
        aws_parent="Suggestive",
        cs_category="sexual_content",
        risk_level="medium",
        action="review",
        policy_ref="Community Standards - Suggestive Content",
    ),
    ModerationMapping(
        aws_category="Partial Nudity",
        aws_parent="Partial Nudity",
        cs_category="sexual_content",
        risk_level="medium",
        action="review",
        policy_ref="Community Standards - Partial Nudity",
    ),
    ModerationMapping(
        aws_category="Revealing Swimwear/Underwear",
        aws_parent="Partial Nudity",
        cs_category="sexual_content",
        risk_level="low",
        action="review",
        policy_ref="Community Standards - Suggestive Content",
    ),

    # Violence
    ModerationMapping(
        aws_category="Graphic Violence",
        aws_parent="Violence",
        cs_category="violence",
        risk_level="high",
        action="block",
        policy_ref="Community Standards - Violence & Graphic Content",
    ),
    ModerationMapping(
        aws_category="Physical Violence",
        aws_parent="Violence",
        cs_category="violence",
        risk_level="high",
        action="block",
        policy_ref="Community Standards - Violence & Graphic Content",
    ),
    ModerationMapping(
        aws_category="Weapon Violence",
        aws_parent="Violence",
        cs_category="dangerous_objects",
        risk_level="medium",
        action="review",
        policy_ref="Community Standards - Dangerous Individuals & Organizations",
    ),
    ModerationMapping(
        aws_category="Weapons",
        aws_parent="Violence",
        cs_category="dangerous_objects",
        risk_level="medium",
        action="review",
        policy_ref="Community Standards - Regulated Goods",
    ),

    # Hate
    ModerationMapping(
        aws_category="Hate Symbols",
        aws_parent="Hate Symbols",
        cs_category="hate_speech",
        risk_level="high",
        action="block",
        policy_ref="Community Standards - Hate Speech",
    ),

    # Drugs
    ModerationMapping(
        aws_category="Drugs",
        aws_parent="Drugs",
        cs_category="regulated_goods",
        risk_level="high",
        action="block",
        policy_ref="Community Standards - Regulated Goods",
    ),
    ModerationMapping(
        aws_category="Tobacco",
        aws_parent="Tobacco",
        cs_category="regulated_goods",
        risk_level="medium",
        action="review",
        policy_ref="Community Standards - Regulated Goods",
    ),
    ModerationMapping(
        aws_category="Alcohol",
        aws_parent="Alcohol",
        cs_category="regulated_goods",
        risk_level="medium",
        action="review",
        policy_ref="Community Standards - Regulated Goods",
    ),

    # Disturbing
    ModerationMapping(
        aws_category="Visually Disturbing",
        aws_parent="Visually Disturbing",
        cs_category="graphic_content",
        risk_level="high",
        action="block",
        policy_ref="Community Standards - Violence & Graphic Content",
    ),
    ModerationMapping(
        aws_category="Emaciated Bodies",
        aws_parent="Visually Disturbing",
        cs_category="graphic_content",
        risk_level="high",
        action="block",
        policy_ref="Community Standards - Self-Injury",
    ),

    # Self-harm
    ModerationMapping(
        aws_category="Self-Harm",
        aws_parent="Self-Harm",
        cs_category="self_harm",
        risk_level="high",
        action="block",
        policy_ref="Community Standards - Self-Injury",
    ),

    # Minors
    ModerationMapping(
        aws_category="Child Sexual Abuse",
        aws_parent="Child Sexual Abuse",
        cs_category="child_safety",
        risk_level="blocked",
        action="block",
        policy_ref="Community Standards - Child Safety",
    ),
    ModerationMapping(
        aws_category="Sexualized Children",
        aws_parent="Child Sexual Abuse",
        cs_category="child_safety",
        risk_level="blocked",
        action="block",
        policy_ref="Community Standards - Child Safety",
    ),
    ModerationMapping(
        aws_category="Exploited Children",
        aws_parent="Child Sexual Abuse",
        cs_category="child_safety",
        risk_level="blocked",
        action="block",
        policy_ref="Community Standards - Child Safety",
    ),
]


IMAGE_LABEL_MAPPINGS: dict[str, tuple[str, RiskLevel, str, str]] = {
    "Blue": ("visual_background", "none", "", "N/A"),
    "Sky": ("visual_background", "none", "", "N/A"),
    "Water": ("visual_background", "none", "", "N/A"),
    "Text": ("ocr_text_detected", "low", "review", "Community Standards - Text in Images"),
    "Text on Screen": ("ocr_text_detected", "low", "review", "Community Standards - Text in Images"),
    "Weapon": ("dangerous_objects", "medium", "review", "Community Standards - Regulated Goods"),
    "Gun": ("dangerous_objects", "high", "block", "Community Standards - Regulated Goods"),
    "Knife": ("dangerous_objects", "medium", "review", "Community Standards - Regulated Goods"),
    "Blood": ("violence", "high", "block", "Community Standards - Violence & Graphic Content"),
    "Fire": ("dangerous_activity", "medium", "review", "Community Standards - Dangerous Individuals"),
    "Smoke": ("dangerous_activity", "low", "review", "Community Standards - Dangerous Individuals"),
    "Police": ("context_law_enforcement", "low", "review", "N/A"),
    "Uniform": ("context_uniform", "low", "review", "N/A"),
}


def map_moderation_labels(labels: list[dict]) -> list[dict]:
    """Convert AWS moderation labels to CS findings."""
    findings = []
    for label in labels:
        name = label.get("name", "")
        confidence = label.get("confidence", 0.0)
        parent = label.get("parent", "")

        for mapping in MODERATION_MAPPINGS:
            if mapping.aws_category == name or mapping.aws_parent == name:
                findings.append({
                    "category": mapping.cs_category,
                    "label": f"{name} ({confidence:.1f}%)",
                    "severity": mapping.risk_level,
                    "action": mapping.action,
                    "policy_ref": mapping.policy_ref,
                    "confidence": "high" if confidence > 80 else "medium",
                    "evidence": f"AWS Rekognition: {name} ({confidence:.1f}%) parent={parent}",
                    "remediation": f"Xét lại đoạn video có nội dung {name.lower()}",
                    "timestamp": None,
                })
                break
    return findings


def map_image_labels(labels: list[dict]) -> list[dict]:
    """Convert AWS DetectLabels to supplementary findings."""
    findings = []
    for label in labels:
        name = label.get("name", "")
        confidence = label.get("confidence", 0.0)

        if name in IMAGE_LABEL_MAPPINGS:
            cs_cat, risk, action, policy = IMAGE_LABEL_MAPPINGS[name]
            findings.append({
                "category": cs_cat,
                "label": f"{name} ({confidence:.1f}%)",
                "severity": risk,
                "action": action,
                "policy_ref": policy,
                "confidence": "medium" if confidence > 60 else "low",
                "evidence": f"AWS Rekognition DetectLabels: {name} ({confidence:.1f}%)",
                "remediation": f"Kiểm tra ngữ cảnh của {name.lower()} trong video",
                "timestamp": None,
            })
    return findings