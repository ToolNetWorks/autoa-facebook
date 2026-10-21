"""Exception hierarchy for AWS Rekognition errors."""
from __future__ import annotations

from botocore.exceptions import (
    BotoCoreError,
    ConnectionError as BotoConnectionError,
    EndpointConnectionError,
)


class RekognitionError(Exception):
    """Base error for all Rekognition-related failures."""


class AccessDeniedError(RekognitionError):
    """Permanent — IAM / SCP denies access.  Do not retry."""


class BadRequestError(RekognitionError):
    """Permanent — malformed request (e.g. image too large).  Do not retry."""


class ThrottlingError(RekognitionError):
    """Temporary — AWS rate-limited.  Retry with back-off."""


class ProviderTimeoutError(RekognitionError):
    """Temporary — the request timed out or could not connect."""


def classify_botocore_error(error: Exception) -> RekognitionError:
    """Map a raw botocore / boto3 exception into a typed RekognitionError."""
    if isinstance(error, AccessDeniedError):
        return error
    if isinstance(error, (BotoConnectionError, EndpointConnectionError)):
        return ProviderTimeoutError(str(error))
    if isinstance(error, BotoCoreError):
        return ProviderTimeoutError(str(error))
    # botocore.client.ClientError has a .response dict
    if hasattr(error, "response"):
        error_code = error.response.get("Error", {}).get("Code", "")
        error_message = error.response.get("Error", {}).get("Message", str(error))
        if error_code == "AccessDeniedException":
            return AccessDeniedError(error_message)
        if error_code == "ThrottlingException":
            return ThrottlingError(error_message)
        if error_code in ("InvalidParameterException", "InvalidImageException",
                          "ImageTooLargeException", "InvalidDimensionException"):
            return BadRequestError(f"{error_code}: {error_message}")
        if error_code == "TooManyRequestsException":
            return ThrottlingError(error_message)
        return RekognitionError(f"{error_code}: {error_message}")
    return RekognitionError(str(error))
