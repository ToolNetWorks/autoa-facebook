from datetime import timedelta
from test_app import client, fake_precheck
from test_workspace_v3 import fixture_workspace, payload
import app as m


def test_group_requires_images_and_anonymous_mode(client):
    p, a = fixture_workspace(client, '@photos')
    g = client.post('/api/groups', json={'pipeline_id': p,'account_id':a,
        'label':'My authorized group', 'url':'https://www.facebook.com/groups/223344/',
        'approved_target': True}).json()['id']
    common={'pipeline_id':p,'body':'Caption','group_ids':[g],
       'scheduled_at':m.iso(m.now()+timedelta(hours=2))}
    assert client.post('/api/posts',json={**common, 'media':[]}).status_code == 422
    photo=client.post('/api/uploads',params={'pipeline_id':p},files={'file':('a.jpg',b'\xff\xd8\xff'+b'0'*70,'image/jpeg')}).json()['path']
    assert client.post('/api/posts',json={**common,'media':[photo],'anonymous_only':False}).status_code == 422
    result=client.post('/api/posts',json={**common,'media':[photo]})
    assert result.status_code == 201, result.text
    record=client.get('/api/jobs').json()[0]
    assert record['anonymous_only']==1


def test_legacy_queued_group_jobs_are_quarantined_on_upgrade(client):
    p, a = fixture_workspace(client,'@old')
    with m.db() as conn:
        post=conn.execute('INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only) VALUES(?,?,?,?,0)', (p,'original','[]',m.iso())).lastrowid
        group=conn.execute('INSERT INTO groups(pipeline_id,account_id,label,url,group_key,approved_target,created_at) VALUES(?,?,?,?,?,?,?)', (p,a,'Group','https://www.facebook.com/groups/222/','222',1,m.iso())).lastrowid
        conn.execute('INSERT INTO jobs(post_id,group_id,account_id,scheduled_at,updated_at) VALUES(?,?,?,?,?)',(post,group,a,m.iso(m.now()+timedelta(hours=4)),m.iso()))
    m.init_db()
    assert client.get('/api/jobs').json()[0]['state']=='needs_review'


def test_reels_are_personal_only_and_queued_experimentally(client):
    p,a=fixture_workspace(client,'@reels')
    g=client.post('/api/groups',json={'pipeline_id':p,'account_id':a,
        'label':'Group', 'url':'https://www.facebook.com/groups/333/',
        'approved_target':True}).json()['id']
    video=client.post('/api/video-uploads',params={'pipeline_id':p},files={'file':('reel.mp4',b'\x00\x00\x00\x18ftypisom'+b'0'*160,'video/mp4')})
    assert video.status_code==200
    args=payload(p,a,'reel')
    args.update(target_url='',body='Description',media=[video.json()['path']],auto_publish=True)
    assert client.post('/api/manual-tasks',json={**args,'group_id':g}).status_code==422
    assert client.post('/api/manual-tasks',json=args).status_code==409
    args['precheck_id']=fake_precheck(client,p,a,video.json()['path'],args['body'])
    created=client.post('/api/manual-tasks',json=args)
    assert created.status_code==201,created.text
    assert created.json()['status']=='queued_experimental'
    task_id=created.json()['id']
    assert client.patch(f'/api/manual-tasks/{task_id}',json={'status':'posted'}).status_code==409
    assert client.patch(f'/api/manual-tasks/{task_id}',json={'status':'cancelled'}).status_code==200
