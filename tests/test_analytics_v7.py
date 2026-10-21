"""V7: the analytics of the pipelines and of the collaborators.

These checks follow the rules of the report:

1. A snapshot is never counted twice.
2. A published post and a post waiting for approval stay apart.
3. A post belongs to the collaborator who created it.
4. A pipeline never shows the numbers of another pipeline.
5. A missing measurement stays "chưa có dữ liệu" and never becomes a zero.
6. The views of a reel are not added to the reactions.
7. A metric that Facebook does not offer keeps its state.
8. The administrator sees the whole system.
9. A collaborator never reaches the report of another pipeline.
10. The chart follows the date filter.
11. The layout holds on a phone.
12. A collector that fails never breaks the posting queue.
"""
import json

from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m
import auth as au

CTV_PASSWORD = "ctv-password-2026-strong"


def make_tenant(admin, name):
    """A pipeline with two collaborators."""
    created = admin.post('/api/admin/pipelines', json={'name': name})
    assert created.status_code == 201, created.text
    pipeline = created.json()
    suffix = name.lstrip('@').replace('_', '')[:10]
    for username in (f'ctv_{suffix}_ana', f'ctv_{suffix}_binh'):
        member = admin.post('/api/admin/users', json={
            'username': username, 'password': CTV_PASSWORD, 'pipeline_id': pipeline['id']})
        assert member.status_code == 201, member.text
    return pipeline


def login_ctv(username, key):
    ctv = AuthClient(m.app)
    response = ctv.post('/api/auth/login', json={
        'username': username, 'password': CTV_PASSWORD, 'pipeline_key': key})
    assert response.status_code == 200, response.text
    ctv.csrf = response.json().get('csrf_token', '')
    return ctv


def seed_content(admin, pipeline_id, owner_user_id, label, state='posted',
                 posting_status=None, permalink='', published_at=None):
    """One post with one job in the given state."""
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline_id,
                                                 'label': f'FB {label}'}).json()
    group = admin.post('/api/pipelines/%d/groups' % pipeline_id, json={
        'pipeline_id': pipeline_id, 'account_id': account['id'],
        'url': f'https://www.facebook.com/groups/{abs(hash(label)) % 10 ** 8}/',
        'label': f'Group {label}', 'posting_mode': 'anonymous'}).json()
    with m.db() as conn:
        cursor = conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,
                                                     anonymous_only,ai_record_id,owner_user_id)
                                 VALUES(?,?,?,?,1,NULL,?)""",
                              (pipeline_id, f'Nội dung {label}', '[]', m.iso(), owner_user_id))
        post_id = cursor.lastrowid
        cursor = conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,state,pipeline_id,
                                                   posting_status,permalink,published_at,
                                                   scheduled_at,started_at,updated_at)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                              (post_id, group['id'], account['id'], state, pipeline_id,
                               posting_status or state, permalink,
                               published_at or (m.iso() if state == 'posted' else None),
                               m.iso(), m.iso(), m.iso()))
        job_id = cursor.lastrowid
    return {'post_id': post_id, 'job_id': job_id, 'account_id': account['id'],
            'group_id': group['id']}


def add_snapshot(pipeline_id, content_type, post_id, account_id, collected_at,
                 reactions=None, comments=None, shares=None, views=None, reach=None,
                 source='facebook_session', verification='measured'):
    with m.db() as conn:
        conn.execute("""INSERT INTO post_engagement_snapshots
            (pipeline_id,content_type,post_id,account_id,reactions_count,comments_count,
             shares_count,views_count,reach_count,source,collected_at,verification_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (pipeline_id, content_type, post_id, account_id, reactions, comments,
                      shares, views, reach, source, collected_at, verification))


# --- 1. a snapshot is never counted twice -------------------------------------

def test_engagement_uses_only_the_newest_snapshot(client):
    pipeline = make_tenant(client, '@snapshots')
    content = seed_content(client, pipeline['id'], None, 'A')
    add_snapshot(pipeline['id'], 'group_post', content['post_id'], content['account_id'],
                 '2026-10-01T10:00:00+00:00', reactions=120, comments=10, shares=5)
    add_snapshot(pipeline['id'], 'group_post', content['post_id'], content['account_id'],
                 '2026-10-02T10:00:00+00:00', reactions=180, comments=20, shares=9)
    add_snapshot(pipeline['id'], 'group_post', content['post_id'], content['account_id'],
                 '2026-10-03T10:00:00+00:00', reactions=230, comments=25, shares=11)

    report = client.get(f"/api/admin/analytics/pipelines/{pipeline['id']}?days=90").json()
    assert report['kpis']['engagement'] == 230 + 25 + 11, report['kpis']
    assert report['kpis']['reactions'] == 230
    assert report['kpis']['comments'] == 25
    assert report['kpis']['shares'] == 11


# --- 2. published and waiting are apart ---------------------------------------

def test_published_and_waiting_posts_are_separated(client):
    pipeline = make_tenant(client, '@states')
    seed_content(client, pipeline['id'], None, 'posted', state='posted')
    seed_content(client, pipeline['id'], None, 'waiting', state='pending_approval',
                 posting_status='pending_approval')
    seed_content(client, pipeline['id'], None, 'refused', state='failed',
                 posting_status='failed')

    kpis = client.get(f"/api/admin/analytics/pipelines/{pipeline['id']}?days=90").json()['kpis']
    assert kpis['posts_published'] == 1, kpis
    assert kpis['posts_waiting'] == 1, kpis
    assert kpis['posts_refused'] == 1, kpis


# --- 3. the collaborator of a post ---------------------------------------------

def test_post_is_attributed_to_its_creator(client):
    pipeline = make_tenant(client, '@attribution')
    members = {row['username']: row for row in client.get('/api/admin/users').json()}
    suffix = pipeline['name'].lstrip('@').replace('_', '')[:10]
    ana = members[f'ctv_{suffix}_ana']['id']
    content = seed_content(client, pipeline['id'], ana, 'Cua Ana')

    ranking = client.get('/api/admin/analytics/collaborators?days=90').json()['collaborators']
    row = next(item for item in ranking if item['username'] == f'ctv_{suffix}_ana')
    assert row['posts_created'] == 1, row
    assert row['posts_published'] == 1, row
    other = next(item for item in ranking if item['username'] == f'ctv_{suffix}_binh')
    assert other['posts_created'] == 0, other


def test_approved_and_published_account_are_recorded(client):
    pipeline = make_tenant(client, '@attribution_two')
    members = {row['username']: row for row in client.get('/api/admin/users').json()}
    suffix = pipeline['name'].lstrip('@').replace('_', '')[:10]
    content = seed_content(client, pipeline['id'], members[f'ctv_{suffix}_ana']['id'], 'Cua Ana')
    with m.db() as conn:
        m.analytics_worker.sync_attribution(conn, 'group_post', content['post_id'],
                                             created_by=members[f'ctv_{suffix}_ana']['id'],
                                             approved_by=1, published_by=content['account_id'])
        row = conn.execute("""SELECT * FROM post_attribution WHERE post_id=?""",
                           (content['post_id'],)).fetchone()
    assert row['created_by_ctv_id'] == members[f'ctv_{suffix}_ana']['id']
    assert row['approved_by_user_id'] == 1
    assert row['published_by_account_id'] == content['account_id']
    assert row['pipeline_id'] == pipeline['id']


# --- 4. a pipeline never shows the numbers of another --------------------------

def test_pipelines_do_not_mix(client):
    first = make_tenant(client, '@first')
    second = make_tenant(client, '@second')
    seed_content(client, first['id'], None, 'A')
    seed_content(client, second['id'], None, 'B')

    report = client.get(f"/api/admin/analytics/pipelines/{first['id']}?days=90").json()
    assert report['kpis']['total'] == 1, report['kpis']
    assert report['kpis']['posts_published'] == 1

    listing = client.get('/api/admin/analytics/pipelines?days=90').json()['pipelines']
    one = next(item for item in listing if item['pipeline_id'] == first['id'])
    other = next(item for item in listing if item['pipeline_id'] == second['id'])
    assert one['posts_published'] == 1
    assert other['posts_published'] == 1


# --- 5. a missing measurement stays missing -----------------------------------

def test_missing_measurement_is_not_a_zero(client):
    pipeline = make_tenant(client, '@missing')
    seed_content(client, pipeline['id'], None, 'A')

    kpis = client.get(f"/api/admin/analytics/pipelines/{pipeline['id']}?days=90").json()['kpis']
    assert kpis['engagement'] is None, kpis
    assert kpis['reactions'] is None
    assert kpis['views'] is None
    assert kpis['engagement_available'] is False
    assert kpis['posts_published'] == 1
    assert kpis['measured_posts'] == 0
    assert kpis['unmeasured_posts'] == 1


# --- 6. the views of a reel stay apart from the reactions ----------------------

def test_reel_views_are_not_added_to_the_reactions(client):
    pipeline = make_tenant(client, '@reels')
    content = seed_content(client, pipeline['id'], None, 'A')
    add_snapshot(pipeline['id'], 'group_post', content['post_id'], content['account_id'],
                 '2026-10-05T10:00:00+00:00', reactions=40, comments=4, shares=2)
    with m.db() as conn:
        cursor = conn.execute("""INSERT INTO manual_tasks(pipeline_id,account_id,task_type,body,
                                     media_json,target_url,scheduled_at,status,created_at,updated_at,
                                     owner_user_id)
                                 VALUES(?,?,'reel','Reel', '[]', '', ?, 'posted', ?, ?, NULL)""",
                              (pipeline['id'], content['account_id'], m.iso(), m.iso(), m.iso()))
        reel_id = cursor.lastrowid
    add_snapshot(pipeline['id'], 'reel', reel_id, content['account_id'],
                 '2026-10-05T11:00:00+00:00', views=12000, reach=8000)

    kpis = client.get(f"/api/admin/analytics/pipelines/{pipeline['id']}?days=90").json()['kpis']
    assert kpis['views'] == 12000, kpis
    assert kpis['reach'] == 8000, kpis
    assert kpis['engagement'] == 46, kpis
    assert kpis['reactions'] == 40


# --- 7. a metric that is not supported keeps its state -------------------------

def test_unsupported_metric_keeps_its_state(client):
    pipeline = make_tenant(client, '@unsupported')
    content = seed_content(client, pipeline['id'], None, 'A')
    add_snapshot(pipeline['id'], 'group_post', content['post_id'], content['account_id'],
                 '2026-10-06T10:00:00+00:00', reactions=7, comments=None, shares=None)

    kpis = client.get(f"/api/admin/analytics/pipelines/{pipeline['id']}?days=90").json()['kpis']
    assert kpis['reactions'] == 7
    assert kpis['comments'] is None
    assert kpis['shares'] is None
    assert kpis['engagement'] == 7


def test_removed_post_is_counted_as_removed(client):
    pipeline = make_tenant(client, '@removed')
    content = seed_content(client, pipeline['id'], None, 'A')
    add_snapshot(pipeline['id'], 'group_post', content['post_id'], content['account_id'],
                 '2026-10-07T10:00:00+00:00', reactions=None, verification='removed')

    kpis = client.get(f"/api/admin/analytics/pipelines/{pipeline['id']}?days=90").json()['kpis']
    assert kpis['posts_removed'] == 1, kpis
    assert kpis['engagement'] is None


# --- 8. the administrator sees the whole system --------------------------------

def test_admin_sees_the_whole_system(client):
    first = make_tenant(client, '@system_one')
    second = make_tenant(client, '@system_two')
    seed_content(client, first['id'], None, 'A')
    seed_content(client, second['id'], None, 'B')

    overview = client.get('/api/admin/analytics/overview').json()
    assert overview['kpis']['posts_published'] == 2, overview['kpis']
    assert overview['kpis']['pipelines_active'] >= 2
    assert overview['kpis']['collaborators_active'] >= 2
    assert 'sources' in overview


# --- 9. a collaborator stays inside their own workspace ------------------------

def test_collaborator_cannot_read_the_report_of_another_pipeline(client):
    pipeline = make_tenant(client, '@isolation')
    other = make_tenant(client, '@isolation_other')
    seed_content(client, other['id'], None, 'B')

    ctv = login_ctv(f"ctv_{pipeline['name'].lstrip('@').replace('_', '')[:10]}_ana", pipeline['key'])
    # Không có quyền analytics.view thì bị chặn ngay.
    assert ctv.get('/api/analytics/me').status_code == 403
    # Không đổi id trên URL cũng không vượt qua được.
    for path in ('/api/admin/analytics/overview', '/api/admin/analytics/pipelines',
                 f"/api/admin/analytics/pipelines/{other['id']}",
                 '/api/admin/analytics/collaborators', '/api/admin/analytics/top-posts',
                 '/api/admin/analytics/timeseries'):
        assert ctv.get(path).status_code == 403, path


def test_collaborator_with_permission_sees_only_their_own_content(client):
    pipeline = make_tenant(client, '@own_view')
    members = {row['username']: row for row in client.get('/api/admin/users').json()}
    suffix = pipeline['name'].lstrip('@').replace('_', '')[:10]
    seed_content(client, pipeline['id'], members[f'ctv_{suffix}_ana']['id'], 'Cua Ana')
    seed_content(client, pipeline['id'], members[f'ctv_{suffix}_binh']['id'], 'Cua Binh')

    client.patch(f"/api/admin/users/{members[f'ctv_{suffix}_ana']['id']}",
                 json={'permissions': ['analytics.view', 'facebook.view', 'groups.view',
                                       'history.view']})
    ctv = login_ctv(f"ctv_{pipeline['name'].lstrip('@').replace('_', '')[:10]}_ana", pipeline['key'])
    report = ctv.get('/api/analytics/me').json()
    assert report['kpis']['total'] == 1, report['kpis']
    assert report['pipeline']['id'] == pipeline['id']


# --- 10. the chart follows the date filter -------------------------------------

def test_timeseries_follows_the_date_filter(client):
    pipeline = make_tenant(client, '@timeseries')
    old = seed_content(client, pipeline['id'], None, 'A',
                       published_at='2026-01-05T10:00:00+00:00')
    add_snapshot(pipeline['id'], 'group_post', old['post_id'], old['account_id'],
                 '2026-01-05T10:00:00+00:00', reactions=10)

    wide = client.get(f"/api/admin/analytics/timeseries?days=365&pipeline_id={pipeline['id']}").json()
    assert wide['series'], wide
    assert wide['series'][-1]['day'] == '2026-01-05'
    assert wide['series'][-1]['engagement'] == 10

    # Bộ lọc ngày bỏ bài nằm ngoài khoảng thời gian.
    narrow = client.get(f"/api/admin/analytics/timeseries?days=7&pipeline_id={pipeline['id']}").json()
    assert narrow['series'] == [], narrow


def test_filter_by_account_and_collaborator(client):
    pipeline = make_tenant(client, '@filters')
    members = {row['username']: row for row in client.get('/api/admin/users').json()}
    suffix = pipeline['name'].lstrip('@').replace('_', '')[:10]
    first = seed_content(client, pipeline['id'], members[f'ctv_{suffix}_ana']['id'], 'A')
    seed_content(client, pipeline['id'], members[f'ctv_{suffix}_binh']['id'], 'B')

    report = client.get(f"/api/admin/analytics/pipelines/{pipeline['id']}?days=90"
                        f"&account_id={first['account_id']}").json()
    assert report['kpis']['total'] == 1, report['kpis']

    report = client.get(f"/api/admin/analytics/pipelines/{pipeline['id']}?days=90"
                        f"&ctv_id={members[f'ctv_{suffix}_binh']['id']}").json()
    assert report['kpis']['total'] == 1, report['kpis']


# --- 12. a collector that fails never breaks the queue -------------------------

def test_failing_collector_keeps_the_posting_queue_intact(client):
    pipeline = make_tenant(client, '@collector')
    content = seed_content(client, pipeline['id'], None, 'A', permalink='https://www.facebook.com/x/1')
    with m.db() as conn:
        m.analytics_worker.enqueue_published(conn)
        row = conn.execute("SELECT * FROM analytics_collection_jobs WHERE post_id=?",
                           (content['post_id'],)).fetchone()
    assert row is not None
    assert row['status'] == 'queued'

    # Không có nguồn đo nào cấu hình: bộ thu thập ghi mã lỗi và không ghi số 0.
    with m.db() as conn:
        done = m.analytics_worker.collect_pending(conn, 5)
    assert done == 0
    with m.db() as conn:
        row = conn.execute("SELECT * FROM analytics_collection_jobs WHERE post_id=?",
                           (content['post_id'],)).fetchone()
        snapshots = conn.execute("SELECT COUNT(*) FROM post_engagement_snapshots").fetchone()[0]
    assert row['status'] == 'queued'
    assert row['error_code']
    assert snapshots == 0

    # Hàng đợi đăng bài vẫn chạy bình thường.
    assert client.get('/api/jobs?pipeline_id=%d' % pipeline['id']).status_code == 200


def test_queue_is_not_duplicated(client):
    pipeline = make_tenant(client, '@queue')
    content = seed_content(client, pipeline['id'], None, 'A', permalink='https://www.facebook.com/x/2')
    with m.db() as conn:
        m.analytics_worker.enqueue_published(conn)
        m.analytics_worker.enqueue_published(conn)
        count = conn.execute("SELECT COUNT(*) FROM analytics_collection_jobs").fetchone()[0]
    assert count == 1


def test_overview_reports_the_sources(client):
    overview = client.get('/api/admin/analytics/overview').json()
    assert 'graph_api' in overview['sources']
    assert 'facebook_session' in overview['sources']
