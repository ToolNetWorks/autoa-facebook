"""Tự động yêu cầu tham gia group và theo dõi tư cách thành viên.

Mười trường hợp bắt buộc: đã là thành viên, chưa tham gia, group cần Admin
duyệt thành viên, group có câu hỏi, group từ chối tham gia, Facebook bị
checkpoint, trình duyệt quá thời gian, nhiều Facebook cùng một group, phiên
Facebook hết hạn, và tham gia thành công nhưng chưa xác minh được đăng bài.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from services import group_membership, group_verification


def make_pipeline(admin, name):
    return admin.post('/api/pipelines', json={'name': name}).json()['id']


def make_account(admin, pipeline_id, label, session="connected"):
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': label}).json()
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status=? WHERE id=?",
                     (session, account['id']))
    return account['id']


def make_group(admin, pipeline_id, account_id, label, url):
    group = admin.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': label, 'url': url,
        'approved_target': True, 'posting_mode': 'anonymous'}).json()
    return group['id']


def make_ctv(admin, pipeline_id, username):
    created = admin.post('/api/admin/users', json={
        'username': username, 'password': 'ctv-join-password-2026', 'pipeline_id': pipeline_id})
    assert created.status_code == 201, created.text
    return created.json()['id']


def login_ctv(admin, pipeline_id, username):
    key = admin.post(f'/api/admin/pipelines/{pipeline_id}/key').json()['key']
    ctv = AuthClient(m.app)
    response = ctv.post('/api/auth/login', json={
        'username': username, 'password': 'ctv-join-password-2026', 'pipeline_key': key})
    assert response.status_code == 200, response.text
    ctv.csrf = response.json()['csrf_token']
    return ctv


def fake_join(monkeypatch, outcome):
    """Thay thao tác trình duyệt bằng một kết quả đã biết trước."""
    calls = []

    def runner(pipeline_id, account_id, group_id, answers=None):
        calls.append((pipeline_id, account_id, group_id, answers or {}))
        return dict(outcome)

    monkeypatch.setattr(m, "run_group_join", runner)
    return calls


# --- 1. Facebook đã là thành viên --------------------------------------------

def test_an_account_that_is_already_a_member_is_not_asked_again(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn1')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30001/')
    calls = fake_join(monkeypatch, {"state": "ok", "status": "joined", "membership": "joined",
                                    "details": "Đã là thành viên group.", "questions": []})
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.JOINED)
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    assert response.status_code == 200
    assert response.json()['state'] == 'joined'
    assert calls == [], "Đã là thành viên thì không gửi yêu cầu nữa"


# --- 2. Facebook chưa tham gia ------------------------------------------------

def test_an_account_that_has_not_joined_is_asked(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn2')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30002/')
    calls = fake_join(monkeypatch, {"state": "ok", "status": "pending_approval",
                                    "membership": "pending_approval",
                                    "details": "Đang chờ group duyệt.", "questions": []})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    assert response.status_code == 200
    assert response.json()['membership'] == group_membership.PENDING_APPROVAL
    assert len(calls) == 1
    with m.db() as conn:
        assert group_membership.status_of(conn, pipeline, account, group) \
            == group_membership.PENDING_APPROVAL


# --- 3. group cần Admin duyệt thành viên ------------------------------------

def test_a_group_that_needs_approval_keeps_waiting(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn3')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30003/')
    fake_join(monkeypatch, {"state": "ok", "status": "pending_approval",
                            "membership": "pending_approval", "details": "", "questions": []})
    client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, group, group_membership.PENDING_APPROVAL)
    calls = fake_join(monkeypatch, {"state": "ok", "status": "joined", "membership": "joined",
                                    "details": "", "questions": []})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    assert response.status_code == 200
    assert response.json()['state'] == 'pending_approval'
    assert calls == [], "Yêu cầu đang chờ duyệt thì không gửi lại"


# --- 4. group có câu hỏi -----------------------------------------------------

def test_a_group_with_questions_asks_the_collaborator(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn4')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30004/')
    questions = ["Bạn làm việc ở đâu?", "Bạn đã đồng ý với quy định của group chưa?"]
    fake_join(monkeypatch, {"state": "needs_answers", "status": "needs_answers",
                            "membership": "not_member", "details": "Group yêu cầu trả lời câu hỏi.",
                            "questions": questions})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    assert response.status_code == 200
    body = response.json()
    assert body['questions'] == questions
    assert 'câu hỏi' in body['message'].lower()
    with m.db() as conn:
        assert group_membership.status_of(conn, pipeline, account, group) \
            == group_membership.NOT_MEMBER


def test_the_answers_of_the_collaborator_are_used(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn4b')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30005/')
    questions = ["Tên của bạn là gì?"]
    fake_join(monkeypatch, {"state": "needs_answers", "status": "needs_answers",
                            "membership": "not_member", "details": "", "questions": questions})
    client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    calls = fake_join(monkeypatch, {"state": "ok", "status": "joined", "membership": "joined",
                                    "details": "", "questions": []})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join/answers',
                           json={"answers": {"Tên của bạn là gì?": "Nguyễn Văn A"}})
    assert response.status_code == 200
    assert calls and calls[0][3] == {"Tên của bạn là gì?": "Nguyễn Văn A"}
    with m.db() as conn:
        saved = conn.execute("SELECT join_answers_json FROM groups WHERE id=?",
                             (group,)).fetchone()["join_answers_json"]
    assert "Nguyễn Văn A" in saved


def test_unanswered_questions_are_refused(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn4c')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30006/')
    fake_join(monkeypatch, {"state": "needs_answers", "status": "needs_answers",
                            "membership": "not_member", "details": "", "questions": ["Câu hỏi?"]})
    client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join/answers',
                           json={"answers": {}})
    assert response.status_code == 422


# --- 5. group từ chối tham gia ------------------------------------------------

def test_a_group_that_refuses_the_request_is_recorded(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn5')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30007/')
    fake_join(monkeypatch, {"state": "failed", "status": "rejected", "membership": "join_rejected",
                            "details": "Group từ chối yêu cầu tham gia.", "questions": []})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    assert response.status_code == 200
    assert response.json()['membership'] == group_membership.JOIN_REJECTED
    with m.db() as conn:
        assert group_membership.status_of(conn, pipeline, account, group) \
            == group_membership.JOIN_REJECTED


# --- 6. Facebook bị checkpoint ------------------------------------------------

def test_a_checkpoint_stops_and_asks_the_collaborator(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn6')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30008/')
    fake_join(monkeypatch, {"state": "failed", "status": "checkpoint", "membership": "unknown",
                            "details": "Facebook yêu cầu xác minh; cần bạn xử lý.",
                            "questions": []})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    assert response.status_code == 200
    assert response.json()['membership'] == group_membership.UNKNOWN
    assert 'xác minh' in response.json()['message'].lower()


# --- 7. trình duyệt quá thời gian --------------------------------------------

def test_a_timeout_is_not_treated_as_a_refusal(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn7')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30009/')
    fake_join(monkeypatch, {"state": "failed", "status": "timeout", "membership": "join_failed",
                            "details": "Kiểm tra quá thời gian; chưa rõ kết quả nên không gửi lại.",
                            "questions": []})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    assert response.status_code == 200
    assert response.json()['membership'] == group_membership.JOIN_FAILED
    with m.db() as conn:
        assert group_membership.status_of(conn, pipeline, account, group) \
            != group_membership.JOIN_REJECTED


# --- 8. nhiều Facebook cùng một group --------------------------------------

def test_each_account_of_a_group_has_its_own_membership(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn8')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group', 'https://www.facebook.com/groups/30010/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,
                                                    nickname,status,created_at,updated_at)
                        VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, second, group, m.iso(), m.iso()))
        conn.commit()
        group_membership.record(conn, pipeline, first, group, group_membership.JOINED)
        assert group_membership.status_of(conn, pipeline, first, group) \
            == group_membership.JOINED
        assert group_membership.status_of(conn, pipeline, second, group) \
            == group_membership.UNKNOWN


# --- 9. phiên Facebook hết hạn ----------------------------------------------

def test_an_expired_session_is_refused(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn9')
    account = make_account(client, pipeline, 'Facebook', session='expired')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30011/')
    calls = fake_join(monkeypatch, {"state": "failed", "status": "error",
                                    "membership": "join_failed", "details": "", "questions": []})
    response = client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    assert response.status_code == 200
    # Lần tham gia vẫn được ghi nhận, nhưng không phải là từ chối của group.
    assert response.json()['membership'] != group_membership.JOIN_REJECTED


# --- 10. tham gia xong nhưng chưa xác minh được đăng bài ---------------------

def test_joining_does_not_verify_the_posting_right(client, monkeypatch):
    """Tham gia được group không có nghĩa là đã xác minh được quyền đăng."""
    pipeline = make_pipeline(client, '@jn10')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30012/')
    fake_join(monkeypatch, {"state": "ok", "status": "joined", "membership": "joined",
                            "details": "Đã là thành viên group.", "questions": []})
    client.post(f'/api/pipelines/{pipeline}/groups/{group}/join', json={})
    with m.db() as conn:
        assert group_membership.status_of(conn, pipeline, account, group) \
            == group_membership.JOINED
        # Bằng chứng đăng bài vẫn chưa có.
        assert group_verification.status_of(conn, pipeline, account, group) \
            == group_verification.NOT_TESTED


# --- tự động tham gia: chỉ group CTV đã bật --------------------------------

def test_only_the_groups_the_collaborator_enabled_are_joined(client, monkeypatch):
    pipeline = make_pipeline(client, '@jn11')
    account = make_account(client, pipeline, 'Facebook')
    enabled = make_group(client, pipeline, account, 'Group bật',
                         'https://www.facebook.com/groups/30013/')
    disabled = make_group(client, pipeline, account, 'Group tắt',
                          'https://www.facebook.com/groups/30014/')
    response = client.patch(f'/api/pipelines/{pipeline}/groups/{enabled}/auto-join',
                            json={'auto_join': True})
    assert response.status_code == 200
    with m.db() as conn:
        group_membership.record(conn, pipeline, account, enabled, group_membership.NOT_MEMBER)
        group_membership.record(conn, pipeline, account, disabled, group_membership.NOT_MEMBER)
        pending = group_membership.pending_requests(conn, pipeline)
    assert [row["group_id"] for row in pending] == [enabled]


def test_the_collaborator_can_turn_auto_join_on(client):
    pipeline = make_pipeline(client, '@jn12')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group', 'https://www.facebook.com/groups/30015/')
    make_ctv(client, pipeline, 'ctv_join')
    ctv = login_ctv(client, pipeline, 'ctv_join')
    response = ctv.patch(f'/api/pipelines/{pipeline}/groups/{group}/auto-join',
                         json={'auto_join': True})
    assert response.status_code == 200
    with m.db() as conn:
        value = conn.execute("SELECT auto_join FROM groups WHERE id=?", (group,)).fetchone()[0]
    assert value == 1
    ctv.close()


def test_a_collaborator_cannot_join_a_group_of_another_workspace(client, monkeypatch):
    mine = make_pipeline(client, '@jn13')
    theirs = make_pipeline(client, '@jn14')
    their_account = make_account(client, theirs, 'Facebook')
    their_group = make_group(client, theirs, their_account, 'Group',
                             'https://www.facebook.com/groups/30016/')
    make_ctv(client, mine, 'ctv_iso_join')
    ctv = login_ctv(client, mine, 'ctv_iso_join')
    response = ctv.post(f'/api/pipelines/{mine}/groups/{their_group}/join', json={})
    assert response.status_code == 404
    ctv.close()
