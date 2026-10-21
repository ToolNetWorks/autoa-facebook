"""API xác minh quyền đăng và tự động tham gia group.

Module này không nhập trực tiếp từ ``app`` để tránh vòng lặp import: các hàm
bảo vệ và hạ tầng được truyền vào qua :class:`Deps` khi ``app`` dựng router.

Quy trình xác minh một cặp Facebook–Group:

1. CTV chọn "Xác minh bằng bài đăng".
2. Hệ thống chuẩn bị một bài viết thật để CTV xem trước.
3. CTV xác nhận gửi.
4. Bài được gửi bằng đúng tài khoản và đúng chế độ đăng đã chọn.
5. Kết quả thật được ghi lại thành bằng chứng của cặp đó.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from services import group_eligibility, group_membership, group_verification


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
    iso: Callable
    now: Callable
    create_post: Callable
    prepare_post: Callable
    run_join: Callable


class JoinAnswers(BaseModel):
    """Câu trả lời của CTV cho các câu hỏi của group."""

    answers: dict[str, str] = Field(default_factory=dict)
    save: bool = True


class JoinRequest(BaseModel):
    """Yêu cầu tham gia group."""

    force: bool = False


class AutoJoinUpdate(BaseModel):
    """Bật hoặc tắt tự động yêu cầu tham gia cho một group."""

    auto_join: bool


def build_router(deps: Deps) -> APIRouter:
    """Dựng router của xác minh và tham gia group."""
    router = APIRouter()

    # --- xác minh bằng bài đăng ---------------------------------------------

    @router.get("/api/pipelines/{pipeline_id}/groups/verifications")
    def list_verifications(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Bằng chứng xác minh của mọi cặp Facebook–Group trong Workspace.

        Cặp chưa có bằng chứng nào cũng có mặt, với trạng thái chưa xác minh,
        để CTV có thể bắt đầu xác minh ngay.
        """
        deps.require_permission(principal, "groups.view")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            groups = conn.execute("""SELECT g.id group_id, g.label group_label, g.url group_url,
                                            g.posting_mode, g.nickname, g.account_id,
                                            g.screening_status
                                     FROM groups g WHERE g.pipeline_id=? ORDER BY g.id""",
                                  (pipeline_id,)).fetchall()
            verifications = group_verification.pairs_of(conn, pipeline_id)
            memberships = group_membership.pairs_of(conn, pipeline_id)
            result = []
            for group in groups:
                for account_id in _accounts_of_group(conn, group):
                    account = conn.execute("SELECT label FROM accounts WHERE id=?",
                                           (account_id,)).fetchone()
                    key = (int(account_id), int(group["group_id"]))
                    item = verifications.get(key) or group_verification.payload(None)
                    item["group_id"] = group["group_id"]
                    item["account_id"] = account_id
                    item["group_label"] = group["group_label"]
                    item["group_url"] = group["group_url"]
                    item["account_label"] = account["label"] if account else ""
                    item["posting_mode"] = group["posting_mode"]
                    item["nickname"] = group["nickname"]
                    item["membership"] = memberships.get(key, {})
                    decision = _decision_of(conn, pipeline_id, account_id,
                                           group["group_id"], item)
                    item["decision"] = decision.get("decision", group_eligibility.NEEDS_ACTION)
                    item["reason"] = decision.get("reason", "")
                    item["next_action"] = decision.get("action", "")
                    result.append(item)
        return {"ok": True, "verifications": result}

    @router.get("/api/pipelines/{pipeline_id}/groups/{group_id}/verification")
    def verification_of(pipeline_id: int, group_id: int,
                        principal=Depends(deps.require_principal)):
        """Bằng chứng xác minh của một group, theo từng tài khoản được gán."""
        deps.require_permission(principal, "groups.view")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            group = deps.scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            accounts = conn.execute("""SELECT DISTINCT a.id, a.label FROM accounts a
                                       LEFT JOIN group_accounts ga ON ga.account_id=a.id
                                       WHERE a.pipeline_id=?""", (pipeline_id,)).fetchall()
            items = []
            for account in accounts:
                row = group_verification.row_of(conn, pipeline_id, account["id"], group_id)
                item = group_verification.payload(row)
                item["account_id"] = account["id"]
                item["account_label"] = account["label"]
                items.append(item)
        return {"ok": True, "group_id": group_id, "verifications": items}

    @router.post("/api/pipelines/{pipeline_id}/groups/{group_id}/verification/prepare")
    def prepare_verification(pipeline_id: int, group_id: int,
                             principal=Depends(deps.require_principal)):
        """Chuẩn bị một bài viết thật để CTV xem trước trước khi xác minh."""
        deps.require_permission(principal, "group_posts.create")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            group = deps.scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            account = deps.scoped_entry(conn, "accounts", group["account_id"], principal)
            if account["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            if not group["approved_target"]:
                raise HTTPException(409, "Group chưa được duyệt làm group đích")
            if group["status"] != "active" or not group["enabled"]:
                raise HTTPException(409, "Group đang tạm dừng")
            draft = deps.prepare_post(conn, pipeline_id, account["id"], group_id)
        return {"ok": True, "draft": draft}

    @router.post("/api/pipelines/{pipeline_id}/groups/{group_id}/verification/confirm")
    def confirm_verification(pipeline_id: int, group_id: int,
                             principal=Depends(deps.require_principal)):
        """Gửi bài xác minh và ghi nhận lần xác minh đang thực hiện."""
        deps.require_permission(principal, "group_posts.create")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            group = deps.scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            account = deps.scoped_entry(conn, "accounts", group["account_id"], principal)
            if account["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            current = group_verification.status_of(conn, pipeline_id, account["id"], group_id)
            if current == group_verification.TEST_IN_PROGRESS:
                raise HTTPException(409, "Lần xác minh này đang thực hiện")
            result = deps.create_post(conn, pipeline_id, account["id"], group_id)
            group_verification.start_attempt(
                conn, pipeline_id, account["id"], group_id,
                post_id=result.get("post_id"), posting_mode=result.get("posting_mode", ""))
        deps.audit("screening", group_id, "Bắt đầu xác minh quyền đăng bằng bài đăng", pipeline_id)
        return {"ok": True, **result}

    # --- tự động tham gia group ---------------------------------------------

    @router.get("/api/pipelines/{pipeline_id}/groups/memberships")
    def list_memberships(pipeline_id: int, principal=Depends(deps.require_principal)):
        """Trạng thái thành viên của mọi cặp Facebook–Group trong Workspace."""
        deps.require_permission(principal, "groups.view")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            rows = conn.execute("""SELECT m.*, g.label group_label, a.label account_label,
                                          g.auto_join, g.url group_url
                                   FROM group_memberships m JOIN groups g ON g.id=m.group_id
                                   JOIN accounts a ON a.id=m.account_id
                                   WHERE m.pipeline_id=? ORDER BY g.id, a.id""",
                                (pipeline_id,)).fetchall()
            result = []
            for row in rows:
                item = group_membership.payload(row)
                item["group_id"] = row["group_id"]
                item["account_id"] = row["account_id"]
                item["group_label"] = row["group_label"]
                item["account_label"] = row["account_label"]
                item["auto_join"] = bool(row["auto_join"])
                result.append(item)
        return {"ok": True, "memberships": result}

    @router.post("/api/pipelines/{pipeline_id}/groups/{group_id}/join")
    def request_join(pipeline_id: int, group_id: int, data: JoinRequest,
                     principal=Depends(deps.require_principal)):
        """Yêu cầu tham gia group bằng đúng tài khoản được gán cho group."""
        deps.require_permission(principal, "groups.manage")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            group = deps.scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            account = deps.scoped_entry(conn, "accounts", group["account_id"], principal)
            if account["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            current = group_membership.status_of(conn, pipeline_id, account["id"], group_id)
            if current == group_membership.JOINED:
                return {"ok": True, "state": "joined", "membership": current, "questions": [],
                        "message": "Tài khoản đã là thành viên group."}
            if current == group_membership.PENDING_APPROVAL and not data.force:
                return {"ok": True, "state": "pending_approval", "membership": current,
                        "questions": [],
                        "message": "Yêu cầu tham gia đang chờ group duyệt."}
            if current == group_membership.JOIN_REQUESTING and not data.force:
                return {"ok": True, "state": "requesting", "membership": current, "questions": [],
                        "message": "Yêu cầu tham gia đang được gửi."}
            answers = _saved_answers(conn, group_id)
            questions = _questions(conn, pipeline_id, account["id"], group_id)
            group_membership.record(conn, pipeline_id, account["id"], group_id,
                                    group_membership.JOIN_REQUESTING, questions=questions,
                                    answers=answers)
        deps.audit("group", group_id, "Yêu cầu tham gia group", pipeline_id)
        outcome = deps.run_join(pipeline_id, account["id"], group_id, answers)
        return _join_response(conn_factory=deps.db, pipeline_id=pipeline_id,
                              account_id=account["id"], group_id=group_id, outcome=outcome,
                              answers=answers)

    @router.post("/api/pipelines/{pipeline_id}/groups/{group_id}/join/answers")
    def answer_join_questions(pipeline_id: int, group_id: int, data: JoinAnswers,
                               principal=Depends(deps.require_principal)):
        """Trả lời câu hỏi của group rồi gửi yêu cầu tham gia."""
        deps.require_permission(principal, "groups.manage")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            group = deps.scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            account = deps.scoped_entry(conn, "accounts", group["account_id"], principal)
            if account["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            questions = _questions(conn, pipeline_id, account["id"], group_id)
            missing = [question for question in questions
                       if not str(data.answers.get(question) or "").strip()]
            if missing:
                raise HTTPException(422, "Còn câu hỏi chưa được trả lời")
            if data.save:
                conn.execute("UPDATE groups SET join_answers_json=? WHERE id=?",
                             (json.dumps(data.answers, ensure_ascii=False), group_id))
            group_membership.record(conn, pipeline_id, account["id"], group_id,
                                    group_membership.JOIN_REQUESTING,
                                    questions=questions, answers=data.answers)
        deps.audit("group", group_id, "Trả lời câu hỏi và yêu cầu tham gia group", pipeline_id)
        outcome = deps.run_join(pipeline_id, account["id"], group_id, data.answers)
        return _join_response(conn_factory=deps.db, pipeline_id=pipeline_id,
                              account_id=account["id"], group_id=group_id, outcome=outcome,
                              answers=data.answers)

    @router.patch("/api/pipelines/{pipeline_id}/groups/{group_id}/auto-join")
    def set_auto_join(pipeline_id: int, group_id: int, data: AutoJoinUpdate,
                      principal=Depends(deps.require_principal)):
        """Bật hoặc tắt tự động yêu cầu tham gia cho một group."""
        deps.require_permission(principal, "groups.manage")
        deps.scope_pipeline(principal, pipeline_id)
        with deps.db() as conn:
            deps.entry(conn, "pipelines", pipeline_id)
            group = deps.scoped_entry(conn, "groups", group_id, principal)
            if group["pipeline_id"] != pipeline_id:
                raise HTTPException(404, "Not found")
            conn.execute("UPDATE groups SET auto_join=? WHERE id=?",
                         (int(bool(data.auto_join)), group_id))
        deps.audit("group", group_id,
                   "Bật tự động yêu cầu tham gia" if data.auto_join
                   else "Tắt tự động yêu cầu tham gia", pipeline_id)
        return {"ok": True, "auto_join": bool(data.auto_join)}

    return router


def _decision_of(conn, pipeline_id: int, account_id: int, group_id: int, item: dict) -> dict:
    """Trạng thái nghiệp vụ của một cặp, dùng chung với bộ xếp lịch."""
    from services import group_eligibility
    return group_eligibility.pair_decision(conn, pipeline_id, account_id, group_id)


def _accounts_of_group(conn, group) -> list[int]:
    """Các tài khoản Facebook được gán cho group này."""
    found: list[int] = []
    if group["account_id"]:
        found.append(int(group["account_id"]))
    for row in conn.execute("""SELECT account_id FROM group_accounts
                               WHERE group_id=? AND status='active'""",
                            (group["group_id"],)).fetchall():
        if int(row["account_id"]) not in found:
            found.append(int(row["account_id"]))
    return found


def _join_response(conn_factory, pipeline_id: int, account_id: int, group_id: int,
                   outcome: dict, answers: dict | None = None) -> dict:
    """Ghi lại kết quả tham gia và trả về câu trả lời cho CTV."""
    status, error_code = group_membership.status_from_outcome(outcome)
    questions = outcome.get("questions") or []
    with conn_factory() as conn:
        group_membership.record(conn, pipeline_id, account_id, group_id, status,
                                questions=questions, answers=answers or {},
                                error_code=error_code)
    message = {
        group_membership.JOINED: "Đã là thành viên group.",
        group_membership.PENDING_APPROVAL: "Yêu cầu tham gia đang chờ group duyệt.",
        group_membership.JOIN_REJECTED: "Group từ chối yêu cầu tham gia.",
        group_membership.UNKNOWN: outcome.get("details") or "Chưa xác định được kết quả.",
    }.get(status, outcome.get("details") or "Tham gia không thành công.")
    if outcome.get("state") == "needs_answers":
        message = "Group yêu cầu trả lời câu hỏi trước khi tham gia."
    return {"ok": True, "state": outcome.get("state", "failed"),
            "status": outcome.get("status", "error"), "membership": status,
            "questions": questions, "details": outcome.get("details", ""),
            "message": message}


def _saved_answers(conn, group_id: int) -> dict:
    row = conn.execute("SELECT join_answers_json FROM groups WHERE id=?", (group_id,)).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row["join_answers_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _questions(conn, pipeline_id: int, account_id: int, group_id: int) -> list:
    row = conn.execute("""SELECT join_questions_json FROM group_memberships
                           WHERE pipeline_id=? AND account_id=? AND group_id=?""",
                        (pipeline_id, account_id, group_id)).fetchone()
    if row is None:
        return []
    return group_verification.questions_json(row["join_questions_json"])
