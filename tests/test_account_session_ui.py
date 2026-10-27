"""Accounts page against the staged bundle and isolated API, never production."""
from pathlib import Path
from urllib.parse import urlsplit
import mimetypes
import subprocess

import pytest
from test_app import client  # noqa: F401
from ui_live import live_server, web_browser, login  # noqa: F401
from test_group_worker_contract import idle_background_workers  # noqa: F401
import app as m


@pytest.fixture(scope='session')
def staged_bundle(tmp_path_factory):
    bundle = tmp_path_factory.mktemp('session-ui-bundle')
    frontend = Path(__file__).resolve().parents[1] / 'frontend'
    subprocess.run(['npm', 'run', 'build', '--', '--outDir', str(bundle)],
                   cwd=frontend, check=True, capture_output=True, text=True)
    return bundle


def test_account_session_labels_and_required_viewports(client, live_server, web_browser, staged_bundle):
    bundle = staged_bundle
    pipeline = client.post('/api/pipelines', json={'name': '@session-ui'}).json()['id']
    for status in ('expired', 'checkpoint', 'connected'):
        account = client.post('/api/accounts', json={'pipeline_id':pipeline, 'label':'Test '+status}).json()['id']
        with m.db() as conn:
            conn.execute('UPDATE accounts SET session_status=?,session_checked_at=? WHERE id=?',
                         (status, '2020-01-01T00:00:00+00:00', account))
    page = web_browser.new_page(viewport={'width':1280,'height':1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    def assets(route):
        path = urlsplit(route.request.url).path.removeprefix('/static/')
        asset = (bundle / path).resolve()
        assert asset.is_relative_to(bundle)
        route.fulfill(path=str(asset), content_type=mimetypes.guess_type(asset.name)[0])
    page.route('**/static/**', assets)
    page.route(live_server+'/', lambda route: route.fulfill(path=str(bundle/'index.html'),content_type='text/html; charset=utf-8'))
    try:
        login(page, live_server)
        page.get_by_role('button', name='Mở Workspace').first.click()
        page.locator('.nav-item[data-page=accounts]').click()
        page.get_by_text('Cần xác minh bảo mật',exact=True).wait_for()
        assert page.get_by_text('Cần đăng nhập lại',exact=True).count() > 0
        assert page.get_by_text('Cần kiểm tra lại',exact=True).count() > 0
        output=Path('/tmp/toolnet-blockers-ui');output.mkdir(mode=0o700,exist_ok=True)
        for width in (390,402,430,440,768,834,1280,1440):
            page.set_viewport_size({'width':width,'height':1000})
            page.wait_for_timeout(180)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), width
            page.screenshot(path=str(output/f'accounts-{width}.png'),full_page=True)
        assert not errors, errors
    finally:
        page.close()
