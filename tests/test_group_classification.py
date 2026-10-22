"""Phân loại group, ưu tiên và tag chế độ đăng.

Mười lăm trường hợp bắt buộc: bài đầu hiện trực tiếp, bài đầu chờ duyệt,
bài chờ duyệt rồi được duyệt vẫn là *cần duyệt*, group chưa từng đăng, lỗi
trình duyệt, group hỗ trợ ẩn danh, group hỗ trợ biệt danh, group hỗ trợ cả
hai, group không hỗ trợ chế độ được chọn, hai Facebook khác nhau trong cùng
group, bộ xếp lịch ưu tiên group đăng trực tiếp, group cần duyệt không bị
loại bỏ vô lý, phân loại được cập nhật khi quy định group thay đổi, refresh
hoặc restart không mất kết quả, và không lẫn dữ liệu giữa các Workspace.
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


def make_group(admin, pipeline_id, account_id, label, url, mode='anonymous', nickname='',
               requires_approval=False):
    payload = nickname or ('Tạm' if mode == 'nickname' else '')
    group = admin.post(f'/api/pipelines/{pipeline_id}/groups', json={
        'pipeline_id': pipeline_id, 'account_id': account_id, 'label': label, 'url': url,
        'approved_target': True, 'posting_mode': mode, 'nickname': payload}).json()
    with m.db() as conn:
        conn.execute("UPDATE groups SET requires_approval=? WHERE id=?",
                     (int(requires_approval), group['id']))
        conn.commit()
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


def record_post_result(conn, pipeline_id, account_id, group_id, status, mode="anonymous",
                       evidence=None):
    """Ghi lại kết quả đăng bài thật cho một cặp."""
    group_verification.record_outcome(conn, pipeline_id, account_id, group_id, status,
                                     posting_mode=mode)
    group_classification.record_outcome(
        conn, pipeline_id, account_id, group_id, status,
        group_requires_approval=bool(evidence.get("requires_approval"))
        if evidence else False,
        evidence=evidence or {}, evidence_reference="test")


# --- 1. bài đầu hiện trực tiếp -> Group Đăng Không Cần Duyệt --------------

def test_a_first_post_that_appears_is_a_no_approval_group(client):
    pipeline = make_pipeline(client, '@cl1')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/40001/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group, group_verification.VERIFIED_POSTED)
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert entry["classification"] == group_classification.NO_APPROVAL
    assert entry["label"] == "Group Đăng Không Cần Duyệt"
    assert entry["priority"] == group_classification.PRIORITY_HIGH
    assert entry["priority_rank"] == 0


# --- 2. bài đầu chờ Admin group duyệt -> Group Cần Duyệt ---------------------

def test_a_first_post_waiting_for_approval_is_a_needs_approval_group(client):
    pipeline = make_pipeline(client, '@cl2')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/40002/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group,
                          group_verification.PENDING_GROUP_APPROVAL)
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert entry["classification"] == group_classification.NEEDS_APPROVAL
    assert entry["label"] == "Group Cần Duyệt"
    assert entry["priority"] == group_classification.PRIORITY_LOW


# --- 3. bài chờ duyệt rồi được duyệt vẫn là Group Cần Duyệt ---------------

def test_a_group_whose_posts_wait_for_approval_stays_needs_approval(client):
    """Bài cuối cùng được duyệt không làm group thành đăng không cần duyệt."""
    pipeline = make_pipeline(client, '@cl3')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group cần duyệt',
                       'https://www.facebook.com/groups/40003/', requires_approval=True)
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group,
                          group_verification.PENDING_GROUP_APPROVAL)
        # Lần sau bài đã hiện trong group, nhưng group vẫn cần duyệt.
        record_post_result(conn, pipeline, account, group,
                          group_verification.VERIFIED_POSTED,
                          evidence={"requires_approval": True})
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert entry["classification"] == group_classification.NEEDS_APPROVAL
    assert entry["priority"] == group_classification.PRIORITY_LOW


# --- 4. group chưa từng đăng -> chưa phân loại, vẫn chuẩn bị được bài đầu --

def test_a_group_that_never_posted_is_unclassified_and_can_start(client):
    pipeline = make_pipeline(client, '@cl4')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group mới',
                       'https://www.facebook.com/groups/40004/')
    with m.db() as conn:
        entry = group_classification.classification_of(conn, pipeline, account, group)
        decision = group_eligibility.pair_decision(conn, pipeline, account, group)
    assert entry["classification"] == group_classification.UNCLASSIFIED
    assert entry["label"] == "Chưa phân loại"
    assert decision["eligible"] is True
    assert decision["decision"] == group_eligibility.FIRST_POST_READY


# --- 5. lỗi trình duyệt -> không kết luận gì --------------------------------

def test_a_browser_failure_leaves_the_group_unclassified(client):
    pipeline = make_pipeline(client, '@cl5')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/40005/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group, group_verification.CHECK_FAILED)
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert entry["classification"] == group_classification.UNCLASSIFIED
    assert entry["priority"] == group_classification.PRIORITY_NONE


# --- 6. group hỗ trợ ẩn danh ------------------------------------------------

def test_a_group_supporting_anonymous_is_tagged(client):
    pipeline = make_pipeline(client, '@cl6')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group ẩn danh',
                       'https://www.facebook.com/groups/40006/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group, group_verification.VERIFIED_POSTED,
                           evidence={"supports_anonymous": True, "supports_nickname": False})
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert entry["supports_anonymous"] is True
    assert entry["supports_nickname"] is False
    assert entry["capability_state"] == group_classification.CAPABILITY_KNOWN


# --- 7. group hỗ trợ biệt danh ------------------------------------------------

def test_a_group_supporting_nickname_is_tagged(client):
    pipeline = make_pipeline(client, '@cl7')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group biệt danh',
                       'https://www.facebook.com/groups/40007/', mode='nickname',
                       nickname='Tên hay')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group, group_verification.VERIFIED_POSTED,
                           mode="nickname",
                           evidence={"supports_anonymous": False, "supports_nickname": True})
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert entry["supports_nickname"] is True
    assert entry["supports_anonymous"] is False


# --- 8. group hỗ trợ cả hai chế độ ------------------------------------------

def test_a_group_supporting_both_modes_has_both_tags(client):
    pipeline = make_pipeline(client, '@cl8')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group cả hai',
                       'https://www.facebook.com/groups/40008/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group, group_verification.VERIFIED_POSTED,
                           evidence={"supports_anonymous": True, "supports_nickname": True})
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert entry["supports_anonymous"] is True
    assert entry["supports_nickname"] is True


# --- 9. group không hỗ trợ chế độ được chọn ---------------------------------

def test_a_group_without_the_chosen_mode_is_not_eligible(client):
    pipeline = make_pipeline(client, '@cl9')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group không ẩn danh',
                       'https://www.facebook.com/groups/40009/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_screening_results
            (pipeline_id,account_id,group_id,moderation_status,verification_source,confidence,
             evidence_json,last_checked_at,expires_at,notes,created_at,updated_at)
            VALUES(?,?,?,'unknown','browser_check',0.0,?,?,?,'',?,?)""",
            (pipeline, account, group,
             '{"supports_anonymous": false, "supports_nickname": true}',
             m.iso(), m.iso(), m.iso(), m.iso()))
        conn.commit()
        decision = group_eligibility.pair_decision(conn, pipeline, account, group)
    assert decision['eligible'] is False
    assert 'ẩn danh' in decision['reason'].lower()


# --- 10. hai Facebook khác nhau trong cùng group ------------------------------

def test_two_accounts_of_one_group_have_different_classifications(client):
    pipeline = make_pipeline(client, '@cl10')
    first = make_account(client, pipeline, 'Facebook A')
    second = make_account(client, pipeline, 'Facebook B')
    group = make_group(client, pipeline, first, 'Group chung',
                       'https://www.facebook.com/groups/40010/')
    with m.db() as conn:
        conn.execute("""INSERT INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,
                                                    nickname,status,created_at,updated_at)
                        VALUES(?,?,?,'anonymous','','active',?,?)""",
                     (pipeline, second, group, m.iso(), m.iso()))
        record_post_result(conn, pipeline, first, group, group_verification.VERIFIED_POSTED)
        record_post_result(conn, pipeline, second, group,
                          group_verification.PENDING_GROUP_APPROVAL)
        one = group_classification.classification_of(conn, pipeline, first, group)
        two = group_classification.classification_of(conn, pipeline, second, group)
    assert one["classification"] == group_classification.NO_APPROVAL
    assert two["classification"] == group_classification.NEEDS_APPROVAL


# --- 11. bộ xếp lịch ưu tiên group đăng trực tiếp ---------------------------

def test_the_scheduler_serves_the_direct_group_first(client):
    pipeline = make_pipeline(client, '@cl11')
    account = make_account(client, pipeline, 'Facebook')
    direct = make_group(client, pipeline, account, 'Group trực tiếp',
                        'https://www.facebook.com/groups/40011/')
    waiting = make_group(client, pipeline, account, 'Group cần duyệt',
                         'https://www.facebook.com/groups/40012/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, direct, group_verification.VERIFIED_POSTED)
        record_post_result(conn, pipeline, account, waiting,
                          group_verification.PENDING_GROUP_APPROVAL)
    make_picture(client, pipeline, 'a.jpg')
    make_picture(client, pipeline, 'b.jpg')
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json={
        'enabled': True, 'scope': 'selected', 'account_ids': [], 'group_ids': [direct, waiting],
        'daily_limit_per_account': 2, 'min_gap_minutes': 0, 'pipeline_daily_limit': 0,
        'topic': 'sản phẩm', 'ai_caption': False, 'anti_duplicate': True,
        'require_approval': True, 'apply_to_new_accounts': True})
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    served = [post['group_id'] for post in preview['posts']]
    assert direct in served
    assert waiting in served
    assert served.index(direct) < served.index(waiting), "Group đăng trực tiếp phải được phục vụ trước"


# --- 12. group cần duyệt không bị loại bỏ vô lý -----------------------------

def test_a_needs_approval_group_is_still_served(client):
    pipeline = make_pipeline(client, '@cl12')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group cần duyệt',
                       'https://www.facebook.com/groups/40013/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group,
                          group_verification.PENDING_GROUP_APPROVAL)
    make_picture(client, pipeline)
    client.put(f'/api/pipelines/{pipeline}/auto-post/settings', json={
        'enabled': True, 'scope': 'selected', 'account_ids': [], 'group_ids': [group],
        'daily_limit_per_account': 2, 'min_gap_minutes': 0, 'pipeline_daily_limit': 0,
        'topic': 'sản phẩm', 'ai_caption': False, 'anti_duplicate': True,
        'require_approval': True, 'apply_to_new_accounts': True})
    preview = client.post(f'/api/pipelines/{pipeline}/auto-post/preview').json()['preview']
    assert [post['group_id'] for post in preview['posts']] == [group]


# --- 13. phân loại được cập nhật khi quy định group thay đổi ----------------

def test_the_classification_follows_the_new_evidence(client):
    pipeline = make_pipeline(client, '@cl13')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/40014/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group, group_verification.VERIFIED_POSTED)
        assert group_classification.classification_of(
            conn, pipeline, account, group)["classification"] == group_classification.NO_APPROVAL
        # Lần sau group chuyển sang cần duyệt: phân loại phải theo bằng chứng mới.
        record_post_result(conn, pipeline, account, group,
                          group_verification.PENDING_GROUP_APPROVAL)
        entry = group_classification.classification_of(conn, pipeline, account, group)
        history = group_classification.history_of(conn, pipeline, account, group)
    assert entry["classification"] == group_classification.NEEDS_APPROVAL
    assert history and history[0]["to_classification"] == group_classification.NEEDS_APPROVAL


# --- 14. refresh hoặc restart không mất kết quả -----------------------------

def test_the_classification_survives_a_restart(client):
    pipeline = make_pipeline(client, '@cl14')
    account = make_account(client, pipeline, 'Facebook')
    group = make_group(client, pipeline, account, 'Group',
                       'https://www.facebook.com/groups/40015/')
    with m.db() as conn:
        record_post_result(conn, pipeline, account, group, group_verification.VERIFIED_POSTED)
    m.init_db()
    with m.db() as conn:
        entry = group_classification.classification_of(conn, pipeline, account, group)
    assert entry["classification"] == group_classification.NO_APPROVAL
    listing = client.get(f'/api/pipelines/{pipeline}/groups/verifications').json()
    row = next(item for item in listing['verifications'] if item['group_id'] == group)
    assert row['classification'] == group_classification.NO_APPROVAL


# --- 15. không lẫn dữ liệu giữa các Workspace -------------------------------

def test_the_classification_never_crosses_workspaces(client):
    mine = make_pipeline(client, '@cl15')
    theirs = make_pipeline(client, '@cl15b')
    my_account = make_account(client, mine, 'Facebook')
    their_account = make_account(client, theirs, 'Facebook')
    my_group = make_group(client, mine, my_account, 'Group của tôi',
                          'https://www.facebook.com/groups/40016/')
    their_group = make_group(client, theirs, their_account, 'Group của họ',
                             'https://www.facebook.com/groups/40017/')
    with m.db() as conn:
        record_post_result(conn, mine, my_account, my_group, group_verification.VERIFIED_POSTED)
        entry = group_classification.classification_of(conn, mine, my_account, their_group)
    assert entry["classification"] == group_classification.UNCLASSIFIED
    assert entry["configured"] is False
    listing = client.get(f'/api/pipelines/{theirs}/groups/verifications').json()
    assert all(item['group_id'] != my_group for item in listing['verifications'])
