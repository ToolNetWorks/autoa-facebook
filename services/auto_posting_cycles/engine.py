"""Cycle planning and caption preparation called by the existing scheduler timer."""
import json
import logging
import secrets
from datetime import timedelta

from services.auto_posting_cycles import assignment, clock, content, repository as repo

LOG = logging.getLogger('facebook-pipeline.cycles')


def request_cycle(conn, pipeline_id, key, moment):
    pipeline = conn.execute('SELECT enabled FROM pipelines WHERE id=?',(pipeline_id,)).fetchone()
    if not pipeline or not pipeline[0]:
        raise repo.CycleError('PIPELINE_PAUSED','Workspace đang tạm dừng.')
    config = repo.config(conn,pipeline_id)
    if not config or not json.loads(config['settings_json'])['enabled']:
        raise repo.CycleError('AUTO_POST_PAUSED','Hãy bật cấu hình tự động đăng trước.')
    repo.owner(conn,pipeline_id)
    inserted = conn.execute('''INSERT OR IGNORE INTO auto_cycles
        (pipeline_id,cycle_key,due_at,settings_json,created_at) VALUES(?,?,?,?,?)''',
        (pipeline_id,key,moment,config['settings_json'],moment))
    if inserted.rowcount and key.startswith('manual-'):
        settings = json.loads(config['settings_json'])
        next_run = clock.allowed_at(clock.parse(moment)+timedelta(minutes=settings['cycle_interval_minutes']),settings)
        conn.execute('UPDATE auto_cycle_configs SET next_run_at=? WHERE pipeline_id=?',(next_run.isoformat(),pipeline_id))
    return conn.execute('SELECT id FROM auto_cycles WHERE pipeline_id=? AND cycle_key=?',(pipeline_id,key)).fetchone()[0]


def _selected_groups(conn, pipeline_id, settings):
    ctv = repo.owner(conn,pipeline_id)
    groups = []
    for row in conn.execute('SELECT * FROM groups WHERE pipeline_id=? ORDER BY id',(pipeline_id,)):
        group = dict(row)
        if group['owner_user_id'] not in (None,ctv):
            continue
        if settings['group_ids'] and group['id'] not in settings['group_ids']:
            continue
        if repo.global_state(conn,pipeline_id,group['id'])=='DISABLED':
            continue
        groups.append(group)
    # Serve least recently assigned Groups first instead of starving the tail.
    last = {r['group_id']:r['last_cycle'] for r in conn.execute('''SELECT group_id,MAX(cycle_id) last_cycle
        FROM auto_cycle_targets WHERE pipeline_id=? GROUP BY group_id''',(pipeline_id,))}
    groups.sort(key=lambda g:(last.get(g['id'],0),g['id']))
    return groups[:settings['groups_per_cycle']]


def plan_cycle(conn, cycle, moment, rng=None):
    pipeline_id = cycle['pipeline_id']
    settings = json.loads(cycle['settings_json'])
    slot = clock.allowed_at(moment,settings)
    excluded = [dict(row) for row in conn.execute('SELECT id,group_name,label,url FROM groups WHERE pipeline_id=?',(pipeline_id,))
        if (not settings['group_ids'] or row['id'] in settings['group_ids']) and repo.global_state(conn,pipeline_id,row['id'])=='DISABLED']
    conn.execute('UPDATE auto_cycles SET excluded_disabled=?,excluded_groups_json=? WHERE id=?',
        (len(excluded),json.dumps(excluded,ensure_ascii=False),cycle['id']))
    for group in _selected_groups(conn,pipeline_id,settings):
        reason,level,state = '', '', 'preparing'
        account_id = None
        media = None
        if assignment.destination_busy(conn,pipeline_id,group['id'],cycle['id']):
            reason,level,state = 'DESTINATION_HAS_OPEN_WORK','group','skipped'
        else:
            selected = assignment.choose(conn,pipeline_id,group['id'],settings,moment,rng,scheduled_for=slot)
            if selected is None:
                reason,level,state = 'NO_ELIGIBLE_ACCOUNT','group','no_eligible_account'
            else:
                account_id,slot = selected['account_id'],selected['scheduled_for']
                media = content.select_image(conn,pipeline_id,settings)
                if media is None:
                    reason,level,state = 'MEDIA_EXHAUSTED','pipeline','failed'
        stamp = moment.isoformat()
        conn.execute('''INSERT INTO auto_cycle_targets
            (pipeline_id,cycle_id,group_id,account_id,media_id,status,reason_code,failure_level,
             scheduled_for,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (pipeline_id,cycle['id'],group['id'],account_id,media['id'] if media else None,state,reason,level,
             slot.isoformat(),stamp,stamp))
        if state=='preparing':
            slot = clock.allowed_at(slot+timedelta(minutes=settings['post_interval_minutes']),settings)
    conn.execute("UPDATE auto_cycles SET state='planned' WHERE id=? AND pipeline_id=?",(cycle['id'],pipeline_id))


def schedule_due(conn, moment, rng=None):
    """At most one overdue automatic cycle per Pipeline; never replay missed days."""
    conn.execute('BEGIN IMMEDIATE')
    for row in conn.execute('SELECT * FROM auto_cycle_configs').fetchall():
        config = dict(row)
        conn.execute('SAVEPOINT one_config')
        try:
            settings = json.loads(config['settings_json'])
            if not settings['enabled'] or clock.parse(config['next_run_at'])>moment:
                continue
            if clock.allowed_at(moment,settings)>moment:
                continue
            request_cycle(conn,config['pipeline_id'],'timer-'+config['next_run_at'],moment.isoformat())
            next_run = clock.allowed_at(moment+timedelta(minutes=settings['cycle_interval_minutes']),settings)
            conn.execute('UPDATE auto_cycle_configs SET next_run_at=? WHERE pipeline_id=?',(next_run.isoformat(),config['pipeline_id']))
        except Exception as error:
            conn.execute('ROLLBACK TO one_config')
            LOG.warning('cycle_plan_refused pipeline=%s type=%s',config['pipeline_id'],type(error).__name__)
        finally:
            conn.execute('RELEASE one_config')
    cycles = conn.execute("SELECT * FROM auto_cycles WHERE state='pending' ORDER BY id").fetchall()
    for row in cycles:
        cycle = dict(row)
        # Settings paused since request: preserve request without executing it.
        conn.execute('SAVEPOINT one_cycle')
        try:
            current = repo.config(conn,cycle['pipeline_id'])
            if not current or not json.loads(current['settings_json'])['enabled']:
                continue
            if not conn.execute('SELECT 1 FROM pipelines WHERE id=? AND enabled=1',(cycle['pipeline_id'],)).fetchone():
                continue
            # After downtime all observations may be stale. Allow bounded time
            # for existing read-only probes; never delay healthy destinations.
            if moment-clock.parse(cycle['created_at'])<timedelta(minutes=30):
                cycle_settings=json.loads(cycle['settings_json'])
                selected=_selected_groups(conn,cycle['pipeline_id'],cycle_settings)
                pending=conn.execute("SELECT 1 FROM screening_queue WHERE pipeline_id=? AND state IN ('queued','running')",(cycle['pipeline_id'],)).fetchone()
                if pending and selected and not any(assignment.choose(conn,cycle['pipeline_id'],g['id'],cycle_settings,moment,rng) for g in selected):
                    continue
            plan_cycle(conn,cycle,moment,rng)
        except Exception as error:
            conn.execute('ROLLBACK TO one_cycle')
            LOG.warning('cycle_plan_failed pipeline=%s type=%s',cycle['pipeline_id'],type(error).__name__)
        finally:
            conn.execute('RELEASE one_cycle')


def prepare_one(db, moment, provider_config):
    """A renewable preparation lease protects AI work without holding SQLite writes."""
    token = secrets.token_hex(16)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('''SELECT t.*,c.settings_json FROM auto_cycle_targets t
            JOIN auto_cycles c ON c.id=t.cycle_id AND c.pipeline_id=t.pipeline_id
            JOIN auto_cycle_configs f ON f.pipeline_id=t.pipeline_id
            WHERE t.status='preparing' AND (t.lease_until IS NULL OR datetime(t.lease_until)<=datetime(?))
            AND CASE WHEN json_valid(f.settings_json) THEN json_extract(f.settings_json,'$.enabled') ELSE 0 END=1
            ORDER BY t.id LIMIT 1''',(moment.isoformat(),)).fetchone()
        if row is None:
            return False
        target = dict(row)
        config = repo.config(conn,target['pipeline_id'])
        if not config or not json.loads(config['settings_json'])['enabled']:
            return False
        conn.execute('UPDATE auto_cycle_targets SET lease_until=?,lease_token=? WHERE id=?',
            ((moment+timedelta(minutes=10)).isoformat(),token,target['id']))
    settings = json.loads(target['settings_json'])
    caption,source,snapshot,error_code = '', '', {}, ''
    try:
        if target['caption']:
            caption,source,snapshot = target['caption'],target['caption_source'],json.loads(target['content_snapshot'])
        else:
            with db() as conn:
                caption,source,snapshot = content.generate(conn,target['pipeline_id'],target,settings,provider_config)
    except Exception as error:
        error_code = error.code if isinstance(error,repo.CycleError) else 'CONTENT_PREPARATION_FAILED'
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT status,lease_token FROM auto_cycle_targets WHERE id=?',(target['id'],)).fetchone()
        if current['status']!='preparing' or current['lease_token']!=token:
            return True
        config = repo.config(conn,target['pipeline_id'])
        if not config or not json.loads(config['settings_json'])['enabled']:
            conn.execute('''UPDATE auto_cycle_targets SET caption=?,caption_source=?,content_snapshot=?,
                lease_token=NULL,lease_until=NULL WHERE id=?''',(caption,source,json.dumps(snapshot),target['id']))
            return True
        if repo.global_state(conn,target['pipeline_id'],target['group_id'])=='DISABLED':
            error_code = 'SKIPPED_GROUP_DISABLED'
        key = content.caption_key(caption) if caption else ''
        if not error_code and settings['unique_captions'] and conn.execute('''SELECT 1 FROM auto_cycle_targets
            WHERE pipeline_id=? AND caption_key=? AND id!=? AND status NOT IN ('failed','skipped')''',
            (target['pipeline_id'],key,target['id'])).fetchone():
            error_code = 'DUPLICATE_CAPTION'
        if not error_code and conn.execute('''SELECT 1 FROM auto_cycle_targets WHERE pipeline_id=?
            AND group_id=? AND media_id=? AND caption_key=? AND id!=? AND status NOT IN ('failed','skipped')''',
            (target['pipeline_id'],target['group_id'],target['media_id'],key,target['id'])).fetchone():
            error_code = 'DUPLICATE_CONTENT'
        if error_code:
            status = 'skipped' if error_code=='SKIPPED_GROUP_DISABLED' else 'failed'
            conn.execute('''UPDATE auto_cycle_targets SET status=?,reason_code=?,failure_level=?,
                lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?''',
                (status,error_code,'group' if status=='skipped' else 'job',moment.isoformat(),target['id']))
            return True
        ctv = repo.owner(conn,target['pipeline_id'])
        nickname = conn.execute('SELECT nickname FROM group_accounts WHERE pipeline_id=? AND group_id=? AND account_id=?',
            (target['pipeline_id'],target['group_id'],target['account_id'])).fetchone()[0]
        state = 'pending' if settings['require_approval'] else 'approved'
        auto_id = conn.execute('''INSERT INTO auto_post_jobs
            (pipeline_id,media_id,account_id,group_id,caption,posting_mode,nickname,status,needs_approval,
             idempotency_key,created_at,updated_at,scheduled_for,created_by_ctv_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (target['pipeline_id'],target['media_id'],target['account_id'],target['group_id'],caption,
             settings['posting_mode'],nickname,state,int(settings['require_approval']),
             f"cycle:{target['cycle_id']}:group:{target['group_id']}",moment.isoformat(),moment.isoformat(),target['scheduled_for'],ctv)).lastrowid
        conn.execute('''UPDATE auto_cycle_targets SET auto_job_id=?,status=?,caption=?,caption_key=?,
            caption_source=?,content_snapshot=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?''',
            (auto_id,state,caption,key,source,json.dumps(snapshot,ensure_ascii=False),moment.isoformat(),target['id']))
    return True


def tick(db, moment, provider_config=None, preparation_batch=50, rng=None):
    from services.auto_posting_cycles.runtime import reconcile
    with db() as conn:
        if not repo.available(conn):
            return set()
        reconcile(conn)
    with db() as conn:
        schedule_due(conn,moment,rng)
        configured = {r[0] for r in conn.execute('SELECT pipeline_id FROM auto_cycle_configs')}
    for _ in range(preparation_batch):
        if not prepare_one(db,moment,provider_config):
            break
    return configured
