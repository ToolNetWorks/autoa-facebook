"""API tạo bài group.

Một chiến dịch có thể chọn nhiều group. Backend kiểm tra danh sách group
**trước** khi ảnh được dàn và trước khi job nào được tạo, rồi chia thành
các job riêng được quản lý qua hàng đợi.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from services import group_campaign, group_post_validator, media_lifecycle


class GroupPostCampaign(BaseModel):
    """Một chiến dịch đăng group.

    Số group không bị giới hạn cứng ở đây: giới hạn thật lấy từ cấu hình và
    được kiểm tra trước khi ảnh được dàn, nên người dùng luôn biết vì sao
    yêu cầu bị từ chối.
    """

    group_ids: list[int] = Field(min_length=1)
    body: str = Field(min_length=1, max_length=20000)
    media: list[str] = Field(min_length=1, max_length=4)
    scheduled_at: str
    ai_record_id: int | None = None


@dataclass(frozen=True)
class Deps:
    """Các hàm của ``app`` mà router này dùng."""

    db: Callable
    require_principal: Callable
    require_permission: Callable
    scope_pipeline: Callable
    scoped_entry: Callable
    entry: Callable
    audit: Callable
    group_config: Callable
    resource_owner: Callable
    usable_caption: Callable
    parse_date: Callable
    media_file: Callable
    uploads_root: Callable
    data_root: Callable
    iso: Callable


def build_router(deps: Deps) -> APIRouter:
    """Dựng router của chiến dịch đăng group."""
    router = APIRouter()

    @router.get("/api/pipelines/{pipeline_id}/group-post/limits")
    def group_post_limits(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Giới hạn mà giao diện và backend dùng chung."""
        deps.require_permission(principal, "group_posts.create")
        deps.scope_pipeline(principal, pipeline_id)
        return {"ok": True, "max_groups": group_post_validator.max_groups_per_campaign(),
                "max_jobs": group_post_validator.MAX_JOBS_PER_CAMPAIGN}

    @router.post("/api/pipelines/{pipeline_id}/group-posts/preflight")
    def preflight_group_post(pipeline_id: int, data: GroupPostCampaign,
                             principal=Depends(deps.require_principal)):
        """Kiểm tra danh sách group **trước** khi người dùng tải ảnh lên.

        Nhờ vậy một danh sách group không hợp lệ không để lại ảnh tạm, và người
        dùng biết ngay group nào sẽ được lên lịch và group nào cần xử lý.
        """
        deps.require_permission(principal, "group_posts.create")
        deps.scope_pipeline(principal, pipeline_id)
        payload = data.model_dump()
        group_ids = [int(value) for value in (payload.get("group_ids") or [])]
        if not str(payload.get("body") or "").strip():
            raise HTTPException(422, "Cần có nội dung bài đăng")
        if not (payload.get("media") or []):
            raise HTTPException(422, "Cần có ít nhất một ảnh")
        scheduled_raw = str(payload.get("scheduled_at") or "")
        try:
            scheduled = deps.parse_date(scheduled_raw)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(422, "Thời điểm đăng không hợp lệ") from error
        with deps.db() as conn:
            def group_lookup(group_id: int):
                group = deps.scoped_entry(conn, "groups", group_id, principal)
                if group is None or int(group["pipeline_id"]) != int(pipeline_id):
                    return None
                group = dict(group)
                group["config"] = deps.group_config(conn, group_id, principal)
                return group

            def account_lookup(account_id: int):
                account = deps.scoped_entry(conn, "accounts", account_id, principal)
                if account is None or int(account["pipeline_id"]) != int(pipeline_id):
                    return None
                return account

            may_publish = principal.can("group_posts.publish")
            try:
                plan = group_post_validator.validate_selection(
                    conn, pipeline_id, group_ids, principal, group_lookup, account_lookup,
                    may_publish, scheduled=scheduled)
            except group_post_validator.ValidationError as error:
                raise HTTPException(422, error.full_message()) from error
        return {"ok": True, "selected": len(plan["selected"]), "limit": plan["limit"],
                "ready": plan["ready"], "skipped": plan["skipped"],
                "deferred": plan.get("deferred") or []}

    @router.post("/api/pipelines/{pipeline_id}/group-posts", status_code=201)
    def create_group_post(pipeline_id: int, data: GroupPostCampaign,
                          principal=Depends(deps.require_principal)):
        """Lên lịch một bài cho nhiều group, mỗi group một job riêng."""
        deps.require_permission(principal, "group_posts.create")
        deps.scope_pipeline(principal, pipeline_id)

        payload = data.model_dump()
        group_ids = [int(value) for value in (payload.get("group_ids") or [])]
        body = str(payload.get("body") or "").strip()
        media = [str(value) for value in (payload.get("media") or [])]
        scheduled_raw = str(payload.get("scheduled_at") or "")

        # 1. Kiểm tra danh sách group trước khi động vào ảnh hay tạo job.
        # 2. Kiểm tra nội dung và thời gian đăng.
        if not body:
            raise HTTPException(422, "Cần có nội dung bài đăng")
        if not media:
            raise HTTPException(422, "Cần có ít nhất một ảnh")
        try:
            scheduled = deps.parse_date(scheduled_raw)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(422, "Thời điểm đăng không hợp lệ") from error

        with deps.db() as conn:
            def group_lookup(group_id: int):
                group = deps.scoped_entry(conn, "groups", group_id, principal)
                if group is None or int(group["pipeline_id"]) != int(pipeline_id):
                    return None
                group = dict(group)
                group["config"] = deps.group_config(conn, group_id, principal)
                return group

            def account_lookup(account_id: int):
                account = deps.scoped_entry(conn, "accounts", account_id, principal)
                if account is None or int(account["pipeline_id"]) != int(pipeline_id):
                    return None
                return account

            may_publish = principal.can("group_posts.publish")
            try:
                plan = group_post_validator.validate_selection(
                    conn, pipeline_id, group_ids, principal, group_lookup, account_lookup,
                    may_publish)
            except group_post_validator.ValidationError as error:
                raise HTTPException(422, error.full_message()) from error

            # 3. Dàn ảnh một lần cho cả chiến dịch.
            staging = media_lifecycle.MediaStaging(deps.data_root(), deps.uploads_root(),
                                                  pipeline_id)
            try:
                media_paths = [staging.stage(
                    path, lambda name: deps.media_file(conn, name, principal)) for path in media]
                ai_caption, ai_record = deps.usable_caption(conn, pipeline_id,
                                                           payload.get("ai_record_id"))
                caption = ai_caption or body
                result = group_campaign.create_campaign(
                    conn, pipeline_id, group_ids, principal, caption, media_paths, scheduled,
                    ai_record_id=payload.get("ai_record_id"), ai_caption=ai_caption,
                    may_publish=may_publish, lookup_group=group_lookup,
                    lookup_account=account_lookup, resource_owner=deps.resource_owner)
            except Exception:
                staging.cleanup()
                raise
            staging.commit()

        deps.audit("post", result["post_id"],
                   f"Lên lịch cho {len(result['jobs'])} group, bỏ qua {len(result['skipped'])}",
                   pipeline_id)
        return {"ok": True, "post_id": result["post_id"], "jobs_created": len(result["jobs"]),
                "jobs": result["jobs"], "skipped": result["skipped"],
                "deferred": result.get("deferred") or [],
                "duplicates": result["duplicates"], "selected": len(result["selected"]),
                "limit": result["limit"], "published": result["published"]}

    return router
