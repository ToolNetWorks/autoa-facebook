"""Chống đăng trùng lặp ở nhiều cấp.

Bộ chống trùng nhìn vào bốn dấu vết của nội dung:

* **Ảnh giống hệt**: SHA256 của tệp.
* **Ảnh gần giống**: perceptual signature, so sánh khoảng cách Hamming.
* **Caption**: chuẩn hóa rồi băm, nên chỉ sửa vài từ hay đổi dấu câu cũng
  không lách được.
* **Dấu vết nội dung**: kết hợp ảnh và caption, dùng cho mỗi cặp
  (group, tài khoản).

Ngoài ra bài đã bị từ chối hoặc bị gỡ không được đăng lại, và bài đã bấm đăng
nhưng chưa xác minh không được tạo lại.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

NEAR_DUPLICATE_DISTANCE = 6
REPEAT_WINDOW_DAYS = 30

KIND_MEDIA_EXACT = "media_exact"
KIND_MEDIA_NEAR = "media_near"
KIND_CAPTION = "caption"
KIND_CONTENT = "content"

_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+", re.UNICODE)


def normalize_caption(text: str) -> str:
    """Bỏ dấu câu, hạ chữ thường và gộp khoảng trắng.

    "Bán sỉ  đẹp!" và "bán sỉ đẹp" cho cùng một dấu vết.
    """
    if not text:
        return ""
    plain = unicodedata.normalize("NFC", str(text)).casefold()
    plain = _PUNCTUATION.sub(" ", plain)
    plain = _SPACES.sub(" ", plain).strip()
    return plain


def caption_fingerprint(text: str) -> str:
    """Dấu vết của một caption đã chuẩn hóa."""
    normalized = normalize_caption(text)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def media_fingerprint(sha256: str) -> str:
    """Dấu vết của một tệp ảnh."""
    if not sha256:
        return ""
    return f"sha256:{sha256}"


def near_fingerprint(signature: str) -> str:
    """Dấu vết gần giống của một ảnh."""
    if not signature:
        return ""
    return f"phash:{signature}"


def content_fingerprint(media_fingerprint_value: str, caption: str) -> str:
    """Dấu vất của một cặp ảnh và caption."""
    if not media_fingerprint_value and not caption:
        return ""
    raw = f"{media_fingerprint_value}|{caption_fingerprint(caption)}"
    return "content:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hamming(first: str, second: str) -> int:
    """Khoảng cách Hamming giữa hai chữ ký ảnh."""
    if not first or not second or len(first) != len(second):
        return 99
    return sum(1 for a, b in zip(first, second) if a != b)


def is_near_duplicate(first: str, second: str, threshold: int = NEAR_DUPLICATE_DISTANCE) -> bool:
    """True when two images are almost the same."""
    if not first or not second:
        return False
    return hamming(first, second) <= threshold


def recent_fingerprints(conn, pipeline_id: int, kinds: tuple[str, ...]) -> list[dict]:
    """Các dấu vết đã ghi trong gần đây của Pipeline."""
    placeholders = ",".join("?" for _ in kinds)
    rows = conn.execute(f"""SELECT * FROM content_fingerprints
                             WHERE pipeline_id=? AND kind IN ({placeholders})
                             ORDER BY id DESC LIMIT 500""",
                         (int(pipeline_id), *kinds)).fetchall()
    return [dict(row) for row in rows]


def record(conn, pipeline_id: int, kind: str, fingerprint: str, media_id: int | None = None,
           job_id: int | None = None, group_id: int | None = None) -> None:
    """Ghi một dấu vết của nội dung đã dùng."""
    if not fingerprint:
        return
    conn.execute("""INSERT OR IGNORE INTO content_fingerprints
        (pipeline_id,kind,fingerprint,media_id,job_id,group_id,created_at)
        VALUES(?,?,?,?,?,?,datetime('now'))""",
                 (int(pipeline_id), kind, fingerprint, media_id, job_id, group_id))


def check(conn, pipeline_id: int, media: dict, caption: str, group_id: int | None = None,
          account_id: int | None = None) -> dict:
    """Kiểm tra một nội dung có bị trùng không.

    Trả về ``{"blocked": bool, "reason": str, "kind": str}``. Khi không có
    trùng, ``reason`` rỗng.
    """
    if not media and not caption:
        return {"blocked": True, "reason": "Nội dung rỗng", "kind": "empty"}

    exact = media_fingerprint(media.get("sha256") if media else "")
    near = near_fingerprint(media.get("signature") if media else "")
    caption_hash = caption_fingerprint(caption)
    content = content_fingerprint(exact, caption)

    known = recent_fingerprints(conn, pipeline_id, (KIND_MEDIA_EXACT, KIND_MEDIA_NEAR, KIND_CAPTION,
                                                      KIND_CONTENT))
    for row in known:
        if exact and row["kind"] == KIND_MEDIA_EXACT and row["fingerprint"] == exact:
            return {"blocked": True, "kind": KIND_MEDIA_EXACT,
                    "reason": "Ảnh này đã được dùng trong Pipeline"}
        if content and row["kind"] == KIND_CONTENT and row["fingerprint"] == content:
            return {"blocked": True, "kind": KIND_CONTENT,
                    "reason": "Ảnh và caption này đã được đăng trong Pipeline"}
        if caption_hash and row["kind"] == KIND_CAPTION and row["fingerprint"] == caption_hash:
            # Cùng caption được dùng lại ở group khác là chuyện bình thường;
            # cùng group thì không được lặp lại.
            if group_id is not None and row.get("group_id") not in (None, group_id):
                continue
            return {"blocked": True, "kind": KIND_CAPTION,
                    "reason": "Caption này đã được dùng cho group này"}
    if near:
        for row in known:
            if row["kind"] != KIND_MEDIA_NEAR:
                continue
            other = row["fingerprint"].split(":", 1)[-1]
            if is_near_duplicate(near, other):
                return {"blocked": True, "kind": KIND_MEDIA_NEAR,
                        "reason": "Ảnh gần giống ảnh đã đăng trong Pipeline"}

    # Nhóm đã nhận nội dung này rồi thì không đăng lần nữa.
    if group_id and (exact or caption_hash):
        for row in known:
            if row["kind"] != KIND_CONTENT:
                continue
            if content and row["fingerprint"] == content:
                return {"blocked": True, "kind": KIND_CONTENT,
                        "reason": "Group này đã nhận nội dung tương tự"}
    return {"blocked": False, "reason": "", "kind": ""}


def was_refused(conn, pipeline_id: int, media_id: int | None, caption: str) -> bool:
    """True when this content was refused or removed before."""
    if not media_id:
        return False
    row = conn.execute("""SELECT COUNT(*) FROM jobs j JOIN posts p ON p.id=j.post_id
                           WHERE j.pipeline_id=? AND j.post_id IN
                             (SELECT id FROM posts WHERE owner_user_id IS NOT NULL AND id=? )
                           AND j.state IN ('failed','needs_review')""",
                        (int(pipeline_id), int(media_id))).fetchone()
    return int(row[0]) > 0


def has_unverified_job(conn, pipeline_id: int, media_id: int | None, group_id: int | None) -> bool:
    """True when a job of this content was sent but not verified yet."""
    if not media_id:
        return False
    row = conn.execute("""SELECT COUNT(*) FROM auto_post_jobs
                           WHERE pipeline_id=? AND media_id=? AND status='submitted_unverified'""",
                        (int(pipeline_id), int(media_id))).fetchone()
    return int(row[0]) > 0
