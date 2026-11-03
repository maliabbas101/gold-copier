import json
import time
from types import SimpleNamespace as NS

import httpx
import pytest

from test_platform import workspace, add_account, worker_ready
from test_worker_v2 import BrokerMT, connected_v2
from goldcopier.broker_v2 import PlatformBroker
from goldcopier.master_source import MasterObserver
from goldcopier.platform_agent import Ledger, execute
from goldcopier.platform_models import Risk
from goldcopier.signals_v2 import parse_signal, parse_signals
from goldcopier.discord_logs import deliver_one


@pytest.mark.parametrize(
    "text",
    [
        "BUY XAUUSD CMP\nSL: 2695",
        "XAUUSD BUY\nENTRY: CMP\nSL: 2695",
        "XAUUSD BUY CMP\nSL: 2695",
    ],
)
def test_cmp_without_a_number_uses_live_quote(connected_v2, text):
    b, mt = connected_v2
    s = parse_signal(text)
    s["signal_id"] = "a" * 32
    assert s["entry"] is None
    request = b.prepare_entry(s, Risk().model_dump(), "cmp")
    assert request["action"] == mt.TRADE_ACTION_DEAL
    assert request["price"] == mt.tick.ask


def test_screenshot_back_to_back_signals_parse_without_blank_separators():
    text = "XAUUSD BUY\nENTRY: 3960.5\nSL: 3900\nTP: 4000\nBE: 3975\nXAUUSD Sell\nENTRY: CMP\nSL: 3955\nTP: 3930\nBE: 3936"
    assert len(parse_signals(text)) == 2
    assert parse_signals(text)[1]["entry"] is None


def source_setup(w, tmp_path):
    master = add_account(w, login="87654321", purpose="master", symbol="XAUUSDm")
    response = w.client.post(
        "/api/operator/master-sources",
        json={
            "account_id": master,
            "analyst_id": w.analyst,
            "enabled": True,
            "confirm_read_only": True,
        },
    )
    assert response.status_code == 200, response.text
    token = w.client.post(f"/api/accounts/{master}/token").json()["token"]
    headers = {"Authorization": "Bearer " + token}
    cfg = w.client.get("/api/worker/config", headers=headers).json()
    cfg.update(terminal_path="master.exe", allow_live=False)
    mt = BrokerMT()
    mt.account.login = 87654321
    mt.account.server = "Demo"
    mt.account.trade_allowed = False
    mt.account.trade_expert = False
    mt.history_deals_get = lambda *args, **kwargs: ()
    broker = PlatformBroker(cfg, mt)

    class Transport:
        fail_once = False

        def post(self, path, json):
            result = w.client.post(path, headers=headers, json=json)
            if self.fail_once:
                self.fail_once = False
                raise httpx.ReadTimeout("lost response")
            return result

        def get(self, path):
            return w.client.get(path, headers=headers)

    client = Transport()
    ledger = Ledger(tmp_path / "master.db")
    observer = MasterObserver(broker, ledger, client)
    return NS(
        account=master,
        cfg=cfg,
        mt=mt,
        broker=broker,
        observer=observer,
        ledger=ledger,
        client=client,
        headers=headers,
    )


def position(**kw):
    return NS(
        identifier=77,
        ticket=77,
        symbol="XAUUSDm",
        type=0,
        magic=0,
        price_open=2700.2,
        sl=kw.get("sl", 2695),
        tp=kw.get("tp", 2710),
        volume=kw.get("volume", 0.1),
        time=int(time.time()),
    )


def step(source):
    source.observer.step(source.cfg["master_source"], source.cfg["pairs"])


def test_trade_once_copies_directly_and_queues_discord_log(
    workspace, tmp_path, connected_v2
):
    w = workspace
    follower = add_account(w)
    token, agent = worker_ready(w, follower)
    source = source_setup(w, tmp_path)
    step(source)
    source.mt.positions = (position(),)
    step(source)
    assert source.mt.sent == []
    job = w.store.claim(agent)
    assert job and job["signal"]["entry"] is None
    b, mt = connected_v2
    mt.account.login = int(agent["login"])
    mt.account.server = agent["server"]
    b.cfg.update(login=agent["login"], server=agent["server"])
    ledger = Ledger(tmp_path / "follower.db")
    execute(job, b, ledger)
    result = json.loads(
        ledger.db.execute(
            "SELECT result FROM operations WHERE id=?", (job["id"],)
        ).fetchone()[0]
    )
    assert result["state"] == "executed" and len(mt.sent) == 1
    w.store.result(agent, job["id"], result)
    with w.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0] == 1
    # Discord fails; the already executed follower trade is unchanged.
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    assert deliver_one(w.store, client, "test-token")
    assert w.store.dashboard(w.owner)["jobs"][0]["state"] == "executed"
    assert (
        w.client.post(
            "/api/operator/signals",
            json={
                "analyst_id": w.analyst,
                "source_id": "manual-test",
                "text": "BUY XAUUSD CMP\nSL:2695",
                "created": time.time(),
            },
        ).status_code
        == 400
    )
    assert w.client.post("/api/worker/claim", headers=source.headers).json() is None
    with pytest.raises(ValueError, match="read-only"):
        source.broker.submit({"action": 1})
    with pytest.raises(ValueError, match="read-only"):
        source.broker.prepare_entry(job["signal"], job["risk"], "no")


def test_lost_response_retries_same_event_without_duplicate_copy(workspace, tmp_path):
    w = workspace
    follower = add_account(w)
    worker_ready(w, follower)
    source = source_setup(w, tmp_path)
    step(source)
    source.mt.positions = (position(),)
    source.client.fail_once = True
    with pytest.raises(httpx.ReadTimeout):
        step(source)
    with w.store.db() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM pjobs WHERE account_id=?", (follower,)
            ).fetchone()[0]
            == 1
        )
    source.observer = MasterObserver(source.broker, source.ledger, source.client)
    step(source)
    with w.store.db() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM pjobs WHERE account_id=?", (follower,)
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0] == 0
    assert source.mt.sent == []


def test_baseline_wait_for_sl_changes_partial_and_confirmed_close(workspace, tmp_path):
    w = workspace
    follower = add_account(w)
    token, agent = worker_ready(w, follower)
    source = source_setup(w, tmp_path)
    source.mt.positions = (position(),)
    step(source)
    assert not w.store.dashboard(w.owner)["jobs"]
    source.mt.positions = ()
    step(source)
    source.mt.positions = (position(sl=0),)
    step(source)
    assert "stop loss" in source.observer.status
    source.mt.positions = (position(),)
    step(source)
    assert len(w.store.dashboard(w.owner)["jobs"]) == 1
    source.mt.positions = (position(sl=2700.2, tp=2720, volume=0.05),)
    step(source)
    actions = [j["payload"]["action"] for j in w.store.dashboard(w.owner)["jobs"]]
    assert set(actions) == {"entry", "breakeven", "modify", "partial"}
    source.mt.positions = ()
    step(source)
    assert "close" not in [
        j["payload"]["action"] for j in w.store.dashboard(w.owner)["jobs"]
    ]
    source.mt.history_deals_get = lambda **kw: (
        NS(entry=0, volume=0.1),
        NS(entry=1, volume=0.1),
    )
    step(source)
    assert "close" in [
        j["payload"]["action"] for j in w.store.dashboard(w.owner)["jobs"]
    ]
    assert source.mt.sent == []


def test_source_endpoint_is_scoped_and_cannot_follow_itself(workspace, tmp_path):
    w = workspace
    source = source_setup(w, tmp_path)
    wrong = w.store.add_analyst(
        {
            "name": "Other",
            "description": "",
            "guild_id": "8",
            "channel_id": "9",
            "trader_id": "7",
            "active": True,
        }
    )
    body = {
        "analyst_id": wrong,
        "source_id": "forged",
        "text": "BUY XAUUSD CMP\nSL: 2695",
        "created": time.time(),
    }
    assert (
        w.client.post(
            "/api/worker/master-events", headers=source.headers, json=body
        ).status_code
        == 403
    )
    assert (
        w.client.post(
            f"/api/accounts/{source.account}/enabled", json={"enabled": True}
        ).status_code
        == 400
    )
    assert (
        w.client.post(
            f"/api/accounts/{source.account}/analysts/{w.analyst}",
            json={"enabled": True},
        ).status_code
        == 400
    )
    with w.store.db() as db:
        db.execute(
            "UPDATE accounts SET user_id=? WHERE id=?", (w.other, source.account)
        )
    assert (
        w.client.post(
            "/api/operator/master-sources",
            json={
                "account_id": source.account,
                "analyst_id": w.analyst,
                "enabled": True,
                "confirm_read_only": True,
            },
        ).status_code
        == 400
    )


def test_discord_logs_are_not_ingested_and_delivery_has_no_mentions(
    workspace, tmp_path
):
    from test_platform import discord_message
    from goldcopier.discord_platform import feeds
    from goldcopier.source_platform import enqueue_log

    w = workspace
    msg = discord_message()
    msg.content = "[Gold Copier log]\nBUY XAUUSD CMP\nSL:2695"
    assert feeds(w.store, msg) == []
    with w.store.db() as db:
        enqueue_log(db, w.analyst, "one", "Trade copied")
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "123"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert deliver_one(w.store, client, "not-a-real-token")
    assert payloads[0]["allowed_mentions"] == {"parse": []}
    assert payloads[0]["enforce_nonce"] is True
    with w.store.db() as db:
        assert db.execute("SELECT state FROM discord_outbox").fetchone()[0] == "sent"


def test_broker_feed_creation_needs_no_discord_and_can_add_log_channel_later(
    workspace, tmp_path
):
    from goldcopier.source_platform import enqueue_log

    w = workspace
    created = w.client.post(
        "/api/operator/analysts",
        json={
            "name": "Direct source",
            "description": "",
            "source_mode": "broker",
            "active": True,
        },
    )
    assert created.status_code == 200, created.text
    analyst = created.json()["id"]
    master = add_account(w, login="333333", purpose="master")
    body = {
        "account_id": master,
        "analyst_id": analyst,
        "enabled": True,
        "confirm_read_only": True,
        "log_channel_id": "",
    }
    assert w.client.post("/api/operator/master-sources", json=body).status_code == 200
    with w.store.db() as db:
        assert (
            db.execute(
                "SELECT source_mode FROM analysts WHERE id=?", (analyst,)
            ).fetchone()[0]
            == "broker"
        )
        enqueue_log(db, analyst, "without-discord", "Fill observed")
        assert (
            db.execute("SELECT state FROM discord_outbox").fetchone()[0]
            == "not_configured"
        )
    called = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200))
    )
    assert not deliver_one(w.store, client, "token") and not called
    body["log_channel_id"] = "123456789"
    assert w.client.post("/api/operator/master-sources", json=body).status_code == 200
    assert deliver_one(w.store, client, "token")
    assert str(called[0].url).endswith("/channels/123456789/messages")


def test_global_entry_pause_does_not_block_master_close_commands(workspace, tmp_path):
    w = workspace
    follower = add_account(w)
    worker_ready(w, follower)
    source = source_setup(w, tmp_path)
    step(source)
    source.mt.positions = (position(),)
    step(source)
    with w.store.db() as db:
        db.execute("UPDATE settings SET value='true' WHERE key='paused'")
    source.mt.positions = ()
    source.mt.history_deals_get = lambda **kw: (
        NS(entry=0, volume=0.1),
        NS(entry=1, volume=0.1),
    )
    step(source)
    assert "close" in [
        j["payload"]["action"] for j in w.store.dashboard(w.owner)["jobs"]
    ]


def test_exness_source_reads_fills_and_closed_events_without_mutating_broker(
    workspace, tmp_path
):
    from test_exness_broker import setup as api_broker

    w = workspace
    follower = add_account(w)
    worker_ready(w, follower)
    master = add_account(w, login="123", server="Exness API", purpose="master")
    w.client.post(
        "/api/operator/pairs",
        json={
            "symbol": "EURUSD",
            "name": "Euro dollar",
            "aliases": [],
            "enabled": True,
        },
    )
    for account in (follower, master):
        assert (
            w.client.post(
                f"/api/accounts/{account}/pairs",
                json={
                    "pair": "EURUSD",
                    "broker_symbol": "EURUSD",
                    "max_spread": 0.0003,
                    "max_entry_distance": 0.001,
                    "max_lots": 0.1,
                    "enabled": True,
                },
            ).status_code
            == 200
        )
    response = w.client.post(
        "/api/operator/master-sources",
        json={
            "account_id": master,
            "analyst_id": w.analyst,
            "enabled": True,
            "confirm_read_only": True,
        },
    )
    assert response.status_code == 200
    token = w.client.post(f"/api/accounts/{master}/token").json()["token"]
    headers = {"Authorization": "Bearer " + token}
    cfg = w.client.get("/api/worker/config", headers=headers).json()
    broker, api, ledger = api_broker(tmp_path)
    broker.cfg.update(purpose="master", connector="exness")

    class Transport:
        def post(self, path, json):
            return w.client.post(path, json=json, headers=headers)

        def get(self, path):
            return w.client.get(path, headers=headers)

    observer = MasterObserver(broker, ledger, Transport())
    observer.step(cfg["master_source"], cfg["pairs"])
    raw = api.snapshot()
    raw["positions"] = [
        {
            "position_id": "999",
            "state": "open",
            "direction": "buy",
            "instrument": "EURUSD",
            "volume": ".01",
            "open_price": "1.0801",
        }
    ]
    raw["orders"] = [
        {
            "position_id": "999",
            "order_type": "sltp",
            "state": "placed",
            "stop_loss_price": "1.0781",
            "take_profit_price": "1.0841",
        }
    ]
    api.snapshot = lambda: raw
    broker.mt.cached_at = 0
    observer.step(cfg["master_source"], cfg["pairs"])
    assert len(w.store.dashboard(w.owner)["jobs"]) == 1
    raw["positions"] = []
    raw["orders"] = []
    broker.mt.cached_at = 0
    broker.mt.streams.events = {
        "closed": {
            "payload": {"positions": [{"position_id": "999", "state": "closed"}]}
        }
    }
    observer.step(cfg["master_source"], cfg["pairs"])
    assert {j["payload"]["action"] for j in w.store.dashboard(w.owner)["jobs"]} == {
        "entry",
        "close",
    }
    assert api.calls == []


def test_restarting_source_does_not_backfill_unobserved_existing_positions(
    workspace, tmp_path
):
    w = workspace
    follower = add_account(w)
    worker_ready(w, follower)
    source = source_setup(w, tmp_path)
    step(source)
    source.mt.positions = (position(),)
    source.observer = MasterObserver(source.broker, source.ledger, source.client)
    step(source)
    assert not w.store.dashboard(w.owner)["jobs"]


@pytest.mark.parametrize("magic,comment", [(0, ""), (987654, "Exness web terminal")])
def test_external_master_fills_copy_before_discord_publication(
    workspace, tmp_path, connected_v2, magic, comment
):
    w = workspace
    follower = add_account(w)
    _, agent = worker_ready(w, follower)
    source = source_setup(w, tmp_path)
    step(source)
    p = position()
    p.magic, p.comment = magic, comment
    source.mt.positions = (p,)
    step(source)
    with w.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0] == 0
    job = w.store.claim(agent)
    assert job and job["signal"]["action"] == "entry"
    b, mt = connected_v2
    mt.account.login, mt.account.server = int(agent["login"]), agent["server"]
    b.cfg.update(login=agent["login"], server=agent["server"])
    ledger = Ledger(tmp_path / "external-follower.db")
    execute(job, b, ledger)
    outcome = json.loads(
        ledger.db.execute(
            "SELECT result FROM operations WHERE id=?", (job["id"],)
        ).fetchone()[0]
    )
    assert outcome["state"] == "executed" and len(mt.sent) == 1
    w.store.result(agent, job["id"], outcome)
    w.store.result(agent, job["id"], outcome)  # repeated acknowledgement
    with w.store.db() as db:
        messages = db.execute("SELECT content FROM discord_outbox").fetchall()
        assert len(messages) == 1
        assert "BUY CMP" in messages[0][0] and "Confirmed executed" in messages[0][0]


def test_no_eligible_follower_means_no_discord_signal_and_private_master_reason(
    workspace, tmp_path
):
    w = workspace
    follower = add_account(w)
    worker_ready(w, follower)
    w.store.set_enabled(w.owner, follower, False)
    source = source_setup(w, tmp_path)
    step(source)
    source.mt.positions = (position(),)
    step(source)
    with w.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0] == 0
        feedback = db.execute(
            "SELECT detail FROM events WHERE account_id=? AND kind='source'",
            (source.account,),
        ).fetchall()
        assert any("Copying is paused" in r[0] for r in feedback)
    assert "Copying is paused" in source.observer.status


@pytest.mark.parametrize("state", ["rejected", "uncertain"])
def test_failed_follower_does_not_publish_master_signal(workspace, tmp_path, state):
    w = workspace
    follower = add_account(w)
    _, agent = worker_ready(w, follower)
    source = source_setup(w, tmp_path)
    step(source)
    source.mt.positions = (position(),)
    step(source)
    job = w.store.claim(agent)
    w.store.result(
        agent, job["id"], {"state": state, "detail": "Spread exceeds risk limit"}
    )
    with w.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0] == 0
        assert db.execute(
            "SELECT 1 FROM events WHERE account_id=? AND detail LIKE ?",
            (source.account, "%Spread exceeds risk limit%"),
        ).fetchone()


def test_first_success_publishes_once_even_when_other_followers_fail(
    workspace, tmp_path
):
    w = workspace
    first, second, third = [
        add_account(w, login=str(n)) for n in (112233, 223344, 334455)
    ]
    agents = [worker_ready(w, account)[1] for account in (first, second, third)]
    source = source_setup(w, tmp_path)
    step(source)
    source.mt.positions = (position(),)
    step(source)
    jobs = [w.store.claim(agent) for agent in agents]
    w.store.result(
        agents[0], jobs[0]["id"], {"state": "rejected", "detail": "Insufficient margin"}
    )
    with w.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0] == 0
    for agent, job in zip(agents[1:], jobs[1:]):
        w.store.result(
            agent, job["id"], {"state": "executed", "detail": "Broker confirmed fill"}
        )
    with w.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0] == 1
        assert (
            "at least one follower"
            in db.execute("SELECT content FROM discord_outbox").fetchone()[0]
        )


def test_legacy_queue_only_log_is_suppressed_then_recreated_on_execution(
    workspace, tmp_path
):
    from goldcopier.source_platform import enqueue_log

    w = workspace
    follower = add_account(w)
    _, agent = worker_ready(w, follower)
    source = source_setup(w, tmp_path)
    step(source)
    source.mt.positions = (position(),)
    step(source)
    job = w.store.claim(agent)
    with w.store.db() as db:
        feed = db.execute(
            "SELECT * FROM feeds WHERE id=(SELECT feed_id FROM pjobs WHERE id=?)",
            (job["id"],),
        ).fetchone()
        enqueue_log(
            db,
            w.analyst,
            "source:" + w.analyst + ":" + feed["source_id"],
            "XAUUSD BUY CMP\nSL:2695\nFollower commands queued: 1; skipped: 0\nSource: "
            + feed["source_id"],
        )
    sent = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: sent.append(req) or httpx.Response(200)
        )
    )
    assert deliver_one(w.store, client, "test-token")
    assert not sent
    w.store.result(
        agent, job["id"], {"state": "executed", "detail": "Broker confirmed fill"}
    )
    assert deliver_one(w.store, client, "test-token")
    assert (
        len(sent) == 1
        and "Confirmed executed" in json.loads(sent[0].content)["content"]
    )
