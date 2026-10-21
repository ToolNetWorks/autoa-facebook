"""AI Caption Automation: Vietnamese analysis, local rewrite and API behaviour."""
import json

import pytest
from test_app import AuthClient, client  # noqa: F401 (registers the client fixture)

import app as m
from schemas.ai_caption import (
    STATUS_APPROVED, STATUS_BLOCKED, STATUS_NEEDS_REVIEW, CaptionIssue, CaptionOptimization,
    CaptionSettings,
)
from services import ai_caption
from services.ai_caption import optimize_caption
from services.caption_validator import (
    analyze_caption, fact_values, invented_contact_details, key_facts, meaning_preserved,
    rewrite_locally,
)

CLEAN = ("Cảm ơn mọi người đã ủng hộ sản phẩm mới. Chiếc túi gọn nhẹ, chịu được mưa và "
         "có ngăn riêng cho đồ. Hẹn gặp lại ở bài viết tiếp theo!")


# --- the local validator ----------------------------------------------------

def test_clean_vietnamese_caption_has_no_issue():
    analysis = analyze_caption(CLEAN)
    assert analysis.issues == []
    assert analysis.risk_level == "low"
    assert analysis.intent


@pytest.mark.parametrize("text,category", [
    ("Sản phẩm này đỉnh thật, ai mua mẹ mày là ngu vcl", "abusive"),
    ("Đừng có làm người, đồ vô dụng", "discrimination"),
    ("Khoả thân 18+ mời vào phòng riêng", "adult"),
    ("Nếu không hợp thì tao giết chết", "violence"),
    ("Kiếm tiền online 5 phút, click link nhận tiền cùng", "scam"),
    ("Cam kết hoàn tiền 100% hiệu quả, không rủi ro", "misleading"),
])
def test_policy_violations_are_detected_in_vietnamese(text, category):
    analysis = analyze_caption(text)
    assert category in {issue.category for issue in analysis.issues}
    assert analysis.risk_level == "high"


def test_spam_and_format_signals_are_detected():
    analysis = analyze_caption("SALE SALE SALE!!! Mua ngay ngay ngay inbox ngay "
                                "#shop #shop #shop #shop #shop #shop #shop #shop #shop #shop #shop "
                                "#shop #shop liên hệ 0912345678")
    categories = {issue.category for issue in analysis.issues}
    assert "spam" in categories
    assert "format" in categories


def test_key_facts_are_extracted_and_protected():
    text = "Giá chỉ 500k cho 2kg, giao hàng toàn quốc trong 3 ngày, liên hệ 0912345678"
    facts = key_facts(text)
    assert "500k" in facts
    assert any("0912345678" in fact for fact in facts)
    assert meaning_preserved(text, "Giá 500k cho 2kg, giao 3 ngày, liên hệ 0912345678") is True
    assert meaning_preserved(text, "Sản phẩm mới về kho, liên hệ để biết giá") is False


# --- the local rewrite ------------------------------------------------------

def test_local_rewrite_keeps_the_meaning():
    cleaned, changes = rewrite_locally(CLEAN, add_hashtags=False)
    assert meaning_preserved(CLEAN, cleaned) is True
    for word in ("cảm", "ơn", "túi", "mưa"):
        assert word in cleaned.lower()


def test_local_rewrite_removes_repetition_and_shouting():
    cleaned, changes = rewrite_locally("CHÚNG TÔI RẤT VUI ĐƯỢC GIỚI THIỆU SẢN PHẨM MỚI!!",
                                       add_hashtags=False)
    assert "CHÚNG TÔI" not in cleaned
    assert "!!!" not in cleaned
    assert changes


def test_local_rewrite_limits_hashtags_but_keeps_the_body():
    text = "Sản phẩm mới " + " ".join(f"#tag{i}" for i in range(20))
    cleaned, _ = rewrite_locally(text, add_hashtags=False, max_hashtags=5)
    assert cleaned.count("#") <= 5
    assert "Sản phẩm mới" in cleaned


def test_local_rewrite_can_add_hashtags_from_the_caption_itself():
    cleaned, _ = rewrite_locally("Sản phẩm túi da handmade ở Hà Nội", add_hashtags=True, max_hashtags=3)
    assert "#" in cleaned
    assert "#tui" not in cleaned.lower()


# --- the four step pipeline -------------------------------------------------

def test_clean_caption_is_approved():
    result = optimize_caption(CLEAN, CaptionSettings())
    assert result.status == STATUS_APPROVED
    assert result.risk_level == "low"
    assert result.meaning_preserved is True


def test_policy_violation_is_blocked_and_not_laundered():
    result = optimize_caption("Sản phẩm này đỉnh thật, ai mua mẹ mày là ngu vcl", CaptionSettings())
    assert result.status == STATUS_BLOCKED
    assert result.risk_level == "high"
    assert "chính sách" in result.reason


def test_spam_caption_is_reported_and_cleaned():
    result = optimize_caption("SALE SALE!!! Mua ngay ngay #a #a #a #a #a #a #a #a #a #a #a #a #a",
                               CaptionSettings())
    assert result.status in (STATUS_APPROVED, STATUS_NEEDS_REVIEW)
    assert result.optimized_caption.count("#") <= 5


def test_provider_answer_is_never_trusted(monkeypatch):
    """A provider that claims everything is fine cannot unblock abusive content."""
    def fake_call(config, caption, analysis, settings, target):
        return CaptionOptimization(status=STATUS_APPROVED, original_caption=caption,
                                   optimized_caption="Sản phẩm rất tốt, mời bạn ủng hộ.",
                                   risk_level="low", meaning_preserved=True, reason="không có vấn đề",
                                   ai_model="fake", provider="openai-compatible", engine="openai")
    monkeypatch.setattr(ai_caption, "call_provider", fake_call)
    result = optimize_caption("Ai mua mẹ mày là ngu vcl", CaptionSettings())
    assert result.status == STATUS_BLOCKED


def test_provider_failure_never_auto_approves(monkeypatch):
    monkeypatch.setenv("AI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("AI_API_KEY", "test-key")
    monkeypatch.setenv("AI_TIMEOUT", "2")
    monkeypatch.setenv("AI_MAX_RETRIES", "0")
    ai_caption.LOG.setLevel(50)
    result = optimize_caption(CLEAN, CaptionSettings())
    assert result.status == STATUS_NEEDS_REVIEW
    assert "AI" in result.reason or "AI provider" in result.reason


# --- the provider transport -------------------------------------------------

def test_provider_config_accepts_both_env_names(monkeypatch):
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.setenv("AI_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AI_KEY", "secret")
    monkeypatch.setenv("AI_MODEL", "some/model")
    config = ai_caption.load_provider_config()
    assert config.configured is True
    assert config.base_url == "https://gateway.example/v1"
    assert config.model == "some/model"
    monkeypatch.setenv("AI_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("AI_API_KEY", "primary")
    config = ai_caption.load_provider_config()
    assert config.base_url == "https://primary.example/v1"


def test_provider_config_honours_the_workspace_model(monkeypatch):
    monkeypatch.setenv("AI_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AI_KEY", "secret")
    monkeypatch.setenv("AI_MODEL", "default/model")
    assert ai_caption.load_provider_config("workspace/model").model == "workspace/model"


def test_gateway_answer_with_a_stream_terminator_is_read():
    content = '{"optimized_caption": "Xin chào"}'
    body = json.dumps({"choices": [{"message": {"content": content}}]})
    raw = body + "data: [DONE]\n\n"
    parsed = ai_caption.parse_provider_response(raw)
    assert parsed["choices"][0]["message"]["content"] == content


def test_gateway_answer_as_a_server_sent_event_stream_is_read():
    part_one = '{"optimized_caption":'
    part_two = ' "Xin chào"}'
    chunk_one = "data: " + json.dumps({"choices": [{"delta": {"content": part_one}}]})
    chunk_two = "data: " + json.dumps({"choices": [{"delta": {"content": part_two}}]})
    parsed = ai_caption.parse_provider_response("\n\n".join([chunk_one, chunk_two, "data: [DONE]"]))
    content = parsed["choices"][0]["message"]["content"]
    assert json.loads(content)["optimized_caption"] == "Xin chào"


def test_provider_answer_may_use_the_caption_key(monkeypatch):
    """A gateway model that answers with "caption" is still understood."""
    monkeypatch.setenv("AI_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AI_KEY", "secret")

    def fake_call(config, caption, analysis, settings, target):
        return CaptionOptimization(status=STATUS_APPROVED, original_caption=caption,
                                   optimized_caption=caption, risk_level="low", reason="ok",
                                   ai_model="fake", engine="openai")
    monkeypatch.setattr(ai_caption, "call_provider", fake_call)
    result = optimize_caption(CLEAN, CaptionSettings())
    assert result.engine == "openai"
    assert result.status == STATUS_APPROVED


# --- real captions written by operators -------------------------------------

SAMPLE_ABBREVIATED = ("Khó khăn quá giao luu 1 tiếng 1 tờ dc tới lun đê\n"
                      "Lh TEIE : @khanhlinh1159\nTên-(khánh linh)")
SAMPLE_COMPACT_NUMBER = ("Em cần đóng trọ nên làm thêm ạ qua trọ em hay ngoài cũng ok em 2k3\n"
                          "Đi Lh em Tê_Le@ Huong2h")


def test_abbreviated_group_caption_is_understood():
    """A heavily abbreviated Vietnamese caption must not trip the checks."""
    analysis = analyze_caption(SAMPLE_ABBREVIATED)
    assert analysis.issues == []
    assert meaning_preserved(SAMPLE_ABBREVIATED, SAMPLE_ABBREVIATED) is True
    result = optimize_caption(SAMPLE_ABBREVIATED, CaptionSettings())
    assert result.status in (STATUS_APPROVED, STATUS_NEEDS_REVIEW)
    assert result.optimized_caption


def test_a_rewrite_that_drops_the_caption_is_refused():
    """The provider answered with a fragment in the field, so it must be rejected."""
    assert meaning_preserved(SAMPLE_ABBREVIATED, "1, 1159") is False
    assert meaning_preserved(SAMPLE_ABBREVIATED, "Khó khăn quá") is False


def test_compact_vietnamese_number_is_a_fact():
    facts = key_facts(SAMPLE_COMPACT_NUMBER)
    assert "2k3" in facts
    assert fact_values(SAMPLE_COMPACT_NUMBER) == {("number", "23")}
    # 2k3 and 23 describe the same thing
    assert meaning_preserved(SAMPLE_COMPACT_NUMBER,
                             "Em cần đóng trọ nên làm thêm ạ, em 23 tuổi") is True
    # a changed number is a changed meaning
    assert meaning_preserved(SAMPLE_COMPACT_NUMBER,
                             "Em cần đóng trọ nên làm thêm ạ, em 30 tuổi") is False


def test_a_rewrite_may_not_invent_a_contact_channel():
    assert invented_contact_details(SAMPLE_COMPACT_NUMBER,
                                    SAMPLE_COMPACT_NUMBER + " liên hệ em qua Telegram") == ["telegram"]
    assert meaning_preserved(SAMPLE_COMPACT_NUMBER,
                             SAMPLE_COMPACT_NUMBER + " liên hệ em qua Telegram") is False
    assert meaning_preserved(SAMPLE_COMPACT_NUMBER, SAMPLE_COMPACT_NUMBER) is True


def test_provider_alert_forces_a_review(monkeypatch):
    """A provider that reports a problem is not silently ignored."""
    monkeypatch.setenv("AI_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AI_KEY", "secret")

    def fake_call(config, caption, analysis, settings, target):
        return CaptionOptimization(status=STATUS_APPROVED, original_caption=caption,
                                   optimized_caption=caption, risk_level="low",
                                   reason="the provider says it is fine",
                                   issues=[CaptionIssue(category="spam", severity="high",
                                                        message="Có thông tin liên hệ bên ngoài")],
                                   ai_model="fake", engine="openai")
    monkeypatch.setattr(ai_caption, "call_provider", fake_call)
    result = optimize_caption(CLEAN, CaptionSettings())
    assert result.status == STATUS_NEEDS_REVIEW


# --- the API ----------------------------------------------------------------

_PIPELINE_SEQ = {"n": 0}


def pipeline_id(client):
    _PIPELINE_SEQ["n"] += 1
    response = client.post('/api/pipelines', json={'name': f"@aitest{_PIPELINE_SEQ['n']}"})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_optimize_requires_the_permission(client):
    """A collaborator without ai_caption.use is refused; the administrator may use it."""
    pipeline = pipeline_id(client)
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_noperm', 'password': 'ctv-ai-password-2026',
                                          'pipeline_id': pipeline, 'permissions': ['facebook.view']})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_noperm', 'password': 'ctv-ai-password-2026',
                                               'pipeline_key': key})
    assert login.status_code == 200
    ctv.csrf = login.json()['csrf_token']
    assert ctv.post(f'/api/pipelines/{pipeline}/caption/optimize',
                    json={'caption': CLEAN}).status_code == 403
    assert ctv.get(f'/api/pipelines/{pipeline}/caption/settings').status_code == 403
    ctv.close()
    # the administrator holds every permission
    assert client.post(f'/api/pipelines/{pipeline}/caption/optimize',
                       json={'caption': CLEAN}).status_code == 200


def test_optimize_creates_a_record_and_history(client):
    pipeline = pipeline_id(client)
    result = client.post(f'/api/pipelines/{pipeline}/caption/optimize', json={'caption': CLEAN})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['status'] == STATUS_APPROVED
    assert body['original_caption'] == CLEAN
    history = client.get(f'/api/pipelines/{pipeline}/caption/history').json()
    assert len(history) == 1
    assert history[0]['original_caption'] == CLEAN
    assert history[0]['approved'] is False


def test_blocked_caption_cannot_be_approved(client):
    pipeline = pipeline_id(client)
    record = client.post(f'/api/pipelines/{pipeline}/caption/optimize',
                         json={'caption': 'Ai mua mẹ mày là ngu vcl'}).json()
    assert record['status'] == STATUS_BLOCKED
    approve = client.post(f'/api/pipelines/{pipeline}/caption/approve',
                          json={'record_id': record['id']})
    assert approve.status_code == 409


def test_approve_accepts_an_edited_caption_and_rechecks_it(client):
    pipeline = pipeline_id(client)
    record = client.post(f'/api/pipelines/{pipeline}/caption/optimize',
                         json={'caption': CLEAN}).json()
    approve = client.post(f'/api/pipelines/{pipeline}/caption/approve',
                          json={'record_id': record['id'], 'optimized_caption': 'Bản đã sửa của người dùng.'})
    assert approve.status_code == 200, approve.text
    assert approve.json()['record']['approved'] is True
    assert approve.json()['record']['optimized_caption'] == 'Bản đã sửa của người dùng.'
    # an edit that breaks the policy is refused
    second = client.post(f'/api/pipelines/{pipeline}/caption/optimize',
                         json={'caption': 'Một caption sạch khác để kiểm tra'}).json()
    bad = client.post(f'/api/pipelines/{pipeline}/caption/approve',
                      json={'record_id': second['id'], 'optimized_caption': 'mẹ mày vcl'})
    assert bad.status_code == 409


def test_caption_records_are_isolated_between_pipelines(client):
    first = pipeline_id(client)
    second = pipeline_id(client)
    record = client.post(f'/api/pipelines/{first}/caption/optimize', json={'caption': CLEAN}).json()
    cross = client.post(f'/api/pipelines/{second}/caption/approve', json={'record_id': record['id']})
    assert cross.status_code == 404


def test_settings_are_per_workspace(client):
    pipeline = pipeline_id(client)
    settings = client.get(f'/api/pipelines/{pipeline}/caption/settings').json()
    assert settings['enabled'] is True
    assert settings['require_manual_review'] is True
    updated = client.patch(f'/api/pipelines/{pipeline}/caption/settings',
                           json={'add_hashtags': True, 'max_hashtags': 3})
    assert updated.status_code == 200
    assert updated.json()['add_hashtags'] is True
    assert updated.json()['max_hashtags'] == 3
    other = client.get(f'/api/pipelines/{pipeline_id(client)}/caption/settings').json()
    assert other['add_hashtags'] is False


def test_collaborator_cannot_change_the_automatic_switch(client):
    pipeline = pipeline_id(client)
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_ai', 'password': 'ctv-ai-password-2026',
                                          'pipeline_id': pipeline,
                                          'permissions': ['ai_caption.use', 'facebook.view']})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_ai', 'password': 'ctv-ai-password-2026',
                                               'pipeline_key': key})
    assert login.status_code == 200
    ctv.csrf = login.json()['csrf_token']
    assert ctv.post(f'/api/pipelines/{pipeline}/caption/optimize', json={'caption': CLEAN}).status_code == 200
    assert ctv.patch(f'/api/pipelines/{pipeline}/caption/settings',
                     json={'enabled': False}).status_code == 403
    assert ctv.patch(f'/api/pipelines/{pipeline}/caption/settings',
                     json={'add_hashtags': True}).status_code == 200
    ctv.close()


def test_collaborator_cannot_read_another_workspace_history(client):
    pipeline = pipeline_id(client)
    other = pipeline_id(client)
    client.post(f'/api/pipelines/{pipeline}/caption/optimize', json={'caption': CLEAN})
    key = client.post(f'/api/admin/pipelines/{other}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_ai2', 'password': 'ctv-ai-password-2026',
                                          'pipeline_id': other, 'permissions': ['ai_caption.use']})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_ai2', 'password': 'ctv-ai-password-2026',
                                               'pipeline_key': key})
    assert login.status_code == 200
    ctv.csrf = login.json()['csrf_token']
    assert ctv.get(f'/api/pipelines/{other}/caption/history').status_code == 200
    assert ctv.get(f'/api/pipelines/{pipeline}/caption/history').status_code == 404
    ctv.close()


# --- integration with the publishing queue ----------------------------------

def test_unapproved_caption_never_reaches_the_queue(client):
    pipeline = pipeline_id(client)
    account = client.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Facebook AI'}).json()['id']
    group = client.post('/api/groups', json={'pipeline_id': pipeline, 'account_id': account,
                                              'label': 'Group AI',
                                              'url': 'https://www.facebook.com/groups/445566/',
                                              'approved_target': True}).json()['id']
    record = client.post(f'/api/pipelines/{pipeline}/caption/optimize',
                         json={'caption': CLEAN}).json()
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('a.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + __import__('datetime').timedelta(hours=2))
    created = client.post('/api/posts', json={'pipeline_id': pipeline, 'body': CLEAN, 'group_ids': [group],
                                              'media': [image], 'anonymous_only': True,
                                              'scheduled_at': scheduled, 'ai_record_id': record['id']})
    assert created.status_code == 201, created.text
    assert created.json()['published'] is False
    assert created.json()['state'] == 'pending_approval'
    with m.db() as conn:
        row = conn.execute('SELECT body FROM posts WHERE id=?', (created.json()['id'],)).fetchone()
        job = conn.execute('SELECT state, details FROM jobs WHERE post_id=?', (created.json()['id'],)).fetchone()
    assert row['body'] == CLEAN
    assert job['state'] == 'pending_approval'
    assert 'AI' in job['details']


def test_approved_caption_is_the_one_that_gets_published(client):
    pipeline = pipeline_id(client)
    account = client.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Facebook AI 2'}).json()['id']
    group = client.post('/api/groups', json={'pipeline_id': pipeline, 'account_id': account,
                                              'label': 'Group AI 2',
                                              'url': 'https://www.facebook.com/groups/445567/',
                                              'approved_target': True}).json()['id']
    record = client.post(f'/api/pipelines/{pipeline}/caption/optimize',
                         json={'caption': CLEAN}).json()
    approved = client.post(f'/api/pipelines/{pipeline}/caption/approve',
                           json={'record_id': record['id'],
                                 'optimized_caption': 'Cảm ơn bạn, sản phẩm đã về kho.'}).json()
    assert approved['record']['approved'] is True
    image = client.post('/api/uploads', params={'pipeline_id': pipeline},
                        files={'file': ('b.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + __import__('datetime').timedelta(hours=2))
    created = client.post('/api/posts', json={'pipeline_id': pipeline, 'body': CLEAN, 'group_ids': [group],
                                              'media': [image], 'anonymous_only': True,
                                              'scheduled_at': scheduled, 'ai_record_id': record['id']})
    assert created.status_code == 201, created.text
    with m.db() as conn:
        row = conn.execute('SELECT body, ai_record_id FROM posts WHERE id=?',
                           (created.json()['id'],)).fetchone()
    assert row['body'] == 'Cảm ơn bạn, sản phẩm đã về kho.'
    assert row['ai_record_id'] == record['id']


def test_caption_of_another_pipeline_is_not_usable(client):
    first = pipeline_id(client)
    second = pipeline_id(client)
    record = client.post(f'/api/pipelines/{first}/caption/optimize', json={'caption': CLEAN}).json()
    client.post(f'/api/pipelines/{first}/caption/approve', json={'record_id': record['id']})
    account = client.post('/api/accounts', json={'pipeline_id': second, 'label': 'Facebook AI 3'}).json()['id']
    group = client.post('/api/groups', json={'pipeline_id': second, 'account_id': account,
                                              'label': 'Group AI 3',
                                              'url': 'https://www.facebook.com/groups/445568/',
                                              'approved_target': True}).json()['id']
    image = client.post('/api/uploads', params={'pipeline_id': second},
                        files={'file': ('c.jpg', b'\xff\xd8\xff' + b'0' * 60, 'image/jpeg')}).json()['path']
    scheduled = m.iso(m.now() + __import__('datetime').timedelta(hours=2))
    created = client.post('/api/posts', json={'pipeline_id': second, 'body': CLEAN, 'group_ids': [group],
                                              'media': [image], 'anonymous_only': True,
                                              'scheduled_at': scheduled, 'ai_record_id': record['id']})
    assert created.status_code == 404
