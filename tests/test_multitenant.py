"""Multi-tenant authentication and per-pipeline RBAC.

Covers the fourteen mandatory scenarios of the multi-tenant upgrade plus the
credential handling guarantees (Argon2id, hashed keys, CSRF, rate limiting).
"""
import concurrent.futures
import json
import os
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient
from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m
import auth as au

CTV_PASSWORD = "ctv-password-2026-strong"


def make_tenant(admin, name, username, permissions=None, password=CTV_PASSWORD):
    """Create a pipeline with a key plus a collaborator bound to it."""
    created = admin.post('/api/admin/pipelines', json={'name': name})
    assert created.status_code == 201, created.text
    pipeline = created.json()
    key = pipeline['key']
    member = admin.post('/api/admin/users', json={
        'username': username, 'password': password, 'pipeline_id': pipeline['id'],
        'permissions': permissions or []})
    assert member.status_code == 201, member.text
    return pipeline, key, member.json()


def login_ctv(username, password=CTV_PASSWORD, key=None):
    """A separate client (own cookie jar) signed in as a collaborator."""
    client = AuthClient(m.app)
    response = client.post('/api/auth/login', json={
        'username': username, 'password': password, 'pipeline_key': key})
    client.csrf = response.json().get('csrf_token', '') if response.status_code == 200 else ''
    return client, response


# --- 1. admin creates a pipeline ---------------------------------------------

def test_admin_creates_pipeline_with_generated_key(client):
    response = client.post('/api/admin/pipelines', json={'name': '@shop01', 'description': 'Shop'})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['name'] == '@shop01'
    assert body['key'].startswith('FPM-')
    assert len(body['key']) > 30
    # the secret is shown once only; afterwards only metadata is available
    listing = client.get(f"/api/admin/pipelines/{body['id']}/key").json()
    assert body["key"] not in json.dumps(listing)
    assert listing[0]['key_prefix'] == body['key'][:12]
    assert listing[0]['revoked_at'] is None


def test_admin_pipeline_list_reports_counters(client):
    pipeline = client.post('/api/admin/pipelines', json={'name': '@counts'}).json()
    client.post('/api/accounts', json={'pipeline_id': pipeline['id'], 'label': 'FB'})
    client.post('/api/admin/users', json={'username': 'ctv_counts', 'password': CTV_PASSWORD,
                                           'pipeline_id': pipeline['id']})
    rows = client.get('/api/admin/pipelines').json()
    row = next(r for r in rows if r['id'] == pipeline['id'])
    assert row['accounts_count'] == 1
    assert row['ctv_count'] == 1
    assert row['active_keys'] == 1
    assert row['posted_count'] == 0


# --- 2. admin creates a collaborator ----------------------------------------

def test_admin_creates_collaborator_bound_to_one_pipeline(client):
    pipeline = client.post('/api/admin/pipelines', json={'name': '@team'}).json()
    created = client.post('/api/admin/users', json={
        'username': 'ctv01', 'password': CTV_PASSWORD, 'pipeline_id': pipeline['id'],
        'permissions': ['facebook.view', 'group_posts.create']})
    assert created.status_code == 201, created.text
    listing = client.get('/api/admin/users').json()
    member = next(u for u in listing if u['username'] == 'ctv01')
    assert member['pipeline_id'] == pipeline['id']
    assert member['role'] == 'ctv'
    # Quản trị chọn quyền, và mỗi CTV luôn có quyền tự thiết lập lịch đăng
    # của riêng mình. Quyền này không đụng tới cấu hình chung của Workspace.
    assert set(member['permissions']) == {
        'facebook.view', 'group_posts.create', 'auto_post.self_configure'}
    # Quyền cấu hình chung vẫn phải được cấp riêng.
    assert 'auto_post.configure' not in member['permissions']
    # duplicate username is refused
    again = client.post('/api/admin/users', json={
        'username': 'ctv01', 'password': CTV_PASSWORD, 'pipeline_id': pipeline['id']})
    assert again.status_code == 409


def test_collaborator_password_is_hashed_and_never_stored_in_clear(client):
    client.post('/api/admin/pipelines', json={'name': '@hash'})
    pipeline_id = client.get('/api/admin/pipelines').json()[0]['id']
    client.post('/api/admin/users', json={'username': 'ctv_hash', 'password': CTV_PASSWORD,
                                           'pipeline_id': pipeline_id})
    with m.db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE username='ctv_hash'").fetchone()
    assert CTV_PASSWORD not in row['password_hash']
    assert row['password_hash'].startswith('$argon2id$')
    assert au.verify_secret(row['password_hash'], CTV_PASSWORD)


def test_pipeline_key_is_only_stored_as_a_hash(client):
    pipeline = client.post('/api/admin/pipelines', json={'name': '@keyhash'}).json()
    with m.db() as conn:
        row = conn.execute("SELECT key_hash FROM pipeline_access_keys WHERE pipeline_id=?",
                           (pipeline['id'],)).fetchone()
    assert pipeline['key'] not in row['key_hash']
    assert row['key_hash'].startswith('$argon2id$')


# --- 3. collaborator signs in with all three factors -------------------------

def test_collaborator_signs_in_with_username_password_and_key(client):
    pipeline, key, _ = make_tenant(client, '@alpha', 'ctv_alpha')
    ctv, response = login_ctv('ctv_alpha', key=key)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['role'] == 'ctv'
    assert body['pipeline_id'] == pipeline['id']
    assert body['csrf_token']
    me = ctv.get('/api/auth/me').json()
    assert me['username'] == 'ctv_alpha'
    assert me['pipeline_name'] == '@alpha'
    # only their own workspace is visible
    assert [p['id'] for p in ctv.get('/api/pipelines').json()] == [pipeline['id']]
    ctv.close()


# --- 4. wrong key is refused -------------------------------------------------

def test_wrong_or_missing_key_is_refused(client):
    pipeline, key, _ = make_tenant(client, '@beta', 'ctv_beta')
    wrong, response = login_ctv('ctv_beta', key='FPM-WRONGKEYWRONGKEYWRONGKEYWRONGKEY')
    assert response.status_code == 401
    missing, response = login_ctv('ctv_beta', key='')
    assert response.status_code == 401
    ctv, response = login_ctv('ctv_beta', key=key)
    assert response.status_code == 200
    ctv.close()


def test_wrong_password_is_refused(client):
    pipeline, key, _ = make_tenant(client, '@gamma', 'ctv_gamma')
    ctv, response = login_ctv('ctv_gamma', password='not-the-password', key=key)
    assert response.status_code == 401
    ctv.close()


# --- 5. another pipeline's key grants nothing --------------------------------

def test_key_of_another_pipeline_is_refused(client):
    first, first_key, _ = make_tenant(client, '@one', 'ctv_one')
    second, second_key, _ = make_tenant(client, '@two', 'ctv_two')
    ctv, response = login_ctv('ctv_one', key=second_key)
    assert response.status_code == 401
    assert 'key' in response.json()['detail'].lower()
    ctv.close()


# --- 6. a collaborator cannot reach another workspace by editing the request --

def test_collaborator_cannot_switch_pipeline_by_tampering(client):
    mine, my_key, _ = make_tenant(client, '@mine', 'ctv_mine',
                                   permissions=['facebook.view', 'groups.view', 'history.view',
                                                'group_posts.create', 'facebook.add'])
    theirs, their_key, _ = make_tenant(client, '@theirs', 'ctv_theirs')
    # give the foreign workspace some data
    foreign_account = client.post('/api/accounts', json={'pipeline_id': theirs['id'], 'label': 'Secret account'}).json()['id']
    client.post('/api/groups', json={'pipeline_id': theirs['id'], 'account_id': foreign_account,
                                     'label': 'Secret group', 'url': 'https://www.facebook.com/groups/771/',
                                     'approved_target': True})
    ctv, response = login_ctv('ctv_mine', key=my_key)
    assert response.status_code == 200

    # Yêu cầu tới Workspace khác: CTV vẫn chỉ nhận Workspace của chính mình,
    # không phải dữ liệu của Workspace kia.
    stolen = ctv.get(f"/api/accounts?pipeline_id={theirs['id']}").json()
    assert all(a['pipeline_id'] == mine['id'] for a in stolen)
    assert all(a['label'] != 'Secret account' for a in stolen)
    assert ctv.get(f"/api/groups?pipeline_id={theirs['id']}").json() == []
    assert ctv.get(f"/api/jobs?pipeline_id={theirs['id']}").status_code == 404
    assert ctv.get(f"/api/manual-tasks?pipeline_id={theirs['id']}").status_code == 404
    injected = ctv.post('/api/accounts', json={'pipeline_id': theirs['id'], 'label': 'Injected'})
    assert injected.status_code in (201, 404)
    if injected.status_code == 201:
        # Tài khoản phải nằm trong Workspace của chính CTV.
        assert all(a['pipeline_id'] == mine['id'] for a in ctv.get('/api/accounts').json())
    assert ctv.post(f'/api/accounts/{foreign_account}/cookies',
                    files={'file': ('c.json', b'[]', 'application/json')}).status_code == 404
    assert ctv.post(f'/api/accounts/{foreign_account}/cookies',
                    files={'file': ('c.json', b'[]', 'application/json')}).status_code == 404
    # deleting an account is an administrator action, never a collaborator one
    assert ctv.delete(f'/api/accounts/{foreign_account}').status_code == 403
    # nothing of the foreign workspace leaked into the collaborator's lists
    assert all(a['pipeline_id'] == mine['id'] for a in ctv.get('/api/accounts').json())
    assert ctv.get('/api/pipelines').json()[0]['name'] == '@mine'
    ctv.close()


def test_collaborator_cannot_read_or_write_other_collaborator_rows(client):
    mine, my_key, _ = make_tenant(client, '@alpha2', 'ctv_alpha2',
                                   permissions=['facebook.view', 'history.view', 'groups.view',
                                                'facebook.update_session', 'groups.manage'])
    theirs, their_key, _ = make_tenant(client, '@beta2', 'ctv_beta2')
    their_account = client.post('/api/accounts', json={'pipeline_id': theirs['id'], 'label': 'Theirs'}).json()['id']
    their_group = client.post('/api/groups', json={'pipeline_id': theirs['id'], 'account_id': their_account,
                                                   'label': 'Theirs group',
                                                   'url': 'https://www.facebook.com/groups/881/',
                                                   'approved_target': True}).json()['id']
    ctv, response = login_ctv('ctv_alpha2', key=my_key)
    assert response.status_code == 200
    assert ctv.get('/api/accounts').json() == []
    assert ctv.get('/api/groups').json() == []
    # every endpoint that accepts the foreign identifiers reports "not found"
    assert ctv.post(f'/api/accounts/{their_account}/cookies',
                    files={'file': ('c.json', b'[]', 'application/json')}).status_code == 404
    assert ctv.patch(f'/api/groups/{their_group}', json={'enabled': False}).status_code == 404
    assert ctv.post('/api/groups', json={'pipeline_id': mine['id'], 'account_id': their_account,
                                         'label': 'Foreign group',
                                         'url': 'https://www.facebook.com/groups/992/',
                                         'approved_target': True}).status_code == 404
    ctv.close()


# --- 7. collaborators never see credentials ---------------------------------

def test_collaborator_cannot_read_sensitive_session_data(client):
    pipeline, key, _ = make_tenant(client, '@secret', 'ctv_secret',
                                   permissions=['facebook.view', 'facebook.update_session'])
    ctv, response = login_ctv('ctv_secret', key=key)
    assert response.status_code == 200
    listing = ctv.get('/api/accounts').json()
    body = json.dumps(listing)
    for forbidden in ('cookie_value', 'xs', '"value"', 'password_hash', 'key_hash', 'session_hash'):
        assert forbidden not in body
    # the legacy audit endpoint and the admin surface stay closed
    assert ctv.get('/api/audits').status_code == 403
    assert ctv.get('/api/admin/pipelines').status_code == 403
    assert ctv.get('/api/admin/users').status_code == 403
    assert ctv.get('/api/admin/audit').status_code == 403
    ctv.close()


# --- 8. collaborators cannot act outside their permissions -------------------

def test_collaborator_cannot_act_without_permission(client):
    pipeline, key, _ = make_tenant(client, '@limited', 'ctv_limited',
                                   permissions=['facebook.view'])
    ctv, response = login_ctv('ctv_limited', key=key)
    assert response.status_code == 200
    assert ctv.get('/api/accounts').status_code == 200
    assert ctv.post('/api/accounts', json={'pipeline_id': pipeline['id'], 'label': 'No'}).status_code == 403
    assert ctv.get('/api/jobs').status_code == 403
    assert ctv.get('/api/groups').status_code == 403
    assert ctv.post('/api/groups', json={'pipeline_id': pipeline['id'], 'account_id': 1, 'label': 'Group X',
                                         'url': 'https://www.facebook.com/groups/1/',
                                         'approved_target': True}).status_code == 403
    assert ctv.post('/api/ai-caption/suggest', json={'topic': 'test'}).status_code == 404
    assert ctv.post(f'/api/pipelines/{pipeline["id"]}/caption/optimize',
                    json={'caption': 'Xin chào các bạn'}).status_code == 403
    assert ctv.post('/api/admin/users', json={'username': 'x', 'password': CTV_PASSWORD,
                                              'pipeline_id': pipeline['id']}).status_code == 403
    ctv.close()


def test_default_collaborator_permissions_are_read_and_draft_only(client):
    pipeline, key, _ = make_tenant(client, '@defaults', 'ctv_defaults')
    member = next(u for u in client.get('/api/admin/users').json() if u['username'] == 'ctv_defaults')
    assert set(member['permissions']) == set(au.DEFAULT_CTV_PERMISSIONS)
    assert 'group_posts.publish' not in member['permissions']
    assert 'reels.publish' not in member['permissions']
    assert 'facebook.update_session' not in member['permissions']


def test_draft_post_waits_for_approval_without_publish_permission(client):
    pipeline, key, _ = make_tenant(client, '@drafts', 'ctv_drafts',
                                   permissions=['facebook.view', 'group_posts.create', 'groups.view'])
    account = client.post('/api/accounts', json={'pipeline_id': pipeline['id'], 'label': 'FB'}).json()['id']
    group = client.post('/api/groups', json={'pipeline_id': pipeline['id'], 'account_id': account,
                                             'label': 'Group G', 'url': 'https://www.facebook.com/groups/331/',
                                             'approved_target': True}).json()['id']
    ctv, response = login_ctv('ctv_drafts', key=key)
    assert response.status_code == 200
    image = ctv.post('/api/uploads', params={'pipeline_id': pipeline['id']},
                     files={'file': ('a.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    created = ctv.post('/api/posts', json={
        'pipeline_id': pipeline['id'], 'body': 'draft', 'group_ids': [group], 'media': [image],
        'anonymous_only': True, 'scheduled_at': m.iso(m.now() + __import__('datetime').timedelta(hours=2))})
    assert created.status_code == 201, created.text
    assert created.json()['published'] is False
    assert created.json()['state'] == 'pending_approval'
    # only the administrator may turn a draft into a scheduled publication
    assert ctv.post(f"/api/jobs/{client.get('/api/jobs').json()[0]['id']}/review",
                    json={'state': 'queued'}).status_code == 403
    ctv.close()


# --- 9. suspending a collaborator removes access immediately ----------------

def test_suspending_a_collaborator_cuts_access_immediately(client):
    pipeline, key, _ = make_tenant(client, '@suspend', 'ctv_suspend',
                                   permissions=['facebook.view'])
    ctv, response = login_ctv('ctv_suspend', key=key)
    assert response.status_code == 200
    assert ctv.get('/api/accounts').status_code == 200
    user_id = next(u['id'] for u in client.get('/api/admin/users').json() if u['username'] == 'ctv_suspend')
    assert client.patch(f'/api/admin/users/{user_id}', json={'status': 'suspended'}).status_code == 200
    assert ctv.get('/api/accounts').status_code == 401
    again, response = login_ctv('ctv_suspend', key=key)
    assert response.status_code == 403
    again.close()
    ctv.close()


def test_revoking_permissions_takes_effect_on_the_next_request(client):
    pipeline, key, _ = make_tenant(client, '@permchange', 'ctv_perm',
                                   permissions=['facebook.view', 'groups.view'])
    ctv, response = login_ctv('ctv_perm', key=key)
    assert response.status_code == 200
    assert ctv.get('/api/groups').status_code == 200
    user_id = next(u['id'] for u in client.get('/api/admin/users').json() if u['username'] == 'ctv_perm')
    client.patch(f'/api/admin/users/{user_id}', json={'permissions': ['facebook.view']})
    assert ctv.get('/api/groups').status_code == 403
    ctv.close()


# --- 10. revoking a key invalidates the sessions bound to it -----------------

def test_revoking_the_key_invalidates_the_bound_sessions(client):
    pipeline, key, _ = make_tenant(client, '@revoke', 'ctv_revoke',
                                   permissions=['facebook.view'])
    ctv, response = login_ctv('ctv_revoke', key=key)
    assert response.status_code == 200
    assert ctv.get('/api/accounts').status_code == 200
    assert client.delete(f"/api/admin/pipelines/{pipeline['id']}/key").status_code == 200
    # the session was created with that key, so it is no longer valid
    assert ctv.get('/api/accounts').status_code == 401
    again, response = login_ctv('ctv_revoke', key=key)
    assert response.status_code == 401
    again.close()
    ctv.close()


def test_issuing_a_new_key_revokes_the_previous_one(client):
    pipeline, key, _ = make_tenant(client, '@rotate', 'ctv_rotate',
                                   permissions=['facebook.view'])
    rotated = client.post(f"/api/admin/pipelines/{pipeline['id']}/key").json()
    assert rotated['key'] != key
    stale, response = login_ctv('ctv_rotate', key=key)
    assert response.status_code == 401
    fresh, response = login_ctv('ctv_rotate', key=rotated['key'])
    assert response.status_code == 200
    stale.close()
    fresh.close()


# --- 11. two collaborators of two workspaces stay apart ----------------------

def test_two_collaborators_cannot_read_modify_or_download_each_other(client):
    one, one_key, _ = make_tenant(client, '@alpha3', 'ctv_a3',
                                   permissions=['facebook.view', 'groups.view', 'history.view',
                                                'group_posts.create'])
    two, two_key, _ = make_tenant(client, '@beta3', 'ctv_b3',
                                   permissions=['facebook.view', 'groups.view', 'history.view',
                                                'group_posts.create'])
    a_account = client.post('/api/accounts', json={'pipeline_id': one['id'], 'label': 'Account A'}).json()['id']
    a_group = client.post('/api/groups', json={'pipeline_id': one['id'], 'account_id': a_account,
                                                'label': 'AG', 'url': 'https://www.facebook.com/groups/221/',
                                                'approved_target': True}).json()['id']
    b_account = client.post('/api/accounts', json={'pipeline_id': two['id'], 'label': 'Account B'}).json()['id']
    b_group = client.post('/api/groups', json={'pipeline_id': two['id'], 'account_id': b_account,
                                                'label': 'BG', 'url': 'https://www.facebook.com/groups/222/',
                                                'approved_target': True}).json()['id']
    ctv_one, response = login_ctv('ctv_a3', key=one_key)
    ctv_two, response = login_ctv('ctv_b3', key=two_key)
    assert response.status_code == 200

    assert [a['id'] for a in ctv_one.get('/api/accounts').json()] == [a_account]
    assert [a['id'] for a in ctv_two.get('/api/accounts').json()] == [b_account]
    assert ctv_one.get('/api/groups').json()[0]['id'] == a_group
    assert ctv_two.get('/api/groups').json()[0]['id'] == b_group
    assert ctv_one.post(f'/api/accounts/{b_account}/cookies',
                        files={'file': ('c.json', b'[]', 'application/json')}).status_code == 404
    assert ctv_two.post(f'/api/accounts/{a_account}/cookies',
                        files={'file': ('c.json', b'[]', 'application/json')}).status_code == 404
    assert ctv_one.delete(f'/api/accounts/{b_account}').status_code == 403
    assert ctv_two.delete(f'/api/accounts/{a_account}').status_code == 403
    assert ctv_one.patch(f'/api/groups/{b_group}', json={'enabled': False}).status_code == 404
    # uploaded media of one workspace is not downloadable from the other
    media = ctv_one.post('/api/uploads', params={'pipeline_id': one['id']},
                         files={'file': ('a.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    assert ctv_one.get('/api/media/' + media).status_code == 200
    assert ctv_two.get('/api/media/' + media).status_code == 404
    ctv_one.close()
    ctv_two.close()


# --- 12. the worker refuses a job whose rows disagree about the owner -------

def test_worker_refuses_a_job_that_mixes_pipelines(client):
    one = client.post('/api/admin/pipelines', json={'name': '@w1'}).json()['id']
    two = client.post('/api/admin/pipelines', json={'name': '@w2'}).json()['id']
    a_account = client.post('/api/accounts', json={'pipeline_id': one, 'label': 'Account A'}).json()['id']
    b_account = client.post('/api/accounts', json={'pipeline_id': two, 'label': 'Account B'}).json()['id']
    a_group = client.post('/api/groups', json={'pipeline_id': one, 'account_id': a_account, 'label': 'AG',
                                                'url': 'https://www.facebook.com/groups/551/',
                                                'approved_target': True}).json()['id']
    with m.db() as conn:
        post = conn.execute("INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)"
                            " VALUES(?,?,?,?,1)", (one, 'x', '[]', m.iso())).lastrowid
        # a post of pipeline one pointing at a group of pipeline two
        job = conn.execute("INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,scheduled_at,updated_at)"
                           " VALUES(?,?,?,?,'queued',?,?)",
                           (post, a_group, b_account, one, m.iso(), m.iso())).lastrowid
    assert m.job_ownership_ok(job) is False
    # the scheduler must not pick it up
    claimed = m.claim_next_job()
    assert claimed is None or claimed['id'] != job


def test_worker_accepts_a_consistent_job(client):
    one = client.post('/api/admin/pipelines', json={'name': '@w3'}).json()['id']
    a_account = client.post('/api/accounts', json={'pipeline_id': one, 'label': 'Account A'}).json()['id']
    a_group = client.post('/api/groups', json={'pipeline_id': one, 'account_id': a_account, 'label': 'AG',
                                                'url': 'https://www.facebook.com/groups/661/',
                                                'approved_target': True}).json()['id']
    with m.db() as conn:
        post = conn.execute("INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)"
                            " VALUES(?,?,?,?,1)", (one, 'x', '["uploads/x.jpg"]', m.iso())).lastrowid
        job = conn.execute("INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,scheduled_at,updated_at)"
                           " VALUES(?,?,?,?,'queued',?,?)",
                           (post, a_group, a_account, one, m.iso(), m.iso())).lastrowid
    assert m.job_ownership_ok(job) is True


# --- 13. existing V4 data survives the migration -----------------------------

def test_existing_data_survives_the_migration(client):
    pipeline = client.post('/api/pipelines', json={'name': '@legacy'}).json()['id']
    account = client.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Legacy FB'}).json()['id']
    group = client.post('/api/groups', json={'pipeline_id': pipeline, 'account_id': account, 'label': 'LG',
                                             'url': 'https://www.facebook.com/groups/112233/',
                                             'approved_target': True}).json()['id']
    with m.db() as conn:
        post = conn.execute("INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)"
                            " VALUES(?,?,?,?,1)", (pipeline, 'legacy body', '[]', m.iso())).lastrowid
        conn.execute("INSERT INTO jobs(post_id,group_id,account_id,scheduled_at,updated_at) VALUES(?,?,?,?,?)",
                     (post, group, account, m.iso(), m.iso()))
    m.init_db()
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pipelines WHERE id=?", (pipeline,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE id=?", (account,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM groups WHERE id=?", (group,)).fetchone()[0] == 1
        row = conn.execute("SELECT pipeline_id FROM jobs WHERE post_id=?", (post,)).fetchone()
        assert row['pipeline_id'] == pipeline
        assert conn.execute("SELECT body FROM posts WHERE id=?", (post,)).fetchone()['body'] == 'legacy body'


# --- 14. concurrent access neither duplicates nor leaks ----------------------

def test_concurrent_access_does_not_duplicate_or_leak(client):
    pipeline, key, _ = make_tenant(client, '@race', 'ctv_race',
                                   permissions=['facebook.view', 'groups.view', 'history.view'])
    client.post('/api/accounts', json={'pipeline_id': pipeline['id'], 'label': 'Race account'})
    ctv, response = login_ctv('ctv_race', key=key)
    assert response.status_code == 200
    results = []
    lock = threading.Lock()

    def reader():
        payload = ctv.get('/api/accounts').json()
        with lock:
            results.append(payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: reader(), range(24)))
    assert len(results) == 24
    for payload in results:
        assert len(payload) == 1
        assert payload[0]['pipeline_id'] == pipeline['id']
    # creating the same username concurrently must produce exactly one account
    created = []

    def creator(index):
        response = client.post('/api/admin/users', json={
            'username': 'ctv_race_dup', 'password': CTV_PASSWORD, 'pipeline_id': pipeline['id']})
        with lock:
            created.append(response.status_code)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(creator, range(6)))
    assert created.count(201) == 1
    assert all(code in (201, 409) for code in created)
    names = [u['username'] for u in client.get('/api/admin/users').json()]
    assert names.count('ctv_race_dup') == 1
    ctv.close()


# --- session and transport guarantees ---------------------------------------

def test_mutating_requests_require_a_valid_csrf_token(client):
    raw = TestClient(m.app)
    raw.post('/api/auth/login', json={'username': 'admin', 'password': os.environ['APP_PASSWORD']})
    # the session cookie is valid but the CSRF header is missing
    assert raw.post('/api/pipelines', json={'name': '@csrf'}).status_code == 403
    raw.headers['X-CSRF-Token'] = 'wrong-token'
    assert raw.post('/api/pipelines', json={'name': '@csrf'}).status_code == 403
    raw.close()
    assert client.post('/api/pipelines', json={'name': '@csrf'}).status_code == 201


def test_session_cookie_is_httponly_and_samesite_strict(client):
    response = client.post('/api/auth/login', json={
        'username': 'admin', 'password': os.environ['APP_PASSWORD']})
    raw = response.headers.get('set-cookie', '')
    assert 'fpm_session=' in raw
    assert 'HttpOnly' in raw
    assert 'samesite=strict' in raw.lower()


def test_login_rate_limit_blocks_repeated_failures(client):
    au.LOGIN_THROTTLE.clear('127.0.0.1', 'ratelimited')
    for _ in range(au.LOGIN_MAX_FAILURES):
        client.post('/api/auth/login', json={'username': 'ratelimited', 'password': 'wrong'})
    blocked = client.post('/api/auth/login', json={'username': 'ratelimited', 'password': 'wrong'})
    assert blocked.status_code == 429
    au.LOGIN_THROTTLE.clear('127.0.0.1', 'ratelimited')


def test_logout_and_session_revocation(client):
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/accounts').status_code == 401


def test_administrator_can_revoke_a_collaborator_sessions(client):
    pipeline, key, _ = make_tenant(client, '@revokesessions', 'ctv_rs',
                                   permissions=['facebook.view'])
    ctv, response = login_ctv('ctv_rs', key=key)
    assert response.status_code == 200
    user_id = next(u['id'] for u in client.get('/api/admin/users').json() if u['username'] == 'ctv_rs')
    with m.db() as conn:
        revoked = m.revoke_user_sessions(conn, user_id)
    assert revoked >= 1
    assert ctv.get('/api/accounts').status_code == 401
    ctv.close()


def test_weak_admin_password_is_flagged_for_rotation(client):
    assert m.weak_password('admin123456789') is True
    assert m.weak_password('short') is True
    assert m.weak_password('a-valid-very-long-test-password') is False


def test_admin_password_change_requires_the_current_password(client):
    assert client.post('/api/auth/change-password', json={
        'current_password': 'wrong', 'new_password': 'brand-new-strong-password'}).status_code == 401
    assert client.post('/api/auth/change-password', json={
        'current_password': os.environ['APP_PASSWORD'], 'new_password': 'short'}).status_code == 422
