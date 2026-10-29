"""Album ảnh của CTV: sáu trạng thái rõ ràng, CTV tự quản lý được.

Những bài kiểm thử này đi đúng ba luồng mà CTV gặp hằng ngày: ảnh kiểm tra
đạt thì dùng được ngay, ảnh có rủi ro thì báo lý do, ảnh kiểm tra lỗi thì báo
đúng và kiểm tra lại được. Không trạng thái nào bị gộp vào nhau.
"""
import subprocess

import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from screening_fixtures import verify_pair
from services import media_service, media_status

CTV_PASSWORD = "ctv-album-password-2026"


@pytest.fixture(scope="session")
def pictures(tmp_path_factory):
    """A few small real images, built once with ffmpeg."""
    folder = tmp_path_factory.mktemp("album")
    made = {}
    for name, source in (("one.jpg", "testsrc=size=200x200:duration=1"),
                         ("two.jpg", "testsrc2=size=200x200:duration=1")):
        target = folder / name
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", source, "-frames:v", "1", str(target)],
                       capture_output=True, timeout=120, check=True)
        made[name] = target
    return made


def make_workspace(admin, name):
    pipeline = admin.post('/api/pipelines', json={'name': name}).json()['id']
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Facebook'}).json()['id']
    group = admin.post('/api/groups', json={'pipeline_id': pipeline, 'account_id': account,
                                            'label': 'Group', 'url': 'https://www.facebook.com/groups/778899/',
                                            'approved_target': True}).json()['id']
    with m.db() as conn:
        verify_pair(conn, pipeline, account, group)
    return pipeline, account, group


def upload(admin, pipeline, path, name="a.jpg"):
    with open(path, "rb") as handle:
        response = admin.post(f'/api/pipelines/{pipeline}/media/upload',
                               files={'files': (name, handle, 'image/jpeg')})
    assert response.status_code == 201, response.text
    return response.json()['media'][0]['id']


def make_ctv(admin, pipeline_id, username, permissions=None):
    payload = {'username': username, 'password': CTV_PASSWORD, 'pipeline_id': pipeline_id}
    if permissions is not None:
        payload['permissions'] = permissions
    created = admin.post('/api/admin/users', json=payload)
    assert created.status_code == 201, created.text
    return created.json()['id']


def login_ctv(username, key):
    ctv = AuthClient(m.app)
    response = ctv.post('/api/auth/login', json={
        'username': username, 'password': CTV_PASSWORD, 'pipeline_key': key})
    assert response.status_code == 200, response.text
    ctv.csrf = response.json().get('csrf_token', '')
    return ctv


def item_of(admin, pipeline, media_id):
    rows = admin.get(f'/api/pipelines/{pipeline}/media').json()['media']
    return next(row for row in rows if row['id'] == media_id)


def fake_analysis(monkeypatch, **overrides):
    """Thay bộ kiểm tra bằng một kết quả đã biết trước."""
    outcome = {
        "status": media_status.SAFETY_PASS, "description": "một sản phẩm đẹp",
        "tags": ["san-pham"], "risk": "none", "category": "none", "note": "",
        "people": False, "text_in_image": "", "duplicate_of": None,
        "near_duplicate_of": None, "model_version": "test", "error": "", "checked": True,
    }
    outcome.update(overrides)
    monkeypatch.setattr(media_service, "analyze_image", lambda *a, **k: dict(outcome))


# --- 1. ảnh kiểm tra đạt thì tự động dùng được, không cần duyệt --------------

def test_a_passing_picture_becomes_usable_without_any_approval(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_pass')
    fake_analysis(monkeypatch)
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    item = item_of(client, pipeline, media_id)
    assert item['state'] == media_status.READY
    assert item['state_label'] == 'Đạt'
    assert item['usable'] is True
    assert item['reason']
    assert item['can_retry'] is False


def test_a_passing_picture_needs_no_admin_and_no_permission(client, pictures, monkeypatch):
    """Không cần quyền duyệt nào cả, cũng không cần quản trị bấm gì."""
    pipeline, account, group = make_workspace(client, '@alb_pass2')
    make_ctv(client, pipeline, 'ctv_pass2')
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    ctv = login_ctv('ctv_pass2', key)
    fake_analysis(monkeypatch)
    media_id = upload(ctv, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    item = item_of(ctv, pipeline, media_id)
    assert item['state'] == media_status.READY
    assert item['usable'] is True
    ctv.close()


# --- 2. ảnh có rủi ro thì báo "Cần xem xét" kèm lý do ------------------------

def test_a_risky_picture_needs_review_with_a_reason(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_risk')
    fake_analysis(monkeypatch, status=media_status.SAFETY_REVIEW, risk="medium",
                  category="misleading", note="Ảnh có thông tin dễ gây hiểu nhầm")
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    item = item_of(client, pipeline, media_id)
    assert item['state'] == media_status.NEEDS_REVIEW
    assert item['state_label'] == 'Cần xem xét'
    assert item['reason'] == "Ảnh có thông tin dễ gây hiểu nhầm"
    assert item['usable'] is False
    # Ảnh có rủi ro thì CTV được tự quyết định dùng hay không.
    assert item['can_be_usable'] is True


def test_a_blocked_picture_cannot_be_used(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_blocked')
    fake_analysis(monkeypatch, status=media_status.SAFETY_BLOCKED, risk="high",
                  category="violence", note="Ảnh có nội dung bạo lực")
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    item = item_of(client, pipeline, media_id)
    assert item['state'] == media_status.BLOCKED
    assert item['state_label'] == 'Không đạt'
    assert item['usable'] is False
    assert item['can_be_usable'] is False


# --- 3. bộ kiểm tra lỗi thì báo đúng, không đánh dấu an toàn -----------------

def test_a_failed_check_is_not_reported_as_needs_review(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_failed')
    fake_analysis(monkeypatch, status=media_status.SAFETY_UNKNOWN,
                  error="ProviderError", description="", model_version="")
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    item = item_of(client, pipeline, media_id)
    assert item['state'] == media_status.CHECK_FAILED
    assert item['state_label'] == 'Lỗi'
    assert item['usable'] is False
    assert item['can_retry'] is True
    assert item['can_be_usable'] is False


def test_a_failed_check_is_not_reported_as_needs_review_in_the_counts(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_failed2')
    fake_analysis(monkeypatch, status=media_status.SAFETY_UNKNOWN, error="ProviderError")
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    counts = client.get(f'/api/pipelines/{pipeline}/media').json()['counts']
    assert counts['failed'] == 1
    assert counts['needs_review'] == 0
    assert counts['usable'] == 0


def test_a_picture_can_be_checked_again(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_retry')
    fake_analysis(monkeypatch, status=media_status.SAFETY_UNKNOWN, error="ProviderError")
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)
    assert item_of(client, pipeline, media_id)['state'] == media_status.CHECK_FAILED

    # Bấm "Kiểm tra lại", lần này dịch vụ trả lời bình thường.
    fake_analysis(monkeypatch)
    response = client.post(f'/api/pipelines/{pipeline}/media/{media_id}/analyze')
    assert response.status_code == 200, response.text
    assert response.json()['queued'] == [media_id]
    m.analyze_media_job(media_id)
    item = item_of(client, pipeline, media_id)
    assert item['state'] == media_status.READY
    assert item['usable'] is True


# --- 4. ảnh chờ kiểm tra không bị tính vào "Cần xem xét" ---------------------

def test_a_picture_that_is_still_being_checked_is_not_needs_review(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb_waiting')
    upload(client, pipeline, pictures["one.jpg"])
    counts = client.get(f'/api/pipelines/{pipeline}/media').json()['counts']
    assert counts['needs_review'] == 0
    assert counts['failed'] == 0
    assert counts['waiting'] == 1


def test_the_six_states_are_never_merged():
    assert media_status.state_of('pending', 'unknown') == media_status.UPLOADED
    assert media_status.state_of('analyzing', 'unknown') == media_status.ANALYZING
    assert media_status.state_of('done', 'pass') == media_status.READY
    assert media_status.state_of('done', 'needs_review') == media_status.NEEDS_REVIEW
    assert media_status.state_of('done', 'blocked') == media_status.BLOCKED
    assert media_status.state_of('error', 'unknown') == media_status.CHECK_FAILED
    # Một lần kiểm tra xong mà không có kết luận là một lần kiểm tra hỏng.
    assert media_status.state_of('done', 'unknown') == media_status.CHECK_FAILED
    assert len(set(media_status.LABEL)) == 6


# --- 5. CTV quản lý được Album của Workspace mình ---------------------------

def test_the_collaborator_manages_the_pictures_of_their_workspace(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_manage')
    make_ctv(client, pipeline, 'ctv_manage')
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    ctv = login_ctv('ctv_manage', key)
    fake_analysis(monkeypatch)
    media_id = upload(ctv, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    # Trạng thái dùng, lưu và khoá: CTV tự quyết, không cần quản trị.
    assert ctv.patch(f'/api/pipelines/{pipeline}/media/{media_id}',
                      json={'usable': False}).status_code == 200
    assert ctv.patch(f'/api/pipelines/{pipeline}/media/{media_id}',
                      json={'saved': True}).status_code == 200
    assert ctv.patch(f'/api/pipelines/{pipeline}/media/{media_id}',
                      json={'locked': True}).status_code == 200
    item = item_of(ctv, pipeline, media_id)
    assert item['saved'] is True
    assert item['locked'] is True
    ctv.close()


def test_the_collaborator_manages_a_picture_of_the_shared_pool(client, pictures, monkeypatch):
    """Ảnh trong kho chung cũng thuộc Album của Workspace."""
    pipeline, account, group = make_workspace(client, '@alb_shared')
    shared_id = upload(client, pipeline, pictures["one.jpg"])
    with m.db() as conn:
        conn.execute("UPDATE media_assets SET owner_user_id=NULL WHERE id=?", (shared_id,))
        conn.commit()
    make_ctv(client, pipeline, 'ctv_shared')
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    ctv = login_ctv('ctv_shared', key)

    assert ctv.get(f'/api/pipelines/{pipeline}/media').json()['counts']['total'] == 1
    assert ctv.patch(f'/api/pipelines/{pipeline}/media/{shared_id}',
                      json={'saved': True}).status_code == 200
    assert ctv.delete(f'/api/pipelines/{pipeline}/media/{shared_id}').status_code == 200
    ctv.close()


def test_the_collaborator_cannot_mark_a_blocked_picture_as_usable(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_nobypass')
    make_ctv(client, pipeline, 'ctv_nobypass')
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    ctv = login_ctv('ctv_nobypass', key)
    fake_analysis(monkeypatch, status=media_status.SAFETY_BLOCKED, risk="high",
                  category="violence")
    media_id = upload(ctv, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    response = ctv.patch(f'/api/pipelines/{pipeline}/media/{media_id}', json={'usable': True})
    assert response.status_code == 409, response.text
    assert item_of(ctv, pipeline, media_id)['usable'] is False
    ctv.close()


def test_the_collaborator_cannot_mark_a_failed_picture_as_usable(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_nobypass2')
    make_ctv(client, pipeline, 'ctv_nobypass2')
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    ctv = login_ctv('ctv_nobypass2', key)
    fake_analysis(monkeypatch, status=media_status.SAFETY_UNKNOWN, error="ProviderError")
    media_id = upload(ctv, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)

    response = ctv.patch(f'/api/pipelines/{pipeline}/media/{media_id}', json={'usable': True})
    assert response.status_code == 409, response.text
    assert item_of(ctv, pipeline, media_id)['usable'] is False
    ctv.close()


def test_not_even_the_administrator_bypasses_a_blocked_picture(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_nobypass3')
    fake_analysis(monkeypatch, status=media_status.SAFETY_BLOCKED, risk="high",
                  category="violence")
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)
    response = client.patch(f'/api/pipelines/{pipeline}/media/{media_id}', json={'usable': True})
    assert response.status_code == 409, response.text
    assert item_of(client, pipeline, media_id)['usable'] is False


# --- 6. cô lập dữ liệu giữa các Workspace -----------------------------------

def test_the_album_of_another_workspace_is_out_of_reach(client, pictures):
    mine, my_account, _ = make_workspace(client, '@alb_iso_mine')
    theirs, their_account, _ = make_workspace(client, '@alb_iso_theirs')
    their_media = upload(client, theirs, pictures["one.jpg"])
    make_ctv(client, mine, 'ctv_iso')
    key = client.post(f'/api/admin/pipelines/{mine}/key').json()['key']
    ctv = login_ctv('ctv_iso', key)

    assert ctv.get(f'/api/pipelines/{mine}/media').json()['counts']['total'] == 0
    assert ctv.get(f'/api/pipelines/{mine}/media/{their_media}').status_code == 404
    assert ctv.get(f'/api/pipelines/{mine}/media/{their_media}/file').status_code == 404
    assert ctv.patch(f'/api/pipelines/{mine}/media/{their_media}',
                      json={'usable': True}).status_code == 404
    assert ctv.delete(f'/api/pipelines/{mine}/media/{their_media}').status_code == 404
    ctv.close()


def test_the_collaborator_always_reads_their_own_workspace(client, pictures):
    mine, my_account, _ = make_workspace(client, '@alb_iso2_mine')
    theirs, their_account, _ = make_workspace(client, '@alb_iso2_theirs')
    upload(client, theirs, pictures["one.jpg"])
    make_ctv(client, mine, 'ctv_iso2')
    key = client.post(f'/api/admin/pipelines/{mine}/key').json()['key']
    ctv = login_ctv('ctv_iso2', key)

    # Một liên kết cũ trỏ sang Workspace khác vẫn phải cho đúng Workspace của CTV.
    rows = ctv.get(f'/api/pipelines/{theirs}/media').json()['media']
    assert rows == []
    ctv.close()


# --- 7. ảnh trong Album được dùng cho bài đăng tự động ----------------------

def test_a_passing_picture_is_used_for_the_automatic_posting(client, pictures, monkeypatch):
    """Ảnh kiểm tra đạt phải được lấy ra dùng cho lịch đăng tự động."""
    pipeline, account, group = make_workspace(client, '@alb_autopost')
    fake_analysis(monkeypatch)
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)
    with m.db() as conn:
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert preview['media_id'] == media_id
    assert len(preview['posts']) == 1


def test_a_failed_picture_is_never_used_for_the_automatic_posting(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_autopost2')
    fake_analysis(monkeypatch, status=media_status.SAFETY_UNKNOWN, error="ProviderError")
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)
    with m.db() as conn:
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert preview['media_id'] is None
    assert preview['posts'] == []


def test_a_picture_reviewed_by_the_collaborator_is_used(client, pictures, monkeypatch):
    """Ảnh có cảnh báo mà CTV đã xem xét thì được dùng cho lịch đăng."""
    pipeline, account, group = make_workspace(client, '@alb_review_use')
    fake_analysis(monkeypatch, status=media_status.SAFETY_REVIEW, risk="medium",
                  category="misleading", note="Cần xem xét")
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.analyze_media_job(media_id)
    with m.db() as conn:
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    # Chưa duyệt thì chưa dùng.
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert preview['media_id'] is None
    # CTV xem xét rồi cho phép dùng thì ảnh được lấy ra đăng.
    assert client.patch(f'/api/pipelines/{pipeline}/media/{media_id}',
                        json={'usable': True}).status_code == 200
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert preview['media_id'] == media_id


def test_a_picture_whose_check_failed_is_never_selected(client, pictures, monkeypatch):
    """Kể cả khi cờ dùng bị bật bằng tay, ảnh kiểm tra lỗi cũng không dùng."""
    from services.image_selection import select_media
    candidate = {"id": 1, "usable": True, "locked": False, "safety_status": "unknown",
                 "analysis_status": "error", "description": "sản phẩm", "tags": [],
                 "usage_count": 0, "last_used_at": None, "saved": False}
    assert select_media([candidate], "sản phẩm")["media"] is None


# --- 8. ảnh cũ không bị mất --------------------------------------------------

def test_the_pictures_of_the_workspace_survive(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb_keep')
    first = upload(client, pipeline, pictures["one.jpg"], name="one.jpg")
    second = upload(client, pipeline, pictures["two.jpg"], name="two.jpg")
    fake_analysis(monkeypatch)
    m.analyze_media_job(first)
    m.analyze_media_job(second)
    with m.db() as conn:
        rows = conn.execute("SELECT id, original_name FROM media_assets WHERE pipeline_id=?",
                            (pipeline,)).fetchall()
    assert sorted(row["original_name"] for row in rows) == ["one.jpg", "two.jpg"]
    assert {row["id"] for row in rows} == {first, second}


def test_the_old_data_survives_the_migration(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb_migrate')
    media_id = upload(client, pipeline, pictures["one.jpg"])
    m.init_db()
    with m.db() as conn:
        row = conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
    assert row is not None
    assert row["original_name"] == "a.jpg"
    assert row["pipeline_id"] == pipeline
