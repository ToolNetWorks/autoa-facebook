import sqlite3
import pytest
from test_app import client
from test_group_worker_contract import idle_background_workers
from migrations import v019_group_nicknames, v020_unique_active_membership, v021_posting_cycles
import app as m


def test_index_name_collision_is_rejected_without_data_changes(client):
    with m.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        v019_group_nicknames.apply(conn);v020_unique_active_membership.apply(conn)
        conn.execute('CREATE UNIQUE INDEX uq_facebook_identity ON accounts(id)')
        before=conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        assert v021_posting_cycles.plan(conn)['incompatible_indexes']==['uq_facebook_identity']
        with pytest.raises(ValueError):v021_posting_cycles.apply(conn)
        assert conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]==before
        conn.rollback()


def test_v21_is_additive_and_preserves_existing_rows(client):
    p=client.post('/api/pipelines',json={'name':'@migration-history'}).json()['id']
    a=client.post('/api/accounts',json={'pipeline_id':p,'label':'Existing account'}).json()['id']
    with m.db() as conn:
        before={table:[tuple(r) for r in conn.execute('SELECT * FROM '+table)] for table in ('users','accounts','pipelines','pipeline_memberships','jobs','posts','audit_logs')}
        conn.execute('BEGIN IMMEDIATE')
        v019_group_nicknames.apply(conn);v020_unique_active_membership.apply(conn);v021_posting_cycles.apply(conn)
        for table,rows in before.items():assert rows==[tuple(r) for r in conn.execute('SELECT * FROM '+table)]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert v021_posting_cycles.plan(conn)['compatible']
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO workspace_album_media(pipeline_id,album_id,media_id) VALUES(?,?,?)',(p,9999,9999))
