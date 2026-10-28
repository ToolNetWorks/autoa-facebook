"""One quota calculation across persisted plans, executions and legacy reservations."""
from datetime import timedelta
from scheduler import account_limits
from services.auto_posting_cycles import clock


def used(conn, pipeline_id, moment, settings, account_id=None, group_id=None, exclude_job=None):
    start,end = clock.day_bounds(moment,settings)
    scope = 'pipeline_id=?';args=[pipeline_id]
    if account_id is not None:
        scope+=' AND account_id=?';args.append(account_id)
    if group_id is not None:
        scope+=' AND group_id=?';args.append(group_id)
    executions = conn.execute(f'''SELECT COUNT(*) FROM jobs WHERE {scope}
        AND id!=? AND state NOT IN ('cancelled','failed','skipped')
        AND datetime(CASE WHEN state IN ('posted','pending_approval','submitted_unverified','needs_review')
            THEN COALESCE(published_at,updated_at) ELSE scheduled_at END)>=datetime(?)
        AND datetime(CASE WHEN state IN ('posted','pending_approval','submitted_unverified','needs_review')
            THEN COALESCE(published_at,updated_at) ELSE scheduled_at END)<datetime(?)''',(*args,exclude_job or -1,start,end)).fetchone()[0]
    pending = conn.execute(f'''SELECT COUNT(*) FROM auto_post_jobs WHERE {scope}
        AND post_id IS NULL AND status IN ('pending','approved')
        AND datetime(scheduled_for)>=datetime(?) AND datetime(scheduled_for)<datetime(?)''',(*args,start,end)).fetchone()[0]
    preparing = conn.execute(f'''SELECT COUNT(*) FROM auto_cycle_targets WHERE {scope}
        AND auto_job_id IS NULL AND status='preparing'
        AND datetime(scheduled_for)>=datetime(?) AND datetime(scheduled_for)<datetime(?)''',(*args,start,end)).fetchone()[0]
    # Reels share the Facebook account, but are not publications into this Group.
    reels = 0
    if group_id is None:
        reels = conn.execute(f'''SELECT COUNT(*) FROM manual_tasks WHERE {scope} AND auto_publish=1
            AND status NOT IN ('failed','cancelled') AND datetime(COALESCE(published_at,scheduled_at))>=datetime(?)
            AND datetime(COALESCE(published_at,scheduled_at))<datetime(?)''',(*args,start,end)).fetchone()[0]
    return executions+pending+preparing+reels


def effective(conn, pipeline_id, account_id, settings):
    policy = account_limits.settings_of(conn,pipeline_id)
    account = account_limits.limits_of(conn,account_id)
    return {'account':min(settings['per_account_daily_limit'],account_limits.effective_limit(conn,account_id,policy)),
            'pipeline':int(policy.get('pipeline_daily_limit') or 0),
            'group':int(account.get('group_daily_limit') or 0),
            'gap':max(settings['post_interval_minutes'],account_limits.effective_gap(conn,account_id,policy))}


def has_capacity(conn, pipeline_id, account_id, group_id, settings, moment, exclude_job=None):
    limits = effective(conn,pipeline_id,account_id,settings)
    if used(conn,pipeline_id,moment,settings,account_id=account_id,exclude_job=exclude_job)>=limits['account']:
        return False
    if limits['pipeline'] and used(conn,pipeline_id,moment,settings,exclude_job=exclude_job)>=limits['pipeline']:
        return False
    if limits['group'] and used(conn,pipeline_id,moment,settings,group_id=group_id,exclude_job=exclude_job)>=limits['group']:
        return False
    return True


def slot_for(conn, pipeline_id, account_id, group_id, settings, nominal):
    limits = effective(conn,pipeline_id,account_id,settings)
    moments = [nominal]
    rows = conn.execute("""SELECT scheduled_for FROM auto_cycle_targets WHERE pipeline_id=? AND account_id=?
        AND status NOT IN ('failed','skipped','no_eligible_account')""",(pipeline_id,account_id)).fetchall()
    rows += conn.execute("""SELECT CASE WHEN state IN ('queued','running') THEN scheduled_at ELSE updated_at END
        FROM jobs WHERE pipeline_id=? AND account_id=? AND state NOT IN ('failed','cancelled','skipped')""",(pipeline_id,account_id)).fetchall()
    rows += conn.execute("""SELECT COALESCE(published_at,scheduled_at) FROM manual_tasks WHERE pipeline_id=? AND account_id=?
        AND auto_publish=1 AND status NOT IN ('failed','cancelled')""",(pipeline_id,account_id)).fetchall()
    for row in rows:
        if row[0]:
            moments.append(clock.parse(row[0])+timedelta(minutes=limits['gap']))
    group_gap = conn.execute('SELECT group_cooldown_minutes FROM pipeline_posting_schedule WHERE pipeline_id=?',(pipeline_id,)).fetchone()
    cooldown = int(group_gap[0]) if group_gap else 120
    for row in conn.execute("""SELECT updated_at FROM jobs WHERE pipeline_id=? AND group_id=?
        AND state IN ('posted','submitted_unverified','pending_approval','needs_review')""",(pipeline_id,group_id)):
        moments.append(clock.parse(row[0])+timedelta(minutes=cooldown))
    return clock.allowed_at(max(moments),settings)


def execution_not_before(conn, pipeline_id, account_id, group_id, settings, moment, job_id):
    """Recheck actual account/Group cooldowns without counting this reservation."""
    gap = effective(conn,pipeline_id,account_id,settings)['gap']
    group_row = conn.execute('SELECT group_cooldown_minutes FROM pipeline_posting_schedule WHERE pipeline_id=?',(pipeline_id,)).fetchone()
    group_gap = int(group_row[0]) if group_row else 120
    moments = [moment]
    for row in conn.execute("""SELECT account_id,group_id,COALESCE(published_at,updated_at) event_time
        FROM jobs WHERE pipeline_id=? AND id!=? AND (account_id=? OR group_id=?)
        AND state IN ('posted','pending_approval','submitted_unverified','needs_review')""",
        (pipeline_id,job_id,account_id,group_id)):
        delay = max(gap if row['account_id']==account_id else 0, group_gap if row['group_id']==group_id else 0)
        moments.append(clock.parse(row['event_time'])+timedelta(minutes=delay))
    for row in conn.execute("""SELECT COALESCE(published_at,updated_at) FROM manual_tasks
        WHERE pipeline_id=? AND account_id=? AND auto_publish=1 AND status='posted'""",(pipeline_id,account_id)):
        moments.append(clock.parse(row[0])+timedelta(minutes=gap))
    return clock.allowed_at(max(moments),settings)
