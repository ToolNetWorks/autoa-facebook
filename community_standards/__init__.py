"""Community Standards policy rules for Meta/Facebook content moderation.

The rules are versioned so that a pre-check made last month can be reproduced
even when the policy text has been updated since.  Each rule binds a
``category`` (one of the 12 community-standards buckets) to a ``severity``
(``violation`` / ``distribution_risk`` / ``copyright``) and an ``action``
(``block`` or ``review``).
"""
from community_standards.policy_rules import (
    POLICY_VERSION,
    CATEGORIES,
    CATEGORY_BY_KEY,
    SEVERITY_VIOLATION,
    SEVERITY_DISTRIBUTION_RISK,
    SEVERITY_COPYRIGHT,
    ACTION_BLOCK,
    ACTION_REVIEW,
    OUTCOME_PASS,
    OUTCOME_REVIEW,
    OUTCOME_BLOCK,
    OUTCOME_CHECK_FAILED,
    CategoryRule,
    get_rule,
    evaluate_category,
)

__all__ = [
    "POLICY_VERSION",
    "CATEGORIES",
    "CATEGORY_BY_KEY",
    "SEVERITY_VIOLATION",
    "SEVERITY_DISTRIBUTION_RISK",
    "SEVERITY_COPYRIGHT",
    "ACTION_BLOCK",
    "ACTION_REVIEW",
    "OUTCOME_PASS",
    "OUTCOME_REVIEW",
    "OUTCOME_BLOCK",
    "OUTCOME_CHECK_FAILED",
    "CategoryRule",
    "get_rule",
    "evaluate_category",
]
