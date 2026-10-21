"""API kiểm tra group của một Workspace.

Module này không nhập trực tiếp từ ``app`` để tránh vòng lặp import: các hàm
bảo vệ và hạ tầng được truyền vào qua :class:`Deps` khi ``app`` dựng router.

Kết quả kiểm tra luôn gắn với đúng một cặp Facebook–Group, và CTV của
Workspace tự kiểm tra được group của mình.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services import group_screening


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
    enqueue_screening: Callable
    screening_row: Callable
    group_payload: Callable


class ScreenRequest(BaseModel):
    """Yêu cầu kiểm tra một hoặc nhiều group."""

    group_ids: list[int] = []
    force: bool = False


def build_router(deps: Deps) -> APIRouter:
    """Dựng router của kiểm tra group."""
    router = APIRouter()

    @router.post("/api/pipelines/{pipeline_id}/groups/screen")
    def screen_groups(pipeline_id: int, data: ScreenRequest,
                      principal=Depends(deps.require_principal)):
        """Xếp hàng kiểm tra các group được chỉ định của Workspace này."""
        deps.require_permission(principal, "groups.manage")
        deps.scope_pipeline(principal, pipeline_id)
        queued, skipped = [], []
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            rows = conn.execute("""SELECT g.id group_id, g.account_id, g.manual_review FROM groups g
                                   WHERE g.pipeline_id=?""", (pipeline_id,)).fetchall()
            for row in rows:
                if data.group_ids and row["group_id"] not in data.group_ids:
                    continue
                if row["manual_review"] and not data.force:
                    skipped.append({"group_id": row["group_id"], "reason": "Đang ở chế độ kiểm tra thủ công"})
                    continue
                if deps.enqueue_screening(conn, pipeline_id, row["account_id"], row["group_id"],
                                         data.force):
                    queued.append(row["group_id"])
                else:
                    skipped.append({"group_id": row["group_id"],
                                    "reason": "Đang chờ kiểm tra hoặc còn trong thời gian chờ"})
        deps.audit("screening", None, f"Xếp hàng kiểm tra {len(queued)} group", pipeline_id)
        return {"ok": True, "queued": queued, "skipped": skipped, "queued_count": len(queued)}

    @router.post("/api/pipelines/{pipeline_id}/groups/screen-bulk")
    def screen_groups_bulk(pipeline_id: int, data: ScreenRequest,
                           principal=Depends(deps.require_principal)):
        """Xếp hàng kiểm tra mọi group của Workspace này."""
        deps.require_permission(principal, "groups.manage")
        deps.scope_pipeline(principal, pipeline_id)
        return screen_groups(pipeline_id, ScreenRequest(force=data.force), principal)

    @router.get("/api/pipelines/{pipeline_id}/groups/screening-results")
    def screening_results(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Mọi kết quả kiểm tra của Workspace này, kèm bằng chứng."""
        deps.require_permission(principal, "groups.view")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            rows = conn.execute("""SELECT r.*, g.label group_label, g.url group_url, g.posting_mode,
                    g.nickname, a.label account_label
                FROM group_screening_results r JOIN groups g ON g.id=r.group_id
                JOIN accounts a ON a.id=r.account_id
                WHERE r.pipeline_id=? ORDER BY r.id DESC""", (pipeline_id,)).fetchall()
            result = [group_screening.screening_payload(row) for row in rows]
            summary = group_screening.summary_of(conn, pipeline_id)
            queue = [dict(row) for row in conn.execute(
                """SELECT q.*, g.label group_label FROM screening_queue q JOIN groups g ON g.id=q.group_id
                   WHERE q.pipeline_id=? AND q.state IN ('queued','running') ORDER BY q.id""",
                (pipeline_id,)).fetchall()]
        return {"ok": True, "summary": summary, "results": result, "queue": queue}

    @router.get("/api/pipelines/{pipeline_id}/groups/priority")
    def group_priority(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Các group của Workspace này theo thứ tự nên được phục vụ."""
        deps.require_permission(principal, "groups.view")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            rows = conn.execute("""SELECT g.*, a.label account_label FROM groups g
                LEFT JOIN accounts a ON a.id=g.account_id WHERE g.pipeline_id=?""",
                (pipeline_id,)).fetchall()
            groups = []
            for row in rows:
                item = deps.group_payload(conn, row, principal)
                result = deps.screening_row(conn, row["account_id"], row["id"])
                item["screening"] = group_screening.screening_payload(result) if result else None
                item["priority"] = group_screening.priority_index(item["screening_status"])
                item["publishable"] = group_screening.is_publishable(item["screening_status"])
                groups.append(item)
            summary = group_screening.summary_of(conn, pipeline_id)
        groups.sort(key=lambda item: (item["priority"], item["id"]))
        return {"ok": True, "groups": groups, "summary": summary}

    @router.post("/api/pipelines/{pipeline_id}/groups/{group_id}/recheck")
    def recheck_group(pipeline_id: int, group_id: int, data: ScreenRequest,
                      principal=Depends(deps.require_principal)):
        """Xếp hàng kiểm tra lại một group."""
        deps.require_permission(principal, "groups.manage")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            group = deps.scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            queued = deps.enqueue_screening(conn, pipeline_id, group["account_id"], group_id,
                                             data.force)
        if not queued:
            return {"ok": True, "queued": False,
                    "message": "Group đang trong hàng đợi hoặc còn trong thời gian chờ kiểm tra lại."}
        deps.audit("screening", group_id, "Xếp hàng kiểm tra lại group", pipeline_id)
        return {"ok": True, "queued": True}

    return router
