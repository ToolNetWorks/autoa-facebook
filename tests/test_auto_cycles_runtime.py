import concurrent.futures
import json
import sqlite3
import os
import subprocess
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from test_auto_cycles import client, world, tick
from services.auto_posting_cycles import engine, repository as repo, runtime
from services.account_identity import claim, DuplicateFacebookIdentity
from services import ai_provider
import app as m


def promote_all(w):
    with m.db() as conn:
        conn.execute('UPDATE auto_post_jobs SET scheduled_for=? WHERE pipeline_id=?',(w['moment'].isoformat(),w['pid']))
    # Existing timer promotes at most five per batch; repeated calls are idempotent.
    m.publish_auto_post_jobs();m.publish_auto_post_jobs()
    with m.db() as conn:
        conn.execute('UPDATE jobs SET scheduled_at=? WHERE pipeline_id=?',(w['moment'].isoformat(),w['pid']))


def test_group_cooldown_rechecked_after_planning_without_blocking_other_group(client,world):
    w=world(accounts=3,groups=2);tick(w);promote_all(w)
    with m.db() as conn:
        rows=conn.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        source=dict(rows[0]);other=dict(rows[1])
        # A publication by another account after this cycle was planned.
        unused=next(aid for aid in w['accounts'] if aid not in (source['account_id'],other['account_id']))
        historical_post=conn.execute('INSERT INTO posts(pipeline_id,body,created_at) VALUES(?,?,?)',
            (w['pid'],'A previous publication',w['moment'].isoformat())).lastrowid
        conn.execute('''INSERT INTO jobs(post_id,pipeline_id,account_id,group_id,state,scheduled_at,updated_at)
            VALUES(?,?,?,?,'posted',?,?)''',(historical_post,w['pid'],unused,source['group_id'],
                w['moment'].isoformat(),w['moment'].isoformat()))
    assert m.claim_next_job()['id']==other['id']
    from services.auto_posting_cycles import constraints
    with m.db() as conn:
        earliest=constraints.execution_not_before(conn,w['pid'],source['account_id'],source['group_id'],
            w['settings'],w['moment'],source['id'])
        assert earliest>=w['moment']+timedelta(minutes=120)
        conn.execute("UPDATE jobs SET state='skipped' WHERE group_id=?",(source['group_id'],))
        assert constraints.used(conn,w['pid'],w['moment'],w['settings'],group_id=source['group_id'])==0


def test_overview_never_promises_jobs_without_media_or_with_open_work(client,world):
    w=world(accounts=2,groups=2)
    endpoint=f"/api/pipelines/{w['pid']}/auto-post/cycle-config"
    assert client.get(endpoint).json()['overview']['expected_jobs']==2
    tick(w)
    assert client.get(endpoint).json()['overview']['expected_jobs']==0
    with m.db() as conn:
        conn.execute("UPDATE auto_cycle_targets SET status='failed'")
        conn.execute("UPDATE auto_post_jobs SET status='failed'")
        conn.execute('UPDATE media_assets SET usable=0')
    assert client.get(endpoint).json()['overview']['expected_jobs']==0


def test_legacy_allocators_cannot_create_parallel_jobs_even_when_cycles_paused(client,world):
    from services import post_scheduler
    from scheduler import ctv_allocator,job_allocator
    w=world(accounts=1,groups=1)
    for enabled in (True,False):
        client.put(f"/api/pipelines/{w['pid']}/auto-post/cycle-config",json={**w['settings'],'enabled':enabled})
        with m.db() as conn:
            conn.execute('UPDATE pipeline_auto_post_settings SET enabled=1 WHERE pipeline_id=?',(w['pid'],))
            assert post_scheduler.plan(conn,w['pid'])['reason_code']=='CYCLE_CONFIG_ACTIVE'
            assert post_scheduler.distribute(conn,w['pid'])['created']==[]
            assert ctv_allocator.allocate(conn,w['pid'])['reason_code']=='CYCLE_CONFIG_ACTIVE'
            assert job_allocator.allocate(conn,w['pid'])['reason_code']=='CYCLE_CONFIG_ACTIVE'
            assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs').fetchone()[0]==0


def test_worker_crash_restart_requires_review_and_continues_other_account(client,world):
    w=world(accounts=2,groups=2);tick(w);promote_all(w)
    code="""import os
from datetime import datetime
from pathlib import Path
import app
app.DB_PATH=Path(os.environ['FIXTURE_DATABASE'])
app.now=lambda:datetime.fromisoformat(os.environ['FIXTURE_MOMENT'])
job=app.claim_next_job()
assert job is not None
os._exit(0)
"""
    child=subprocess.run([__import__('sys').executable,'-c',code],cwd=m.ROOT,
        env={**os.environ,'DATA_DIR':str(m.DATA),'FIXTURE_MOMENT':w['moment'].isoformat(),'FIXTURE_DATABASE':str(m.DB_PATH)},capture_output=True,text=True,timeout=30)
    assert child.returncode==0,child.stderr
    with m.db() as conn:
        interrupted=dict(conn.execute("SELECT * FROM jobs WHERE state='running'").fetchone())
    # Only the isolated app DB is restarted; no service is invoked.
    m.init_db()
    with m.db() as conn:
        runtime.reconcile(conn)
        assert conn.execute('SELECT state FROM jobs WHERE id=?',(interrupted['id'],)).fetchone()[0]=='needs_review'
        assert conn.execute('SELECT status FROM auto_post_jobs WHERE post_id=?',(interrupted['post_id'],)).fetchone()[0]=='needs_review'
    next_job=m.claim_next_job()
    assert next_job and next_job['account_id']!=interrupted['account_id']
    assert m.claim_next_job() is None


def test_preparation_lease_recovers_without_duplicate_auto_job(client,world):
    w=world(accounts=1,groups=1)
    with m.db() as conn:engine.schedule_due(conn,w['moment'])
    with m.db() as conn:
        conn.execute("UPDATE auto_cycle_targets SET lease_token='dead-worker',lease_until=?",((w['moment']+timedelta(minutes=5)).isoformat(),))
    assert not engine.prepare_one(m.db,w['moment'],None)
    assert engine.prepare_one(m.db,w['moment']+timedelta(minutes=6),None)
    assert not engine.prepare_one(m.db,w['moment']+timedelta(minutes=6),None)
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs').fetchone()[0]==1


def test_anonymous_unavailable_and_pending_approval_block_only_destination(client,world):
    w=world(accounts=2,groups=3)
    with m.db() as conn:
        conn.execute('UPDATE group_screening_results SET evidence_json=? WHERE group_id=?',
            (json.dumps({'accessible':True,'can_post':True,'supports_anonymous':False}),w['groups'][0]))
    tick(w)
    with m.db() as conn:
        assert conn.execute('SELECT reason_code FROM auto_cycle_targets WHERE group_id=?',(w['groups'][0],)).fetchone()[0]=='NO_ELIGIBLE_ACCOUNT'
        target=conn.execute('SELECT id,auto_job_id FROM auto_cycle_targets WHERE group_id=?',(w['groups'][1],)).fetchone()
        conn.execute("UPDATE auto_cycle_targets SET status='pending_approval' WHERE id=?",(target['id'],))
        conn.execute("UPDATE auto_post_jobs SET status='pending_approval' WHERE id=?",(target['auto_job_id'],))
    client.post(f"/api/pipelines/{w['pid']}/auto-post/cycles",json={'idempotency_key':'second-cycle-manual'})
    tick(w)
    with m.db() as conn:
        assert conn.execute("SELECT reason_code FROM auto_cycle_targets WHERE cycle_id=2 AND group_id=?",(w['groups'][1],)).fetchone()[0]=='DESTINATION_HAS_OPEN_WORK'


def test_disabled_reenable_requires_new_pair_evidence(client,world):
    w=world(accounts=1,groups=1);gid=w['groups'][0]
    base=f"/api/pipelines/{w['pid']}/auto-post/groups/{gid}/global-status"
    assert client.put(base,json={'state':'DISABLED','evidence':'Reviewed Facebook disabled notice'}).status_code==200
    assert client.put(base,json={'state':'ACTIVE','evidence':'A bare assertion is insufficient'}).status_code==409
    assert client.put(base,json={'state':'UNKNOWN','evidence':'Cannot bypass disabled by resetting to unknown'}).status_code==409
    with m.db() as conn:
        # Simulates a later real check in the isolated fixture.
        conn.execute('UPDATE group_screening_results SET last_checked_at=?',((m.datetime.now(m.timezone.utc)+timedelta(seconds=1)).isoformat(),))
    assert client.put(base,json={'state':'ACTIVE','evidence':'A subsequent check confirmed access and permission'}).status_code==200


def test_uid_duplicate_import_stops_before_browser_and_db_constraint(client,world,monkeypatch):
    w=world(accounts=1,groups=1)
    new=client.post('/api/accounts',json={'pipeline_id':w['pid'],'label':'Unverified second account'}).json()['id']
    import fb_browser
    browser=AsyncMock();monkeypatch.setattr(fb_browser,'import_cookie_session',browser)
    payload=[{'name':'c_user','value':str(122+w['accounts'][0]),'domain':'.facebook.com'},
             {'name':'xs','value':'synthetic-offline-only','domain':'.facebook.com'}]
    result=client.post(f'/api/accounts/{new}/cookies',files={'file':('fixture.json',json.dumps(payload).encode(),'application/json')})
    assert result.status_code==409,result.text
    browser.assert_not_called()
    with m.db() as conn:
        fingerprint=conn.execute('SELECT fb_uid_fingerprint FROM accounts WHERE id=?',(w['accounts'][0],)).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):conn.execute('UPDATE accounts SET fb_uid_fingerprint=? WHERE id=?',(fingerprint,new))


def test_ai_context_is_pipeline_scoped_and_unique_captions_are_enforced(client,world,monkeypatch):
    w=world(accounts=2,groups=2)
    other=world(accounts=1,groups=1)
    settings={**w['settings'],'caption_mode':'ai','unique_captions':True}
    client.put(f"/api/pipelines/{w['pid']}/auto-post/cycle-config",json=settings)
    calls=[]
    def generate(system,prompt,provider):
        calls.append(json.loads(prompt));return {'caption':'Một nội dung chung đã kiểm tra'}
    monkeypatch.setattr(ai_provider,'chat_json',generate)
    with m.db() as conn:
        cfg=json.loads(repo.config(conn,other['pid'])['settings_json']);cfg['enabled']=False
        repo.save_config(conn,other['pid'],cfg,1,w['moment'].isoformat())
    engine.tick(m.db,w['moment'],provider_config=ai_provider.ProviderConfig(base_url='https://offline.invalid',api_key='fixture'),rng=__import__('random').Random(1))
    assert len(calls)==2 and all(c['pipeline']=='@cycles-1' for c in calls)
    assert {c['group'] for c in calls}=={'Group 0','Group 1'}
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM auto_cycle_targets WHERE reason_code='DUPLICATE_CAPTION'").fetchone()[0]==1


def test_album_scope_exhaustion_and_media_reference_protection(client,world):
    w=world(accounts=1,groups=1);other=world(accounts=1,groups=1)
    endpoint=f"/api/pipelines/{w['pid']}/auto-post/albums"
    assert client.post(endpoint,json={'name':'Foreign','media_ids':[other['media']]}).status_code==404
    album=client.post(endpoint,json={'name':'Allowed','media_ids':[w['media']]})
    assert album.status_code==201
    assert client.delete(f"/api/pipelines/{w['pid']}/media/{w['media']}").status_code==409
    # An empty selected Album never falls back to another Album or Pipeline.
    empty=client.post(endpoint,json={'name':'Empty','media_ids':[]}).json()['id']
    client.put(f"/api/pipelines/{w['pid']}/auto-post/cycle-config",json={**w['settings'],'album_id':empty})
    tick(w)
    with m.db() as conn:
        assert conn.execute('SELECT reason_code FROM auto_cycle_targets WHERE pipeline_id=?',(w['pid'],)).fetchone()[0]=='MEDIA_EXHAUSTED'


def test_paused_and_expired_pipelines_do_not_block_other_pipeline(client,world):
    a=world(accounts=1,groups=1);b=world(accounts=1,groups=1)
    with m.db() as conn:
        engine.schedule_due(conn,a['moment'])
    client.put(f"/api/pipelines/{a['pid']}/auto-post/cycle-config",json={**a['settings'],'enabled':False})
    tick(b)
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs WHERE pipeline_id=?',(b['pid'],)).fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs WHERE pipeline_id=?',(a['pid'],)).fetchone()[0]==0


def test_uneven_pairs_are_not_fixed_five_groups_per_account(client,world):
    w=world(accounts=10,groups=50)
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status='expired' WHERE id NOT IN (?,?)",w['accounts'][:2])
    tick(w)
    with m.db() as conn:
        rows=conn.execute('SELECT account_id,COUNT(*) FROM auto_cycle_targets WHERE account_id IS NOT NULL GROUP BY account_id').fetchall()
        assert len(rows)==2 and sum(r[1] for r in rows)==50
        assert all(r[1]>5 for r in rows)


def test_admin_pipeline_account_and_group_limits_are_effective(client,world):
    w=world(accounts=2,groups=6)
    with m.db() as conn:
        conn.execute('UPDATE pipeline_auto_post_settings SET pipeline_daily_limit=3 WHERE pipeline_id=?',(w['pid'],))
        conn.execute('INSERT INTO account_posting_limits(account_id,daily_limit,min_gap_minutes,group_daily_limit,paused,updated_at) VALUES(?,1,30,1,0,?)',(w['accounts'][0],w['moment'].isoformat()))
    tick(w)
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs').fetchone()[0]==3
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs WHERE account_id=?',(w['accounts'][0],)).fetchone()[0]<=1


def test_wrong_group_permalink_never_counts_as_posted(client,world):
    w=world(accounts=2,groups=2);tick(w);promote_all(w)
    with m.db() as conn:
        job=conn.execute('SELECT id,group_id FROM jobs ORDER BY id').fetchone()
        other_url=conn.execute('SELECT url FROM groups WHERE id=?',(w['groups'][1],)).fetchone()[0]+'posts/fixture-post/'
        conn.execute("UPDATE jobs SET state='posted',verification_status='verified',permalink=? WHERE id=?",(other_url,job['id']))
        runtime.reconcile(conn)
    report=client.get(f"/api/pipelines/{w['pid']}/auto-post/cycles/1").json()
    assert report['summary']['posted']==0 and report['summary']['submitted_unverified']==1


def test_group_checks_are_pair_scoped_and_reuse_existing_screening_queue(client,world):
    w=world(accounts=2,groups=1)
    result=client.post(f"/api/pipelines/{w['pid']}/auto-post/groups/{w['groups'][0]}/check",json={'account_ids':[w['accounts'][1]]})
    assert result.status_code==202,result.text
    with m.db() as conn:
        row=conn.execute('SELECT pipeline_id,account_id,group_id FROM screening_queue').fetchone()
        assert tuple(row)==(w['pid'],w['accounts'][1],w['groups'][0])
        repo.record_pair_observation(conn,w['pid'],w['accounts'][1],w['groups'][0],{'is_member':False,'accessible':False})
        assert repo.global_state(conn,w['pid'],w['groups'][0])!='DISABLED'


def test_frozen_assignment_rejects_caption_tampering(client,world):
    w=world(accounts=1,groups=1);tick(w)
    with m.db() as conn:conn.execute("UPDATE auto_post_jobs SET caption='Changed after planning'")
    m.publish_auto_post_jobs()
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
        assert conn.execute('SELECT reason_code FROM auto_cycle_targets').fetchone()[0]=='ASSIGNMENT_CHANGED'
