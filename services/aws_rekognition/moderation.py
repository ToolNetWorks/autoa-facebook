"""High-level helpers for calling Rekognition moderation/labels."""
from __future__ import annotations

from services.aws_rekognition.client import RekognitionClient


def analyze_frame_moderation(
    client: RekognitionClient,
    image_bytes: bytes,
    min_confidence: float | None = None,
) -> list[dict]:
    """Call DetectModerationLabels and return normalized label list."""
    return client.detect_moderation_labels(image_bytes, min_confidence)


def analyze_frame_labels(
    client: RekognitionClient,
    image_bytes: bytes,
    min_confidence: float | None = None,
    max_labels: int | None = None,
) -> list[dict]:
    """Call DetectLabels and return normalized label list."""
    return client.detect_labels(image_bytes, min_confidence, max_labels)