"""Warm existing read-only Group probes before eligibility evidence expires."""
import json
import logging
from services.auto_posting_cycles import assignment, repository as repo

LOG = logging.getLogger('facebook-pipeline.cycles')


def recover_probes(conn, stamp):
    if repo.available(conn):
        conn.execute("""UPDATE screening_queue SET state=CASE WHEN attempts<3 THEN 'queued' ELSE 'failed' END,
            attempts=attempts+1,last_error='Read-only probe interrupted',updated_at=? WHERE state='running'
            AND pipeline_id IN (SELECT pipeline_id FROM auto_cycle_configs)""",(stamp,))


def enqueue_due(db, moment, enqueue, budget=20):
    with db() as conn:
        if not repo.available(conn):
            return
        conn.execute('BEGIN IMMEDIATE')
        for config in conn.execute('SELECT * FROM auto_cycle_configs').fetchall():
            try:
                settings=json.loads(config['settings_json']);pid=config['pipeline_id']
                if not settings['enabled'] or not conn.execute('SELECT 1 FROM pipelines WHERE id=? AND enabled=1',(pid,)).fetchone():
                    continue
                owner=repo.owner(conn,pid)
                accounts=[dict(a) for a in conn.execute("SELECT * FROM accounts WHERE pipeline_id=? AND enabled=1 AND session_status='connected'",(pid,))
                    if a['owner_user_id'] in (None,owner) and a['fb_uid_fingerprint'] and (not settings['account_ids'] or a['id'] in settings['account_ids'])]
                for group in conn.execute('SELECT * FROM groups WHERE pipeline_id=? AND enabled=1 AND approved_target=1 ORDER BY id',(pid,)).fetchall():
                    gid=group['id']
                    if (settings['group_ids'] and gid not in settings['group_ids']) or repo.global_state(conn,pid,gid)=='DISABLED':
                        continue
                    if conn.execute("SELECT 1 FROM screening_queue WHERE pipeline_id=? AND group_id=? AND state IN ('queued','running')",(pid,gid)).fetchone():
                        continue
                    pairs=[]
                    for account in accounts:
                        pair=conn.execute('''SELECT r.last_checked_at FROM group_accounts ga
                            JOIN group_memberships gm ON gm.pipeline_id=ga.pipeline_id AND gm.account_id=ga.account_id AND gm.group_id=ga.group_id
                            LEFT JOIN group_screening_results r ON r.pipeline_id=ga.pipeline_id AND r.account_id=ga.account_id AND r.group_id=ga.group_id
                            WHERE ga.pipeline_id=? AND ga.account_id=? AND ga.group_id=? AND ga.status='active' AND gm.membership_status='joined' ''',
                            (pid,account['id'],gid)).fetchone()
                        if pair:
                            pairs.append((account,pair[0]))
                    if any(assignment.fresh(a['session_checked_at'],moment,hours=22) and assignment.fresh(checked,moment,hours=22) for a,checked in pairs):
                        continue
                    loads={a['id']:conn.execute("SELECT COUNT(*) FROM screening_queue WHERE account_id=? AND state IN ('queued','running')",(a['id'],)).fetchone()[0] for a,_ in pairs}
                    for account,_ in sorted(pairs,key=lambda pair:(loads[pair[0]['id']],pair[0]['id'])):
                        if budget<=0:
                            return
                        if enqueue(conn,pid,account['id'],gid,False):
                            budget-=1
                            break
            except (ValueError,TypeError,KeyError,repo.CycleError) as error:
                LOG.warning('cycle_refresh_refused pipeline=%s type=%s',config['pipeline_id'],type(error).__name__)
