"""Tenant-scoped persistence and truthful presentation of verified sessions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

class SessionIdentityMismatch(ValueError):
    pass


VERIFIED = {'connected', 'expired', 'checkpoint', 'restricted'}


def presentation(account: dict, *, moment: datetime | None = None) -> dict:
    moment = moment or datetime.now(timezone.utc)
    status = account.get('session_status') or 'unknown'
    if status != 'connected':
        return {'session_display_status': status}
    try:
        checked = datetime.fromisoformat(account.get('session_checked_at') or '').astimezone(timezone.utc)
        stale = moment - checked > timedelta(hours=24) or checked > moment
    except ValueError:
        stale = True
    return {'session_display_status': 'stale' if stale else 'connected'}


def record_check(conn, pipeline_id: int, account_id: int, outcome: dict, checked_at: str,
                 *, encrypt_uid=None, fingerprint_uid=None) -> None:
    """Technical errors cannot replace the last verification or refresh its time."""
    row = conn.execute('SELECT * FROM accounts WHERE id=? AND pipeline_id=?',
                       (account_id, pipeline_id)).fetchone()
    if row is None:
        raise ValueError('Account ownership mismatch')
    status = outcome.get('status')
    if status not in VERIFIED:
        return
    uid = str(outcome.get('uid') or '')
    if status == 'connected' and (not uid.isdigit() or encrypt_uid is None):
        raise ValueError('Connected session requires verified identity')
    if status == 'connected' and row['fb_uid_fingerprint']:
        import hmac
        if fingerprint_uid is None or not hmac.compare_digest(fingerprint_uid(uid), row['fb_uid_fingerprint']):
            raise SessionIdentityMismatch('Browser identity does not match account')
    if status == 'connected' and fingerprint_uid is not None:
        from services.account_identity import claim, DuplicateFacebookIdentity
        try:
            claim(conn,account_id,pipeline_id,fingerprint_uid(uid))
        except DuplicateFacebookIdentity as error:
            raise SessionIdentityMismatch('Facebook identity already registered') from error
    conn.execute('''UPDATE accounts SET session_status=?,session_checked_at=?,session_details=?,updated_at=?,
                     fb_name=?,fb_avatar=?,fb_uid_enc=? WHERE id=? AND pipeline_id=?''',
                 (status, checked_at, str(outcome.get('details') or '')[:500], checked_at,
                  str(outcome.get('name') or row['fb_name'])[:120],
                  str(outcome.get('avatar') or row['fb_avatar'])[:2000],
                  encrypt_uid(uid) if status == 'connected' else row['fb_uid_enc'], account_id, pipeline_id))


def record_worker_session(conn, pipeline_id: int, account_id: int, result: dict, checked_at: str) -> None:
    status = result.get('session_status')
    if status in ('expired', 'checkpoint', 'restricted'):
        record_check(conn, pipeline_id, account_id,
                     {'status': status, 'details': result.get('details', '')}, checked_at)


def record_probe(conn, pipeline_id, account_id, observation, checked_at):
    """Refresh an existing identity only from the private browser probe result."""
    if observation is None:
        return True  # Compatibility with older probe workers; never refresh time.
    status = observation.get('status')
    if status in ('expired','checkpoint','restricted'):
        record_worker_session(conn,pipeline_id,account_id,{'session_status':status},checked_at)
        return True
    if status!='connected':
        return False
    row = conn.execute('SELECT fb_uid_fingerprint FROM accounts WHERE id=? AND pipeline_id=?',(account_id,pipeline_id)).fetchone()
    import hmac
    if not row or not row[0] or not hmac.compare_digest(row[0],str(observation.get('fingerprint') or '')):
        if row:
            conn.execute("UPDATE accounts SET session_status='unknown',session_details=? WHERE id=? AND pipeline_id=?",
                ('Danh tính browser profile không khớp; cần kiểm tra lại tài khoản.',account_id,pipeline_id))
        return False
    conn.execute("UPDATE accounts SET session_status='connected',session_checked_at=?,session_details=?,updated_at=? WHERE id=? AND pipeline_id=?",
        (checked_at,'Phiên và danh tính Facebook đã được kiểm tra qua Group.',checked_at,account_id,pipeline_id))
    return True
