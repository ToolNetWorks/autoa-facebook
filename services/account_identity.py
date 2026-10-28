"""Reserve verified Facebook identities without creating duplicate profiles."""
import sqlite3


class DuplicateFacebookIdentity(ValueError):
    pass


def claim(conn, account_id, pipeline_id, fingerprint):
    row = conn.execute('SELECT fb_uid_fingerprint FROM accounts WHERE id=? AND pipeline_id=?',(account_id,pipeline_id)).fetchone()
    if row is None or not fingerprint:
        raise ValueError('Account identity unavailable')
    if row[0] and row[0]!=fingerprint:
        raise DuplicateFacebookIdentity()
    if conn.execute('SELECT 1 FROM accounts WHERE fb_uid_fingerprint=? AND id!=?',(fingerprint,account_id)).fetchone():
        raise DuplicateFacebookIdentity()
    try:
        conn.execute('UPDATE accounts SET fb_uid_fingerprint=? WHERE id=? AND pipeline_id=?',(fingerprint,account_id,pipeline_id))
    except sqlite3.IntegrityError as error:
        raise DuplicateFacebookIdentity() from error
