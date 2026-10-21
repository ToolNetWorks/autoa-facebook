"""Transcription client: wraps :mod:`services.ai_provider` for ASR.

The ASR path wraps the extracted audio in a minimal video container so that
providers which only accept ``video_url`` (e.g. alims-intl) can read the
audio track.  See :func:`services.ai_provider.transcribe_json`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from services import ai_provider

LOG = logging.getLogger("facebook-pipeline.transcription")


ASR_SYSTEM_PROMPT = """Bạn kiểm tra lời thoại của một video ngắn.
Trả về đúng một đối tượng JSON:
{"speech_detected": true,
 "text": "toàn bộ lời thoại đã nghe được",
 "risks": [{"time": 0.0, "category": "none|abusive|offensive|misleading|sensitive|inappropriate", "note": "mô tả ngắn"}]}
Chỉ nêu những gì thực sự nghe được. Phân biệt lỗi nhận dạng giọng nói với vi phạm thật."""


def transcribe(
    audio_path: Path,
    provider_config: ai_provider.ProviderConfig,
    timeout: int = 0,
    work_dir: Path | None = None,
) -> dict | None:
    """Transcribe the audio track.

    Returns the JSON answer, or ``None`` when the model is not configured.
    Raises :class:`ProviderError` on provider errors so the caller can
    distinguish a technical failure from a missing configuration.
    """
    if not provider_config.asr_configured:
        LOG.warning("ASR model not configured; cannot transcribe audio")
        return None
    try:
        caps = ai_provider.discover_models(provider_config)
        model_name = provider_config.asr_model
        if model_name and model_name in caps:
            info = caps[model_name]
            if not info.get("audio", False) and not info.get("vision", False):
                LOG.error("ASR model %s does not support audio/vision input", model_name)
                raise ai_provider.ProviderError(f"ASR model '{model_name}' does not support audio input")
    except Exception:
        LOG.error("ASR capability check failed")
        raise
    # Exceptions propagate to the caller for distinction of error vs not-configured.
    return ai_provider.transcribe_json(audio_path, provider_config, timeout=timeout, work_dir=work_dir)
