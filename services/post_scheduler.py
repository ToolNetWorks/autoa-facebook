"""Bộ xếp lịch hiểu tài khoản.

Bộ xếp lịch chia công việc cho các tài khoản Facebook của Workspace theo hạn
mức của **từng tài khoản**, theo group đủ điều kiện và theo album chung.

Mỗi job được tạo ra đã có đủ ``account_id``, ``group_id``, ``media_id`` và
caption, nên không bao giờ có chuyện đổi tài khoản lúc chạy. Tài khoản đã bị
Facebook cảnh báo hoặc khóa sẽ không được chọn, và nếu không còn tài khoản
đủ điều kiện thì bộ xếp lịch không tạo gì cả.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from services import ai_provider, duplicate_guard, posting_limits
from services import group_classification, group_eligibility, group_verification
from services.auto_posting import create_caption
from services.caption_validator import analyze_caption
from services.image_selection import select_media

LOG = logging.getLogger("facebook-pipeline.post-scheduler")

STATUS_APPROVED = "approved"
STATUS_PENDING = "pending"


def _account_rows(conn, pipeline_id: int, settings: dict) -> list[dict]:
    """The Facebook accounts of the workspace that may post."""
    rows = conn.execute("""SELECT a.* FROM accounts a WHERE a.pipeline_id=?
                            ORDER BY a.id""", (int(pipeline_id),)).fetchall()
    accounts = []
    chosen = settings.get("account_ids") or []
    for row in rows:
        item = dict(row)
        if settings.get("scope") == "selected" and chosen and item["id"] not in chosen:
            continue
        if not item.get("enabled"):
            continue
        # Chỉ loại tài khoản mà phiên đăng nhập đã hỏng rõ ràng; tài khoản
        # chưa kiểm tra phiên vẫn được thử, worker sẽ báo lỗi nếu thật sự hỏng.
        if (item.get("session_status") or "") in ("expired", "error", "invalid", "revoked"):
            continue
        accounts.append(item)
    return accounts


def _group_rows(conn, pipeline_id: int, settings: dict) -> list[dict]:
    """The groups of the workspace that are allowed to receive a post."""
    rows = conn.execute("""SELECT g.*, a.label account_label, a.session_status account_session,
                                   a.enabled account_enabled
                              FROM groups g LEFT JOIN accounts a ON a.id=g.account_id
                             WHERE g.pipeline_id=? ORDER BY g.id""", (int(pipeline_id),)).fetchall()
    groups = []
    chosen = settings.get("group_ids") or []
    for row in rows:
        item = dict(row)
        if chosen and item["id"] not in chosen:
            continue
        groups.append(item)
    return groups


def _groups_of_account(conn, account_id: int, groups: list[dict]) -> list[dict]:
    """The groups this account is allowed to post into."""
    assignments = conn.execute("""SELECT group_id FROM group_accounts
                                   WHERE account_id=? AND status='active'""",
                               (int(account_id),)).fetchall()
    allowed = {row["group_id"] for row in assignments}
    return [group for group in groups
            if group.get("account_id") == account_id or group.get("id") in allowed]


def _media_rows(conn, pipeline_id: int) -> list[dict]:
    """The pictures of the album with their fingerprints."""
    rows = conn.execute("""SELECT id, description, tags_json, safety_status, analysis_status,
                                   usable, locked, sha256, signature, usage_count, last_used_at
                              FROM media_assets WHERE pipeline_id=? ORDER BY id""",
                        (int(pipeline_id),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.get("tags_json") or "[]")
        except (TypeError, ValueError):
            item["tags"] = []
        item["usable"] = bool(item.get("usable"))
        item["locked"] = bool(item.get("locked"))
        result.append(item)
    return result


def _job_key(pipeline_id: int, account_id: int, group_id: int, media_id: int, day: str) -> str:
    """The key that keeps two workers from making the same job."""
    return f"{int(pipeline_id)}:{int(account_id)}:{int(group_id)}:{int(media_id)}:{day}"


def group_blockers(groups: list[dict], decisions: dict | None = None) -> list[dict]:
    """Vì sao một group chưa được đưa vào lịch.

    Lý do viết bằng chữ để người quản trị hiểu được, không phải mã lỗi.
    ``decisions`` là quyết định của từng cặp Facebook–Group, để lý do nói
    đúng về tài khoản nào chưa đủ điều kiện.
    """
    blockers = []
    for group in groups:
        reason = ""
        status = group.get("screening_status") or "unknown"
        pair = (decisions or {}).get(group.get("id")) or {}
        if not group.get("enabled"):
            reason = "Group đang tạm dừng"
        elif group.get("status") != "active":
            reason = "Group không hoạt động"
        elif not group.get("approved_target"):
            reason = "Chưa được duyệt làm group đích"
        elif status == group_verification.NOT_TESTED:
            reason = "Chưa có bằng chứng đăng bài thật cho group này"
        elif status == group_verification.TEST_IN_PROGRESS:
            reason = "Đang xác minh quyền đăng"
        elif status == group_verification.SUBMITTED_UNVERIFIED:
            reason = "Đã gửi bài nhưng chưa xác nhận được Facebook tiếp nhận"
        elif status == group_verification.REJECTED:
            reason = "Bài đăng bị từ chối"
        elif status == group_verification.RESTRICTED_CONFIRMED:
            reason = "Facebook hạn chế quyền đăng trong group này"
        elif status == group_verification.CHECK_FAILED:
            reason = "Kiểm tra quyền đăng không thành công"
        elif status not in group_verification.SERVABLE_STATUSES:
            reason = "Chưa xác minh đủ điều kiện đăng tự động"
        elif (group.get("posting_mode") == "nickname"
              and not (group.get("nickname") or "").strip()):
            reason = "Chế độ biệt danh chưa có tên"
        elif (group.get("account_session") or "") in ("expired", "error", "invalid", "revoked"):
            reason = "Phiên đăng nhập Facebook đã hết hạn"
        if reason:
            blockers.append({"group_id": group.get("id"), "label": group.get("label"),
                             "screening_status": status, "reason": reason,
                             "action": pair.get("action", ""),
                             "eligible_account_ids": pair.get("eligible_account_ids", [])})
    return blockers


def pair_decisions(conn, pipeline_id: int, groups: list[dict]) -> dict:
    """Quyết định của từng cặp Facebook–Group, theo từng group."""
    decisions = {}
    for group in groups:
        allowed = []
        for account_id in group_eligibility.accounts_of(conn, pipeline_id, group):
            decision = group_eligibility.pair_decision(conn, pipeline_id, account_id,
                                                        group["id"], group)
            if decision["eligible"]:
                allowed.append(account_id)
        decisions[group["id"]] = {"eligible_account_ids": allowed}
    return decisions


def plan(conn, pipeline_id: int, moment: datetime | None = None,
         provider_config=None) -> dict:
    """What the scheduler would do right now, without creating anything."""
    settings = posting_limits.settings_of(conn, pipeline_id)
    if not settings["enabled"]:
        return {"enabled": False, "reason": "Tự động đăng group đang tắt", "jobs": [],
                "accounts": [], "groups": []}
    moment = moment or posting_limits.now_in(settings.get("timezone"))
    day = posting_limits.day_of(moment)
    accounts = _account_rows(conn, pipeline_id, settings)
    groups = _group_rows(conn, pipeline_id, settings)
    decisions = pair_decisions(conn, pipeline_id, groups)
        # Bài đầu tiên của một cặp cũng là lần xác minh, nên group chưa từng
    # đăng vẫn được phục vụ. Sau lần đầu, trạng thái bằng chứng quyết định.
    eligible = group_eligibility.eligible_pairs(conn, pipeline_id, groups)
    media = _media_rows(conn, pipeline_id)
    provider_config = provider_config or ai_provider.load_config()

    jobs = []
    notes = []
    no_media = False
    eligible_by_account: dict[str, int] = {}
    max_pending_approvals = max(0, int(settings.get("max_pending_approvals") or 0))
    pending_approvals = group_classification.pending_approval_count(conn, pipeline_id)
    classifications = group_classification.pairs_of(conn, pipeline_id)
    if not eligible:
        notes.append("Chưa có cặp Facebook–Group nào đủ điều kiện đăng tự động trong Workspace")
    if not accounts:
        notes.append("Chưa có Facebook nào đủ điều kiện để đăng")
    used_media: set[int] = set()
    for account in accounts:
        state = posting_limits.account_state(conn, account["id"], settings, moment)
        own_groups = [group for group in eligible
                      if account["id"] in group.get("eligible_account_ids", [])]
        eligible_by_account[str(account["id"])] = len(own_groups)
        if state["paused"]:
            notes.append(f"{account['label']}: đang tạm dừng")
            continue
        if state["at_limit"]:
            notes.append(f"{account['label']}: đã đủ {state['used']}/{state['daily_limit']} bài hôm nay")
            continue
        if state["next_allowed_at"]:
            notes.append(f"{account['label']}: chờ đủ khoảng cách giữa hai bài")
            continue
        if not own_groups:
            continue
        for group in own_groups:
            if state["used"] >= state["daily_limit"]:
                break
            # So bai cho group "can duyet" duoc gioi han, de khong gui lien tuc
            # vao mot group moi bai deu can nguoi duyet.
            entry = classifications.get((account["id"], group["id"]))
            if (entry is not None
                    and entry["classification"] == group_classification.NEEDS_APPROVAL
                    and pending_approvals >= max_pending_approvals):
                notes.append(f"{group.get('label')}: đang chờ group duyệt, tạm dừng gửi thêm")
                continue
            if (group.get("posting_mode") == "nickname"
                    and not (group.get("nickname") or "").strip()):
                notes.append(f"{group.get('label')}: chưa có tên biệt danh")
                continue
            group_limit = posting_limits.limits_of(conn, account["id"]).get("group_daily_limit")
            if group_limit is not None and posting_limits.group_usage_of(conn, group["id"], day) >= int(group_limit):
                notes.append(f"{group.get('label')}: đã đủ hạn mức riêng của group hôm nay")
                continue
            pipeline_limit = int(settings.get("pipeline_daily_limit") or 0)
            if pipeline_limit and posting_limits.pipeline_usage_of(conn, pipeline_id, day) >= pipeline_limit:
                notes.append("Pipeline đã đủ hạn mức tổng hôm nay")
                break
            candidates = [item for item in media if item["id"] not in used_media]
            chosen = select_media(candidates, settings.get("topic", ""))
            picture = chosen.get("media")
            if not picture:
                notes.append("Album chưa có ảnh đủ điều kiện an toàn")
                no_media = True
                break
            caption_result = create_caption(picture.get("description") or "",
                                            list(picture.get("tags") or []),
                                            settings.get("topic", ""), provider_config,
                                            posting_mode=group.get("posting_mode") or "anonymous")
            if caption_result.get("status") == "blocked":
                notes.append("Caption bị chặn vì nội dung rủi ro")
                continue
            verdict = duplicate_guard.check(conn, pipeline_id, picture,
                                            caption_result.get("caption", ""), group["id"], account["id"])
            if settings.get("anti_duplicate") and verdict["blocked"]:
                notes.append(f"{group.get('label')}: {verdict['reason']}")
                continue
            jobs.append({
                "account_id": account["id"], "account_label": account.get("label"),
                "group_id": group["id"], "group_label": group.get("label"),
                "media_id": picture["id"], "media_description": (picture.get("description") or "")[:120],
                "caption": caption_result.get("caption", ""),
                "caption_status": caption_result.get("status"),
                "posting_mode": group.get("posting_mode") or "anonymous",
                "nickname": group.get("nickname") or "",
                "key": _job_key(pipeline_id, account["id"], group["id"], picture["id"], day),
            })
            used_media.add(picture["id"])
            state["used"] += 1
    return {"enabled": True, "jobs": jobs, "notes": notes, "no_media": no_media, "accounts": accounts,
            "groups": groups, "eligible_groups": [group["id"] for group in eligible],
            "decisions": decisions,
            "eligible_by_account": eligible_by_account,
            "blockers": group_blockers(groups, decisions),
            "day": day}


def distribute(conn, pipeline_id: int, moment: datetime | None = None,
               provider_config=None, max_jobs: int = 10) -> dict:
    """Create the jobs of this workspace for today.

    Việc chọn tài khoản, giữ chỗ bài và ghi dấu vết nằm trong cùng một giao
    dịch, nên hai worker chạy cùng lúc không thể tạo ra hai job giống nhau.
    """
    settings = posting_limits.settings_of(conn, pipeline_id)
    if not settings["enabled"]:
        return {"created": [], "skipped": [], "notes": ["Tự động đăng group đang tắt"]}
    moment = moment or posting_limits.now_in(settings.get("timezone"))
    day = posting_limits.day_of(moment)
    outcome = plan(conn, pipeline_id, moment, provider_config)
    created = []
    skipped = list(outcome.get("notes", []))
    for job in outcome["jobs"][:max_jobs]:
        key = job["key"]
        try:
            cursor = conn.execute("""INSERT INTO auto_post_jobs
                (pipeline_id,config_id,media_id,account_id,group_id,caption,posting_mode,nickname,
                 status,needs_approval,idempotency_key,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(pipeline_id), None, job["media_id"], job["account_id"], job["group_id"],
                 job["caption"], job["posting_mode"], job["nickname"],
                 STATUS_PENDING if settings.get("require_approval", True) else STATUS_APPROVED,
                 int(settings.get("require_approval", True)), key,
                 datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
        except Exception as error:
            skipped.append(f"Group {job.get('group_label')}: không tạo được job ({type(error).__name__})")
            continue
        job_id = cursor.lastrowid
        posting_limits.reserve(conn, job["account_id"], job["group_id"], day)
        media = {"sha256": _sha256_of(conn, job["media_id"]),
                 "signature": _signature_of(conn, job["media_id"])}
        duplicate_guard.record(conn, pipeline_id, duplicate_guard.KIND_MEDIA_EXACT,
                               duplicate_guard.media_fingerprint(media["sha256"]), job["media_id"], job_id)
        duplicate_guard.record(conn, pipeline_id, duplicate_guard.KIND_CAPTION,
                               duplicate_guard.caption_fingerprint(job["caption"]), job["media_id"], job_id,
                               job["group_id"])
        duplicate_guard.record(conn, pipeline_id, duplicate_guard.KIND_CONTENT,
                               duplicate_guard.content_fingerprint(
                                   duplicate_guard.media_fingerprint(media["sha256"]), job["caption"]),
                               job["media_id"], job_id)
        conn.execute("""INSERT INTO media_usage_history(pipeline_id,media_id,account_id,group_id,job_id,used_at,note)
                        VALUES(?,?,?,?,?,?,?)""",
                     (int(pipeline_id), job["media_id"], job["account_id"], job["group_id"],
                      job_id, datetime.now(timezone.utc).isoformat(), "auto-post"))
        created.append({"id": job_id, "account_label": job["account_label"],
                        "group_id": job["group_id"], "group_label": job["group_label"],
                        "media_id": job["media_id"],
                        "status": STATUS_PENDING if settings.get("require_approval", True) else STATUS_APPROVED})
    return {"created": created, "skipped": skipped, "day": day,
            "no_media": bool(outcome.get("no_media")),
            "planned": len(outcome.get("jobs", [])),
            "notes": outcome.get("notes", []),
            "blockers": outcome.get("blockers", []),
            "eligible_groups": outcome.get("eligible_groups", []),
            "eligible_by_account": outcome.get("eligible_by_account", {})}


def _sha256_of(conn, media_id: int) -> str:
    row = conn.execute("SELECT sha256 FROM media_assets WHERE id=?", (int(media_id),)).fetchone()
    return row["sha256"] if row else ""


def _signature_of(conn, media_id: int) -> str:
    row = conn.execute("SELECT signature FROM media_assets WHERE id=?", (int(media_id),)).fetchone()
    return row["signature"] if row else ""
