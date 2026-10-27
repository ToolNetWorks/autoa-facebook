"""Authenticated staging UI against isolated real API/SQLite; no production login."""
from pathlib import Path
from urllib.parse import urlsplit
import mimetypes
from playwright.sync_api import expect
from test_app import client
from test_group_worker_contract import idle_background_workers,due_job
from test_account_session_ui import staged_bundle
from ui_live import live_server,web_browser,login
import app as m


def route_bundle(page,base,bundle):
    def assets(route):
        path=urlsplit(route.request.url).path.removeprefix('/static/')
        asset=(bundle/path).resolve();assert asset.is_relative_to(bundle)
        route.fulfill(path=str(asset),content_type=mimetypes.guess_type(asset.name)[0])
    page.route('**/static/**',assets)
    page.route(base+'/',lambda route:route.fulfill(path=str(bundle/'index.html'),content_type='text/html; charset=utf-8'))


def viewports(page,name):
    output=Path('/tmp/toolnet-remaining-ui');output.mkdir(mode=0o700,exist_ok=True)
    for width in (390,402,430,440,768,834,1280,1440):
        page.set_viewport_size({'width':width,'height':1000});page.wait_for_timeout(180)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(name,width)
        page.screenshot(path=str(output/f'{name}-{width}.png'),full_page=True)


def test_authenticated_dashboard_api_error_and_post_link(client,live_server,web_browser,staged_bundle):
    job,pipeline,account,group=due_job(client)
    with m.db() as conn:
        post=conn.execute('SELECT post_id FROM jobs WHERE id=?',(job,)).fetchone()[0]
        conn.execute("UPDATE jobs SET state='failed',posting_status='failed' WHERE id=?",(job,))
        auto=conn.execute('''INSERT INTO auto_post_jobs(pipeline_id,account_id,group_id,post_id,caption,posting_mode,status,idempotency_key,created_at,updated_at)
                            VALUES(?,?,?,?,'UI test caption','anonymous','failed','ui-test',?,?)''',(pipeline,account,group,post,m.iso(),m.iso())).lastrowid
    page=web_browser.new_page(viewport={'width':1280,'height':1000});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    route_bundle(page,live_server,staged_bundle)
    page.route('**/api/admin/analytics/overview',lambda route:route.fulfill(status=500,json={'detail':'private internal failure'}))
    try:
        login(page,live_server)
        expect(page.get_by_text('Chưa tải được thống kê',exact=True)).to_be_visible()
        assert 'private internal failure' not in page.locator('body').inner_text()
        assert page.locator('.kpi-grid').count()==0
        expect(page.get_by_text('@worker-contract',exact=True)).to_be_visible()
        rows=page.request.get(live_server+'/api/admin/pipelines').json()
        assert len(rows)==1 and rows[0]['accounts_count']==1 and rows[0]['groups_count']==1
        assert rows[0]['failed_count']==1 and rows[0]['posted_count']==0
        for label,value in [('Pipeline','1'),('Tài khoản Facebook','1'),('Nhóm','1'),('Công việc lỗi','1')]:
            expect(page.locator('.stat').filter(has=page.get_by_text(label,exact=True)).first.locator('strong')).to_have_text(value)
        viewports(page,'dashboard-error')
        page.unroute('**/api/admin/analytics/overview')
        page.get_by_role('button',name='Thử lại',exact=True).click()
        expect(page.get_by_text('Chưa tải được thống kê',exact=True)).to_have_count(0)
        page.get_by_role('button',name='Mở Workspace').click()
        page.locator('.nav-item[data-page=auto-posting]').click()
        page.get_by_role('tab',name='Danh sách bài đăng').click()
        page.get_by_text('UI test caption',exact=True).click()
        link=page.get_by_role('link',name=f'Post #{post}',exact=True)
        expect(link).to_have_attribute('href',f'#/workspace/{pipeline}/group-post?post_id={post}')
        viewports(page,'auto-job-details')
        link.click()
        expect(page.get_by_text('Chi tiết job (1 group)',exact=True)).to_be_visible()
        expect(page.get_by_text('Thất bại',exact=True).first).to_be_visible()
        assert not errors,errors
    finally:page.close()
