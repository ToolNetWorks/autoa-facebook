"""V6: every collaborator owns the resources they create.

Two collaborators of the same workspace must not see each other's accounts,
groups, pictures or tasks. Resources created by the administrator stay in the
shared pool of the workspace and remain visible to everyone.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m

CTV_PASSWORD = "ctv-password-2026-strong"


def make_workspace(admin, name):
    """A pipeline with two collaborators and one shared account."""
    created = admin.post('/api/admin/pipelines', json={'name': name})
    assert created.status_code == 201, created.text
    pipeline = created.json()
    pipeline_id = pipeline['id']
    for username in ('ctv_alpha', 'ctv_beta'):
        member = admin.post('/api/admin/users', json={
            'username': username, 'password': CTV_PASSWORD, 'pipeline_id': pipeline_id})
        assert member.status_code == 201, member.text
    return pipeline_id, pipeline['key']


def login_ctv(username, key):
    ctv = AuthClient(m.app)
    response = ctv.post('/api/auth/login', json={
        'username': username, 'password': CTV_PASSWORD, 'pipeline_key': key})
    assert response.status_code == 200, response.text
    ctv.csrf = response.json().get('csrf_token', '')
    return ctv


def add_account(owner, pipeline_id, label):
    response = owner.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label})
    assert response.status_code == 201, response.text
    return response.json()['id']


def add_group(owner, pipeline_id, account_id, url, label):
    response = owner.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'url': url, 'label': label,
        'posting_mode': 'anonymous'})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_collaborator_accounts_stay_private(client):
    pipeline_id, key = make_workspace(client, '@own_accounts')
    alpha = login_ctv('ctv_alpha', key)
    beta = login_ctv('ctv_beta', key)

    alpha_account = add_account(alpha, pipeline_id, 'Alpha FB')
    beta_account = add_account(beta, pipeline_id, 'Beta FB')

    alpha_rows = alpha.get(f'/api/accounts?pipeline_id={pipeline_id}').json()
    labels = sorted(row['label'] for row in alpha_rows)
    assert labels == ['Alpha FB'], alpha_rows

    beta_rows = beta.get(f'/api/accounts?pipeline_id={pipeline_id}').json()
    assert sorted(row['label'] for row in beta_rows) == ['Beta FB']

    # the administrator supervises the whole workspace
    admin_rows = client.get(f'/api/accounts?pipeline_id={pipeline_id}').json()
    assert sorted(row['label'] for row in admin_rows) == ['Alpha FB', 'Beta FB']

    # the owner knows which rows are private
    owned = next(row for row in alpha_rows if row['id'] == alpha_account)
    assert owned['owner_user_id'] == alpha_rows[0]['owner_user_id']
    assert owned['owner_user_id'] is not None


def test_collaborator_groups_stay_private(client):
    pipeline_id, key = make_workspace(client, '@own_groups')
    alpha = login_ctv('ctv_alpha', key)
    beta = login_ctv('ctv_beta', key)

    alpha_account = add_account(alpha, pipeline_id, 'Alpha FB')
    beta_account = add_account(beta, pipeline_id, 'Beta FB')

    alpha_group = add_group(alpha, pipeline_id, alpha_account,
                            'https://www.facebook.com/groups/111111/', 'Alpha Group')
    beta_group = add_group(beta, pipeline_id, beta_account,
                            'https://www.facebook.com/groups/222222/', 'Beta Group')

    alpha_rows = alpha.get(f'/api/pipelines/{pipeline_id}/groups').json()
    assert [row['label'] for row in alpha_rows] == ['Alpha Group'], alpha_rows
    beta_rows = beta.get(f'/api/pipelines/{pipeline_id}/groups').json()
    assert [row['label'] for row in beta_rows] == ['Beta Group']

    # a group of another collaborator is hidden completely
    assert alpha.patch(f'/api/pipelines/{pipeline_id}/groups/{beta_group}',
                       json={'enabled': False}).status_code == 404
    assert beta.patch(f'/api/pipelines/{pipeline_id}/groups/{alpha_group}',
                      json={'enabled': False}).status_code == 404

    # the administrator sees both
    admin_rows = client.get(f'/api/pipelines/{pipeline_id}/groups').json()
    assert sorted(row['label'] for row in admin_rows) == ['Alpha Group', 'Beta Group']


def test_two_collaborators_may_target_the_same_group(client):
    pipeline_id, key = make_workspace(client, '@same_group')
    alpha = login_ctv('ctv_alpha', key)
    beta = login_ctv('ctv_beta', key)

    alpha_account = add_account(alpha, pipeline_id, 'Alpha FB')
    beta_account = add_account(beta, pipeline_id, 'Beta FB')

    url = 'https://www.facebook.com/groups/333333/'
    alpha_group = add_group(alpha, pipeline_id, alpha_account, url, 'Alpha view')
    beta_group = add_group(beta, pipeline_id, beta_account, url, 'Beta view')

    assert alpha_group != beta_group
    assert [row['label'] for row in alpha.get(f'/api/pipelines/{pipeline_id}/groups').json()] == ['Alpha view']
    assert [row['label'] for row in beta.get(f'/api/pipelines/{pipeline_id}/groups').json()] == ['Beta view']


def test_shared_pool_stays_visible_to_every_collaborator(client):
    pipeline_id, key = make_workspace(client, '@shared_pool')
    alpha = login_ctv('ctv_alpha', key)
    beta = login_ctv('ctv_beta', key)

    shared_account = add_account(client, pipeline_id, 'Shared FB')
    shared_group = add_group(client, pipeline_id, shared_account,
                              'https://www.facebook.com/groups/444444/', 'Shared Group')

    alpha_rows = alpha.get(f'/api/accounts?pipeline_id={pipeline_id}').json()
    assert [row['label'] for row in alpha_rows] == ['Shared FB']
    assert alpha_rows[0]['owner_user_id'] is None

    alpha_groups = alpha.get(f'/api/pipelines/{pipeline_id}/groups').json()
    assert [row['label'] for row in alpha_groups] == ['Shared Group']
    assert alpha_groups[0]['shared'] is True
    assert alpha.patch(f'/api/pipelines/{pipeline_id}/groups/{shared_group}',
                       json={'label': 'Shared Group'}).status_code == 200

    beta_groups = beta.get(f'/api/pipelines/{pipeline_id}/groups').json()
    assert [row['label'] for row in beta_groups] == ['Shared Group']


def test_collaborator_cannot_use_a_foreign_account(client):
    pipeline_id, key = make_workspace(client, '@foreign_account')
    alpha = login_ctv('ctv_alpha', key)
    beta = login_ctv('ctv_beta', key)
    alpha_account = add_account(alpha, pipeline_id, 'Alpha FB')

    # beta may not attach a group to the account of alpha
    response = beta.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': alpha_account,
        'url': 'https://www.facebook.com/groups/555555/', 'label': 'Beta Group',
        'posting_mode': 'anonymous'})
    assert response.status_code == 404, response.text


def test_collaborator_without_permission_cannot_manage_groups(client):
    pipeline_id, key = make_workspace(client, '@no_manage')
    member = client.get('/api/admin/users').json()
    target = next(row for row in member if row['username'] == 'ctv_beta')
    client.patch(f'/api/admin/users/{target["id"]}', json={
        'permissions': ['groups.view', 'facebook.view']})

    beta = login_ctv('ctv_beta', key)
    shared = add_account(client, pipeline_id, 'Shared FB')
    response = beta.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': shared,
        'url': 'https://www.facebook.com/groups/666666/', 'label': 'No permission',
        'posting_mode': 'anonymous'})
    assert response.status_code == 403, response.text


def test_manual_tasks_and_pictures_follow_the_owner(client):
    pipeline_id, key = make_workspace(client, '@own_tasks')
    member = client.get('/api/admin/users').json()
    alpha_row = next(row for row in member if row['username'] == 'ctv_alpha')
    client.patch(f'/api/admin/users/{alpha_row["id"]}', json={
        'permissions': ['facebook.view', 'facebook.add', 'groups.view', 'groups.manage',
                        'comments.manage', 'history.view']})
    alpha = login_ctv('ctv_alpha', key)
    beta = login_ctv('ctv_beta', key)

    alpha_account = add_account(alpha, pipeline_id, 'Alpha FB')
    beta_account = add_account(beta, pipeline_id, 'Beta FB')

    from datetime import timedelta
    task = alpha.post('/api/manual-tasks', json={
        'pipeline_id': pipeline_id, 'account_id': alpha_account, 'task_type': 'comment',
        'body': 'Bình luận riêng của alpha', 'target_url': 'https://www.facebook.com/somebody/posts/1',
        'scheduled_at': m.iso(m.now() + timedelta(hours=1))})
    assert task.status_code == 201, task.text

    alpha_rows = alpha.get(f'/api/manual-tasks?pipeline_id={pipeline_id}').json()
    assert len(alpha_rows) == 1
    assert alpha_rows[0]['body'] == 'Bình luận riêng của alpha'

    beta_rows = beta.get(f'/api/manual-tasks?pipeline_id={pipeline_id}').json()
    assert beta_rows == []

    admin_rows = client.get(f'/api/manual-tasks?pipeline_id={pipeline_id}').json()
    assert len(admin_rows) == 1
    assert beta_account  # the beta account stays untouched


def test_collaborator_default_permissions_allow_own_groups(client):
    """A new collaborator may add accounts and groups without the admin."""
    pipeline_id, key = make_workspace(client, '@defaults_v6')
    alpha = login_ctv('ctv_alpha', key)
    account = add_account(alpha, pipeline_id, 'Alpha FB')
    group_id = add_group(alpha, pipeline_id, account,
                         'https://www.facebook.com/groups/777777/', 'Alpha Group')
    assert group_id > 0

    member = client.get('/api/admin/users').json()
    target = next(row for row in member if row['username'] == 'ctv_alpha')
    assert 'groups.manage' in target['permissions']
    assert 'facebook.add' in target['permissions']
