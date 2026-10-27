"""V19: additive nickname evidence; never infer confirmation from a configured name.

Caller owns backup, transaction and approval. This migration is NOT invoked by
application startup, so deploying source alone cannot change production schema.
"""
from datetime import datetime, timezone
import sqlite3

VERSION = 19
COLUMNS = {
    'facebook_nickname': "TEXT NOT NULL DEFAULT ''",
    'nickname_status': "TEXT NOT NULL DEFAULT 'unverified'",
    'confirmed_at': 'TEXT',
    'last_verified_at': 'TEXT',
}


def plan(conn: sqlite3.Connection) -> dict:
    existing = {r[1] for r in conn.execute('PRAGMA table_info(group_accounts)')}
    if not existing:
        raise ValueError('group_accounts does not exist')
    return {'version': VERSION, 'missing_columns': [k for k in COLUMNS if k not in existing],
            'configured_nicknames': conn.execute("SELECT COUNT(*) FROM group_accounts WHERE nickname<>''").fetchone()[0],
            'backfill_confirmed': False}


def apply(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise ValueError('Migration requires an explicit transaction')
    changes = plan(conn)
    for name in changes['missing_columns']:
        conn.execute(f'ALTER TABLE group_accounts ADD COLUMN {name} {COLUMNS[name]}')
    conn.execute('''CREATE TABLE IF NOT EXISTS fpm_schema_migrations
                    (version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)''')
    conn.execute('''INSERT OR IGNORE INTO fpm_schema_migrations(version,name,applied_at) VALUES(?,?,?)''',
                 (VERSION, 'group_nickname_evidence', datetime.now(timezone.utc).isoformat()))
