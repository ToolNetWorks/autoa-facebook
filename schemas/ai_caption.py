"""Schemas for the AI Caption Automation pipeline.

The structures mirror the JSON contract of an OpenAI-compatible provider, but
they are also the contract of the local engine, so a provider response and a
locally produced result are interchangeable.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

STATUS_APPROVED = "APPROVED"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_BLOCKED = "BLOCKED"
STATUS_PENDING = "PENDING"

CAPTION_STATUSES = (STATUS_APPROVED, STATUS_NEEDS_REVIEW, STATUS_BLOCKED, STATUS_PENDING)

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_UNKNOWN = "unknown"

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"

TARGET_GROUP = "group"
TARGET_REEL = "reel"


class CaptionIssue(BaseModel):
    """One problem found in a caption."""
    category: str = Field(max_length=40)
    severity: str = Field(default=SEVERITY_LOW, max_length=10)
    message: str = Field(max_length=400)
    excerpt: str = Field(default="", max_length=200)


class CaptionChange(BaseModel):
    """One difference between the original and the rewritten caption."""
    kind: str = Field(default="formatting", max_length=40)
    summary: str = Field(max_length=400)
    before: str = Field(default="", max_length=2000)
    after: str = Field(default="", max_length=2000)


class CaptionAnalysis(BaseModel):
    """Result of the analyze step."""
    topic: str = Field(default="", max_length=300)
    intent: str = Field(default="", max_length=300)
    key_facts: list[str] = Field(default_factory=list)
    issues: list[CaptionIssue] = Field(default_factory=list)
    risk_level: str = Field(default=RISK_UNKNOWN, max_length=10)
    meaning_preserved: bool = True
    reason: str = Field(default="", max_length=1000)


class CaptionOptimization(BaseModel):
    """Result of the full analyze → rewrite → validate → decide pipeline."""
    status: str = Field(default=STATUS_NEEDS_REVIEW, max_length=20)
    original_caption: str = Field(default="", max_length=20000)
    optimized_caption: str = Field(default="", max_length=20000)
    risk_level: str = Field(default=RISK_UNKNOWN, max_length=10)
    meaning_preserved: bool = True
    issues: list[CaptionIssue] = Field(default_factory=list)
    changes: list[CaptionChange] = Field(default_factory=list)
    reason: str = Field(default="", max_length=1000)
    ai_model: str = Field(default="", max_length=120)
    provider: str = Field(default="local", max_length=40)
    engine: str = Field(default="local", max_length=40)
    topic: str = Field(default="", max_length=300)
    intent: str = Field(default="", max_length=300)
    key_facts: list[str] = Field(default_factory=list)


class CaptionSettings(BaseModel):
    """Per-workspace configuration of the caption automation."""
    enabled: bool = True
    tone: str = Field(default="", max_length=200)
    language: str = Field(default="vi", max_length=20)
    model: str = Field(default="", max_length=120)
    target: str = Field(default=TARGET_GROUP, max_length=20)
    add_hashtags: bool = False
    preserve_meaning: bool = True
    optimize_for_reel: bool = False
    optimize_for_group: bool = False
    auto_use_when_safe: bool = False
    require_manual_review: bool = True
    max_hashtags: int = Field(default=5, ge=0, le=20)


class CaptionSettingsUpdate(BaseModel):
    """Partial update of the per-workspace configuration."""
    enabled: bool | None = None
    tone: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default=None, max_length=20)
    model: str | None = Field(default=None, max_length=120)
    target: str | None = Field(default=None, max_length=20)
    add_hashtags: bool | None = None
    preserve_meaning: bool | None = None
    optimize_for_reel: bool | None = None
    optimize_for_group: bool | None = None
    auto_use_when_safe: bool | None = None
    require_manual_review: bool | None = None
    max_hashtags: int | None = Field(default=None, ge=0, le=20)


class CaptionOptimizeRequest(BaseModel):
    caption: str = Field(min_length=1, max_length=20000)
    target: str = Field(default=TARGET_GROUP, max_length=20)
    tone: str = Field(default="", max_length=200)


class CaptionApproveRequest(BaseModel):
    record_id: int
    optimized_caption: str = Field(default="", max_length=20000)
