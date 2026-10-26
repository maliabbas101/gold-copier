import base64
import json
import secrets
import time
from types import SimpleNamespace as Obj

import pytest
from fastapi.testclient import TestClient

from goldcopier.auth import Vault, digest
from goldcopier.platform import create_app
from goldcopier.platform_models import Risk
from goldcopier.platform_store import dump
from goldcopier.signals_v2 import parse_signal

TEXT = "XAUUSD BUY\nENTRY: 2700 - 2701\nSL: 2695\nTP1: 2710\nTP2: 2720\nBE: 2705"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "VAULT_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    monkeypatch.setenv("BILLING_ENABLED", "false")
    app = create_app()
    store = app.state.store
    owner = store.signup("Owner", "owner@example.com", "a-long-test-password", "admin")
    other = store.signup("Other", "other@example.com", "another-test-password")
    analyst = store.add_analyst(
        {
            "name": "Gold trader",
            "description": "Gold",
            "guild_id": "1",
            "channel_id": "2",
            "trader_id": "3",
            "active": True,
        }
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        auth = client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "a-long-test-password"},
        ).json()
        client.headers["X-CSRF-Token"] = auth["csrf"]
        yield Obj(
            client=client,
            store=store,
            owner=owner,
            other=other,
            analyst=analyst,
            app=app,
        )


def add_account(w, **kwargs):
    body = {
        "name": "Gold",
        "broker": "Exness",
        "login": "12345678",
        "server": "Demo",
        "symbol": "XAUUSD",
        "mode": "demo",
        "hosting": "self",
        "consent": True,
        **kwargs,
    }
    r = w.client.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def worker_ready(w, account):
    r = w.client.post(f"/api/accounts/{account}/token")
    assert r.status_code == 200
    token = r.json()["token"]
    a = w.store.agent(token)
    snap = {
        "login": a["login"],
        "server": "Demo",
        "mode": "demo",
        "balance": 10000,
        "equity": 10000,
        "currency": "USD",
        "positions": [],
        "orders": [],
        "daily_pnl": 0,
    }
    w.store.heartbeat(a, snap)
    w.store.follow(w.owner, account, w.analyst, True)
    w.store.set_enabled(w.owner, account, True)
    return token, a


def test_frontend_and_security_headers(workspace):
    c = workspace.client
    r = c.get("/")
    assert r.status_code == 200 and "/assets/app.js" in r.text
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert c.get("/assets/styles.css").status_code == 200
    assert c.get("/assets/app.js").status_code == 200
    assert (
        c.post(
            "/api/accounts", json={}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        c.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    )


def test_user_cannot_promote_self_or_access_other_account(workspace):
    a = add_account(workspace)
    c = workspace.client
    assert (
        c.post(
            "/api/auth/signup",
            json={
                "name": "Attacker",
                "email": "attack@example.com",
                "password": "test-password-123",
                "role": "admin",
            },
        ).status_code
        == 422
    )
    auth = c.post(
        "/api/auth/login",
        json={"email": "other@example.com", "password": "another-test-password"},
    ).json()
    c.headers["X-CSRF-Token"] = auth["csrf"]
    assert c.get("/api/dashboard").json()["accounts"] == []
    assert c.post(f"/api/accounts/{a}/token").status_code == 400
    assert c.get("/api/operator/status").status_code == 403


def test_vault_is_account_bound_and_not_exposed(workspace, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "s" * 48)
    a = add_account(workspace, hosting="managed", password="broker-secret-password")
    dashboard = workspace.client.get("/api/dashboard").json()
    serialized = json.dumps(dashboard)
    assert "broker-secret-password" not in serialized and "agent_hash" not in serialized
    assert dashboard["accounts"][0]["credential_stored"]
    with workspace.store.db() as db:
        ciphertext = db.execute(
            "SELECT secret FROM accounts WHERE id=?", (a,)
        ).fetchone()[0]
    vault = Vault(__import__("os").environ["VAULT_KEY"])
    assert vault.decrypt(ciphertext, a) == "broker-secret-password"
    with pytest.raises(Exception):
        vault.decrypt(ciphertext, "other-account")


def test_live_gate_and_unverified_start(workspace):
    body = {
        "name": "Live",
        "broker": "Exness",
        "login": "123456",
        "server": "Live",
        "mode": "live",
        "consent": True,
    }
    assert workspace.client.post("/api/accounts", json=body).status_code == 400
    a = add_account(workspace)
    r = workspace.client.post(f"/api/accounts/{a}/enabled", json={"enabled": True})
    assert r.status_code == 400 and "healthy" in r.text


def test_signal_fanout_duplicate_and_account_bound_result(workspace):
    w = workspace
    a = add_account(w)
    token, agent = worker_ready(w, a)
    r = w.store.ingest(w.analyst, "100", TEXT, time.time())
    assert r["queued"] == 1
    assert w.store.ingest(w.analyst, "101", TEXT, time.time())["duplicate"]
    job = w.store.claim(agent)
    assert job and job["signal"]["targets"] == [2710, 2720]
    assert w.store.claim(agent) is None
    with pytest.raises(ValueError):
        w.store.result(
            {"id": "other", "user_id": w.other},
            job["id"],
            {"state": "executed", "detail": "fake"},
        )
    w.store.result(
        agent, job["id"], {"state": "uncertain", "detail": "Timeout", "ticket": None}
    )
    with pytest.raises(ValueError):
        w.store.set_enabled(w.owner, a, True)
    w.store.resolve(
        job["id"], "executed", "Verified deal #123 and open position #124", "Operator"
    )
    assert w.store.dashboard(w.owner)["jobs"][0]["state"] == "executed"


def test_pause_cancels_entries_but_allows_management(workspace):
    w = workspace
    a = add_account(w)
    token, agent = worker_ready(w, a)
    feed = w.store.ingest(w.analyst, "100", TEXT, time.time())
    job = w.store.claim(agent)
    w.store.result(
        agent, job["id"], {"state": "executed", "detail": "filled", "ticket": 1}
    )
    w.store.set_enabled(w.owner, a, False)
    r = w.store.ingest(w.analyst, "102", "BE SIGNAL: 100", time.time())
    assert r["queued"] == 1
    command = w.store.claim(agent)
    assert command["signal"]["action"] == "breakeven"
    with pytest.raises(ValueError):
        w.store.ingest("another-analyst", "103", "CLOSE SIGNAL: 100", time.time())


def test_token_rotation_revokes_and_pauses(workspace):
    a = add_account(workspace)
    token, agent = worker_ready(workspace, a)
    next_token = workspace.store.rotate_agent(workspace.owner, a)
    assert workspace.store.agent(token) is None
    assert workspace.store.agent(next_token)["enabled"] == 0


def test_billing_gate_rechecks_at_claim(workspace):
    a = add_account(workspace)
    token, agent = worker_ready(workspace, a)
    workspace.store.ingest(workspace.analyst, "1", TEXT, time.time())
    assert workspace.store.claim(agent, billing=True) is None
    with workspace.store.db() as db:
        db.execute(
            "UPDATE subscriptions SET status='active',period_end=? WHERE user_id=?",
            (time.time() + 86400, workspace.owner),
        )
    assert workspace.store.claim(agent, billing=True)


def test_demo_never_returns_a_broker_job(workspace, monkeypatch):
    a = add_account(workspace)
    token, agent = worker_ready(workspace, a)
    workspace.store.ingest(workspace.analyst, "1", TEXT, time.time())
    monkeypatch.setenv("DEMO_MODE", "true")
    demo = create_app()
    with TestClient(demo) as c:
        response = c.post(
            "/api/worker/claim", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 200 and response.json() is None


def test_password_reset_one_time_and_revokes_sessions(workspace):
    token = secrets.token_urlsafe(48)
    with workspace.store.db() as db:
        db.execute(
            "INSERT INTO resets VALUES(?,?,?)",
            (digest(token), workspace.owner, time.time() + 60),
        )
    c = workspace.client
    assert (
        c.post(
            "/api/auth/reset", json={"token": token, "password": "new-long-password"}
        ).status_code
        == 200
    )
    assert c.get("/api/auth/me").status_code == 401
    assert (
        c.post(
            "/api/auth/reset", json={"token": token, "password": "another-password"}
        ).status_code
        == 400
    )


def test_rate_limit_persisted(workspace):
    workspace.store.limit("test", count=1)
    with pytest.raises(ValueError):
        workspace.store.limit("test", count=1)


@pytest.mark.parametrize(
    "text",
    [
        TEXT.replace("XAUUSD", "EURUSD"),
        TEXT.replace("2700 - 2701", "2715"),
        TEXT.replace("TP2: 2720", "TP2: 2709"),
        TEXT.replace("BUY", "BUY LIMIT"),
        TEXT.replace("SL: 2695", "SL: NaN"),
        TEXT + "\nTP: 2730",
        "UPDATE SIGNAL: 100\nENTRY: 2700",
        "CLOSE SIGNAL: other\nSL: 123",
    ],
)
def test_invalid_signal_grammar(text):
    with pytest.raises(ValueError):
        parse_signal(text)


@pytest.mark.parametrize(
    "text",
    [
        TEXT,
        TEXT.replace("2700 - 2701", "2700").replace("BUY", "BUY LIMIT"),
        "XAUUSD SELL STOP\nENTRY: 2700\nSL: 2710\nTP1: 2690\nTP2: 2680\nBE: 2695",
        "UPDATE SIGNAL: 100\nSL: 2700\nTP: 2720",
        "CANCEL SIGNAL: 100",
    ],
)
def test_v2_grammar(text):
    assert parse_signal(text)["action"]


def test_allocations_and_cash_are_validated():
    with pytest.raises(ValueError):
        Risk(tp1_close_pct=80, tp2_close_pct=80)
    with pytest.raises(ValueError):
        Risk(risk_pct=float("nan"))


def test_public_demo_is_off_and_session_cookie_is_httponly(workspace):
    assert workspace.client.get("/api/demo").status_code == 404
    login = workspace.client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": "a-long-test-password"},
    )
    assert (
        "HttpOnly" in login.headers["set-cookie"]
        and "SameSite=strict" in login.headers["set-cookie"]
    )


def test_central_pause_and_worker_automatic_exit_audit(workspace):
    w = workspace
    a = add_account(w)
    token, agent = worker_ready(w, a)
    feed = w.store.ingest(w.analyst, "1", TEXT, time.time())
    entry = w.store.claim(agent)
    w.store.result(
        agent, entry["id"], {"state": "executed", "detail": "confirmed", "ticket": 1}
    )
    h = {"Authorization": "Bearer " + token}
    payload = {
        "id": "auto-" + "b" * 32,
        "signal_id": feed["id"],
        "result": {"state": "uncertain", "detail": "partial exit timed out"},
    }
    assert (
        w.client.post("/api/worker/auto-result", headers=h, json=payload).status_code
        == 200
    )
    assert w.store.account(w.owner, a)["enabled"] == 0
    w.store.resolve(
        payload["id"],
        "executed",
        "Verified close deal #123 for partial exit",
        "Operator",
    )
    result = w.client.get(
        "/api/worker/reconciled", headers=h, params={"ids": payload["id"]}
    ).json()
    assert result[0]["state"] == "executed"
    payload["id"] = "auto-" + "c" * 32
    payload["signal_id"] = "d" * 32
    assert (
        w.client.post("/api/worker/auto-result", headers=h, json=payload).status_code
        == 400
    )


def test_disconnect_requires_fresh_flat_account_and_revokes_token(workspace):
    a = add_account(workspace)
    token, agent = worker_ready(workspace, a)
    workspace.store.set_enabled(workspace.owner, a, False)
    assert workspace.client.delete(f"/api/accounts/{a}").status_code == 200
    assert workspace.store.agent(token) is None
