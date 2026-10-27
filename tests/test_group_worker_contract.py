"""Worker/DB contracts using isolated SQLite; subprocess publication is stubbed."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_app import client  # noqa: F401
import app as m
from test_first_post_flow import make_pipeline, make_account, make_group
from services import group_membership


@pytest.fixture(autouse=True)
def idle_background_workers(monkeypatch):
    for name in ('scheduler', 'screening_worker', 'precheck_worker', 'media_worker',
                 'auto_post_worker', 'analytics_thread', 'group_join_worker_thread'):
        monkeypatch.setattr(m, name, lambda: None)


def due_job(client, *, expired=False):
    pipeline = make_pipeline(client, '@worker-contract')
    account = make_account(client, pipeline, 'Test Facebook', 'expired' if expired else 'connected')
    group = make_group(client, pipeline, account, 'Test group', 'https://www.facebook.com/groups/contract/')
    when = m.iso(m.now() - m.timedelta(minutes=1))
    image = m.UPLOADS / str(pipeline) / 'test.jpg'
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b'isolated worker fixture')
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
        post = conn.execute('''INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)
                               VALUES(?,?,?,?,1)''',
                            (pipeline, 'Caption', json.dumps([str(image.relative_to(m.DATA))]), when)).lastrowid
        job = conn.execute('''INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,
                            scheduled_at,updated_at,requested_posting_mode,posting_status,verification_status)
                            VALUES(?,?,?,?,'queued','',?,?,'anonymous','pending','unverified')''',
                           (post, group, account, pipeline, when, when)).lastrowid
    return job, pipeline, account, group


@pytest.mark.parametrize('outcome', ['submitted_unverified', 'pending_approval', 'posted'])
def test_worker_payload_and_persistent_result(client, monkeypatch, outcome):
    job_id, pipeline, account, group = due_job(client)
    job = m.claim_next_job()
    assert job['id'] == job_id
    calls = []
    def offline_browser(command, **kwargs):
        payload = json.loads(Path(command[-2]).read_text())
        calls.append(payload)
        assert payload['pipeline_id'] == pipeline
        assert payload['account_id'] == account
        assert payload['group_id'] == group
        Path(command[-1]).write_text(json.dumps({
            'state': outcome, 'details': 'Offline browser contract',
            'evidence': '', 'verification': 'verified' if outcome == 'posted' else 'unverified',
            'permalink': 'https://www.facebook.com/groups/contract/posts/fixture/' if outcome == 'posted' else '',
        }))
        return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(m.subprocess, 'run', offline_browser)
    m.execute_job(job)
    assert len(calls) == 1
    assert m.claim_next_job() is None
    with m.db() as conn:
        persisted = dict(conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
    assert persisted['state'] == outcome
    assert persisted['posting_status'] == outcome
    assert bool(persisted['published_at']) == (outcome == 'posted')
    if outcome == 'submitted_unverified':
        assert client.post(f'/api/jobs/{job_id}/review', json={'state':'queued'}).status_code == 409


def test_expired_session_does_not_prevent_job_persistence(client):
    job_id, _, _, _ = due_job(client, expired=True)
    assert m.claim_next_job() is None
    with m.db() as conn:
        row = conn.execute('SELECT state, details FROM jobs WHERE id=?', (job_id,)).fetchone()
        assert row['state'] == 'needs_review'
        assert row['details']


def test_cross_tenant_job_never_launches_browser(client, monkeypatch):
    job_id, _, _, _ = due_job(client)
    other = make_pipeline(client, '@other-workspace')
    other_account = make_account(client, other, 'Other Facebook')
    with m.db() as conn:
        conn.execute('UPDATE jobs SET account_id=? WHERE id=?', (other_account, job_id))
    called = []
    monkeypatch.setattr(m.subprocess, 'run', lambda *a, **kw: called.append(True))
    m.execute_job({'id':job_id})
    assert called == []
    with m.db() as conn:
        assert conn.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()['state'] == 'needs_review'


def approved_album_job(client):
    from test_first_post_flow import make_picture
    job_id, pipeline, account, group = due_job(client)
    picture = make_picture(client, pipeline)
    with m.db() as conn:
        conn.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job_id,))
        auto_id = conn.execute('''INSERT INTO auto_post_jobs
            (pipeline_id,media_id,account_id,group_id,caption,posting_mode,status,idempotency_key,created_at,updated_at)
            VALUES(?,?,?,?,'Caption','anonymous','approved','offline-test-key',?,?)''',
            (pipeline, picture, account, group, m.iso(), m.iso())).lastrowid
    return auto_id, pipeline


def test_promotion_rechecks_ownership_of_album_media(client, monkeypatch):
    from test_first_post_flow import make_picture
    auto_id, pipeline = approved_album_job(client)
    other = make_pipeline(client, '@other-album')
    foreign_media = make_picture(client, other)
    with m.db() as conn:
        conn.execute('UPDATE auto_post_jobs SET media_id=? WHERE id=?', (foreign_media, auto_id))
        before = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    monkeypatch.setattr(m.media_cloud, 'enabled', lambda: False)
    m.publish_auto_post_jobs()
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == before
        assert conn.execute('SELECT status FROM auto_post_jobs WHERE id=?', (auto_id,)).fetchone()[0] == 'failed'


def test_concurrent_promotion_creates_only_one_persistent_post(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    auto_id, pipeline = approved_album_job(client)
    monkeypatch.setattr(m.media_cloud, 'enabled', lambda: False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(m.publish_auto_post_jobs) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    with m.db() as conn:
        auto = conn.execute('SELECT * FROM auto_post_jobs WHERE id=?', (auto_id,)).fetchone()
        assert auto['status'] == 'queued', auto['error']
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE state='queued' AND pipeline_id=?", (pipeline,)).fetchone()[0] == 1
        assert auto['post_id'] is not None
