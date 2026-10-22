"""Hạn mức bài mỗi ngày tính theo múi giờ của Workspace.

Những bài kiểm thử này bảo vệ khỏi lỗi quanh nửa đêm: một bài đăng lúc
23:59 và một bài lúc 00:01 phải thuộc hai ngày khác nhau theo giờ Việt Nam, và
một ngày có 23 hoặc 25 giờ (giờ tiết kiệm ban ngày) vẫn phải được tính đúng.
"""
import pytest
from test_app import AuthClient, client  # noqa: F401 (fixture)

import app as m
from scheduler import account_limits, time_windows


def make_pipeline(admin, name, timezone="Asia/Ho_Chi_Minh"):
    pipeline = admin.post('/api/pipelines', json={'name': name}).json()['id']
    account = admin.post('/api/accounts', json={'pipeline_id': pipeline, 'label': 'Facebook'}).json()['id']
    with m.db() as conn:
        conn.execute("UPDATE accounts SET session_status='connected' WHERE id=?", (account,))
        conn.execute("""INSERT INTO pipeline_auto_post_settings
            (pipeline_id,enabled,scope,account_ids_json,group_ids_json,daily_limit_per_account,
             min_gap_minutes,pipeline_daily_limit,topic,ai_caption,anti_duplicate,require_approval,
             apply_to_new_accounts,timezone,created_at,updated_at)
            VALUES(?,1,'all','[]','[]',3,0,0,'',0,1,1,1,?,?,?)""",
                     (pipeline, timezone, m.iso(), m.iso()))
        conn.commit()
    return pipeline, account


def add_job(conn, pipeline_id, account_id, group_id, when, state="posted"):
    cursor = conn.execute("""INSERT INTO posts(pipeline_id,body,media_json,created_at,anonymous_only)
                             VALUES(?,?,?,?,1)""", (pipeline_id, "x", "[]", when))
    post_id = cursor.lastrowid
    conn.execute("""INSERT INTO jobs(post_id,group_id,account_id,pipeline_id,state,details,
                     scheduled_at,updated_at,published_at,requested_posting_mode,posting_status,
                     verification_status)
                     VALUES(?,?,?,?,?,'',?,?,?,'anonymous','posted','verified')""",
                 (post_id, group_id, account_id, pipeline_id, state, when, when, when))
    return post_id


def test_the_day_bounds_follow_the_workspace_timezone():
    """23:59 giờ Việt Nam thuộc ngày hôm đó, 00:01 thuộc ngày hôm sau."""
    moment = time_windows.now_in("Asia/Ho_Chi_Minh").replace(
        hour=23, minute=59, second=0, microsecond=0)
    start, end = time_windows.day_bounds_utc(moment)
    start_at = time_windows.datetime.fromisoformat(start)
    end_at = time_windows.datetime.fromisoformat(end)
    assert start_at <= moment.astimezone(time_windows.timezone.utc) < end_at
    assert start.endswith("+00:00")
    assert time_windows.day_of(moment) == moment.strftime("%Y-%m-%d")

    later = moment + time_windows.timedelta(minutes=2)
    start2, end2 = time_windows.day_bounds_utc(later)
    assert start2 != start
    start2_at = time_windows.datetime.fromisoformat(start2)
    end2_at = time_windows.datetime.fromisoformat(end2)
    assert start2_at <= later.astimezone(time_windows.timezone.utc) < end2_at


def test_a_post_at_2359_and_a_post_at_0001_are_in_different_days(client):
    pipeline, account = make_pipeline(client, '@tz1')
    with m.db() as conn:
        conn.execute("INSERT INTO groups(pipeline_id,account_id,label,url,group_key,enabled,"
                     "approved_target,created_at) VALUES(?,?,'G','https://www.facebook.com/groups/1/',"
                     "'g1',1,1,?)", (pipeline, account, m.iso()))
        group_id = conn.execute("SELECT id FROM groups WHERE pipeline_id=?", (pipeline,)).fetchone()["id"]
        conn.commit()

    # 23:59 và 00:01 giờ Việt Nam của cùng một đêm.
    evening = time_windows.now_in("Asia/Ho_Chi_Minh").replace(
        hour=23, minute=58, second=0, microsecond=0)
    midnight = evening + time_windows.timedelta(minutes=3)
    with m.db() as conn:
        add_job(conn, pipeline, account, group_id, evening.isoformat(timespec="seconds"))
        conn.commit()
        state = account_limits.account_state(conn, account, {"timezone": "Asia/Ho_Chi_Minh"},
                                            evening)
    assert state["used"] == 1, "Bài lúc 23:59 phải tính vào ngày đó"

    with m.db() as conn:
        add_job(conn, pipeline, account, group_id, midnight.isoformat(timespec="seconds"))
        conn.commit()
        state = account_limits.account_state(conn, account, {"timezone": "Asia/Ho_Chi_Minh"},
                                            midnight)
    assert state["used"] == 1, "Bài lúc 00:01 thuộc ngày mới, không tính vào ngày cũ"


def test_a_day_with_a_dst_change_still_counts_correctly(client):
    """Một ngày 23 hoặc 25 giờ vẫn được tính đúng."""
    # Châu Âu: đêm cuối tháng 3 có 23 giờ, đêm cuối tháng 10 có 25 giờ.
    for name in ("Europe/Berlin", "Australia/Sydney"):
        moment = time_windows.now_in(name)
        start, end = time_windows.day_bounds_utc(moment)
        start_at = time_windows.datetime.fromisoformat(start)
        end_at = time_windows.datetime.fromisoformat(end)
        assert start_at <= moment.astimezone(time_windows.timezone.utc) < end_at, name
        # Ngày kế tiếp bắt đầu đúng lúc nửa đêm địa phương.
        nxt = moment + time_windows.timedelta(days=1)
        start2, _ = time_windows.day_bounds_utc(nxt)
        start2_at = time_windows.datetime.fromisoformat(start2)
        assert start2_at == nxt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            time_windows.timezone.utc)


def test_the_usage_of_a_restart_is_kept(client):
    """Worker khởi động lại không làm mất hạn mức của ngày đó."""
    pipeline, account = make_pipeline(client, '@tz2')
    with m.db() as conn:
        conn.execute("INSERT INTO groups(pipeline_id,account_id,label,url,group_key,enabled,"
                     "approved_target,created_at) VALUES(?,?,'G','https://www.facebook.com/groups/2/',"
                     "'g2',1,1,?)", (pipeline, account, m.iso()))
        group_id = conn.execute("SELECT id FROM groups WHERE pipeline_id=?", (pipeline,)).fetchone()["id"]
        moment = time_windows.now_in("Asia/Ho_Chi_Minh")
        add_job(conn, pipeline, account, group_id, moment.isoformat(timespec="seconds"))
        conn.commit()
        before = account_limits.account_state(conn, account, {"timezone": "Asia/Ho_Chi_Minh"},
                                             moment)
        # Dung lai bo dem tu job that su.
        account_limits.reconcile(conn, pipeline)
        after = account_limits.account_state(conn, account, {"timezone": "Asia/Ho_Chi_Minh"},
                                            moment)
    assert before["used"] == 1
    assert after["used"] == before["used"], "Không được reset hạn mức sai thời điểm"


def test_a_job_stored_in_utc_is_counted_in_the_workspace_day(client):
    """Mốc thời gian lưu bằng UTC vẫn thuộc đúng ngày của Workspace."""
    pipeline, account = make_pipeline(client, '@tz3')
    with m.db() as conn:
        conn.execute("INSERT INTO groups(pipeline_id,account_id,label,url,group_key,enabled,"
                     "approved_target,created_at) VALUES(?,?,'G','https://www.facebook.com/groups/3/',"
                     "'g3',1,1,?)", (pipeline, account, m.iso()))
        group_id = conn.execute("SELECT id FROM groups WHERE pipeline_id=?", (pipeline,)).fetchone()["id"]
        # 17:30 UTC = 00:30 ngày hôm sau theo giờ Việt Nam.
        utc_moment = time_windows.now_in("Asia/Ho_Chi_Minh").replace(
            hour=0, minute=30, second=0, microsecond=0).astimezone(time_windows.timezone.utc)
        add_job(conn, pipeline, account, group_id, utc_moment.isoformat(timespec="seconds"))
        conn.commit()
        local_moment = utc_moment.astimezone(time_windows.zone("Asia/Ho_Chi_Minh"))
        state = account_limits.account_state(conn, account, {"timezone": "Asia/Ho_Chi_Minh"},
                                            local_moment)
    assert state["used"] == 1, "Bài lúc 00:30 giờ Việt Nam phải thuộc ngày hôm đó"


def test_the_group_cooldown_uses_the_workspace_day(client):
    """Số bài của group trong ngày cũng theo múi giờ của Workspace."""
    pipeline, account = make_pipeline(client, '@tz4')
    with m.db() as conn:
        conn.execute("INSERT INTO groups(pipeline_id,account_id,label,url,group_key,enabled,"
                     "approved_target,created_at) VALUES(?,?,'G','https://www.facebook.com/groups/4/',"
                     "'g4',1,1,?)", (pipeline, account, m.iso()))
        group_id = conn.execute("SELECT id FROM groups WHERE pipeline_id=?", (pipeline,)).fetchone()["id"]
        evening = time_windows.now_in("Asia/Ho_Chi_Minh").replace(
            hour=23, minute=50, second=0, microsecond=0)
        add_job(conn, pipeline, account, group_id, evening.isoformat(timespec="seconds"))
        conn.commit()
        from scheduler import group_cooldowns
        used = group_cooldowns.group_usage_today(conn, group_id, evening.strftime("%Y-%m-%d"),
                                                  evening)
    assert used == 1
