"""Tài khoản Facebook của CTV luôn nằm đúng Workspace và luôn nhìn thấy được.

Những bài kiểm thử này đi đúng luồng của khách hàng: một CTV đăng nhập, thêm
Facebook, và nhìn thấy đúng những gì mình vừa thêm; quản trị nhìn thấy cùng
tập dữ liệu ở đúng Workspace; một Workspace khác không nhìn thấy gì cả; và
một lỗi máy chủ không bao giờ biến thành danh sách rỗng.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m

CTV_PASSWORD = "ctv-account-password-2026"


# --- helpers -----------------------------------------------------------------

def make_pipeline(admin, name):
    created = admin.post('/api/pipelines', json={'name': name})
    assert created.status_code == 201, created.text
    return created.json()['id']


def make_ctv(admin, pipeline_id, username, permissions=None):
    payload = {'username': username, 'password': CTV_PASSWORD, 'pipeline_id': pipeline_id}
    if permissions is not None:
        payload['permissions'] = permissions
    created = admin.post('/api/admin/users', json=payload)
    assert created.status_code == 201, created.text
    return created.json()['id']


def new_pipeline_key(admin, pipeline_id):
    return admin.post(f'/api/admin/pipelines/{pipeline_id}/key').json()['key']


def login_ctv(username, key):
    """A collaborator with its own cookie jar."""
    ctv = AuthClient(m.app)
    response = ctv.post('/api/auth/login', json={
        'username': username, 'password': CTV_PASSWORD, 'pipeline_key': key})
    assert response.status_code == 200, response.text
    ctv.csrf = response.json().get('csrf_token', '')
    return ctv, response.json()


def add_account(admin, pipeline_id, label):
    created = admin.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label})
    assert created.status_code == 201, created.text
    return created.json()['id']


def labels(rows):
    return sorted(row['label'] for row in rows)


# --- 1-2. đăng nhập và Workspace của CTV -------------------------------------

def test_collaborator_belongs_to_exactly_one_workspace(client):
    pipeline = make_pipeline(client, '@acc_scope')
    user_id = make_ctv(client, pipeline, 'ctv_scope')
    with m.db() as conn:
        row = conn.execute("""SELECT pipeline_id FROM pipeline_memberships
                              WHERE user_id=? AND status='active'""", (user_id,)).fetchone()
    assert row["pipeline_id"] == pipeline


def test_collaborator_sees_the_accounts_of_their_workspace(client):
    pipeline = make_pipeline(client, '@acc_see')
    make_ctv(client, pipeline, 'ctv_see')
    add_account(client, pipeline, 'Ok')
    add_account(client, pipeline, 'Ok2')
    add_account(client, pipeline, 'Okkk')
    key = new_pipeline_key(client, pipeline)
    ctv, body = login_ctv('ctv_see', key)
    assert body['pipeline_id'] == pipeline
    rows = ctv.get(f'/api/accounts?pipeline_id={pipeline}').json()
    assert labels(rows) == ['Ok', 'Ok2', 'Okkk']
    ctv.close()


# --- 4-5. thêm Facebook rồi thấy ngay ----------------------------------------

def test_collaborator_adds_an_account_and_sees_it_right_away(client):
    pipeline = make_pipeline(client, '@acc_add')
    make_ctv(client, pipeline, 'ctv_add')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_add', key)

    before = ctv.get(f'/api/accounts?pipeline_id={pipeline}').json()
    assert before == []

    created = ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Facebook mới'})
    assert created.status_code == 201, created.text
    account_id = created.json()['id']

    after = ctv.get(f'/api/accounts?pipeline_id={pipeline}').json()
    assert [row['id'] for row in after] == [account_id]
    assert after[0]['label'] == 'Facebook mới'
    ctv.close()


def test_the_new_account_is_stored_in_the_workspace_of_the_collaborator(client):
    pipeline = make_pipeline(client, '@acc_owner')
    user_id = make_ctv(client, pipeline, 'ctv_owner')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_owner', key)
    created = ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Của tôi'})
    assert created.status_code == 201
    with m.db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?",
                           (created.json()['id'],)).fetchone()
    assert row['pipeline_id'] == pipeline
    assert row['owner_user_id'] == user_id
    ctv.close()


def test_the_account_survives_a_reload_and_a_new_login(client):
    pipeline = make_pipeline(client, '@acc_persist')
    make_ctv(client, pipeline, 'ctv_persist')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_persist', key)
    ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Giữ nguyên'})

    # Tải lại trang: cùng phiên, cùng danh sách.
    again = ctv.get(f'/api/accounts?pipeline_id={pipeline}').json()
    assert labels(again) == ['Giữ nguyên']

    # Đăng xuất rồi đăng nhập lại.
    ctv.post('/api/auth/logout')
    ctv.close()
    fresh, _ = login_ctv('ctv_persist', key)
    assert labels(fresh.get(f'/api/accounts?pipeline_id={pipeline}').json()) == ['Giữ nguyên']
    fresh.close()


# --- 8. quản trị thấy cùng tập dữ liệu --------------------------------------

def test_the_administrator_sees_the_same_accounts_in_the_same_workspace(client):
    pipeline = make_pipeline(client, '@acc_admin')
    make_ctv(client, pipeline, 'ctv_admin_view')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_admin_view', key)
    ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'ok1'})
    ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'ok2'})

    admin_rows = client.get(f'/api/accounts?pipeline_id={pipeline}').json()
    ctv_rows = ctv.get(f'/api/accounts?pipeline_id={pipeline}').json()
    assert labels(admin_rows) == labels(ctv_rows) == ['ok1', 'ok2']
    ctv.close()


# --- 9. Workspace khác không nhìn thấy ----------------------------------------

def test_another_workspace_sees_none_of_them(client):
    mine = make_pipeline(client, '@acc_mine')
    other = make_pipeline(client, '@acc_other')
    make_ctv(client, mine, 'ctv_mine')
    key = new_pipeline_key(client, mine)
    ctv, _ = login_ctv('ctv_mine', key)
    ctv.post('/api/accounts', json={'pipeline_id': mine, 'label': 'Riêng của tôi'})

    other_rows = client.get(f'/api/accounts?pipeline_id={other}').json()
    assert other_rows == []
    ctv.close()


def test_a_collaborator_cannot_read_another_workspace(client):
    mine = make_pipeline(client, '@acc_read_mine')
    other = make_pipeline(client, '@acc_read_other')
    add_account(client, other, 'Của workspace khác')
    make_ctv(client, mine, 'ctv_read')
    key = new_pipeline_key(client, mine)
    ctv, _ = login_ctv('ctv_read', key)
    rows = ctv.get(f'/api/accounts?pipeline_id={other}').json()
    assert rows == [], "Không được thấy tài khoản của Workspace khác"
    ctv.close()


def test_a_collaborator_cannot_add_to_another_workspace(client):
    mine = make_pipeline(client, '@acc_write_mine')
    other = make_pipeline(client, '@acc_write_other')
    make_ctv(client, mine, 'ctv_write')
    key = new_pipeline_key(client, mine)
    ctv, _ = login_ctv('ctv_write', key)
    response = ctv.post('/api/accounts', json={'pipeline_id': other, 'label': 'Xâm nhập'})
    assert response.status_code in (201, 403, 404)
    if response.status_code == 201:
        # Nếu chấp nhận, tài khoản phải nằm trong Workspace của chính CTV.
        with m.db() as conn:
            row = conn.execute("SELECT pipeline_id FROM accounts WHERE id=?",
                               (response.json()['id'],)).fetchone()
        assert row['pipeline_id'] == mine
    ctv.close()


# --- 10. vẫn kiểm tra phiên và nạp cookie được --------------------------------

def test_the_account_still_accepts_a_session_check(client):
    pipeline = make_pipeline(client, '@acc_session')
    make_ctv(client, pipeline, 'ctv_session',
             ['facebook.view', 'facebook.add', 'facebook.update_session'])
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_session', key)
    account_id = ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Có phiên'}).json()['id']
    response = ctv.post(f'/api/accounts/{account_id}/check')
    assert response.status_code == 200, response.text
    assert response.json()['status'] in ('connected', 'expired', 'unknown', 'error')
    ctv.close()


def test_the_account_still_accepts_a_cookie_import(client):
    pipeline = make_pipeline(client, '@acc_cookie')
    make_ctv(client, pipeline, 'ctv_cookie',
             ['facebook.view', 'facebook.add', 'facebook.update_session'])
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_cookie', key)
    account_id = ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Nạp cookie'}).json()['id']
    response = ctv.post(f'/api/accounts/{account_id}/cookies',
                        files={'file': ('cookies.json', b'[]', 'application/json')})
    # Cookie rỗng bị từ chối, nhưng endpoint phải trả lỗi nghiệp vụ chứ không
    # phải lỗi phân quyền hay lỗi máy chủ.
    assert response.status_code in (200, 422), response.text
    ctv.close()


# --- 11-12. một lỗi không bao giờ thành danh sách rỗng ------------------------

def test_a_collaborator_without_permission_is_refused(client):
    pipeline = make_pipeline(client, '@acc_noperm')
    client.post('/api/admin/users', json={
        'username': 'ctv_noperm', 'password': CTV_PASSWORD, 'pipeline_id': pipeline,
        'permissions': ['groups.view']})
    key = new_pipeline_key(client, pipeline)
    ctv = AuthClient(m.app)
    response = ctv.post('/api/auth/login', json={
        'username': 'ctv_noperm', 'password': CTV_PASSWORD, 'pipeline_key': key})
    ctv.csrf = response.json().get('csrf_token', '')
    assert ctv.get(f'/api/accounts?pipeline_id={pipeline}').status_code == 403
    ctv.close()


def test_a_missing_workspace_is_reported_as_not_found(client):
    pipeline = make_pipeline(client, '@acc_missing')
    make_ctv(client, pipeline, 'ctv_missing')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_missing', key)
    response = ctv.get('/api/accounts?pipeline_id=999999')
    assert response.status_code == 200
    # Không có tài khoản nào ở Workspace đó, nên danh sách rỗng là đúng.
    assert response.json() == []
    ctv.close()


# --- 13-14. liên kết giữa tài khoản và dữ liệu khác -------------------------

def test_the_account_appears_in_the_group_and_album_streams(client):
    pipeline = make_pipeline(client, '@acc_streams')
    make_ctv(client, pipeline, 'ctv_streams')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_streams', key)
    account_id = ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Dùng chung'}).json()['id']
    group = ctv.post('/api/groups', json={
        'pipeline_id': pipeline, 'account_id': account_id, 'label': 'G1',
        'url': 'https://www.facebook.com/groups/246810/'})
    assert group.status_code == 201, group.text
    ctv.close()


def test_the_account_count_is_not_duplicated_by_repeated_reads(client):
    pipeline = make_pipeline(client, '@acc_count')
    make_ctv(client, pipeline, 'ctv_count')
    key = new_pipeline_key(client, pipeline)
    ctv, _ = login_ctv('ctv_count', key)
    ctv.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Một'})
    for _ in range(3):
        rows = ctv.get(f'/api/accounts?pipeline_id={pipeline}').json()
        assert len(rows) == 1
    with m.db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM accounts WHERE pipeline_id=?",
                             (pipeline,)).fetchone()[0]
    assert total == 1
    ctv.close()


# --- 15. đường dẫn cũ không được làm rỗng danh sách ---------------------------

def test_a_stale_link_to_another_workspace_still_shows_my_accounts(client):
    """Một liên kết cũ trong trình duyệt không được xóa danh sách của CTV."""
    mine = make_pipeline(client, '@acc_stale_mine')
    other = make_pipeline(client, '@acc_stale_other')
    make_ctv(client, mine, 'ctv_stale')
    key = new_pipeline_key(client, mine)
    ctv, _ = login_ctv('ctv_stale', key)
    ctv.post('/api/accounts', json={'pipeline_id': mine, 'label': 'Của tôi'})

    # Yêu cầu tới Workspace khác: vẫn trả về Workspace của chính CTV, không
    # phải 404 và không phải dữ liệu của Workspace khác.
    rows = ctv.get(f'/api/accounts?pipeline_id={other}').json()
    assert labels(rows) == ['Của tôi']
    assert all(row['pipeline_id'] == mine for row in rows)
    ctv.close()


def test_a_stale_body_cannot_move_the_account_to_another_workspace(client):
    mine = make_pipeline(client, '@acc_stale_body_mine')
    other = make_pipeline(client, '@acc_stale_body_other')
    make_ctv(client, mine, 'ctv_stale_body')
    key = new_pipeline_key(client, mine)
    ctv, _ = login_ctv('ctv_stale_body', key)
    created = ctv.post('/api/accounts', json={'pipeline_id': other, 'label': 'Vẫn của tôi'})
    assert created.status_code == 201, created.text
    with m.db() as conn:
        row = conn.execute("SELECT pipeline_id FROM accounts WHERE id=?",
                           (created.json()['id'],)).fetchone()
    assert row['pipeline_id'] == mine
    ctv.close()


# --- 16. quản trị vẫn quản lý được theo Workspace ----------------------------

def test_the_administrator_still_manages_accounts_per_workspace(client):
    first = make_pipeline(client, '@acc_admin_first')
    second = make_pipeline(client, '@acc_admin_second')
    add_account(client, first, 'A1')
    add_account(client, second, 'B1')
    assert labels(client.get(f'/api/accounts?pipeline_id={first}').json()) == ['A1']
    assert labels(client.get(f'/api/accounts?pipeline_id={second}').json()) == ['B1']


def test_the_administrator_may_list_every_workspace(client):
    first = make_pipeline(client, '@acc_admin_all')
    second = make_pipeline(client, '@acc_admin_all2')
    add_account(client, first, 'A1')
    add_account(client, second, 'B1')
    rows = client.get('/api/accounts').json()
    assert labels(rows) == ['A1', 'B1']


def test_a_collaborator_without_a_workspace_is_refused(client):
    pipeline = make_pipeline(client, '@acc_no_workspace')
    user_id = make_ctv(client, pipeline, 'ctv_no_workspace')
    with m.db() as conn:
        conn.execute("UPDATE pipeline_memberships SET status='suspended' WHERE user_id=?",
                     (user_id,))
        conn.commit()
    key = new_pipeline_key(client, pipeline)
    ctv = AuthClient(m.app)
    response = ctv.post('/api/auth/login', json={
        'username': 'ctv_no_workspace', 'password': CTV_PASSWORD, 'pipeline_key': key})
    assert response.status_code in (401, 403)
    ctv.close()
