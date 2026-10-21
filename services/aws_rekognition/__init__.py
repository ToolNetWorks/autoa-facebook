"""AWS Rekognition vision engine for the video pre-check.

This package provides a thin, well-typed wrapper around the Rekognition
Image APIs (``DetectModerationLabels`` and ``DetectLabels``).  It is used
by :mod:`services.video_precheck` as the primary vision engine.

Error handling:

* ``AccessDeniedException`` → :class:`services.aws_rekognition.exceptions.AccessDeniedError`
  (permanent — do not retry, report ``CHECK_FAILED``).
* ``ThrottlingException``  → :class:`services.aws_rekognition.exceptions.ThrottlingError`
  (retry with exponential back-off).
* ``InvalidParameterException`` → :class:`services.aws_rekognition.exceptions.BadRequestError`
  (permanent — do not retry).
* Connection / timeout → :class:`services.aws_rekognition.exceptions.ProviderTimeoutError`
  (retry once, then ``CHECK_FAILED``).

The module never stores AWS responses in the database beyond the mapped
category labels and confidence scores.  Raw JSON is kept only in memory.
"""
from __future__ import annotations

from services.aws_rekognition.client import (
    RekognitionClient,
    RekognitionConfig,
    load_config,
    client_from_env,
)
from services.aws_rekognition.exceptions import (
    AccessDeniedError,
    BadRequestError,
    RekognitionError,
    ThrottlingError,
    ProviderTimeoutError,
)

__all__ = [
    "RekognitionClient",
    "RekognitionConfig",
    "load_config",
    "client_from_env",
    "RekognitionError",
    "AccessDeniedError",
    "BadRequestError",
    "ThrottlingError",
    "ProviderTimeoutError",
]
