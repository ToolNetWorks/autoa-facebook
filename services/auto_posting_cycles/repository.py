"""Cycle persistence and tenant checks; no network or implicit migrations."""
import json
from datetime import datetime, timezone


class CycleError(ValueError):
    def __init__(self, code, message='Chưa thể chuẩn bị lịch đăng. Vui lòng kiểm tra cấu hình.'):
        self.code, self.message = code, message
        super().__init__(code)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def available(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_cycles'").fetchone())


def owned(conn, table, resource_id, pipeline_id):
    if table not in {'accounts','groups','posts','media_assets','workspace_albums','caption_templates','auto_cycles'}:
        raise ValueError('Unsupported resource')
    row = conn.execute(f'SELECT * FROM {table} WHERE id=? AND pipeline_id=?',(resource_id,pipeline_id)).fetchone()
    if row is None:
        raise CycleError('RESOURCE_NOT_FOUND','Không tìm thấy dữ liệu trong Workspace.')
    return dict(row)


def owner(conn, pipeline_id):
    rows = conn.execute("""SELECT m.user_id FROM pipeline_memberships m JOIN users u ON u.id=m.user_id
        WHERE m.pipeline_id=? AND m.status='active' AND u.status='active' AND u.role='ctv'""",(pipeline_id,)).fetchall()
    if len(rows) != 1:
        raise CycleError('PIPELINE_MEMBERSHIP_CONFLICT','Workspace cần đúng một CTV đang hoạt động.')
    return rows[0][0]


def config(conn, pipeline_id):
    row = conn.execute('SELECT * FROM auto_cycle_configs WHERE pipeline_id=?',(pipeline_id,)).fetchone()
    return dict(row) if row else None


def validate_scope(conn, pipeline_id, settings):
    for key,table in [('account_ids','accounts'),('group_ids','groups'),('media_ids','media_assets'),('template_ids','caption_templates')]:
        for resource_id in settings.get(key,[]):
            owned(conn,table,resource_id,pipeline_id)
    if settings.get('album_id'):
        owned(conn,'workspace_albums',settings['album_id'],pipeline_id)
    if settings.get('fallback_template_id'):
        owned(conn,'caption_templates',settings['fallback_template_id'],pipeline_id)


def save_config(conn, pipeline_id, settings, actor_id, moment):
    owner(conn,pipeline_id)
    validate_scope(conn,pipeline_id,settings)
    conn.execute('''INSERT INTO auto_cycle_configs
        (pipeline_id,settings_json,revision,next_run_at,updated_by,updated_at) VALUES(?,?,1,?,?,?)
        ON CONFLICT(pipeline_id) DO UPDATE SET settings_json=excluded.settings_json,
        revision=revision+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at''',
        (pipeline_id,json.dumps(settings,ensure_ascii=False),moment,actor_id,moment))


def global_state(conn, pipeline_id, group_id):
    if not available(conn):
        return 'UNKNOWN'
    row = conn.execute('SELECT state FROM group_global_status WHERE pipeline_id=? AND group_id=?',(pipeline_id,group_id)).fetchone()
    return row[0] if row else 'UNKNOWN'


def set_global_state(conn, pipeline_id, group_id, state, evidence, actor_id):
    owned(conn,'groups',group_id,pipeline_id)
    previous = conn.execute('SELECT state,checked_at FROM group_global_status WHERE group_id=? AND pipeline_id=?',(group_id,pipeline_id)).fetchone()
    if state!='DISABLED' and previous and previous['state']=='DISABLED':
        if state!='ACTIVE':
            raise CycleError('GROUP_RECHECK_REQUIRED','Cần kiểm tra lại quyền đăng bằng Facebook trước khi bật Group.')
        verified = False
        for row in conn.execute('SELECT evidence_json,last_checked_at FROM group_screening_results WHERE pipeline_id=? AND group_id=?',(pipeline_id,group_id)):
            try:
                observed = json.loads(row['evidence_json'])
                verified = verified or (row['last_checked_at']>previous['checked_at'] and observed.get('accessible') is True and observed.get('can_post') is True)
            except (TypeError,ValueError):
                continue
        if not verified:
            raise CycleError('GROUP_RECHECK_REQUIRED','Cần kiểm tra lại quyền đăng bằng Facebook trước khi bật Group.')
    conn.execute('''INSERT INTO group_global_status
        (group_id,pipeline_id,state,reason_code,evidence,checked_at,confirmed_by) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(group_id) DO UPDATE SET state=excluded.state,reason_code=excluded.reason_code,
        evidence=excluded.evidence,checked_at=excluded.checked_at,confirmed_by=excluded.confirmed_by''',
        (group_id,pipeline_id,state,'ADMIN_CONFIRMED_'+state,evidence,now_iso(),actor_id))


def record_pair_observation(conn, pipeline_id, account_id, group_id, check):
    """Account access failures never set a Group's global disabled state."""
    if not check:
        return
    for table,resource_id in [('accounts',account_id),('groups',group_id)]:
        owned(conn,table,resource_id,pipeline_id)
    from services import group_membership
    observed = check.get('is_member')
    if observed is True:
        group_membership.record(conn,pipeline_id,account_id,group_id,group_membership.JOINED)
    elif observed is False and group_membership.status_of(conn,pipeline_id,account_id,group_id) != group_membership.PENDING_APPROVAL:
        group_membership.record(conn,pipeline_id,account_id,group_id,group_membership.NOT_MEMBER)
    diagnostic = check.get('access_diagnostics') or {}
    if (check.get('resolution_verified') is True and check.get('accessible') is True
            and diagnostic.get('resolution_verified') is True and diagnostic.get('code')=='ACCESSIBLE'):
        from services.group_links import normalize_group_url
        try:
            group = owned(conn,'groups',group_id,pipeline_id)
            original_key = normalize_group_url(group['url'])[1]
            requested_key = normalize_group_url(diagnostic['requested_url'])[1]
            url,key = normalize_group_url(check['resolved_url'])
            if original_key==requested_key and not key.startswith('share:') and not conn.execute(
                    'SELECT 1 FROM groups WHERE pipeline_id=? AND group_key=? AND id!=?',(pipeline_id,key,group_id)).fetchone():
                conn.execute('UPDATE groups SET url=?,group_key=? WHERE id=? AND pipeline_id=?',(url,key,group_id,pipeline_id))
        except (ValueError,KeyError):
            pass
