import concurrent.futures
import sqlite3
import pytest
from test_app import client
from test_multitenant import make_tenant, CTV_PASSWORD, login_ctv
from test_group_worker_contract import idle_background_workers
import app as m
from migrations import v020_unique_active_membership as migration
from services.pipeline_membership_policy import MembershipConflict


def create(client, pipeline, username, status='active'):
    return client.post('/api/admin/users', json={'username':username,'password':CTV_PASSWORD,
                        'pipeline_id':pipeline,'status':status})


def test_create_reactivate_and_move_reject_occupied_pipeline(client):
    p, key, member = make_tenant(client, '@one', 'first')
    assert create(client,p['id'],'second').status_code == 409
    suspended=create(client,p['id'],'inactive','suspended')
    assert suspended.status_code == 201, suspended.text
    assert client.patch(f"/api/admin/users/{suspended.json()['id']}",json={'status':'active'}).status_code == 409
    p2, _, other = make_tenant(client,'@two','other')
    assert client.patch(f"/api/admin/users/{other['id']}",json={'pipeline_id':p['id']}).status_code == 409
    ctv,response=login_ctv('first',key=key)
    assert response.status_code==200
    assert ctv.get(f"/api/pipelines/{p2['id']}/groups").status_code in (403,404)


def test_concurrent_creation_only_one_owner(client):
    p=client.post('/api/admin/pipelines',json={'name':'@race'}).json()['id']
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        codes=list(pool.map(lambda name:create(client,p,name).status_code,['racer1','racer2']))
    assert sorted(codes)==[201,409]
    with m.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pipeline_memberships WHERE pipeline_id=? AND status='active'",(p,)).fetchone()[0]==1


def test_legacy_conflict_fails_closed_without_mutating_users(client):
    p,key,first=make_tenant(client,'@legacy','owner')
    second=create(client,p['id'],'legacy-other','suspended').json()['id']
    ctv,response=login_ctv('owner',key=key)
    assert response.status_code==200
    with m.db() as conn:
        conn.execute("UPDATE pipeline_memberships SET status='active' WHERE user_id=?",(second,))
        conn.execute("UPDATE users SET status='active' WHERE id=?",(second,))
    assert ctv.get('/api/auth/me').status_code==401
    _,response=login_ctv('owner',key=key)
    assert response.status_code==403
    with m.db() as conn:
        assert not migration.plan(conn)['compatible']
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(MembershipConflict): migration.apply(conn)
        assert conn.execute("SELECT COUNT(*) FROM pipeline_memberships WHERE pipeline_id=? AND status='active'",(p['id'],)).fetchone()[0]==2


def test_migration_idempotent_constraints_and_rollback(client):
    p,_,first=make_tenant(client,'@migration','first')
    second=create(client,p['id'],'inactive','suspended').json()['id']
    with m.db() as conn:
        conn.execute('BEGIN IMMEDIATE'); migration.apply(conn); migration.apply(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE pipeline_memberships SET status='active' WHERE user_id=?",(second,))
        conn.rollback()
        assert 'uq_active_pipeline_ctv' in migration.plan(conn)['missing_indexes']
        conn.execute('BEGIN IMMEDIATE'); migration.apply(conn)
    with m.db() as conn:
        assert migration.plan(conn)['missing_indexes']==[]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE pipeline_memberships SET status='active' WHERE user_id=?",(second,))


def test_migration_rejects_same_name_nonunique_index(client):
    with m.db() as conn:
        conn.execute('CREATE INDEX uq_active_pipeline_ctv ON pipeline_memberships(pipeline_id)')
        with pytest.raises(ValueError): migration.plan(conn)


def test_database_rejects_one_ctv_in_two_active_pipelines(client):
    p,_,member=make_tenant(client,'@primary','single')
    other=client.post('/api/admin/pipelines',json={'name':'@secondary'}).json()['id']
    with m.db() as conn:
        conn.execute('BEGIN IMMEDIATE');migration.apply(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO pipeline_memberships(user_id,pipeline_id,status,created_at,updated_at) VALUES(?,?,'active',?,?)",(member['id'],other,m.iso(),m.iso()))
