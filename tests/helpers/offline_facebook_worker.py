"""Test-only subprocess adapter. All browser requests are fulfilled locally."""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from playwright.async_api import async_playwright
import fb_browser
from services.post_verifier import wait_post_outcome


async def run(payload):
    html=(Path(__file__).parents[1]/'fixtures/facebook/composer.html').read_text()
    from urllib.parse import urlparse
    group=urlparse(payload['group_url']).path.rstrip('/').split('/')[-1]
    html=html.replace('/groups/allowed/','/groups/'+group+'/')
    outcome=os.environ['OFFLINE_FACEBOOK_OUTCOME']
    if outcome=='pending_approval':
        html=html.replace('/posts/new-post/','/pending_posts/new-post/').replace('Your post is now published','Đã gửi để duyệt')
    if outcome=='submitted_unverified':
        html=html.replace("document.querySelector('[role=feed]').append(article);",'').replace("document.querySelector('#notice').textContent='Your post is now published';",'')
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,args=['--no-sandbox'])
        context=await browser.new_context()
        if outcome!='expired_session':
            await context.add_cookies([{'name':'c_user','value':'123','domain':'.facebook.com','path':'/'}])
        intercepted=[]
        async def local_only(route):
            intercepted.append(route.request.url)
            await route.fulfill(body=html,content_type='text/html')
        await context.route('**/*',local_only)
        lock=MagicMock();lock.validate_caption_with_lock.return_value=(True,'')
        async def short_outcome(page,body='',**kwargs):
            return await wait_post_outcome(page,body,timeout=600,**kwargs)
        with patch.object(fb_browser,'context_for',AsyncMock(return_value=context)), \
             patch.object(fb_browser,'close_context',AsyncMock()), \
             patch.object(fb_browser,'dismiss_common_popups',AsyncMock()), \
             patch.object(fb_browser,'DestinationLockService',return_value=lock), \
             patch.object(fb_browser,'wait_post_outcome',short_outcome):
            result=await fb_browser.attempt_post(payload)
        clicks=sum([await page.evaluate('window.submissions||0') for page in context.pages])
        proof={'clicks':clicks,'intercepted_requests':len(intercepted),'result':result,
               'ids':{k:payload[k] for k in ('pipeline_id','account_id','group_id','job_id')}}
        (Path(os.environ['DATA_DIR'])/'dispatch-proof.json').write_text(json.dumps(proof))
        await browser.close()
        return result


if __name__=='__main__':
    payload=json.loads(Path(sys.argv[1]).read_text())
    Path(sys.argv[2]).write_text(json.dumps(asyncio.run(run(payload))))
