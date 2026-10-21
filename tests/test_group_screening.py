"""Automatic group screening: classification, evidence, priority and isolation."""
import json

import pytest
from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m
from services.group_screening import (
    STATUS_FAILED, STATUS_PRIORITY, STATUS_REVIEW, STATUS_RESTRICTED, STATUS_UNKNOWN,
    classify, is_publishable, priority_index,
)


def make_pipeline(client, name):
    response = client.post('/api/pipelines', json={'name': name})
    assert response.status_code == 201, response.text
    return response.json()['id']


def make_account(client, pipeline_id, label="Facebook"):
    response = client.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label})
    assert response.status_code == 201, response.text
    return response.json()['id']


def make_group(client, pipeline_id, account_id, url="https://www.facebook.com/groups/555/"):
    response = client.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': 'Group',
        'url': url, 'posting_mode': 'anonymous', 'approved_target': True})
    assert response.status_code == 201, response.text
    return response.json()


def mark_direct_post(conn, pipeline_id, account_id, group_id):
    """A job the operator confirmed as published is direct evidence.

    It is dated two days back so the anti spam gap and the daily limit do not
    hold back the job that the test wants to see claimed.
    """
    when = m.iso(m.now() - m.timedelta(days=2))
    conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)
                    VALUES(?,?,?,?,1)""", (pipeline_id, "bài đã đăng", "[]", when))
    post_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,
                    scheduled_at,updated_at,requested_posting_mode,posting_status,verification_status)
                    VALUES(?,?,?,?,'posted','người dùng đã xác nhận',?,?,'anonymous','posted','verified')""",
                 (post_id, group_id, account_id, pipeline_id, when, when))
    return post_id


def fake_probe(monkeypatch, outcome):
    calls = []

    def probe(account_id, url, nickname):
        calls.append((account_id, url, nickname))
        return outcome

    monkeypatch.setattr(m, "run_group_probe", probe)
    return calls


# --- the classifier ---------------------------------------------------------

def test_group_with_direct_posts_is_priority():
    status, source, confidence = classify({
        "check_status": "ok", "accessible": True, "is_member": True, "can_post": True,
        "requires_approval": False, "direct_posts": 2,
        "last_direct_post_at": m.iso(m.now() - m.timedelta(days=1))})
    assert status == STATUS_PRIORITY
    assert source == "post_evidence"
    assert 0 < confidence < 1


def test_one_successful_post_is_evidence_but_not_a_guarantee():
    status, _, confidence = classify({
        "check_status": "ok", "accessible": True, "is_member": True, "can_post": True,
        "direct_posts": 1, "last_direct_post_at": m.iso(m.now() - m.timedelta(days=1))})
    assert status == STATUS_PRIORITY
    assert confidence <= 0.8


def test_old_direct_post_is_not_priority_anymore():
    status, _, _ = classify({
        "check_status": "ok", "accessible": True, "is_member": True, "can_post": True,
        "direct_posts": 3, "last_direct_post_at": m.iso(m.now() - m.timedelta(days=120))})
    assert status == STATUS_UNKNOWN


def test_group_that_needs_approval_is_review():
    status, source, _ = classify({
        "check_status": "ok", "accessible": True, "is_member": True, "can_post": True,
        "requires_approval": True})
    assert status == STATUS_REVIEW
    assert source == "browser_check"


def test_group_with_pending_posts_is_review():
    status, source, _ = classify({
        "check_status": "ok", "accessible": True, "is_member": True, "can_post": True,
        "pending_posts": 1, "last_pending_post_at": m.iso(m.now() - m.timedelta(days=1))})
    assert status == STATUS_REVIEW
    assert source == "post_evidence"


def test_group_without_evidence_is_unknown():
    status, source, confidence = classify({
        "check_status": "unknown", "accessible": None, "is_member": None, "can_post": None})
    assert status == STATUS_UNKNOWN
    assert source == "none"
    assert confidence == 0


def test_group_without_access_is_restricted():
    status, source, _ = classify({
        "check_status": "restricted", "accessible": False, "is_member": False, "can_post": False})
    assert status == STATUS_RESTRICTED
    assert source == "browser_check"


def test_group_without_membership_is_restricted():
    status, _, _ = classify({"check_status": "ok", "accessible": True, "is_member": False,
                              "can_post": False})
    assert status == STATUS_RESTRICTED


def test_group_without_composer_is_restricted():
    status, _, _ = classify({"check_status": "ok", "accessible": True, "is_member": True,
                              "can_post": False})
    assert status == STATUS_RESTRICTED


def test_rejection_after_a_good_post_is_restricted():
    status, source, _ = classify({
        "check_status": "ok", "accessible": True, "is_member": True, "can_post": True,
        "direct_posts": 3, "last_direct_post_at": m.iso(m.now() - m.timedelta(days=5)),
        "rejected_posts": 1, "last_rejected_post_at": m.iso(m.now() - m.timedelta(days=1))})
    assert status == STATUS_RESTRICTED
    assert source == "post_evidence"


def test_failed_check_is_failed():
    status, _, _ = classify({"check_status": "error"})
    assert status == STATUS_FAILED


def test_manual_review_overrides_everything():
    status, source, _ = classify({"check_status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "manual_review": True,
                                  "direct_posts": 9, "last_direct_post_at": m.iso()})
    assert status == STATUS_UNKNOWN
    assert source == "manual"


def test_priority_order_and_publishable():
    assert priority_index(STATUS_PRIORITY) < priority_index(STATUS_REVIEW)
    assert priority_index(STATUS_REVIEW) < priority_index(STATUS_UNKNOWN)
    assert priority_index(STATUS_UNKNOWN) < priority_index(STATUS_RESTRICTED)
    assert is_publishable(STATUS_PRIORITY) is True
    assert is_publishable(STATUS_REVIEW) is True
    assert is_publishable(STATUS_UNKNOWN) is False
    assert is_publishable(STATUS_RESTRICTED) is False
    assert is_publishable(STATUS_FAILED) is False


# --- the screening of one group ---------------------------------------------

def test_screening_stores_the_classification(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr1')
    account = make_account(client, pipeline)
    group = make_group(client, pipeline, account)
    calls = fake_probe(monkeypatch, {"status": "ok", "accessible": True, "is_member": True,
                                     "can_post": True, "requires_approval": True,
                                     "group_name": "Group thật", "details": ""})
    with m.db() as conn:
        result = m.store_screening_result(conn, pipeline, account, group['id'],
                                          {"status": "ok", "accessible": True, "is_member": True,
                                           "can_post": True, "requires_approval": True})
    assert result['status'] == STATUS_REVIEW
    with m.db() as conn:
        row = conn.execute("SELECT * FROM group_screening_results WHERE group_id=?",
                            (group['id'],)).fetchone()
        stored = conn.execute("SELECT screening_status FROM groups WHERE id=?", (group['id'],)).fetchone()
    assert row['moderation_status'] == STATUS_REVIEW
    assert row['verification_source'] == 'browser_check'
    assert stored['screening_status'] == STATUS_REVIEW


def test_screening_results_endpoint_lists_the_groups(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr2')
    account = make_account(client, pipeline)
    group = make_group(client, pipeline, account)
    fake_probe(monkeypatch, {"status": "ok", "accessible": True, "is_member": True,
                              "can_post": True, "requires_approval": False, "details": ""})
    with m.db() as conn:
        m.store_screening_result(conn, pipeline, account, group['id'],
                                 {"status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "requires_approval": False})
    response = client.get(f'/api/pipelines/{pipeline}/groups/screening-results')
    assert response.status_code == 200
    body = response.json()
    assert body['summary']['total'] == 1
    assert body['results'][0]['group_id'] == group['id']


def test_priority_endpoint_orders_the_groups(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr3')
    account = make_account(client, pipeline)
    priority = make_group(client, pipeline, account, url="https://www.facebook.com/groups/111/")['id']
    review = make_group(client, pipeline, account, url="https://www.facebook.com/groups/222/")['id']
    unknown = make_group(client, pipeline, account, url="https://www.facebook.com/groups/333/")['id']
    with m.db() as conn:
        mark_direct_post(conn, pipeline, account, priority)
        m.store_screening_result(conn, pipeline, account, priority,
                                 {"status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "requires_approval": False})
        m.store_screening_result(conn, pipeline, account, review,
                                 {"status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "requires_approval": True})
    response = client.get(f'/api/pipelines/{pipeline}/groups/priority')
    assert response.status_code == 200
    order = [item['id'] for item in response.json()['groups']]
    assert order.index(priority) < order.index(review)
    assert order.index(review) < order.index(unknown)


def test_two_accounts_in_one_group_can_have_different_states(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr4')
    first = make_account(client, pipeline, "Facebook A")
    second = make_account(client, pipeline, "Facebook B")
    group = make_group(client, pipeline, first)
    with m.db() as conn:
        m.store_screening_result(conn, pipeline, first, group['id'],
                                 {"status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "requires_approval": False})
        m.store_screening_result(conn, pipeline, second, group['id'],
                                 {"status": "restricted", "accessible": False})
    results = client.get(f'/api/pipelines/{pipeline}/groups/screening-results').json()['results']
    by_account = {item['account_id']: item['moderation_status'] for item in results}
    assert by_account[first] == STATUS_UNKNOWN
    assert by_account[second] == STATUS_RESTRICTED


# --- the publishing queue respects the screening ----------------------------

def test_a_job_of_an_unknown_group_is_not_published(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr5')
    account = make_account(client, pipeline)
    group = make_group(client, pipeline, account)
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('a.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + m.timedelta(hours=2))
    created = client.post(f'/api/pipelines/{pipeline}/group-posts', json={
        'group_ids': [group['id']], 'body': 'Xin chào', 'media': [image], 'scheduled_at': scheduled})
    assert created.status_code == 201
    job_id = created.json()['jobs'][0]['job_id']
    # the group was never screened, so the job waits
    assert m.claim_next_job() is None
    with m.db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row['state'] == 'queued'
    assert 'chưa được xác minh' in row['details']


def test_a_verified_group_is_served_before_a_review_group(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr6')
    account = make_account(client, pipeline)
    priority = make_group(client, pipeline, account, url="https://www.facebook.com/groups/611/")['id']
    review = make_group(client, pipeline, account, url="https://www.facebook.com/groups/622/")['id']
    with m.db() as conn:
        mark_direct_post(conn, pipeline, account, priority)
        m.store_screening_result(conn, pipeline, account, priority,
                                 {"status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "requires_approval": False})
        m.store_screening_result(conn, pipeline, account, review,
                                 {"status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "requires_approval": True})
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('b.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() - m.timedelta(minutes=5))
    for group_id in (priority, review):
        client.post(f'/api/pipelines/{pipeline}/group-posts', json={
            'group_ids': [group_id], 'body': 'Xin chào', 'media': [image], 'scheduled_at': scheduled})
    claimed = m.claim_next_job()
    assert claimed is not None
    assert claimed['group_id'] == priority


def test_a_pending_post_is_not_reported_as_posted(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr7')
    account = make_account(client, pipeline)
    group = make_group(client, pipeline, account)
    with m.db() as conn:
        m.store_screening_result(conn, pipeline, account, group['id'],
                                 {"status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "requires_approval": True})
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('c.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() - m.timedelta(minutes=5))
    created = client.post(f'/api/pipelines/{pipeline}/group-posts', json={
        'group_ids': [group['id']], 'body': 'Xin chào', 'media': [image], 'scheduled_at': scheduled})
    job_id = created.json()['jobs'][0]['job_id']
    # the worker clicked publish but the result is not confirmed
    with m.db() as conn:
        conn.execute("""UPDATE jobs SET state='submitted_unverified', details='Đã bấm đăng, cần xác minh',
                        actual_posting_identity='anonymous', verification_status='verified',
                        posting_status='pending_approval' WHERE id=?""", (job_id,))
        conn.commit()
    with m.db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row['state'] == 'submitted_unverified'
    assert row['posting_status'] == 'pending_approval'
    assert row['state'] != 'posted'


def test_a_verified_post_turns_the_group_into_a_priority(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr8')
    account = make_account(client, pipeline)
    group = make_group(client, pipeline, account)
    with m.db() as conn:
        m.store_screening_result(conn, pipeline, account, group['id'],
                                 {"status": "ok", "accessible": True, "is_member": True,
                                  "can_post": True, "requires_approval": False})
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('d.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() - m.timedelta(minutes=5))
    created = client.post(f'/api/pipelines/{pipeline}/group-posts', json={
        'group_ids': [group['id']], 'body': 'Xin chào', 'media': [image], 'scheduled_at': scheduled})
    job_id = created.json()['jobs'][0]['job_id']
    with m.db() as conn:
        conn.execute("UPDATE jobs SET state='posted', updated_at=? WHERE id=?", (m.iso(), job_id))
        conn.commit()
    with m.db() as conn:
        m.refresh_screening_for_job(conn, job_id)
        row = conn.execute("SELECT * FROM group_screening_results WHERE group_id=?", (group['id'],)).fetchone()
    assert row['moderation_status'] == STATUS_PRIORITY
    assert row['direct_posts'] == 1


# --- access control ---------------------------------------------------------

def test_collaborator_cannot_screen_groups_without_permission(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr9')
    account = make_account(client, pipeline)
    group = make_group(client, pipeline, account)
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_scr', 'password': 'ctv-screen-password-2026',
                                          'pipeline_id': pipeline, 'permissions': ['groups.view']})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_scr', 'password': 'ctv-screen-password-2026',
                                               'pipeline_key': key})
    assert login.status_code == 200
    ctv.csrf = login.json()['csrf_token']
    assert ctv.get(f'/api/pipelines/{pipeline}/groups/screening-results').status_code == 200
    assert ctv.get(f'/api/pipelines/{pipeline}/groups/priority').status_code == 200
    assert ctv.post(f'/api/pipelines/{pipeline}/groups/screen', json={}).status_code == 403
    assert ctv.post(f'/api/pipelines/{pipeline}/groups/{group["id"]}/recheck', json={}).status_code == 403
    ctv.close()


def test_screening_of_another_pipeline_is_hidden(client, monkeypatch):
    mine = make_pipeline(client, '@scr10')
    theirs = make_pipeline(client, '@scr11')
    my_account = make_account(client, mine)
    their_account = make_account(client, theirs)
    their_group = make_group(client, theirs, their_account)
    assert client.get(f'/api/pipelines/{mine}/groups/screening-results').json()['summary']['total'] == 0
    assert client.get(f'/api/pipelines/{theirs}/groups/screening-results'
                       ).json()['summary']['total'] == 0
    response = client.post(f'/api/pipelines/{mine}/groups/{their_group["id"]}/recheck', json={})
    assert response.status_code == 404


# --- migration --------------------------------------------------------------

def test_migration_keeps_the_existing_data(client):
    pipeline = make_pipeline(client, '@scr12')
    account = make_account(client, pipeline)
    group = make_group(client, pipeline, account)
    m.init_db()
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM groups WHERE id=?", (group['id'],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE id=?", (account,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pipelines WHERE id=?", (pipeline,)).fetchone()[0] == 1
        columns = {row[1] for row in conn.execute("PRAGMA table_info(groups)")}
    assert "screening_status" in columns
    assert "manual_review" in columns


def test_the_screening_queue_respects_the_cooldown(client, monkeypatch):
    pipeline = make_pipeline(client, '@scr13')
    account = make_account(client, pipeline)
    group = make_group(client, pipeline, account)
    with m.db() as conn:
        conn.execute("UPDATE groups SET screening_checked_at=? WHERE id=?", (m.iso(), group['id']))
        conn.commit()
        queued = m.enqueue_screening(conn, pipeline, account, group['id'], False)
    assert queued is False
    with m.db() as conn:
        forced = m.enqueue_screening(conn, pipeline, account, group['id'], True)
    assert forced is True
