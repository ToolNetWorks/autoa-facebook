"""V8: tự động đăng group cho cả Pipeline.

Cấu hình một lần ở cấp Pipeline, mỗi Facebook có hạn mức riêng, và bộ chống
đăng trùng chặn được ảnh lặp, caption lặp và hai Facebook cùng nhắm vào một
group.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m
from screening_fixtures import verify_pair
from services import duplicate_guard, posting_limits, post_scheduler


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
    # Chế độ biệt danh bắt buộc có tên, nên tạo rồi xoá tên để mô phỏng
    # group thiếu tên biệt danh.
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


def settings_payload(**overrides):
    payload = {'enabled': True, 'scope': 'all', 'daily_limit_per_account': 3,
               'min_gap_minutes': 0, 'pipeline_daily_limit': 0, 'topic': 'sản phẩm',
               'ai_caption': False, 'anti_duplicate': True, 'require_approval': True,
               'apply_to_new_accounts': True}
    payload.update(overrides)
    return payload


# --- 1. một Pipeline nhiều Facebook ------------------------------------------

def test_one_configuration_covers_every_account(client):
    pipeline = make_pipeline(client, '@v8_multi')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    make_group(client, pipeline, first, 'Group A', 'https://www.facebook.com/groups/111111/')
    make_group(client, pipeline, second, 'Group B', 'https://www.facebook.com/groups/222222/')
    make_media(client, pipeline, 'a.jpg')
    make_media(client, pipeline, 'b.jpg')

    saved = client.put(f'/api/pipelines/{pipeline}/auto-post/settings',
                       json=settings_payload()).json()
    assert saved['settings']['enabled'] is True
    assert saved['settings']['daily_limit_per_account'] == 3

    with m.db() as conn:
        outcome = post_scheduler.plan(conn, pipeline)
    accounts = {job["account_id"] for job in outcome["jobs"]}
    assert accounts == {first, second}, outcome["jobs"]
    assert len(outcome["jobs"]) == 2


# --- 2. phạm vi áp dụng ------------------------------------------------------

def test_scope_can_limit_the_accounts(client):
    pipeline = make_pipeline(client, '@v8_scope')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    make_group(client, pipeline, first, 'Group A', 'https://www.facebook.com/groups/333333/')
    make_group(client, pipeline, second, 'Group B', 'https://www.facebook.com/groups/444444/')
    make_media(client, pipeline, 'a.jpg')
    make_media(client, pipeline, 'b.jpg')

    client.put(f'/api/pipelines/{pipeline}/auto-post/settings',
               json=settings_payload(scope='selected', account_ids=[first]))
    with m.db() as conn:
        outcome = post_scheduler.plan(conn, pipeline)
    assert {job["account_id"] for job in outcome["jobs"]} == {first}


# --- 3. Facebook mới dùng luôn cấu hình chung ---------------------------------

def test_new_account_uses_the_shared_configuration(client):
    pipeline = make_pipeline(client, '@v8_new')
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    added = make_account(client, pipeline, 'Facebook mới')
    with m.db() as conn:
        settings = posting_limits.settings_of(conn, pipeline)
        limit = posting_limits.effective_limit(conn, added, settings)
    assert settings["apply_to_new_accounts"] is True
    assert settings["scope"] == "all"
    assert limit == 3


# --- 4. hạn mức riêng của một tài khoản ---------------------------------------

def test_account_can_have_its_own_limit(client):
    pipeline = make_pipeline(client, '@v8_limit')
    account = make_account(client, pipeline, 'Facebook A')
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings',
               json=settings_payload(daily_limit_per_account=3))
    client.put(f'/api/pipelines/{pipeline}/auto-post/limits/{account}',
               json={'daily_limit': 5})
    with m.db() as conn:
        settings = posting_limits.settings_of(conn, pipeline)
        assert posting_limits.effective_limit(conn, account, settings) == 5
        other = make_account(client, pipeline, 'Facebook B')
        assert posting_limits.effective_limit(conn, other, settings) == 3


# --- 5. tài khoản hết hạn mức -------------------------------------------------

def test_account_at_the_limit_is_skipped(client):
    pipeline = make_pipeline(client, '@v8_atlimit')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A', 'https://www.facebook.com/groups/555555/')
    make_media(client, pipeline, 'a.jpg')
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        settings = posting_limits.settings_of(conn, pipeline)
        day = posting_limits.day_of(posting_limits.now_in(settings['timezone']))
        for _ in range(3):
            posting_limits.reserve(conn, account, 0, day)
    with m.db() as conn:
        outcome = post_scheduler.plan(conn, pipeline)
    assert outcome["jobs"] == []
    assert any("bài hôm nay" in note for note in outcome["notes"]), outcome["notes"]


def test_reserved_slot_is_kept_until_it_is_verified(client):
    pipeline = make_pipeline(client, '@v8_reserve')
    account = make_account(client, pipeline, 'Facebook A')
    with m.db() as conn:
        settings = posting_limits.settings_of(conn, pipeline)
        day = posting_limits.day_of(posting_limits.now_in(settings['timezone']))
        posting_limits.reserve(conn, account, 0, day)
        usage = posting_limits.usage_of(conn, account, day)
    assert usage["reserved"] == 1
    assert usage["used"] == 1


# --- 6. group bị hạn chế ------------------------------------------------------

def test_restricted_group_is_never_scheduled(client):
    pipeline = make_pipeline(client, '@v8_restricted')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group bị hạn chế',
               'https://www.facebook.com/groups/666666/', screening='restricted')
    make_media(client, pipeline, 'a.jpg')
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        outcome = post_scheduler.plan(conn, pipeline)
    assert outcome["jobs"] == []
    with m.db() as conn:
        settings = posting_limits.settings_of(conn, pipeline)
        blockers = post_scheduler.group_blockers(post_scheduler._group_rows(conn, pipeline, settings))
    assert blockers and "hạn chế" in blockers[0]["reason"]


def test_group_without_nickname_is_skipped(client):
    pipeline = make_pipeline(client, '@v8_nickname')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group biệt danh',
               'https://www.facebook.com/groups/777777/', mode='nickname')
    make_media(client, pipeline, 'a.jpg')
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        outcome = post_scheduler.plan(conn, pipeline)
    assert outcome["jobs"] == []
    assert any("biệt danh" in note for note in outcome["notes"]), outcome["notes"]


# --- 7. trùng ảnh -------------------------------------------------------------

def test_the_same_picture_is_not_used_twice(client):
    pipeline = make_pipeline(client, '@v8_dup_image')
    account = make_account(client, pipeline, 'Facebook A')
    group = make_group(client, pipeline, account, 'Group A',
                       'https://www.facebook.com/groups/888888/')
    make_media(client, pipeline, 'a.jpg', sha='a' * 64)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        first = post_scheduler.distribute(conn, pipeline)
        second = post_scheduler.distribute(conn, pipeline)
    assert len(first["created"]) == 1
    assert second["created"] == []
    assert any("đã được" in note for note in second["skipped"]), second["skipped"]


# --- 8. trùng caption --------------------------------------------------------

def test_the_same_caption_is_not_used_twice_in_one_group(client):
    pipeline = make_pipeline(client, '@v8_dup_caption')
    account = make_account(client, pipeline, 'Facebook A')
    group = make_group(client, pipeline, account, 'Group A',
                       'https://www.facebook.com/groups/999999/')
    make_media(client, pipeline, 'a.jpg', sha='a' * 64)
    make_media(client, pipeline, 'b.jpg', sha='b' * 64)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings',
               json=settings_payload(anti_duplicate=True))
    with m.db() as conn:
        first = post_scheduler.distribute(conn, pipeline)
        second = post_scheduler.distribute(conn, pipeline)
    assert len(first["created"]) == 1
    # Ảnh khác nhưng caption giống và cùng group nên vẫn bị chặn.
    assert second["created"] == []


def test_caption_is_normalized_before_the_check(client):
    first = duplicate_guard.caption_fingerprint("Bán sỉ  đẹp!")
    second = duplicate_guard.caption_fingerprint("bán sỉ đẹp")
    assert first == second


def test_near_duplicate_picture_is_blocked(client):
    signature = "0" * 64
    near = "1" * 2 + "0" * 62
    assert duplicate_guard.is_near_duplicate(signature, near)


# --- 9. hai Facebook cùng nhắm vào một group ---------------------------------

def test_two_accounts_cannot_post_the_same_content_into_one_group(client):
    pipeline = make_pipeline(client, '@v8_two_accounts')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group A',
                       'https://www.facebook.com/groups/121212/')
    make_media(client, pipeline, 'a.jpg', sha='c' * 64)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,group_id,account_id,posting_mode,
                          nickname,status,created_at,updated_at)
                          VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, group, second, m.iso(), m.iso()))
    with m.db() as conn:
        first_run = post_scheduler.distribute(conn, pipeline)
        second_run = post_scheduler.distribute(conn, pipeline)
    assert len(first_run["created"]) == 1
    assert second_run["created"] == []


# --- 10. hai worker chạy đồng thời -------------------------------------------

def test_two_workers_do_not_create_the_same_job(client):
    pipeline = make_pipeline(client, '@v8_two_workers')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/131313/')
    make_media(client, pipeline, 'a.jpg', sha='d' * 64)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        first = post_scheduler.distribute(conn, pipeline)
    with m.db() as conn:
        second = post_scheduler.distribute(conn, pipeline)
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
    assert len(first["created"]) == 1
    assert second["created"] == []
    assert count == 1


# --- 11. scheduler chạy lại sau khi restart -----------------------------------

def test_scheduler_is_idempotent_after_a_restart(client):
    pipeline = make_pipeline(client, '@v8_restart')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/141414/')
    make_media(client, pipeline, 'a.jpg', sha='e' * 64)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        post_scheduler.distribute(conn, pipeline)
    # Mô phỏng lần khởi động lại: bộ đếm được dựng lại từ bảng jobs.
    with m.db() as conn:
        posting_limits.reconcile(conn, pipeline)
        again = post_scheduler.distribute(conn, pipeline)
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
    assert again["created"] == []
    assert count == 1


# --- 12. không tạo job trùng -------------------------------------------------

def test_the_same_job_is_not_created_twice(client):
    pipeline = make_pipeline(client, '@v8_no_dupe')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/151515/')
    make_media(client, pipeline, 'a.jpg', sha='f' * 64)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        post_scheduler.distribute(conn, pipeline)
        keys = [row["idempotency_key"] for row in
                conn.execute("SELECT idempotency_key FROM auto_post_jobs").fetchall()]
    assert len(keys) == len(set(keys))


# --- 13. CTV không vượt quyền -------------------------------------------------

def test_collaborator_cannot_change_the_configuration(client):
    pipeline = make_pipeline(client, '@v8_rbac')
    member = client.post('/api/admin/users', json={
        'username': 'ctv_autopost', 'password': 'ctv-password-2026-strong',
        'pipeline_id': pipeline})
    assert member.status_code == 201, member.text
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={
        'username': 'ctv_autopost', 'password': 'ctv-password-2026-strong',
        'pipeline_key': client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']})
    assert login.status_code == 200, login.text
    assert ctv.put(f'/api/pipelines/{pipeline}/auto-post/settings',
                   json=settings_payload()).status_code == 403
    assert ctv.post(f'/api/pipelines/{pipeline}/auto-post/distribute').status_code == 403
    assert ctv.get(f'/api/pipelines/{pipeline}/auto-post/dashboard').status_code == 403


# --- 14. nội dung chưa qua kiểm tra an toàn ----------------------------------

def test_content_without_safety_pass_is_not_scheduled(client):
    pipeline = make_pipeline(client, '@v8_safety')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/161616/')
    media_id = make_media(client, pipeline, 'a.jpg', sha='9' * 64)
    with m.db() as conn:
        conn.execute("UPDATE media_assets SET safety_status='needs_review', usable=0 WHERE id=?",
                     (media_id,))
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        outcome = post_scheduler.plan(conn, pipeline)
    assert outcome["jobs"] == []
    assert outcome["no_media"] is True


def test_blocked_picture_is_not_scheduled(client):
    pipeline = make_pipeline(client, '@v8_blocked')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/171717/')
    media_id = make_media(client, pipeline, 'a.jpg', sha='8' * 64)
    with m.db() as conn:
        conn.execute("UPDATE media_assets SET safety_status='blocked', usable=0 WHERE id=?",
                     (media_id,))
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    with m.db() as conn:
        outcome = post_scheduler.plan(conn, pipeline)
    assert outcome["jobs"] == []


# --- bảng điều khiển ----------------------------------------------------------

def test_dashboard_counts_the_accounts_and_the_groups(client):
    pipeline = make_pipeline(client, '@v8_dashboard')
    account = make_account(client, pipeline, 'Facebook A')
    make_group(client, pipeline, account, 'Group A',
               'https://www.facebook.com/groups/181818/')
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json=settings_payload())
    dashboard = client.get(f'/api/pipelines/{pipeline}/auto-post/dashboard').json()
    assert dashboard["totals"]["accounts"] == 1
    assert dashboard["totals"]["eligible_groups"] == 1
    assert dashboard["totals"]["total_groups"] == 1
    assert dashboard["accounts"][0]["daily_limit"] == 3
    assert dashboard["accounts"][0]["remaining"] == 3


def test_limits_are_stored_per_account(client):
    pipeline = make_pipeline(client, '@v8_store')
    account = make_account(client, pipeline, 'Facebook A')
    client.put(f'/api/pipelines/{pipeline}/auto-post/limits/{account}',
               json={'daily_limit': 2, 'min_gap_minutes': 30, 'group_daily_limit': 1,
                     'paused': True})
    limits = client.get(f'/api/pipelines/{pipeline}/auto-post/limits').json()['limits']
    row = next(item for item in limits if item['account_id'] == account)
    assert row['daily_limit'] == 2
    assert row['min_gap_minutes'] == 30
    assert row['group_daily_limit'] == 1
    assert row['paused'] is True
