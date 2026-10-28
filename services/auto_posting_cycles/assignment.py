"""Balanced random assignment to verified pairs; assignments never change on retry."""
import json
import random
from datetime import timedelta

from scheduler import account_limits
from services.auto_posting_cycles import clock, constraints, repository as repo


def fresh(value, moment, hours=24):
    try:
        delta = moment - clock.parse(value)
        return timedelta(0) <= delta <= timedelta(hours=hours)
    except (ValueError, TypeError):
        return False


def account_ready(account, moment):
    return bool(account['enabled'] and account['session_status']=='connected'
                and account.get('fb_uid_fingerprint') and fresh(account.get('session_checked_at'),moment))


def pair_status(conn, pipeline_id, account_id, group_id, settings, moment):
    """Never read group-wide legacy can_post/screening as another account's evidence."""
    group = repo.owned(conn,'groups',group_id,pipeline_id)
    account = repo.owned(conn,'accounts',account_id,pipeline_id)
    if repo.global_state(conn,pipeline_id,group_id)=='DISABLED':
        return 'DISABLED'
    if not group['enabled'] or not group['approved_target']:
        return 'CHECK_PENDING'
    from services.group_links import normalize_group_url
    try:
        if normalize_group_url(group['url'])[1].startswith('share:'):
            return 'CHECK_PENDING'
    except ValueError:
        return 'ACCESS_ERROR'
    if not account_ready(account,moment) or account_limits.is_paused(conn,account_id):
        return 'SESSION_INVALID'
    member = conn.execute('''SELECT membership_status FROM group_memberships
        WHERE pipeline_id=? AND account_id=? AND group_id=?''',(pipeline_id,account_id,group_id)).fetchone()
    if not member or member[0]!='joined':
        return 'NOT_JOINED'
    pair = conn.execute('''SELECT * FROM group_accounts WHERE pipeline_id=? AND account_id=?
        AND group_id=? AND status='active' ''',(pipeline_id,account_id,group_id)).fetchone()
    screen = conn.execute('''SELECT * FROM group_screening_results WHERE pipeline_id=?
        AND account_id=? AND group_id=?''',(pipeline_id,account_id,group_id)).fetchone()
    if not pair or not screen:
        return 'CHECK_PENDING'
    if conn.execute("SELECT 1 FROM screening_queue WHERE pipeline_id=? AND group_id=? AND account_id=? AND state IN ('queued','running')",(pipeline_id,group_id,account_id)).fetchone():
        return 'CHECK_PENDING'
    screen = dict(screen)
    try:
        evidence = json.loads(screen.get('evidence_json') or '{}')
    except (ValueError,TypeError):
        return 'UNKNOWN'
    if not isinstance(evidence,dict):
        return 'UNKNOWN'
    if screen.get('moderation_status') in ('restricted','restricted_confirmed','rejected') or evidence.get('check_status')=='restricted':
        return 'RESTRICTED'
    if evidence.get('accessible') is False:
        return 'ACCESS_ERROR'
    if not fresh(screen.get('last_checked_at'),moment) or evidence.get('accessible') is not True or evidence.get('can_post') is not True:
        return 'CHECK_PENDING'
    if pair['posting_mode'] != settings['posting_mode']:
        return 'IDENTITY_UNAVAILABLE'
    if settings['posting_mode']=='anonymous' and evidence.get('supports_anonymous') is not True:
        return 'IDENTITY_UNAVAILABLE'
    if settings['posting_mode']=='nickname' and (not pair['nickname'] or pair['nickname_status']!='confirmed'
            or pair['facebook_nickname']!=pair['nickname'] or evidence.get('nickname_available') is not True):
        return 'IDENTITY_UNAVAILABLE'
    return 'ACTIVE'


def choose(conn, pipeline_id, group_id, settings, moment, rng=None, scheduled_for=None):
    rng = rng or random.SystemRandom()
    owner = repo.owner(conn,pipeline_id)
    candidates = []
    for row in conn.execute('SELECT * FROM accounts WHERE pipeline_id=? ORDER BY id',(pipeline_id,)):
        account = dict(row)
        if settings['account_ids'] and account['id'] not in settings['account_ids']:
            continue
        if account.get('owner_user_id') not in (None,owner):
            continue
        if pair_status(conn,pipeline_id,account['id'],group_id,settings,moment)!='ACTIVE':
            continue
        try:
            slot = constraints.slot_for(conn,pipeline_id,account['id'],group_id,settings,scheduled_for or moment)
        except (ValueError,TypeError):
            continue
        limit = constraints.effective(conn,pipeline_id,account['id'],settings)['account']
        used = constraints.used(conn,pipeline_id,slot,settings,account_id=account['id'])
        if constraints.has_capacity(conn,pipeline_id,account['id'],group_id,settings,slot):
            candidates.append((used / max(1,limit),account['id'],slot))
    if not candidates:
        return None
    best = min(score for score,_,_ in candidates)
    _,aid,slot = rng.choice([entry for entry in candidates if entry[0]==best])
    return {'account_id':aid,'scheduled_for':slot}


def destination_busy(conn, pipeline_id, group_id, exclude_cycle=None):
    return bool(conn.execute("""SELECT 1 FROM auto_cycle_targets WHERE pipeline_id=? AND group_id=?
        AND cycle_id!=? AND status IN ('preparing','assigned','pending','approved','queued','running',
        'submitted_unverified','pending_approval','needs_review') LIMIT 1""",(pipeline_id,group_id,exclude_cycle or -1)).fetchone()
        or conn.execute("""SELECT 1 FROM auto_post_jobs WHERE pipeline_id=? AND group_id=?
        AND status IN ('pending','approved','queued','running','submitted_unverified','pending_approval') LIMIT 1""",(pipeline_id,group_id)).fetchone()
        or conn.execute("""SELECT 1 FROM jobs WHERE pipeline_id=? AND group_id=?
        AND state IN ('queued','running','submitted_unverified','pending_approval') LIMIT 1""",(pipeline_id,group_id)).fetchone())
