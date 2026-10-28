"""Offline Chromium integration: no Facebook network or real session is used."""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import async_playwright
from services.composer_detector import find_composer, find_editor, find_publish_button
from services.facebook_selectors import SelectorRegistry, SelectorResolutionError, active_composer_dialog
from services.media_upload import upload_media, attachment_ready
from services.post_verifier import newest_permalink, wait_post_outcome, group_post_links
from services.posting_mode import anonymous_mode_verified, identity_shown, require_nickname_identity
from services.posting_preflight import same_group, session_problem

FIXTURE = Path(__file__).parent / 'fixtures/facebook/composer.html'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==')


class PublishingDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.picture = Path(self.temp.name) / 'image.png'
        from PIL import Image
        Image.new('RGB', (320,240), 'steelblue').save(self.picture)
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True, args=['--no-sandbox'])
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.fulfill(body=FIXTURE.read_text(), content_type='text/html'))
        self.page = await self.context.new_page()
        await self.page.goto('https://www.facebook.com/groups/allowed/')
        await (await find_composer(self.page)).click()
        self.dialog = await active_composer_dialog(self.page)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temp.cleanup()

    async def test_account_display_name_uses_profile_metadata_not_generic_control(self):
        import fb_browser
        await self.page.set_content('<button aria-label="Account, your profile">Account</button><h1>Quản lý trang cá nhân</h1><meta property="og:title" content="Fixture Account | Facebook">')
        with patch.object(self.page,'goto',AsyncMock()), patch.object(self.page,'wait_for_timeout',AsyncMock()):
            name,_=await fb_browser.facebook_identity(self.page,'123')
        self.assertEqual(name,'Fixture Account')

    async def test_semantic_profile_and_reel_publish_ambiguity(self):
        await self.page.set_content('<button>Create post</button><button>Publish reel</button><button>Publish reel</button>')
        registry = SelectorRegistry(profile='desktop_semantic', locales=('en',))
        self.assertEqual(await (await registry.resolve(self.page, 'composer')).inner_text(), 'Create post')
        with self.assertRaises(SelectorResolutionError):
            await registry.resolve(self.page, 'reel_publish')

    async def test_hidden_input_preview_caption_and_scoped_submit(self):
        await (await find_editor(self.page, self.dialog)).fill('Nội dung đầy đủ')
        self.assertTrue(await upload_media(self.page, [self.picture], dialog=self.dialog, timeout=1000))
        button = await find_publish_button(self.page, self.dialog)
        self.assertEqual(await button.get_attribute('id'), 'submit')
        previous = await group_post_links(self.page)
        task = asyncio.create_task(wait_post_outcome(self.page, timeout=1000))
        await button.click()
        outcome = await task
        self.assertEqual(outcome['state'], 'published')
        self.assertIn('permalink', outcome)
        self.assertEqual(await self.page.evaluate('submissions'), 1)
        link = await newest_permalink(self.page, 'Nội dung đầy đủ', expected_group=self.page.url, exclude=previous, expected_image=self.picture, submitted_at=__import__('time').time(), expected_identity='Anonymous member')
        self.assertIn('/groups/allowed/posts/new-post/', link)
        self.assertEqual(await newest_permalink(self.page, 'khác', expected_group=self.page.url), '')
        self.assertEqual(await newest_permalink(self.page, 'Nội dung đầy đủ', expected_group='https://www.facebook.com/groups/other/'), '')
        self.assertEqual(await newest_permalink(self.page, 'Nội dung đầy đủ', exclude=[link]), '')

    async def test_ambiguous_publish_is_rejected(self):
        await self.dialog.evaluate("el => el.append(el.querySelector('#submit').cloneNode(true))")
        with self.assertRaises(SelectorResolutionError):
            await find_publish_button(self.page, self.dialog, timeout=100)
        self.assertEqual(await self.page.evaluate('submissions'), 0)

    async def test_no_page_wide_editor_or_publish_fallback(self):
        await self.dialog.locator('#submit').evaluate('(el) => el.remove()')
        self.assertIsNone(await find_publish_button(self.page, self.dialog, timeout=100))
        self.assertIsNone(await find_editor(self.page, None, timeout=100))

    async def test_upload_requires_preview_and_exactly_one_image(self):
        await self.page.evaluate("document.querySelector('input').onchange = null")
        self.assertFalse(await upload_media(self.page, [self.picture], dialog=self.dialog, timeout=200))
        self.assertFalse(await upload_media(self.page, [self.picture, self.picture], dialog=self.dialog, timeout=100))
        self.assertFalse(await attachment_ready(self.dialog))

    async def test_progress_and_second_attachment_block_readiness(self):
        self.assertTrue(await upload_media(self.page, [self.picture], dialog=self.dialog, timeout=1000))
        await self.dialog.evaluate("el => el.insertAdjacentHTML('beforeend', '<div role=progressbar>Uploading</div>')")
        self.assertFalse(await attachment_ready(self.dialog))
        await self.dialog.locator('[role=progressbar]').evaluate('(el) => el.remove()')
        await self.dialog.evaluate("el => el.querySelector('#attachments').append(el.querySelector('img').cloneNode())")
        self.assertFalse(await attachment_ready(self.dialog))

    async def test_anonymous_requires_composer_identity_not_caption(self):
        self.assertTrue(await anonymous_mode_verified(self.page))
        await self.dialog.locator('#author').evaluate("el => el.textContent='Real name'")
        await self.dialog.locator('[role=switch]').evaluate("el => el.setAttribute('aria-checked','false')")
        await (await find_editor(self.page, self.dialog)).fill('Anonymous member')
        self.assertFalse(await anonymous_mode_verified(self.page))
        self.assertFalse(await identity_shown(self.page, 'Anonymous member'))

    async def test_nickname_reuses_requested_identity_and_refuses_random(self):
        await self.dialog.locator('#author').evaluate("el => el.textContent='Chosen42'")
        with patch('services.posting_mode.get_saved_facebook_nickname_async', AsyncMock(return_value='Chosen42')):
            selected, _, _ = await require_nickname_identity(self.page, 1, 2, 3, 'Different99')
            self.assertEqual(selected, 'Chosen42')
        with patch('services.posting_mode.get_saved_facebook_nickname_async', AsyncMock(return_value=None)):
            selected, _, _ = await require_nickname_identity(self.page, 1, 2, 3, 'Unavailable55')
            self.assertEqual(selected, '')
        self.assertEqual(await self.page.evaluate('submissions'), 0)

    async def test_pending_notice_later_label_and_missing_permalink_contract(self):
        task = asyncio.create_task(wait_post_outcome(self.page, timeout=500))
        await self.page.evaluate("setTimeout(() => document.querySelector('#notice').textContent='Đã gửi để duyệt', 50)")
        outcome = await task
        self.assertEqual(outcome['state'], 'pending_approval')
        self.assertEqual(outcome['permalink'], '')

    async def test_closed_composer_or_generic_thanks_does_not_prove_posted(self):
        await self.dialog.evaluate('(el) => el.hidden = true')
        await self.page.locator('#notice').evaluate("el => el.textContent='Thank you for posting'")
        outcome = await wait_post_outcome(self.page, timeout=100)
        self.assertEqual(outcome['state'], 'ambiguous')

    async def test_session_login_overlay_and_missing_cookie(self):
        self.assertIn('đăng nhập', await session_problem(self.page, self.context))
        await self.context.add_cookies([{'name':'c_user','value':'123','domain':'.facebook.com','path':'/'}])
        self.assertEqual(await session_problem(self.page, self.context), '')
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<input name=email>')")
        self.assertIn('đăng nhập', await session_problem(self.page, self.context))

    def test_version_locale_and_destination_guards(self):
        self.assertEqual(SelectorRegistry(version='v1', locales=('vi',)).version, 'v1')
        with self.assertRaises(ValueError):
            SelectorRegistry(version='../private')
        self.assertTrue(same_group('https://www.facebook.com/groups/allowed/', self.page.url))
        self.assertFalse(same_group('https://evil.test/groups/allowed/', self.page.url))
        self.assertFalse(same_group('https://www.facebook.com/groups/other/', self.page.url))

    async def run_offline_runner(self, *, dry_run=False, timeout_after_click=False, pending=False, no_notice=False):
        import fb_browser
        from playwright.async_api import Locator
        await self.context.add_cookies([{'name':'c_user','value':'123','domain':'.facebook.com','path':'/'}])
        original_click = Locator.click
        async def click_once(locator, **kwargs):
            result = await original_click(locator, **kwargs)
            if timeout_after_click and await locator.get_attribute('id') == 'submit':
                raise TimeoutError('dispatch completed but acknowledgment lost')
            return result
        async def short_outcome(page, body='', **kwargs):
            return await wait_post_outcome(page, body, timeout=500, **kwargs)
        original_html = FIXTURE.read_text()
        if pending:
            original_html = original_html.replace('/posts/new-post/', '/pending_posts/new-post/').replace('Your post is now published', 'Đã gửi để duyệt')
        if no_notice:
            original_html = original_html.replace("document.querySelector('#notice').textContent='Your post is now published';", '')
            original_html = original_html.replace("document.querySelector('[role=feed]').append(article);", '')
        await self.context.unroute('**/*')
        await self.context.route('**/*', lambda route: route.fulfill(body=original_html, content_type='text/html'))
        lock = MagicMock()
        lock.validate_caption_with_lock.return_value = (True, '')
        with patch.object(fb_browser, 'context_for', AsyncMock(return_value=self.context)), \
             patch.object(fb_browser, 'close_context', AsyncMock()), \
             patch.object(fb_browser, 'dismiss_common_popups', AsyncMock()), \
             patch.object(fb_browser, 'DATA', Path(self.temp.name)), \
             patch.object(fb_browser, 'EVIDENCE_DIR', Path(self.temp.name)), \
             patch.object(fb_browser, 'DestinationLockService', return_value=lock), \
             patch.object(fb_browser, 'wait_post_outcome', short_outcome), \
             patch.object(Locator, 'click', click_once):
            return await fb_browser.attempt_post({
                'pipeline_id': 1, 'account_id': 2, 'group_id': 3, 'job_id': 4,
                'group_url': 'https://www.facebook.com/groups/allowed/',
                'body': 'Caption verified end to end', 'media': [str(self.picture)],
                'posting_mode': 'anonymous', 'dry_run': dry_run,
            })

    async def test_runner_dry_run_never_submits(self):
        result = await self.run_offline_runner(dry_run=True)
        self.assertEqual(result['verification'], 'draft_verified')
        self.assertEqual(await self.page.evaluate('submissions'), 0)
        self.assertTrue((Path(self.temp.name) / result['evidence']).is_file())

    async def test_runner_posts_only_with_new_content_permalink(self):
        result = await self.run_offline_runner()
        self.assertEqual(result['state'], 'posted', result)
        self.assertIn('/posts/new-post/', result['permalink'])
        self.assertEqual(await self.page.evaluate('submissions'), 1)

    async def test_runner_pending_is_distinct_from_posted(self):
        result = await self.run_offline_runner(pending=True)
        self.assertEqual(result['state'], 'pending_approval', result)
        self.assertIn('/pending_posts/', result['permalink'])
        self.assertEqual(await self.page.evaluate('submissions'), 1)

    async def test_dispatch_timeout_does_not_retry_submit(self):
        result = await self.run_offline_runner(timeout_after_click=True)
        self.assertEqual(result['state'], 'submitted_unverified', result)
        self.assertEqual(await self.page.evaluate('submissions'), 1)

    async def test_runner_closed_composer_without_evidence_stays_unverified(self):
        result = await self.run_offline_runner(no_notice=True)
        self.assertEqual(result['state'], 'submitted_unverified', result)
        self.assertEqual(await self.page.evaluate('submissions'), 1)

    async def test_file_chooser_upload_also_requires_preview(self):
        await self.dialog.locator('input').evaluate('(el) => el.remove()')
        await self.dialog.evaluate("""el => {
          const button = document.createElement('button'); button.textContent='Photo/video';
          button.onclick = () => {
            const input = document.createElement('input'); input.type='file'; input.accept='image/*';
            input.hidden=true; el.append(input);
            input.onchange = () => { const img=document.createElement('img');
              img.src=URL.createObjectURL(input.files[0]); el.querySelector('#attachments').append(img); };
            input.click();
          }; el.append(button);
        }""")
        self.assertTrue(await upload_media(self.page, [self.picture], dialog=self.dialog, timeout=1000))

    async def test_vietnamese_profile_and_disabled_publish(self):
        await self.dialog.locator('#submit').evaluate("el => el.textContent='Đăng'")
        target = await SelectorRegistry(locales=('vi',)).resolve(self.dialog, 'publish', timeout=100)
        self.assertEqual(await target.get_attribute('id'), 'submit')
        await target.evaluate('(el) => el.disabled = true')
        self.assertIsNone(await find_publish_button(self.page, self.dialog, timeout=100))

    async def test_preexisting_notice_does_not_verify_new_attempt(self):
        from services.post_verifier import visible_notices
        await self.page.locator('#notice').evaluate("el => el.textContent='Đã gửi để duyệt'")
        before = await visible_notices(self.page)
        outcome = await wait_post_outcome(self.page, timeout=100, prior_notices=before)
        self.assertEqual(outcome['state'], 'ambiguous')

    async def test_security_or_action_block_stops_preflight(self):
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=alert>Action blocked</div>')")
        self.assertIn('hạn chế', await session_problem(self.page, self.context))
        await self.page.goto('https://www.facebook.com/checkpoint/test/')
        self.assertIn('xác minh bảo mật', await session_problem(self.page, self.context))

    async def test_runner_rejects_missing_tenant_context_before_browser(self):
        import fb_browser
        with patch.object(fb_browser, 'context_for', AsyncMock()) as launch:
            outcome = await fb_browser.attempt_post({'account_id': 1, 'job_id': 1})
            self.assertEqual(outcome['state'], 'needs_review')
            launch.assert_not_awaited()

    async def test_anonymous_dedicated_trigger_continues_notice_before_editor(self):
        from services.posting_mode import require_anonymous_mode
        await self.dialog.evaluate('(el) => el.hidden = true')
        await self.page.evaluate("""() => {
          const trigger=document.createElement('button'); trigger.id='anon-trigger'; trigger.textContent='Bài viết ẩn danh';
          trigger.onclick=()=>{
            const notice=document.createElement('div'); notice.setAttribute('role','dialog'); notice.id='anon-notice';
            notice.innerHTML='<h2>Bài viết ẩn danh</h2><button>Tạo bài viết ẩn danh</button>';
            notice.querySelector('button').onclick=()=>{notice.remove();document.querySelector('[role=dialog]').hidden=false;};
            document.body.append(notice);
          };document.body.append(trigger);
        }""")
        trigger=await find_composer(self.page,posting_mode='anonymous')
        self.assertEqual(await trigger.get_attribute('id'),'anon-trigger')
        await trigger.click()
        self.assertTrue(await require_anonymous_mode(self.page))
        self.assertEqual(await self.page.evaluate('submissions'),0)

    async def test_cookie_without_authenticated_controls_is_unknown(self):
        from services.facebook_session import inspect_session
        await self.context.add_cookies([{'name':'c_user','value':'123','domain':'.facebook.com','path':'/'}])
        await self.page.get_by_role('button',name='Tài khoản, trang cá nhân của bạn').evaluate('(el)=>el.remove()')
        observed=await inspect_session(self.page,self.context)
        self.assertEqual(observed['status'],'unknown')
        self.assertEqual(observed['uid'],'')

    async def test_unavailable_group_does_not_claim_membership_or_selector_failure(self):
        from services.group_access import inspect_group_access
        await self.context.add_cookies([{'name':'c_user','value':'123','domain':'.facebook.com','path':'/'}])
        await self.page.locator('[role=main]').evaluate("el=>el.textContent='Bạn hiện không xem được nội dung này'")
        observed=await inspect_group_access(self.page,self.context,self.page.url)
        self.assertEqual(observed['code'],'CONTENT_UNAVAILABLE')
        self.assertFalse(observed['accessible'])
        self.assertIsNone(observed['is_member'])


class TargetEvidenceTests(PublishingDOMTests):
    # Reuse only setup helpers, not the parent test suite (collected below).
    async def verify_target(self):
        import time
        return await newest_permalink(self.page, 'Target caption', expected_group=self.page.url,
            expected_image=self.picture, submitted_at=time.time(), expected_identity='Anonymous member')

    async def prepare_target(self):
        await (await find_editor(self.page,self.dialog)).fill('Target caption')
        await upload_media(self.page,[self.picture],dialog=self.dialog,timeout=1000)
        await (await find_publish_button(self.page,self.dialog)).click()
        self.assertTrue(await self.verify_target())

    async def test_avatar_only_cannot_verify(self):
        await self.prepare_target()
        await self.page.locator('article img').evaluate("el=>{el.alt='Profile picture';el.parentElement.href='/someone/';}")
        self.assertEqual(await self.verify_target(),'')

    async def test_large_avatar_in_author_heading_cannot_verify(self):
        await self.prepare_target()
        await self.page.locator('article').evaluate("a=>a.querySelector('h3').append(a.querySelector('a[href*=photo]'))")
        self.assertEqual(await self.verify_target(),'')

    async def test_wrong_photo_cannot_verify(self):
        from PIL import Image
        await self.prepare_target()
        Image.new('RGB',(320,240),'red').save(self.picture)
        self.assertEqual(await self.verify_target(),'')

    async def test_duplicate_caption_and_photo_is_ambiguous(self):
        await self.prepare_target()
        await self.page.locator('article').evaluate("a=>{let b=a.cloneNode(true);b.querySelector('a[href*=posts]').href='/groups/allowed/posts/another/';a.parentElement.append(b);}")
        self.assertEqual(await self.verify_target(),'')

    async def test_comment_photo_cannot_verify_outer_post(self):
        await self.prepare_target()
        await self.page.locator('article').evaluate("a=>{const nested=document.createElement('div');nested.setAttribute('role','article');nested.append(a.querySelector('a[href*=photo]'));a.append(nested);}")
        self.assertEqual(await self.verify_target(),'')

    async def test_old_timestamp_or_wrong_identity_cannot_verify(self):
        await self.prepare_target()
        await self.page.locator('article h3').evaluate("el=>el.textContent='Someone else'")
        self.assertEqual(await self.verify_target(),'')
        await self.page.locator('article h3').evaluate("el=>el.textContent='Anonymous member'")
        await self.page.locator('article a[href*=posts]').evaluate("el=>el.innerHTML='<time datetime=2020-01-01T00:00:00Z>Just now</time>'")
        self.assertEqual(await self.verify_target(),'')

    async def test_pending_article_remains_pending_link(self):
        await self.prepare_target()
        await self.page.locator('article a[href*=posts]').evaluate("el=>el.href='/groups/allowed/pending_posts/draft/'")
        self.assertIn('/pending_posts/',await self.verify_target())


# Avoid executing the inherited integration tests a second time.
for _name in list(PublishingDOMTests.__dict__):
    if _name.startswith('test_'):
        setattr(TargetEvidenceTests, _name, None)
