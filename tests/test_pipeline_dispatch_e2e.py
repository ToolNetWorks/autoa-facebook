"""Schedule API -> promotion -> real worker subprocess -> local Facebook -> UI.

Only the Facebook boundary is replaced with a local DOM. Test database, accounts,
media analysis metadata and browser cookies are synthetic and never production.
"""
import json
import subprocess
from pathlib import Path
from PIL import Image
from playwright.sync_api import expect
from test_app import client
from test_first_post_flow import make_pipeline, make_account, make_group, make_picture, enable_auto_post
from test_group_worker_contract import idle_background_workers
from test_account_session_ui import staged_bundle
from test_dashboard_blockers_ui import route_bundle, viewports
from ui_live import live_server, web_browser, login
from services import group_membership
import app as m


def test_schedule_promote_dispatch_history_dashboard(client,monkeypatch,live_server,web_browser,staged_bundle):
    pipeline=make_pipeline(client,'@dispatch-ui')
    account=make_account(client,pipeline,'Fixture account')
    group=make_group(client,pipeline,account,'Fixture group','https://www.facebook.com/groups/dispatch-ui/')
    media=make_picture(client,pipeline,'dispatch.png')
    with m.db() as conn:
        group_membership.record(conn,pipeline,account,group,group_membership.JOINED)
        key=conn.execute('SELECT storage_key FROM media_assets WHERE id=?',(media,)).fetchone()[0]
    from services.media_store import safe_join
    Image.new('RGB',(320,240),'steelblue').save(safe_join(m.DATA,key))
    enable_auto_post(client,pipeline,[group])
    scheduled=client.post(f'/api/pipelines/{pipeline}/auto-post/schedule')
    assert scheduled.status_code==201,scheduled.text
    assert len(scheduled.json()['created'])==1,scheduled.json()
    assert client.post(f'/api/pipelines/{pipeline}/auto-post/schedule').json()['created']==[]
    history=client.get(f'/api/pipelines/{pipeline}/auto-post/history').json()
    auto=history['jobs'][0]
    assert auto['post_id'] is None and auto['status']=='pending'
    assert client.post(f"/api/pipelines/{pipeline}/auto-post/{auto['id']}/approve").status_code==200
    # Advance the schedule in the disposable fixture, never production.
    with m.db() as conn:
        conn.execute('UPDATE auto_post_jobs SET scheduled_for=? WHERE id=?',
                     (m.iso(m.now()-m.timedelta(minutes=1)),auto['id']))
    monkeypatch.setattr(m.media_cloud,'enabled',lambda:False)
    m.publish_auto_post_jobs();m.publish_auto_post_jobs()
    with m.db() as conn:
        updated=dict(conn.execute('SELECT * FROM auto_post_jobs WHERE id=?',(auto['id'],)).fetchone())
        assert updated['status']=='queued',updated['error']
        assert conn.execute('SELECT COUNT(*) FROM posts WHERE pipeline_id=?',(pipeline,)).fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM jobs WHERE pipeline_id=?',(pipeline,)).fetchone()[0]==1
    job=m.claim_next_job();assert job and job['post_id']==updated['post_id']
    actual_run=subprocess.run
    def offline(command,**kwargs):
        kwargs['env']={**kwargs['env'],'OFFLINE_FACEBOOK_OUTCOME':'posted'}
        helper=Path(__file__).parent/'helpers/offline_facebook_worker.py'
        return actual_run([command[0],str(helper),command[-2],command[-1]],**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(m.subprocess,'run',offline)
        m.execute_job(job)
    proof=json.loads((m.DATA/'dispatch-proof.json').read_text())
    assert proof['clicks']==1 and proof['result']['state']=='posted',proof
    assert m.claim_next_job() is None
    assert client.post(f"/api/jobs/{job['id']}/review",json={'state':'queued'}).status_code==409
    result=client.get(f'/api/pipelines/{pipeline}/auto-post/history').json()
    assert result['counts']['posted']==1 and result['jobs'][0]['post_id']==job['post_id']
    dashboard=client.get(f'/api/pipelines/{pipeline}/auto-post/dashboard').json()
    assert dashboard['totals']['published_today']==1
    with m.db() as conn:
        row=conn.execute('SELECT * FROM post_attribution WHERE content_type=? AND post_id=?',('group_post',job['id'])).fetchone()
        assert row['pipeline_id']==pipeline and row['published_by_account_id']==account
    page=web_browser.new_page(viewport={'width':1280,'height':1000});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    route_bundle(page,live_server,staged_bundle)
    try:
        login(page,live_server)
        rows=page.request.get(live_server+'/api/admin/pipelines').json()
        assert rows[0]['posted_count']==1
        page.get_by_role('button',name='Mở Workspace').click()
        page.locator('.nav-item[data-page=auto-posting]').click()
        page.get_by_role('tab',name='Danh sách bài đăng').click()
        expect(page.get_by_text('Đã đăng',exact=True).first).to_be_visible()
        expect(page.get_by_text(f"Auto Job #{auto['id']} · Post #{job['post_id']}",exact=True)).to_be_visible()
        expect(page.locator('.stat').filter(has=page.get_by_text('Bài đã xác nhận đăng',exact=True)).locator('strong')).to_have_text('1')
        viewports(page,'dispatch-history')
        assert not errors,errors
    finally:
        page.close()
