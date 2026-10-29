"""Regression contracts from the ownership audit and integration review."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_app import client
from test_group_worker_contract import due_job, idle_background_workers
from test_campaign_persistence import campaign
from test_first_post_flow import make_group
import app as m
from services import facebook_selectors as selectors
from services.analytics_worker import sync_attribution
from services.browser_session import atomic_storage_state
from services.logging import JsonlEventLogger
from services.retry_policy import AmbiguousPublishStateError, run_with_bounded_retry


def test_campaign_attribution_uses_execution_id_not_colliding_post_id(client):
    old_job,other,_,_=due_job(client)
    # Reserve execution ID 2 in a different tenant, but do not reserve posts.id=2.
    with m.db() as conn:
        conn.execute('UPDATE jobs SET id=2 WHERE id=?',(old_job,))
    p,a,g,payload=campaign(client,'expired')
    g2=make_group(client,p,a,'Second','https://facebook.com/groups/second-target/')
    payload['group_ids'].append(g2)
    response=client.post(f'/api/pipelines/{p}/group-posts',json=payload)
    assert response.status_code==201,response.text
    result=response.json()
    assert result['post_id']==2
    assert result['jobs_created']==2,result
    execution_ids={row['job_id'] for row in result['jobs']}
    assert 2 not in execution_ids
    with m.db() as conn:
        rows=conn.execute('SELECT * FROM post_attribution').fetchall()
        assert {row['post_id'] for row in rows}==execution_ids
        assert {row['pipeline_id'] for row in rows}=={p}
        assert all(row['published_by_account_id'] is None for row in rows)
        assert not sync_attribution(conn,'group_post',2,pipeline_id=p,published_by=a)
        assert conn.execute('SELECT COUNT(*) FROM post_attribution WHERE pipeline_id=?',(other,)).fetchone()[0]==0


def test_attribution_rejects_cross_account_and_preserves_legacy_conflict(client):
    job,p,a,g=due_job(client)
    with m.db() as conn:
        assert not sync_attribution(conn,'group_post',job,published_by=a,pipeline_id=p)
        conn.execute("UPDATE jobs SET state='posted',verification_status='verified',permalink='https://facebook.com/groups/contract/posts/local/' WHERE id=?",(job,))
        assert not sync_attribution(conn,'group_post',job,published_by=a+100,pipeline_id=p)
        assert sync_attribution(conn,'group_post',job,published_by=a,pipeline_id=p)
        before=tuple(conn.execute('SELECT * FROM post_attribution').fetchone())
        assert not sync_attribution(conn,'group_post',job,pipeline_id=p+100)
        assert tuple(conn.execute('SELECT * FROM post_attribution').fetchone())==before


def test_registry_pins_profile_until_workflow_finishes(tmp_path,monkeypatch):
    for name in ('active.json','v1.json','v2.json'):
        (tmp_path/name).write_bytes((selectors.CONFIG/name).read_bytes())
    monkeypatch.setattr(selectors,'CONFIG',tmp_path)
    @selectors.selector_workflow
    async def workflow():
        first=selectors.current_registry()
        (tmp_path/'active.json').write_text(json.dumps({'version':'v2','profile':'desktop_semantic','locales':['en']}))
        await asyncio.sleep(0)
        assert selectors.current_registry() is first
        assert first.profile=='desktop'
    asyncio.run(workflow())
    assert selectors.current_registry().profile=='desktop_semantic'
    assert selectors.SelectorRegistry(version='v1',profile='desktop').version=='v1'


def test_registry_validation_rejects_unreviewed_path_and_xpath(tmp_path,monkeypatch):
    with pytest.raises(ValueError):selectors.SelectorRegistry(version='../v2')
    doc=json.loads((selectors.CONFIG/'v2.json').read_text())
    (tmp_path/'active.json').write_bytes((selectors.CONFIG/'active.json').read_bytes())
    doc['profiles']['desktop']['publish']=[{'css':'xpath=/html/body/button'}]
    (tmp_path/'v2.json').write_text(json.dumps(doc));monkeypatch.setattr(selectors,'CONFIG',tmp_path)
    with pytest.raises(ValueError):selectors.SelectorRegistry()


def test_atomic_session_preserves_last_good_snapshot_on_invalid_export(tmp_path):
    target=tmp_path/'private.json';target.write_text('previous session')
    context=AsyncMock();context.storage_state.return_value={'cookies':[]}
    with pytest.raises(ValueError):asyncio.run(atomic_storage_state(context,target))
    assert target.read_text()=='previous session'
    context.storage_state.return_value={'cookies':[],'origins':[]}
    asyncio.run(atomic_storage_state(context,target))
    assert json.loads(target.read_text())=={'cookies':[],'origins':[]}
    assert target.stat().st_mode & 0o777==0o600
    assert list(tmp_path.glob('.session-*'))==[]


def test_jsonl_preserves_all_events_and_redacts(tmp_path):
    target=tmp_path/'events.jsonl';one=JsonlEventLogger(target);two=JsonlEventLogger(target)
    assert one.emit('before',token='fixture-private')
    assert two.emit('after',message='cookie=fixture-private')
    rows=[json.loads(row) for row in target.read_text().splitlines()]
    assert [row['event'] for row in rows]==['before','after']
    assert 'fixture-private' not in target.read_text()
    assert target.stat().st_mode & 0o777==0o600


def test_retry_override_cannot_repeat_uncertain_submit():
    operation=AsyncMock(side_effect=AmbiguousPublishStateError('uncertain'))
    with pytest.raises(AmbiguousPublishStateError):
        asyncio.run(run_with_bounded_retry(operation,should_retry=lambda _:True))
    assert operation.await_count==1


def test_cli_preflight_cannot_enable_publish(tmp_path,monkeypatch):
    import fb_browser
    source=tmp_path/'request.json';target=tmp_path/'result.json'
    source.write_text(json.dumps({'dry_run':False}))
    runner=AsyncMock(return_value={'state':'needs_review','verification':'draft_verified'})
    monkeypatch.setattr(fb_browser,'attempt_post',runner)
    monkeypatch.setattr('sys.argv',['fb_browser.py','preflight',str(source),str(target)])
    fb_browser.main()
    assert runner.call_args.args[0]['dry_run'] is True
    assert json.loads(target.read_text())['verification']=='draft_verified'


def test_startup_preserves_expired_web_sessions(client):
    with m.db() as conn:
        conn.execute("UPDATE user_sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        before=[tuple(row) for row in conn.execute('SELECT * FROM user_sessions ORDER BY id')]
    async def restart_lifespan():
        async with m.lifespan(m.app):
            pass
    asyncio.run(restart_lifespan())
    with m.db() as conn:
        after=[tuple(row) for row in conn.execute('SELECT * FROM user_sessions ORDER BY id')]
    assert before and after==before
    assert client.get('/api/auth/me').status_code==401


def test_history_and_review_reject_group_landing_url_as_post_evidence(client):
    job,p,a,g=due_job(client)
    with m.db() as conn:
        post=conn.execute('SELECT post_id FROM jobs WHERE id=?',(job,)).fetchone()[0]
        link='https://www.facebook.com/groups/contract/'
        conn.execute("UPDATE jobs SET state='needs_review',verification_status='verified',permalink=? WHERE id=?",(link,job))
    assert client.post(f'/api/jobs/{job}/review',json={'state':'posted'}).status_code==409
    with m.db() as conn:
        conn.execute("UPDATE jobs SET state='posted' WHERE id=?",(job,))  # corrupt legacy fixture only
        conn.execute("""INSERT INTO auto_post_jobs(pipeline_id,account_id,group_id,post_id,caption,status,
            permalink,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,'legacy','posted',?,'legacy-proof',?,?)""",
            (p,a,g,post,link,m.iso(),m.iso()))
    response=client.get(f'/api/pipelines/{p}/auto-post/history').json()
    assert response['counts']['posted']==0
    assert response['jobs'][0]['status']=='submitted_unverified'
    assert client.get(f'/api/pipelines/{p}/auto-post/dashboard').json()['totals']['published_today']==0


def test_history_hides_worker_internal_diagnostics(client):
    job,p,a,g=due_job(client)
    with m.db() as conn:
        post=conn.execute('SELECT post_id FROM jobs WHERE id=?',(job,)).fetchone()[0]
        conn.execute("""INSERT INTO auto_post_jobs(pipeline_id,account_id,group_id,post_id,caption,status,
            error,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,'fixture','failed',?,'diagnostic-fixture',?,?)""",
            (p,a,g,post,'Traceback /private/runtime.py SELECT secret_fixture FROM private_table',m.iso(),m.iso()))
    response=client.get(f'/api/pipelines/{p}/auto-post/history')
    assert 'secret_fixture' not in response.text and '/private/' not in response.text
    assert response.json()['jobs'][0]['error_code']=='PUBLISHING_REVIEW_REQUIRED'
