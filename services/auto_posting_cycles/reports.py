"""Cycle reports combine assignments with actual persisted worker evidence."""
import json
from collections import Counter

from services.group_job_lifecycle import verified_group_post_link
from services.auto_posting_cycles import assignment, clock, constraints, content, repository as repo


def report(conn, pipeline_id, cycle_id):
    cycle = repo.owned(conn,'auto_cycles',cycle_id,pipeline_id)
    rows = conn.execute('''SELECT t.*,g.group_name,g.label group_label,g.url group_url,
        a.label account_label,a.fb_name account_name,m.original_name media_name,
        j.state execution_state,j.verification_status,j.permalink,j.evidence,j.started_at,j.updated_at executed_at,j.scheduled_at execution_scheduled,
        aj.status auto_state FROM auto_cycle_targets t
        JOIN groups g ON g.id=t.group_id AND g.pipeline_id=t.pipeline_id
        LEFT JOIN accounts a ON a.id=t.account_id AND a.pipeline_id=t.pipeline_id
        LEFT JOIN media_assets m ON m.id=t.media_id AND m.pipeline_id=t.pipeline_id
        LEFT JOIN jobs j ON j.id=t.job_id AND j.pipeline_id=t.pipeline_id
            AND j.account_id=t.account_id AND j.group_id=t.group_id AND j.post_id=t.post_id
        LEFT JOIN auto_post_jobs aj ON aj.id=t.auto_job_id AND aj.pipeline_id=t.pipeline_id
            AND aj.account_id=t.account_id AND aj.group_id=t.group_id
        WHERE t.cycle_id=? AND t.pipeline_id=? ORDER BY t.id''',(cycle_id,pipeline_id)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        status = item['execution_state'] or item['auto_state'] or item['status']
        if item['status']=='skipped':
            status = 'skipped'
        if status=='posted' and (item['verification_status']!='verified' or not verified_group_post_link(item['permalink'],item['group_url'])):
            status = 'submitted_unverified'
        permalink = item['permalink'] if status=='posted' else ''
        item['scheduled_for'] = item['execution_scheduled'] or item['scheduled_for']
        items.append({k:item.get(k) for k in ('id','group_id','group_name','group_label','group_url','account_id',
            'account_label','account_name','media_id','media_name','caption','caption_source','post_id','auto_job_id',
            'job_id','reason_code','failure_level','scheduled_for','started_at','executed_at','evidence')} |
            {'status':status,'permalink':permalink})
    counts = Counter(x['status'] for x in items)
    config = repo.config(conn,pipeline_id)
    return {'id':cycle_id,'pipeline_id':pipeline_id,'timezone':json.loads(cycle['settings_json'])['timezone'],'due_at':cycle['due_at'],
        'state':'pending' if cycle['state']=='pending' else 'completed' if not any(
            counts[x] for x in ('planned','preparing','assigned','pending','approved','queued','running')) else 'processing',
        'items':items,'excluded_groups':json.loads(cycle['excluded_groups_json']),
        'summary':{'total_groups':len(items)+cycle['excluded_disabled'],'eligible_groups':sum(x['account_id'] is not None and x['media_id'] is not None for x in items),
            'posted':counts['posted'],'pending_approval':counts['pending_approval'],
            'submitted_unverified':counts['submitted_unverified'],'needs_review':counts['needs_review'],
            'disabled_groups':cycle['excluded_disabled']+sum(x['reason_code']=='SKIPPED_GROUP_DISABLED' for x in items),
            'skipped':counts['skipped'],'failed':counts['failed'],'no_eligible_account':counts['no_eligible_account']},
        'next_run_at':config['next_run_at'] if config and json.loads(config['settings_json'])['enabled'] else None}


def overview(conn, pipeline_id, moment):
    config = repo.config(conn,pipeline_id)
    settings = json.loads(config['settings_json']) if config else None
    groups = []
    for row in conn.execute('SELECT id,label,url FROM groups WHERE pipeline_id=? ORDER BY id',(pipeline_id,)):
        group = dict(row)
        group['global_state'] = repo.global_state(conn,pipeline_id,group['id'])
        group['label_state'] = 'Group Bị Vô Hiệu Hoá' if group['global_state']=='DISABLED' else group['global_state']
        group['accounts'] = []
        for account in conn.execute('SELECT id FROM accounts WHERE pipeline_id=? ORDER BY id',(pipeline_id,)).fetchall():
            if settings:
                group['accounts'].append({'account_id':account[0], 'state':assignment.pair_status(conn,pipeline_id,account[0],group['id'],settings,moment)})
        groups.append(group)
    selected = [g for g in groups if not settings or not settings['group_ids'] or g['id'] in settings['group_ids']]
    eligible = sum(any(a['state']=='ACTIVE' and (not settings['account_ids'] or a['account_id'] in settings['account_ids']) for a in g['accounts']) for g in selected)
    # A read-only upper bound, not a promise that AI/quotas/future preflight pass.
    expected = None
    if settings:
        candidates = [g for g in selected if any(a['state']=='ACTIVE' and
            (not settings['account_ids'] or a['account_id'] in settings['account_ids']) for a in g['accounts'])
            and not assignment.destination_busy(conn,pipeline_id,g['id'])]
        slot = clock.allowed_at(moment,settings)
        account_ids = {a['account_id'] for g in candidates for a in g['accounts'] if a['state']=='ACTIVE'
                       and (not settings['account_ids'] or a['account_id'] in settings['account_ids'])}
        capacity = sum(max(0,constraints.effective(conn,pipeline_id,aid,settings)['account']-
            constraints.used(conn,pipeline_id,slot,settings,account_id=aid)) for aid in account_ids)
        expected = min(len(candidates),settings['groups_per_cycle'],capacity)
        if not content.select_image(conn,pipeline_id,settings):
            expected = 0
        if account_ids:
            limit = constraints.effective(conn,pipeline_id,next(iter(account_ids)),settings)['pipeline']
            if limit:
                expected = min(expected,max(0,limit-constraints.used(conn,pipeline_id,slot,settings)))
    return {'groups':groups,'selected_accounts':len(settings['account_ids']) if settings and settings['account_ids'] else conn.execute('SELECT COUNT(*) FROM accounts WHERE pipeline_id=?',(pipeline_id,)).fetchone()[0],
            'eligible_groups':eligible if settings else None,'disabled_groups':sum(g['global_state']=='DISABLED' for g in selected),
            'expected_jobs':expected,'estimate_kind':'upper_bound',
            'next_run_at':config['next_run_at'] if config and settings['enabled'] else None}
