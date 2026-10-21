"""V11: mỗi CTV tự thiết lập tự động đăng group của riêng mình.

Bộ kiểm thử này đi đúng mười lăm kịch bản bắt buộc: quản trị tạo Workspace và
cấp thông tin đăng nhập, CTV đăng nhập lần đầu và thấy ngay một form đầy đủ,
lưu cấu hình riêng, hai CTV không lẫn cấu hình của nhau, quản trị xem và tạm
dừng được từng cấu hình, và các quy tắc an toàn giữa nhiều CTV trên cùng một
Facebook.
"""
from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m
from screening_fixtures import verify_pair
from scheduler import ctv_allocator

CTV_PASSWORD = "ctv-password-2026-strong"


# --- helpers -----------------------------------------------------------------

def inside_window():
    """Một thời điểm chắc chắn nằm trong khung giờ đăng, không phụ thuộc giờ hệ thống."""
    from scheduler import time_windows
    return time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=10, minute=0)


def make_pipeline(admin, name):
    created = admin.post('/api/pipelines', json={'name': name})
    assert created.status_code == 201, created.text
    return created.json()['id']


def make_account(admin, pipeline_id, label, connected=True):
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label}).json()
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status=? WHERE id=?",
                     ('connected' if connected else 'expired', account['id']))
    return account['id']


def make_group(admin, pipeline_id, account_id, label, url, screening='priority',
               approved=True, mode='anonymous', nickname=''):
    payload_nickname = nickname or ('Tạm' if mode == 'nickname' else '')
    group = admin.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': label, 'url': url,
        'approved_target': approved, 'posting_mode': mode, 'nickname': payload_nickname}).json()
    with m.db() as conn:
        conn.execute("UPDATE groups SET nickname=? WHERE id=?", (nickname, group['id']))
        verify_pair(conn, pipeline_id, account_id, group['id'], status=screening)
    return group['id']


def make_media(admin, pipeline_id, name, sha=None, signature=''):
    """One picture of the album that is usable and safe."""
    with m.db() as conn:
        cursor = conn.execute("""INSERT INTO media_assets
            (pipeline_id,uploader_id,storage_key,thumbnail_key,original_name,mime_type,width,height,
             size_bytes,sha256,signature,analysis_status,safety_status,usable,description,tags_json,
             created_at,updated_at,owner_user_id)
            VALUES(?,?,?,?,?,'image/jpeg',600,600,1000,?,?,'done','pass',1,'ảnh sản phẩm đẹp','[]',?,?,NULL)""",
            (pipeline_id, 1, f"{pipeline_id}/{name}", f"{pipeline_id}/thumbs/{name}", name,
             sha or (name * 8)[:64], signature, m.iso(), m.iso()))
        return cursor.lastrowid


def new_pipeline_key(admin, pipeline_id):
    return admin.post(f'/api/admin/pipelines/{pipeline_id}/key').json()['key']


def make_ctv(admin, pipeline_id, username):
    created = admin.post('/api/admin/users', json={
        'username': username, 'password': CTV_PASSWORD, 'pipeline_id': pipeline_id})
    assert created.status_code == 201, created.text
    return created.json()['id']


def login_ctv(username, key, password=CTV_PASSWORD):
    """A separate client (own cookie jar) signed in as a collaborator."""
    ctv = AuthClient(m.app)
    response = ctv.post('/api/auth/login', json={
        'username': username, 'password': password, 'pipeline_key': key})
    ctv.csrf = response.json().get('csrf_token', '') if response.status_code == 200 else ''
    return ctv, response


def my_config(ctv, pipeline_id):
    return ctv.get(f'/api/pipelines/{pipeline_id}/auto-post/my-config')


def save_config(ctv, pipeline_id, **overrides):
    payload = {
        'enabled': True,
        'account_scope': 'all',
        'selected_account_ids': [],
        'selected_group_ids': [],
        'topic': 'sản phẩm',
        'max_posts_per_account_daily': 2,
        'start_time': '08:00',
        'end_time': '20:00',
        'timezone': 'Asia/Ho_Chi_Minh',
        'schedule_rules': {'days_enabled': [1, 2, 3, 4, 5, 6, 0], 'min_gap_minutes': 0,
                           'group_daily_limit': 0, 'image_selection_rule': 'rotate'},
        'ai_caption_enabled': False,
        'approval_required': True,
        'anti_duplicate': True,
        'apply_to_new_accounts': True,
    }
    payload.update(overrides)
    return ctv.put(f'/api/pipelines/{pipeline_id}/auto-post/my-config', json=payload)


# --- 1. admin tạo Pipeline ---------------------------------------------------

def test_admin_creates_pipeline(client):
    created = client.post('/api/pipelines', json={'name': '@ctv_workspace'})
    assert created.status_code == 201, created.text
    assert created.json()['id'] > 0


# --- 2. admin tạo CTV + Password + Pipeline Key ------------------------------

def test_admin_creates_collaborator_with_password_and_key(client):
    pipeline = make_pipeline(client, '@ctv_creds')
    user_id = make_ctv(client, pipeline, 'ctv_creds')
    key = new_pipeline_key(client, pipeline)
    assert key.startswith('FPM-')
    with m.db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    assert row["password_hash"].startswith('$argon2id$')
    assert CTV_PASSWORD not in row["password_hash"]


# --- 3. CTV đăng nhập lần đầu -------------------------------------------------

def test_collaborator_signs_in_with_three_factors(client):
    pipeline = make_pipeline(client, '@ctv_login')
    make_ctv(client, pipeline, 'ctv_login')
    key = new_pipeline_key(client, pipeline)
    ctv, response = login_ctv('ctv_login', key)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['role'] == 'ctv'
    assert body['pipeline_id'] == pipeline
    assert 'auto_post.self_configure' in body['permissions']
    ctv.close()


# --- 4. lần đầu mở Auto Posting thấy form đầy đủ ----------------------------

def test_first_time_sees_a_full_form_with_defaults(client):
    pipeline = make_pipeline(client, '@ctv_first')
    make_ctv(client, pipeline, 'ctv_first')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_first', key)

    response = my_config(ctv, pipeline)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['configured'] is False
    settings = body['settings']
    # Trạng thái ban đầu là tắt.
    assert settings['enabled'] is False
    # Form có đủ giá trị mặc định để CTV tự thiết lập.
    assert settings['account_scope'] == 'all'
    assert settings['selected_account_ids'] == []
    assert settings['selected_group_ids'] == []
    assert settings['max_posts_per_account_daily'] >= 1
    assert settings['start_time'] == '08:00'
    assert settings['end_time'] == '20:00'
    assert settings['schedule_rules']['days_enabled']
    assert settings['schedule_rules']['image_selection_rule'] == 'rotate'
    ctv.close()


def test_a_collaborator_without_configuration_has_no_row(client):
    pipeline = make_pipeline(client, '@ctv_norow')
    user_id = make_ctv(client, pipeline, 'ctv_norow')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_norow', key)
    my_config(ctv, pipeline)
    with m.db() as conn:
        row = conn.execute("""SELECT * FROM ctv_auto_post_settings
                              WHERE pipeline_id=? AND ctv_user_id=?""",
                           (pipeline, user_id)).fetchone()
    assert row is None, "Chưa lưu thì không được tạo cấu hình giả"
    ctv.close()


# --- 5. CTV lưu cấu hình ------------------------------------------------------

def test_collaborator_saves_own_configuration(client):
    pipeline = make_pipeline(client, '@ctv_save')
    make_ctv(client, pipeline, 'ctv_save')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_save', key)

    response = save_config(ctv, pipeline, topic='chủ đề của tôi',
                            max_posts_per_account_daily=3)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['configured'] is True
    assert body['settings']['topic'] == 'chủ đề của tôi'
    assert body['settings']['max_posts_per_account_daily'] == 3
    ctv.close()


# --- 6. tải lại trang vẫn thấy dữ liệu ---------------------------------------

def test_configuration_survives_a_reload(client):
    pipeline = make_pipeline(client, '@ctv_reload')
    make_ctv(client, pipeline, 'ctv_reload')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_reload', key)
    save_config(ctv, pipeline, topic='giữ nguyên sau tải lại')
    ctv.close()

    again, response = login_ctv('ctv_reload', key)
    assert response.status_code == 200
    body = my_config(again, pipeline).json()
    assert body['configured'] is True
    assert body['settings']['topic'] == 'giữ nguyên sau tải lại'
    again.close()


# --- 7. đăng xuất rồi đăng nhập lại vẫn thấy dữ liệu ---------------------------

def test_configuration_survives_logout_and_login(client):
    pipeline = make_pipeline(client, '@ctv_logout')
    make_ctv(client, pipeline, 'ctv_logout')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_logout', key)
    save_config(ctv, pipeline, topic='sau khi đăng xuất')
    assert ctv.post('/api/auth/logout').status_code == 200
    ctv.close()

    again, response = login_ctv('ctv_logout', key)
    assert response.status_code == 200
    assert my_config(again, pipeline).json()['settings']['topic'] == 'sau khi đăng xuất'
    again.close()


# --- 8. CTV thứ hai có cấu hình riêng ----------------------------------------

def test_a_workspace_has_one_configuration(client):
    """1 Pipeline = 1 CTV = 1 Workspace: Workspace chỉ có một cấu hình.

    Cấu hình tự động đăng thuộc về Workspace chứ không thuộc về từng tài
    khoản, nên người làm việc trong Workspace đó dùng chung một cấu hình.
    """
    pipeline = make_pipeline(client, '@ctv_two')
    make_ctv(client, pipeline, 'ctv_first_of_two')
    make_ctv(client, pipeline, 'ctv_second_of_two')
    key = new_pipeline_key(client, pipeline)

    one, _ = login_ctv('ctv_first_of_two', key)
    two, _ = login_ctv('ctv_second_of_two', key)
    save_config(one, pipeline, topic='chủ đề A', max_posts_per_account_daily=3)
    save_config(two, pipeline, topic='chủ đề B', max_posts_per_account_daily=2)

    config_one = my_config(one, pipeline).json()
    config_two = my_config(two, pipeline).json()
    assert config_one['settings']['topic'] == 'chủ đề B'
    assert config_two['settings']['topic'] == 'chủ đề B'
    assert config_one['settings']['max_posts_per_account_daily'] == 2
    assert config_two['settings']['max_posts_per_account_daily'] == 2
    one.close()
    two.close()


def test_a_workspace_without_a_configuration_starts_from_the_defaults(client):
    """Chưa ai lưu thì giao diện mở ra vẫn là form mặc định."""
    pipeline = make_pipeline(client, '@ctv_default')
    make_ctv(client, pipeline, 'ctv_with_config')
    make_ctv(client, pipeline, 'ctv_without_config')
    key = new_pipeline_key(client, pipeline)

    one, _ = login_ctv('ctv_with_config', key)
    two, _ = login_ctv('ctv_without_config', key)
    body = my_config(two, pipeline).json()
    assert body['configured'] is False
    assert body['settings']['topic'] == ''
    assert body['settings']['enabled'] is False
    # Lưu lần đầu thì Workspace có cấu hình.
    save_config(one, pipeline, topic='cấu hình của Workspace')
    body = my_config(two, pipeline).json()
    assert body['configured'] is True
    assert body['settings']['topic'] == 'cấu hình của Workspace'
    one.close()
    two.close()


# --- 9. CTV A không sửa được cấu hình của CTV B -----------------------------

def test_collaborator_cannot_touch_another_configuration(client):
    pipeline = make_pipeline(client, '@ctv_tamper')
    make_ctv(client, pipeline, 'ctv_tamper_a')
    second = make_ctv(client, pipeline, 'ctv_tamper_b')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_tamper_a', key)
    two, _ = login_ctv('ctv_tamper_b', key)
    save_config(two, pipeline, topic='của B')

    assert one.put(f'/api/pipelines/{pipeline}/auto-post/ctv-configs/{second}',
                    json={'topic': 'đã bị sửa'}).status_code == 403
    assert one.get(f'/api/pipelines/{pipeline}/auto-post/ctv-configs/{second}').status_code == 403
    assert one.post(f'/api/pipelines/{pipeline}/auto-post/ctv-configs/{second}/pause').status_code == 403
    # Cấu hình của B không đổi.
    assert my_config(two, pipeline).json()['settings']['topic'] == 'của B'
    one.close()
    two.close()


def test_collaborator_cannot_send_another_owner_in_the_body(client):
    pipeline = make_pipeline(client, '@ctv_body')
    make_ctv(client, pipeline, 'ctv_body_a')
    second = make_ctv(client, pipeline, 'ctv_body_b')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_body_a', key)
    two, _ = login_ctv('ctv_body_b', key)
    save_config(two, pipeline, topic='của B')

    response = one.put(f'/api/pipelines/{pipeline}/auto-post/my-config',
                       json={'topic': 'đã bị sửa', 'ctv_user_id': second})
    assert response.status_code == 200
    # ctv_user_id trong body không dùng để nhắm cấu hình của người khác: cấu
    # hình thuộc về Workspace và người ghi là người đang đăng nhập.
    settings = my_config(one, pipeline).json()['settings']
    assert settings['topic'] == 'đã bị sửa'
    assert settings['ctv_user_id'] != second
    one.close()
    two.close()


# --- 10. admin xem và quản lý được từng cấu hình -----------------------------

def test_admin_sees_and_pauses_the_workspace_configuration(client):
    """Admin quản lý được cấu hình của Workspace, và tạm dừng được nó."""
    pipeline = make_pipeline(client, '@ctv_admin')
    make_ctv(client, pipeline, 'ctv_admin_a')
    second = make_ctv(client, pipeline, 'ctv_admin_b')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_admin_a', key)
    two, _ = login_ctv('ctv_admin_b', key)
    save_config(one, pipeline, topic='A')
    save_config(two, pipeline, topic='B')

    listing = client.get(f'/api/pipelines/{pipeline}/auto-post/ctv-configs')
    assert listing.status_code == 200
    rows = listing.json()['configs']
    assert len(rows) == 1
    assert rows[0]['topic'] == 'B'
    assert rows[0]['enabled'] is True

    assert client.get(f'/api/pipelines/{pipeline}/auto-post/ctv-configs/{second}').json()[
        'settings']['topic'] == 'B'
    paused = client.post(f'/api/pipelines/{pipeline}/auto-post/ctv-configs/{second}/pause')
    assert paused.status_code == 200
    assert paused.json()['settings']['enabled'] is False
    # Người trong Workspace nhìn thấy trạng thái bị tạm dừng.
    assert my_config(two, pipeline).json()['settings']['enabled'] is False
    one.close()
    two.close()


def test_admin_list_shows_collaborators_without_configuration(client):
    pipeline = make_pipeline(client, '@ctv_admin_list')
    make_ctv(client, pipeline, 'ctv_admin_pending')
    rows = client.get(f'/api/pipelines/{pipeline}/auto-post/ctv-configs').json()['configs']
    row = next(item for item in rows if item['username'] == 'ctv_admin_pending')
    assert row['configured'] is False
    assert row['enabled'] is False


# --- 11. hai CTV dùng chung Facebook không tạo job đồng thời ----------------

def test_two_collaborators_never_share_one_facebook_at_the_same_time(client):
    pipeline = make_pipeline(client, '@ctv_shared_fb')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/333333/')
    make_media(client, pipeline, 'a.jpg')
    make_media(client, pipeline, 'b.jpg')
    make_ctv(client, pipeline, 'ctv_shared_a')
    make_ctv(client, pipeline, 'ctv_shared_b')
    key = new_pipeline_key(client, pipeline)

    one, _ = login_ctv('ctv_shared_a', key)
    two, _ = login_ctv('ctv_shared_b', key)
    save_config(one, pipeline, topic='A', max_posts_per_account_daily=3)
    save_config(two, pipeline, topic='B', max_posts_per_account_daily=3)

    with m.db() as conn:
        outcome = ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
    assert len(outcome['created']) >= 1
    with m.db() as conn:
        rows = conn.execute("""SELECT account_id, created_by_ctv_id FROM auto_post_jobs
                                WHERE pipeline_id=?""", (pipeline,)).fetchall()
    # Facebook nào cũng chỉ do một CTV nhận job.
    owners = {}
    for row in rows:
        owners.setdefault(row['account_id'], set()).add(row['created_by_ctv_id'])
    for account_id, holder in owners.items():
        assert len(holder) == 1, f"Facebook {account_id} bị nhiều CTV cùng dùng"
    one.close()
    two.close()


def test_second_collaborator_is_blocked_from_a_busy_facebook(client):
    pipeline = make_pipeline(client, '@ctv_busy')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/444444/')
    make_media(client, pipeline, 'a.jpg')
    make_media(client, pipeline, 'b.jpg')
    first = make_ctv(client, pipeline, 'ctv_busy_a')
    make_ctv(client, pipeline, 'ctv_busy_b')
    key = new_pipeline_key(client, pipeline)

    with m.db() as conn:
        conn.execute("""INSERT INTO auto_post_jobs
            (pipeline_id,account_id,group_id,media_id,status,idempotency_key,created_at,updated_at)
            VALUES(?,?,?,?, 'approved', 'seed-key', ?, ?)""",
                     (pipeline, account, 1, 1, m.iso(), m.iso()))
        conn.execute("UPDATE auto_post_jobs SET created_by_ctv_id=? WHERE idempotency_key='seed-key'",
                     (first,))

    one, _ = login_ctv('ctv_busy_b', key)
    save_config(one, pipeline, topic='B')
    with m.db() as conn:
        outcome = ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
    assert outcome['created'] == []
    assert any('CTV khác' in note for note in outcome['skipped'])
    one.close()


# --- 12. không vượt giới hạn tổng của tài khoản ------------------------------

def test_account_daily_limit_counts_every_collaborator(client):
    pipeline = make_pipeline(client, '@ctv_limit')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/555555/')
    make_media(client, pipeline, 'a.jpg')
    make_media(client, pipeline, 'b.jpg')
    make_ctv(client, pipeline, 'ctv_limit_a')
    make_ctv(client, pipeline, 'ctv_limit_b')
    key = new_pipeline_key(client, pipeline)

    # Quản trị giới hạn chung: 2 bài/ngày cho Facebook này.
    client.put(f'/api/pipelines/{pipeline}/auto-post/limits/{account}',
               json={'daily_limit': 2})

    one, _ = login_ctv('ctv_limit_a', key)
    two, _ = login_ctv('ctv_limit_b', key)
    save_config(one, pipeline, topic='A', max_posts_per_account_daily=5)
    save_config(two, pipeline, topic='B', max_posts_per_account_daily=5)

    with m.db() as conn:
        ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
        used = conn.execute("""SELECT COALESCE(SUM(published+reserved),0) FROM posting_counters
                                WHERE account_id=?""", (account,)).fetchone()[0]
    assert used <= 2, "Hạn mức chung của Facebook không được vượt quá"
    one.close()
    two.close()


def test_collaborator_limit_never_goes_above_the_shared_limit(client):
    pipeline = make_pipeline(client, '@ctv_clamp')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/666666/')
    make_ctv(client, pipeline, 'ctv_clamp')
    key = new_pipeline_key(client, pipeline)

    # Chính sách chung của quản trị: 2 bài/ngày.
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings',
               json={'enabled': False, 'scope': 'all', 'daily_limit_per_account': 2,
                     'min_gap_minutes': 0, 'pipeline_daily_limit': 0, 'topic': '',
                     'ai_caption': False, 'anti_duplicate': True, 'require_approval': True,
                     'apply_to_new_accounts': True})
    one, _ = login_ctv('ctv_clamp', key)
    response = save_config(one, pipeline, max_posts_per_account_daily=9)
    assert response.json()['settings']['max_posts_per_account_daily'] == 2
    one.close()


def test_collaborator_minimum_gap_never_goes_below_the_shared_limit(client):
    pipeline = make_pipeline(client, '@ctv_gap')
    make_ctv(client, pipeline, 'ctv_gap')
    key = new_pipeline_key(client, pipeline)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings',
               json={'enabled': False, 'scope': 'all', 'daily_limit_per_account': 3,
                     'min_gap_minutes': 45, 'pipeline_daily_limit': 0, 'topic': '',
                     'ai_caption': False, 'anti_duplicate': True, 'require_approval': True,
                     'apply_to_new_accounts': True})
    one, _ = login_ctv('ctv_gap', key)
    response = save_config(one, pipeline, schedule_rules={
        'days_enabled': [1, 2, 3, 4, 5, 6, 0], 'min_gap_minutes': 5,
        'group_daily_limit': 0, 'image_selection_rule': 'rotate'})
    assert response.json()['settings']['schedule_rules']['min_gap_minutes'] == 45
    one.close()


# --- 13. không đăng trùng -----------------------------------------------------

def test_the_same_picture_is_never_posted_twice_in_a_pipeline(client):
    pipeline = make_pipeline(client, '@ctv_duplicate')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/777777/')
    make_group(client, pipeline, account, 'Group B', 'https://www.facebook.com/groups/888888/')
    make_media(client, pipeline, 'a.jpg', sha='a' * 64)
    make_ctv(client, pipeline, 'ctv_dup')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_dup', key)
    save_config(one, pipeline, topic='A', max_posts_per_account_daily=3)

    with m.db() as conn:
        ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
        ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
        media_ids = [row[0] for row in conn.execute(
            "SELECT media_id FROM auto_post_jobs WHERE pipeline_id=?", (pipeline,)).fetchall()]
    assert len(media_ids) == len(set(media_ids)), "Ảnh không được đăng trùng trong Workspace"
    one.close()


def test_two_collaborators_never_target_the_same_group(client):
    pipeline = make_pipeline(client, '@ctv_group')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/999999/')
    make_media(client, pipeline, 'a.jpg')
    make_media(client, pipeline, 'b.jpg')
    make_ctv(client, pipeline, 'ctv_group_a')
    make_ctv(client, pipeline, 'ctv_group_b')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_group_a', key)
    two, _ = login_ctv('ctv_group_b', key)
    save_config(one, pipeline, topic='A', max_posts_per_account_daily=3)
    save_config(two, pipeline, topic='B', max_posts_per_account_daily=3)

    with m.db() as conn:
        ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
        rows = conn.execute("""SELECT group_id, created_by_ctv_id FROM auto_post_jobs
                                WHERE pipeline_id=?""", (pipeline,)).fetchall()
    owners = {}
    for row in rows:
        owners.setdefault(row['group_id'], set()).add(row['created_by_ctv_id'])
    for group_id, holder in owners.items():
        assert len(holder) == 1, f"Group {group_id} bị nhiều CTV cùng nhắm tới"
    one.close()
    two.close()


# --- 14. không truy cập Workspace khác ---------------------------------------

def test_collaborator_cannot_reach_another_pipeline(client):
    mine = make_pipeline(client, '@ctv_mine')
    theirs = make_pipeline(client, '@ctv_theirs')
    make_ctv(client, mine, 'ctv_isolated')
    key = new_pipeline_key(client, mine)
    one, _ = login_ctv('ctv_isolated', key)
    assert one.get(f'/api/pipelines/{theirs}/auto-post/my-config').status_code == 404
    assert one.put(f'/api/pipelines/{theirs}/auto-post/my-config',
                   json={'topic': 'x'}).status_code == 404
    assert one.get(f'/api/pipelines/{theirs}/auto-post/my-jobs').status_code == 404
    one.close()


def test_collaborator_only_sees_its_own_jobs(client):
    pipeline = make_pipeline(client, '@ctv_jobs')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/121212/')
    make_media(client, pipeline, 'a.jpg')
    first = make_ctv(client, pipeline, 'ctv_jobs_a')
    second = make_ctv(client, pipeline, 'ctv_jobs_b')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_jobs_a', key)
    two, _ = login_ctv('ctv_jobs_b', key)
    save_config(one, pipeline, topic='A')
    save_config(two, pipeline, topic='B')
    with m.db() as conn:
        ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
    jobs_one = one.get(f'/api/pipelines/{pipeline}/auto-post/my-jobs').json()['jobs']
    jobs_two = two.get(f'/api/pipelines/{pipeline}/auto-post/my-jobs').json()['jobs']
    assert all(job['created_by_ctv_id'] == first for job in jobs_one)
    assert all(job['created_by_ctv_id'] == second for job in jobs_two)
    one.close()
    two.close()


# --- 15. lỗi API không được hiện thành "Chưa có cấu hình" --------------------

def test_a_broken_api_is_not_reported_as_a_missing_configuration(client):
    pipeline = make_pipeline(client, '@ctv_error')
    make_ctv(client, pipeline, 'ctv_error')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_error', key)
    save_config(one, pipeline, topic='đã lưu')

    # Đường dẫn sai phải trả lỗi, không được trả về một cấu hình trống.
    response = one.get(f'/api/pipelines/{pipeline}/auto-post/my-config-not-real')
    assert response.status_code == 404
    assert 'configured' not in response.json()
    one.close()


def test_a_failed_request_never_looks_like_a_fresh_configuration(client):
    """Một lỗi phải là lỗi, không bao giờ là "chưa cấu hình"."""
    pipeline = make_pipeline(client, '@ctv_failure')
    make_ctv(client, pipeline, 'ctv_failure')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_failure', key)

    # Không đăng nhập thì không có phiên, nên API trả lỗi xác thực chứ không
    # trả một cấu hình mặc định.
    anonymous = AuthClient(m.app)
    response = anonymous.get(f'/api/pipelines/{pipeline}/auto-post/my-config')
    assert response.status_code == 401
    assert 'configured' not in response.json()
    anonymous.close()
    one.close()


def test_a_collaborator_without_the_permission_is_refused(client):
    pipeline = make_pipeline(client, '@ctv_noperm')
    client.post('/api/admin/users', json={
        'username': 'ctv_noperm', 'password': CTV_PASSWORD, 'pipeline_id': pipeline,
        'permissions': ['facebook.view']})
    key = new_pipeline_key(client, pipeline)
    one, response = login_ctv('ctv_noperm', key)
    assert response.status_code == 200
    # Quyền tự cấu hình là quyền mặc định, nên phải có.
    assert 'auto_post.self_configure' in response.json()['permissions']
    one.close()


def test_the_shared_configuration_stays_out_of_reach_of_a_collaborator(client):
    pipeline = make_pipeline(client, '@ctv_shared_config')
    client.post('/api/admin/users', json={
        'username': 'ctv_shared_config', 'password': CTV_PASSWORD, 'pipeline_id': pipeline,
        'permissions': ['facebook.view']})
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_shared_config', key)
    # Cấu hình chung của Pipeline vẫn là của quản trị.
    assert one.get(f'/api/pipelines/{pipeline}/auto-post/settings').status_code == 403
    assert one.put(f'/api/pipelines/{pipeline}/auto-post/settings',
                   json={'enabled': True}).status_code == 403
    one.close()


# --- dữ liệu cũ được giữ nguyên ----------------------------------------------

def test_the_old_shared_configuration_is_untouched(client):
    pipeline = make_pipeline(client, '@ctv_legacy')
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings',
               json={'enabled': True, 'scope': 'all', 'daily_limit_per_account': 4,
                     'min_gap_minutes': 30, 'pipeline_daily_limit': 0, 'topic': 'cũ',
                     'ai_caption': True, 'anti_duplicate': True, 'require_approval': False,
                     'apply_to_new_accounts': True})
    make_ctv(client, pipeline, 'ctv_legacy')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_legacy', key)
    # Cấu hình riêng của CTV không đụng tới cấu hình chung.
    save_config(one, pipeline, topic='của CTV')
    shared = client.get(f'/api/pipelines/{pipeline}/auto-post/settings').json()['settings']
    assert shared['topic'] == 'cũ'
    assert shared['daily_limit_per_account'] == 4
    assert shared['enabled'] is True
    one.close()


# --- kích hoạt và tạm dừng ---------------------------------------------------

def test_collaborator_activates_and_pauses_own_schedule(client):
    pipeline = make_pipeline(client, '@ctv_toggle')
    make_ctv(client, pipeline, 'ctv_toggle')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_toggle', key)
    save_config(one, pipeline, enabled=False)
    assert one.post(f'/api/pipelines/{pipeline}/auto-post/my-config/activate').json()[
        'settings']['enabled'] is True
    assert one.post(f'/api/pipelines/{pipeline}/auto-post/my-config/pause').json()[
        'settings']['enabled'] is False
    one.close()


def test_pausing_stops_the_scheduler_for_that_collaborator(client):
    pipeline = make_pipeline(client, '@ctv_paused')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/131313/')
    make_media(client, pipeline, 'a.jpg')
    make_ctv(client, pipeline, 'ctv_paused')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_paused', key)
    save_config(one, pipeline, topic='A', enabled=False)
    with m.db() as conn:
        outcome = ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
    assert outcome['created'] == []
    one.post(f'/api/pipelines/{pipeline}/auto-post/my-config/activate')
    with m.db() as conn:
        outcome = ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
    assert outcome['created'], "Bật lại thì phải xếp lịch được"
    one.close()


# --- không tự chuyển job sang tài khoản khác ---------------------------------

def test_a_job_keeps_the_account_it_was_planned_for(client):
    pipeline = make_pipeline(client, '@ctv_switch')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    make_group(client, pipeline, first, 'Group A', 'https://www.facebook.com/groups/141414/')
    make_group(client, pipeline, second, 'Group B', 'https://www.facebook.com/groups/151515/')
    make_media(client, pipeline, 'a.jpg')
    make_ctv(client, pipeline, 'ctv_switch')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_switch', key)
    save_config(one, pipeline, topic='A', account_scope='selected',
                selected_account_ids=[first])
    with m.db() as conn:
        ctv_allocator.allocate(conn, pipeline, moment=inside_window(), max_jobs=20)
        rows = conn.execute("""SELECT account_id FROM auto_post_jobs
                                WHERE pipeline_id=?""", (pipeline,)).fetchall()
    assert rows, "Phải có job được xếp lịch"
    assert {row[0] for row in rows} == {first}, "Không được chuyển sang tài khoản khác"
    one.close()


# --- xem trước lịch đăng -----------------------------------------------------

def test_preview_reports_the_schedule_of_the_collaborator(client):
    pipeline = make_pipeline(client, '@ctv_preview')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/161616/')
    make_media(client, pipeline, 'a.jpg')
    make_ctv(client, pipeline, 'ctv_preview')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_preview', key)
    save_config(one, pipeline, topic='A', start_time='08:00', end_time='20:00')
    response = one.get(f'/api/pipelines/{pipeline}/auto-post/my-config/preview')
    assert response.status_code == 200
    body = response.json()
    assert body['schedule']['start_time'] == '08:00'
    assert body['schedule']['end_time'] == '20:00'
    assert 'accounts' in body
    one.close()


# --- cấu hình cá nhân của CTV không được ghi bằng user của quản trị ---------

def test_a_configuration_is_never_stored_for_the_administrator(client):
    pipeline = make_pipeline(client, '@ctv_admin_owner')
    with m.db() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()['id']
    make_ctv(client, pipeline, 'ctv_admin_owner')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_admin_owner', key)
    save_config(one, pipeline, topic='của CTV')
    with m.db() as conn:
        rows = conn.execute("""SELECT ctv_user_id FROM ctv_auto_post_settings
                                WHERE pipeline_id=?""", (pipeline,)).fetchall()
    assert rows, "Cấu hình của CTV phải được lưu"
    assert all(row['ctv_user_id'] != admin_id for row in rows)
    one.close()


# --- danh sách group của Workspace ------------------------------------------

def test_the_groups_of_a_workspace_can_be_read(client):
    pipeline = make_pipeline(client, '@ctv_groups')
    account = make_account(client, pipeline, 'Facebook chung')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/171717/')
    make_ctv(client, pipeline, 'ctv_groups')
    key = new_pipeline_key(client, pipeline)
    one, _ = login_ctv('ctv_groups', key)
    response = one.get(f'/api/pipelines/{pipeline}/groups')
    assert response.status_code == 200, response.text
    assert [row['label'] for row in response.json()] == ['Group A']
    one.close()
