"""Enforce the active Pipeline/CTV bijection without choosing historical owners."""
from __future__ import annotations


class MembershipConflict(ValueError):
    code = 'PIPELINE_MEMBERSHIP_CONFLICT'
    message = 'Workspace có phân công CTV xung đột. Vui lòng liên hệ quản trị viên.'


def conflicts(conn) -> dict:
    return {
        'pipelines': [dict(row) for row in conn.execute("SELECT pipeline_id, COUNT(*) count FROM pipeline_memberships WHERE status='active' GROUP BY pipeline_id HAVING COUNT(*)>1")],
        'users': [dict(row) for row in conn.execute("SELECT user_id, COUNT(*) count FROM pipeline_memberships WHERE status='active' GROUP BY user_id HAVING COUNT(*)>1")],
    }


def require_available(conn, pipeline_id: int, user_id: int | None = None,
                      membership_id: int | None = None) -> None:
    """Call inside the same BEGIN IMMEDIATE transaction as the membership write."""
    found = conn.execute("""SELECT id FROM pipeline_memberships WHERE status='active'
        AND (pipeline_id=? OR user_id=?) AND id!=? LIMIT 1""",
        (pipeline_id, user_id, membership_id or -1)).fetchone()
    if found is not None:
        raise MembershipConflict()


def unique_active_membership(conn, user_id: int):
    rows = conn.execute("SELECT * FROM pipeline_memberships WHERE user_id=? AND status='active'", (user_id,)).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise MembershipConflict()
    row = rows[0]
    require_available(conn, row['pipeline_id'], user_id, row['id'])
    return row
