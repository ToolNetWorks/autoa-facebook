"""Policy evaluation engine.

Takes the structured outputs of vision, transcription, OCR and caption
analysis and evaluates them against the Community Standards rules defined in
:mod:`community_standards.policy_rules`.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from community_standards import (
    ACTION_BLOCK,
    ACTION_REVIEW,
    OUTCOME_BLOCK,
    OUTCOME_CHECK_FAILED,
    OUTCOME_PASS,
    OUTCOME_REVIEW,
    SEVERITY_COPYRIGHT,
    SEVERITY_DISTRIBUTION_RISK,
    SEVERITY_VIOLATION,
    CATEGORY_BY_KEY,
    evaluate_category,
    get_rule,
)

LOG = logging.getLogger("facebook-pipeline.policy")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_vision(
    vision_answer: dict | None,
    checked: bool,
    error: bool = False,
) -> list[dict]:
    """Evaluate the vision model response against the 12 categories.

    ``checked`` is True when the vision call completed (even if it returned "none").
    ``error`` is True when the vision call raised an exception (ProviderError,
    timeout, etc.).
    """
    findings: list[dict] = []
    if error:
        findings.append({
            "category": "vision_check_failed",
            "label": "Không thể kiểm tra hình ảnh bằng AI",
            "severity": "check_failed",
            "action": "review",
            "policy_ref": "N/A — lỗi kỹ thuật",
            "confidence": "high",
            "evidence": "Vision model returned an error (ProviderError/timeout).",
            "remediation": "Kiểm tra cấu hình model (AI_VISION_MODEL) và thử lại.",
        })
        return findings
    if not checked or vision_answer is None:
        findings.append({
            "category": "visual_unchecked",
            "label": "Chưa kiểm tra được hình ảnh",
            "severity": "check_failed",
            "action": "review",
            "policy_ref": "N/A — chưa kiểm tra",
            "confidence": "high",
            "evidence": "Vision model chưa được cấu hình hoặc không gọi được.",
            "remediation": "Cấu hình AI_VISION_MODEL rồi chạy lại kiểm tra.",
        })
        return findings

    overall = str(vision_answer.get("overall", "low")).lower()
    if overall == "high":
        findings.append(evaluate_category(
            "violence_threats",
            evidence=f"Vision overall={overall}: {vision_answer.get('reason', '')[:200]}",
            confidence="high",
        ) or {})
    for frame in vision_answer.get("frames", []) or []:
        if not isinstance(frame, dict):
            continue
        category = str(frame.get("category", "none")).lower()
        if category in ("none", ""):
            continue
        timestamp = _safe_float(frame.get("time"))
        note = str(frame.get("note", ""))[:200]
        mapped = _map_vision_category(category)
        if mapped:
            finding = evaluate_category(
                mapped,
                evidence=f"Khung hình {timestamp:.1f}s: {note}",
                confidence="medium",
            )
            if finding:
                finding["timestamp"] = timestamp
                findings.append(finding)
    return findings


def _map_vision_category(vision_cat: str) -> str | None:
    """Map a vision model frame category to a policy category key."""
    mapping = {
        "violence": "violence_threats",
        "blood": "graphic_content",
        "sexual": "nudity_sexual",
        "harassment": "hate_speech",
        "dangerous": "violence_threats",
        "offensive_text": "hate_speech",
        "misleading": "fraud_deception",
        "watermark": "non_original_low_quality",
        "low_quality": "non_original_low_quality",
    }
    return mapping.get(vision_cat)


def evaluate_transcription(
    asr_answer: dict | None,
    checked: bool,
    error: bool = False,
) -> list[dict]:
    """Evaluate the ASR transcription against the 12 categories."""
    findings: list[dict] = []
    if error:
        findings.append({
            "category": "audio_check_failed",
            "label": "Không thể chuyển lời nói thành văn bản",
            "severity": "check_failed",
            "action": ACTION_REVIEW,
            "policy_ref": "N/A — lỗi kỹ thuật",
            "confidence": "high",
            "evidence": "ASR model returned an error (ProviderError/timeout).",
            "remediation": "Kiểm tra cấu hình model (AI_ASR_MODEL) và thử lại.",
        })
        return findings
    if not checked or asr_answer is None:
        findings.append({
            "category": "audio_unchecked",
            "label": "Chưa kiểm tra được âm thanh/lời thoại",
            "severity": "check_failed",
            "action": ACTION_REVIEW,
            "policy_ref": "N/A — chưa kiểm tra",
            "confidence": "high",
            "evidence": "ASR model chưa được cấu hình hoặc không gọi được.",
            "remediation": "Cấu hình AI_ASR_MODEL rồi chạy lại kiểm tra.",
        })
        return findings

    if not asr_answer.get("speech_detected", False):
        findings.append({
            "category": "no_speech",
            "label": "Không phát hiện lời thoại",
            "severity": SEVERITY_DISTRIBUTION_RISK,
            "action": ACTION_REVIEW,
            "policy_ref": "Reels Recommendation Guidelines",
            "confidence": "high",
            "evidence": "Video có âm thanh nhưng không phát hiện lời nói rõ ràng.",
            "remediation": "Thêm âm thanh hoặc lời thoại nếu nội dung cần truyền tải thông điệp.",
        })

    transcript = str(asr_answer.get("text", ""))[:2000]
    for risk in asr_answer.get("risks", []) or []:
        if not isinstance(risk, dict):
            continue
        category = str(risk.get("category", "none")).lower()
        if category in ("none", ""):
            continue
        timestamp = _safe_float(risk.get("time"))
        note = str(risk.get("note", ""))[:200]
        mapped = _map_asr_category(category)
        if mapped:
            finding = evaluate_category(
                mapped,
                evidence=f"Lời thoại {timestamp:.1f}s: {note}",
                confidence="medium",
            )
            if finding:
                finding["timestamp"] = timestamp
                findings.append(finding)

    # Also scan the transcript text for known high-risk terms.
    findings.extend(_scan_text(transcript, "transcript"))
    return findings


# Alias so the precheck module can use a shorter name.
evaluate_audio = evaluate_transcription


def compute_severity_from_findings(findings: list[dict]) -> str | None:
    """Return the single-worst severity among findings, or None when empty."""
    has_violation = False
    has_technical = False
    has_review = False
    for f in findings:
        sev = f.get("severity", "")
        if sev == SEVERITY_VIOLATION:
            has_violation = True
        elif sev == "technical_quality":
            has_technical = True
        elif sev in (SEVERITY_DISTRIBUTION_RISK, SEVERITY_COPYRIGHT, "check_failed"):
            has_review = True
    if has_violation or has_technical:
        return "blocked"
    if has_review:
        return "needs_review"
    return "pass"


def _map_asr_category(asr_cat: str) -> str | None:
    mapping = {
        "abusive": "hate_speech",
        "offensive": "hate_speech",
        "misleading": "fraud_deception",
        "sensitive": "nudity_sexual",
        "inappropriate": "nudity_sexual",
    }
    return mapping.get(asr_cat)


def _scan_text(text: str, source: str) -> list[dict]:
    """Scan a block of text (caption or transcript) for localisable risk terms."""
    findings: list[dict] = []
    lowered = text.lower()
    checks = [
        ("child_safety", SEVERITY_VIOLATION, ACTION_BLOCK,
         [r"bố.*cho.*con", r"bạo.*lực.*trẻ", r"bóc.*lột.*trẻ", r"xâm.*hại.*trẻ"]),
        ("suicide_self_harm", SEVERITY_VIOLATION, ACTION_REVIEW,
         [r"tự.*tử", r"tự.*cắt", r"tự.*giết", r"đầu hàng", r"không.*sống"]),
        ("hate_speech", SEVERITY_VIOLATION, ACTION_REVIEW,
         [r"loại.*vô.*dụng", r"người.*khác.*loại", r"phân.*biệt.*chủng",
          r"phân.*biệt.*giới", r"chủng.*tộc"]),
        ("fraud_deception", SEVERITY_VIOLATION, ACTION_BLOCK,
         [r"100%.*hiệu quả", r"cam kết.*hoàn tiền", r"không.*rủi ro",
          r"lãi.*caorất", r"thu nhập.*không.*giới hạn"]),
        ("nudity_sexual", SEVERITY_VIOLATION, ACTION_BLOCK,
         [r"khỏa thân", r"nude", r"kích dụng", r"yêu.*âm.*đức"]),
        ("personal_info", SEVERITY_VIOLATION, ACTION_BLOCK,
         [r"stk.*:", r"tk.*ngân hàng", r"cccd.*:", r"cmnd.*:", r"mật khẩu"]),
        ("spam_duplicate", SEVERITY_DISTRIBUTION_RISK, ACTION_REVIEW,
         [r"bấm.*like", r"tag.*bạn bè", r"chia sẻ.*bài", r"đăng ký.*nhận"]),
    ]
    for category, severity, action, patterns in checks:
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                rule = get_rule(category)
                findings.append({
                    "category": category,
                    "label": rule.label if rule else category,
                    "severity": severity,
                    "action": action,
                    "policy_ref": rule.policy_ref if rule else "Community Standards",
                    "confidence": "low",
                    "evidence": f"Nguồn {source}: '{text[max(0, match.start()-15):match.end()+15].strip()}'",
                    "remediation": rule and _remediation_lookup(rule.key),
                    "timestamp": 0.0 if source == "caption" else None,
                    "source": source,
                })
                break  # one finding per category
    return findings


def _remediation_lookup(key: str) -> str:
    remediation_map = {
        "child_safety": "Xóa ngay mọi nội dung liên quan đến trẻ em.",
        "suicide_self_harm": "Loại bỏ lời kêu gọi tự làm hại; thêm liên kết hỗ trợ.",
        "hate_speech": "Thay đổi ngôn ngữ, tránh từ ngữ thù hại.",
        "fraud_deception": "Xóa lời hứa không căn cứ.",
        "nudity_sexual": "Làm mờ hoặc cắt nội dung khỏa thân.",
        "personal_info": "Xóa thông tin cá nhân.",
        "spam_duplicate": "Nhấn mạnh sự sáng tạo; tránh nội dung trùng lặp.",
    }
    return remediation_map.get(key, "Xem xét lại nội dung.")


def evaluate_caption(caption: str, analysis, ai_verdict: dict | None, ai_error: bool = False) -> list[dict]:
    """Evaluate caption text and AI caption verdict."""
    findings: list[dict] = []
    if analysis and analysis.issues:
        for issue in analysis.issues:
            category = _map_caption_issue(issue.category)
            if not category:
                continue
            rule = get_rule(category)
            severity = SEVERITY_VIOLATION if issue.severity == "high" else (
                SEVERITY_DISTRIBUTION_RISK if issue.severity == "medium" else None)
            if severity is None:
                findings.append({
                    "category": category,
                    "label": rule.label if rule else category,
                    "severity": "distribution_risk",
                    "action": ACTION_REVIEW,
                    "policy_ref": rule.policy_ref if rule else "Community Standards",
                    "confidence": "high",
                    "evidence": f"Caption: {issue.message}",
                    "remediation": _remediation_lookup(category),
                })
                continue
            action = ACTION_BLOCK if severity == SEVERITY_VIOLATION else ACTION_REVIEW
            findings.append({
                "category": category,
                "label": rule.label if rule else category,
                "severity": severity,
                "action": action,
                "policy_ref": rule.policy_ref if rule else "Community Standards",
                "confidence": "high",
                "evidence": f"Caption: {issue.message}",
                "remediation": _remediation_lookup(category),
            })
    if ai_error:
        findings.append({
            "category": "caption_ai_check_failed",
            "label": "AI kiểm tra caption lỗi",
            "severity": "check_failed",
            "action": ACTION_REVIEW,
            "policy_ref": "N/A — lỗi kỹ thuật",
            "confidence": "high",
            "evidence": "Model AI không thể kiểm tra caption.",
            "remediation": "Kiểm tra cấu hình model rồi thử lại.",
        })
    elif ai_verdict:
        model_status = str(ai_verdict.get("status", "")).upper()
        if model_status == "BLOCKED":
            findings.append({
                "category": "fraud_deception",
                "label": "Caption gây hiểu nhầm",
                "severity": SEVERITY_VIOLATION,
                "action": ACTION_BLOCK,
                "policy_ref": "Community Standards - Fraud & Deception",
                "confidence": "medium",
                "evidence": str(ai_verdict.get("reason", ""))[:300],
                "remediation": "Xóa lời hứa không căn cứ, quảng cáo sai sự thật.",
            })
    return findings


def _map_caption_issue(issue_category: str) -> str | None:
    mapping = {
        "abusive": "hate_speech",
        "discrimination": "hate_speech",
        "adult": "nudity_sexual",
        "violence": "violence_threats",
        "scam": "fraud_deception",
        "misleading": "fraud_deception",
        "commercial": "restricted_goods",
        "spam": "spam_duplicate",
        "format": "spam_duplicate",
    }
    return mapping.get(issue_category)


def evaluate_copyright(
    known_media: list[dict],
    video_sha256: str,
    video_signature: str,
    has_verification_source: bool = False,
) -> list[dict]:
    """Evaluate copyright: internal duplicate check + verification status."""
    findings: list[dict] = []
    rule = get_rule("copyright")

    # Internal duplicate check — this is NOT copyright verification.
    for item in known_media:
        if item.get("sha256") == video_sha256:
            findings.append({
                "category": "spam_duplicate",
                "label": "Nội dung trùng với video đã có trong Workspace",
                "severity": SEVERITY_DISTRIBUTION_RISK,
                "action": ACTION_REVIEW,
                "policy_ref": "Community Standards - Spam & Fake Engagement",
                "confidence": "high",
                "evidence": f"Trùng đúng với một video đã có trong Workspace (media_id={item.get('path', '')})",
                "remediation": "Xác nhận đây không phải nội dung tái đăng.",
            })
            break
        if video_signature and item.get("signature"):
            from services.media_fingerprint import hamming_distance
            distance = hamming_distance(video_signature, item["signature"])
            if distance <= 10:
                findings.append({
                    "category": "spam_duplicate",
                    "label": "Video gần giống với nội dung đã có trong Workspace",
                    "severity": SEVERITY_DISTRIBUTION_RISK,
                    "action": ACTION_REVIEW,
                    "policy_ref": "Community Standards - Spam & Fake Engagement",
                    "confidence": "medium",
                    "evidence": f"Perceptual hash distance={distance} bit",
                    "remediation": "Xác nhận quyền sở hữu nội dung.",
                })
                break

    # Copyright verification status — separate from duplicate check.
    if not has_verification_source:
        findings.append({
            "category": "copyright",
            "label": "Chưa xác minh bản quyền",
            "severity": SEVERITY_COPYRIGHT,
            "action": ACTION_REVIEW,
            "policy_ref": rule.policy_ref if rule else "Intellectual Property Policy",
            "confidence": "high",
            "evidence": "Chưa có nguồn xác minh quyền sử dụng nội dung.",
            "remediation": "Cung cấp giấy tờ chứng minh quyền sở hữu hoặc sử dụng nội dung tự sản xuất.",
        })

    return findings


def evaluate_technical(
    tech_report: dict,
    policy,
    black_frames: dict | None = None,
    frozen_frames: dict | None = None,
) -> list[dict]:
    """Evaluate technical quality results."""
    findings: list[dict] = []
    status = tech_report.get("status", "pass")
    for error in tech_report.get("errors", []):
        findings.append({
            "category": "technical_quality",
            "label": "Lỗi kỹ thuật video",
            "severity": "technical_quality",
            "action": ACTION_BLOCK,
            "policy_ref": "Reels Publishing Requirements",
            "confidence": "high",
            "evidence": error,
            "remediation": "Sửa kỹ thuật video và tải lên lại.",
        })
    for warning in tech_report.get("warnings", []):
        findings.append({
            "category": "low_quality",
            "label": "Cảnh báo chất lượng",
            "severity": SEVERITY_DISTRIBUTION_RISK,
            "action": ACTION_REVIEW,
            "policy_ref": "Reels Recommendation Guidelines",
            "confidence": "high",
            "evidence": warning,
            "remediation": "Cải thiện chất lượng video.",
        })
    if black_frames and black_frames.get("ratio", 0) > (getattr(policy, "max_black_ratio", 0.4)):
        findings.append({
            "category": "graphic_content",
            "label": "Khoảng thời gian quá dài với khung hình đen",
            "severity": SEVERITY_VIOLATION,
            "action": ACTION_BLOCK,
            "policy_ref": "Community Standards - Graphic Violence",
            "confidence": "high",
            "evidence": f"Tỉ lệ khung hình đen: {black_frames.get('ratio', 0):.1%}",
            "remediation": "Cắt bỏ đoạn quá sống động không có hình ảnh.",
        })
    return findings


def compute_final_status(
    all_findings: list[dict],
    category_statuses: dict[str, str],
    has_any_checked: bool,
) -> str:
    """Compute the final outcome from findings and category statuses.

    Rules:
    - Any ``violation`` severity finding → BLOCKED (content violation blocker)
    - Any ``technical_quality`` severity finding → BLOCKED (file-level blocker)
    - Any ``check_failed`` category status → NEEDS_REVIEW (can't auto-pass)
    - Any ``unknown`` category status → NEEDS_REVIEW
    - Any ``distribution_risk`` or ``copyright`` severity finding → NEEDS_REVIEW
    - All pass → PASS
    """
    has_check_failed = False
    has_unknown = False
    has_violation = False
    has_technical = False
    has_review = False

    for finding in all_findings:
        severity = finding.get("severity", "")
        if severity == SEVERITY_VIOLATION:
            has_violation = True
        elif severity == "technical_quality":
            has_technical = True
        elif severity in (SEVERITY_DISTRIBUTION_RISK, SEVERITY_COPYRIGHT):
            has_review = True
        # "check_failed" severity does not count here; it's tracked via category status.

    for status in category_statuses.values():
        if status == "check_failed":
            has_check_failed = True
        elif status == "unknown":
            has_unknown = True

    if has_violation or has_technical:
        return OUTCOME_BLOCK
    if has_check_failed or has_unknown or has_review:
        return OUTCOME_REVIEW
    return OUTCOME_PASS


def build_issues(findings: list[dict]) -> list[dict]:
    """Convert raw findings into the final issue objects with timestamps."""
    issues: list[dict] = []
    seen = set()
    for finding in findings:
        key = (finding.get("category", ""), finding.get("timestamp") or 0.0)
        if key in seen:
            continue
        seen.add(key)
        issues.append({
            "policy_category": finding.get("category", "unknown"),
            "label": finding.get("label", ""),
            "risk_level": _severity_label(finding.get("severity", "")),
            "action": finding.get("action", ACTION_REVIEW),
            "reason": finding.get("evidence", ""),
            "timestamp": finding.get("timestamp"),
            "evidence": finding.get("evidence", ""),
            "remediation": finding.get("remediation", ""),
            "confidence": finding.get("confidence", "medium"),
            "policy_ref": finding.get("policy_ref", ""),
        })
    return issues


def _severity_label(severity: str) -> str:
    mapping = {
        SEVERITY_VIOLATION: "high",
        SEVERITY_DISTRIBUTION_RISK: "medium",
        SEVERITY_COPYRIGHT: "medium",
        "technical_quality": "high",
        "check_failed": "high",
    }
    return mapping.get(severity, "low")
