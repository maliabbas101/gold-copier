import json
import time
from unittest.mock import patch

import pytest
import stripe

from test_platform import workspace, add_account
from test_billing import signed
from goldcopier.billing import Billing
from goldcopier import community as C


def login(w, email="owner@example.com", password="a-long-test-password"):
    response = w.client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    w.client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return response.json()["user"]["id"]


def create(w, mode="manual"):
    response = w.client.post(
        "/api/communities",
        json={
            "name": "Alpha Master",
            "discord_channel": "https://discord.com/channels/123/456",
            "access_mode": mode,
            "monthly_cents": 2500,
        },
    )
    assert response.status_code == 200, response.text
    cid = response.json()["id"]
    master = w.client.get("/api/community").json()["masters"][0]
    return cid, master["invite_url"].split("/")[-1]


def join(w, token):
    response = w.client.post("/api/communities/invites/" + token + "/join")
    assert response.status_code == 200, response.text
    return response.json()["community_id"]


def grant(w, cid, uid, enabled=True):
    response = w.client.post(
        f"/api/communities/{cid}/members/{uid}/access",
        json={"enabled": enabled, "days": 30},
    )
    assert response.status_code == 200, response.text


def connect(w, cid, login_id):
    response = w.client.post(
        f"/api/communities/{cid}/child-bot",
        json={
            "name": "Private child",
            "broker": "Exness",
            "login": login_id,
            "server": "Demo",
            "symbol": "XAUUSDm",
            "mode": "demo",
            "password": "private-test-password",
            "consent": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def ready(w, account, uid):
    token = w.store.rotate_agent(uid, account)
    agent = w.store.agent(token)
    response = w.client.post(
        "/api/worker/heartbeat",
        headers={"Authorization": "Bearer " + token},
        json={
            "login": agent["login"],
            "server": agent["server"],
            "mode": agent["mode"],
            "balance": 10000,
            "equity": 10000,
            "currency": "USD",
            "positions": [],
            "orders": [],
        },
    )
    assert response.status_code == 200, response.text
    return agent


def test_master_parent_generates_many_subscriber_children_and_fans_out(
    workspace, monkeypatch
):
    w = workspace
    monkeypatch.setenv("SUPERVISOR_TOKEN", "s" * 48)
    cid, invite = create(w)
    master_account = add_account(w, login="111111", purpose="master")
    response = w.client.post(
        f"/api/communities/{cid}/source-existing",
        json={"account_id": master_account, "consent": True},
    )
    assert response.status_code == 200, response.text
    with w.store.db() as db:
        analyst = db.execute(
            "SELECT analyst_id FROM communities WHERE id=?", (cid,)
        ).fetchone()[0]
    third = w.store.signup("Third", "third@example.com", "third-test-password")
    members = [
        (w.other, "other@example.com", "another-test-password", "222222"),
        (third, "third@example.com", "third-test-password", "333333"),
    ]
    children = []
    for uid, email, pwd, number in members:
        login(w, email, pwd)
        join(w, invite)
        login(w)
        grant(w, cid, uid)
        login(w, email, pwd)
        state = w.client.get("/api/community").json()["memberships"][0]
        assert state["bot"] and state["bot"]["account_id"] is None
        child = connect(w, cid, number)
        children.append((uid, child, ready(w, child["account_id"], uid)))
    result = w.store.ingest(
        analyst,
        "parent-trade",
        "BUY XAUUSD CMP\nSL: 2695",
        time.time(),
        origin="broker",
    )
    assert result["queued"] == 2
    assert len({child["bot_id"] for _, child, _ in children}) == 2
    for uid, child, agent in children:
        assert w.store.account(uid, child["account_id"])["enabled"] == 1
        job = w.store.claim(agent)
        assert job and job["signal"]["broker_symbol"] == "XAUUSDm"
        token = w.store.rotate_agent(uid, child["account_id"])
        cfg = w.client.get(
            "/api/worker/config", headers={"Authorization": "Bearer " + token}
        ).json()
        assert cfg["bot_context"]["parent_id"] == cid
        assert cfg["bot_context"]["child_id"] == child["bot_id"]
    login(w)
    state = w.client.get("/api/community").json()
    assert len(state["masters"][0]["members"]) == 2
    assert len(state["memberships"]) == 0


def test_invitation_is_pending_not_paid_and_member_cannot_administer_master(
    workspace, monkeypatch
):
    w = workspace
    monkeypatch.setenv("SUPERVISOR_TOKEN", "s" * 48)
    cid, token = create(w)
    assert (
        w.client.get("/api/communities/invites/" + token).json()["name"]
        == "Alpha Master"
    )
    login(w, "other@example.com", "another-test-password")
    join(w, token)
    status = w.client.get("/api/community").json()
    assert status["memberships"][0]["status"] == "pending"
    assert status["memberships"][0]["bot"] is None
    assert (
        w.client.post(
            f"/api/communities/{cid}/members/{w.other}/access",
            json={"enabled": True, "days": 30},
        ).status_code
        == 400
    )
    assert (
        w.client.post(
            f"/api/communities/{cid}/child-bot",
            json={
                "name": "Illegal child",
                "broker": "Exness",
                "login": "444444",
                "server": "Demo",
                "password": "test-password",
                "consent": True,
            },
        ).status_code
        == 400
    )
    assert (
        w.client.put(
            f"/api/communities/{cid}/channel", json={"discord_channel": "999"}
        ).status_code
        == 400
    )


def test_revoke_cancels_queued_entries_but_preserves_close_management(
    workspace, monkeypatch
):
    w = workspace
    monkeypatch.setenv("SUPERVISOR_TOKEN", "s" * 48)
    cid, token = create(w)
    master = add_account(w, login="111111", purpose="master")
    w.client.post(
        f"/api/communities/{cid}/source-existing",
        json={"account_id": master, "consent": True},
    )
    with w.store.db() as db:
        analyst = db.execute(
            "SELECT analyst_id FROM communities WHERE id=?", (cid,)
        ).fetchone()[0]
    login(w, "other@example.com", "another-test-password")
    join(w, token)
    login(w)
    grant(w, cid, w.other)
    login(w, "other@example.com", "another-test-password")
    child = connect(w, cid, "222222")
    agent = ready(w, child["account_id"], w.other)
    w.store.ingest(
        analyst,
        "before-expiry",
        "BUY XAUUSD CMP\nSL: 2695",
        time.time(),
        origin="broker",
    )
    login(w)
    grant(w, cid, w.other, False)
    assert w.store.claim(agent) is None
    with w.store.db() as db:
        assert (
            db.execute(
                "SELECT state FROM pjobs WHERE account_id=?", (child["account_id"],)
            ).fetchone()[0]
            == "cancelled"
        )
    w.store.control(w.other, child["account_id"], {"action": "close", "position": 123})
    assert w.store.claim(agent)["signal"]["action"] == "close"
    with pytest.raises(ValueError, match="subscription"):
        w.store.set_enabled(w.other, child["account_id"], True)


def test_private_master_feed_cannot_be_followed_by_legacy_or_foreign_children(
    workspace, monkeypatch
):
    w = workspace
    monkeypatch.setenv("SUPERVISOR_TOKEN", "s" * 48)
    cid, token = create(w)
    with w.store.db() as db:
        private = db.execute(
            "SELECT analyst_id FROM communities WHERE id=?", (cid,)
        ).fetchone()[0]
    login(w, "other@example.com", "another-test-password")
    public = w.client.get("/api/dashboard").json()["analysts"]
    assert private not in [a["id"] for a in public]
    account = add_account(w, login="777777")
    assert (
        w.client.post(
            f"/api/accounts/{account}/analysts/{private}", json={"enabled": True}
        ).status_code
        == 400
    )
    join(w, token)
    login(w)
    grant(w, cid, w.other)
    login(w, "other@example.com", "another-test-password")
    child = connect(w, cid, "222222")
    assert (
        w.client.post(
            f"/api/accounts/{child['account_id']}/analysts/{w.analyst}",
            json={"enabled": True},
        ).status_code
        == 400
    )


def test_subscription_webhook_generates_child_only_after_verified_authoritative_state(
    workspace,
):
    w = workspace
    cid, token = create(w, "stripe")
    login(w, "other@example.com", "another-test-password")
    join(w, token)
    with w.store.db() as db:
        db.execute(
            "UPDATE communities SET stripe_account=? WHERE id=?", ("acct_master", cid)
        )
        db.execute(
            "UPDATE community_memberships SET customer=? WHERE community_id=? AND user_id=?",
            ("cus_member", cid, w.other),
        )
    billing = Billing(w.store, "https://copier.example", True)
    billing.key = "sk_test"
    billing.secret = "whsec_test"
    billing.prices = {"basic": "basic", "pro": "pro"}
    metadata = {"community_id": cid, "user_id": w.other, "owner_id": w.owner}
    event = {
        "id": "evt_community",
        "type": "customer.subscription.updated",
        "created": int(time.time()),
        "data": {"object": {"id": "sub_member", "metadata": metadata}},
    }
    subscription = stripe.StripeObject.construct_from(
        {
            "id": "sub_member",
            "customer": "cus_member",
            "metadata": metadata,
            "status": "active",
            "current_period_end": time.time() + 86400,
            "transfer_data": {"destination": "acct_master"},
            "items": {
                "data": [
                    {
                        "price": {
                            "unit_amount": 2500,
                            "currency": "usd",
                            "recurring": {"interval": "month"},
                        }
                    }
                ]
            },
        },
        None,
    )
    payload = json.dumps(event).encode()
    with patch.object(
        stripe.Subscription, "retrieve", return_value=subscription
    ) as retrieve:
        billing.webhook(payload, signed(payload, billing.secret))
        billing.webhook(payload, signed(payload, billing.secret))
        assert retrieve.call_count == 1
    with w.store.db() as db:
        assert (
            db.execute(
                "SELECT status FROM community_memberships WHERE user_id=?", (w.other,)
            ).fetchone()[0]
            == "active"
        )
        assert (
            db.execute(
                "SELECT account_id FROM community_bots WHERE user_id=?", (w.other,)
            ).fetchone()[0]
            is None
        )
    # An older event cannot restore access because current Stripe state is retrieved again.
    event["id"] = "evt_old"
    event["created"] -= 50
    payload = json.dumps(event).encode()
    subscription["status"] = "canceled"
    with patch.object(stripe.Subscription, "retrieve", return_value=subscription):
        billing.webhook(payload, signed(payload, billing.secret))
    with w.store.db() as db:
        assert (
            db.execute(
                "SELECT status FROM community_memberships WHERE user_id=?", (w.other,)
            ).fetchone()[0]
            == "canceled"
        )
    with pytest.raises(Exception):
        billing.webhook(payload, "t=1,v1=forged")


def test_checkout_routes_subscription_to_its_master_and_cannot_grant_access(workspace):
    from goldcopier.community_billing import CommunityBilling

    w = workspace
    cid, token = create(w, "stripe")
    login(w, "other@example.com", "another-test-password")
    join(w, token)
    with w.store.db() as db:
        db.execute(
            "UPDATE communities SET stripe_account=? WHERE id=?", ("acct_master", cid)
        )
    billing = Billing(w.store, "https://copier.example", True)
    billing.key = "sk_test"
    billing.secret = "secret"
    user = {"id": w.other, "email": "other@example.com"}
    with (
        patch.object(
            stripe.Account,
            "retrieve",
            return_value=type(
                "Account", (), {"charges_enabled": True, "payouts_enabled": True}
            )(),
        ),
        patch.object(
            stripe.Customer,
            "create",
            return_value=type("Customer", (), {"id": "cus_new"})(),
        ),
        patch.object(
            stripe.checkout.Session,
            "create",
            return_value=type(
                "Checkout",
                (),
                {"id": "cs_one", "url": "https://checkout.stripe.com/test"},
            )(),
        ) as checkout,
    ):
        assert (
            CommunityBilling(billing).checkout(user, cid)
            == "https://checkout.stripe.com/test"
        )
        params = checkout.call_args.kwargs
        assert (
            params["subscription_data"]["transfer_data"]["destination"] == "acct_master"
        )
        assert params["subscription_data"]["metadata"]["community_id"] == cid
    with w.store.db() as db:
        assert (
            db.execute(
                "SELECT status FROM community_memberships WHERE user_id=?", (w.other,)
            ).fetchone()[0]
            == "pending"
        )
        assert db.execute("SELECT COUNT(*) FROM community_bots").fetchone()[0] == 0


def test_missing_hosting_does_not_pretend_a_child_is_connected(workspace):
    w = workspace
    cid, token = create(w)
    login(w, "other@example.com", "another-test-password")
    join(w, token)
    login(w)
    grant(w, cid, w.other)
    login(w, "other@example.com", "another-test-password")
    response = w.client.post(
        f"/api/communities/{cid}/child-bot",
        json={
            "name": "Child",
            "broker": "Exness",
            "login": "222222",
            "server": "Demo",
            "password": "test-password",
            "consent": True,
        },
    )
    assert response.status_code == 400 and "hosting" in response.json()["detail"]
    state = w.client.get("/api/community").json()["memberships"][0]
    assert state["bot"]["account_id"] is None


def test_normal_master_has_own_dashboard_without_global_operator_access(workspace):
    w = workspace
    login(w, "other@example.com", "another-test-password")
    cid, token = create(w)
    assert w.client.get("/api/operator/status").status_code == 403
    assert w.client.get("/api/community").json()["masters"][0]["id"] == cid
    login(w)
    assert w.client.get("/api/community").json()["masters"] == []
    assert (
        w.client.post(
            f"/api/communities/{cid}/enabled", json={"enabled": False}
        ).status_code
        == 400
    )
    with w.store.db() as db:
        assert not C.master_licensed(db, w.other, True)
        db.execute(
            "UPDATE subscriptions SET plan='pro',status='active',period_end=? WHERE user_id=?",
            (time.time() + 86400, w.other),
        )
        assert C.master_licensed(db, w.other, True)
        db.execute(
            "UPDATE subscriptions SET period_end=? WHERE user_id=?",
            (time.time() - 1, w.other),
        )
        assert not C.master_licensed(db, w.other, True)


def test_child_worker_environment_does_not_inherit_parent_or_service_secrets():
    from goldcopier.supervisor import worker_environment

    env = worker_environment(
        {
            "PATH": "bin",
            "COPIER_URL": "https://copier.example",
            "SUPERVISOR_TOKEN": "root",
            "MASTER_PASSWORD": "master",
            "STRIPE_SECRET_KEY": "billing",
            "VAULT_KEY": "vault",
            "DISCORD_BOT_TOKEN": "discord",
            "AGENT_TOKEN": "old-parent",
        },
        "private-child",
        "terminal.exe",
    )
    assert env["AGENT_TOKEN"] == "private-child"
    assert env["COPIER_URL"] == "https://copier.example"
    assert not any(
        k in env
        for k in (
            "MASTER_PASSWORD",
            "SUPERVISOR_TOKEN",
            "STRIPE_SECRET_KEY",
            "VAULT_KEY",
            "DISCORD_BOT_TOKEN",
        )
    )


@pytest.mark.parametrize("manage", [True, False])
def test_discord_destination_requires_own_server_authorization(
    workspace, monkeypatch, manage
):
    import httpx
    from urllib.parse import parse_qs, urlsplit
    from goldcopier.community_discord import DiscordConnection

    w = workspace
    # A normal master has no platform operator privileges.
    login(w, "other@example.com", "another-test-password")
    cid, _ = create(w)
    with w.store.db() as db:
        c = db.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone()
        assert (
            db.execute(
                "SELECT channel_id FROM analysts WHERE id=?", (c["analyst_id"],)
            ).fetchone()[0]
            == ""
        )
    for key in ("DISCORD_APPLICATION_ID", "DISCORD_CLIENT_SECRET", "DISCORD_BOT_TOKEN"):
        monkeypatch.setenv(key, "test-value")

    def provider(req):
        path = req.url.path
        if path.endswith("/oauth2/token"):
            payload = {"access_token": "private-test-oauth-token"}
        elif path.endswith("/users/@me"):
            payload = {"id": "1000"}
        elif path.endswith("/channels/456"):
            payload = {"id": "456", "guild_id": "123", "type": 0}
        else:
            assert path.endswith("/users/@me/guilds")
            payload = [{"id": "123" if manage else "999", "permissions": "32"}]
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(provider))
    oauth = DiscordConnection(w.store, "http://127.0.0.1:8000", client)
    state = parse_qs(
        urlsplit(oauth.start(w.other, cid, w.client.cookies.get("gold_session"))).query
    )["state"][0]
    if manage:
        oauth.complete(state, "code")
        saved = w.client.put(
            f"/api/communities/{cid}/channel",
            json={"discord_channel": "456", "discord_role_ids": ["123"]},
        )
        assert (
            saved.status_code == 200
        )  # editing roles must not revoke the authorized channel
        with pytest.raises(ValueError):
            oauth.complete(state, "replayed-code")
    else:
        with pytest.raises(ValueError):
            oauth.complete(state, "code")
    with w.store.db() as db:
        actual = db.execute(
            "SELECT channel_id FROM analysts WHERE id=?", (c["analyst_id"],)
        ).fetchone()[0]
        assert actual == ("456" if manage else "")
    client.close()


def test_discord_callback_rejects_revoked_browser_session(workspace, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from goldcopier.community_discord import DiscordConnection

    w = workspace
    cid, _ = create(w)
    for key in ("DISCORD_APPLICATION_ID", "DISCORD_CLIENT_SECRET", "DISCORD_BOT_TOKEN"):
        monkeypatch.setenv(key, "test-value")
    oauth = DiscordConnection(w.store, "http://127.0.0.1:8000")
    state = parse_qs(
        urlsplit(oauth.start(w.owner, cid, w.client.cookies.get("gold_session"))).query
    )["state"][0]
    with w.store.db() as db:
        db.execute("DELETE FROM sessions WHERE user_id=?", (w.owner,))
    with pytest.raises(ValueError, match="revoked"):
        oauth.complete(state, "code")


def test_master_configures_only_explicit_discord_roles(workspace):
    import httpx
    from goldcopier.source_platform import enqueue_log
    from goldcopier.discord_logs import deliver_one

    w = workspace
    cid, _ = create(w)
    url = f"/api/communities/{cid}/channel"
    response = w.client.put(
        url, json={"discord_channel": "456", "discord_role_ids": ["123", "789"]}
    )
    assert response.status_code == 200, response.text
    master = w.client.get("/api/community").json()["masters"][0]
    assert master["discord_role_ids"] == ["123", "789"]
    with w.store.db() as db:
        analyst = db.execute(
            "SELECT analyst_id FROM communities WHERE id=?", (cid,)
        ).fetchone()[0]
        enqueue_log(
            db,
            analyst,
            "roles",
            "**<@&123>** <@&789> <@&999> @everyone\nXAUUSD SELL · Trade #A1B2C3D4",
            formatted=True,
        )
    sent = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: sent.append(json.loads(req.content)) or httpx.Response(200)
        )
    )
    assert deliver_one(w.store, client, "test-token")
    assert sent[0]["allowed_mentions"] == {"parse": [], "roles": ["123", "789"]}
    assert (
        w.client.put(
            url, json={"discord_channel": "456", "discord_role_ids": ["@everyone"]}
        ).status_code
        == 422
    )
    login(w, "other@example.com", "another-test-password")
    assert (
        w.client.put(
            url, json={"discord_channel": "456", "discord_role_ids": ["999"]}
        ).status_code
        == 400
    )
