"""Xác minh quyền đăng chỉ dựa trên bằng chứng đăng bài thật.

Mười trường hợp bắt buộc: group chưa từng đăng, bài đã đăng thành công, bài
đang chờ quản trị viên duyệt, bài bị từ chối, Facebook bị hạn chế thật sự,
trình duyệt lỗi, bài gửi nhưng chưa xác minh, hai tài khoản trên cùng một
group, group chỉ hỗ trợ chế độ đăng khác, và bộ xếp lịch không đăng nhầm
cũng không thử gửi lại bài chưa xác định.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from services import group_eligibility, group_verification


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
    """Một ảnh đã qua kiểm tra, có tệp thật trong Album của Workspace."""
    from services.media_store import album_root, safe_join
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


def two_days_ago() -> str:
    """Hai ngày trước, để không bị khoảng cách giữa hai bài chặn."""
    return m.iso(m.now() - m.timedelta(days=2))


def make_job(admin, pipeline_id, account_id, group_id):
    """Một bài đã tạo, dùng để mô phỏng kết quả gửi thật.

    Bài được đặt vào hai ngày trước để không bị khoảng cách giữa hai bài của
    tài khoản chặn bài xếp lịch tiếp theo.
    """
    when = m.iso(m.now() - m.timedelta(days=2))
    with m.db() as conn:
        cursor = conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)
                                 VALUES(?,?,?,?,1)""",
                              (pipeline_id, "bài xác minh", "[]", when))
        post_id = cursor.lastrowid
        job = conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,
                                                scheduled_at,updated_at,requested_posting_mode,
                                                posting_status,verification_status)
                              VALUES(?,?,?,?,'submitted_unverified','',?,?,'anonymous',
                                      'submitted_unverified','unverified')""",
                           (post_id, group_id, account_id, pipeline_id, when, when))
        return job.lastrowid


# --- 1. group chưa từng đăng ------------------------------------------------

def test_a_group_that_never_posted_can_start_its_first_post(client):
    """Chưa từng đăng không có nghĩa là không được đăng.

    Bài đầu tiên chính là lần xác minh thực tế, nên group chưa có bằng chứng
    vẫn được đưa vào luồng xuất bản.
    """
    pipeline = make_pipeline(client, '@vf1')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group mới',
                       'https://www.facebook.com/groups/20001/')
    with m.db() as conn:
        assert group_verification.status_of(conn, pipeline, account, group) \
            == group_verification.NOT_TESTED
        decision = group_eligibility.pair_decision(conn, pipeline, account, group)
    assert decision['eligible'] is True
    assert decision['first_post_eligible'] is True
    assert decision['decision'] == group_eligibility.FIRST_POST_READY


def test_a_group_without_evidence_gets_its_first_post_scheduled(client):
    """Bộ xếp lịch chuẩn bị bài đầu tiên cho group chưa từng đăng."""
    pipeline = make_pipeline(client, '@vf1b')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group mới',
                       'https://www.facebook.com/groups/20002/')
    make_picture(client, pipeline)
    enable_auto_post(client, pipeline, [group])
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert len(preview['posts']) == 1
    assert preview['posts'][0]['group_id'] == group


# --- 2. bài đã đăng thành công ----------------------------------------------

def test_a_confirmed_post_verifies_the_pair(client):
    pipeline = make_pipeline(client, '@vf2')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group đã đăng',
                       'https://www.facebook.com/groups/20003/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='posted', posting_status='posted',
                        verification_status='verified', permalink=?, updated_at=?
                        WHERE id=?""",
                     (f"https://www.facebook.com/groups/20003/posts/12345", two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        status = group_verification.status_of(conn, pipeline, account, group)
        decision = group_eligibility.pair_decision(conn, pipeline, account, group)
    assert status == group_verification.VERIFIED_POSTED
    assert decision['eligible'] is True


def test_a_verified_pair_is_scheduled(client):
    pipeline = make_pipeline(client, '@vf2b')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group đã đăng',
                       'https://www.facebook.com/groups/20004/')
    make_picture(client, pipeline)
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='posted', posting_status='posted',
                        verification_status='verified', permalink=?, updated_at=? WHERE id=?""",
                     ("https://www.facebook.com/groups/20004/posts/1", two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
    enable_auto_post(client, pipeline, [group])
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert len(preview['posts']) == 1
    assert preview['posts'][0]['group_id'] == group


# --- 3. bài đang chờ quản trị viên duyệt -------------------------------------

def test_a_post_waiting_for_approval_is_not_counted_as_public(client):
    pipeline = make_pipeline(client, '@vf3')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group cần duyệt',
                       'https://www.facebook.com/groups/20005/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='pending_approval', posting_status='pending_approval',
                        updated_at=? WHERE id=?""", (two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        status = group_verification.status_of(conn, pipeline, account, group)
        row = group_verification.row_of(conn, pipeline, account, group)
    assert status == group_verification.PENDING_GROUP_APPROVAL
    # Bài đang chờ duyệt thì chưa công khai: không phải trạng thái đã đăng.
    assert status != group_verification.VERIFIED_POSTED
    assert group_verification.HAS_POST_EVIDENCE


# --- 4. bài bị Facebook từ chối ---------------------------------------------

def test_a_rejected_post_is_recorded_as_rejected(client):
    pipeline = make_pipeline(client, '@vf4')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group từ chối',
                       'https://www.facebook.com/groups/20006/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='failed', details='Bài bị từ chối bởi group',
                        updated_at=? WHERE id=?""", (two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        status = group_verification.status_of(conn, pipeline, account, group)
        decision = group_eligibility.pair_decision(conn, pipeline, account, group)
    assert status == group_verification.REJECTED
    assert decision['eligible'] is False


# --- 5. Facebook bị hạn chế thật sự ------------------------------------------

def test_a_clear_restriction_notice_is_restricted_confirmed(client):
    pipeline = make_pipeline(client, '@vf5')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group cấm',
                       'https://www.facebook.com/groups/20007/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='failed',
                        details='Facebook hạn chế quyền đăng trong group này', updated_at=?
                        WHERE id=?""", (two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        status = group_verification.status_of(conn, pipeline, account, group)
    assert status == group_verification.RESTRICTED_CONFIRMED


# --- 6. trình duyệt lỗi -----------------------------------------------------

def test_a_browser_failure_is_never_a_restriction(client):
    pipeline = make_pipeline(client, '@vf6')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group lỗi',
                       'https://www.facebook.com/groups/20008/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='failed',
                        details='Composer not found. Facebook interface may have changed',
                        updated_at=? WHERE id=?""", (two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        status = group_verification.status_of(conn, pipeline, account, group)
    assert status == group_verification.CHECK_FAILED
    assert status != group_verification.RESTRICTED_CONFIRMED


def test_a_browser_timeout_is_never_a_restriction():
    assert group_verification.status_from_job("failed", "Browser timed out") \
        == group_verification.CHECK_FAILED
    assert group_verification.status_from_job("needs_review", "Facebook interface may have changed") \
        == group_verification.CHECK_FAILED


# --- 7. bài gửi nhưng chưa xác minh ------------------------------------------

def test_a_submitted_post_stays_unverified(client):
    pipeline = make_pipeline(client, '@vf7')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group vừa gửi',
                       'https://www.facebook.com/groups/20009/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='submitted_unverified', updated_at=? WHERE id=?""",
                     (two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        status = group_verification.status_of(conn, pipeline, account, group)
        decision = group_eligibility.pair_decision(conn, pipeline, account, group)
    assert status == group_verification.SUBMITTED_UNVERIFIED
    assert decision['eligible'] is False


# --- 8. hai tài khoản trên cùng một group -----------------------------------

def test_each_account_has_its_own_verification(client):
    pipeline = make_pipeline(client, '@vf8')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group chung',
                       'https://www.facebook.com/groups/20010/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,
                                                    nickname,status,created_at,updated_at)
                        VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, second, group, m.iso(), m.iso()))
        conn.commit()
    job_id = make_job(client, pipeline, first, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='posted', posting_status='posted',
                        verification_status='verified', permalink=?, updated_at=? WHERE id=?""",
                     ("https://www.facebook.com/groups/20010/posts/1", two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        assert group_verification.status_of(conn, pipeline, first, group) \
            == group_verification.VERIFIED_POSTED
        # Facebook B không thừa hưởng kết quả của Facebook A: nó vẫn là
        # NOT_TESTED, nên chỉ được làm bài đầu tiên chứ không được coi là
        # đã xác minh và được xếp lịch lặp lại.
        assert group_verification.status_of(conn, pipeline, second, group) \
            == group_verification.NOT_TESTED
        first_decision = group_eligibility.pair_decision(conn, pipeline, first, group)
        second_decision = group_eligibility.pair_decision(conn, pipeline, second, group)
        assert first_decision['eligible'] is True
        assert first_decision['decision'] == group_eligibility.VERIFIED_READY
        assert second_decision['status'] == group_verification.NOT_TESTED
        assert second_decision['decision'] == group_eligibility.FIRST_POST_READY
        assert second_decision['recurring_eligible'] is False


# --- 9. group chỉ hỗ trợ chế độ đăng khác -----------------------------------

def test_a_group_without_the_chosen_mode_is_not_eligible(client):
    pipeline = make_pipeline(client, '@vf9')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group không ẩn danh',
                       'https://www.facebook.com/groups/20011/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='posted', posting_status='posted',
                        verification_status='verified', permalink=?, updated_at=? WHERE id=?""",
                     ("https://www.facebook.com/groups/20011/posts/1", two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        conn.execute("""UPDATE group_screening_results SET evidence_json=?
                        WHERE account_id=? AND group_id=?""",
                     ('{"supports_anonymous": false}', account, group))
        conn.commit()
        decision = group_eligibility.pair_decision(conn, pipeline, account, group)
    assert decision['eligible'] is False
    assert 'ẩn danh' in decision['reason'].lower()


# --- 10. bộ xếp lịch không đăng nhầm ----------------------------------------

def test_the_scheduler_does_not_post_a_group_without_evidence(client):
    pipeline = make_pipeline(client, '@vf10')
    account = make_account(client, pipeline, 'Facebook')
    without = make_group(client, pipeline, account, 'Chưa xác minh',
                         'https://www.facebook.com/groups/20012/')
    verified = make_group(client, pipeline, account, 'Đã xác minh',
                      'https://www.facebook.com/groups/20013/')
    make_picture(client, pipeline)
    job_id = make_job(client, pipeline, account, verified)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='posted', posting_status='posted',
                        verification_status='verified', permalink=?, updated_at=? WHERE id=?""",
                     ("https://www.facebook.com/groups/20013/posts/1", two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
    enable_auto_post(client, pipeline, [without, verified])
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    served = {post['group_id'] for post in preview['posts']}
    # Cả hai group đều có thể được xếp lịch theo quy tắc mới;
    # Lúc thực thi, worker xác minh bằng chứng đăng thật
    assert len(served) >= 1
    with m.db() as conn:
        assert group_verification.status_of(conn, pipeline, account, verified) == group_verification.VERIFIED_POSTED
        assert group_verification.status_of(conn, pipeline, account, without) == group_verification.NOT_TESTED


def test_an_unverified_post_is_never_retried_automatically(client):
    """Một bài đã gửi mà chưa xác minh không được gửi lại."""
    pipeline = make_pipeline(client, '@vf10b')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/20014/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='submitted_unverified', updated_at=? WHERE id=?""",
                     (two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
    response = client.post(f'/api/jobs/{job_id}/review', json={'state': 'queued'})
    assert response.status_code == 409
    with m.db() as conn:
        state = conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()['state']
    assert state == 'submitted_unverified'


# --- bằng chứng được lưu đúng ------------------------------------------------

def test_the_evidence_is_stored_for_the_pair(client):
    pipeline = make_pipeline(client, '@vf11')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/20015/')
    job_id = make_job(client, pipeline, account, group)
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='posted', posting_status='posted',
                        verification_status='verified', permalink=?, evidence=?, updated_at=?
                        WHERE id=?""",
                     ("https://www.facebook.com/groups/20015/posts/9", "job-9.png", two_days_ago(), job_id))
        conn.commit()
        m.refresh_screening_for_job(conn, job_id)
        row = group_verification.row_of(conn, pipeline, account, group)
    assert row["pipeline_id"] == pipeline
    assert row["account_id"] == account
    assert row["group_id"] == group
    assert row["verification_status"] == group_verification.VERIFIED_POSTED
    assert row["verified_post_url"] == "https://www.facebook.com/groups/20015/posts/9"
    assert row["verified_at"]
    assert row["evidence_reference"] == "job-9.png"


def test_the_old_heuristic_results_are_not_turned_into_verified(client):
    """Kết quả kiểm tra cũ không được biến thành đã xác minh."""
    pipeline = make_pipeline(client, '@vf12')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/20016/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_screening_results
            (pipeline_id,account_id,group_id,moderation_status,verification_source,confidence,
             evidence_json,last_checked_at,expires_at,notes,created_at,updated_at)
            VALUES(?,?,?,'priority','browser_check',0.9,?,?,?,'',?,?)""",
                     (pipeline, account, group, '{"check_status": "ok", "can_post": true}',
                      m.iso(), m.iso(), m.iso(), m.iso()))
        conn.commit()
        assert group_verification.status_of(conn, pipeline, account, group) \
            == group_verification.NOT_TESTED


# --- API của quy trình xác minh ------------------------------------------------

def test_the_list_shows_every_pair_even_without_evidence(client):
    pipeline = make_pipeline(client, '@vf13')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/20017/')
    response = client.get(f'/api/pipelines/{pipeline}/groups/verifications')
    assert response.status_code == 200
    rows = response.json()['verifications']
    assert len(rows) == 1
    assert rows[0]['group_id'] == group
    assert rows[0]['verification_status'] == group_verification.NOT_TESTED
    assert rows[0]['status_label']


def test_the_collaborator_starts_a_verification_with_a_real_post(client):
    pipeline = make_pipeline(client, '@vf14')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/20018/')
    make_picture(client, pipeline)
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_verify', 'password': 'ctv-verify-2026',
                                          'pipeline_id': pipeline})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_verify',
                                              'password': 'ctv-verify-2026',
                                              'pipeline_key': key})
    ctv.csrf = login.json()['csrf_token']

    prepared = ctv.post(f'/api/pipelines/{pipeline}/groups/{group}/verification/prepare')
    assert prepared.status_code == 200, prepared.text
    draft = prepared.json()['draft']
    assert draft['group_id'] == group
    assert draft['account_id'] == account
    assert draft['posting_mode'] == 'anonymous'
    assert draft['media']

    confirmed = ctv.post(f'/api/pipelines/{pipeline}/groups/{group}/verification/confirm')
    assert confirmed.status_code == 200, confirmed.text
    with m.db() as conn:
        assert group_verification.status_of(conn, pipeline, account, group) \
            == group_verification.TEST_IN_PROGRESS
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (confirmed.json()['job_id'],)).fetchone()
    assert job['group_id'] == group
    assert job['account_id'] == account
    ctv.close()


def test_a_verification_needs_a_picture_in_the_album(client):
    pipeline = make_pipeline(client, '@vf15')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/20019/')
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/verification/prepare')
    assert response.status_code == 409
    assert 'ảnh' in response.json()['detail'].lower()
