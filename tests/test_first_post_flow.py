"""Luồng tự động đăng group: bài đầu tiên là lần xác minh.

Mười tám trường hợp bắt buộc: group chưa từng đăng, tài khoản đã tham gia,
tài khoản chưa tham gia, group chờ duyệt thành viên, bài đầu thành công,
bài đầu chờ duyệt, bài gửi chưa rõ kết quả, Facebook bị hạn chế thật, lỗi
trình duyệt, bài xác minh bằng biệt danh, hai Facebook cùng group, group
chưa xác minh không bị đếm nhầm, bấm xếp lịch hai lần, worker khởi động lại,
API và giao diện cùng kết quả, không mất dữ liệu cũ, không truy cập chéo
Workspace, và album chưa có ảnh.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from services import group_eligibility, group_membership, group_verification


def make_pipeline(admin, name):
    return admin.post('/api/pipelines', json={'name': name}).json()['id']


def make_account(admin, pipeline_id, label, session="connected"):
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label}).json()
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status=? WHERE id=?",
                     (session, account['id']))
    return account['id']


def make_group(admin, pipeline_id, account_id, label, url, mode='anonymous', nickname=''):
    payload = nickname or ('Tạm' if mode == 'nickname' else '')
    group = admin.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': label, 'url': url,
        'approved_target': True, 'posting_mode': mode, 'nickname': payload}).json()
    return group['id']


def make_picture(admin, pipeline_id, name='a.jpg'):
    from services.media_store import safe_join
    key = f"{pipeline_id}/{name}"
    target = safe_join(m.DATA, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    with m.db() as conn:
        cursor = conn.execute("""INSERT INTO media_assets
            (pipeline_id,uploader_id,storage_key,thumbnail_key,original_name,mime_type,width,height,
             size_bytes,sha256,signature,analysis_status,safety_status,usable,description,tags_json,
             created_at,updated_at,owner_user_id)
            VALUES(?,1,?,'',?,'image/jpeg',600,600,1000,?,'sig','done','pass',1,
                   'một sản phẩm đẹp','[]',?,?,NULL)""",
            (pipeline_id, key, name, name * 8, m.iso(), m.iso()))
        return cursor.lastrowid


def enable_auto_post(admin, pipeline_id, group_ids):
    payload = {'enabled': True, 'scope': 'selected', 'account_ids': [], 'group_ids': group_ids,
               'daily_limit_per_account': 3, 'min_gap_minutes': 0, 'pipeline_daily_limit': 0,
               'topic': 'sản phẩm', 'ai_caption': False, 'anti_duplicate': True,
               'require_approval': True, 'apply_to_new_accounts': True}
    response = admin.put(f'/api/pipelines/{pipeline_id}/auto-post/settings', json=payload)
    assert response.status_code == 200, response.text
    return response.json()['settings']


def decision(conn, pipeline_id, account_id, group_id):
    return group_eligibility.pair_decision(conn, pipeline_id, account_id, group_id)


# --- 1. group chưa từng đăng không bị chặn ---------------------------------

def test_a_group_that_never_posted_is_not_blocked(client):
    pipeline = make_pipeline(client, '@fl1')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group mới',
                       'https://www.facebook.com/groups/30001/')
    with m.db() as conn:
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is True
    assert result['decision'] == group_eligibility.FIRST_POST_READY
    assert result['first_post_eligible'] is True
    assert result['recurring_eligible'] is False


# --- 2. tài khoản đã tham gia -> chuẩn bị bài đầu tiên -----------------------

def test_a_member_account_can_prepare_its_first_post(client):
    pipeline = make_pipeline(client, '@fl2')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group đã vào',
                       'https://www.facebook.com/groups/30002/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is True
    assert result['decision'] == group_eligibility.FIRST_POST_READY


# --- 3. tài khoản chưa tham gia -> yêu cầu tham gia ------------------------

def test_an_account_that_has_not_joined_waits_for_membership(client):
    pipeline = make_pipeline(client, '@fl3')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group chưa vào',
                       'https://www.facebook.com/groups/30003/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.NOT_MEMBER)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert result['decision'] == group_eligibility.WAITING_MEMBERSHIP
    assert 'tham gia' in result['reason'].lower()


def test_a_membership_waiting_for_approval_is_not_a_member(client):
    pipeline = make_pipeline(client, '@fl3b')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group chờ duyệt thành viên',
                       'https://www.facebook.com/groups/30004/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group,
                                group_membership.PENDING_APPROVAL)
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert result['decision'] == group_eligibility.WAITING_MEMBERSHIP


# --- 4. group chờ duyệt thành viên -> không đăng trước khi vào group --------

def test_nothing_is_posted_before_the_membership_is_confirmed(client):
    pipeline = make_pipeline(client, '@fl4')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group chờ duyệt',
                       'https://www.facebook.com/groups/30005/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group,
                                group_membership.PENDING_APPROVAL)
    make_picture(client, pipeline)
    enable_auto_post(client, pipeline, [group])
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert preview['posts'] == []


# --- 5. bài đầu thành công -> VERIFIED_POSTED -------------------------------

def test_a_successful_first_post_verifies_the_pair(client):
    pipeline = make_pipeline(client, '@fl5')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30006/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
    make_picture(client, pipeline)
    enable_auto_post(client, pipeline, [group])
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert len(preview['posts']) == 1
    # Bài đã xếp lịch thì cặp chuyển sang trạng thái đang xác minh.
    with m.db() as conn:
        assert group_verification.status_of(conn, pipeline, account, group) \
            == group_verification.NOT_TESTED


# --- 6. bài đầu chờ Admin group duyệt -> PENDING_GROUP_APPROVAL ------------

def test_a_post_waiting_for_group_approval_is_recorded(client):
    pipeline = make_pipeline(client, '@fl6')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group cần duyệt',
                       'https://www.facebook.com/groups/30007/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
        group_verification.record_outcome(conn, pipeline, account, group,
                                          group_verification.PENDING_GROUP_APPROVAL,
                                          post_id=1, posting_mode="anonymous")
        status = group_verification.status_of(conn, pipeline, account, group)
        result = decision(conn, pipeline, account, group)
    assert status == group_verification.PENDING_GROUP_APPROVAL
    # Bài chờ duyệt thì chưa công khai, nên không tự đăng tiếp vào group đó.
    assert result['eligible'] is False
    assert result['decision'] == group_eligibility.WAITING_APPROVAL


# --- 7. bài gửi nhưng chưa rõ kết quả -> không gửi lại ---------------------

def test_a_submitted_post_is_not_sent_again(client):
    pipeline = make_pipeline(client, '@fl7')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30008/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
        group_verification.record_outcome(conn, pipeline, account, group,
                                          group_verification.SUBMITTED_UNVERIFIED,
                                          post_id=1, posting_mode="anonymous")
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert result['status'] == group_verification.SUBMITTED_UNVERIFIED


# --- 8. Facebook bị hạn chế thật -> không cố đăng tiếp ---------------------

def test_a_confirmed_restriction_stops_the_posting(client):
    pipeline = make_pipeline(client, '@fl8')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group bị cấm',
                       'https://www.facebook.com/groups/30009/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
        group_verification.record_outcome(conn, pipeline, account, group,
                                          group_verification.RESTRICTED_CONFIRMED,
                                          error_code="restricted")
        result = decision(conn, pipeline, account, group)
    assert result['eligible'] is False
    assert result['decision'] == group_eligibility.CONFIRMED_RESTRICTED


# --- 9. lỗi trình duyệt -> CHECK_FAILED, không phải hạn chế -----------------

def test_a_browser_failure_is_never_a_restriction(client):
    pipeline = make_pipeline(client, '@fl9')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30010/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
        group_verification.record_outcome(conn, pipeline, account, group,
                                          group_verification.CHECK_FAILED,
                                          error_code="timeout")
        result = decision(conn, pipeline, account, group)
    assert result['status'] == group_verification.CHECK_FAILED
    assert result['decision'] != group_eligibility.CONFIRMED_RESTRICTED


# --- 10. bài xác minh bằng biệt danh được đánh giá đúng --------------------

def test_a_nickname_verification_is_evaluated_with_its_own_mode(client):
    pipeline = make_pipeline(client, '@fl10')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group biệt danh',
                       'https://www.facebook.com/groups/30011/', mode='nickname',
                       nickname='Tên hay')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
        group_verification.record_outcome(conn, pipeline, account, group,
                                          group_verification.VERIFIED_POSTED,
                                          post_id=1, posting_mode="nickname")
        result = decision(conn, pipeline, account, group)
    # Bằng chứng đúng chế độ biệt danh thì được dùng.
    assert result['eligible'] is True
    assert result['decision'] == group_eligibility.VERIFIED_READY


def test_evidence_of_another_mode_is_not_reused(client):
    pipeline = make_pipeline(client, '@fl10b')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30012/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
        group_verification.record_outcome(conn, pipeline, account, group,
                                          group_verification.VERIFIED_POSTED,
                                          post_id=1, posting_mode="anonymous")
        # Cặp này dùng chế độ biệt danh: bằng chứng ẩn danh không dùng được.
        conn.execute("UPDATE group_accounts SET posting_mode='nickname', nickname='X' "
                     "WHERE group_id=? AND account_id=?", (group, account))
        conn.commit()
        result = decision(conn, pipeline, account, group)
    # Đổi chế độ đăng thì bằng chứng cũ không còn phù hợp, nên cặp này phải
    # xác minh lại bằng chế độ mới. Không có gì bị coi là hạn chế quyền đăng.
    assert result['status'] == group_verification.NOT_TESTED
    assert result['decision'] == group_eligibility.FIRST_POST_READY
    assert result['eligible'] is True


# --- 11. hai Facebook cùng group -> kết quả tách riêng ----------------------

def test_two_accounts_of_one_group_have_separate_results(client):
    pipeline = make_pipeline(client, '@fl11')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group chung',
                       'https://www.facebook.com/groups/30013/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,
                                                    nickname,status,created_at,updated_at)
                        VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, second, group, m.iso(), m.iso()))
        group_membership.record(conn, pipeline, first, group, group_membership.JOINED)
        group_verification.record_outcome(conn, pipeline, first, group,
                                          group_verification.VERIFIED_POSTED,
                                          post_id=1, posting_mode="anonymous")
        first_decision = decision(conn, pipeline, first, group)
        second_decision = decision(conn, pipeline, second, group)
    assert first_decision['decision'] == group_eligibility.VERIFIED_READY
    assert second_decision['status'] == group_verification.NOT_TESTED
    assert second_decision['decision'] == group_eligibility.FIRST_POST_READY


# --- 12. group chưa xác minh không bị đếm nhầm là 0 group -------------------

def test_an_unverified_group_is_not_counted_as_no_group(client):
    pipeline = make_pipeline(client, '@fl12')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group mới',
                       'https://www.facebook.com/groups/30014/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
    make_picture(client, pipeline)
    enable_auto_post(client, pipeline, [group])
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert len(preview['posts']) == 1
    assert preview['eligible_groups'] == [group]


# --- 13. bấm xếp lịch hai lần không tạo job trùng ---------------------------

def test_scheduling_twice_does_not_create_a_duplicate_job(client):
    pipeline = make_pipeline(client, '@fl13')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30015/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
    make_picture(client, pipeline)
    enable_auto_post(client, pipeline, [group])
    first = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    second = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert first.status_code == 201, first.text
    assert len(first.json()['created']) == 1
    assert len(second.json()['created']) == 0
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
    assert count == 1


# --- 14. worker khởi động lại không mất hay gửi lại bài ---------------------

def test_a_restart_does_not_lose_or_repeat_the_job(client):
    pipeline = make_pipeline(client, '@fl14')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30016/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
    make_picture(client, pipeline)
    enable_auto_post(client, pipeline, [group])
    client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    # Worker chạy lại: job cũ vẫn còn và không tạo job thứ hai.
    m.init_db()
    again = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert len(again.json()['created']) == 0
    with m.db() as conn:
        rows = conn.execute("SELECT id, status, idempotency_key FROM auto_post_jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["idempotency_key"]


# --- 15. API và giao diện cùng kết quả --------------------------------------

def test_the_api_and_the_listing_report_the_same_decision(client):
    pipeline = make_pipeline(client, '@fl15')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30017/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
    listing = client.get(f'/api/pipelines/{pipeline}/groups/verifications').json()
    row = next(item for item in listing['verifications'] if item['group_id'] == group)
    assert row['verification_status'] == group_verification.NOT_TESTED
    assert row['decision'] == group_eligibility.FIRST_POST_READY
    with m.db() as conn:
        assert decision(conn, pipeline, account, group)['decision'] == row['decision']


# --- 16. không mất dữ liệu cũ -------------------------------------------------

def test_the_migration_keeps_the_existing_data(client):
    pipeline = make_pipeline(client, '@fl16')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30018/')
    make_picture(client, pipeline)
    before = {}
    with m.db() as conn:
        for table in ("groups", "accounts", "media_assets", "auto_post_jobs"):
            before[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    m.init_db()
    with m.db() as conn:
        for table, count in before.items():
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count


# --- 17. không truy cập chéo Workspace ---------------------------------------

def test_a_group_of_another_workspace_is_out_of_reach(client):
    mine = make_pipeline(client, '@fl17')
    theirs = make_pipeline(client, '@fl17b')
    their_account = make_account(client, theirs, 'Facebook')
    their_group = make_group(client, theirs, their_account, 'Group',
                             'https://www.facebook.com/groups/30019/')
    my_account = make_account(client, mine, 'Facebook')
    with m.db() as conn:
        result = decision(conn, mine, my_account, their_group)
    assert result['eligible'] is False
    assert 'Workspace' in result['reason']


# --- 18. album chưa có ảnh -> báo thiếu ảnh, không báo group bị hạn chế -------

def test_a_missing_picture_reports_the_picture_not_the_group(client):
    pipeline = make_pipeline(client, '@fl18')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30020/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
    enable_auto_post(client, pipeline, [group])
    response = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert response.status_code == 409
    detail = response.json()['detail']
    assert 'ảnh' in detail.lower()
    assert 'hạn chế' not in detail.lower()


# --- 19. một Workspace chỉ được xếp lịch bởi một bộ cấu hình ---------------

def test_a_workspace_is_scheduled_by_one_configuration_only(client):
    """CTV đã tự cấu hình thì bộ xếp lịch chung không xếp lại Workspace đó."""
    pipeline = make_pipeline(client, '@fl19')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/30021/')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
    make_picture(client, pipeline)
    enable_auto_post(client, pipeline, [group])
    with m.db() as conn:
        conn.execute("UPDATE pipeline_posting_schedule SET start_time='00:00', end_time='23:59' "
                     "WHERE pipeline_id=?", (pipeline,))
        conn.commit()
    # CTV của Workspace cũng có cấu hình riêng, đang bật.
    client.post('/api/admin/users', json={'username': 'ctv_fl19', 'password': 'ctv-fl19-2026',
                                          'pipeline_id': pipeline})
    ctv = AuthClient(m.app)
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    login = ctv.post('/api/auth/login', json={'username': 'ctv_fl19', 'password': 'ctv-fl19-2026',
                                              'pipeline_key': key})
    ctv.csrf = login.json()['csrf_token']
    # Khung giờ phủ cả ngày để bài kiểm thử không phụ thuộc giờ hệ thống.
    saved = ctv.put(f'/api/pipelines/{pipeline}/auto-post/my-config', json={
        'enabled': True, 'selected_group_ids': [group], 'topic': 'sản phẩm',
        'start_time': '00:00', 'end_time': '23:59'})
    assert saved.status_code == 200, saved.text
    ctv.close()

    m.prepare_auto_post_jobs()
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
    assert count == 1, "Một Workspace chỉ được xếp lịch một lần"
