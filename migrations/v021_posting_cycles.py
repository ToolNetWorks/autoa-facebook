"""Additive recurring-campaign storage. Explicit migration, never startup repair."""
from datetime import datetime, timezone
from migrations import v019_group_nicknames, v020_unique_active_membership

VERSION = 21


def incompatible_indexes(conn):
    expected = {f'uq_{table}_id_pipeline':(table,['id','pipeline_id'],'')
                for table in ('accounts','groups','media_assets','auto_post_jobs','jobs','posts')}
    expected.update({'uq_facebook_identity':('accounts',['fb_uid_fingerprint'],"coalesce(fb_uid_fingerprint,'')<>''"),
                     'uq_workspace_group_key':('groups',['pipeline_id','group_key'],"group_key<>''")})
    invalid = []
    for name,(table,columns,predicate) in expected.items():
        row=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
        if not row:
            continue
        fields=[r[2] for r in conn.execute(f'PRAGMA index_info({name})')]
        info=next((r for r in conn.execute(f'PRAGMA index_list({table})') if r[1]==name),None)
        compact=''.join(row[0].lower().split())
        actual=compact.split('where',1)[1] if 'where' in compact else ''
        if not info or not info[2] or fields!=columns or actual!=predicate:
            invalid.append(name)
    return invalid


def plan(conn) -> dict:
    duplicates = conn.execute("""SELECT COUNT(*) FROM (
        SELECT fb_uid_fingerprint FROM accounts WHERE COALESCE(fb_uid_fingerprint,'')<>''
        GROUP BY fb_uid_fingerprint HAVING COUNT(*)>1)""").fetchone()[0]
    conflicts = conn.execute("""SELECT COUNT(*) FROM (SELECT pipeline_id FROM pipeline_memberships
        WHERE status='active' GROUP BY pipeline_id HAVING COUNT(*)>1)""").fetchone()[0]
    v20 = v020_unique_active_membership.plan(conn)
    missing_nickname_columns = v019_group_nicknames.plan(conn)['missing_columns']
    missing_fingerprints = conn.execute("SELECT COUNT(*) FROM accounts WHERE COALESCE(fb_uid_enc,'')<>'' AND COALESCE(fb_uid_fingerprint,'')=''").fetchone()[0]
    duplicate_groups = conn.execute("SELECT COUNT(*) FROM (SELECT pipeline_id,group_key FROM groups WHERE group_key<>'' GROUP BY pipeline_id,group_key HAVING COUNT(*)>1)").fetchone()[0]
    invalid_indexes = incompatible_indexes(conn)
    return {'version':VERSION, 'duplicate_facebook_identities':duplicates,
            'membership_conflicts':conflicts,'v20_required':bool(v20['missing_indexes']),
            'nickname_columns_missing':missing_nickname_columns,'identity_backfill_required':missing_fingerprints,
            'duplicate_groups':duplicate_groups,
            'incompatible_indexes':invalid_indexes,
            'compatible':not any([duplicates,conflicts,v20['missing_indexes'],invalid_indexes,missing_nickname_columns,missing_fingerprints,duplicate_groups])}


def apply(conn) -> None:
    if not conn.in_transaction:
        raise ValueError('Explicit transaction and verified backup required')
    if not plan(conn)['compatible']:
        raise ValueError('Review legacy identity/membership conflicts before migration')
    for table in ('accounts','groups','media_assets','auto_post_jobs','jobs','posts'):
        conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_id_pipeline ON {table}(id,pipeline_id)')
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_facebook_identity
        ON accounts(fb_uid_fingerprint) WHERE COALESCE(fb_uid_fingerprint,'')<>''""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_group_key ON groups(pipeline_id,group_key) WHERE group_key<>''")
    statements = [
        '''CREATE TABLE IF NOT EXISTS workspace_albums (
            id INTEGER PRIMARY KEY,pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
            name TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(id,pipeline_id),UNIQUE(pipeline_id,name))''',
        '''CREATE TABLE IF NOT EXISTS workspace_album_media (
            pipeline_id INTEGER NOT NULL,album_id INTEGER NOT NULL,media_id INTEGER NOT NULL,
            PRIMARY KEY(album_id,media_id),
            FOREIGN KEY(album_id,pipeline_id) REFERENCES workspace_albums(id,pipeline_id),
            FOREIGN KEY(media_id,pipeline_id) REFERENCES media_assets(id,pipeline_id))''',
        '''CREATE TABLE IF NOT EXISTS caption_templates (
            id INTEGER PRIMARY KEY,pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
            name TEXT NOT NULL,body TEXT NOT NULL,approved INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1,created_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL,UNIQUE(id,pipeline_id))''',
        '''CREATE TABLE IF NOT EXISTS auto_cycle_configs (
            pipeline_id INTEGER PRIMARY KEY REFERENCES pipelines(id),settings_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,next_run_at TEXT NOT NULL,
            updated_by INTEGER REFERENCES users(id),updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS auto_cycles (
            id INTEGER PRIMARY KEY,pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
            cycle_key TEXT NOT NULL,due_at TEXT NOT NULL,settings_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',excluded_disabled INTEGER NOT NULL DEFAULT 0,
            excluded_groups_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,
            UNIQUE(pipeline_id,cycle_key),UNIQUE(id,pipeline_id))''',
        '''CREATE TABLE IF NOT EXISTS auto_cycle_targets (
            id INTEGER PRIMARY KEY,pipeline_id INTEGER NOT NULL,cycle_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,account_id INTEGER,media_id INTEGER,auto_job_id INTEGER,
            post_id INTEGER,job_id INTEGER,status TEXT NOT NULL DEFAULT 'planned',
            reason_code TEXT NOT NULL DEFAULT '',failure_level TEXT NOT NULL DEFAULT '',
            caption TEXT NOT NULL DEFAULT '',caption_key TEXT NOT NULL DEFAULT '',caption_source TEXT NOT NULL DEFAULT '',
            content_snapshot TEXT NOT NULL DEFAULT '{}',scheduled_for TEXT,
            lease_until TEXT,lease_token TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            UNIQUE(cycle_id,group_id),
            FOREIGN KEY(cycle_id,pipeline_id) REFERENCES auto_cycles(id,pipeline_id),
            FOREIGN KEY(group_id,pipeline_id) REFERENCES groups(id,pipeline_id),
            FOREIGN KEY(account_id,pipeline_id) REFERENCES accounts(id,pipeline_id),
            FOREIGN KEY(media_id,pipeline_id) REFERENCES media_assets(id,pipeline_id),
            FOREIGN KEY(auto_job_id,pipeline_id) REFERENCES auto_post_jobs(id,pipeline_id),
            FOREIGN KEY(post_id,pipeline_id) REFERENCES posts(id,pipeline_id),
            FOREIGN KEY(job_id,pipeline_id) REFERENCES jobs(id,pipeline_id))''',
        '''CREATE TABLE IF NOT EXISTS group_global_status (
            group_id INTEGER PRIMARY KEY,pipeline_id INTEGER NOT NULL,state TEXT NOT NULL
            CHECK(state IN ('ACTIVE','DISABLED','CHECK_PENDING','ACCESS_ERROR','UNKNOWN')),
            reason_code TEXT NOT NULL,evidence TEXT NOT NULL,checked_at TEXT NOT NULL,
            confirmed_by INTEGER REFERENCES users(id),
            FOREIGN KEY(group_id,pipeline_id) REFERENCES groups(id,pipeline_id))''',
    ]
    for statement in statements:
        conn.execute(statement)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cycle_targets_queue ON auto_cycle_targets(pipeline_id,status,id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cycle_targets_auto ON auto_cycle_targets(auto_job_id,pipeline_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cycle_targets_job ON auto_cycle_targets(job_id,pipeline_id)')
    conn.execute("""CREATE TABLE IF NOT EXISTS fpm_schema_migrations
        (version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)""")
    conn.execute('INSERT OR IGNORE INTO fpm_schema_migrations VALUES(?,?,?)',
        (VERSION,'multi_account_posting_cycles',datetime.now(timezone.utc).isoformat()))
