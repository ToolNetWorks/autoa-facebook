"""Existing timer -> real subprocess/local browser -> verifier -> DB/API/UI."""
import json
import subprocess
from pathlib import Path

from playwright.sync_api import expect
import pytest
from test_auto_cycles import client, world
from test_auto_cycles_runtime import promote_all
from test_account_session_ui import staged_bundle
from test_dashboard_blockers_ui import route_bundle, viewports
from ui_live import live_server,web_browser,login
from services.auto_posting_cycles import engine
import app as m


def offline_execute(monkeypatch,job,outcome):
    actual_run=subprocess.run
    helper=Path(__file__).parent/'helpers/offline_facebook_worker.py'
    def run(command,**kwargs):
        kwargs['env']={**kwargs['env'],'OFFLINE_FACEBOOK_OUTCOME':outcome,
                       'OFFLINE_FACEBOOK_UID':str(122+job['account_id'])}
        return actual_run([command[0],str(helper),command[-2],command[-1]],**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(m.subprocess,'run',run)
        m.execute_job(job)
    return json.loads((m.DATA/'dispatch-proof.json').read_text())


def test_cycle_timer_worker_verifier_history_dashboard(client,world,monkeypatch,live_server,web_browser,staged_bundle):
    w=world(accounts=3,groups=3)
    # Actual scheduler entry point, not a second scheduler or a direct posting call.
    m.prepare_auto_post_jobs();promote_all(w)
    outcomes=['posted','pending_approval','submitted_unverified'];jobs=[]
    for outcome in outcomes:
        job=m.claim_next_job();assert job
        proof=offline_execute(monkeypatch,job,outcome)
        assert proof['clicks']==1 and proof['result']['state']==outcome,proof
        jobs.append(job)
    assert m.claim_next_job() is None
    for job in jobs:
        assert client.post(f"/api/jobs/{job['id']}/review",json={'state':'queued'}).status_code==409
    report=client.get(f"/api/pipelines/{w['pid']}/auto-post/cycles/1").json()
    assert report['summary']['posted']==1 and report['summary']['pending_approval']==1
    assert report['summary']['submitted_unverified']==1
    assert len({x['job_id'] for x in report['items']})==3
    assert len({x['account_id'] for x in report['items']})==3
    history=client.get(f"/api/pipelines/{w['pid']}/auto-post/history").json()
    assert history['counts']['posted']==1 and history['counts']['needs_review']==2
    # Authenticated UI reads these persisted results, including pending/unverified.
    page=web_browser.new_page(viewport={'width':1280,'height':1000});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    route_bundle(page,live_server,staged_bundle)
    try:
        login(page,live_server)
        page.get_by_role('button',name='Mở Workspace').click()
        page.locator('.nav-item[data-page=auto-posting]').click()
        expect(page.get_by_text('Một cấu hình cho toàn Workspace',exact=True)).to_be_visible()
        viewports(page,'multi-account-config')
        page.get_by_role('tab',name='Facebook Groups',exact=True).click()
        viewports(page,'multi-account-groups')
        page.get_by_role('tab',name='Albums và Caption',exact=True).click()
        viewports(page,'multi-account-library')
        page.get_by_role('tab',name='Lịch và báo cáo',exact=True).click()
        expect(page.get_by_text('Đã xác minh đăng',exact=True).first).to_be_visible()
        expect(page.get_by_text('Chờ Admin nhóm duyệt',exact=True)).to_be_visible()
        expect(page.get_by_text('Đã gửi, chưa xác minh',exact=True)).to_be_visible()
        page.locator('details.cycle-report-row').filter(has=page.get_by_text('Đã xác minh đăng',exact=True)).locator('summary').click()
        expect(page.get_by_role('link',name='Xem bài đã xác minh')).to_have_count(1)
        viewports(page,'multi-account-report')
        page.route('**/auto-post/cycles',lambda route:route.fulfill(status=500,json={'detail':'private fixture diagnostics'}))
        page.get_by_role('button',name='Làm mới',exact=True).click()
        expect(page.get_by_text('Chưa tải được cấu hình và báo cáo',exact=True)).to_be_visible()
        assert 'private fixture diagnostics' not in page.locator('body').inner_text()
        assert not errors,errors
    finally:page.close()


@pytest.mark.parametrize('outcome,clicks,expected',[('anonymous_unavailable',0,'needs_review'),('expired_session',0,'needs_review')])
def test_live_boundary_stops_without_submit(client,world,monkeypatch,outcome,clicks,expected):
    w=world(accounts=1,groups=1);m.prepare_auto_post_jobs();promote_all(w)
    job=m.claim_next_job();assert job
    proof=offline_execute(monkeypatch,job,outcome)
    assert proof['clicks']==clicks and proof['result']['state']==expected,proof
    report=client.get(f"/api/pipelines/{w['pid']}/auto-post/cycles/1").json()
    assert report['summary']['posted']==0
