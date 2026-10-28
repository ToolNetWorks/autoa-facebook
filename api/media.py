"""API của Album ảnh.

Module này không nhập trực tiếp từ ``app`` để tránh vòng lặp import: các hàm
bảo vệ và hạ tầng được truyền vào qua :class:`Deps` khi ``app`` dựng router.

Quy tắc nghiệp vụ:

* Album thuộc một Workspace. CTV quản lý được toàn bộ Album của Workspace
  mình, kể cả ảnh trong kho chung.
* Ảnh kiểm tra đạt thì tự động dùng được, không cần ai duyệt.
* Ảnh ``BLOCKED`` hoặc ``CHECK_FAILED`` không bao giờ được đánh dấu dùng được,
  kể cả quản trị.
* Một ảnh đang được job nào đó dùng thì không xoá được.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services import media_cloud, media_service, media_status, pipeline_scope
from services.media_store import StorageError, delete_media, safe_join, store_image


@dataclass(frozen=True)
class Deps:
    """Các hàm của ``app`` mà router này dùng."""

    db: Callable
    iso: Callable
    data_root: Callable
    require_principal: Callable
    require_permission: Callable
    entry: Callable
    audit: Callable
    load_ai_config: Callable


class MediaUpdate(BaseModel):
    """Một lần cập nhật ảnh trong Album."""

    tags: list[str] | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=600)
    usable: bool | None = None
    locked: bool | None = None
    saved: bool | None = None


def _workspace(deps: Deps, principal, requested: int | None) -> int:
    try:
        return pipeline_scope.workspace_for(principal, requested)
    except pipeline_scope.WorkspaceRequired:
        raise HTTPException(422, "Thiếu pipeline_id")
    except pipeline_scope.NoWorkspace:
        raise HTTPException(403, "Tài khoản chưa được gán Workspace")


def _album_row(deps: Deps, conn, pipeline_id: int, media_id: int):
    """Ảnh của Workspace này. Ảnh của Workspace khác không tồn tại với CTV."""
    row = conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
    if row is None or row["pipeline_id"] != pipeline_id:
        raise HTTPException(404, "Not found")
    return row


def build_router(deps: Deps) -> APIRouter:
    """Dựng router của Album ảnh."""
    router = APIRouter()

    def _context():
        return media_service.MediaContext(db=deps.db, iso=deps.iso,
                                          data_root=deps.data_root(),
                                          provider_config=deps.load_ai_config)

    # --- tải ảnh lên ---------------------------------------------------------

    @router.post("/api/pipelines/{pipeline_id}/media/upload", status_code=201)
    async def upload_album_media(pipeline_id: int, files: list[UploadFile] = File(...),
                                 principal=Depends(deps.require_principal)):
        """Tải ảnh lên Album của Workspace. Ảnh được kiểm tra ở nền."""
        deps.require_permission(principal, "media.upload")
        workspace = _workspace(deps, principal, pipeline_id)
        if not files:
            raise HTTPException(422, "Chưa có ảnh nào")
        if len(files) > 20:
            raise HTTPException(422, "Tối đa 20 ảnh mỗi lần")
        base = deps.data_root()
        stored = []
        for upload in files:
            data = await upload.read()
            try:
                item = store_image(base, workspace, upload.filename or "",
                                    upload.content_type or "", data)
            except StorageError as error:
                raise HTTPException(422, str(error)) from error
            with deps.db() as conn:
                deps.entry(conn, "pipelines", workspace)
                owner = None if principal.is_admin else principal.user_id
                scope = media_cloud.SHARED_SCOPE if not owner else ""
                if owner:
                    user = conn.execute("SELECT username FROM users WHERE id=?",
                                        (owner,)).fetchone()
                    scope = user["username"] if user else media_cloud.SHARED_SCOPE
                pipeline_name = conn.execute("SELECT name FROM pipelines WHERE id=?",
                                             (workspace,)).fetchone()["name"]
                if media_cloud.enabled():
                    media_cloud.ensure_bucket()
                    media_cloud.ensure_folder(media_cloud.folder(pipeline_name))
                    media_cloud.put_bytes(
                        media_cloud.remote_key(pipeline_name, scope, item.storage_key),
                        data, item.mime_type)
                    if item.thumbnail_key:
                        try:
                            thumb = safe_join(base, item.thumbnail_key).read_bytes()
                        except (StorageError, OSError):
                            thumb = b""
                        if thumb:
                            media_cloud.put_bytes(
                                media_cloud.remote_key(pipeline_name, scope, item.thumbnail_key),
                                thumb, "image/jpeg")
                    # Cloud giữ Album; bản ở đĩa chỉ là chỗ làm việc tạm.
                    for scratch in (item.storage_key, item.thumbnail_key):
                        if not scratch:
                            continue
                        try:
                            safe_join(base, scratch).unlink(missing_ok=True)
                        except (StorageError, OSError):
                            continue
                cursor = conn.execute("""INSERT INTO media_assets
                    (pipeline_id,uploader_id,storage_key,thumbnail_key,original_name,mime_type,
                     width,height,size_bytes,sha256,signature,analysis_status,created_at,updated_at,
                     owner_user_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (workspace, principal.user_id, item.storage_key, item.thumbnail_key,
                     item.original_name, item.mime_type, item.width, item.height, item.size_bytes,
                     item.sha256, "", media_status.ANALYSIS_QUEUED, deps.iso(), deps.iso(), owner))
                media_id = cursor.lastrowid
            deps.audit("media", media_id, f"Tải ảnh lên Album: {item.original_name}", workspace)
            stored.append({"id": media_id, "name": item.original_name,
                           "size": item.size_bytes, "width": item.width, "height": item.height})
        return {"ok": True, "media": stored, "count": len(stored)}

    # --- xem Album ------------------------------------------------------------

    @router.get("/api/pipelines/{pipeline_id}/media")
    def list_album_media(pipeline_id: int, tag: str = "", search: str = "",
                         only_usable: bool = False, principal=Depends(deps.require_principal)):
        """Album của Workspace, kèm trạng thái của từng ảnh."""
        deps.require_permission(principal, "media.view")
        workspace = _workspace(deps, principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", workspace)
            rows = conn.execute("""SELECT m.* FROM media_assets m WHERE m.pipeline_id=?
                                   ORDER BY m.id DESC""", (workspace,)).fetchall()
            result = [media_service.media_payload(row) for row in rows]
            if tag:
                result = [item for item in result
                          if tag.casefold() in [t.casefold() for t in item["tags"]]]
            if search:
                needle = search.casefold()
                result = [item for item in result
                          if needle in (item.get("description") or "").casefold()
                          or needle in (item.get("original_name") or "").casefold()
                          or any(needle in t.casefold() for t in item["tags"])]
            if only_usable:
                result = [item for item in result if item["usable"] and not item["locked"]]
            usage = {}
            for row in conn.execute("""SELECT media_id, COUNT(*) total FROM media_usage_history
                                        WHERE pipeline_id=? GROUP BY media_id""",
                                    (workspace,)).fetchall():
                usage[row["media_id"]] = row["total"]
        for item in result:
            item["usage_history_count"] = usage.get(item["id"], 0)
        return {"ok": True, "media": result, "counts": media_status.counts_of(result)}

    @router.get("/api/pipelines/{pipeline_id}/media/{media_id}")
    def get_album_media(pipeline_id: int, media_id: int, principal=Depends(deps.require_principal)):
        """Một ảnh cùng kết quả kiểm tra và lịch sử dùng."""
        deps.require_permission(principal, "media.view")
        workspace = _workspace(deps, principal, pipeline_id)
        with deps.db() as conn:
            row = _album_row(deps, conn, workspace, media_id)
            history = [dict(item) for item in conn.execute(
                """SELECT h.*, g.label group_label FROM media_usage_history h
                   LEFT JOIN groups g ON g.id=h.group_id WHERE h.media_id=? ORDER BY h.id DESC LIMIT 50""",
                (media_id,)).fetchall()]
        return {"ok": True, "media": media_service.media_payload(row), "usage_history": history}

    @router.get("/api/pipelines/{pipeline_id}/media/{media_id}/file")
    def get_album_media_file(pipeline_id: int, media_id: int,
                             principal=Depends(deps.require_principal)):
        """Trả về ảnh. Chỉ thành viên của Workspace này được đọc."""
        deps.require_permission(principal, "media.view")
        workspace = _workspace(deps, principal, pipeline_id)
        with deps.db() as conn:
            row = _album_row(deps, conn, workspace, media_id)
            payload = media_service.album_bytes(conn, row, deps.data_root())
        if payload is None:
            raise HTTPException(404, "Not found")
        return Response(content=payload, media_type=row["mime_type"] or "image/jpeg")

    @router.get("/api/pipelines/{pipeline_id}/media/{media_id}/thumbnail")
    def get_album_media_thumbnail(pipeline_id: int, media_id: int,
                                  principal=Depends(deps.require_principal)):
        """Trả về ảnh nhỏ để xem trước."""
        deps.require_permission(principal, "media.view")
        workspace = _workspace(deps, principal, pipeline_id)
        with deps.db() as conn:
            row = _album_row(deps, conn, workspace, media_id)
            key = row["thumbnail_key"] or row["storage_key"]
            payload = media_service.album_bytes(conn, row, deps.data_root(), key)
        if payload is None:
            raise HTTPException(404, "Not found")
        return Response(content=payload, media_type="image/jpeg")

    # --- quản lý ảnh ---------------------------------------------------------

    @router.patch("/api/pipelines/{pipeline_id}/media/{media_id}")
    def update_album_media(pipeline_id: int, media_id: int, data: MediaUpdate,
                           principal=Depends(deps.require_principal)):
        """Đổi thẻ, mô tả, trạng thái dùng, lưu hoặc khoá của một ảnh."""
        deps.require_permission(principal, "media.edit")
        workspace = _workspace(deps, principal, pipeline_id)
        with deps.db() as conn:
            row = _album_row(deps, conn, workspace, media_id)
            state = media_status.state_of(row["analysis_status"], row["safety_status"])
            if data.usable is not None and int(bool(data.usable)):
                if not media_status.can_be_usable(state):
                    raise HTTPException(409, "Ảnh này chưa có kết luận an toàn, không thể dùng để đăng")
            if data.tags is not None:
                conn.execute("UPDATE media_assets SET tags_json=?, updated_at=? WHERE id=?",
                             (json.dumps([str(tag)[:40] for tag in data.tags][:20]), deps.iso(),
                              media_id))
                conn.execute("DELETE FROM media_tags WHERE media_id=?", (media_id,))
                for tag in data.tags[:20]:
                    conn.execute("INSERT OR IGNORE INTO media_tags(media_id,tag,created_at) VALUES(?,?,?)",
                                 (media_id, str(tag)[:40], deps.iso()))
            if data.description is not None:
                conn.execute("UPDATE media_assets SET description=?, updated_at=? WHERE id=?",
                             (str(data.description)[:600], deps.iso(), media_id))
            if data.usable is not None:
                conn.execute("UPDATE media_assets SET usable=?, updated_at=? WHERE id=?",
                             (int(bool(data.usable)), deps.iso(), media_id))
            if data.locked is not None:
                conn.execute("UPDATE media_assets SET locked=?, updated_at=? WHERE id=?",
                             (int(bool(data.locked)), deps.iso(), media_id))
            if data.saved is not None:
                conn.execute("UPDATE media_assets SET saved=?, updated_at=? WHERE id=?",
                             (int(bool(data.saved)), deps.iso(), media_id))
            updated = conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
        deps.audit("media", media_id, "Cập nhật ảnh trong Album", workspace)
        return {"ok": True, "media": media_service.media_payload(updated)}

    @router.delete("/api/pipelines/{pipeline_id}/media/{media_id}")
    def delete_album_media(pipeline_id: int, media_id: int,
                           principal=Depends(deps.require_principal)):
        """Xoá một ảnh. Ảnh mà job đang chờ còn dùng thì giữ lại."""
        deps.require_permission(principal, "media.delete")
        workspace = _workspace(deps, principal, pipeline_id)
        with deps.db() as conn:
            row = _album_row(deps, conn, workspace, media_id)
            used = conn.execute("""SELECT COUNT(*) FROM auto_post_jobs WHERE media_id=? AND status IN
                                   ('pending','approved','queued')""", (media_id,)).fetchone()[0]
            from services.auto_posting_cycles.repository import available
            if available(conn):
                used += conn.execute('SELECT COUNT(*) FROM auto_cycle_targets WHERE media_id=? AND pipeline_id=?',(media_id,workspace)).fetchone()[0]
                used += conn.execute('SELECT COUNT(*) FROM workspace_album_media WHERE media_id=? AND pipeline_id=?',(media_id,workspace)).fetchone()[0]
            if used:
                raise HTTPException(409, "Ảnh đang được một job chưa hoàn tất sử dụng, chưa thể xóa")
            conn.execute("DELETE FROM media_assets WHERE id=?", (media_id,))
        if media_cloud.enabled():
            with deps.db() as conn:
                for key in (row["storage_key"], row["thumbnail_key"]):
                    if key:
                        media_cloud.delete_key(
                            media_service.album_remote_key(conn, row, key))
        try:
            delete_media(deps.data_root(), row["storage_key"], row["thumbnail_key"])
        except StorageError:
            pass
        deps.audit("media", media_id, "Xóa ảnh khỏi Album", workspace)
        return {"ok": True}

    # --- kiểm tra lại ---------------------------------------------------------

    @router.post("/api/pipelines/{pipeline_id}/media/analyze")
    def analyze_album_media(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Đưa các ảnh đang chờ hoặc vừa lỗi vào hàng đợi kiểm tra."""
        deps.require_permission(principal, "media.upload")
        workspace = _workspace(deps, principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", workspace)
            rows = conn.execute("""SELECT id FROM media_assets WHERE pipeline_id=?
                                   AND analysis_status IN (?,?)""",
                                (workspace, media_status.ANALYSIS_QUEUED,
                                 media_status.ANALYSIS_ERROR)).fetchall()
            queued = media_service.enqueue(conn, [row["id"] for row in rows], deps.iso())
        deps.audit("media", None, f"Xếp hàng phân tích {len(queued)} ảnh", workspace)
        return {"ok": True, "queued": queued, "count": len(queued)}

    @router.post("/api/pipelines/{pipeline_id}/media/{media_id}/analyze")
    def analyze_one_album_media(pipeline_id: int, media_id: int,
                                principal=Depends(deps.require_principal)):
        """Kiểm tra lại một ảnh."""
        deps.require_permission(principal, "media.upload")
        workspace = _workspace(deps, principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", workspace)
            _album_row(deps, conn, workspace, media_id)
            media_service.enqueue(conn, [media_id], deps.iso())
        deps.audit("media", media_id, "Yêu cầu kiểm tra lại ảnh", workspace)
        return {"ok": True, "queued": [media_id], "count": 1}

    return router
