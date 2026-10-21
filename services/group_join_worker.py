"""Thực hiện yêu cầu tham gia group và worker xử lý hàng đợi.

Thao tác tham gia chạy trong một tiến trình trình duyệt riêng, giống các
thao tác kiểm tra group khác. Không vượt checkpoint, không vượt captcha và
không gửi lại một yêu cầu mà kết quả chưa rõ.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from services import group_membership

LOG = logging.getLogger("facebook-pipeline.group-join")

#: Yêu cầu tham gia tối đa cho một tài khoản trong một lần chạy worker.
MAX_REQUESTS_PER_RUN = 3
#: Số phút chờ giữa hai lần thử lại cùng một cặp.
RETRY_COOLDOWN_MINUTES = 60


@dataclass(frozen=True)
class JoinContext:
    """Những phần hạ tầng mà thao tác tham gia cần."""

    db: Callable
    iso: Callable
    root: Path
    data_dir: Path
    provider_config: Callable


def run_join_in_browser(context: JoinContext, account_id: int, group_url: str,
                        answers: dict | None = None) -> dict:
    """Chạy thao tác tham gia group trong tiến trình trình duyệt riêng."""
    token = secrets.token_hex(6)
    payload_file = context.data_dir / f"group-join-{account_id}-{token}.json"
    result_file = context.data_dir / f"group-join-result-{account_id}-{token}.json"
    payload = {"account_id": int(account_id), "group_url": str(group_url),
               "answers": answers or {}}
    payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    payload_file.chmod(0o600)
    outcome = {"status": "error", "state": "failed", "details": "Không chạy được trình duyệt",
               "questions": []}
    try:
        proc = subprocess.run(
            [sys.executable, str(context.root / "fb_browser.py"), "join-group",
             str(payload_file), str(result_file)],
            cwd=context.root, env={**os.environ, "DATA_DIR": str(context.data_dir)},
            capture_output=True, text=True, timeout=210)
        if result_file.exists():
            outcome = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            outcome["details"] = f"Worker exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        outcome["status"] = "timeout"
        outcome["state"] = "failed"
        outcome["details"] = "Kiểm tra quá thời gian; chưa rõ kết quả nên không gửi lại."
    except Exception as error:  # noqa: BLE001 - một lỗi không dừng cả worker
        outcome["status"] = "error"
        outcome["state"] = "failed"
        outcome["details"] = f"Lỗi tham gia: {type(error).__name__}"
    finally:
        payload_file.unlink(missing_ok=True)
        result_file.unlink(missing_ok=True)
    return outcome


def record_outcome(context: JoinContext, pipeline_id: int, account_id: int, group_id: int,
                   outcome: dict, answers: dict | None = None) -> str:
    """Ghi lại kết quả của một lần tham gia và trả về trạng thái mới."""
    status = str(outcome.get("status") or "error")
    state = str(outcome.get("state") or "failed")
    questions = outcome.get("questions") or []
    if state == "ok" and status == "joined":
        membership = group_membership.JOINED
    elif state == "ok" and status == "pending_approval":
        membership = group_membership.PENDING_APPROVAL
    elif state == "needs_answers":
        membership = group_membership.NOT_MEMBER
    elif status == "rejected":
        membership = group_membership.JOIN_REJECTED
    elif status == "checkpoint":
        membership = group_membership.UNKNOWN
    elif status == "unknown":
        membership = group_membership.UNKNOWN
    else:
        membership = group_membership.JOIN_FAILED
    with context.db() as conn:
        group_membership.record(conn, pipeline_id, account_id, group_id, membership,
                                questions=questions, answers=answers or {},
                                error_code=status[:40])
    return membership


def pending_requests(context: JoinContext, pipeline_id: int, limit: int = 5) -> list[dict]:
    """Các cặp cần gửi yêu cầu tham gia, theo thứ tự đã cập nhật."""
    with context.db() as conn:
        rows = conn.execute(f"""SELECT m.*, g.url group_url, g.label group_label,
                                      g.join_answers_json, g.auto_join
                               FROM group_memberships m JOIN groups g ON g.id=m.group_id
                               WHERE m.pipeline_id=? AND g.auto_join=1 AND g.enabled=1
                                 AND m.membership_status NOT IN
                                     ({','.join('?' * len(group_membership.SETTLED_STATUSES))})
                               ORDER BY m.updated_at LIMIT ?""",
                            (int(pipeline_id), *group_membership.SETTLED_STATUSES,
                             int(limit))).fetchall()
    return [dict(row) for row in rows]


def join_worker(context: JoinContext, pipeline_ids: Callable[[], list[int]],
                stop_event) -> None:
    """Worker xử lý yêu cầu tham gia của các group CTV đã bật."""
    while not stop_event.is_set():
        handled = 0
        for pipeline_id in pipeline_ids():
            if handled >= MAX_REQUESTS_PER_RUN:
                break
            try:
                for row in pending_requests(context, pipeline_id):
                    if handled >= MAX_REQUESTS_PER_RUN:
                        break
                    answers = _answers(row.get("join_answers_json"))
                    outcome = run_join_in_browser(context, row["account_id"],
                                                  row["group_url"], answers)
                    record_outcome(context, pipeline_id, row["account_id"], row["group_id"],
                                   outcome, answers)
                    handled += 1
            except Exception as error:  # noqa: BLE001 - worker không được chết
                LOG.info("Group join worker: %s", type(error).__name__)
        stop_event.wait(60)


def _answers(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
