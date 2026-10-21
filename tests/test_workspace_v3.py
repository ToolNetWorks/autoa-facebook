from datetime import timedelta

from test_app import client, fake_precheck
import ui_live
from ui_live import live_server, web_browser  # noqa: F401  (registers the fixtures)
import app as m


def fixture_workspace(client, name):
    pipeline = client.post('/api/pipelines', json={'name': name})
    assert pipeline.status_code == 201, pipeline.text
    pipeline_id = pipeline.json()['id']
    account = client.post('/api/accounts', json={'pipeline_id': pipeline_id, 'label': 'Facebook chính'})
    assert account.status_code == 201
    account_id = account.json()['id']
    return pipeline_id, account_id


def payload(pipeline, account, task_type='comment', **kwargs):
    return dict(pipeline_id=pipeline, account_id=account, task_type=task_type,
                scheduled_at=m.iso(m.now() + timedelta(hours=1)),
                body='Bình luận đã duyệt', target_url='https://www.facebook.com/somebody/posts/123',
                **kwargs)


def test_workspace_cards_and_scoped_task_listing(client, live_server, web_browser):
    one, a1 = fixture_workspace(client, '@hiu9h')
    two, a2 = fixture_workspace(client, '@shop01')
    assert client.get('/').status_code == 200
    html = client.get('/').text
    # The interface is a single page application: the server only ships the shell.
    assert 'id="root"' in html
    assert '/static/assets/' in html
    for pipeline, account in [(one, a1), (two, a2)]:
        assert client.post('/api/manual-tasks', json=payload(pipeline, account)).status_code == 201
    scoped = client.get(f'/api/manual-tasks?pipeline_id={one}')
    assert scoped.status_code == 200
    assert len(scoped.json()) == 1
    assert scoped.json()[0]['pipeline_id'] == one
    assert len(client.get('/api/manual-tasks').json()) == 2

    # The dashboard card opens the workspace of that pipeline in the browser.
    page = web_browser.new_page()
    try:
        ui_live.login(page, live_server)
        page.get_by_role('button', name='Mở Workspace').first.click()
        page.wait_for_timeout(1200)
        assert '#/workspace/' in page.url
        assert str(one) in page.url
        assert page.locator('.nav-item.active').count() >= 1
    finally:
        page.close()


def test_cross_workspace_mutation_rejected(client):
    p1, a1 = fixture_workspace(client, '@one')
    p2, a2 = fixture_workspace(client, '@two')
    invalid = client.post('/api/manual-tasks', json=payload(p2, a1))
    # A row of another workspace is hidden completely, not merely rejected.
    assert invalid.status_code in (404, 422)
    gid = client.post('/api/groups', json={'pipeline_id': p1, 'account_id': a1,
                      'label': 'Group của one', 'url': 'https://www.facebook.com/groups/991/',
                      'approved_target': True}).json()['id']
    assert client.post('/api/manual-tasks', json=payload(p2, a2, group_id=gid)).status_code in (404, 422)
    assert client.post('/api/manual-tasks', json=payload(p1, a1, group_id=gid)).status_code == 201


def test_manual_comment_requires_facebook_post_url(client):
    p, a = fixture_workspace(client, '@hiu9h')
    for value in ('http://www.facebook.com/post/11', 'https://evil.example/post/11',
                  'https://facebook.com.evil.example/post/11', 'https://facebook.com/'):
        args = payload(p, a)
        args['target_url'] = value
        assert client.post('/api/manual-tasks', json=args).status_code == 422
    created = client.post('/api/manual-tasks', json=payload(p, a))
    assert created.status_code == 201
    assert created.json()['manual_only'] is True
    task_id = created.json()['id']
    assert client.patch(f'/api/manual-tasks/{task_id}', json={'status': 'completed'}).status_code == 200
    assert client.patch(f'/api/manual-tasks/{task_id}', json={'status': 'completed'}).status_code == 409


def test_reels_require_valid_video_file(client):
    p, a = fixture_workspace(client, '@hiu9h')
    args = payload(p, a, task_type='reel')
    args['target_url'] = ''
    assert client.post('/api/manual-tasks', json=args).status_code == 422
    content = b'\x00\x00\x00\x14ftypisom' + b'\x00' * 96
    uploaded = client.post('/api/video-uploads', params={'pipeline_id': p},
                            files={'file': ('clip.mp4', content, 'video/mp4')})
    assert uploaded.status_code == 200, uploaded.text
    args['media'] = [uploaded.json()['path']]
    # without a pre-check the reel is refused by the safety engine
    assert client.post('/api/manual-tasks', json=args).status_code == 409
    check = fake_precheck(client, p, a, uploaded.json()['path'], args.get('body', ''))
    args['precheck_id'] = check
    created = client.post('/api/manual-tasks', json=args)
    assert created.status_code == 201, created.text
    assert created.json()['status'] == 'awaiting_manual'
    path = uploaded.json()['path'].split('/')[-1]
    assert client.get('/api/manual-media/' + path).status_code == 200
    assert client.get('/api/manual-media/' + uploaded.json()['path']).status_code == 200
    rejected = client.post('/api/video-uploads', params={'pipeline_id': p},
                            files={'file': ('invalid.mp4', b'not-mp4', 'video/mp4')})
    assert rejected.status_code == 415
