"""Real API/SQLite/claim/execute/subprocess/Chromium; Facebook is local DOM only."""
import json
import subprocess
from pathlib import Path
from PIL import Image
import pytest
from test_app import client
from test_group_worker_contract import idle_background_workers, due_job
import app as m


@pytest.mark.parametrize('outcome',['posted','pending_approval','submitted_unverified','expired_session'])
def test_worker_dispatch_database_history_and_no_resubmit(client,monkeypatch,outcome):
    job_id,pipeline,account,group=due_job(client)
    Image.new('RGB',(320,240),'steelblue').save(m.UPLOADS/str(pipeline)/'test.jpg')
    job=m.claim_next_job()
    assert job['id']==job_id
    from test_first_post_flow import make_group
    other_group=make_group(client,pipeline,account,'Other target','https://www.facebook.com/groups/other-target/')
    with m.db() as conn:
        for target,state in ((group,'queued'),(other_group,'pending')):
            conn.execute("""INSERT INTO auto_post_jobs(pipeline_id,account_id,group_id,post_id,caption,status,idempotency_key,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",(pipeline,account,target,job['post_id'],'Caption',state,f'dispatch-{target}',m.iso(),m.iso()))
    actual_run=subprocess.run
    helper=Path(__file__).parent/'helpers/offline_facebook_worker.py'
    calls=[]
    def run_offline(command,**kwargs):
        calls.append(command)
        kwargs['env']={**kwargs['env'],'OFFLINE_FACEBOOK_OUTCOME':outcome}
        return actual_run([command[0],str(helper),command[-2],command[-1]],**kwargs)
    monkeypatch.setattr(m.subprocess,'run',run_offline)
    m.execute_job(job)
    proof=json.loads((m.DATA/'dispatch-proof.json').read_text())
    expected='needs_review' if outcome=='expired_session' else outcome
    assert proof['clicks']==(0 if outcome=='expired_session' else 1)
    assert proof['intercepted_requests']>0
    assert proof['ids']==dict(pipeline_id=pipeline,account_id=account,group_id=group,job_id=job_id)
    assert proof['result']['state']==expected,proof['result']
    assert (m.EVIDENCE/proof['result']['evidence']).is_file()
    with m.db() as conn:
        row=dict(conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
    assert row['state']==expected
    assert bool(row['published_at'])==(outcome=='posted')
    history=client.get(f"/api/posts/{job['post_id']}/jobs")
    assert history.status_code==200
    assert history.json()[0]['state']==expected
    auto=client.get(f'/api/pipelines/{pipeline}/auto-post/history').json()
    by_group={entry['group_id']:entry for entry in auto['jobs']}
    assert by_group[group]['post_id']==job['post_id']
    assert by_group[group]['status']==expected
    assert by_group[other_group]['status']=='pending'
    assert m.claim_next_job() is None
    if outcome in ('submitted_unverified','pending_approval','posted'):
        assert client.post(f'/api/jobs/{job_id}/review',json={'state':'queued'}).status_code==409
    if outcome=='expired_session':
        with m.db() as conn:
            assert conn.execute('SELECT session_status FROM accounts WHERE id=?',(account,)).fetchone()[0]=='expired'
    assert len(calls)==1


def test_worker_rejects_unproven_success():
    from services.group_job_lifecycle import validated_result_state
    job={'group_url':'https://www.facebook.com/groups/one/'}
    for result in ({'state':'posted'}, {'state':'posted','verification':'verified','permalink':'https://www.facebook.com/groups/other/posts/1/'}):
        assert validated_result_state(result,job)=='submitted_unverified'
