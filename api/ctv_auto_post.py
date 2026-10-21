"""API của cấu hình tự động đăng group riêng của từng CTV.

Module này không nhập trực tiếp từ ``app`` để tránh vòng lặp import: các
hàm bảo vệ (đăng nhập, phân quyền, giới hạn theo Pipeline) được truyền vào
qua :class:`Deps` khi ``app`` dựng router.

Nguyên tắc bảo mật:

* CTV luôn là người đang đăng nhập. ``ctv_user_id`` trong URL hoặc body **không
  bao giờ** được tin, nên CTV không thể sửa cấu hình của người khác.
* CTV chỉ chọn được tài nguyên của Pipeline mà tài khoản đó được phép dùng.
* Cấu hình của CTV khác được xem và quản lý bởi quản trị qua API riêng.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from scheduler import ctv_allocator
from services import ctv_auto_post

PERMISSION_SELF = "auto_post.self_configure"


@dataclass(frozen=True)
class Deps:
    """Các hàm của ``app`` mà router này dùng."""

    db: Callable
    require_principal: Callable
    require_admin: Callable
    require_permission: Callable
    scope_pipeline: Callable
    scoped_entry: Callable
    entry: Callable
    audit: Callable
    load_ai_config: Callable


class CtvSettingsPayload(BaseModel):
    """Cấu hình tự động đăng mà một CTV được phép gửi lên."""

    enabled: bool | None = None
    account_scope: str | None = None
    selected_account_ids: list[int] | None = None
    selected_group_ids: list[int] | None = None
    topic: str | None = None
    caption_template: str | None = None
    max_posts_per_account_daily: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    timezone: str | None = None
    schedule_rules: dict | None = None
    ai_caption_enabled: bool | None = None
    approval_required: bool | None = None
    anti_duplicate: bool | None = None
    apply_to_new_accounts: bool | None = None


class CtvAdminPayload(CtvSettingsPayload):
    """Cấu hình mà quản trị gửi cho một CTV cụ thể."""


def _validate_scope(deps: Deps, conn, pipeline_id: int, principal, payload: CtvSettingsPayload) -> None:
    """Chỉ cho phép chọn tài nguyên thuộc Pipeline và thuộc quyền của tài khoản."""
    for account_id in payload.selected_account_ids or []:
        account = deps.scoped_entry(conn, "accounts", account_id, principal)
        if account["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")
    for group_id in payload.selected_group_ids or []:
        group = deps.scoped_entry(conn, "groups", group_id, principal)
        if group["pipeline_id"] != pipeline_id:
            raise HTTPException(404, "Not found")


def _clean_payload(payload: CtvSettingsPayload) -> dict:
    data = payload.model_dump()
    return {key: value for key, value in data.items() if value is not None}


def build_router(deps: Deps) -> APIRouter:
    """Dựng router của cấu hình tự động đăng riêng theo CTV."""
    router = APIRouter()

    # --- của chính CTV --------------------------------------------------------

    @router.get("/api/pipelines/{pipeline_id}/auto-post/my-config")
    def get_my_config(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Cấu hình riêng của CTV đang đăng nhập.

        Trả về ``configured: false`` kèm giá trị mặc định khi CTV chưa lưu lần
        nào, để giao diện vẫn hiện đầy đủ form ở lần đầu.
        """
        deps.require_permission(principal, PERMISSION_SELF)
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            payload = ctv_auto_post.payload_of(conn, pipeline_id, principal.user_id)
            owner = ctv_auto_post.owner_of(conn, pipeline_id, principal.user_id)
            policy = ctv_auto_post.pipeline_policy(conn, pipeline_id)
        payload["owner"] = owner
        payload["policy"] = policy
        return {"ok": True, **payload}

    @router.put("/api/pipelines/{pipeline_id}/auto-post/my-config")
    def put_my_config(pipeline_id: int, payload: CtvSettingsPayload,
                      principal=Depends(deps.require_principal)):
        """Tạo hoặc cập nhật cấu hình của CTV đang đăng nhập."""
        deps.require_permission(principal, PERMISSION_SELF)
        deps.scope_pipeline(principal, pipeline_id)
        if payload.account_scope not in (None, ctv_auto_post.SCOPE_ALL,
                                         ctv_auto_post.SCOPE_SELECTED):
            raise HTTPException(422, "Phạm vi áp dụng không hợp lệ")
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            _validate_scope(deps, conn, pipeline_id, principal, payload)
            settings = ctv_auto_post.save_settings(conn, pipeline_id, principal.user_id,
                                                   _clean_payload(payload))
        deps.audit("auto_post", principal.user_id,
                   "Lưu cấu hình tự động đăng group của CTV", pipeline_id)
        return {"ok": True, "configured": True, "settings": settings}

    @router.post("/api/pipelines/{pipeline_id}/auto-post/my-config/activate")
    def activate_my_config(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Bật hệ thống đăng bài của CTV đang đăng nhập."""
        deps.require_permission(principal, PERMISSION_SELF)
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            settings = ctv_auto_post.set_enabled(conn, pipeline_id, principal.user_id, True)
        deps.audit("auto_post", principal.user_id, "Bật tự động đăng group của CTV", pipeline_id)
        return {"ok": True, "configured": True, "settings": settings}

    @router.post("/api/pipelines/{pipeline_id}/auto-post/my-config/pause")
    def pause_my_config(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Tạm dừng hệ thống đăng bài của CTV đang đăng nhập."""
        deps.require_permission(principal, PERMISSION_SELF)
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            settings = ctv_auto_post.set_enabled(conn, pipeline_id, principal.user_id, False)
        deps.audit("auto_post", principal.user_id, "Tạm dừng tự động đăng group của CTV", pipeline_id)
        return {"ok": True, "configured": True, "settings": settings}

    @router.get("/api/pipelines/{pipeline_id}/auto-post/my-jobs")
    def get_my_jobs(pipeline_id: int, limit: int = 50,
                    principal=Depends(deps.require_principal)):
        """Các job do CTV đang đăng nhập tạo ra."""
        deps.require_permission(principal, PERMISSION_SELF)
        deps.scope_pipeline(principal, pipeline_id)
        limit = max(1, min(200, int(limit)))
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            jobs = ctv_auto_post.jobs_of(conn, pipeline_id, principal.user_id, limit)
        return {"ok": True, "jobs": jobs}

    @router.get("/api/pipelines/{pipeline_id}/auto-post/my-config/preview")
    def preview_my_config(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Xem trước lịch đăng của CTV đang đăng nhập."""
        deps.require_permission(principal, PERMISSION_SELF)
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            return ctv_allocator.preview(conn, pipeline_id, principal.user_id)

    # --- của quản trị --------------------------------------------------------

    @router.get("/api/pipelines/{pipeline_id}/auto-post/ctv-configs")
    def list_ctv_configs(pipeline_id: int, principal=Depends(deps.require_admin)):
        """Danh sách cấu hình của mọi CTV trong Pipeline."""
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            configs = ctv_auto_post.list_configs(conn, pipeline_id)
        return {"ok": True, "configs": configs}

    @router.get("/api/pipelines/{pipeline_id}/auto-post/ctv-configs/{ctv_user_id}")
    def get_ctv_config(pipeline_id: int, ctv_user_id: int, principal=Depends(deps.require_admin)):
        """Cấu hình của một CTV cụ thể."""
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            owner = ctv_auto_post.owner_of(conn, pipeline_id, ctv_user_id)
            if owner is None:
                raise HTTPException(404, "Not found")
            payload = ctv_auto_post.payload_of(conn, pipeline_id, ctv_user_id)
        payload["owner"] = owner
        return {"ok": True, **payload}

    @router.put("/api/pipelines/{pipeline_id}/auto-post/ctv-configs/{ctv_user_id}")
    def put_ctv_config(pipeline_id: int, ctv_user_id: int, payload: CtvAdminPayload,
                       principal=Depends(deps.require_admin)):
        """Quản trị sửa cấu hình của một CTV cụ thể."""
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            owner = ctv_auto_post.owner_of(conn, pipeline_id, ctv_user_id)
            if owner is None:
                raise HTTPException(404, "Not found")
            for account_id in payload.selected_account_ids or []:
                account = deps.entry(conn, "accounts", account_id)
                if account["pipeline_id"] != pipeline_id:
                    raise HTTPException(404, "Not found")
            for group_id in payload.selected_group_ids or []:
                group = deps.entry(conn, "groups", group_id)
                if group["pipeline_id"] != pipeline_id:
                    raise HTTPException(404, "Not found")
            settings = ctv_auto_post.save_settings(conn, pipeline_id, ctv_user_id,
                                                   _clean_payload(payload))
        deps.audit("auto_post", ctv_user_id,
                   "Quản trị cập nhật cấu hình tự động đăng của CTV", pipeline_id)
        return {"ok": True, "configured": True, "settings": settings}

    @router.post("/api/pipelines/{pipeline_id}/auto-post/ctv-configs/{ctv_user_id}/activate")
    def activate_ctv_config(pipeline_id: int, ctv_user_id: int,
                            principal=Depends(deps.require_admin)):
        """Quản trị bật lịch của một CTV cụ thể."""
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            settings = ctv_auto_post.set_enabled(conn, pipeline_id, ctv_user_id, True)
        deps.audit("auto_post", ctv_user_id, "Quản trị bật tự động đăng của CTV", pipeline_id)
        return {"ok": True, "configured": True, "settings": settings}

    @router.post("/api/pipelines/{pipeline_id}/auto-post/ctv-configs/{ctv_user_id}/pause")
    def pause_ctv_config(pipeline_id: int, ctv_user_id: int,
                         principal=Depends(deps.require_admin)):
        """Quản trị tạm dừng lịch của một CTV cụ thể."""
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            settings = ctv_auto_post.set_enabled(conn, pipeline_id, ctv_user_id, False)
        deps.audit("auto_post", ctv_user_id, "Quản trị tạm dừng tự động đăng của CTV", pipeline_id)
        return {"ok": True, "configured": True, "settings": settings}

    return router
