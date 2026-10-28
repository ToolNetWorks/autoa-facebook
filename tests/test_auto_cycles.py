"""Multi-account cycles on isolated SQLite and the real API/worker contracts."""
import concurrent.futures
import hashlib
import hmac
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from test_app import client
from test_group_worker_contract import idle_background_workers
from test_multitenant import make_tenant, login_ctv
from test_first_post_flow import make_account, make_group, make_picture
from migrations import v019_group_nicknames, v020_unique_active_membership, v021_posting_cycles
from services import group_membership
from services.auto_posting_cycles import engine, reports, repository as repo, runtime
from schemas.auto_cycles import CycleSettings
import app as m


@pytest.fixture
def world(client, monkeypatch):
    moment = datetime.now(timezone.utc).replace(hour=8,minute=0,second=0,microsecond=0)
    monkeypatch.setattr(m,'now',lambda:moment)
    monkeypatch.setattr(m.media_cloud,'enabled',lambda:False)
    with m.db() as conn:
        conn.execute('BEGIN IMMEDIATE');v019_group_nicknames.apply(conn);v020_unique_active_membership.apply(conn);v021_posting_cycles.apply(conn)
    serial = 0
    def build(accounts=10,groups=50):
        nonlocal serial
        serial += 1
        pipeline,key,user = make_tenant(client,f'@cycles-{serial}',f'cycle-owner-{serial}')
        pid = pipeline['id']
        aids = [make_account(client,pid,f'Account {i}') for i in range(accounts)]
        gids = [make_group(client,pid,aids[0],f'Group {i}',f'https://www.facebook.com/groups/cycle-{pid}-{i}/') for i in range(groups)]
        mid = make_picture(client,pid,'cycle.png')
        from services.media_store import safe_join
        with m.db() as conn:
            key_media = conn.execute('SELECT storage_key FROM media_assets WHERE id=?',(mid,)).fetchone()[0]
            Image.new('RGB',(320,240),'steelblue').save(safe_join(m.DATA,key_media))
            conn.execute('UPDATE media_assets SET sha256=? WHERE id=?',(hashlib.sha256(safe_join(m.DATA,key_media).read_bytes()).hexdigest(),mid))
            conn.execute("INSERT INTO pipeline_auto_post_settings(pipeline_id,enabled,daily_limit_per_account,min_gap_minutes,created_at,updated_at) VALUES(?,0,100,1,?,?)",(pid,moment.isoformat(),moment.isoformat()))
            for aid in aids:
                uid = str(122+aid)
                fingerprint = hmac.new(m.APP_SECRET.encode(),uid.encode(),hashlib.sha256).hexdigest()
                conn.execute("UPDATE accounts SET session_checked_at=?,fb_uid_fingerprint=? WHERE id=?",(moment.isoformat(),fingerprint,aid))
                for gid in gids:
                    conn.execute("INSERT OR IGNORE INTO group_accounts(pipeline_id,account_id,group_id,posting_mode,nickname,status,created_at,updated_at) VALUES(?,?,?,'anonymous','','active',?,?)",(pid,aid,gid,moment.isoformat(),moment.isoformat()))
                    group_membership.record(conn,pid,aid,gid,group_membership.JOINED)
                    evidence=json.dumps({'accessible':True,'is_member':True,'can_post':True,'supports_anonymous':True})
                    conn.execute("INSERT INTO group_screening_results(pipeline_id,account_id,group_id,moderation_status,verification_source,confidence,evidence_json,last_checked_at,created_at,updated_at) VALUES(?,?,?,'unknown','browser',1,?,?,?,?)",(pid,aid,gid,evidence,moment.isoformat(),moment.isoformat(),moment.isoformat()))
        template = client.post(f'/api/pipelines/{pid}/auto-post/templates',json={'name':'Approved','body':'Chia sẻ từ {group_name}','approved':True})
        assert template.status_code==201,template.text
        settings=CycleSettings(enabled=True,template_ids=[template.json()['id']],timezone='UTC',start_time='00:00',end_time='23:59',post_interval_minutes=1,per_account_daily_limit=100,require_approval=False).model_dump()
        response=client.put(f'/api/pipelines/{pid}/auto-post/cycle-config',json=settings)
        assert response.status_code==200,response.text
        return {'pid':pid,'accounts':aids,'groups':gids,'media':mid,'settings':settings,'moment':moment,'user':user,'key':key}
    return build


def tick(w):
    return engine.tick(m.db,w['moment'],rng=random.Random(41),preparation_batch=100)


def test_ten_accounts_fifty_groups_balanced_persistent_idempotent(client,world):
    w=world();tick(w);tick(w)
    with m.db() as conn:
        rows=conn.execute('SELECT account_id,COUNT(*) n FROM auto_cycle_targets WHERE pipeline_id=? GROUP BY account_id',(w['pid'],)).fetchall()
        assert len(rows)==10 and sum(r['n'] for r in rows)==50
        assert max(r['n'] for r in rows)-min(r['n'] for r in rows)<=1
        assert conn.execute('SELECT COUNT(*) FROM auto_cycles').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs').fetchone()[0]==50
        assert conn.execute('SELECT COUNT(DISTINCT group_id) FROM auto_cycle_targets').fetchone()[0]==50
        assert conn.execute('SELECT COUNT(DISTINCT media_id) FROM auto_cycle_targets').fetchone()[0]==1


def test_disabled_groups_excluded_from_future_cycles(client,world):
    w=world();disabled=w['groups'][:5]
    for gid in disabled:
        response=client.put(f"/api/pipelines/{w['pid']}/auto-post/groups/{gid}/global-status",json={'state':'DISABLED','evidence':'Admin confirmed with an independently reviewed screenshot.'})
        assert response.status_code==200,response.text
    tick(w)
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_cycle_targets').fetchone()[0]==45
        assert not conn.execute('SELECT 1 FROM auto_cycle_targets WHERE group_id IN ('+','.join('?'*5)+')',disabled).fetchone()
        # A destination skip is not restored by an account-local accessible result.
        assert repo.global_state(conn,w['pid'],disabled[0])=='DISABLED'


def test_expired_account_no_pair_and_account_local_access_errors(client,world):
    w=world(accounts=3,groups=4)
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status='expired' WHERE id=?",(w['accounts'][0],))
        conn.execute("UPDATE group_memberships SET membership_status='not_member' WHERE group_id=?",(w['groups'][0],))
        conn.execute("UPDATE group_screening_results SET evidence_json=? WHERE account_id=? AND group_id=?",(json.dumps({'accessible':False}),w['accounts'][1],w['groups'][1]))
    tick(w)
    with m.db() as conn:
        targets=[dict(r) for r in conn.execute('SELECT * FROM auto_cycle_targets ORDER BY group_id')]
        assert targets[0]['reason_code']=='NO_ELIGIBLE_ACCOUNT'
        assert all(t['account_id']!=w['accounts'][0] for t in targets)
        assert targets[1]['account_id']==w['accounts'][2]
        assert repo.global_state(conn,w['pid'],w['groups'][1])=='UNKNOWN'


@pytest.mark.parametrize('position',[0,1,2])
def test_disabled_first_middle_last_queue_continues(client,world,position):
    w=world(accounts=3,groups=3);tick(w)
    m.publish_auto_post_jobs()
    # Promote future slots in the test fixture only.
    with m.db() as conn:
        conn.execute('UPDATE auto_post_jobs SET scheduled_for=?',(w['moment'].isoformat(),))
    m.publish_auto_post_jobs()
    with m.db() as conn:
        conn.execute('UPDATE jobs SET scheduled_at=?',(w['moment'].isoformat(),))
        repo.set_global_state(conn,w['pid'],w['groups'][position],'DISABLED','Independent admin evidence',1)
    claimed=[]
    while job:=m.claim_next_job():claimed.append(job)
    assert len(claimed)==2 and all(j['group_id']!=w['groups'][position] for j in claimed)
    response=client.get(f"/api/pipelines/{w['pid']}/auto-post/cycles/1")
    assert response.status_code==200,response.text
    assert response.json()['summary']['skipped']==1
    assert response.json()['summary']['disabled_groups']==1


def test_continuous_cycles_no_replay_uncertain_submission(client,world):
    w=world(accounts=2,groups=2);tick(w)
    with m.db() as conn:
        conn.execute("UPDATE auto_cycle_targets SET status='posted'")
        conn.execute("UPDATE auto_post_jobs SET status='posted'")
    tomorrow={**w,'moment':w['moment']+timedelta(days=1)}
    engine.tick(m.db,tomorrow['moment'],rng=random.Random(41))
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_cycles').fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM auto_cycle_targets').fetchone()[0]==4
        # Repeating identical content into the same Group must be refused.
        assert conn.execute("SELECT COUNT(*) FROM auto_cycle_targets WHERE reason_code='DUPLICATE_CONTENT'").fetchone()[0]==2
    tick(tomorrow)
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_cycles').fetchone()[0]==2


def test_ai_error_only_explicit_approved_fallback(client,world,monkeypatch):
    w=world(accounts=1,groups=2)
    settings={**w['settings'],'caption_mode':'ai','fallback_template_id':None}
    assert client.put(f"/api/pipelines/{w['pid']}/auto-post/cycle-config",json=settings).status_code==200
    tick(w)
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM auto_cycle_targets WHERE reason_code='AI_CAPTION_FAILED'").fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs').fetchone()[0]==0
    settings['fallback_template_id']=w['settings']['template_ids'][0]
    client.put(f"/api/pipelines/{w['pid']}/auto-post/cycle-config",json=settings)
    client.post(f"/api/pipelines/{w['pid']}/auto-post/cycles",json={'idempotency_key':'approved-fallback'})
    tick(w)
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM auto_cycle_targets WHERE caption_source='approved_fallback'").fetchone()[0]==2


def test_album_exhaustion_review_failed_and_unique_caption(client,world):
    w=world(accounts=2,groups=3)
    settings={**w['settings'],'repeat_media':False}
    client.put(f"/api/pipelines/{w['pid']}/auto-post/cycle-config",json=settings)
    tick(w)
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs').fetchone()[0]==1
        assert conn.execute("SELECT COUNT(*) FROM auto_cycle_targets WHERE reason_code='MEDIA_EXHAUSTED'").fetchone()[0]==2
    w2=world(accounts=1,groups=1)
    with m.db() as conn:
        conn.execute("UPDATE media_assets SET safety_status='unknown',analysis_status='done',usable=1 WHERE id=?",(w2['media'],))
    tick(w2)
    with m.db() as conn:
        assert not conn.execute('SELECT 1 FROM auto_post_jobs WHERE pipeline_id=?',(w2['pid'],)).fetchone()


def test_concurrent_schedulers_and_claims_never_share_account(client,world):
    w=world(accounts=2,groups=4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:tick(w),range(2)))
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_cycles').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM auto_post_jobs').fetchone()[0]==4
        conn.execute('UPDATE auto_post_jobs SET scheduled_for=?',(w['moment'].isoformat(),))
    m.publish_auto_post_jobs()
    with m.db() as conn:conn.execute('UPDATE jobs SET scheduled_at=?',(w['moment'].isoformat(),))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs=[j for j in pool.map(lambda _:m.claim_next_job(),range(4)) if j]
    assert len(jobs)==2 and len({j['account_id'] for j in jobs})==2


def test_tenant_scope_and_composite_fk(client,world):
    a=world(accounts=1,groups=1);b=world(accounts=1,groups=1)
    ctv,response=login_ctv('cycle-owner-1',key=a['key']);assert response.status_code==200
    assert ctv.get(f"/api/pipelines/{b['pid']}/auto-post/cycle-config").status_code==404
    bad={**a['settings'],'account_ids':b['accounts']}
    assert ctv.put(f"/api/pipelines/{a['pid']}/auto-post/cycle-config",json=bad).status_code==404
    assert ctv.put(f"/api/pipelines/{a['pid']}/auto-post/groups/{a['groups'][0]}/global-status",json={'state':'DISABLED','evidence':'A generic account cannot disable Groups'}).status_code==403
    tick(a)
    with m.db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE auto_cycle_targets SET account_id=? WHERE pipeline_id=?',(b['accounts'][0],a['pid']))


def test_explicit_migration_rollback_and_identity_uniqueness(client):
    with m.db() as conn:
        conn.execute('BEGIN IMMEDIATE');v019_group_nicknames.apply(conn);v020_unique_active_membership.apply(conn);v021_posting_cycles.apply(conn);v021_posting_cycles.apply(conn)
        conn.rollback()
        assert not repo.available(conn)
    response=client.get('/api/pipelines/1/auto-post/cycle-config')
    assert response.status_code==503
