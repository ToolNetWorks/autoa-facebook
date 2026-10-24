import os
import sqlite3
from datetime import timedelta
from pathlib import Path

os.environ.setdefault('APP_USERNAME', 'admin')
os.environ.setdefault('APP_PASSWORD', 'a-valid-very-long-test-password')
os.environ.setdefault('APP_SECRET', 'test-secret-is-at-least-32-characters-long')

from fastapi.testclient import TestClient
import app as m
import pytest


class AuthClient(TestClient):
    """Test client that keeps the CSRF header in sync with the session cookie."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.csrf = ""

    def request(self, method, url, **kwargs):
        if method.upper() not in ("GET", "HEAD", "OPTIONS") and self.csrf:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", self.csrf)
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'DATA', tmp_path)
    monkeypatch.setattr(m, 'DB_PATH', tmp_path / 'test.sqlite3')
    monkeypatch.setattr(m, 'UPLOADS', tmp_path / 'uploads')
    monkeypatch.setattr(m, 'EVIDENCE', tmp_path / 'evidence')
    monkeypatch.setattr(m, 'PROFILES', tmp_path / 'profiles')
    for folder in (m.UPLOADS, m.EVIDENCE, m.PROFILES):
        folder.mkdir(parents=True, exist_ok=True)
    
    # Create the destination_locks table in the test database
    import sqlite3
    conn = sqlite3.connect(str(m.DB_PATH), timeout=25)
    try:
        # Check if destination_locks table exists
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='destination_locks'")
        if cursor.fetchone() is None:
            # Create destination_locks table
            conn.execute("""
                CREATE TABLE destination_locks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pipeline_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    destination_username TEXT NOT NULL,
                    display_label TEXT NOT NULL,
                    allowed_labels_json TEXT NOT NULL,
                    is_locked INTEGER NOT NULL DEFAULT 1,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(pipeline_id, platform, destination_username)
                )
            """)
            conn.commit()
    finally:
        conn.close()
    
    with AuthClient(m.app) as c:
        login = c.post('/api/auth/login', json={"username": "admin", "password": os.environ['APP_PASSWORD']})
        assert login.status_code == 200, login.text
        c.csrf = login.json()['csrf_token']
        yield c


def test_authentication_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'DB_PATH', tmp_path / 'guard.sqlite3')
    with TestClient(m.app) as c:
        assert c.get('/api/pipelines').status_code == 401
        assert c.post('/api/auth/login', json={"username": "admin", "password": "wrong"}).status_code == 401


def test_full_pipeline_flow(client):
    create = client.post('/api/pipelines', json={'name': '@hiu9h'})
    assert create.status_code == 201, create.text
    pipeline_id = create.json()['id']
    assert client.post('/api/pipelines', json={'name':'@hiu9h'}).status_code == 409
    account = client.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': 'Tài khoản 1'})
    assert account.status_code == 201
    account_id = account.json()['id']
    link = 'https://www.facebook.com/groups/123456/?ref=share'
    group = client.post('/api/groups', json={'pipeline_id': pipeline_id, 'account_id': account_id,
                                              'label': 'Nhóm được phép', 'url': link,
                                              'approved_target': True})
    assert group.status_code == 201, group.text
    group_id = group.json()['id']
    assert client.post('/api/groups', json={'pipeline_id': pipeline_id, 'account_id': account_id,
                                           'label': 'Duplicate', 'url': link,
                                           'approved_target': True}).status_code == 409
    image = client.post('/api/uploads', params={'pipeline_id': pipeline_id},
                        files={'file': ('photo.jpg', b'\xff\xd8\xff' + b'\x00' * 90, 'image/jpeg')})
    assert image.status_code == 200, image.text
    image_path = image.json()['path']
    future = m.iso(m.now() + timedelta(hours=5))
    post = client.post('/api/posts', json={'pipeline_id':pipeline_id, 'body':'Nội dung đã duyệt',
                                           'group_ids':[group_id], 'media':[image_path], 'anonymous_only':True,'scheduled_at':future})
    assert post.status_code == 201, post.text
    assert post.json()['jobs_created'] == 1
    job = client.get('/api/jobs').json()[0]
    assert job['state'] == 'queued'
    assert client.post(f'/api/jobs/{job["id"]}/review', json={'state':'cancelled'}).status_code == 200
    assert client.get('/api/jobs').json()[0]['state'] == 'cancelled'


def test_link_validation(client):
    for bad in ('http://www.facebook.com/groups/2', 'https://evil.com/groups/2',
                'https://facebook.com/groups/2/posts/3', 'https://facebook.com.evil.org/groups/2'):
        with pytest.raises(Exception):
            m.group_link(bad)
    assert m.group_link('https://m.facebook.com/groups/MyPublicGroup/?abc=1') == (
        'https://www.facebook.com/groups/MyPublicGroup/', 'mypublicgroup')


def test_cross_pipeline_and_permissions(client):
    p1=client.post('/api/pipelines',json={'name':'@one'}).json()['id']
    p2=client.post('/api/pipelines',json={'name':'@two'}).json()['id']
    a=client.post('/api/accounts',json={'pipeline_id':p1,'label':'Profile'}).json()['id']
    # A row of another workspace is hidden completely (404), never confirmed.
    assert client.post('/api/groups',json={'pipeline_id':p2,'account_id':a,'label':'Wrong group',
                 'url':'https://facebook.com/groups/5678/','approved_target':True}).status_code in (404,422)
    gid=client.post('/api/groups',json={'pipeline_id':p1,'account_id':a,'label':'Read only',
                 'url':'https://facebook.com/groups/5678/','approved_target':False}).json()['id']
    img = client.post('/api/uploads', params={'pipeline_id': p1},
                      files={'file': ('a.jpg', b'\xff\xd8\xff' + b'\x00'*50, 'image/jpeg')}).json()['path']
    invalid=client.post('/api/posts',json={'pipeline_id':p1,'body':'test','media':[img],'group_ids':[gid],
             'scheduled_at':m.iso(m.now()+timedelta(hours=1))})
    assert invalid.status_code==409


def fake_precheck(client, pipeline_id, account_id, media_path, caption="", status="PASS"):
    """Store a finished pre-check so the publish guard can be exercised.

    The real check needs a real video and a vision model, so the tests insert
    the outcome directly and keep the guard itself under test.
    """
    from pathlib import Path
    from services.media_fingerprint import file_sha256
    name = Path(media_path).name
    path = m.UPLOADS / str(pipeline_id) / name
    if not path.is_file():
        path = m.UPLOADS / name
    sha = ""
    try:
        sha = file_sha256(path)
    except OSError:
        sha = ""
    created = client.post(f'/api/pipelines/{pipeline_id}/reels/precheck', json={
        'account_id': account_id, 'media_path': name, 'caption': caption})
    assert created.status_code == 202, created.text
    check_id = created.json()['id']
    with m.db() as conn:
        conn.execute("""UPDATE video_prechecks SET status=?, video_sha256=?, caption_checked=?,
                        checked_at=?, progress='Đã hoàn tất', updated_at=? WHERE id=?""",
                     (status, sha, (caption or "").strip(), m.iso(), m.iso(), check_id))
    return check_id
