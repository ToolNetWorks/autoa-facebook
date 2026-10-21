"""Choosing the picture for an automatic post.

The choice is never random and it is explainable. Every candidate is scored
on the same grounds and the reasons are returned with the result, so a
collaborator can see why a picture was picked and why another one was not.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from services import media_status

STOP_WORDS = {
    "với", "của", "cho", "các", "những", "một", "được", "này", "đó", "là", "và",
    "trong", "ngoài", "rất", "đã", "sẽ", "không", "còn", "bạn", "mình", "chúng",
    "tôi", "nhé", "ạ", "cùng", "theo", "sau", "trước", "một", "thể", "loại",
}


def _words(text: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[\wÀ-ỹ]{2,}", text or "") if word.casefold() not in STOP_WORDS}


def _parse(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def score_candidate(media: dict, topic: str, now: datetime | None = None) -> tuple[float, list[str]]:
    """Score one picture. The reasons explain the score."""
    now = now or datetime.now(timezone.utc)
    score = 0.0
    reasons: list[str] = []

    if not media.get("usable"):
        return -1.0, ["Ảnh chưa được đánh dấu là có thể sử dụng"]
    if media.get("locked"):
        return -1.0, ["Ảnh đang bị tạm khóa"]

    state = media_status.state_of(media.get("analysis_status"), media.get("safety_status"))
    if state == media_status.BLOCKED:
        return -1.0, ["Ảnh bị chặn vì nội dung rủi ro"]
    if state == media_status.CHECK_FAILED:
        return -1.0, ["Ảnh chưa kiểm tra được nên không dùng để đăng"]
    if state in (media_status.UPLOADED, media_status.ANALYZING):
        return -1.0, ["Ảnh đang được kiểm tra"]
    if state == media_status.NEEDS_REVIEW:
        # CTV của Workspace đã xem xét và cho phép dùng, nên ảnh được dùng.
        score += 0.5
        reasons.append("đã xem xét và cho phép dùng")
    else:
        score += 3.0
        reasons.append("đã qua kiểm tra an toàn")

    topic_words = _words(topic)
    if topic_words:
        haystack = _words(" ".join([media.get("description") or ""] + (media.get("tags") or [])))
        overlap = topic_words & haystack
        if overlap:
            score += min(3.0, 0.9 * len(overlap))
            reasons.append("phù hợp chủ đề: " + ", ".join(sorted(overlap)[:4]))
        else:
            reasons.append("không rõ sự phù hợp với chủ đề")

    last_used = _parse(media.get("last_used_at"))
    if last_used is None:
        score += 2.0
        reasons.append("chưa được dùng lần nào")
    else:
        age_days = (now - last_used).days
        if age_days >= 7:
            score += min(2.0, age_days / 7.0)
            reasons.append(f"đã {age_days} ngày chưa dùng lại")
        else:
            reasons.append(f"mới dùng {age_days} ngày trước")

    usage = int(media.get("usage_count") or 0)
    if usage == 0:
        score += 1.5
    else:
        score -= min(3.0, usage * 0.75)
        reasons.append(f"đã dùng {usage} lần")

    if media.get("saved"):
        score += 0.5
        reasons.append("được lưu để dùng cho lịch sau")

    return score, reasons


def select_media(candidates: list[dict], topic: str, now: datetime | None = None) -> dict:
    """Pick the best picture and explain the choice."""
    scored = []
    for media in candidates:
        score, reasons = score_candidate(media, topic, now)
        scored.append({"media": media, "score": round(score, 3), "reasons": reasons})
    usable = [item for item in scored if item["score"] >= 0]
    if not usable:
        return {"media": None, "score": 0, "reasons": ["Không có ảnh nào đủ điều kiện dùng cho lịch đăng"],
                "candidates": scored}
    usable.sort(key=lambda item: (-item["score"], item["media"].get("id", 0)))
    best = usable[0]
    return {"media": best["media"], "score": best["score"], "reasons": best["reasons"],
            "candidates": scored}


def media_tags(media: dict) -> list[str]:
    tags = media.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = []
    return [str(tag) for tag in tags]
