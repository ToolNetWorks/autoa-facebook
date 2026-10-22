"""Chiến dịch đăng group với nhiều group.

Bộ kiểm thử này phủ đúng những gì người dùng gặp phải: chọn 1 group, 49,
50, 51, 65, 100 group; chọn trùng id; chọn group của Workspace khác; group đang
tạm dừng; group chưa từng đăng; group cần Admin Facebook duyệt; group dùng
chế độ biệt danh; và nhiều group thuộc nhiều Facebook.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from services import group_post_validator, group_verification


def make_pipeline(admin, name):
    return admin.post('/api/pipelines', json={'name': name}).json()['id']


def make_account(admin, pipeline_id, label, session="connected"):
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label}).json()
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status=? WHERE id=?", (session, account['id']))
    return account['id']


#: Bộ đếm để mỗi group có một đường dẫn khác nhau trong Workspace.
_URL_COUNTER = [70000]


def make_groups(admin, pipeline_id, account_id, count, prefix="G"):
    """Tạo nhiều group trong một Workspace."""
    ids = []
    for index in range(count):
        _URL_COUNTER[0] += 1
        group = admin.post(f'/api/pipelines/{pipeline_id}/groups', json={
            'pipeline_id': pipeline_id, 'account_id': account_id,
            'label': f"{prefix} {index}",
            'url': f"https://www.facebook.com/groups/{_URL_COUNTER[0]}/",
            'approved_target': True, 'posting_mode': 'anonymous'}).json()
        ids.append(group['id'])
    return ids


def upload_image(admin, pipeline_id, name="a.jpg"):
    return admin.post('/api/uploads', params={'pipeline_id': pipeline_id},
                      files={'file': (name, b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']


def schedule(hours=2):
    return m.iso(m.now() + m.timedelta(hours=hours))


def campaign(admin, pipeline_id, group_ids, media=None):
    return admin.post(f'/api/pipelines/{pipeline_id}/group-posts', json={
        'group_ids': list(group_ids), 'body': 'Xin chào', 'media': [media or upload_image(admin, pipeline_id)],
        'scheduled_at': schedule()})


# --- số group ----------------------------------------------------------------

@pytest.mark.parametrize("count", [1, 49, 50, 51, 65, 100])
def test_a_campaign_accepts_many_groups(client, count):
    """Chọn 1, 49, 50, 51, 65 hay 100 group đều không lỗi vì giới hạn cứng."""
    pipeline = make_pipeline(client, f'@camp{count}')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, count)
    response = campaign(client, pipeline, groups)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['jobs_created'] == count, body
    assert body['skipped'] == []
    with m.db() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE pipeline_id=?", (pipeline,)).fetchall()
    assert len(rows) == count
    # Mỗi group một job riêng, không phải một job cho tất cả.
    assert len({row['group_id'] for row in rows}) == count
    # Cùng một ảnh dùng chung cho cả chiến dịch, không tải lại nhiều lần.
    assert len({row['post_id'] for row in rows}) == 1


def test_the_limit_comes_from_the_configuration(client, monkeypatch):
    """Giới hạn lấy từ cấu hình, không phải hằng số viết cứng."""
    monkeypatch.setenv("MAX_GROUPS_PER_CAMPAIGN", "10")
    pipeline = make_pipeline(client, '@camp_limit')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 11)
    response = campaign(client, pipeline, groups)
    assert response.status_code == 422
    detail = response.json()['detail']
    assert "10" in detail
    # Không có job nào được tạo khi yêu cầu bị từ chối.
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_id=?",
                            (pipeline,)).fetchone()[0] == 0


def test_the_limit_is_reported_to_the_interface(client):
    pipeline = make_pipeline(client, '@camp_limits')
    response = client.get(f'/api/pipelines/{pipeline}/group-post/limits')
    assert response.status_code == 200
    body = response.json()
    assert body['max_groups'] == group_post_validator.max_groups_per_campaign()


# --- id trùng ----------------------------------------------------------------

def test_duplicate_ids_are_removed_not_an_error(client):
    pipeline = make_pipeline(client, '@camp_dup')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 3)
    chosen = [groups[0], groups[0], groups[1], groups[0], groups[2]]
    response = campaign(client, pipeline, chosen)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['jobs_created'] == 3
    assert body['selected'] == 3


# --- group của Workspace khác -------------------------------------------------

def test_a_group_of_another_workspace_is_refused(client):
    mine = make_pipeline(client, '@camp_mine')
    theirs = make_pipeline(client, '@camp_theirs')
    my_account = make_account(client, mine, 'Facebook')
    their_account = make_account(client, theirs, 'Facebook')
    their_group = make_groups(client, theirs, their_account, 1, prefix="Cua ho")[0]
    response = campaign(client, mine, [their_group])
    assert response.status_code == 422
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_id=?", (mine,)).fetchone()[0] == 0


# --- group tạm dừng -----------------------------------------------------------

def test_a_paused_group_is_reported_and_the_others_still_run(client):
    pipeline = make_pipeline(client, '@camp_paused')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 3)
    with m.db() as conn:
        conn.execute("UPDATE groups SET enabled=0 WHERE id=?", (groups[1],))
        conn.commit()
    response = campaign(client, pipeline, groups)
    assert response.status_code == 201
    body = response.json()
    assert body['jobs_created'] == 2
    skipped = {item['group_id']: item for item in body['skipped']}
    assert groups[1] in skipped
    assert skipped[groups[1]]['reason']
    # Hai group còn lại vẫn được lên lịch: không mất dữ liệu của group nào.
    assert {item['group_id'] for item in body['jobs']} == {groups[0], groups[2]}


# --- group chưa từng đăng ----------------------------------------------------

def test_a_group_that_never_posted_can_get_its_first_post(client):
    pipeline = make_pipeline(client, '@camp_first')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 3)
    response = campaign(client, pipeline, groups)
    assert response.status_code == 201
    assert response.json()['jobs_created'] == 3
    with m.db() as conn:
        for group_id in groups:
            status = group_verification.status_of(conn, pipeline, account, group_id)
            assert status == group_verification.NOT_TESTED


# --- group cần Admin Facebook duyệt -------------------------------------------

def test_a_group_needing_approval_is_served_but_marked(client):
    pipeline = make_pipeline(client, '@camp_approval')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 2)
    with m.db() as conn:
        group_verification.record_outcome(conn, pipeline, account, groups[0],
                                          group_verification.PENDING_GROUP_APPROVAL)
    response = campaign(client, pipeline, groups)
    assert response.status_code == 201
    body = response.json()
    assert body['jobs_created'] == 2
    with m.db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE group_id=?", (groups[0],)).fetchone()
    assert row['state'] == 'queued'


# --- chế độ biệt danh --------------------------------------------------------

def test_a_group_with_a_nickname_keeps_its_own_mode(client):
    pipeline = make_pipeline(client, '@camp_nick')
    account = make_account(client, pipeline, 'Facebook')
    group = admin_group = client.post(f'/api/pipelines/{pipeline}/groups', json={
        'pipeline_id': pipeline, 'account_id': account, 'label': 'Biệt danh',
        'url': 'https://www.facebook.com/groups/71000/', 'approved_target': True,
        'posting_mode': 'nickname', 'nickname': 'Tên hay'}).json()
    response = campaign(client, pipeline, [group['id']])
    assert response.status_code == 201
    assert response.json()['jobs_created'] == 1
    with m.db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE pipeline_id=?", (pipeline,)).fetchone()
    assert row['requested_posting_mode'] == 'nickname'
    assert row['requested_nickname'] == 'Tên hay'


def test_a_nickname_group_without_a_name_is_reported(client):
    pipeline = make_pipeline(client, '@camp_nick2')
    account = make_account(client, pipeline, 'Facebook')
    group = client.post(f'/api/pipelines/{pipeline}/groups', json={
        'pipeline_id': pipeline, 'account_id': account, 'label': 'Thiếu tên',
        'url': 'https://www.facebook.com/groups/72000/', 'approved_target': True,
        'posting_mode': 'nickname', 'nickname': 'Tạm'}).json()
    # Tên biệt danh bị xoá sau đó: group còn chế độ biệt danh nhưng chưa có tên.
    with m.db() as conn:
        conn.execute("UPDATE groups SET nickname='' WHERE id=?", (group['id'],))
        conn.execute("UPDATE group_accounts SET nickname='' WHERE group_id=?", (group['id'],))
        conn.commit()
    response = campaign(client, pipeline, [group['id']])
    assert response.status_code == 201
    body = response.json()
    assert body['jobs_created'] == 0
    assert 'biệt danh' in body['skipped'][0]['reason'].lower()


# --- nhiều group thuộc nhiều Facebook -----------------------------------------

def test_many_groups_across_many_accounts(client):
    pipeline = make_pipeline(client, '@camp_multi')
    accounts = [make_account(client, pipeline, f'Facebook {index}') for index in range(3)]
    groups = []
    for index, account in enumerate(accounts):
        groups.extend(make_groups(client, pipeline, account, 20, prefix=f"A{index}"))
    response = campaign(client, pipeline, groups)
    assert response.status_code == 201
    assert response.json()['jobs_created'] == 60
    with m.db() as conn:
        rows = conn.execute("""SELECT account_id, COUNT(*) n FROM jobs WHERE pipeline_id=?
                               GROUP BY account_id""", (pipeline,)).fetchall()
    assert len(rows) == 3
    assert sum(row['n'] for row in rows) == 60


# --- idempotency --------------------------------------------------------------

def test_the_same_campaign_twice_does_not_duplicate_jobs(client):
    pipeline = make_pipeline(client, '@camp_idem')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 5)
    media = upload_image(client, pipeline)
    first = campaign(client, pipeline, groups, media)
    second = campaign(client, pipeline, groups, media)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()['jobs_created'] == 5
    assert second.json()['jobs_created'] == 0
    assert second.json()['duplicates']
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_id=?", (pipeline,)).fetchone()[0]
    assert count == 5


# --- preflight ----------------------------------------------------------------

def test_the_preflight_reports_before_anything_is_uploaded(client):
    """Danh sách group được kiểm tra trước khi người dùng tải ảnh lên."""
    pipeline = make_pipeline(client, '@camp_pre')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 3)
    response = client.post(f'/api/pipelines/{pipeline}/group-posts/preflight', json={
        'group_ids': groups, 'body': 'Xin chào', 'media': ['uploads/1/x.jpg'],
        'scheduled_at': schedule()})
    assert response.status_code == 200
    body = response.json()
    assert body['selected'] == 3
    assert len(body['ready']) == 3
    assert body['skipped'] == []
    # Chưa có job nào được tạo.
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_id=?",
                            (pipeline,)).fetchone()[0] == 0


def test_the_preflight_reports_the_groups_that_need_work(client):
    pipeline = make_pipeline(client, '@camp_pre2')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 3)
    with m.db() as conn:
        conn.execute("UPDATE groups SET enabled=0 WHERE id=?", (groups[0],))
        conn.commit()
    response = client.post(f'/api/pipelines/{pipeline}/group-posts/preflight', json={
        'group_ids': groups, 'body': 'Xin chào', 'media': ['uploads/1/x.jpg'],
        'scheduled_at': schedule()})
    body = response.json()
    assert len(body['ready']) == 2
    assert body['skipped'][0]['group_id'] == groups[0]


def test_an_invalid_list_is_refused_before_the_image_is_staged(client):
    """Không để lại ảnh tạm khi danh sách group không hợp lệ."""
    pipeline = make_pipeline(client, '@camp_clean')
    account = make_account(client, pipeline, 'Facebook')
    other = make_pipeline(client, '@camp_clean_other')
    other_account = make_account(client, other, 'Facebook')
    foreign = make_groups(client, other, other_account, 1, prefix="X")[0]
    media = upload_image(client, pipeline)
    response = campaign(client, pipeline, [foreign], media)
    assert response.status_code == 422
    # Ảnh vẫn còn nguyên vì đó là ảnh người dùng đã tải lên từ trước.
    with m.db() as conn:
        row = conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_id=?", (pipeline,)).fetchone()[0]
    assert row == 0


# --- hạn mức -----------------------------------------------------------------

def test_the_daily_limit_is_respected_by_a_campaign(client):
    pipeline = make_pipeline(client, '@camp_limit')
    account = make_account(client, pipeline, 'Facebook')
    groups = make_groups(client, pipeline, account, 10)
    with m.db() as conn:
        conn.execute("""INSERT INTO account_posting_limits(account_id,daily_limit,min_gap_minutes,
                                                      group_daily_limit,paused,updated_at)
                        VALUES(?,?,?,?,0,?)""",
                     (account, 2, 0, 0, m.iso()))
        conn.commit()
    response = campaign(client, pipeline, groups)
    assert response.status_code == 201
    body = response.json()
    # Mọi group đều được lên lịch: phần vượt hạn mức chờ đến ngày sau, và
    # worker giữ nhịp đăng nên không group nào bị mất.
    assert body['jobs_created'] == 10
    assert body['skipped'] == []
    assert len(body['deferred']) == 8
    assert all(item['reason'] for item in body['deferred'])
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE pipeline_id=?",
                             (pipeline,)).fetchone()[0]
    assert count == 10
