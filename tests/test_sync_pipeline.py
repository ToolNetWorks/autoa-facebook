"""Đồng bộ xuyên suốt: giao diện → API → database → bộ xếp lịch → worker → lịch sử.

Cùng một con số phải hiện giống nhau ở mọi tầng. Nếu một tầng lệch, bài
kiểm thử này bắt được, thay vì để người dùng thấy hai con số khác nhau.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from services import group_classification, group_eligibility, group_verification


def make_pipeline(admin, name):
    return admin.post('/api/pipelines', json={'name': name}).json()['id']


def make_account(admin, pipeline_id, label, session="connected"):
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label}).json()
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status=? WHERE id=?", (session, account['id']))
    return account['id']


def make_group(admin, pipeline_id, account_id, label, url):
    group = admin.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': label, 'url': url,
        'approved_target': True, 'posting_mode': 'anonymous'}).json()
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


def test_the_same_numbers_are_reported_everywhere(client):
    """Số group theo phân loại phải giống nhau ở API, database và bộ xếp lịch."""
    pipeline = make_pipeline(client, '@sync1')
    account = make_account(client, pipeline, 'Facebook')
    direct = make_group(client, pipeline, account, 'Group trực tiếp',
                       'https://www.facebook.com/groups/50001/')
    waiting = make_group(client, pipeline, account, 'Group cần duyệt',
                         'https://www.facebook.com/groups/50002/')
    fresh = make_group(client, pipeline, account, 'Group mới',
                       'https://www.facebook.com/groups/50003/')

    with m.db() as conn:
        group_verification.record_outcome(conn, pipeline, account, direct,
                                          group_verification.VERIFIED_POSTED)
        group_classification.record_outcome(conn, pipeline, account, direct,
                                            group_verification.VERIFIED_POSTED)
        group_verification.record_outcome(conn, pipeline, account, waiting,
                                          group_verification.PENDING_GROUP_APPROVAL)
        group_classification.record_outcome(conn, pipeline, account, waiting,
                                            group_verification.PENDING_GROUP_APPROVAL)

    # Tầng database.
    with m.db() as conn:
        counts_db = group_classification.counts_of(conn, pipeline)

    # Tầng API.
    api_counts = client.get(f'/api/pipelines/{pipeline}/groups/verifications').json()['counts']

    # Tầng danh sách group.
    groups = client.get(f'/api/pipelines/{pipeline}/groups').json()
    counts_api = {
        'no_approval': sum(1 for item in groups if item['classification'] == 'no_approval'),
        'needs_approval': sum(1 for item in groups if item['classification'] == 'needs_approval'),
        'unclassified': sum(1 for item in groups if item['classification'] == 'unclassified'),
    }

    assert counts_db['no_approval'] == counts_api['no_approval'] == api_counts['no_approval'] == 1
    assert (counts_db['needs_approval'] == counts_api['needs_approval']
            == api_counts['needs_approval'] == 1)
    assert (counts_db['unclassified'] == counts_api['unclassified']
            == api_counts['unclassified'] == 1)

    # Tầng quyết định của bộ xếp lịch: cùng số cặp được phục vụ.
    with m.db() as conn:
        decisions = {}
        for group_id in (direct, waiting, fresh):
            decision = group_eligibility.pair_decision(conn, pipeline, account, group_id)
            decisions[decision['decision']] = decisions.get(decision['decision'], 0) + 1
    assert decisions.get(group_eligibility.VERIFIED_READY) == 1
    assert decisions.get(group_eligibility.WAITING_APPROVAL) == 1
    assert decisions.get(group_eligibility.FIRST_POST_READY) == 1


def test_the_job_reaches_the_history_with_the_same_state(client):
    """Job đi qua bộ xếp lịch rồi hiện trong lịch sử với cùng trạng thái."""
    pipeline = make_pipeline(client, '@sync2')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/50004/')
    make_picture(client, pipeline)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json={
        'enabled': True, 'scope': 'selected', 'account_ids': [], 'group_ids': [group],
        'daily_limit_per_account': 2, 'min_gap_minutes': 0, 'pipeline_daily_limit': 0,
        'topic': 'sản phẩm', 'ai_caption': False, 'anti_duplicate': True,
        'require_approval': True, 'apply_to_new_accounts': True})
    created = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert created.status_code == 201, created.text
    assert len(created.json()['created']) == 1

    with m.db() as conn:
        rows = conn.execute("""SELECT id, group_id, account_id, status FROM auto_post_jobs
                               WHERE pipeline_id=?""", (pipeline,)).fetchall()
    assert len(rows) == 1
    assert rows[0]['status'] == 'pending'
    assert rows[0]['group_id'] == group
    assert rows[0]['account_id'] == account

    # Lịch sử của CTV trong cùng Workspace phải thấy job đó.
    with m.db() as conn:
        history = conn.execute("""SELECT id FROM auto_post_jobs WHERE pipeline_id=?
                                  ORDER BY id""", (pipeline,)).fetchall()
    assert len(history) == 1


def test_the_worker_state_reaches_the_verification_and_the_classification(client):
    """Kết quả worker cập nhật cả bằng chứng lẫn phân loại."""
    pipeline = make_pipeline(client, '@sync3')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/50005/')
    make_picture(client, pipeline)
    with m.db() as conn:
        post_id = conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)
                                  VALUES(?,?,?,?,1)""",
                               (pipeline, "bài kiểm tra", "[]", m.iso())).lastrowid
        job_id = conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,
                                scheduled_at,updated_at,published_at,requested_posting_mode,
                                posting_status,verification_status,is_verification)
                                VALUES(?,?,?,?,'posted','',?,?,?,'anonymous','posted','verified',1)""",
                               (post_id, group, account, pipeline, m.iso(), m.iso(), m.iso())).lastrowid
        m.refresh_screening_for_job(conn, job_id)
        status = group_verification.status_of(conn, pipeline, account, group)
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert status == group_verification.VERIFIED_POSTED
    assert entry['classification'] == group_classification.NO_APPROVAL

    # API phải trả đúng hai giá trị đó.
    listing = client.get(f'/api/pipelines/{pipeline}/groups/verifications').json()
    row = next(item for item in listing['verifications'] if item['group_id'] == group)
    assert row['verification_status'] == group_verification.VERIFIED_POSTED
    assert row['classification'] == group_classification.NO_APPROVAL
    assert row['decision'] == group_eligibility.VERIFIED_READY
