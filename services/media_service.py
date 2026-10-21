"""Nghiệp vụ Album: đọc ảnh, phân tích và ghi trạng thái.

Module này không phụ thuộc framework HTTP. Những thứ nó cần từ hạ tầng
(database, đồng hồ, thư mục ảnh, cấu hình AI) được truyền vào qua
:class:`MediaContext`, nhờ vậy phần đây có thể kiểm thử độc lập.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from services import media_cloud, media_status
from services.image_analysis import analyze_image
from services.media_store import StorageError, safe_join

LOG = logging.getLogger("facebook-pipeline.media-service")


@dataclass(frozen=True)
class MediaContext:
    """Những phần hạ tầng mà nghiệp vụ Album cần."""

    db: Callable
    iso: Callable
    data_root: Path
    provider_config: Callable


# --- đọc ảnh -----------------------------------------------------------------

def album_scope_label(conn, row) -> str:
    """Thư mục của một ảnh: chủ sở hữu, hoặc kho chung."""
    owner = row["owner_user_id"] if "owner_user_id" in row.keys() else None
    if not owner:
        return media_cloud.SHARED_SCOPE
    user = conn.execute("SELECT username FROM users WHERE id=?", (owner,)).fetchone()
    return user["username"] if user else media_cloud.SHARED_SCOPE


def album_remote_key(conn, row, key: str = "") -> str:
    """Khoá của một ảnh trong bucket của Album."""
    pipeline = conn.execute("SELECT name FROM pipelines WHERE id=?",
                             (row["pipeline_id"],)).fetchone()
    name = pipeline["name"] if pipeline else str(row["pipeline_id"])
    return media_cloud.remote_key(name, album_scope_label(conn, row), key or row["storage_key"])


def album_bytes(conn, row, base: Path, key: str = "") -> bytes | None:
    """Đọc một ảnh của Album, từ cloud hoặc từ ổ đĩa.

    Ảnh đã tải lên trước khi bật cloud vẫn đọc được từ ổ đĩa, nên không có
    ảnh nào biến mất trong lúc bucket đang được dựng.
    """
    storage_key = key or row["storage_key"]
    if media_cloud.enabled():
        payload = media_cloud.get_bytes(album_remote_key(conn, row, storage_key))
        if payload is not None:
            return payload
    try:
        path = safe_join(base, storage_key)
    except StorageError:
        return None
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


# --- trạng thái ---------------------------------------------------------------

def media_payload(row) -> dict:
    """Dạng API của một ảnh trong Album."""
    item = dict(row)
    try:
        item["tags"] = json.loads(item.get("tags_json") or "[]")
    except (TypeError, ValueError):
        item["tags"] = []
    try:
        item["safety"] = json.loads(item.get("safety_json") or "{}")
    except (TypeError, ValueError):
        item["safety"] = {}
    item["usable"] = bool(item.get("usable"))
    item["locked"] = bool(item.get("locked"))
    item["saved"] = bool(item.get("saved"))
    state = media_status.state_of(item.get("analysis_status"), item.get("safety_status"))
    item["state"] = state
    item["state_label"] = media_status.label_of(state)
    item["state_tone"] = media_status.tone_of(state)
    item["reason"] = media_status.reason_of(item)
    item["can_retry"] = media_status.can_retry(state)
    item["can_be_usable"] = media_status.can_be_usable(state)
    return item


def known_album_media(conn, pipeline_id: int, exclude_id: int | None = None) -> list[dict]:
    """Các ảnh đã có trong Album, để tìm ảnh trùng."""
    rows = conn.execute("""SELECT id, storage_key, sha256, signature FROM media_assets
                            WHERE pipeline_id=?""", (pipeline_id,)).fetchall()
    return [dict(row) for row in rows if exclude_id is None or row["id"] != exclude_id]


def enqueue(conn, media_ids: list[int], stamp: str) -> list[int]:
    """Đưa ảnh vào hàng đợi kiểm tra."""
    queued = []
    for media_id in media_ids:
        conn.execute("""UPDATE media_assets SET analysis_status=?, safety_status=?,
                        updated_at=? WHERE id=?""",
                     (media_status.ANALYSIS_QUEUED, media_status.SAFETY_UNKNOWN, stamp, media_id))
        queued.append(media_id)
    return queued


def apply_result(ctx: MediaContext, record: dict, outcome: dict) -> None:
    """Ghi kết quả kiểm tra vào ảnh.

    Ảnh kiểm tra đạt tự động dùng được, không cần ai duyệt. Ảnh kiểm tra
    hỏng không được coi là an toàn, cũng không được coi là vi phạm, và được
    ghi lại là lỗi để CTV kiểm tra lại được.
    """
    status = outcome.get("status", media_status.SAFETY_UNKNOWN)
    usable = 1 if status == media_status.SAFETY_PASS else 0
    analysis = media_status.ANALYSIS_DONE
    if status not in (media_status.SAFETY_PASS, media_status.SAFETY_REVIEW,
                      media_status.SAFETY_BLOCKED):
        analysis = media_status.ANALYSIS_ERROR
    tags = outcome.get("tags") or []
    stamp = ctx.iso()
    with ctx.db() as conn:
        conn.execute("""UPDATE media_assets SET description=?, tags_json=?, safety_status=?,
                        safety_json=?, analysis_status=?, usable=?, updated_at=? WHERE id=?""",
                     (outcome.get("description", ""), json.dumps(tags, ensure_ascii=False),
                      status,
                      json.dumps({key: value for key, value in outcome.items() if key != "tags"},
                                 ensure_ascii=False),
                      analysis, usable, stamp, record["id"]))
        conn.execute("DELETE FROM media_tags WHERE media_id=?", (record["id"],))
        for tag in tags:
            conn.execute("INSERT OR IGNORE INTO media_tags(media_id,tag,created_at) VALUES(?,?,?)",
                         (record["id"], str(tag)[:40], stamp))
        conn.execute("""INSERT INTO media_analysis_results
            (media_id,pipeline_id,status,description,tags_json,risk,findings_json,model_version,
             checked_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     (record["id"], record["pipeline_id"], status,
                      outcome.get("description", ""), json.dumps(tags, ensure_ascii=False),
                      outcome.get("risk", "none"),
                      json.dumps([outcome.get("note", ""), outcome.get("category", "")],
                                 ensure_ascii=False),
                      outcome.get("model_version", ""), stamp, stamp))


def mark_failed(ctx: MediaContext, media_id: int, reason: str) -> None:
    """Ghi nhận một lần kiểm tra hỏng."""
    stamp = ctx.iso()
    with ctx.db() as conn:
        conn.execute("""UPDATE media_assets SET analysis_status=?, safety_status=?, safety_json=?,
                        usable=0, updated_at=? WHERE id=?""",
                     (media_status.ANALYSIS_ERROR, media_status.SAFETY_UNKNOWN,
                      json.dumps({"status": media_status.SAFETY_UNKNOWN, "error": reason,
                                  "note": "", "category": "none", "checked": False},
                                 ensure_ascii=False),
                      stamp, media_id))


def run_analysis(ctx: MediaContext, media_id: int) -> None:
    """Kiểm tra một ảnh trong nền."""
    with ctx.db() as conn:
        row = conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
        if row is None:
            return
        record = dict(row)
        known = known_album_media(conn, record["pipeline_id"], record["id"])
        conn.execute("""UPDATE media_assets SET analysis_status=?, updated_at=? WHERE id=?""",
                     (media_status.ANALYSIS_RUNNING, ctx.iso(), media_id))
    scratch = None
    try:
        if media_cloud.enabled():
            with ctx.db() as conn:
                payload = album_bytes(conn, record, ctx.data_root)
            if payload is None:
                raise StorageError("Ảnh không còn tồn tại trên R2")
            scratch = Path(ctx.data_root) / "album" / f"analysis-{int(record['id'])}.tmp"
            scratch.parent.mkdir(parents=True, exist_ok=True)
            scratch.write_bytes(payload)
            path = scratch
        else:
            path = safe_join(ctx.data_root, record["storage_key"])
        outcome = analyze_image(path, ctx.provider_config(), known)
    except Exception as error:  # noqa: BLE001 - một ảnh hỏng không dừng cả worker
        LOG.info("Không kiểm tra được ảnh %s: %s", media_id, type(error).__name__)
        mark_failed(ctx, media_id, type(error).__name__)
        return
    finally:
        if scratch is not None:
            try:
                scratch.unlink(missing_ok=True)
            except OSError:
                pass
    apply_result(ctx, record, outcome)
