"""Group management: modes, isolation, checks and the publishing queue."""
import asyncio
import json

import pytest
from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m
import fb_browser
from services import posting_mode
from services.group_links import InvalidGroupLink, normalize_group_url


def make_pipeline(client, name):
    response = client.post('/api/pipelines', json={'name': name})
    assert response.status_code == 201, response.text
    return response.json()['id']


def make_account(client, pipeline_id, label="Facebook chính"):
    response = client.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label})
    assert response.status_code == 201, response.text
    return response.json()['id']


def add_group(client, pipeline_id, account_id, url="https://www.facebook.com/groups/123456789/",
              mode="anonymous", nickname=""):
    return client.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': 'Group thử',
        'url': url, 'posting_mode': mode, 'nickname': nickname, 'approved_target': True})


# --- 1. add one group by link ------------------------------------------------

def test_add_group_by_link(client):
    pipeline = make_pipeline(client, '@grp1')
    account = make_account(client, pipeline)
    response = add_group(client, pipeline, account)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['url'] == 'https://www.facebook.com/groups/123456789/'
    assert body['posting_mode'] == 'anonymous'
    assert body['account_id'] == account
    assert body['posted_count'] == 0
    listing = client.get(f'/api/pipelines/{pipeline}/groups').json()
    assert len(listing) == 1
    assert listing[0]['id'] == body['id']


def test_group_link_is_normalised(client):
    pipeline = make_pipeline(client, '@grp2')
    account = make_account(client, pipeline)
    response = add_group(client, pipeline, account,
                         url="https://m.facebook.com/groups/987654321/?fbclid=xyz&utm_source=share")
    assert response.status_code == 201
    assert response.json()['url'] == 'https://www.facebook.com/groups/987654321/'
    assert "fbclid" not in response.json()['url']


def test_a_shared_group_link_and_its_numeric_form_are_the_same_group(client):
    """Facebook shares a group as /share/g/<code> and keeps it under /groups/<id>."""
    pipeline = make_pipeline(client, '@grp2b')
    account = make_account(client, pipeline)
    shared = add_group(client, pipeline, account,
                       url="https://www.facebook.com/share/g/FixtureShareAb12/?mibextid=wwXIfr")
    assert shared.status_code == 201, shared.text
    assert shared.json()['url'] == 'https://www.facebook.com/share/g/FixtureShareAb12/'
    numeric = add_group(client, pipeline, account, url=(
        "https://www.facebook.com/groups/1234567890123456/?ref=share&mibextid=wwXIfr"
        "&share_url=https%3A%2F%2Fwww.facebook.com%2Fshare%2Fg%2FFixtureShareAb12%2F"))
    assert numeric.status_code == 409
    assert len(client.get(f'/api/pipelines/{pipeline}/groups').json()) == 1


def test_invalid_group_link_is_refused(client):
    pipeline = make_pipeline(client, '@grp3')
    account = make_account(client, pipeline)
    for bad in ("https://evil.example/groups/1", "http://www.facebook.com/groups/1",
                "https://www.facebook.com/username/x", "https://www.facebook.com/groups/"):
        assert add_group(client, pipeline, account, url=bad).status_code == 422


# --- 2. add many groups from a list -----------------------------------------

def test_bulk_add_reports_valid_invalid_and_duplicate(client):
    pipeline = make_pipeline(client, '@grp4')
    account = make_account(client, pipeline)
    add_group(client, pipeline, account, url="https://www.facebook.com/groups/111111111/")
    response = client.post(f'/api/pipelines/{pipeline}/groups/bulk', json={
        'urls': "https://www.facebook.com/groups/111111111/\n"
                "https://www.facebook.com/groups/222222222/\n"
                "not a link at all\n"
                "https://www.facebook.com/groups/222222222/\n"
                "333333333",
        'account_id': account, 'posting_mode': 'anonymous', 'approved_target': True})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['added_count'] == 2
    # one duplicate already in the workspace plus the same link repeated
    assert body['duplicate_count'] == 2
    assert body['invalid_count'] == 1
    assert len(client.get(f'/api/pipelines/{pipeline}/groups').json()) == 3


# --- 3. duplicates are blocked ----------------------------------------------

def test_duplicate_group_is_blocked(client):
    pipeline = make_pipeline(client, '@grp5')
    account = make_account(client, pipeline)
    assert add_group(client, pipeline, account).status_code == 201
    again = add_group(client, pipeline, account, url="https://www.facebook.com/groups/123456789")
    assert again.status_code == 409
    # the same group in another pipeline is allowed
    other = make_pipeline(client, '@grp6')
    other_account = make_account(client, other)
    assert add_group(client, other, other_account).status_code == 201


# --- 4. the right Facebook account is assigned -------------------------------

def test_account_must_belong_to_the_pipeline(client):
    pipeline = make_pipeline(client, '@grp7')
    other = make_pipeline(client, '@grp8')
    other_account = make_account(client, other)
    response = add_group(client, pipeline, other_account)
    assert response.status_code == 404
    assert client.get(f'/api/pipelines/{pipeline}/groups').json() == []


def test_group_keeps_its_account_and_mode(client):
    pipeline = make_pipeline(client, '@grp9')
    account = make_account(client, pipeline)
    group = add_group(client, pipeline, account, mode="nickname",
                      nickname="Hội giao lưu").json()
    assert group['account_id'] == account
    assert group['posting_mode'] == 'nickname'
    assert group['nickname'] == 'Hội giao lưu'
    assert group['accounts'][0]['account_id'] == account


def test_nickname_mode_requires_a_nickname(client):
    pipeline = make_pipeline(client, '@grp10')
    account = make_account(client, pipeline)
    assert add_group(client, pipeline, account, mode="nickname").status_code == 422


# --- 5. and 6. the anonymous mode is enforced or the post stops ------------

def test_anonymous_identity_is_accepted_when_verified(monkeypatch):
    async def fake_anonymous(page):
        return True
    monkeypatch.setattr(posting_mode, "require_anonymous_mode", fake_anonymous)
    identity, verification, error = asyncio.run(
        posting_mode.enforce_posting_identity(None, 1, 1, 1, "anonymous", ""))
    assert identity == "anonymous"
    assert verification == "verified"
    assert error == ""


def test_anonymous_post_stops_when_the_mode_cannot_be_verified(monkeypatch):
    async def fake_anonymous(page):
        return False
    monkeypatch.setattr(posting_mode, "require_anonymous_mode", fake_anonymous)
    identity, verification, error = asyncio.run(
        posting_mode.enforce_posting_identity(None, 1, 1, 1, "anonymous", ""))
    assert identity == ""
    assert verification == "not_verified"
    assert "tên thật" in error.lower()


# --- 7. and 8. the nickname identity ----------------------------------------

def test_valid_nickname_is_selected(monkeypatch):
    async def fake(page, pipeline_id, account_id, group_id, configured_nickname):
        return "Hội giao lưu", ["Hội giao lưu", "Tên thật"], "saved"
    monkeypatch.setattr(posting_mode, "require_nickname_identity", fake)
    identity, verification, error = asyncio.run(
        posting_mode.enforce_posting_identity(None, 1, 1, 1, "nickname", "Hội giao lưu"))
    assert identity == "Hội giao lưu"
    assert verification == "verified"
    assert error == ""


def test_post_stops_when_the_nickname_is_not_available(monkeypatch):
    async def fake(page, pipeline_id, account_id, group_id, configured_nickname):
        return "", ["Tên thật", "Nickname khác"], "not_found"
    monkeypatch.setattr(posting_mode, "require_nickname_identity", fake)
    identity, verification, error = asyncio.run(
        posting_mode.enforce_posting_identity(None, 1, 1, 1, "nickname", "Hội giao lưu"))
    assert identity == ""
    assert verification == "not_verified"
    assert "tên thật" in error.lower()


# --- 9. there is never a fallback to the real name -------------------------

def test_no_fallback_to_the_profile_name(monkeypatch):
    async def fake_nickname(page, pipeline_id, account_id, group_id, configured_nickname):
        return "", [], "not_found"
    async def fake_anonymous(page):
        return False
    monkeypatch.setattr(posting_mode, "require_nickname_identity", fake_nickname)
    monkeypatch.setattr(posting_mode, "require_anonymous_mode", fake_anonymous)
    for mode, nickname in (("nickname", "X"), ("anonymous", "")):
        identity, _, error = asyncio.run(
            posting_mode.enforce_posting_identity(None, 1, 1, 1, mode, nickname))
        assert identity == ""
        assert error


def test_unknown_mode_is_refused(monkeypatch):
    identity, _, error = asyncio.run(
        posting_mode.enforce_posting_identity(None, 1, 1, 1, "real_name", ""))
    assert identity == ""
    assert "không hợp lệ" in error


# --- 10. groups of another pipeline are out of reach ------------------------

def test_group_of_another_pipeline_is_hidden(client):
    mine = make_pipeline(client, '@grp11')
    theirs = make_pipeline(client, '@grp12')
    my_account = make_account(client, mine)
    their_account = make_account(client, theirs)
    their_group = add_group(client, theirs, their_account).json()
    assert client.get(f'/api/pipelines/{mine}/groups').json() == []
    assert client.patch(f'/api/pipelines/{mine}/groups/{their_group["id"]}',
                        json={'posting_mode': 'nickname'}).status_code == 404
    assert client.delete(f'/api/pipelines/{mine}/groups/{their_group["id"]}').status_code == 404
    assert client.post(f'/api/pipelines/{mine}/groups/{their_group["id"]}/check').status_code == 404


def test_collaborator_cannot_manage_groups_without_permission(client):
    pipeline = make_pipeline(client, '@grp13')
    account = make_account(client, pipeline)
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_grp', 'password': 'ctv-group-password-2026',
                                          'pipeline_id': pipeline, 'permissions': ['groups.view']})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_grp', 'password': 'ctv-group-password-2026',
                                               'pipeline_key': key})
    assert login.status_code == 200
    ctv.csrf = login.json()['csrf_token']
    assert ctv.get(f'/api/pipelines/{pipeline}/groups').status_code == 200
    assert ctv.post(f'/api/pipelines/{pipeline}/groups', json={
        'pipeline_id': pipeline, 'account_id': account, 'label': 'Group X',
        'url': 'https://www.facebook.com/groups/9/',
        'posting_mode': 'anonymous'}).status_code == 403
    assert ctv.post(f'/api/pipelines/{pipeline}/groups/bulk',
                     json={'urls': 'https://www.facebook.com/groups/9/'}).status_code == 403
    ctv.close()


# --- 11. and 12. the publishing queue ---------------------------------------

def test_one_job_per_group_with_its_own_identity(client):
    pipeline = make_pipeline(client, '@grp14')
    account = make_account(client, pipeline)
    group = add_group(client, pipeline, account, mode="nickname", nickname="Biệt danh").json()
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('a.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + __import__('datetime').timedelta(hours=2))
    response = client.post(f'/api/pipelines/{pipeline}/group-posts', json={
        'group_ids': [group['id']], 'body': 'Xin chào', 'media': [image], 'scheduled_at': scheduled})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['jobs_created'] == 1
    with m.db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (body['jobs'][0]['job_id'],)).fetchone()
    assert job['requested_posting_mode'] == 'nickname'
    assert job['requested_nickname'] == 'Biệt danh'
    assert job['account_id'] == account
    assert job['group_id'] == group['id']
    assert job['pipeline_id'] == pipeline


def test_no_duplicate_job_for_the_same_group(client):
    pipeline = make_pipeline(client, '@grp15')
    account = make_account(client, pipeline)
    group = add_group(client, pipeline, account).json()
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('b.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + __import__('datetime').timedelta(hours=2))
    payload = {'group_ids': [group['id']], 'body': 'Xin chào', 'media': [image], 'scheduled_at': scheduled}
    first = client.post(f'/api/pipelines/{pipeline}/group-posts', json=payload)
    assert first.json()['jobs_created'] == 1
    second = client.post(f'/api/pipelines/{pipeline}/group-posts', json=payload)
    assert second.json()['jobs_created'] == 0
    assert second.json()['skipped'][0]['reason']
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE group_id=?", (group['id'],)).fetchone()[0]
    assert count == 1


def test_a_paused_group_is_skipped(client):
    pipeline = make_pipeline(client, '@grp16')
    account = make_account(client, pipeline)
    group = add_group(client, pipeline, account).json()
    client.patch(f'/api/pipelines/{pipeline}/groups/{group["id"]}', json={'status': 'paused'})
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('c.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + __import__('datetime').timedelta(hours=2))
    response = client.post(f'/api/pipelines/{pipeline}/group-posts', json={
        'group_ids': [group['id']], 'body': 'Xin chào', 'media': [image], 'scheduled_at': scheduled})
    assert response.json()['jobs_created'] == 0
    assert response.json()['skipped'][0]['reason']


# --- 13. a post waiting for approval is never reported as posted ------------

def test_pending_approval_is_not_posted(client):
    pipeline = make_pipeline(client, '@grp17')
    account = make_account(client, pipeline)
    group = add_group(client, pipeline, account).json()
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('d.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + __import__('datetime').timedelta(hours=2))
    response = client.post(f'/api/pipelines/{pipeline}/group-posts', json={
        'group_ids': [group['id']], 'body': 'Xin chào', 'media': [image], 'scheduled_at': scheduled})
    job_id = response.json()['jobs'][0]['job_id']
    with m.db() as conn:
        conn.execute("UPDATE jobs SET state='submitted_unverified' WHERE id=?", (job_id,))
        conn.commit()
    # the worker outcome is mapped to a pending approval when the group needs one
    with m.db() as conn:
        conn.execute("UPDATE groups SET requires_approval=1 WHERE id=?", (group['id'],))
        conn.commit()
    with m.db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert job['state'] == 'submitted_unverified'
    assert job['posting_status'] == 'pending'


# --- 14. old data survives the migration ------------------------------------

def test_existing_data_survives_the_migration(client):
    pipeline = make_pipeline(client, '@grp18')
    account = make_account(client, pipeline)
    group = add_group(client, pipeline, account).json()
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('e.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + __import__('datetime').timedelta(hours=2))
    post = client.post(f'/api/pipelines/{pipeline}/group-posts', json={
        'group_ids': [group['id']], 'body': 'Bài cũ', 'media': [image], 'scheduled_at': scheduled}).json()
    m.init_db()
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM groups WHERE id=?", (group['id'],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM posts WHERE id=?", (post['post_id'],)).fetchone()[0] == 1
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (post['jobs'][0]['job_id'],)).fetchone()
    assert job['state'] == 'queued'
    assert job['pipeline_id'] == pipeline
    assert job['requested_posting_mode'] == 'anonymous'


# --- helpers -----------------------------------------------------------------

def _async_true():
    async def value():
        return True
    return value()


def _async_false():
    async def value():
        return False
    return value()


def test_link_normalisation_helper():
    assert normalize_group_url("https://www.facebook.com/groups/123/")[1] == "123"
    with pytest.raises(InvalidGroupLink):
        normalize_group_url("https://www.facebook.com/groups/")
