"""Disable autonomous timers in isolated regression; tests dispatch explicitly."""
import pytest


@pytest.fixture(autouse=True)
def offline_timers(monkeypatch):
    import app
    for name in ('scheduler','screening_worker','precheck_worker','media_worker',
                 'auto_post_worker','analytics_thread','group_join_worker_thread'):
        monkeypatch.setattr(app,name,lambda:None)
