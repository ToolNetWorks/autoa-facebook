"""V5: per-pipeline Facebook accounts — cookie import, session checks, isolation."""
import json
from pathlib import Path

import pytest
from test_app import client  # noqa: F401  (registers the client fixture)

import app as m
import ui_live
from ui_live import live_server, web_browser  # noqa: F401  (registers the fixtures)
import fb_browser
import profile_lock as pl
import session_crypto as sc

UID = "100012345678901"
XS = "super-secret-xs-value"


def cookie_bytes(uid=UID, xs=XS, extra=None, as_storage_state=False):
    items = [
        {"name": "c_user", "value": uid, "domain": "facebook.com", "path": "/"},
        {"name": "xs", "value": xs, "domain": "facebook.com", "path": "/"},
        {"name": "datr", "value": "datr-value", "domain": ".facebook.com", "path": "/"},
    ]
    if extra:
        items.extend(extra)
    if as_storage_state:
        return json.dumps({"cookies": items, "origins": []}).encode()
    return json.dumps(items).encode()


@pytest.fixture
def browser(monkeypatch):
    """Replace the real Chromium calls with recorded stubs."""
    calls = {"import": [], "popen": []}

    async def fake_import(account_id, cookies):
        calls["import"].append((account_id, len(cookies)))

    class FakeProc:
        pid = 4242
        returncode = 0
        stderr = ""

    def fake_popen(args, **kwargs):
        calls["popen"].append(list(args))
        return FakeProc()

    monkeypatch.setattr(fb_browser, "import_cookie_session", fake_import)
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    return calls


@pytest.fixture
def checker(monkeypatch):
    """Control the result of the session-check worker subprocess."""
    state = {"outcome": {"status": "connected", "name": "Nguyen Van A", "uid": UID,
                          "avatar": "https://scontent.example/avatar.jpg", "details": "ok"}}

    def fake_run(args, **kwargs):
        result_file = Path(args[-1])
        result_file.write_text(json.dumps(state["outcome"]), encoding="utf-8")

        class P:
            returncode = 0
            stderr = ""

        return P()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    return state


def make_account(client, pipeline_id, label="Facebook chính"):
    return client.post("/api/accounts", json={"pipeline_id": pipeline_id, "label": label}).json()["id"]


# --- cookie import -----------------------------------------------------------

def test_cookie_import_accepts_plain_array(client, browser):
    account = make_account(client, client.post("/api/pipelines", json={"name": "@a1"}).json()["id"])
    response = client.post(f"/api/accounts/{account}/cookies",
                           files={"file": ("cookies.json", cookie_bytes(), "application/json")})
    assert response.status_code == 200, response.text
    assert browser["import"] == [(account, 3)]
    assert response.json()["verified"] is False
    record = client.get("/api/accounts").json()[0]
    assert record["auth_method"] == "cookie_file"
    assert record["cookie_updated_at"]
    assert record["session_status"] == "unknown"


def test_cookie_import_accepts_playwright_storage_state(client, browser):
    account = make_account(client, client.post("/api/pipelines", json={"name": "@a2"}).json()["id"])
    response = client.post(f"/api/accounts/{account}/cookies",
                           files={"file": ("state.json", cookie_bytes(as_storage_state=True), "application/json")})
    assert response.status_code == 200, response.text
    assert browser["import"] == [(account, 3)]


@pytest.mark.parametrize("payload,reason", [
    (b"not json at all", "not json"),
    (b"{}", "empty object"),
    (b"[]", "empty array"),
    (json.dumps([{"name": "c_user", "value": "123", "domain": "facebook.com"}]).encode(), "missing xs"),
    (json.dumps([{"name": "c_user", "value": "abc", "domain": "facebook.com"},
                 {"name": "xs", "value": "v", "domain": "facebook.com"}]).encode(), "non numeric c_user"),
    (json.dumps([{"name": "c_user", "value": "123", "domain": "example.com"},
                 {"name": "xs", "value": "v", "domain": "example.com"}]).encode(), "foreign domain"),
    (json.dumps([{"name": "c_user", "value": "123", "domain": "facebook.com.evil.org"},
                 {"name": "xs", "value": "v", "domain": "facebook.com.evil.org"}]).encode(), "lookalike domain"),
    (json.dumps({"cookies": {"name": "c_user"}}).encode(), "cookies not a list"),
])
def test_cookie_import_rejects_bad_payloads(client, browser, payload, reason):
    account = make_account(client, client.post("/api/pipelines", json={"name": f"@bad{abs(hash(reason))%9999}"}).json()["id"])
    response = client.post(f"/api/accounts/{account}/cookies",
                           files={"file": ("cookies.json", payload, "application/json")})
    assert response.status_code == 422, f"{reason} should be rejected, got {response.status_code}"
    assert browser["import"] == []


def test_cookie_import_rejects_oversized_file(client, browser):
    account = make_account(client, client.post("/api/pipelines", json={"name": "@big"}).json()["id"])
    blob = b'{"pad":"' + b"x" * (300 * 1024) + b'"}'
    response = client.post(f"/api/accounts/{account}/cookies",
                           files={"file": ("cookies.json", blob, "application/json")})
    assert response.status_code == 422
    assert browser["import"] == []


def test_cookie_import_never_echoes_or_stores_cookie_values(client, browser):
    pipeline = client.post("/api/pipelines", json={"name": "@secret"}).json()["id"]
    account = make_account(client, pipeline)
    response = client.post(f"/api/accounts/{account}/cookies",
                           files={"file": ("cookies.json", cookie_bytes(), "application/json")})
    assert XS.encode() not in response.content
    listing = client.get("/api/accounts").json()
    assert XS not in json.dumps(listing)
    for table in ("accounts", "jobs", "manual_tasks", "audits"):
        with m.db() as conn:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        assert XS not in json.dumps([dict(r) for r in rows])


def test_cookie_update_requires_the_same_facebook(client, browser):
    pipeline = client.post("/api/pipelines", json={"name": "@swap"}).json()["id"]
    account = make_account(client, pipeline)
    assert client.post(f"/api/accounts/{account}/cookies",
                       files={"file": ("c.json", cookie_bytes(), "application/json")}).status_code == 200
    other = client.post(f"/api/accounts/{account}/cookies",
                        files={"file": ("c.json", cookie_bytes(uid="999888777666555"), "application/json")})
    assert other.status_code == 409
    assert "khác" in other.json()["detail"].lower() or "facebook khác" in other.json()["detail"].lower()


# --- session status ----------------------------------------------------------

def test_session_check_reports_connected_and_stores_identity(client, checker):
    pipeline = client.post("/api/pipelines", json={"name": "@ok"}).json()["id"]
    account = make_account(client, pipeline)
    result = client.post(f"/api/accounts/{account}/check")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "connected"
    assert body["name"] == "Nguyen Van A"
    assert body["uid"] == UID
    record = client.get("/api/accounts").json()[0]
    assert record["session_status"] == "connected"
    assert record["fb_name"] == "Nguyen Van A"
    assert record["fb_uid"] == UID
    assert record["session_checked_at"]


def test_session_check_reports_expired_and_cookie_can_be_updated(client, checker, browser):
    pipeline = client.post("/api/pipelines", json={"name": "@exp"}).json()["id"]
    account = make_account(client, pipeline)
    client.post(f"/api/accounts/{account}/cookies",
                files={"file": ("c.json", cookie_bytes(), "application/json")})
    checker["outcome"] = {"status": "expired", "name": "", "uid": "", "avatar": "",
                          "details": "Phiên đã hết hạn"}
    result = client.post(f"/api/accounts/{account}/check")
    assert result.json()["status"] == "expired"
    assert client.get("/api/accounts").json()[0]["session_status"] == "expired"
    # a fresh cookie file revives the account without recreating it
    again = client.post(f"/api/accounts/{account}/cookies",
                        files={"file": ("c.json", cookie_bytes(), "application/json")})
    assert again.status_code == 200
    record = client.get("/api/accounts").json()[0]
    assert record["session_status"] == "unknown"
    assert record["auth_method"] == "cookie_file"


def test_session_check_detects_checkpoint_without_bypassing_it(client, checker):
    pipeline = client.post("/api/pipelines", json={"name": "@cp"}).json()["id"]
    account = make_account(client, pipeline)
    checker["outcome"] = {"status": "checkpoint", "name": "", "uid": "", "avatar": "",
                          "details": "Facebook yêu cầu xác minh bảo mật"}
    body = client.post(f"/api/accounts/{account}/check").json()
    assert body["status"] == "checkpoint"
    assert "xác minh" in body["details"].lower()


def test_uid_is_encrypted_at_rest_but_returned_in_clear(client, checker):
    pipeline = client.post("/api/pipelines", json={"name": "@enc"}).json()["id"]
    account = make_account(client, pipeline)
    client.post(f"/api/accounts/{account}/check")
    with m.db() as conn:
        stored = conn.execute("SELECT fb_uid_enc FROM accounts WHERE id=?", (account,)).fetchone()["fb_uid_enc"]
    assert UID not in stored
    assert stored.startswith("v1.")
    assert sc.decrypt(stored, m.APP_SECRET) == UID
    assert client.get("/api/accounts").json()[0]["fb_uid"] == UID


def test_check_endpoint_never_returns_cookies(client, checker):
    pipeline = client.post("/api/pipelines", json={"name": "@nocookie"}).json()["id"]
    account = make_account(client, pipeline)
    body = client.post(f"/api/accounts/{account}/check").text
    assert "xs" not in body.lower() or "session" in body.lower()
    assert "cookie" not in body.lower() or "session" in body.lower()


# --- browser login -----------------------------------------------------------

def test_browser_login_launches_and_closes_isolated_browser(client, browser):
    pipeline = client.post("/api/pipelines", json={"name": "@login"}).json()["id"]
    account = make_account(client, pipeline)
    opened = client.post(f"/api/accounts/{account}/connect")
    assert opened.status_code == 200, opened.text
    assert browser["popen"] and str(account) in browser["popen"][0]
    assert client.post(f"/api/accounts/{account}/close").status_code == 200


def test_relogin_reopens_the_login_browser(client, browser):
    pipeline = client.post("/api/pipelines", json={"name": "@relogin"}).json()["id"]
    account = make_account(client, pipeline)
    assert client.post(f"/api/accounts/{account}/relogin").status_code == 200
    assert browser["popen"]


# --- pipeline isolation ------------------------------------------------------

def test_accounts_never_leak_between_pipelines(client, browser):
    p1 = client.post("/api/pipelines", json={"name": "@one"}).json()["id"]
    p2 = client.post("/api/pipelines", json={"name": "@two"}).json()["id"]
    a1 = make_account(client, p1, "Acc one")
    a2 = make_account(client, p2, "Acc two")
    one = client.get("/api/accounts", params={"pipeline_id": p1}).json()
    two = client.get("/api/accounts", params={"pipeline_id": p2}).json()
    assert [a["id"] for a in one] == [a1]
    assert [a["id"] for a in two] == [a2]
    assert all(a["pipeline_id"] == p1 for a in one)
    # a group may not borrow an account from another pipeline
    response = client.post("/api/groups", json={"pipeline_id": p2, "account_id": a1, "label": "X",
                                                "url": "https://www.facebook.com/groups/1234/",
                                                "approved_target": True})
    assert response.status_code == 422


def test_deleting_one_account_leaves_other_pipelines_intact(client, browser):
    p1 = client.post("/api/pipelines", json={"name": "@del1"}).json()["id"]
    p2 = client.post("/api/pipelines", json={"name": "@del2"}).json()["id"]
    a1 = make_account(client, p1, "Doomed")
    a2 = make_account(client, p2, "Survivor")
    client.post("/api/groups", json={"pipeline_id": p1, "account_id": a1, "label": "G",
                                     "url": "https://www.facebook.com/groups/1111/",
                                     "approved_target": True})
    assert client.delete(f"/api/accounts/{a1}").status_code == 200
    remaining = client.get("/api/accounts").json()
    assert [a["id"] for a in remaining] == [a2]
    assert client.get("/api/pipelines").json()[1]["name"] in ("@del1", "@del2")
    assert client.get("/api/groups", params={"pipeline_id": p2}).json() == []


# --- security ----------------------------------------------------------------

def test_account_endpoints_require_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DATA", tmp_path)
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "guard2.sqlite3")
    monkeypatch.setattr(m, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(m, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(m, "PROFILES", tmp_path / "profiles")
    for folder in (m.UPLOADS, m.EVIDENCE, m.PROFILES):
        folder.mkdir(parents=True, exist_ok=True)
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        assert c.get("/api/accounts").status_code == 401
        assert c.post("/api/accounts/1/check").status_code == 401
        assert c.post("/api/accounts/1/connect").status_code == 401
        assert c.post("/api/accounts/1/close").status_code == 401
        assert c.post("/api/accounts/1/relogin").status_code == 401
        assert c.delete("/api/accounts/1").status_code == 401
        assert c.post("/api/accounts/1/cookies", files={"file": ("c.json", cookie_bytes(), "application/json")}).status_code == 401


# --- profile lock ------------------------------------------------------------

def test_profile_lock_blocks_two_sessions_on_one_profile(tmp_path):
    first = pl.ProfileLock(11, tmp_path).acquire()
    with pytest.raises(pl.ProfileBusy):
        pl.ProfileLock(11, tmp_path).acquire()
    first.release()
    pl.ProfileLock(11, tmp_path).acquire().release()


def test_account_listing_reports_busy_profile(client, browser, tmp_path, monkeypatch):
    pipeline = client.post("/api/pipelines", json={"name": "@busy"}).json()["id"]
    account = make_account(client, pipeline)
    monkeypatch.setattr(m, "DATA", tmp_path)
    pl.ProfileLock(account, tmp_path).acquire()
    try:
        record = client.get("/api/accounts").json()[0]
        assert record["profile_busy"] is True
    finally:
        pl.ProfileLock(account, tmp_path).release()


# --- interface ---------------------------------------------------------------

def test_workspace_account_page_offers_modal_and_actions(client, live_server, web_browser, browser):
    pipeline = client.post("/api/pipelines", json={"name": "@hiu9h"}).json()["id"]
    account = client.post("/api/accounts", json={"pipeline_id": pipeline, "label": "Facebook chính"}).json()["id"]
    uploaded = client.post(f"/api/accounts/{account}/cookies",
                            files={"file": ("cookies.json", cookie_bytes(), "application/json")})
    assert uploaded.status_code == 200, uploaded.text

    page = web_browser.new_page()
    try:
        ui_live.login(page, live_server)
        page.get_by_role("button", name="Mở Workspace").first.click()
        page.wait_for_timeout(1200)
        page.click('.nav-item[data-page=accounts]')
        page.wait_for_timeout(900)

        # The account table keeps the identifier and the session state visible.
        assert page.locator("table.data tbody tr").count() >= 1
        body = page.inner_text("main")
        assert "Facebook chính" in body
        assert "Cookie JSON" in body

        # The add dialog is a real modal dialog with the expected fields.
        page.get_by_role("button", name="Thêm tài khoản").first.click()
        page.wait_for_selector('[role="dialog"]', timeout=10000)
        assert page.locator('[role="dialog"] input[name=account_label]').count() == 1
        assert page.locator('[role="dialog"] input[name=profile_url]').count() == 1
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # The stylesheet ships the responsive rules for phones and desktop.
        css = page.evaluate("""async () => {
            const href = [...document.querySelectorAll('link[rel=stylesheet]')].map(l => l.href)[0];
            return (await fetch(href)).text();
        }""")
        assert "@media (max-width: 900px)" in css
        assert "@media (max-width: 560px)" in css
    finally:
        page.close()
