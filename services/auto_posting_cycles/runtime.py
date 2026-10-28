"""Adapters for existing promotion/claim/execute; no independent worker loop."""
import json
import hashlib
from pathlib import Path

from services import analytics_worker
from services.media_store import safe_join
from services.auto_posting_cycles import assignment, clock, constraints, content, repository as repo


def target_for_auto(conn, auto_id):
    if not repo.available(conn):
        return None
    row = conn.execute('SELECT * FROM auto_cycle_targets WHERE auto_job_id=?',(auto_id,)).fetchone()
    return dict(row) if row else None


def target_for_job(conn, job_id):
    if not repo.available(conn):
        return None
    row = conn.execute('SELECT * FROM auto_cycle_targets WHERE job_id=?',(job_id,)).fetchone()
    return dict(row) if row else None


def stop_target(conn, target, code, level='job', skipped=False):
    state = 'skipped' if skipped else 'failed'
    conn.execute('UPDATE auto_cycle_targets SET status=?,reason_code=?,failure_level=?,updated_at=? WHERE id=?',
        (state,code,level,repo.now_iso(),target['id']))
    if target.get('auto_job_id'):
        conn.execute('UPDATE auto_post_jobs SET status=?,error=?,updated_at=? WHERE id=? AND pipeline_id=?',
            (state,code,repo.now_iso(),target['auto_job_id'],target['pipeline_id']))
    if target.get('job_id'):
        conn.execute("UPDATE jobs SET state=?,posting_status=?,details=?,updated_at=? WHERE id=? AND pipeline_id=? AND state='queued'",
            (state,state,code,repo.now_iso(),target['job_id'],target['pipeline_id']))


def _settings(conn, target):
    row = conn.execute('SELECT settings_json FROM auto_cycles WHERE id=? AND pipeline_id=?',(target['cycle_id'],target['pipeline_id'])).fetchone()
    return json.loads(row[0])


def promote(conn, auto_job, data_root, moment, album_bytes):
    """Return False for legacy jobs; preserve assigned account and frozen caption."""
    target = target_for_auto(conn,auto_job['id'])
    if target is None:
        return False
    if target['job_id'] or auto_job['status']!='approved':
        return True
    settings = _settings(conn,target)
    current = repo.config(conn,target['pipeline_id'])
    if not current or not json.loads(current['settings_json'])['enabled']:
        return True
    if repo.global_state(conn,target['pipeline_id'],target['group_id'])=='DISABLED':
        stop_target(conn,target,'SKIPPED_GROUP_DISABLED','group',True)
        return True
    ctv = repo.owner(conn,target['pipeline_id'])
    if auto_job['created_by_ctv_id']!=ctv:
        stop_target(conn,target,'PIPELINE_OWNER_CHANGED','pipeline')
        return True
    if assignment.pair_status(conn,target['pipeline_id'],target['account_id'],target['group_id'],settings,moment)!='ACTIVE':
        stop_target(conn,target,'PAIR_NOT_ELIGIBLE','account')
        return True
    media = repo.owned(conn,'media_assets',target['media_id'],target['pipeline_id'])
    if not content.media_ready(media):
        stop_target(conn,target,'MEDIA_NO_LONGER_ELIGIBLE','job')
        return True
    if (auto_job['caption']!=target['caption'] or auto_job['media_id']!=target['media_id'] or auto_job['account_id']!=target['account_id']
            or auto_job['group_id']!=target['group_id'] or auto_job['pipeline_id']!=target['pipeline_id']
            or auto_job['posting_mode']!=settings['posting_mode']):
        stop_target(conn,target,'ASSIGNMENT_CHANGED','job')
        return True
    source = safe_join(data_root,media['storage_key'])
    destination = Path(data_root)/'uploads'/str(target['pipeline_id'])/Path(media['storage_key']).name
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    payload = album_bytes(conn,media)
    if payload is None:
        stop_target(conn,target,'MEDIA_FILE_MISSING','job')
        return True
    image_hash = hashlib.sha256(payload).hexdigest()
    if image_hash != media['sha256']:
        stop_target(conn,target,'MEDIA_CONTENT_CHANGED','job')
        return True
    if source.resolve()!=destination.resolve() or not destination.is_file():
        destination.write_bytes(payload)
    destination.chmod(0o600)
    media_path = str(destination.relative_to(data_root))
    post = conn.execute('''INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only,owner_user_id)
        VALUES(?,?,?,?,1,?)''',(target['pipeline_id'],target['caption'],json.dumps([media_path]),moment.isoformat(),ctv)).lastrowid
    job_id = conn.execute('''INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,
        scheduled_at,updated_at,requested_posting_mode,requested_nickname,posting_status,verification_status,created_by_ctv_id)
        VALUES(?,?,?,?,'queued','',?,?,?,?,'pending','unverified',?)''',
        (post,target['group_id'],target['account_id'],target['pipeline_id'],auto_job['scheduled_for'],moment.isoformat(),
         auto_job['posting_mode'],auto_job['nickname'],ctv)).lastrowid
    conn.execute("UPDATE auto_post_jobs SET post_id=?,status='queued',updated_at=? WHERE id=? AND pipeline_id=?",
        (post,moment.isoformat(),auto_job['id'],target['pipeline_id']))
    snapshot = json.loads(target['content_snapshot']);snapshot['image_sha256']=image_hash
    conn.execute("UPDATE auto_cycle_targets SET post_id=?,job_id=?,status='queued',content_snapshot=?,updated_at=? WHERE id=?",
        (post,job_id,json.dumps(snapshot),moment.isoformat(),target['id']))
    conn.execute('''INSERT INTO media_usage_history(pipeline_id,media_id,account_id,group_id,job_id,used_at,note)
        VALUES(?,?,?,?,?,?,?)''',(target['pipeline_id'],target['media_id'],target['account_id'],target['group_id'],auto_job['id'],moment.isoformat(),'campaign cycle'))
    conn.execute('UPDATE media_assets SET usage_count=usage_count+1,last_used_at=?,updated_at=? WHERE id=? AND pipeline_id=?',
        (moment.isoformat(),moment.isoformat(),target['media_id'],target['pipeline_id']))
    analytics_worker.sync_attribution(conn,'group_post',job_id,created_by=ctv,pipeline_id=target['pipeline_id'])
    return True


def claim(conn, moment, profile_busy):
    """Skip local failures in this transaction and continue to other destinations."""
    if not repo.available(conn):
        return None
    rows = conn.execute('''SELECT j.*,g.url group_url,g.group_key,p.body,p.media_json,p.anonymous_only,
        t.id target_id,t.cycle_id,a.fb_uid_fingerprint expected_uid_fingerprint
        FROM jobs j JOIN auto_cycle_targets t ON t.job_id=j.id AND t.pipeline_id=j.pipeline_id
        JOIN groups g ON g.id=j.group_id AND g.pipeline_id=j.pipeline_id
        JOIN posts p ON p.id=j.post_id AND p.pipeline_id=j.pipeline_id
        JOIN accounts a ON a.id=j.account_id AND a.pipeline_id=j.pipeline_id
        WHERE j.state='queued' AND datetime(j.scheduled_at)<=datetime(?) ORDER BY j.scheduled_at,j.id''',
        (moment.isoformat(),)).fetchall()
    for row in rows:
        job = dict(row)
        target = target_for_job(conn,job['id'])
        if any(job[k]!=target[k] for k in ('pipeline_id','account_id','group_id','post_id')):
            stop_target(conn,target,'ASSIGNMENT_LINK_MISMATCH')
            continue
        try:
            settings = _settings(conn,target)
            job['expected_image_sha256'] = json.loads(target['content_snapshot']).get('image_sha256','')
        except (ValueError,TypeError):
            stop_target(conn,target,'ASSIGNMENT_SNAPSHOT_INVALID')
            continue
        current = repo.config(conn,job['pipeline_id'])
        if not current or not json.loads(current['settings_json'])['enabled']:
            continue
        pipeline = conn.execute('SELECT enabled FROM pipelines WHERE id=?',(job['pipeline_id'],)).fetchone()
        if not pipeline or not pipeline[0]:
            stop_target(conn,target,'PIPELINE_PAUSED','pipeline',True)
            continue
        if repo.global_state(conn,job['pipeline_id'],job['group_id'])=='DISABLED':
            stop_target(conn,target,'SKIPPED_GROUP_DISABLED','group',True)
            continue
        try:
            ctv = repo.owner(conn,job['pipeline_id'])
            owners = [repo.owned(conn,table,job[key],job['pipeline_id']).get('owner_user_id')
                      for table,key in [('accounts','account_id'),('groups','group_id'),('posts','post_id')]]
            if any(owner not in (None,ctv) for owner in owners):
                raise repo.CycleError('OWNERSHIP_CHANGED')
            decision = assignment.pair_status(conn,job['pipeline_id'],job['account_id'],job['group_id'],settings,moment)
        except repo.CycleError:
            decision = 'OWNERSHIP_CHANGED'
        if decision!='ACTIVE':
            stop_target(conn,target,decision,'account')
            continue
        selected_media = repo.owned(conn,'media_assets',target['media_id'],job['pipeline_id'])
        if not content.media_ready(selected_media):
            stop_target(conn,target,'MEDIA_NO_LONGER_ELIGIBLE')
            continue
        try:
            matching_media = json.loads(job['media_json']) == [f"uploads/{job['pipeline_id']}/{Path(selected_media['storage_key']).name}"]
        except (ValueError,TypeError):
            matching_media = False
        if job['body']!=target['caption'] or not matching_media:
            stop_target(conn,target,'JOB_CONTENT_CHANGED')
            continue
        if clock.allowed_at(moment,settings)>moment:
            conn.execute('UPDATE jobs SET scheduled_at=? WHERE id=?',(clock.allowed_at(moment,settings).isoformat(),job['id']))
            continue
        if profile_busy(job['account_id']) or conn.execute("SELECT 1 FROM jobs WHERE account_id=? AND state='running'",(job['account_id'],)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM manual_tasks WHERE account_id=? AND status='running'",(job['account_id'],)).fetchone():
            continue
        if not constraints.has_capacity(conn,job['pipeline_id'],job['account_id'],job['group_id'],settings,moment,exclude_job=job['id']):
            stop_target(conn,target,'EFFECTIVE_DAILY_LIMIT','account')
            continue
        try:
            earliest = constraints.execution_not_before(conn,job['pipeline_id'],job['account_id'],job['group_id'],settings,moment,job['id'])
        except (ValueError,TypeError):
            stop_target(conn,target,'ACCOUNT_HISTORY_INVALID','account')
            continue
        if earliest>moment:
            conn.execute('UPDATE jobs SET scheduled_at=? WHERE id=?',(earliest.isoformat(),job['id']))
            continue
        conn.execute("UPDATE jobs SET state='running',attempts=attempts+1,started_at=?,updated_at=? WHERE id=? AND state='queued'",
            (moment.isoformat(),moment.isoformat(),job['id']))
        conn.execute("UPDATE auto_post_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND pipeline_id=?",
            (moment.isoformat(),target['auto_job_id'],job['pipeline_id']))
        conn.execute("UPDATE auto_cycle_targets SET status='running',updated_at=? WHERE id=?",(moment.isoformat(),target['id']))
        return job
    return None


def before_execute(conn, job, moment):
    target = target_for_job(conn,job['id'])
    if target is None:
        return True
    if repo.global_state(conn,job['pipeline_id'],job['group_id'])=='DISABLED':
        # Claimed but browser not started: no submission has occurred.
        conn.execute("UPDATE jobs SET state='skipped',posting_status='skipped',details='SKIPPED_GROUP_DISABLED' WHERE id=? AND state='running'",(job['id'],))
        stop_target(conn,target,'SKIPPED_GROUP_DISABLED','group',True)
        return False
    settings = _settings(conn,target)
    try:
        ctv = repo.owner(conn,job['pipeline_id'])
        decision = assignment.pair_status(conn,job['pipeline_id'],job['account_id'],job['group_id'],settings,moment)
        if job['created_by_ctv_id']!=ctv:
            decision = 'OWNERSHIP_CHANGED'
    except repo.CycleError:
        decision = 'OWNERSHIP_CHANGED'
    if decision!='ACTIVE':
        conn.execute("UPDATE jobs SET state='needs_review',posting_status='needs_review',details='PAIR_CHANGED_BEFORE_BROWSER' WHERE id=?",(job['id'],))
        conn.execute("UPDATE auto_post_jobs SET status='needs_review',error='PAIR_CHANGED_BEFORE_BROWSER' WHERE id=?",(target['auto_job_id'],))
        conn.execute("UPDATE auto_cycle_targets SET status='needs_review',reason_code='PAIR_CHANGED_BEFORE_BROWSER',failure_level='account' WHERE id=?",(target['id'],))
        return False
    return True


def reconcile(conn):
    """Mirror recorded execution evidence; never infer success from a click/claim."""
    if not repo.available(conn):
        return
    from services.group_job_lifecycle import verified_group_post_link
    for row in conn.execute('''SELECT t.id,aj.status FROM auto_cycle_targets t JOIN auto_post_jobs aj
        ON aj.id=t.auto_job_id AND aj.pipeline_id=t.pipeline_id WHERE t.job_id IS NULL
        AND aj.status IN ('failed','cancelled') AND t.status NOT IN ('failed','skipped')''').fetchall():
        conn.execute("UPDATE auto_cycle_targets SET status=?,reason_code='PREPARATION_FAILED',failure_level='job' WHERE id=?",(row['status'],row['id']))
    rows = conn.execute('''SELECT t.id,t.status,t.auto_job_id,t.pipeline_id,j.state,j.verification_status,j.permalink,j.evidence,j.details,g.url
        FROM auto_cycle_targets t JOIN jobs j ON j.id=t.job_id AND j.pipeline_id=t.pipeline_id
        JOIN groups g ON g.id=t.group_id AND g.pipeline_id=t.pipeline_id''').fetchall()
    for row in rows:
        state = row['state']
        if row['status']=='skipped' and state=='skipped':
            continue
        if state=='posted' and (row['verification_status']!='verified' or not verified_group_post_link(row['permalink'],row['url'])):
            state = 'submitted_unverified'
        conn.execute('''UPDATE auto_post_jobs SET status=?,evidence=?,permalink=?,error=?,updated_at=?
            WHERE id=? AND pipeline_id=? AND status!=?''',
            (state,row['evidence'] or '',row['permalink'] or '',
             row['details'] if state in ('failed','needs_review') else '',repo.now_iso(),
             row['auto_job_id'],row['pipeline_id'],state))
        if state==row['status']:
            continue
        code = {'failed':'EXECUTION_FAILED','needs_review':'EXECUTION_NEEDS_REVIEW',
                'submitted_unverified':'SUBMISSION_UNVERIFIED','cancelled':'EXECUTION_CANCELLED'}.get(state,'')
        conn.execute('UPDATE auto_cycle_targets SET status=?,reason_code=?,failure_level=?,updated_at=? WHERE id=?',
            (state,code,'job' if code else '',repo.now_iso(),row['id']))
