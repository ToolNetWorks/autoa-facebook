"""V9: lịch đăng thông minh cho cả Pipeline.

Lịch phân bổ bài đều trong khung giờ, giữ khoảng cách giữa hai bài của cùng
một tài khoản, nghỉ giữa hai lần đăng vào cùng group, và không tạo job trùng
kể cả khi hai worker chạy cùng lúc.
"""
from datetime import datetime, timedelta

from test_app import client  # noqa: F401 (registers the client fixture)

import app as m
from scheduler import account_limits, group_cooldowns, job_allocator, time_windows
from services import ai_provider


def make_pipeline(admin, name):
    return admin.post('/api/pipelines', json={'name': name}).json()['id']


def make_account(admin, pipeline_id, label, connected=True):
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label}).json()
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status=? WHERE id=?",
                     ('connected' if connected else 'expired', account['id']))
    return account['id']


def make_group(admin, pipeline_id, account_id, label, url, screening='priority'):
    group = admin.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': label, 'url': url,
        'approved_target': True, 'posting_mode': 'anonymous'}).json()
    with m.db() as conn:
        conn.execute("UPDATE groups SET screening_status=? WHERE id=?", (screening, group['id']))
    return group['id']


def make_media(admin, pipeline_id, name, sha=None):
    with m.db() as conn:
        cursor = conn.execute("""INSERT INTO media_assets
            (pipeline_id,uploader_id,storage_key,thumbnail_key,original_name,mime_type,width,height,
             size_bytes,sha256,signature,analysis_status,safety_status,usable,description,tags_json,
             created_at,updated_at,owner_user_id)
            VALUES(?,?,?,?,?,'image/jpeg',600,600,1000,?,?,'done','pass',1,'ảnh sản phẩm đẹp','[]',?,?,NULL)""",
            (pipeline_id, 1, f"{pipeline_id}/{name}", f"{pipeline_id}/thumbs/{name}", name,
             sha or (name * 8)[:64], '', m.iso(), m.iso()))
        return cursor.lastrowid


def enable_auto_post(admin, pipeline_id, **overrides):
    payload = {'enabled': True, 'scope': 'all', 'daily_limit_per_account': 3,
               'min_gap_minutes': 0, 'pipeline_daily_limit': 0, 'topic': 'sản phẩm',
               'ai_caption': False, 'anti_duplicate': True, 'require_approval': True,
               'apply_to_new_accounts': True}
    payload.update(overrides)
    response = admin.put(f'/api/pipelines/{pipeline_id}/auto-post/settings', json=payload)
    assert response.status_code == 200, response.text
    return response.json()['settings']


def save_schedule(admin, pipeline_id, **overrides):
    payload = {'mode': 'spread', 'timezone': 'Asia/Ho_Chi_Minh', 'start_time': '08:00',
               'end_time': '22:00', 'min_interval_minutes': 90,
               'max_posts_per_account_per_day': 3,
               'days_enabled_json': '[1,2,3,4,5,6,0]', 'group_cooldown_minutes': 120,
               'paused': False}
    payload.update(overrides)
    response = admin.put(f'/api/pipelines/{pipeline_id}/schedule', json=payload)
    assert response.status_code == 200, response.text
    return response.json()['schedule']


# --- khung giờ ---------------------------------------------------------------

def test_outside_the_window_nothing_is_scheduled(client):
    pipeline = make_pipeline(client, '@v9_window')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/111111/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline, start_time='08:00', end_time='12:00')

    # 03:00 giờ Việt Nam nằm ngoài khung giờ.
    outside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=3, minute=0)
    with m.db() as conn:
        outcome = job_allocator.plan(conn, pipeline, outside)
    assert outcome["jobs"] == []
    assert "khung giờ" in outcome.get("reason", ""), outcome


def test_inside_the_window_jobs_are_scheduled(client):
    pipeline = make_pipeline(client, '@v9_inside')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/222222/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline, start_time='08:00', end_time='22:00')

    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=9, minute=0)
    with m.db() as conn:
        outcome = job_allocator.plan(conn, pipeline, inside)
    assert outcome["jobs"], outcome


def test_window_crossing_midnight_is_allowed(client):
    settings = {'timezone': 'Asia/Ho_Chi_Minh', 'start_time': '22:00', 'end_time': '02:00',
                'windows_json': '[]', 'days_enabled_json': '[1,2,3,4,5,6,0]'}
    moment = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=23, minute=30)
    assert time_windows.is_inside_window(moment, settings) is True
    morning = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=1, minute=0)
    assert time_windows.is_inside_window(morning, settings) is True
    noon = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=12, minute=0)
    assert time_windows.is_inside_window(noon, settings) is False


def test_disabled_day_is_not_scheduled(client):
    pipeline = make_pipeline(client, '@v9_day')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/333333/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    # Chỉ chạy thứ hai (isoweekday = 1).
    save_schedule(client, pipeline, days_enabled_json='[1]')

    moment = time_windows.now_in('Asia/Ho_Chi_Minh')
    if moment.isoweekday() != 1:
        with m.db() as conn:
            outcome = job_allocator.plan(conn, pipeline, moment)
        assert outcome["jobs"] == []
        assert "ngày" in outcome.get("reason", ""), outcome
    assert time_windows.day_enabled(datetime(2026, 10, 19, 9, 0), [1]) is True
    assert time_windows.day_enabled(datetime(2026, 10, 20, 9, 0), [1]) is False


# --- hạn mức và khoảng cách ---------------------------------------------------

def test_account_at_the_limit_is_not_scheduled(client):
    pipeline = make_pipeline(client, '@v9_limit')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/444444/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline, daily_limit_per_account=2)
    save_schedule(client, pipeline, max_posts_per_account_per_day=3)

    with m.db() as conn:
        settings = account_limits.settings_of(conn, pipeline)
        day = account_limits.day_of(account_limits.now_in(settings['timezone']))
        for _ in range(2):
            account_limits.reserve(conn, account, 0, day)
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=9, minute=0)
    with m.db() as conn:
        outcome = job_allocator.plan(conn, pipeline, inside)
    assert outcome["jobs"] == []


def test_slots_keep_the_minimum_interval(client):
    pipeline = make_pipeline(client, '@v9_interval')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    make_group(client, pipeline, first, 'Group A', 'https://www.facebook.com/groups/555555/')
    make_group(client, pipeline, second, 'Group B', 'https://www.facebook.com/groups/666666/')
    make_media(client, pipeline, 'a.jpg')
    make_media(client, pipeline, 'b.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline, min_interval_minutes=90, max_posts_per_account_per_day=2)

    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=8, minute=0)
    with m.db() as conn:
        outcome = job_allocator.plan(conn, pipeline, inside)
    moments = [datetime.fromisoformat(job["scheduled_for"]) for job in outcome["jobs"]]
    assert len(moments) >= 2
    for first_moment, second_moment in zip(moments, moments[1:]):
        assert (second_moment - first_moment).total_seconds() >= 90 * 60 - 1


def test_no_two_accounts_post_at_the_same_moment(client):
    pipeline = make_pipeline(client, '@v9_same_time')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    make_group(client, pipeline, first, 'Group A', 'https://www.facebook.com/groups/777777/')
    make_group(client, pipeline, second, 'Group B', 'https://www.facebook.com/groups/888888/')
    make_media(client, pipeline, 'a.jpg')
    make_media(client, pipeline, 'b.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline, min_interval_minutes=60)
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=8, minute=0)
    with m.db() as conn:
        outcome = job_allocator.plan(conn, pipeline, inside)
    moments = [job["scheduled_for"] for job in outcome["jobs"]]
    assert len(moments) == len(set(moments))


# --- group -------------------------------------------------------------------

def test_group_waiting_for_approval_is_not_scheduled(client):
    pipeline = make_pipeline(client, '@v9_group')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group chờ duyệt',
               'https://www.facebook.com/groups/999999/', screening='unknown')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline)
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=9, minute=0)
    with m.db() as conn:
        outcome = job_allocator.plan(conn, pipeline, inside)
    assert outcome["jobs"] == []


def test_group_cooldown_is_respected(client):
    pipeline = make_pipeline(client, '@v9_cooldown')
    account = make_account(client, pipeline, 'Facebook A')
    group = make_group(client, pipeline, account, 'Group A',
                       'https://www.facebook.com/groups/121212/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline, group_cooldown_minutes=180)
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=9, minute=0)
    with m.db() as conn:
        # Group vừa có một bài cách đây 30 phút.
        conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)
                        VALUES(?,?,'[]',?,1)""", (pipeline, 'x', m.iso()))
        post_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,
                        scheduled_at,updated_at,requested_posting_mode,posting_status,
                        verification_status,published_at)
                        VALUES(?,?,?,?,'posted','',?,?,'anonymous','posted','verified',?)""",
                     (post_id, group, account, pipeline, m.iso(), m.iso(),
                      (inside - timedelta(minutes=30)).isoformat()))
    with m.db() as conn:
        outcome = job_allocator.plan(conn, pipeline, inside)
        cooling = group_cooldowns.cooldown_active(conn, group, inside, 180)
    assert cooling is True
    # Bài vẫn được xếp nhưng dịch ra sau thời gian nghỉ của group.
    assert outcome["jobs"], outcome
    scheduled = datetime.fromisoformat(outcome["jobs"][0]["scheduled_for"])
    # Bài cuối cách đây 30 phút nên bài mới đến sau 150 phút nữa.
    assert scheduled >= inside + timedelta(minutes=150) - timedelta(seconds=1)


# --- job trùng, restart và hai worker -----------------------------------------

def test_the_same_job_is_not_created_twice(client):
    pipeline = make_pipeline(client, '@v9_dupe')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/131313/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline)
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=9, minute=0)
    with m.db() as conn:
        first = job_allocator.allocate(conn, pipeline, inside)
        second = job_allocator.allocate(conn, pipeline, inside)
    assert len(first["created"]) == 1
    assert second["created"] == []
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
        keys = [row["idempotency_key"] for row in
                conn.execute("SELECT idempotency_key FROM auto_post_jobs").fetchall()]
    assert count == 1
    assert len(keys) == len(set(keys))


def test_two_workers_do_not_create_the_same_job(client):
    pipeline = make_pipeline(client, '@v9_workers')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/141414/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline)
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=9, minute=0)
    with m.db() as conn:
        job_allocator.allocate(conn, pipeline, inside)
    with m.db() as conn:
        again = job_allocator.allocate(conn, pipeline, inside)
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
    assert again["created"] == []
    assert count == 1


def test_schedule_survives_a_restart(client):
    pipeline = make_pipeline(client, '@v9_restart')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/151515/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline, start_time='09:30', end_time='20:45',
                  min_interval_minutes=75, max_posts_per_account_per_day=2,
                  group_cooldown_minutes=45, timezone='Asia/Ho_Chi_Minh')
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=10, minute=0)
    with m.db() as conn:
        job_allocator.allocate(conn, pipeline, inside)
    # Mô phỏng lần khởi động lại: đọc lại cấu hình từ database.
    with m.db() as conn:
        stored = job_allocator.schedule_of(conn, pipeline)
        jobs = conn.execute("SELECT * FROM auto_post_jobs").fetchall()
    assert stored["start_time"] == "09:30"
    assert stored["end_time"] == "20:45"
    assert stored["min_interval_minutes"] == 75
    assert stored["max_posts_per_account_per_day"] == 2
    assert stored["group_cooldown_minutes"] == 45
    assert len(jobs) == 1
    assert jobs[0]["scheduled_for"]


def test_history_keeps_the_reason_of_a_postponement(client):
    pipeline = make_pipeline(client, '@v9_history')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/161616/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline)
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=9, minute=0)
    with m.db() as conn:
        job_allocator.allocate(conn, pipeline, inside)
        history = job_allocator.history_of(conn, pipeline)
    assert history
    assert history[0]["action"] in ("allocated", "postponed")


# --- múi giờ -----------------------------------------------------------------

def test_timezone_change_moves_the_schedule(client):
    pipeline = make_pipeline(client, '@v9_timezone')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/171717/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline, timezone='Asia/Ho_Chi_Minh', start_time='08:00',
                  end_time='22:00')
    with m.db() as conn:
        vietnam = job_allocator.schedule_of(conn, pipeline)
    assert vietnam["timezone"] == 'Asia/Ho_Chi_Minh'
    save_schedule(client, pipeline, timezone='UTC', start_time='08:00', end_time='22:00')
    with m.db() as conn:
        utc = job_allocator.schedule_of(conn, pipeline)
    assert utc["timezone"] == 'UTC'
    # 08:00 giờ UTC là 15:00 giờ Việt Nam: vẫn nằm trong khung giờ 08:00-22:00 UTC.
    moment = datetime(2026, 10, 20, 8, 0, tzinfo=time_windows.zone('UTC'))
    assert time_windows.is_inside_window(moment, utc) is True
    moment_vietnam = datetime(2026, 10, 20, 8, 0, tzinfo=time_windows.zone('Asia/Ho_Chi_Minh'))
    assert time_windows.is_inside_window(moment_vietnam, vietnam) is True


def test_the_paused_schedule_creates_nothing(client):
    pipeline = make_pipeline(client, '@v9_paused')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/181818/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline, paused=True)
    inside = time_windows.now_in('Asia/Ho_Chi_Minh').replace(hour=9, minute=0)
    with m.db() as conn:
        outcome = job_allocator.plan(conn, pipeline, inside)
    assert outcome["jobs"] == []
    assert "tạm dừng" in outcome.get("reason", ""), outcome


# --- giao diện và API ---------------------------------------------------------

def test_the_schedule_api_returns_the_preview(client):
    pipeline = make_pipeline(client, '@v9_api')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/191919/')
    make_media(client, pipeline, 'a.jpg')
    enable_auto_post(client, pipeline)
    save_schedule(client, pipeline)

    schedule = client.get(f'/api/pipelines/{pipeline}/schedule').json()['schedule']
    assert schedule["mode"] == "spread"
    assert schedule["max_posts_per_account_per_day"] == 3
    preview = client.get(f'/api/pipelines/{pipeline}/schedule/preview').json()
    assert preview["ok"] is True
    assert "accounts" in preview
    history = client.get(f'/api/pipelines/{pipeline}/schedule/history').json()
    assert history["ok"] is True


def test_collaborator_cannot_change_the_schedule(client):
    pipeline = make_pipeline(client, '@v9_rbac')
    member = client.post('/api/admin/users', json={
        'username': 'ctv_schedule', 'password': 'ctv-password-2026-strong',
        'pipeline_id': pipeline})
    assert member.status_code == 201, member.text
    from test_app import AuthClient

    ctv = AuthClient(m.app)
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    login = ctv.post('/api/auth/login', json={
        'username': 'ctv_schedule', 'password': 'ctv-password-2026-strong', 'pipeline_key': key})
    assert login.status_code == 200, login.text
    assert ctv.put(f'/api/pipelines/{pipeline}/schedule', json={'mode': 'spread'}).status_code == 403
    assert ctv.get(f'/api/pipelines/{pipeline}/schedule/preview').status_code == 403
