"""AI Caption Automation: analyze → rewrite → validate → publish decision.

The provider is optional and configured through the environment:

    AI_BASE_URL   e.g. https://api.openai.com/v1   (AI_URL is accepted too)
    AI_API_KEY    never hardcoded, never written to a log  (AI_KEY is accepted too)
    AI_MODEL      default model for every workspace
    AI_TIMEOUT    seconds per request
    AI_MAX_RETRIES  extra attempts on a transient failure

A workspace may override the model in its caption settings.

The provider is never trusted. Its answer is parsed into
`schemas.ai_caption.CaptionOptimization` and then re-checked by the local
validator: the status, the risk level and the meaning check are always decided
by this backend, and wording that violates policy is reported instead of being
laundered.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from schemas.ai_caption import (
    RISK_HIGH, RISK_LOW, RISK_MEDIUM, RISK_UNKNOWN, SEVERITY_HIGH, SEVERITY_LOW,
    SEVERITY_MEDIUM, STATUS_APPROVED, STATUS_BLOCKED, STATUS_NEEDS_REVIEW,
    CaptionAnalysis, CaptionChange, CaptionIssue, CaptionOptimization, CaptionSettings,
)
from services.caption_validator import (
    HIGH_SEVERITY_CATEGORIES, analyze_caption, meaning_preserved, rewrite_locally,
)

LOG = logging.getLogger("facebook-pipeline.ai-caption")

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 2
DEFAULT_MODEL = "gpt-4o-mini"
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

SYSTEM_PROMPT = """Bạn là biên tập viên caption cho Facebook.
Nhiệm vụ: viết lại caption mà người dùng đã viết, cho tự nhiên, dễ đọc, ít spam.
Bắt buộc:
- Giữ nguyên ý nghĩa, số liệu, tên riêng và thông tin sự kiện.
- Không bịa thêm thông tin, không thêm cam kết hoặc lời hứa mà người dùng không nói.
- Không dùng ký tự đặc biệt, mã hóa, biến thể chính tả hoặc kỹ thuật nào để né bộ lọc kiểm duyệt.
- Nếu nội dung vi phạm chính sách (lăng mạ, phân biệt đối xử, nội dung người lớn, bạo lực, lừa đảo, cam kết sai lệch), đừng sửa để né; hãy trả về optimized_caption bằng chính văn bản gốc và nêu rõ vấn đề.
Trả về đúng một đối tượng JSON, không thêm văn bản khác:
{"optimized_caption": "...", "topic": "...", "intent": "...", "key_facts": ["..."],
 "issues": [{"category": "...", "severity": "low|medium|high", "message": "...", "excerpt": "..."}],
 "changes": [{"kind": "...", "summary": "...", "before": "...", "after": "..."}],
 "reason": "..."}"""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


def load_provider_config(model_override: str = "") -> ProviderConfig:
    """Read the provider configuration from the environment."""
    # Both spellings are accepted: the documented AI_BASE_URL / AI_API_KEY and
    # the AI_URL / AI_KEY names already present in the deployment .env.
    base_url = (os.getenv("AI_BASE_URL") or os.getenv("AI_URL") or "").strip().rstrip("/")
    api_key = (os.getenv("AI_API_KEY") or os.getenv("AI_KEY") or "").strip()
    model = (model_override or os.getenv("AI_MODEL", "") or DEFAULT_MODEL).strip()
    try:
        timeout = max(5, int(os.getenv("AI_TIMEOUT", DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    try:
        retries = max(0, min(5, int(os.getenv("AI_MAX_RETRIES", DEFAULT_MAX_RETRIES))))
    except ValueError:
        retries = DEFAULT_MAX_RETRIES
    return ProviderConfig(base_url=base_url, api_key=api_key, model=model,
                          timeout=timeout, max_retries=retries)


def parse_provider_response(raw: str) -> dict:
    """Read an answer from an OpenAI-compatible gateway.

    Some gateways append the stream terminator to an ordinary JSON body,
    others answer with a real server sent event stream. Both are accepted.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty answer")
    for marker in ("data: [DONE]", "data:[DONE]"):
        if text.endswith(marker):
            text = text[: -len(marker)].strip()
    if text.startswith("data:"):
        contents: list[str] = []
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
                contents.append(delta.get("content") or "")
        if contents:
            return {"choices": [{"message": {"content": "".join(contents)}}]}
    return json.loads(text)


def _extract_json(content: str) -> dict:
    """Pull a JSON object out of a model answer that may be fenced."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as error:
            raise ValueError("AI provider returned a malformed JSON answer") from error
    raise ValueError("AI provider returned no JSON answer")


class ProviderError(RuntimeError):
    """Raised when the AI provider cannot be used."""


def call_provider(config: ProviderConfig, caption: str, analysis: CaptionAnalysis,
                   settings: CaptionSettings, target: str) -> CaptionOptimization:
    """Ask the provider for a rewrite and validate the shape of the answer."""
    if not config.configured:
        raise ProviderError("AI provider is not configured")
    user_prompt = (
        f"Loại bài đăng: {'Reel trang cá nhân' if target == 'reel' else 'bài ảnh trong group'}\n"
        f"Văn phong mong muốn: {settings.tone or 'tự nhiên, thân thiện'}\n"
        f"Chủ đề phát hiện: {analysis.topic}\n"
        f"Ý định chính: {analysis.intent}\n"
        f"Thông tin phải giữ nguyên: {', '.join(analysis.key_facts) or '(không có)'}\n"
        f"Caption gốc:\n\"\"\"\n{caption}\n\"\"\"")
    payload = {
        "model": config.model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user_prompt}],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode("utf-8")
    endpoint = f"{config.base_url}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.api_key}"}
    last_error = ""
    for attempt in range(config.max_retries + 1):
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=config.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            data = parse_provider_response(raw)
            content = data["choices"][0]["message"]["content"]
            parsed = _extract_json(content)
            rewritten = str(parsed.get("optimized_caption") or parsed.get("caption")
                            or parsed.get("rewritten_caption") or parsed.get("text") or "").strip()
            if not rewritten:
                rewritten = text.strip()
            return CaptionOptimization(
                status=STATUS_NEEDS_REVIEW,
                original_caption=caption,
                optimized_caption=rewritten[:20000],
                issues=[CaptionIssue(**issue) for issue in parsed.get("issues", []) or []
                        if isinstance(issue, dict)],
                changes=[CaptionChange(**change) for change in parsed.get("changes", []) or []
                         if isinstance(change, dict)],
                reason=str(parsed.get("reason", ""))[:1000],
                topic=str(parsed.get("topic", ""))[:300],
                intent=str(parsed.get("intent", ""))[:300],
                key_facts=[str(fact)[:200] for fact in parsed.get("key_facts", []) or []],
                ai_model=config.model, provider="openai-compatible", engine="openai")
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}"
            if error.code not in RETRYABLE_STATUS:
                raise ProviderError(f"AI provider refused the request: {last_error}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = type(error).__name__
        except (ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
            raise ProviderError(f"AI provider returned an unusable answer: {type(error).__name__}") from error
        if attempt < config.max_retries:
            time.sleep(min(8, 1.5 * (attempt + 1)))
    raise ProviderError(f"AI provider is unreachable: {last_error}")


def _worst(issues: list[CaptionIssue]) -> str:
    if any(issue.severity == SEVERITY_HIGH for issue in issues):
        return SEVERITY_HIGH
    if any(issue.severity == SEVERITY_MEDIUM for issue in issues):
        return SEVERITY_MEDIUM
    if issues:
        return SEVERITY_LOW
    return SEVERITY_LOW


def _risk_from(issues: list[CaptionIssue]) -> str:
    severity = _worst(issues)
    if severity == SEVERITY_HIGH:
        return RISK_HIGH
    if severity == SEVERITY_MEDIUM:
        return RISK_MEDIUM
    return RISK_LOW


def decide(original_analysis: CaptionAnalysis, optimized: str, local_issues: list[CaptionIssue],
           meaning_ok: bool, provider_failed: bool,
           provider_issues: list[CaptionIssue] | None = None) -> tuple[str, str, str, bool]:
    """Decide the publish status from local evidence only.

    Wording that breaks the rules in the original caption is reported and must
    be fixed by a person, so it is never silently rewritten into something that
    only looks acceptable.
    """
    original_blocking = [issue for issue in original_analysis.issues
                         if issue.category in HIGH_SEVERITY_CATEGORIES]
    if original_blocking:
        reasons = "; ".join(issue.message for issue in original_blocking[:5])
        return STATUS_BLOCKED, RISK_HIGH, (
            "Nội dung gốc vi phạm chính sách, cần người dùng sửa lại nội dung thực chất: " + reasons), meaning_ok

    if provider_failed:
        return (STATUS_NEEDS_REVIEW, _risk_from(local_issues),
                "Dịch vụ AI không phản hồi; đã dùng bộ kiểm tra cục bộ và cần người duyệt.", meaning_ok)

    remaining = [issue for issue in local_issues
                 if issue.severity in (SEVERITY_HIGH, SEVERITY_MEDIUM)]
    if remaining:
        reasons = "; ".join(issue.message for issue in remaining[:5])
        return STATUS_NEEDS_REVIEW, _risk_from(local_issues), (
            "Bản viết lại vẫn còn vấn đề cần người duyệt: " + reasons), meaning_ok

    if not meaning_ok:
        return (STATUS_NEEDS_REVIEW, _risk_from(local_issues),
                "Bản viết lại làm mất thông tin quan trọng của bản gốc, cần kiểm tra thủ công.", False)

    # The provider may notice a problem the local rules do not cover. Its
    # severity is not trusted blindly, but an alert is never ignored.
    alerts = [issue for issue in (provider_issues or [])
              if issue.severity in (SEVERITY_HIGH, SEVERITY_MEDIUM)]
    if alerts:
        reasons = "; ".join(issue.message for issue in alerts[:3])
        risk = RISK_HIGH if any(issue.severity == SEVERITY_HIGH for issue in alerts) \
            else _risk_from(local_issues)
        return STATUS_NEEDS_REVIEW, risk, ("AI nhận diện vấn đề cần người duyệt: " + reasons), meaning_ok

    if local_issues:
        return (STATUS_APPROVED, _risk_from(local_issues),
                "Đã tối ưu; vẫn còn ghi chú về spam hoặc hình thức, không có lỗi chính sách.", meaning_ok)

    return STATUS_APPROVED, RISK_LOW, "Nội dung đạt yêu cầu về chính sách và không còn dấu hiệu spam.", meaning_ok


def optimize_caption(caption: str, settings: CaptionSettings, model_override: str = "") -> CaptionOptimization:
    """Run the four steps and return the decided result."""
    text = (caption or "").strip()
    if not text:
        return CaptionOptimization(status=STATUS_NEEDS_REVIEW, original_caption=caption or "",
                                   risk_level=RISK_UNKNOWN, reason="Caption rỗng.")

    # STEP 1 — analyze the original.
    analysis = analyze_caption(text)

    # STEP 2 — rewrite. The provider is used when it is configured, otherwise
    # the local engine performs the same meaning preserving cleanup.
    config = load_provider_config(model_override or settings.model)
    provider_failed = False
    optimized = ""
    changes: list[CaptionChange] = []
    provider_issues: list[CaptionIssue] = []
    engine = "local"
    model = ""
    if config.configured:
        try:
            answer = call_provider(config, text, analysis, settings, settings.target)
            optimized = answer.optimized_caption.strip()
            changes = answer.changes
            provider_issues = answer.issues
            model = config.model
            engine = "openai"
        except ProviderError as error:
            provider_failed = True
            LOG.warning("AI caption provider unavailable: %s", type(error).__name__)
    if not optimized:
        optimized, local_changes = rewrite_locally(
            text, add_hashtags=settings.add_hashtags, max_hashtags=settings.max_hashtags,
            target=settings.target, tone=settings.tone)
        changes = local_changes
        model = model or "local-rules"

    # STEP 3 — validate the rewritten caption with the local validator.
    recheck = analyze_caption(optimized)
    local_issues = recheck.issues
    issues: list[CaptionIssue] = []
    for issue in provider_issues + local_issues:
        if issue.category not in {item.category for item in issues}:
            issues.append(issue)
    meaning_ok = meaning_preserved(text, optimized) if settings.preserve_meaning else True

    # STEP 4 — publish decision, taken by this backend only.
    status, risk, reason, meaning_ok = decide(analysis, optimized, local_issues,
                                               meaning_ok, provider_failed, provider_issues)
    if not changes and optimized.strip() != text.strip():
        changes = [CaptionChange(kind="wording", summary="Bản caption được viết lại.",
                                 before=text[:2000], after=optimized[:2000])]
    return CaptionOptimization(status=status, original_caption=text, optimized_caption=optimized,
                               risk_level=risk, meaning_preserved=meaning_ok, issues=issues,
                               changes=changes, reason=reason, ai_model=model,
                               provider="openai-compatible" if config.configured else "local",
                               engine=engine, topic=analysis.topic, intent=analysis.intent,
                               key_facts=analysis.key_facts)
