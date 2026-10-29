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


#: Trạng thái job tự động không giữ chỗ nội dung: job chưa từng được gửi đi.
RELEASED_JOB_STATUSES = ("failed", "cancelled")

#: Trạng thái job tự động vẫn đang chờ đăng.
ACTIVE_JOB_STATUSES = ("pending", "approved", "waiting_login", "needs_action", "queued")


def _group_of_row(conn, row: dict) -> int | None:
    """Group của một dấu vết; dấu vết cũ chưa ghi group thì tra theo job."""
    if row.get("group_id") is not None:
        return int(row["group_id"])
    if row.get("job_id"):
        job = conn.execute("SELECT group_id FROM auto_post_jobs WHERE id=?",
                           (row["job_id"],)).fetchone()
        if job is not None and job["group_id"] is not None:
            return int(job["group_id"])
    return None


def _group_history(conn, pipeline_id: int, group_id: int) -> list[dict]:
    """Ảnh và caption đã dùng cho đúng group này.

    Đây là nguồn chính cho việc chống trùng trong group: bảng dấu vết chỉ giữ
    một dòng cho mỗi ảnh trên toàn Pipeline, nên không đủ để biết ảnh đã vào
    những group nào.
    """
    rows = conn.execute(f"""SELECT j.caption, m.sha256, m.signature
                             FROM auto_post_jobs j LEFT JOIN media_assets m ON m.id=j.media_id
                            WHERE j.pipeline_id=? AND j.group_id=?
                              AND j.status NOT IN ({",".join("?" for _ in RELEASED_JOB_STATUSES)})
                              AND datetime(j.created_at) >= datetime('now', ?)""",
                        (int(pipeline_id), int(group_id), *RELEASED_JOB_STATUSES,
                         f"-{REPEAT_WINDOW_DAYS} days")).fetchall()
    return [dict(row) for row in rows]


def check(conn, pipeline_id: int, media: dict, caption: str, group_id: int | None = None,
          account_id: int | None = None) -> dict:
    """Kiểm tra một nội dung có bị trùng không.

    Khi có ``group_id``, chỉ chặn trùng trong **đúng group đó**: một ảnh hay
    một caption được phép dùng cho các group khác nhau, nhưng không được lặp
    lại trong cùng một group. Không có ``group_id`` thì so trên toàn Pipeline.

    Trả về ``{"blocked": bool, "reason": str, "kind": str}``. Khi không có
    trùng, ``reason`` rỗng.
    """
    if not media and not caption:
        return {"blocked": True, "reason": "Nội dung rỗng", "kind": "empty"}

    exact = media_fingerprint(media.get("sha256") if media else "")
    near = near_fingerprint(media.get("signature") if media else "")
    caption_hash = caption_fingerprint(caption)
    content = content_fingerprint(exact, caption)
    signature = (media.get("signature") if media else "") or ""

    if group_id is not None:
        for used in _group_history(conn, pipeline_id, group_id):
            if exact and media_fingerprint(used.get("sha256") or "") == exact:
                return {"blocked": True, "kind": KIND_MEDIA_EXACT,
                        "reason": "Ảnh này đã được dùng cho group này"}
            if signature and used.get("signature") and is_near_duplicate(signature, used["signature"]):
                return {"blocked": True, "kind": KIND_MEDIA_NEAR,
                        "reason": "Ảnh gần giống ảnh đã dùng cho group này"}
            if caption_hash and caption_fingerprint(used.get("caption") or "") == caption_hash:
                return {"blocked": True, "kind": KIND_CAPTION,
                        "reason": "Caption này đã được dùng cho group này"}

    known = recent_fingerprints(conn, pipeline_id, (KIND_MEDIA_EXACT, KIND_MEDIA_NEAR, KIND_CAPTION,
                                                      KIND_CONTENT))
    for row in known:
        if group_id is not None:
            row_grp = _group_of_row(conn, row)
            if row_grp is None or row_grp != int(group_id):
                continue
        if exact and row["kind"] == KIND_MEDIA_EXACT and row["fingerprint"] == exact:
            return {"blocked": True, "kind": KIND_MEDIA_EXACT,
                    "reason": "Ảnh này đã được dùng cho group này" if group_id is not None
                    else "Ảnh này đã được dùng trong Pipeline"}
        if content and row["kind"] == KIND_CONTENT and row["fingerprint"] == content:
            return {"blocked": True, "kind": KIND_CONTENT,
                    "reason": "Group này đã nhận nội dung tương tự" if group_id is not None
                    else "Ảnh và caption này đã được đăng trong Pipeline"}
        if caption_hash and row["kind"] == KIND_CAPTION and row["fingerprint"] == caption_hash:
            return {"blocked": True, "kind": KIND_CAPTION,
                    "reason": "Caption này đã được dùng cho group này" if group_id is not None
                    else "Caption này đã được dùng trong Pipeline"}
        if near and row["kind"] == KIND_MEDIA_NEAR:
            other = row["fingerprint"].split(":", 1)[-1]
            if is_near_duplicate(near.split(":", 1)[-1], other):
                return {"blocked": True, "kind": KIND_MEDIA_NEAR,
                        "reason": "Ảnh gần giống ảnh đã dùng cho group này" if group_id is not None
                        else "Ảnh gần giống ảnh đã đăng trong Pipeline"}
    return {"blocked": False, "reason": "", "kind": ""}


def active_job_group_ids(conn, pipeline_id: int) -> set[int]:
    """Các group đang có bài tự động chờ đăng, để không xếp thêm bài chồng lên."""
    rows = conn.execute(f"""SELECT DISTINCT group_id FROM auto_post_jobs
                            WHERE pipeline_id=? AND group_id IS NOT NULL
                              AND status IN ({",".join("?" for _ in ACTIVE_JOB_STATUSES)})""",
                        (int(pipeline_id), *ACTIVE_JOB_STATUSES)).fetchall()
    return {int(row["group_id"]) for row in rows}


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
