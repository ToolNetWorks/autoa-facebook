import concurrent.futures
from PIL import Image
import pytest
from test_app import client
from test_group_worker_contract import idle_background_workers
from test_first_post_flow import make_pipeline,make_account,make_group
import app as m


def campaign(client,session):
    p=make_pipeline(client,'@campaign');a=make_account(client,p,'FB',session)
    g=make_group(client,p,a,'Group','https://www.facebook.com/groups/campaign/')
    image=m.UPLOADS/str(p)/'picture.png';image.parent.mkdir(parents=True,exist_ok=True)
    Image.new('RGB',(320,240),'blue').save(image)
    with image.open('rb') as stream:
        upload=client.post('/api/uploads',params={'pipeline_id':p},files={'file':('picture.png',stream,'image/png')})
    assert upload.status_code==200,upload.text
    return p,a,g,{'group_ids':[g],'body':'Saved even before verification','media':[upload.json()['path']],
                  'scheduled_at':m.iso(m.now()-m.timedelta(minutes=1))}


@pytest.mark.parametrize('session',['expired','checkpoint','unknown'])
def test_campaign_persists_before_execution_checks(client,session):
    p,a,g,payload=campaign(client,session)
    response=client.post(f'/api/pipelines/{p}/group-posts',json=payload)
    assert response.status_code==201,response.text
    result=response.json();assert result['jobs_created']==1,result
    assert result['published'] is False
    with m.db() as conn:
        row=conn.execute('SELECT * FROM jobs WHERE id=?',(result['jobs'][0]['job_id'],)).fetchone()
        assert (row['pipeline_id'],row['account_id'],row['group_id'],row['post_id'])==(p,a,g,result['post_id'])
        assert row['state']=='queued'
    claimed=m.claim_next_job()
    if session=='unknown':
        # Unknown is evaluated by the real browser session check at execution.
        assert claimed and claimed['id']==row['id']
    else:
        assert claimed is None


def test_campaign_concurrent_duplicate_does_not_leave_orphan_posts(client):
    p,_,_,payload=campaign(client,'expired')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:client.post(f'/api/pipelines/{p}/group-posts',json=payload),range(2)))
    assert [r.status_code for r in responses]==[201,201]
    assert sorted(r.json()['jobs_created'] for r in responses)==[0,1]
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM posts WHERE pipeline_id=?',(p,)).fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM jobs WHERE pipeline_id=?',(p,)).fetchone()[0]==1
