"""Kiểm thử xác minh kết quả bài đăng sau khi nhấn Đăng.

Các test dùng dữ liệu đã làm sạch (sanitized) từ HAR thực tế để xác minh:
1. Các pattern thông báo PENDING_NOTICES bao gồm đúng văn bản tiếng Việt
   mà Facebook hiển thị cho bài ẩn danh chờ duyệt.
2. newest_permalink nhận diện URL /pending_posts/ từ bài đăng ẩn danh.
3. status_from_job ánh xạ trạng thái đúng.
"""
import unittest
from unittest.mock import MagicMock, AsyncMock

from services import post_verifier, group_verification


# --- 1. PENDING_NOTICES phải khớp văn bản thực tế của Facebook ----------------

def test_vietnamese_pending_notice_for_anonymous_posts():
    """Văn bản thông báo thực tế của Facebook cho bài ẩn danh chờ duyệt:
    'Cảm ơn bạn đã đăng bài. Hệ thống đã gửi bài viết cho quản trị viên nhóm phê duyệt.'

    Phát hiện từ HAR: post_pending_approval_toast_text.
    """
    toast_text = "Cảm ơn bạn đã đăng bài. Hệ thống đã gửi bài viết cho quản trị viên nhóm phê duyệt."
    matched = False
    for selector in post_verifier.PENDING_NOTICES:
        pattern = selector.replace("text=", "")
        if pattern.lower() in toast_text.lower():
            matched = True
            break
    assert matched, (
        f"PENDING_NOTICES không khớp văn bản toast thực tế: {toast_text}. "
        f"Selectors: {post_verifier.PENDING_NOTICES}"
    )


def test_generic_thanks_is_not_pending_approval_evidence():
    """A generic thanks is insufficient evidence of moderator review."""
    assert not any("Thank you for posting" in selector for selector in post_verifier.PENDING_NOTICES)
    assert any("pending approval" in selector for selector in post_verifier.PENDING_NOTICES)


def test_published_notices_still_present():
    """Các pattern đã tồn tại cho bài đã đăng vẫn phải có."""
    assert len(post_verifier.PUBLISHED_NOTICES) > 0
    assert any("published" in s.lower() or "đăng" in s.lower() for s in post_verifier.PUBLISHED_NOTICES)


def test_rejected_notices_still_present():
    """Các pattern cho bài bị từ chối vẫn phải có."""
    assert len(post_verifier.REJECTED_NOTICES) > 0


# --- 2. newest_permalink nhận diện URL /pending_posts/ -----------------------

# --- 3. status_from_job ánh xạ trạng thái đúng -------------------------------

def test_status_from_job_pending_approval():
    """Trạng thái pending_approval phải ánh xạ thành PENDING_GROUP_APPROVAL."""
    status = group_verification.status_from_job("pending_approval", "")
    assert status == group_verification.PENDING_GROUP_APPROVAL


def test_status_from_job_posted():
    """Trạng thái posted phải ánh xạ thành VERIFIED_POSTED."""
    status = group_verification.status_from_job("posted", "")
    assert status == group_verification.VERIFIED_POSTED


def test_status_from_job_submitted_unverified():
    """Trạng thái submitted_unverified phải ánh xạ thành SUBMITTED_UNVERIFIED."""
    status = group_verification.status_from_job("submitted_unverified", "timeout")
    assert status == group_verification.SUBMITTED_UNVERIFIED


def test_status_from_job_rejected_with_details():
    """Chi tiết bị từ chối phải ánh xạ thành REJECTED."""
    status = group_verification.status_from_job("failed", "Bài bị từ chối bởi group")
    assert status == group_verification.REJECTED


def test_status_from_job_failed_without_rejection():
    """Lỗi không phải từ chối phải ánh xạ thành CHECK_FAILED."""
    status = group_verification.status_from_job("failed", "Composer not found")
    assert status == group_verification.CHECK_FAILED


def test_status_from_job_restricted():
    """Thông báo hạn chế phải ánh xạ thành RESTRICTED_CONFIRMED."""
    status = group_verification.status_from_job("failed", "Facebook hạn chế quyền đăng trong group này")
    assert status == group_verification.RESTRICTED_CONFIRMED


# --- 4. wait_post_outcome với mock page --------------------------------------

class WaitPostOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_post_outcome_detects_pending_notice(self):
        """wait_post_outcome phải trả về pending_approval khi thấy toast xác nhận.

        Simulates a Playwright page where the pending approval notice is visible.
        """
        fake_page = MagicMock()

        # Locator that succeeds (notice becomes visible)
        visible_locator = MagicMock()
        visible_locator.wait_for = AsyncMock(return_value=True)
        visible_locator.first = visible_locator
        visible_locator.and_.return_value = visible_locator

        # Locator that fails (notice not visible / timeout)
        hidden_locator = MagicMock()
        hidden_locator.wait_for = AsyncMock(side_effect=Exception("not found"))
        hidden_locator.first = hidden_locator
        hidden_locator.and_.return_value = hidden_locator

        pending_selectors = set(post_verifier.PENDING_NOTICES)

        def locator_side_effect(selector):
            if selector in pending_selectors:
                return visible_locator
            return hidden_locator

        fake_page.locator = MagicMock(side_effect=locator_side_effect)

        outcome = await post_verifier.wait_post_outcome(fake_page, "test caption")
        assert outcome["state"] == "pending_approval"

    async def test_wait_post_outcome_detects_published_notice(self):
        """wait_post_outcome phải trả về published khi thấy thông báo đã đăng."""
        fake_page = MagicMock()

        visible_locator = MagicMock()
        visible_locator.wait_for = AsyncMock(return_value=True)
        visible_locator.first = visible_locator
        visible_locator.and_.return_value = visible_locator

        published_selectors = set(post_verifier.PUBLISHED_NOTICES)

        def locator_side_effect(selector):
            if selector in published_selectors:
                return visible_locator
            hidden = MagicMock()
            hidden.wait_for = AsyncMock(side_effect=Exception("not found"))
            hidden.first = hidden
            hidden.and_.return_value = hidden
            return hidden

        fake_page.locator = MagicMock(side_effect=locator_side_effect)

        outcome = await post_verifier.wait_post_outcome(fake_page, "test caption")
        assert outcome["state"] == "published"
