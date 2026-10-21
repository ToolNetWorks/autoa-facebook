"""AI video pre-check: technical quality, safety categories and the publish guard."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_app import AuthClient, client, fake_precheck  # noqa: F401 (fixture)

import app as m
from services import ai_provider
from services.media_probe import ReelPolicy, technical_check
from services.video_precheck import (
    CATEGORY_AUDIO, CATEGORY_CAPTION, CATEGORY_COPYRIGHT, CATEGORY_TECHNICAL, CATEGORY_VISUAL,
    FINAL_BLOCKED, FINAL_NEEDS_REVIEW, FINAL_PASS, run_precheck,
)


@pytest.fixture(scope="session")
def videos(tmp_path_factory):
    """A few small real videos, built once with ffmpeg."""
    folder = tmp_path_factory.mktemp("reels")
    def build(name, *args):
        target = folder / name
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args, str(target)],
                       capture_output=True, timeout=120, check=True)
        return target
    valid = build("valid.mp4", "-f", "lavfi", "-i", "testsrc=size=480x854:rate=25:duration=2",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p")
    black = build("black.mp4", "-f", "lavfi", "-i", "color=c=black:size=480x854:rate=25:duration=2",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p")
    with_audio = build("audio.mp4", "-f", "lavfi", "-i", "testsrc=size=480x854:rate=25:duration=2",
                       "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    corrupt = folder / "corrupt.mp4"
    # the upload only looks at the signature, ffprobe will not find the movie box
    corrupt.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 200)
    return {"valid": valid, "black": black, "audio": with_audio, "corrupt": corrupt}


def make_workspace(client, name):
    pipeline = client.post('/api/pipelines', json={'name': name}).json()['id']
    account = client.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Facebook'}).json()['id']
    return pipeline, account


def upload(client, pipeline, path, name="clip.mp4"):
    with open(path, "rb") as handle:
        return client.post('/api/video-uploads', params={'pipeline_id': pipeline},
                           files={'file': (name, handle, 'video/mp4')})


# --- the technical check ----------------------------------------------------

def test_valid_video_passes_the_technical_check(videos):
    report = technical_check(videos["valid"], ReelPolicy(min_bitrate=0))
    assert report["status"] == "pass", report["reasons"]
    assert report["info"]["video_codec"] == "h264"
    assert report["info"]["width"] == 480
    assert report["info"]["duration"] > 1


def test_corrupt_video_is_blocked(videos):
    report = technical_check(videos["corrupt"], ReelPolicy())
    assert report["status"] == "blocked"
    assert report["errors"]


def test_wrong_codec_is_reported(videos, tmp_path):
    other = tmp_path / "legacy.avi"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=480x854:rate=25:duration=1",
                    "-c:v", "mpeg4", str(other)], capture_output=True, timeout=120)
    if not other.is_file():
        pytest.skip("this ffmpeg build cannot write mpeg4")
    report = technical_check(other, ReelPolicy(min_bitrate=0))
    assert report["status"] in ("needs_review", "blocked")
    assert any("codec" in reason.lower() or "định dạng" in reason.lower()
               for reason in report["reasons"])


def test_black_video_is_blocked(videos):
    report = run_precheck(videos["black"], "", [], ai_provider.ProviderConfig(), ReelPolicy(min_bitrate=0))
    assert report["categories"][CATEGORY_TECHNICAL]["status"] == "blocked"
    assert report["status"] == FINAL_BLOCKED


def test_audio_category_asr_disabled_returns_pass(videos):
    report = run_precheck(videos["audio"], "", [], ai_provider.ProviderConfig(), ReelPolicy(min_bitrate=0))
    assert report["categories"][CATEGORY_AUDIO]["status"] == "pass"
    assert "ASR disabled" in report["categories"][CATEGORY_AUDIO]["checked_scope"]
    # overall status depends on other categories
    assert report["status"] in (FINAL_PASS, FINAL_NEEDS_REVIEW)


def test_vision_timeout_never_becomes_a_pass(videos, monkeypatch):
    monkeypatch.setenv("AI_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AI_KEY", "secret")
    monkeypatch.setenv("AI_VISION_MODEL", "some/vision-model")

    def explode(*args, **kwargs):
        raise ai_provider.ProviderError("timeout")

    monkeypatch.setattr(ai_provider, "vision_json", explode)
    report = run_precheck(videos["valid"], "", [], ai_provider.load_config(), ReelPolicy())
    # A vision error is CHECK_FAILED, never a content violation or a pass.
    assert report["categories"][CATEGORY_VISUAL]["status"] == "check_failed"
    assert report["status"] == FINAL_NEEDS_REVIEW


def test_clean_vision_and_caption_reach_a_pass(videos, monkeypatch):
    monkeypatch.setenv("AWS_REKOGNITION_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")

    # Mock AWS Rekognition client
    from services.aws_rekognition import RekognitionClient, load_config
    def mock_detect_moderation_labels(self, image_bytes, min_confidence=None):
        return []
    def mock_detect_labels(self, image_bytes, min_confidence=None, max_labels=None):
        return []
    monkeypatch.setattr(RekognitionClient, "detect_moderation_labels", mock_detect_moderation_labels)
    monkeypatch.setattr(RekognitionClient, "detect_labels", mock_detect_labels)

    monkeypatch.setattr(ai_provider, "chat_json",
                        lambda *a, **k: {"status": "APPROVED", "reason": "caption sạch"})

    report = run_precheck(videos["valid"], "Cảm ơn mọi người đã ủng hộ!",
                          [], ai_provider.load_config(), ReelPolicy(min_bitrate=0))
    assert report["categories"][CATEGORY_VISUAL]["status"] == "pass"
    assert report["categories"][CATEGORY_CAPTION]["status"] == "pass"
    # Copyright is always UNVERIFIED unless an external source is provided,
    # so the overall result stays at NEEDS_REVIEW even with clean content.
    assert report["categories"][CATEGORY_COPYRIGHT]["status"] == "needs_review"
    assert report["status"] == FINAL_NEEDS_REVIEW


# --- the caption category ---------------------------------------------------

def test_risky_caption_is_flagged(videos):
    report = run_precheck(videos["valid"], "Cam kết hoàn tiền 100% hiệu quả, không rủi ro",
                          [], ai_provider.ProviderConfig(), ReelPolicy(min_bitrate=0))
    caption = report["categories"][CATEGORY_CAPTION]
    assert caption["status"] in ("blocked", "needs_review")
    assert caption["reasons"]


def test_clean_caption_passes(videos):
    report = run_precheck(videos["valid"], "Cảm ơn mọi người đã ủng hộ sản phẩm mới nhé!",
                          [], ai_provider.ProviderConfig(), ReelPolicy(min_bitrate=0))
    assert report["categories"][CATEGORY_CAPTION]["status"] == "pass"


# --- the copyright category -------------------------------------------------

def test_duplicate_video_is_flagged(videos):
    digest = hashlib.sha256(videos["valid"].read_bytes()).hexdigest()
    known = [{"sha256": digest, "signature": "", "path": "other.mp4", "state": "known"}]
    report = run_precheck(videos["valid"], "", known, ai_provider.ProviderConfig(), ReelPolicy(min_bitrate=0))
    copyright = report["categories"][CATEGORY_COPYRIGHT]
    assert copyright["status"] == "needs_review"
    assert any("Trùng" in reason for reason in copyright["reasons"])


def test_no_duplicate_is_reported_as_unverified(videos):
    # Copyright is always UNVERIFIED unless an external verification source
    # is provided, so the category stays at needs_review even when no
    # internal duplicate is found.
    known = [{"sha256": "different-hash", "signature": "abc", "path": "other.mp4"}]
    report = run_precheck(videos["valid"], "", known, ai_provider.ProviderConfig(), ReelPolicy(min_bitrate=0))
    copyright = report["categories"][CATEGORY_COPYRIGHT]
    assert copyright["status"] == "needs_review"
    assert any("quyền sử dụng" in reason for reason in copyright["suggestions"])
    assert "UNVERIFIED" in str(copyright.get("details", {}))


# --- the API and the publish guard ------------------------------------------

def test_precheck_api_creates_and_reports(client, videos, monkeypatch):
    pipeline, account = make_workspace(client, '@pre1')
    media = upload(client, pipeline, videos["valid"]).json()['path']
    created = client.post(f'/api/pipelines/{pipeline}/reels/precheck', json={
        'account_id': account, 'media_path': media, 'caption': 'Xin chào'})
    assert created.status_code == 202, created.text
    check_id = created.json()['id']
    fetched = client.get(f'/api/pipelines/{pipeline}/reels/precheck/{check_id}')
    assert fetched.status_code == 200
    assert fetched.json()['precheck']['id'] == check_id
    history = client.get(f'/api/pipelines/{pipeline}/reels/precheck/history').json()
    assert len(history['prechecks']) == 1


def test_reel_is_refused_without_a_precheck(client, videos):
    pipeline, account = make_workspace(client, '@pre2')
    media = upload(client, pipeline, videos["valid"]).json()['path']
    response = client.post('/api/manual-tasks', json={
        'pipeline_id': pipeline, 'account_id': account, 'task_type': 'reel', 'body': 'Mô tả',
        'media': [media], 'auto_publish': True, 'scheduled_at': m.iso(m.now() + m.timedelta(hours=1))})
    assert response.status_code == 409
    assert "kiểm tra" in response.json()['detail'].lower()


def test_blocked_precheck_is_never_published(client, videos):
    pipeline, account = make_workspace(client, '@pre3')
    media = upload(client, pipeline, videos["corrupt"], name="broken.mp4").json()['path']
    check = fake_precheck(client, pipeline, account, media, "Mô tả", status=FINAL_BLOCKED)
    response = client.post('/api/manual-tasks', json={
        'pipeline_id': pipeline, 'account_id': account, 'task_type': 'reel', 'body': 'Mô tả',
        'media': [media], 'auto_publish': True, 'scheduled_at': m.iso(m.now() + m.timedelta(hours=1)),
        'precheck_id': check})
    assert response.status_code == 409
    assert "chặn" in response.json()['detail'].lower()


def test_needs_review_does_not_become_a_pass_by_itself(client, videos):
    pipeline, account = make_workspace(client, '@pre4')
    media = upload(client, pipeline, videos["valid"], name="review.mp4").json()['path']
    check = fake_precheck(client, pipeline, account, media, "Mô tả", status=FINAL_NEEDS_REVIEW)
    response = client.post('/api/manual-tasks', json={
        'pipeline_id': pipeline, 'account_id': account, 'task_type': 'reel', 'body': "Mô tả",
        'media': [media], 'auto_publish': True, 'scheduled_at': m.iso(m.now() + m.timedelta(hours=1)),
        'precheck_id': check})
    assert response.status_code == 409
    assert "duyệt" in response.json()['detail'].lower()


def test_a_changed_video_invalidates_the_precheck(client, videos, tmp_path):
    pipeline, account = make_workspace(client, '@pre5')
    media = upload(client, pipeline, videos["valid"], name="change.mp4").json()['path']
    check = fake_precheck(client, pipeline, account, media, "Mô tả", status=FINAL_PASS)
    # the file is replaced after the check
    target = m.UPLOADS / str(pipeline) / Path(media).name
    target.write_bytes(b"a completely different video file" * 10)
    response = client.post('/api/manual-tasks', json={
        'pipeline_id': pipeline, 'account_id': account, 'task_type': 'reel', 'body': "Mô tả",
        'media': [media], 'auto_publish': True, 'scheduled_at': m.iso(m.now() + m.timedelta(hours=1)),
        'precheck_id': check})
    assert response.status_code == 409
    assert "thay đổi" in response.json()['detail'].lower()


def test_a_changed_caption_invalidates_the_precheck(client, videos):
    pipeline, account = make_workspace(client, '@pre6')
    media = upload(client, pipeline, videos["valid"], name="caption.mp4").json()['path']
    check = fake_precheck(client, pipeline, account, media, "Caption cũ", status=FINAL_PASS)
    response = client.post('/api/manual-tasks', json={
        'pipeline_id': pipeline, 'account_id': account, 'task_type': 'reel', 'body': "Caption mới",
        'media': [media], 'auto_publish': True, 'scheduled_at': m.iso(m.now() + m.timedelta(hours=1)),
        'precheck_id': check})
    assert response.status_code == 409
    assert "caption" in response.json()['detail'].lower()


def test_an_approved_precheck_can_be_published(client, videos):
    pipeline, account = make_workspace(client, '@pre7')
    media = upload(client, pipeline, videos["valid"], name="approved.mp4").json()['path']
    check = fake_precheck(client, pipeline, account, media, "Mô tả", status=FINAL_NEEDS_REVIEW)
    approved = client.post(f'/api/pipelines/{pipeline}/reels/precheck/{check}/approve',
                           json={'note': 'đã xem xét'})
    assert approved.status_code == 200, approved.text
    assert approved.json()['precheck']['approved'] is True
    created = client.post('/api/manual-tasks', json={
        'pipeline_id': pipeline, 'account_id': account, 'task_type': 'reel', 'body': "Mô tả",
        'media': [media], 'auto_publish': True, 'scheduled_at': m.iso(m.now() + m.timedelta(hours=1)),
        'precheck_id': check})
    assert created.status_code == 201, created.text


def test_collaborator_without_permission_is_refused(client, videos):
    pipeline, account = make_workspace(client, '@pre8')
    media = upload(client, pipeline, videos["valid"], name="ctv.mp4").json()['path']
    key = client.post(f'/api/admin/pipelines/{pipeline}/key').json()['key']
    client.post('/api/admin/users', json={'username': 'ctv_pre', 'password': 'ctv-precheck-password-26',
                                          'pipeline_id': pipeline, 'permissions': ['facebook.view']})
    ctv = AuthClient(m.app)
    login = ctv.post('/api/auth/login', json={'username': 'ctv_pre', 'password': 'ctv-precheck-password-26',
                                               'pipeline_key': key})
    assert login.status_code == 200
    ctv.csrf = login.json()['csrf_token']
    response = ctv.post(f'/api/pipelines/{pipeline}/reels/precheck', json={
        'account_id': account, 'media_path': Path(media).name, 'caption': 'x'})
    assert response.status_code == 403
    ctv.close()


def test_two_workspaces_cannot_reach_each_others_media(client, videos):
    mine, my_account = make_workspace(client, '@pre9')
    theirs, their_account = make_workspace(client, '@pre10')
    their_media = upload(client, theirs, videos["valid"], name="theirs.mp4").json()['path']
    response = client.post(f'/api/pipelines/{mine}/reels/precheck', json={
        'account_id': my_account, 'media_path': Path(their_media).name, 'caption': 'x'})
    assert response.status_code in (404, 422)
    history = client.get(f'/api/pipelines/{theirs}/reels/precheck/history').json()
    assert history["prechecks"] == []
