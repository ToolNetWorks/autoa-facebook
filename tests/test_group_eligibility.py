"""Điều kiện đăng của một cặp Facebook–Group.

Mười lăm trường hợp bắt buộc: group mới, group riêng tư, tài khoản chưa tham
gia, tài khoản bị cấm đăng thật, phiên hết hạn, checkpoint, giao diện đổi,
trình duyệt quá thời gian, group cần Admin duyệt, group không hỗ trợ ẩn danh,
một group nhiều Facebook, hai Facebook khác nhau, album chưa có ảnh, CTV chọn
nhiều group, và bộ xếp lịch không tạo job trùng.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from scheduler import job_allocator
from services import group_eligibility, group_membership, group_screening, group_verification
from services.group_screening import (
    STATUS_FAILED, STATUS_NOT_ACCESSIBLE, STATUS_NOT_MEMBER, STATUS_PRIORITY, STATUS_REVIEW,
    STATUS_RESTRICTED, STATUS_UNKNOWN, classify,
)

CTV_PASSWORD = "ctv-eligibility-password-2026"


def make_pipeline(admin, name):
    return admin.post('/api/pipelines', json={'name': name}).json()['id']


def make_account(admin, pipeline_id, label, session="connected"):
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label}).json()
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status=? WHERE id=?",
                     (session, account['id']))
    return account['id']


def make_group(admin, pipeline_id, account_id, label, url, mode='anonymous', nickname='',
               approved=True):
    payload = nickname or ('Tạm' if mode == 'nickname' else '')
    group = admin.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': label, 'url': url,
        'approved_target': approved, 'posting_mode': mode, 'nickname': payload}).json()
    return group['id']


def store_result(conn, pipeline_id, account_id, group_id, status, **evidence):
    """Ghi lại quan sát của trình duyệt và bằng chứng đăng bài của một cặp."""
    payload = {"check_status": "ok", "accessible": True, "is_member": True, "can_post": True,
               "requires_approval": False, "rules_mention_approval": False,
               "supports_anonymous": True, "supports_nickname": True, "nickname_available": True,
               "direct_posts": 0, "pending_posts": 0, "rejected_posts": 0, "manual_review": False}
    payload.update(evidence)
    stamp = m.iso()
    conn.execute("""INSERT INTO group_screening_results
        (pipeline_id,account_id,group_id,moderation_status,verification_source,confidence,
         evidence_json,last_checked_at,expires_at,direct_posts,pending_posts,rejected_posts,
         notes,created_at,updated_at)
        VALUES(?,?,?,?,'browser_check',0.9,?,?,?,?,?,?,'',?,?)
        ON CONFLICT(account_id,group_id) DO UPDATE SET
            moderation_status=excluded.moderation_status, evidence_json=excluded.evidence_json,
            last_checked_at=excluded.last_checked_at, updated_at=excluded.updated_at""",
                 (pipeline_id, account_id, group_id, status, m.json.dumps(payload, ensure_ascii=False),
                  stamp, stamp, int(payload.get("direct_posts") or 0),
                  int(payload.get("pending_posts") or 0), int(payload.get("rejected_posts") or 0),
                  stamp, stamp))
    # Bằng chứng đăng bài thật là nguồn kết luận duy nhất.
    verification = {
        STATUS_PRIORITY: group_verification.VERIFIED_POSTED,
        STATUS_REVIEW: group_verification.PENDING_GROUP_APPROVAL,
        STATUS_RESTRICTED: group_verification.RESTRICTED_CONFIRMED,
        STATUS_NOT_MEMBER: group_verification.NOT_TESTED,
        STATUS_NOT_ACCESSIBLE: group_verification.NOT_TESTED,
        STATUS_FAILED: group_verification.CHECK_FAILED,
        STATUS_UNKNOWN: group_verification.NOT_TESTED,
    }.get(status, group_verification.NOT_TESTED)
    group_verification.record_outcome(conn, pipeline_id, account_id, group_id, verification,
                                      posting_mode="anonymous", evidence_reference="test")


def make_picture(admin, pipeline_id, name='a.jpg'):
    """Một ảnh đã qua kiểm tra trong Album."""
    with m.db() as conn:
        cursor = conn.execute("""INSERT INTO media_assets
            (pipeline_id,uploader_id,storage_key,thumbnail_key,original_name,mime_type,width,height,
             size_bytes,sha256,signature,analysis_status,safety_status,usable,description,tags_json,
             created_at,updated_at,owner_user_id)
            VALUES(?,1,?,'t/a.jpg',?,'image/jpeg',600,600,1000,?,'sig','done','pass',1,
                   'một sản phẩm đẹp','[]',?,?,NULL)""",
            (pipeline_id, f"{pipeline_id}/{name}", name, name * 8, m.iso(), m.iso()))
        return cursor.lastrowid


def enable_auto_post(admin, pipeline_id, **overrides):
    """Bật tự động đăng chung của Workspace."""
    payload = {'enabled': True, 'scope': 'selected', 'account_ids': [], 'group_ids': [],
               'daily_limit_per_account': 3, 'min_gap_minutes': 0, 'pipeline_daily_limit': 0,
               'topic': 'sản phẩm', 'ai_caption': False, 'anti_duplicate': True,
               'require_approval': True, 'apply_to_new_accounts': True}
    payload.update(overrides)
    response = admin.put(f'/api/pipelines/{pipeline_id}/auto-post/settings', json=payload)
    assert response.status_code == 200, response.text
    return response.json()['settings']


def decision(conn, pipeline_id, account_id, group_id):
    return group_eligibility.pair_decision(conn, pipeline_id, account_id, group_id)


# --- 1. group mới, Facebook đã là thành viên --------------------------------

def test_a_new_group_with_a_member_account_is_eligible(client):
    pipeline = make_pipeline(client, '@el1')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group mới',
                       'https://www.facebook.com/groups/10001/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_PRIORITY)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is True
    assert result['status'] == group_verification.VERIFIED_POSTED


# --- 2. group riêng tư nhưng Facebook có quyền xem --------------------------

def test_a_private_group_that_can_be_seen_is_eligible(client):
    pipeline = make_pipeline(client, '@el2')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group riêng tư',
                       'https://www.facebook.com/groups/10002/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_PRIORITY)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is True


# --- 3. Facebook chưa tham gia group -----------------------------------------

def test_an_account_that_has_not_joined_is_not_eligible(client):
    pipeline = make_pipeline(client, '@el3')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group chưa vào',
                       'https://www.facebook.com/groups/10003/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_NOT_MEMBER,
                     check_status="not_member", is_member=False, can_post=None)
        # Tai khoan bi ro biet la chua tham gia group.
        group_membership.record(conn, pipeline, account, group,
                                group_membership.NOT_MEMBER)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert result['decision'] == group_eligibility.WAITING_MEMBERSHIP
    assert 'tham gia' in result['reason'].lower()


# --- 4. Facebook bị cấm đăng thực sự ----------------------------------------

def test_a_really_restricted_account_is_not_eligible(client):
    pipeline = make_pipeline(client, '@el4')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group bị cấm',
                       'https://www.facebook.com/groups/10004/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_RESTRICTED,
                     check_status="restricted", can_post=False)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert result['status'] == group_verification.RESTRICTED_CONFIRMED


def test_a_rejected_post_after_a_good_one_is_a_restriction():
    status, source, _ = classify({
        "check_status": "ok", "accessible": True, "is_member": True, "can_post": True,
        "direct_posts": 3, "last_direct_post_at": m.iso(m.now() - m.timedelta(days=5)),
        "rejected_posts": 1, "last_rejected_post_at": m.iso(m.now() - m.timedelta(days=1))})
    assert status == STATUS_RESTRICTED
    assert source == "post_evidence"


# --- 5. Facebook login hết hạn ----------------------------------------------

def test_an_expired_session_is_not_eligible(client):
    pipeline = make_pipeline(client, '@el5')
    account = make_account(client, pipeline, 'Facebook', session='expired')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/10005/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_PRIORITY)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert 'đăng nhập lại' in result['action'].lower()


# --- 6. Facebook đang checkpoint ---------------------------------------------

def test_a_checkpoint_is_a_failed_check_not_a_restriction():
    status, _, _ = classify({"check_status": "error", "accessible": None, "is_member": None,
                             "can_post": None, "check_details": "Facebook yêu cầu xác minh"})
    assert status == STATUS_FAILED


# --- 7. composer selector bị thay đổi --------------------------------------

def test_a_changed_composer_is_unknown_not_restricted():
    status, _, _ = classify({"check_status": "needs_review", "accessible": True,
                             "is_member": True, "can_post": None})
    assert status == STATUS_UNKNOWN
    assert status != STATUS_RESTRICTED


# --- 8. browser timeout ------------------------------------------------------

def test_a_browser_timeout_is_a_failed_check():
    for failed in ("error", "busy", "failed", "timeout"):
        status, _, _ = classify({"check_status": failed})
        assert status == STATUS_FAILED


def test_a_failed_check_does_not_block_the_pair_for_ever(client):
    pipeline = make_pipeline(client, '@el8')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/10008/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_FAILED, check_status="busy")
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert result['status'] == group_verification.CHECK_FAILED
    assert 'kiểm tra lại' in result['action'].lower()


# --- 9. group yêu cầu Admin duyệt bài ---------------------------------------

def test_a_group_that_needs_approval_is_served_at_low_priority(client):
    """Group cần quản trị group duyệt vẫn dùng được, nhưng ưu tiên thấp.

    Bài chờ duyệt không được coi là đã công khai, và số bài chờ duyệt được
    kiểm soát, nên group này không bị loại bỏ vô lý.
    """
    pipeline = make_pipeline(client, '@el9')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group cần duyệt',
                       'https://www.facebook.com/groups/10009/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_REVIEW, requires_approval=True)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is True
    assert result['status'] == group_verification.PENDING_GROUP_APPROVAL
    assert result['decision'] == group_eligibility.WAITING_APPROVAL


# --- 10. group không hỗ trợ ẩn danh -----------------------------------------

def test_a_group_without_anonymous_mode_is_not_eligible(client):
    pipeline = make_pipeline(client, '@el10')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group không ẩn danh',
                       'https://www.facebook.com/groups/10010/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_PRIORITY, supports_anonymous=False)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert 'ẩn danh' in result['reason'].lower()


def test_a_nickname_group_without_a_name_is_not_eligible(client):
    pipeline = make_pipeline(client, '@el10b')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group biệt danh',
                       'https://www.facebook.com/groups/10011/', mode='nickname')
    with m.db() as conn:
        conn.execute("UPDATE groups SET nickname='' WHERE id=?", (group,))
        conn.execute("UPDATE group_accounts SET nickname='' WHERE group_id=?", (group,))
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_PRIORITY)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert 'biệt danh' in result['reason'].lower()


# --- 11. một group có nhiều Facebook được gán -------------------------------

def test_one_group_can_have_several_accounts(client):
    pipeline = make_pipeline(client, '@el11')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group nhiều Facebook',
                       'https://www.facebook.com/groups/10012/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,
                                                    nickname,status,created_at,updated_at)
                        VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, second, group, m.iso(), m.iso()))
        store_result(conn, pipeline, first, group, STATUS_PRIORITY)
        store_result(conn, pipeline, second, group, STATUS_PRIORITY)
        for account_id in (first, second):
            assert decision(conn, pipeline, account_id, group)['eligible'] is True


# --- 12. Facebook A đủ điều kiện, Facebook B không đủ điều kiện -------------

def test_two_accounts_of_one_group_have_different_decisions(client):
    pipeline = make_pipeline(client, '@el12')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group hai tài khoản',
                       'https://www.facebook.com/groups/10013/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,
                                                    nickname,status,created_at,updated_at)
                        VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, second, group, m.iso(), m.iso()))
        store_result(conn, pipeline, first, group, STATUS_PRIORITY)
        store_result(conn, pipeline, second, group, STATUS_NOT_MEMBER,
                     check_status="not_member", is_member=False, can_post=None)
        first_decision = decision(conn, pipeline, first, group)
        second_decision = decision(conn, pipeline, second, group)
        assert first_decision['eligible'] is True
        assert first_decision['decision'] == group_eligibility.VERIFIED_READY
        # Facebook B chua tung co bang chung: chi duoc lam bai dau tien.
        assert second_decision['status'] == group_verification.NOT_TESTED
        assert second_decision['decision'] == group_eligibility.FIRST_POST_READY


def test_the_state_of_one_account_is_never_used_for_another(client):
    """Trạng thái của Facebook A không được dùng cho Facebook B."""
    pipeline = make_pipeline(client, '@el12b')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group chung',
                       'https://www.facebook.com/groups/10014/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,
                                                    nickname,status,created_at,updated_at)
                        VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, second, group, m.iso(), m.iso()))
        # Chỉ Facebook A được kiểm tra và đạt.
        store_result(conn, pipeline, first, group, STATUS_PRIORITY)
        result = decision(conn, pipeline, second, group)
    assert result['status'] == group_verification.NOT_TESTED
    assert result['decision'] == group_eligibility.FIRST_POST_READY
    assert 'bài đầu tiên' in result['reason'].lower()


def test_the_scheduler_serves_the_account_that_is_eligible(client):
    pipeline = make_pipeline(client, '@el12c')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group',
                       'https://www.facebook.com/groups/10015/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,
                                                    nickname,status,created_at,updated_at)
                        VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, second, group, m.iso(), m.iso()))
        store_result(conn, pipeline, first, group, STATUS_PRIORITY)
        conn.execute("""UPDATE groups SET screening_status='priority' WHERE id=?""", (group,))
        eligible = group_eligibility.eligible_pairs(conn, pipeline, [
            {"id": group, "pipeline_id": pipeline, "account_id": first, "label": "Group",
             "enabled": 1, "status": "active", "approved_target": 1, "posting_mode": "anonymous",
             "nickname": ""}])
    assert [item["eligible_account_ids"] for item in eligible] == [[first, second]]
    # Tai khoan chua xac minh chi duoc lam bai dau tien, khong duoc xep lich lap lai.
    with m.db() as conn:
        second_decision = group_eligibility.pair_decision(conn, pipeline, second, group)
    assert second_decision['recurring_eligible'] is False


# --- 13. group đủ điều kiện nhưng Album chưa có ảnh READY -------------------

def test_an_eligible_group_without_a_picture_creates_no_job(client):
    pipeline = make_pipeline(client, '@el13')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/10016/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_PRIORITY)
    enable_auto_post(client, pipeline, group_ids=[group])
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert preview['media_id'] is None
    assert preview['posts'] == []
    scheduled = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert scheduled.status_code == 409


# --- 14. CTV chọn nhiều group nhưng chỉ một số đủ điều kiện -----------------

def test_unverified_membership_does_not_prevent_scheduling_but_blocks_execution(client):
    pipeline = make_pipeline(client, '@el14')
    account = make_account(client, pipeline, 'Facebook')
    good = make_group(client, pipeline, account, 'Group đạt',
                      'https://www.facebook.com/groups/10017/')
    bad = make_group(client, pipeline, account, 'Group chưa vào',
                     'https://www.facebook.com/groups/10018/')
    with m.db() as conn:
        store_result(conn, pipeline, account, good, STATUS_PRIORITY)
        store_result(conn, pipeline, account, bad, STATUS_NOT_MEMBER,
                     check_status="not_member", is_member=False, can_post=None)
        group_membership.record(conn,pipeline,account,bad,group_membership.NOT_MEMBER)
    make_picture(client, pipeline)
    enable_auto_post(client, pipeline, group_ids=[good, bad])
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    served = {post['group_id'] for post in preview['posts']}
    assert good in served
    assert bad in served  # Scheduling is independent of execution checks.
    scheduled = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert scheduled.status_code == 201, scheduled.text
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM auto_post_jobs WHERE pipeline_id=? AND group_id=?", (pipeline,bad)).fetchone()[0] == 1
        decision = group_eligibility.pair_decision(conn,pipeline,account,bad)
        assert decision["eligible"] is False


# --- 15. scheduler restart không tạo job trùng ------------------------------

def test_the_scheduler_does_not_create_the_same_job_twice(client):
    pipeline = make_pipeline(client, '@el15')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/10019/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_PRIORITY)
        conn.execute("""INSERT INTO media_assets
            (pipeline_id,uploader_id,storage_key,thumbnail_key,original_name,mime_type,width,height,
             size_bytes,sha256,signature,analysis_status,safety_status,usable,description,tags_json,
             created_at,updated_at,owner_user_id)
            VALUES(?,1,?,'t/a.jpg','a.jpg','image/jpeg',600,600,1000,?,'sig','done','pass',1,
                   'một sản phẩm đẹp','[]',?,?,NULL)""",
                     (pipeline, f"{pipeline}/a.jpg", "a" * 64, m.iso(), m.iso()))
    enable_auto_post(client, pipeline, group_ids=[group])
    first = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule').json()
    second = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule').json()
    assert len(first['created']) == 1
    assert len(second['created']) == 0
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
    assert count == 1


# --- cô lập dữ liệu ---------------------------------------------------------

def test_a_group_of_another_workspace_is_out_of_reach(client):
    mine = make_pipeline(client, '@el_iso_mine')
    theirs = make_pipeline(client, '@el_iso_theirs')
    their_account = make_account(client, theirs, 'Facebook')
    their_group = make_group(client, theirs, their_account, 'Group',
                             'https://www.facebook.com/groups/10020/')
    my_account = make_account(client, mine, 'Facebook')
    with m.db() as conn:
        result = decision(conn, mine, my_account, their_group)
    assert result['eligible'] is False
    assert 'workspace' in result['reason'].lower()


def test_a_collaborator_keeps_the_groups_it_selected(client):
    """Danh sách group của CTV không bị xoá khi group chưa đủ điều kiện."""
    pipeline = make_pipeline(client, '@el_keep')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/10021/')
    with m.db() as conn:
        store_result(conn, pipeline, account, group, STATUS_NOT_MEMBER,
                     check_status="not_member", is_member=False, can_post=None)
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_keep', 'password': CTV_PASSWORD,
                                          'pipeline_id': pipeline})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_keep', 'password': CTV_PASSWORD,
                                              'pipeline_key': key})
    ctv.csrf = login.json()['csrf_token']
    saved = ctv.put(f'/api/pipelines/{pipeline}/auto-post/my-config', json={
        'account_scope': 'selected', 'selected_group_ids': [group], 'topic': 'x',
        'enabled': True})
    assert saved.status_code == 200, saved.text
    again = ctv.get(f'/api/pipelines/{pipeline}/auto-post/my-config').json()
    assert group in again['settings']['selected_group_ids']
    ctv.close()
