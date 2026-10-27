"""Regression tests for observed access, session, queue and nickname blockers."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_app import client  # noqa: F401
import app as m
from migrations.v019_group_nicknames import apply, plan
from services import account_session_state, group_job_lifecycle, nickname_repository
from services.group_links import InvalidGroupLink, normalize_group_url
from services.group_access import verified_redirect
from services.posting_preflight import same_group
from services.posting_mode import enforce_posting_identity
from test_group_worker_contract import due_job, approved_album_job, idle_background_workers  # noqa: F401


def test_share_namespace_and_unicode_roundtrip():
    url, key = normalize_group_url('https://m.facebook.com/share/g/19XXY4GjsM/?ref=test')
    assert url == 'https://www.facebook.com/share/g/19XXY4GjsM/'
    assert key == 'share:19XXY4GjsM'
    assert key != normalize_group_url('19XXY4GjsM')[1]
    raw = 'https://www.facebook.com/groups/cà-phê/'
    encoded, key = normalize_group_url(raw)
    assert '%C3%A0' in encoded
    assert same_group(encoded, raw)
    assert normalize_group_url(encoded)[1] == key


@pytest.mark.parametrize('url', ['https://www.facebook.com/groups/a%2Fb/',
    'https://www.facebook.com/groups/%2E%2E/', 'https://user:secret@facebook.com/groups/1/',
    'https://facebook.com:wrong/groups/1/', 'https://fb.watch/groups/1/',
    'https://facebook.com.evil.test/groups/1/', 'https://facebook.com/groups/a%0Ab/'])
def test_unsafe_group_references_are_rejected(url):
    with pytest.raises(InvalidGroupLink):
        normalize_group_url(url)


def test_redirect_requires_observed_facebook_chain():
    expected = 'https://www.facebook.com/share/g/CODE/'
    final = 'https://www.facebook.com/groups/123/'
    request = SimpleNamespace(url=final, redirected_from=SimpleNamespace(url=expected, redirected_from=None))
    assert verified_redirect(expected, final, SimpleNamespace(request=request)) == final
    assert verified_redirect(expected, final, None) == ''
    assert verified_redirect('https://www.facebook.com/share/g/OTHER/', final, SimpleNamespace(request=request)) == ''
    request.redirected_from.url = 'https://evil.test/share/g/CODE/'
    assert verified_redirect(expected, final, SimpleNamespace(request=request)) == ''


def test_stale_connected_is_not_presented_as_current():
    now = datetime(2026,10,27,tzinfo=timezone.utc)
    assert account_session_state.presentation({'session_status':'connected','session_checked_at':(now-timedelta(days=3)).isoformat()}, moment=now)['session_display_status']=='stale'
    assert account_session_state.presentation({'session_status':'connected','session_checked_at':now.isoformat()}, moment=now)['session_display_status']=='connected'
    assert account_session_state.presentation({'session_status':'checkpoint'}, moment=now)['session_display_status']=='checkpoint'


def test_session_persistence_is_scoped_and_technical_error_does_not_refresh(client):
    _, pipeline, account, _ = due_job(client)
    stamp = m.iso()
    with m.db() as conn:
        account_session_state.record_check(conn,pipeline,account,{'status':'checkpoint','details':'Manual verification'},stamp)
        account_session_state.record_check(conn,pipeline,account,{'status':'error'},'2099-01-01T00:00:00+00:00')
        row=conn.execute('SELECT session_status,session_checked_at FROM accounts WHERE id=?',(account,)).fetchone()
        assert tuple(row)==('checkpoint',stamp)
        with pytest.raises(ValueError):
            account_session_state.record_check(conn,pipeline+1,account,{'status':'expired'},stamp)


def test_check_account_does_not_accept_a_different_uid(client, monkeypatch):
    _,pipeline,account,_=due_job(client)
    with m.db() as conn:
        conn.execute("UPDATE accounts SET fb_uid_fingerprint='different-account' WHERE id=?",(account,))
    def fake_check(args,**kwargs):
        Path(args[-1]).write_text(json.dumps({'status':'connected','uid':'123'}))
        return SimpleNamespace(returncode=0,stderr='')
    monkeypatch.setattr(m.subprocess,'run',fake_check)
    response=client.post(f'/api/accounts/{account}/check')
    assert response.status_code==409
    with m.db() as conn:
        assert conn.execute('SELECT session_checked_at FROM accounts WHERE id=?',(account,)).fetchone()[0] is None


def test_queued_missing_post_is_diagnosed_without_creating_jobs(client):
    auto,pipeline=approved_album_job(client)
    with m.db() as conn:
        conn.execute("UPDATE auto_post_jobs SET status='queued' WHERE id=?",(auto,))
        count=conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        assert group_job_lifecycle.audit_queue(conn,pipeline)==[{'auto_job_id':auto,'pipeline_id':pipeline,'code':'AUTO_JOB_MISSING_POST'}]
        assert group_job_lifecycle.audit_queue(conn,pipeline+1)==[]
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==count


def test_prebrowser_failure_updates_linked_auto_job(client):
    job,pipeline,account,group=due_job(client,expired=True)
    with m.db() as conn:
        post=conn.execute('SELECT post_id FROM jobs WHERE id=?',(job,)).fetchone()[0]
        auto=conn.execute('''INSERT INTO auto_post_jobs(pipeline_id,account_id,group_id,status,idempotency_key,created_at,updated_at,post_id)
                         VALUES(?,?,?,'queued','block-test',?,?,?)''',(pipeline,account,group,m.iso(),m.iso(),post)).lastrowid
    assert m.claim_next_job() is None
    with m.db() as conn:
        row=conn.execute('SELECT status,defer_reason FROM auto_post_jobs WHERE id=?',(auto,)).fetchone()
        assert row['status']=='needs_review' and row['defer_reason']


def test_payload_contains_post_id_and_refuses_other_pipeline_media(client):
    job,_,_,_=due_job(client)
    claimed=m.claim_next_job()
    payload=group_job_lifecycle.worker_payload(claimed,m.DATA)
    assert payload['post_id']==claimed['post_id']
    claimed['media_json']=json.dumps(['../private-session.json'])
    with pytest.raises(ValueError):group_job_lifecycle.worker_payload(claimed,m.DATA)


def test_nickname_migration_is_additive_idempotent_and_does_not_invent_confirmation(client):
    _,pipeline,account,group=due_job(client)
    with m.db() as conn:
        conn.execute("UPDATE group_accounts SET nickname='Configured only' WHERE account_id=? AND group_id=?",(account,group))
    with m.db() as conn:
        conn.execute('BEGIN IMMEDIATE');apply(conn)
        assert plan(conn)['missing_columns']==[]
        row=conn.execute('SELECT nickname,nickname_status,facebook_nickname FROM group_accounts WHERE account_id=? AND group_id=?',(account,group)).fetchone()
        assert row['nickname']=='Configured only' and row['nickname_status'] in ('','unverified') and row['facebook_nickname']==''
        nickname_repository.save(conn,pipeline,account,group,'Confirmed42')
        saved=conn.execute('SELECT confirmed_at FROM group_accounts WHERE account_id=? AND group_id=?',(account,group)).fetchone()[0]
    with m.db() as conn:
        conn.execute('BEGIN IMMEDIATE');apply(conn)
        assert nickname_repository.confirmed(conn,pipeline,account,group)=='Confirmed42'
        assert nickname_repository.confirmed(conn,pipeline+1,account,group) is None
        assert conn.execute('SELECT confirmed_at FROM group_accounts WHERE account_id=? AND group_id=?',(account,group)).fetchone()[0]==saved
        with pytest.raises(ValueError):nickname_repository.save(conn,pipeline+1,account,group,'WrongOwner')


def test_nickname_unapplied_schema_is_controlled_review(tmp_path,monkeypatch):
    legacy=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(legacy) as conn:
        conn.execute('CREATE TABLE group_accounts(id INTEGER PRIMARY KEY,nickname TEXT)')
    monkeypatch.setattr(m,'DB_PATH',legacy)
    outcome=asyncio.run(enforce_posting_identity(None,1,1,1,'nickname','Saved'))
    assert outcome[1]=='not_verified' and 'cập nhật' in outcome[2]


def test_migration_rollback_has_no_partial_columns():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE group_accounts(id INTEGER PRIMARY KEY,nickname TEXT)')
    c.commit();c.execute('BEGIN IMMEDIATE');apply(c);c.rollback()
    assert 'facebook_nickname' not in {r[1] for r in c.execute('PRAGMA table_info(group_accounts)')}
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='fpm_schema_migrations'").fetchone() is None


@pytest.mark.parametrize('command,handler,async_handler', [
    ('check-composer','_cli_check_composer',True), ('check-identity','_cli_check_identity',True),
    ('dry-run','_cli_dry_run',False)])
def test_diagnostic_commands_reach_their_handler(tmp_path,monkeypatch,command,handler,async_handler):
    import fb_browser
    from unittest.mock import AsyncMock, Mock
    source=tmp_path/'request.json';source.write_text('{}')
    target=tmp_path/'result.json'
    stub=AsyncMock(return_value={'status':'ok'}) if async_handler else Mock(return_value={'status':'ok'})
    monkeypatch.setattr(fb_browser,handler,stub)
    monkeypatch.setattr(fb_browser.sys,'argv',['fb_browser.py',command,str(source),str(target)])
    fb_browser.main()
    assert json.loads(target.read_text())['status']=='ok'
    stub.assert_called_once()


def test_dry_run_reports_validation_scope_and_exactly_one_image():
    from fb_browser import _cli_dry_run
    result=_cli_dry_run({'account_id':1,'group_url':'https://facebook.com/groups/test/','body':'Draft','media':['a','b']})
    assert result['scope']=='payload_validation_only'
    assert result['publish_attempted'] is False
    assert result['status']=='invalid'


def test_diagnostic_url_omits_credentials_and_query():
    from services.group_access import public_url
    assert public_url('https://user:private@facebook.com/groups/test/?token=private')=='https://facebook.com/groups/test/'
