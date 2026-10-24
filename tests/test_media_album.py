"""Media album and automatic group posting."""
import io
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from screening_fixtures import verify_pair
from services import auto_posting
from services.image_selection import score_candidate, select_media
from services.media_store import StorageError, store_image


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
    made["broken.jpg"] = folder / "broken.jpg"
    made["broken.jpg"].write_bytes(b"not an image at all" * 20)
    return made


def make_workspace(client, name):
    pipeline = client.post('/api/pipelines', json={'name': name}).json()['id']
    account = client.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Facebook'}).json()['id']
    group = client.post('/api/groups', json={'pipeline_id': pipeline, 'account_id': account,
                                             'label': 'Group', 'url': 'https://www.facebook.com/groups/778899/',
                                             'approved_target': True}).json()['id']
    with m.db() as conn:
        verify_pair(conn, pipeline, account, group)
    return pipeline, account, group


def upload(client, pipeline, path, name="a.jpg"):
    with open(path, "rb") as handle:
        return client.post(f'/api/pipelines/{pipeline}/media/upload',
                           files={'files': (name, handle, 'image/jpeg')})


def make_media(client, pipeline, account, path, usable=True):
    response = upload(client, pipeline, path)
    assert response.status_code == 201, response.text
    media_id = response.json()['media'][0]['id']
    if usable:
        with m.db() as conn:
            conn.execute("""UPDATE media_assets SET usable=1, safety_status='pass',
                            analysis_status='done', description='một sản phẩm đẹp',
                            tags_json='["san-pham"]', updated_at=? WHERE id=?""", (m.iso(), media_id))
    return media_id


# --- 1. a collaborator uploads many pictures ---------------------------------

def test_upload_many_pictures(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb1')
    for name in ("one.jpg", "two.jpg"):
        with open(pictures[name], "rb") as handle:
            response = client.post(f'/api/pipelines/{pipeline}/media/upload',
                                   files={'files': (name, handle, 'image/jpeg')})
        assert response.status_code == 201, response.text
    listing = client.get(f'/api/pipelines/{pipeline}/media').json()
    assert listing['counts']['total'] == 2
    assert all(item['width'] == 200 for item in listing['media'])


# --- 2. an invalid picture is refused ---------------------------------------

def test_invalid_picture_is_refused(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb2')
    response = upload(client, pipeline, pictures["broken.jpg"], name="broken.jpg")
    assert response.status_code == 422
    assert client.get(f'/api/pipelines/{pipeline}/media').json()['counts']['total'] == 0


def test_store_rejects_a_wrong_signature(tmp_path):
    with pytest.raises(StorageError):
        store_image(tmp_path, 1, "x.jpg", "image/jpeg", b"this is not an image")


# --- 3. a duplicate picture is found ----------------------------------------

def test_duplicate_picture_is_detected(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb3')
    first = upload(client, pipeline, pictures["one.jpg"], name="one.jpg").json()['media'][0]['id']
    second = upload(client, pipeline, pictures["one.jpg"], name="copy.jpg").json()['media'][0]['id']
    with m.db() as conn:
        rows = {row["id"]: row["sha256"] for row in conn.execute(
            "SELECT id, sha256 FROM media_assets WHERE pipeline_id=?", (pipeline,)).fetchall()}
    assert rows[first] == rows[second]
    assert len({value for value in rows.values()}) == 1


# --- 4. the AI describes the picture and the engine picks it ----------------

def test_selection_is_explainable_and_not_random():
    good = {"id": 1, "usable": True, "locked": False, "safety_status": "pass",
            "description": "một chiếc túi da nâu", "tags": ["tui", "da"],
            "analysis_status": "done", "usage_count": 0, "last_used_at": None, "saved": False}
    other = {"id": 2, "usable": True, "locked": False, "safety_status": "pass",
             "description": "một chiếc giày", "tags": ["giay"],
             "analysis_status": "done", "usage_count": 4, "last_used_at": "2026-01-01T00:00:00+00:00",
             "saved": False}
    choice = select_media([other, good], "túi da")
    assert choice["media"]["id"] == 1
    assert choice["reasons"]
    blocked = dict(good, id=3, safety_status="blocked")
    assert select_media([blocked], "túi da")["media"] is None
    locked = dict(good, id=4, locked=True)
    assert select_media([locked], "túi da")["media"] is None


def test_selection_prefers_the_topic_and_the_untouched_picture():
    base = {"usable": True, "locked": False, "safety_status": "pass", "analysis_status": "done",
            "saved": False, "usage_count": 0, "last_used_at": None}
    matching = dict(base, id=1, description="túi da", tags=["tui"])
    other = dict(base, id=2, description="giay dep", tags=["giay"])
    assert select_media([other, matching], "túi da")["media"]["id"] == 1


# --- 5. the AI writes a caption that fits the picture -----------------------

def test_caption_is_written_for_the_picture(monkeypatch):
    from services import ai_provider
    monkeypatch.setattr(ai_provider, "chat_json",
                        lambda *a, **k: {"caption": "Chiếc túi da mới về, mời bạn xem nhé",
                                          "hashtags": ["tui"], "reason": "theo mô tả ảnh"})
    result = auto_posting.create_caption("một chiếc túi da nâu", ["tui"], "túi da mới",
                                         ai_provider.ProviderConfig(base_url="https://x/v1", api_key="k"),
                                         posting_mode="anonymous")
    assert result["caption"].startswith("Chiếc túi da")
    assert result["status"] == "pass"


def test_caption_of_a_risky_picture_is_blocked(monkeypatch):
    from services import ai_provider
    monkeypatch.setattr(ai_provider, "chat_json",
                        lambda *a, **k: {"caption": "Cam kết hoàn tiền 100% hiệu quả, không rủi ro",
                                          "hashtags": [], "reason": ""})
    result = auto_posting.create_caption("một sản phẩm", [], "sản phẩm",
                                         ai_provider.ProviderConfig(base_url="https://x/v1", api_key="k"),
                                         posting_mode="anonymous")
    assert result["status"] == "blocked"


# --- 6. every post uses exactly one picture ---------------------------------

def test_every_post_uses_exactly_one_picture(client, pictures, monkeypatch):
    pipeline, account, group = make_workspace(client, '@alb6')
    media_id = make_media(client, pipeline, account, pictures["one.jpg"])
    with m.db() as conn:
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm mới',
        'ai_caption': False, 'daily_limit': 3, 'require_approval': True, 'enabled': True})
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert preview['media_id'] == media_id
    assert len(preview['posts']) == 1
    scheduled = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule').json()
    assert len(scheduled['created']) == 1
    with m.db() as conn:
        row = conn.execute("SELECT media_id, caption, posting_mode FROM auto_post_jobs").fetchone()
    assert row["media_id"] == media_id
    assert row["caption"]


# --- 7. a blocked picture is never used --------------------------------------

def test_blocked_picture_is_not_used_for_auto_posting(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb7')
    media_id = make_media(client, pipeline, account, pictures["one.jpg"], usable=False)
    with m.db() as conn:
        conn.execute("UPDATE media_assets SET safety_status='blocked' WHERE id=?", (media_id,))
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert preview["media_id"] is None
    assert preview["posts"] == []
    schedule = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert schedule.status_code == 409


# --- 8. the picture of another workspace is out of reach ---------------------

def test_picture_of_another_workspace_is_hidden(client, pictures):
    mine, my_account, _ = make_workspace(client, '@alb8')
    theirs, their_account, _ = make_workspace(client, '@alb9')
    their_media = make_media(client, theirs, their_account, pictures["one.jpg"])
    assert client.get(f'/api/pipelines/{mine}/media').json()['counts']['total'] == 0
    assert client.get(f'/api/pipelines/{mine}/media/{their_media}').status_code == 404
    assert client.get(f'/api/pipelines/{mine}/media/{their_media}/file').status_code == 404
    assert client.delete(f'/api/pipelines/{mine}/media/{their_media}').status_code == 404
    assert client.patch(f'/api/pipelines/{mine}/media/{their_media}',
                         json={'usable': True}).status_code == 404


# --- 9. the same post is never created twice --------------------------------

def test_no_duplicate_auto_post(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb10')
    make_media(client, pipeline, account, pictures["one.jpg"])
    with m.db() as conn:
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    first = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule').json()
    second = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule').json()
    assert len(first["created"]) == 1
    assert len(second["created"]) == 0
    assert second["skipped"]
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
    assert count == 1


# --- 10. the daily limit of the account is respected -------------------------

def test_daily_limit_is_respected(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb11')
    make_media(client, pipeline, account, pictures["one.jpg"])
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    # Bai da duoc danh trong ngay cua Workspace, tinh theo mui gio cua no.
    with m.db() as conn:
        settings = m.posting_limits.settings_of(conn, pipeline)
        # Chot giua ngay de moi mui gio UTC va mui gio Workspace deu tro ve cung
        # mot ngay, tranh viec dem sai han muc o ranh giooi.
        when = m.posting_limits.now_in(settings.get("timezone")).replace(
            hour=9, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        for _ in range(3):
            conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)
                            VALUES(?,?,?,?,1)""", (pipeline, 'x', '[]', when))
            post_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,
                            scheduled_at,updated_at,requested_posting_mode,posting_status,verification_status)
                            VALUES(?,?,?,?,'posted','',?,?,'anonymous','posted','verified')""",
                         (post_id, group, account, pipeline, when, when))
        conn.commit()
    # Bộ xếp lịch tôn trọng hạn mức của tài khoản ngay khi chuẩn bị bài.
    response = client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert response.status_code == 201, response.text
    assert response.json()["created"] == []
    with m.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM auto_post_jobs").fetchone()[0]
    assert count == 0
    # Hạn mứn cũng được kiểm tra lúc đăng, không chỉ lúc chuẩn bị.
    with m.db() as conn:
        state = m.posting_limits.account_state(
            conn, account, m.posting_limits.settings_of(conn, pipeline))
    assert state["at_limit"] is True
    assert state["used"] >= state["daily_limit"]


# --- 11. the posting identity of the group is kept ---------------------------

def test_the_mode_of_the_group_is_kept(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb12')
    with m.db() as conn:
        conn.execute("UPDATE groups SET posting_mode='nickname', nickname='Biệt danh test' WHERE id=?",
                     (group,))
        conn.execute("UPDATE group_accounts SET posting_mode='nickname', nickname='Biệt danh test' "
                     "WHERE group_id=?", (group,))
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    make_media(client, pipeline, account, pictures["one.jpg"])
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    with m.db() as conn:
        row = conn.execute("SELECT posting_mode, nickname FROM auto_post_jobs").fetchone()
    assert row["posting_mode"] == "nickname"
    assert row["nickname"] == "Biệt danh test"


def test_a_group_without_a_nickname_is_skipped(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb13')
    with m.db() as conn:
        conn.execute("UPDATE groups SET posting_mode='nickname', nickname='' WHERE id=?", (group,))
        conn.execute("UPDATE group_accounts SET posting_mode='nickname', nickname='' WHERE group_id=?",
                     (group,))
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    make_media(client, pipeline, account, pictures["one.jpg"])
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    # the group is reported but nothing publishable is prepared for it
    assert all(post.get('skipped') for post in preview['posts'])
    assert not [post for post in preview['posts'] if not post.get('skipped')]


# --- 12. a pending post is never published without an approval ---------------

def test_a_pending_post_is_not_published(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb14')
    make_media(client, pipeline, account, pictures["one.jpg"])
    with m.db() as conn:
        conn.execute("UPDATE groups SET screening_status='priority' WHERE id=?", (group,))
        conn.commit()
    client.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'sản phẩm', 'ai_caption': False,
        'daily_limit': 3, 'require_approval': True, 'enabled': True})
    client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    with m.db() as conn:
        row = conn.execute("SELECT status, approved_at FROM auto_post_jobs").fetchone()
    assert row["status"] == "pending"
    assert row["approved_at"] is None
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# --- 13. the collaborator may not do more than the permission allows ----------

def test_collaborator_needs_the_permission(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb15')
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_alb', 'password': 'ctv-album-password-2026',
                                          'pipeline_id': pipeline,
                                          'permissions': ['media.view', 'media.upload']})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_alb', 'password': 'ctv-album-password-2026',
                                             'pipeline_key': key})
    assert login.status_code == 200
    ctv.csrf = login.json()['csrf_token']
    with open(pictures["one.jpg"], "rb") as handle:
        response = ctv.post(f'/api/pipelines/{pipeline}/media/upload',
                            files={'files': ('a.jpg', handle, 'image/jpeg')})
    assert response.status_code == 201
    media_id = response.json()['media'][0]['id']
    assert ctv.get(f'/api/pipelines/{pipeline}/media').status_code == 200
    # no permission to delete, approve or to configure the automatic posting
    assert ctv.delete(f'/api/pipelines/{pipeline}/media/{media_id}').status_code == 403
    assert ctv.patch(f'/api/pipelines/{pipeline}/media/{media_id}',
                      json={'usable': True}).status_code == 403
    assert ctv.post(f'/api/pipelines/{pipeline}/auto-post/config', json={
        'account_id': account, 'group_ids': [group], 'topic': 'x', 'enabled': True}).status_code == 403
    ctv.close()


# --- 14. the old data survives the migration ---------------------------------

def test_migration_keeps_the_old_data(client, pictures):
    pipeline, account, group = make_workspace(client, '@alb16')
    make_media(client, pipeline, account, pictures["one.jpg"])
    m.init_db()
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_assets WHERE pipeline_id=?",
                            (pipeline,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pipelines WHERE id=?", (pipeline,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE id=?", (account,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM groups WHERE id=?", (group,)).fetchone()[0] == 1
