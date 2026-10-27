"""Tenant-scoped nickname evidence, compatible with an unapplied migration."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


class NicknameSchemaRequired(RuntimeError):
    pass


def require_schema(conn) -> None:
    columns = {row[1] for row in conn.execute('PRAGMA table_info(group_accounts)')}
    if not {'facebook_nickname', 'nickname_status', 'confirmed_at', 'last_verified_at'}.issubset(columns):
        raise NicknameSchemaRequired('Nickname evidence schema v19 is required')


def confirmed(conn, pipeline_id: int, account_id: int, group_id: int) -> str | None:
    require_schema(conn)
    row = conn.execute('''SELECT ga.facebook_nickname FROM group_accounts ga
        JOIN accounts a ON a.id=ga.account_id AND a.pipeline_id=ga.pipeline_id
        JOIN groups g ON g.id=ga.group_id AND g.pipeline_id=ga.pipeline_id
        WHERE ga.pipeline_id=? AND ga.account_id=? AND ga.group_id=?
        AND ga.nickname_status='confirmed' AND ga.status='active' ''',
        (pipeline_id, account_id, group_id)).fetchone()
    return row[0] if row and row[0] else None


def save(conn, pipeline_id: int, account_id: int, group_id: int, nickname: str,
         status: str = 'confirmed') -> None:
    require_schema(conn)
    if status not in ('pending', 'confirmed', 'failed', 'expired', 'unverified'):
        raise ValueError('Invalid nickname status')
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute('''UPDATE group_accounts SET facebook_nickname=?,nickname_status=?,
        confirmed_at=CASE WHEN ?='confirmed' THEN ? ELSE confirmed_at END,last_verified_at=?,updated_at=?
        WHERE pipeline_id=? AND account_id=? AND group_id=? AND status='active'
        AND EXISTS(SELECT 1 FROM accounts WHERE id=? AND pipeline_id=?)
        AND EXISTS(SELECT 1 FROM groups WHERE id=? AND pipeline_id=?)''',
        (nickname, status, status, now, now, now, pipeline_id, account_id, group_id,
         account_id, pipeline_id, group_id, pipeline_id))
    if cursor.rowcount != 1:
        raise ValueError('Nickname ownership mismatch')


def update_status(conn, pipeline_id: int, account_id: int, group_id: int, status: str) -> None:
    require_schema(conn)
    if status not in ('pending', 'confirmed', 'failed', 'expired', 'unverified'):
        raise ValueError('Invalid nickname status')
    if status == 'confirmed':
        raise ValueError('Confirmation requires an observed nickname')
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('''UPDATE group_accounts SET nickname_status=?,last_verified_at=?,updated_at=?
                    WHERE pipeline_id=? AND account_id=? AND group_id=?''',
                 (status, now, now, pipeline_id, account_id, group_id))
