"""Report builder: assembles the final pre-check report from all findings.

The report has three layers:
1. per-category ``status`` (pass / needs_review / blocked / unknown / check_failed)
2. a flat list of ``issues`` (one per finding with policy category, risk level,
   reason, timestamp, evidence and remediation)
3. a top-level ``status`` (PASS / NEEDS_REVIEW / BLOCKED / CHECK_FAILED)
"""
from __future__ import annotations

import logging
from typing import Any

from community_standards import (
    OUTCOME_BLOCK,
    OUTCOME_CHECK_FAILED,
    OUTCOME_PASS,
    OUTCOME_REVIEW,
    POLICY_VERSION,
    SEVERITY_VIOLATION,
    SEVERITY_DISTRIBUTION_RISK,
    SEVERITY_COPYRIGHT,
)
from policy_evaluator import (
    compute_final_status,
    build_issues,
)

LOG = logging.getLogger("facebook-pipeline.report")

# Category keys that map to DB columns.
STATUS_PASS = "pass"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_BLOCKED = "blocked"
STATUS_UNKNOWN = "unknown"
STATUS_CHECK_FAILED = "check_failed"

CATEGORY_TECHNICAL = "technical"
CATEGORY_VISUAL = "visual"
CATEGORY_AUDIO = "audio"
CATEGORY_CAPTION = "caption"
CATEGORY_OCR = "ocr"
CATEGORY_COPYRIGHT = "copyright"
CATEGORIES = (
    CATEGORY_TECHNICAL,
    CATEGORY_VISUAL,
    CATEGORY_AUDIO,
    CATEGORY_OCR,
    CATEGORY_CAPTION,
    CATEGORY_COPYRIGHT,
)


def _category(
    status: str,
    reasons: list[str] | None = None,
    suggestions: list[str] | None = None,
    details: dict | None = None,
    checked: bool = True,
    score: float = 0.0,
    checked_scope: str = "",
) -> dict:
    return {
        "status": status,
        "checked": checked,
        "score": round(score, 3),
        "reasons": list(reasons or []),
        "suggestions": list(suggestions or []),
        "details": details or {},
        "checked_scope": checked_scope,
    }


def build_category_report(
    status: str,
    reasons: list[str] | None = None,
    suggestions: list[str] | None = None,
    details: dict | None = None,
    checked: bool = True,
    score: float = 0.0,
    checked_scope: str = "",
) -> dict:
    """Public helper to build a single category dict."""
    return _category(status, reasons, suggestions, details, checked, score, checked_scope)


def build_report(
    categories: dict[str, dict],
    all_findings: list[dict],
    model_version: str = "",
    vision_model: str = "",
    provider: str = "none",
    checked_scope: str = "",
    video_info: dict | None = None,
    duration: float | None = None,
) -> dict[str, Any]:
    """Assemble the complete pre-check report."""
    issues = build_issues(all_findings)
    category_statuses = {name: cat.get("status", "unknown") for name, cat in categories.items()}
    final = compute_final_status(all_findings, category_statuses,
                                  has_any_checked=any(c.get("checked") for c in categories.values()))

    warnings: list[str] = []
    recommendations: list[str] = []
    for name in CATEGORIES:
        cat = categories.get(name, {})
        for reason in cat.get("reasons", []):
            warnings.append(f"{name}: {reason}")
        for suggestion in cat.get("suggestions", []):
            recommendations.append(suggestion)

    # Add policy-level warnings from findings.
    for issue in issues:
        if issue.get("risk_level") == "high":
            warnings.append(f"[{issue.get('policy_category')}]: {issue.get('reason', '')[:200]}")
        if issue.get("remediation"):
            rec = issue["remediation"]
            if rec not in recommendations:
                recommendations.append(rec)

    max_score = max((cat.get("score", 0) for cat in categories.values()), default=0.0)

    # Map internal outcome to external status label.
    status_map = {
        OUTCOME_PASS: "PASS",
        OUTCOME_REVIEW: "NEEDS_REVIEW",
        OUTCOME_BLOCK: "BLOCKED",
        OUTCOME_CHECK_FAILED: "NEEDS_REVIEW",
    }

    return {
        "status": status_map.get(final, "NEEDS_REVIEW"),
        "internal_status": final,
        "score": round(max_score, 3),
        "categories": categories,
        "issues": issues,
        "warnings": warnings,
        "recommendations": recommendations,
        "model_version": model_version,
        "vision_model": vision_model,
        "provider": provider,
        "policy_version": POLICY_VERSION,
        "checked_scope": checked_scope,
        "video_info": video_info or {},
        "duration_seconds": duration,
        "note": ("Kết quả là gợi ý của công cụ, không phải dự đoán quyết định kiểm duyệt của Facebook. "
                 "Mỗi nội dung vi phạm sẽ được đưa ra dựa trên bằng chứng cụ thể."),
    }
