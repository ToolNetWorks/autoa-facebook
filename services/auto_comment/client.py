"""AWS Rekognition client configuration and factory."""
from __future__ import annotations

import os
import logging
import time
from dataclasses import dataclass

import boto3
from botocore.config import Config as BotoConfig

from services.aws_rekognition.exceptions import (
    AccessDeniedError,
    BadRequestError,
    RekognitionError,
    ThrottlingError,
    ProviderTimeoutError,
    classify_botocore_error,
)

LOG = logging.getLogger("facebook-pipeline.aws-rekognition.client")


def _int_env(name: str, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, fallback))))
    except (TypeError, ValueError):
        return fallback


def _float_env(name: str, fallback: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.getenv(name, fallback))))
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class RekognitionConfig:
    """Runtime configuration for the Rekognition client."""
    enabled: bool
    region: str
    access_key_id: str
    secret_access_key: str
    max_frames: int
    timeout: int
    max_retries: int
    min_confidence: float
    max_labels: int

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)


def load_config() -> RekognitionConfig:
    enabled = os.getenv("AWS_REKOGNITION_ENABLED", "false").lower() in ("1", "true", "yes")
    region = os.getenv("AWS_REGION", "").strip()
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
    return RekognitionConfig(
        enabled=enabled,
        region=region or "us-east-1",
        access_key_id=access_key,
        secret_access_key=secret_key,
        max_frames=_int_env("AWS_REKOGNITION_MAX_FRAMES", 10, 1, 50),
        timeout=_int_env("AWS_REKOGNITION_TIMEOUT", 30, 5, 120),
        max_retries=_int_env("AWS_REKOGNITION_MAX_RETRIES", 3, 0, 10),
        min_confidence=_float_env("AWS_REKOGNITION_MIN_CONFIDENCE", 60.0, 1.0, 99.0),
        max_labels=_int_env("AWS_REKOGNITION_MAX_LABELS", 20, 1, 100),
    )


class RekognitionClient:
    """Wrapper around the Rekognition Image API with retry logic."""

    def __init__(self, config: RekognitionConfig):
        self.config = config
        self._calls_total = 0
        self._calls_failed = 0
        self._session = boto3.Session(
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name=config.region,
        )
        self._client = self._session.client(
            "rekognition",
            config=BotoConfig(
                connect_timeout=config.timeout,
                read_timeout=config.timeout,
                max_pool_connections=4,
                retries={"max_attempts": 0},
            ),
        )

    @property
    def call_count(self) -> int:
        return self._calls_total

    @property
    def failed_count(self) -> int:
        return self._calls_failed

    def _retry_sleep(self, attempt: int) -> None:
        delay = min(8, 1.5 * (attempt + 1))
        LOG.debug("Rekognition retry: sleeping %.1fs (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    def detect_moderation_labels(self, image_bytes: bytes,
                                 min_confidence: float | None = None) -> list[dict]:
        self._calls_total += 1
        conf = min_confidence or self.config.min_confidence
        last_error: RekognitionError | None = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.detect_moderation_labels(
                    Image={"Bytes": image_bytes},
                    MinConfidence=conf,
                )
                labels = response.get("ModerationLabels", [])
                result: list[dict] = []
                for label in labels:
                    parent = label.get("ParentName") or ""
                    result.append({
                        "name": label.get("Name", ""),
                        "confidence": float(label.get("Confidence", 0)),
                        "parent": parent,
                        "id": label.get("Name", ""),
                    })
                LOG.debug("DetectModerationLabels: %d labels returned", len(result))
                return result
            except Exception as exc:
                cls = classify_botocore_error(exc)
                last_error = cls
                if isinstance(cls, (AccessDeniedError, BadRequestError)):
                    self._calls_failed += 1
                    raise
                if attempt < self.config.max_retries:
                    self._retry_sleep(attempt)
                else:
                    self._calls_failed += 1
                    raise

        self._calls_failed += 1
        raise last_error  # type: ignore[misc]

    def detect_labels(self, image_bytes: bytes,
                      min_confidence: float | None = None,
                      max_labels: int | None = None) -> list[dict]:
        self._calls_total += 1
        conf = min_confidence or self.config.min_confidence
        limit = max_labels or self.config.max_labels
        last_error: RekognitionError | None = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._client.detect_labels(
                    Image={"Bytes": image_bytes},
                    MinConfidence=conf,
                    MaxLabels=limit,
                )
                labels = response.get("Labels", [])
                result = [
                    {"name": l.get("Name", ""), "confidence": float(l.get("Confidence", 0))}
                    for l in labels
                ]
                LOG.debug("DetectLabels: %d labels returned", len(result))
                return result
            except Exception as exc:
                cls = classify_botocore_error(exc)
                last_error = cls
                if isinstance(cls, (AccessDeniedError, BadRequestError)):
                    self._calls_failed += 1
                    raise
                if attempt < self.config.max_retries:
                    self._retry_sleep(attempt)
                else:
                    self._calls_failed += 1
                    raise

        self._calls_failed += 1
        raise last_error  # type: ignore[misc]


def client_from_env() -> RekognitionClient | None:
    """Return a configured client, or ``None`` if not enabled."""
    config = load_config()
    if not config.enabled:
        return None
    return RekognitionClient(config)