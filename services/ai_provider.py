"""A small OpenAI-compatible client for the text and vision models.

The gateway in use answers with a normal chat completion body and sometimes
appends the stream terminator, so the reader accepts both. Nothing is sent
anywhere unless a provider is configured, and the API key never reaches a log.

Real error tracing (2026-10-20):
- ``AI_VISION_MODEL`` was set to ``alims-intl/tongyi-embedding-vision-plus``
  which is an *embedding* model, not a chat/vision model.  The gateway could
  not find it and fell back to the deprecated ``opencode/mimo-v2.5-free``,
  returning HTTP 410.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("facebook-pipeline.ai")

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_RETRIES = 2
DEFAULT_MODEL = "gpt-4o-mini"
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    """The provider could not be used."""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = DEFAULT_MODEL
    vision_model: str = ""
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def vision_configured(self) -> bool:
        return self.configured and bool(self.vision_model)


# --- model capability discovery -----------------------------------------------

_MODEL_CACHE: dict[str, dict] = {}
_MODEL_CACHE_TIME: float = 0.0
_MODEL_CACHE_TTL: int = 300  # seconds


def discover_models(config: ProviderConfig, force: bool = False) -> dict[str, dict]:
    """Query the ``/models`` endpoint and cache the result.

    Each entry maps a model id to its capabilities, e.g.::

        {"vision": True, "audio": False, "pdf": False}

    The cache is per-process and TTL-gated so that burst checks do not hammer
    the endpoint.
    """
    global _MODEL_CACHE, _MODEL_CACHE_TIME
    now = time.monotonic()
    if not force and config.base_url and _MODEL_CACHE and now - _MODEL_CACHE_TIME < _MODEL_CACHE_TTL:
        return _MODEL_CACHE
    if not config.configured:
        return {}
    endpoint = f"{config.base_url}/models"
    request = urllib.request.Request(endpoint,
                                     headers={"Authorization": f"Bearer {config.api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=min(config.timeout, 15)) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as error:
        LOG.warning("Could not discover models: %s", type(error).__name__)
        return _MODEL_CACHE if _MODEL_CACHE else {}
    cache: dict[str, dict] = {}
    for item in data.get("data", []) or []:
        model_id = item.get("id", "")
        if not model_id:
            continue
        caps = item.get("capabilities", {}) or {}
        cache[model_id] = {
            "vision": bool(caps.get("vision", False)),
            "audio": bool(caps.get("audioInput", False)),
            "pdf": bool(caps.get("pdf", False)),
        }
    _MODEL_CACHE = cache
    _MODEL_CACHE_TIME = now
    return cache


def model_supports(config: ProviderConfig, model_name: str, capability: str) -> bool:
    """Return True when *model_name* advertises the given capability.

    Falls back to ``False`` when the models list cannot be fetched, which
    means the caller must treat the model as unsupported and either try a
    fallback or mark the check as failed.
    """
    models = discover_models(config)
    info = models.get(model_name, {})
    return bool(info.get(capability, False))


_VISION_FALLBACKS = (
    "alims-intl/qwen3-vl-8b-instruct",
    "alims-intl/qwen3-vl-8b-thinking",
    "alims-intl/qwen-vl-ocr-2025-11-20",
)


def _int_env(name: str, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, fallback))))
    except (TypeError, ValueError):
        return fallback


def load_config(model_override: str = "", vision_override: str = "") -> ProviderConfig:
    """Read the provider configuration from the environment."""
    base_url = (os.getenv("AI_BASE_URL") or os.getenv("AI_URL") or "").strip().rstrip("/")
    api_key = (os.getenv("AI_API_KEY") or os.getenv("AI_KEY") or "").strip()
    model = (model_override or os.getenv("AI_MODEL") or DEFAULT_MODEL).strip()
    vision = (vision_override or os.getenv("AI_VISION_MODEL") or "").strip()
    return ProviderConfig(base_url=base_url, api_key=api_key, model=model, vision_model=vision,
                          timeout=_int_env("AI_TIMEOUT", DEFAULT_TIMEOUT, 5, 600),
                          max_retries=_int_env("AI_MAX_RETRIES", DEFAULT_MAX_RETRIES, 0, 5))


def parse_answer(raw: str) -> str:
    """Read the message out of a body that may carry the stream terminator."""
    text = (raw or "").strip()
    for marker in ("data: [DONE]", "data:[DONE]"):
        if text.endswith(marker):
            text = text[: -len(marker)].strip()
    if text.startswith("data:"):
        chunks = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                chunks.append(delta.get("content") or "")
        if chunks:
            return "".join(chunks)
    try:
        return json.loads(text)["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise ProviderError("AI provider returned an unusable answer") from error


def extract_json(content: str) -> dict:
    """Pull a JSON object out of a model answer."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as error:
            raise ProviderError("AI provider returned a malformed JSON answer") from error
    raise ProviderError("AI provider returned no JSON answer")


def _post(config: ProviderConfig, payload: dict, timeout: int) -> str:
    body = json.dumps(payload).encode("utf-8")
    endpoint = f"{config.base_url}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.api_key}"}
    last = ""
    for attempt in range(config.max_retries + 1):
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return parse_answer(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
            if error.code not in RETRYABLE_STATUS:
                raise ProviderError(f"AI provider refused the request: {last}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = type(error).__name__
        if attempt < config.max_retries:
            time.sleep(min(8, 1.5 * (attempt + 1)))
    raise ProviderError(f"AI provider is unreachable: {last}")


def chat_json(system_prompt: str, user_prompt: str, config: ProviderConfig,
              model: str = "", timeout: int = 0) -> dict:
    """Ask the provider for a JSON answer."""
    if not config.configured:
        raise ProviderError("AI provider is not configured")
    payload = {"model": model or config.model,
               "messages": [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}],
               "temperature": 0.2, "response_format": {"type": "json_object"}}
    content = _post(config, payload, timeout or config.timeout)
    return extract_json(content)


def image_data_url(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".") or "jpg"
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp"}.get(suffix, "image/jpeg")
    encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def vision_json(system_prompt: str, user_prompt: str, image_paths, config: ProviderConfig,
                model: str = "", timeout: int = 0) -> dict:
    """Ask a vision model about a set of frames."""
    if not config.vision_configured:
        raise ProviderError("AI vision model is not configured")
    content = [{"type": "text", "text": user_prompt}]
    for path in image_paths:
        content.append({"type": "image_url", "image_url": {"url": image_data_url(Path(path))}})
    payload = {"model": model or config.vision_model, "messages": [{"role": "user", "content": content}],
               "temperature": 0.1}
    answer = _post(config, payload, timeout or config.timeout)
    return extract_json(answer)
