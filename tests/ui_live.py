"""Helpers for the browser driven checks.

The interface is a single page application, so the markup that used to be
asserted in the server response is now rendered by the browser. These helpers
start the very same application on a free port and let Playwright inspect it.
"""
import os
import socket
import threading
import time

import pytest
import uvicorn

os.environ.setdefault('DISPLAY', ':99')

import app as m  # noqa: E402
from test_app import client  # noqa: E402,F401  (makes the client fixture visible here)


@pytest.fixture
def live_server(client):
    """Run the patched application on a free port and yield its base url."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(m.app, host='127.0.0.1', port=port, log_level='error')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError('khong khoi dong duoc server de test')

    try:
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture
def web_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            yield b
        finally:
            b.close()


def login(page, base_url, username='admin', password=None):
    """Open the application and sign in, then land on the dashboard."""
    password = password or os.environ['APP_PASSWORD']
    page.goto(base_url, wait_until='domcontentloaded')
    page.wait_for_selector('#loginForm input[name=username]', timeout=20000)
    page.fill('#loginForm input[name=username]', username)
    page.fill('#loginForm input[name=password]', password)
    page.click('#loginForm button[type=submit]')
    page.wait_for_selector('.workspace-card', timeout=30000)
    return page
