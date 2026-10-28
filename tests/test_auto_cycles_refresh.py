import hashlib
import hmac
import json
from datetime import timedelta

from test_auto_cycles import client,world,tick
from test_auto_cycles_runtime import promote_all
from services.auto_posting_cycles import assignment,engine,refresh
from services import account_session_state
import app as m


def observed(account_id):
    return {'status':'ok','accessible':True,'is_member':True,'can_post':True,'supports_anonymous':True,
        'session_observation':{'status':'connected','fingerprint':hmac.new(m.APP_SECRET.encode(),str(122+account_id).encode(),hashlib.sha256).hexdigest()}}


def test_automatic_probe_warmup_keeps_next_day_cycle_operational(client,world,monkeypatch):
    w=world(accounts=2,groups=2);tick(w)
    # First-cycle work failed before submission in the isolated fixture.
    with m.db() as conn:
        conn.execute("UPDATE auto_post_jobs SET status='failed'")
        conn.execute("UPDATE auto_cycle_targets SET status='failed'")
    tomorrow=w['moment']+timedelta(hours=25)
    monkeypatch.setattr(m,'now',lambda:tomorrow)
    refresh.enqueue_due(m.db,tomorrow,m.enqueue_screening)
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM screening_queue WHERE state='queued'").fetchone()[0]==2
        engine.schedule_due(conn,tomorrow)
        assert conn.execute("SELECT COUNT(*) FROM auto_cycles WHERE state='pending'").fetchone()[0]==1
    monkeypatch.setattr(m,'run_group_probe',lambda aid,*args:observed(aid))
    while item:=m.claim_screening_item():m.run_screening_item(item)
    m.prepare_auto_post_jobs()
    with m.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM auto_cycles').fetchone()[0]==2
        assert conn.execute("SELECT COUNT(*) FROM auto_post_jobs WHERE status='approved'").fetchone()[0]==2
        assert all(assignment.account_ready(dict(a),tomorrow) for a in conn.execute('SELECT * FROM accounts'))
        assert not conn.execute("SELECT 1 FROM jobs").fetchone()


def test_refresh_never_probes_disabled_or_checkpoint_and_is_bounded(client,world,monkeypatch):
    w=world(accounts=2,groups=3)
    from services.auto_posting_cycles import repository as repo
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status='checkpoint' WHERE id=?",(w['accounts'][0],))
        repo.set_global_state(conn,w['pid'],w['groups'][0],'DISABLED','Reviewed disabled evidence',1)
    moment=w['moment']+timedelta(hours=23)
    monkeypatch.setattr(m,'now',lambda:moment)
    refresh.enqueue_due(m.db,moment,m.enqueue_screening,budget=1)
    with m.db() as conn:
        rows=conn.execute('SELECT * FROM screening_queue').fetchall()
        assert len(rows)==1 and rows[0]['account_id']==w['accounts'][1]
        assert rows[0]['group_id']!=w['groups'][0]
        conn.execute("UPDATE screening_queue SET state='running'")
        refresh.recover_probes(conn,m.iso())
        assert conn.execute('SELECT state FROM screening_queue').fetchone()[0]=='queued'
        conn.execute("UPDATE screening_queue SET state='running',attempts=3")
        refresh.recover_probes(conn,m.iso())
        assert conn.execute('SELECT state FROM screening_queue').fetchone()[0]=='failed'


def test_probe_identity_mismatch_cannot_refresh_connected_state(client,world):
    w=world(accounts=1,groups=1)
    with m.db() as conn:
        before=conn.execute('SELECT session_checked_at FROM accounts WHERE id=?',(w['accounts'][0],)).fetchone()[0]
        assert not account_session_state.record_probe(conn,w['pid'],w['accounts'][0],{'status':'connected','fingerprint':'incorrect'},m.iso())
        assert conn.execute('SELECT session_status FROM accounts WHERE id=?',(w['accounts'][0],)).fetchone()[0]=='unknown'
        assert conn.execute('SELECT session_checked_at FROM accounts WHERE id=?',(w['accounts'][0],)).fetchone()[0]==before
        assert account_session_state.record_probe(conn,w['pid'],w['accounts'][0],None,m.iso())
        assert conn.execute('SELECT session_checked_at FROM accounts WHERE id=?',(w['accounts'][0],)).fetchone()[0]==before


def test_probe_proof_never_contains_raw_uid():
    from services.facebook_account_identity import session_observation
    proof=session_observation({'status':'connected','uid':'123'},'synthetic-secret')
    assert proof['status']=='connected' and len(proof['fingerprint'])==64
    assert '123' not in json.dumps(proof) and 'uid' not in proof


def test_post_outcome_preserves_identity_evidence_and_real_check_timestamp(client,world):
    w=world(accounts=1,groups=1);tick(w);promote_all(w)
    with m.db() as conn:
        original=(w['moment']-timedelta(hours=2)).isoformat()
        conn.execute('UPDATE group_screening_results SET last_checked_at=?',(original,))
        job=conn.execute('SELECT id FROM jobs').fetchone()[0]
        m.refresh_screening_for_job(conn,job)
        row=conn.execute('SELECT * FROM group_screening_results').fetchone()
        assert row['last_checked_at']==original
        assert json.loads(row['evidence_json'])['supports_anonymous'] is True
