"""V20: active membership bijection. Explicit maintenance only; never auto-repair."""
from datetime import datetime, timezone
from services.pipeline_membership_policy import conflicts, MembershipConflict

VERSION = 20
INDEXES = {'uq_active_pipeline_ctv': 'pipeline_id', 'uq_active_ctv_pipeline': 'user_id'}


def plan(conn) -> dict:
    found = conflicts(conn)
    present = {row[1]: row for row in conn.execute('PRAGMA index_list(pipeline_memberships)')}
    for name,column in INDEXES.items():
        if name not in present:
            continue
        fields=[row[2] for row in conn.execute(f'PRAGMA index_info({name})')]
        sql=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()[0]
        predicate=''.join(sql.lower().split()).split('where',1)[-1]
        if not present[name][2] or not present[name][4] or fields != [column] or predicate != "status='active'":
            raise ValueError('Incompatible existing membership index; manual review required')
    return {'version': VERSION, 'conflicts': found, 'compatible': not any(found.values()),
            'missing_indexes': [name for name in INDEXES if name not in present]}


def apply(conn) -> None:
    if not conn.in_transaction:
        raise ValueError('Explicit maintenance transaction and verified backup required')
    change = plan(conn)
    if not change['compatible']:
        raise MembershipConflict()
    for name, column in INDEXES.items():
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON pipeline_memberships({column}) WHERE status='active'")
    conn.execute('''CREATE TABLE IF NOT EXISTS fpm_schema_migrations
        (version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)''')
    conn.execute('INSERT OR IGNORE INTO fpm_schema_migrations VALUES(?,?,?)',
                 (VERSION, 'unique_active_membership', datetime.now(timezone.utc).isoformat()))
