import json
import time
from types import SimpleNamespace as NS

import pytest
from test_platform import workspace, add_account, worker_ready, TEXT
from test_worker_v2 import BrokerMT
from goldcopier.broker_v2 import PlatformBroker, MAGIC
from goldcopier.instruments import (
    PairInput,
    MappingInput,
    save_pair,
    save_mapping,
    delete_pair,
    delete_mapping,
)
from goldcopier.platform_store import PlatformStore
from goldcopier.signals_v2 import parse_signal, parse_signals

FOREX = "EURUSD BUY\nENTRY: 1.08001\nSL: 1.07801\nTP: 1.08401\nBE: 1.08201"


def euro(w):
    save_pair(
        w.store,
        PairInput(
            symbol="eurusd", name="Euro / US dollar", aliases=["EURO"]
        ).model_dump(),
    )


def mapping(w, a):
    save_mapping(
        w.store,
        w.owner,
        a,
        MappingInput(
            pair="EURUSD",
            broker_symbol="EURUSDm",
            max_spread=0.0003,
            max_entry_distance=0.001,
            max_lots=0.05,
        ).model_dump(),
    )


def test_registry_and_mapping_crud_authorization(workspace):
    w = workspace
    a = add_account(w)
    r = w.client.post(
        "/api/operator/pairs",
        json={"symbol": "EURUSD", "name": "Euro", "aliases": ["EURO"]},
    )
    assert r.status_code == 200
    body = {
        "pair": "EURUSD",
        "broker_symbol": "EURUSDm",
        "max_spread": 0.0002,
        "max_entry_distance": 0.0005,
        "max_lots": 0.04,
    }
    assert w.client.post(f"/api/accounts/{a}/pairs", json=body).status_code == 200
    assert (
        w.client.put(
            f"/api/accounts/{a}/pairs/EURUSD",
            json={**body, "broker_symbol": "EURUSD.x"},
        ).status_code
        == 200
    )
    d = w.client.get("/api/dashboard").json()
    assert d["accounts"][0]["pairs"][-1]["pair"] in ("EURUSD", "XAUUSD")
    assert "secret" not in json.dumps(d)
    assert w.client.delete(f"/api/accounts/{a}/pairs/EURUSD").status_code == 200
    assert w.client.delete("/api/operator/pairs/EURUSD").status_code == 200
    # Deleted pairs and mappings stay deleted after a restart.
    store = PlatformStore(w.store.path)
    assert not any(p["symbol"] == "EURUSD" for p in store.dashboard(w.owner)["pairs"])
    auth = w.client.post(
        "/api/auth/login",
        json={"email": "other@example.com", "password": "another-test-password"},
    ).json()
    w.client.headers["X-CSRF-Token"] = auth["csrf"]
    assert (
        w.client.post(
            "/api/operator/pairs", json={"symbol": "BTCUSD", "name": "Bitcoin"}
        ).status_code
        == 403
    )
    assert w.client.get(f"/api/accounts/{a}/pairs").status_code == 400


def test_aliases_cannot_be_stolen(workspace):
    euro(workspace)
    with pytest.raises(ValueError, match="belongs"):
        save_pair(
            workspace.store,
            PairInput(
                symbol="EURGBP", name="Euro sterling", aliases=["EURO"]
            ).model_dump(),
        )


def test_distinct_identical_signals_are_both_copied_but_replayed_message_is_not(
    workspace,
):
    w = workspace
    a = add_account(w)
    _, agent = worker_ready(w, a)
    assert w.store.ingest(w.analyst, "first", TEXT, time.time())["queued"] == 1
    assert w.store.ingest(w.analyst, "second", TEXT, time.time())["queued"] == 1
    assert w.store.ingest(w.analyst, "first", TEXT, time.time())["duplicate"]
    one = w.store.claim(agent)
    w.store.result(
        agent, one["id"], {"state": "executed", "detail": "filled", "ticket": 1}
    )
    two = w.store.claim(agent)
    assert two and two["id"] != one["id"]
    assert len(w.store.dashboard(w.owner)["jobs"]) == 2


def test_multi_pair_batch_fanout_reference_and_frozen_mappings(workspace):
    w = workspace
    a = add_account(w)
    _, agent = worker_ready(w, a)
    euro(w)
    mapping(w, a)
    result = w.store.ingest(
        w.analyst,
        "batch",
        TEXT + "\n---\n" + FOREX.replace("EURUSD", "EURO"),
        time.time(),
    )
    assert result["queued"] == 2 and len(result["ids"]) == 2
    first = w.store.claim(agent)
    assert first["signal"]["broker_symbol"] == "XAUUSD"
    w.store.result(
        agent, first["id"], {"state": "executed", "detail": "filled", "ticket": 1}
    )
    second = w.store.claim(agent)
    assert second["signal"]["symbol"] == "EURUSD"
    assert second["signal"]["broker_symbol"] == "EURUSDm"
    assert second["risk"]["max_spread"] == 0.0003 and second["risk"]["max_lots"] == 0.05
    w.store.result(
        agent, second["id"], {"state": "executed", "detail": "filled", "ticket": 2}
    )
    close = w.store.ingest(w.analyst, "close", "CLOSE SIGNAL: batch-2", time.time())
    assert close["queued"] == 1
    assert w.store.claim(agent)["signal"]["broker_symbol"] == "EURUSDm"


def test_batch_is_atomic_and_unmapped_pair_is_reported(workspace):
    w = workspace
    a = add_account(w)
    worker_ready(w, a)
    euro(w)
    with pytest.raises(ValueError):
        w.store.ingest(
            w.analyst,
            "bad",
            TEXT + "\n---\n" + FOREX.replace("EURUSD", "UNKNOWN"),
            time.time(),
        )
    assert w.store.dashboard(w.owner)["jobs"] == []
    result = w.store.ingest(w.analyst, "unmapped", FOREX, time.time())
    assert result["queued"] == 0 and result["skipped"][0]["pair"] == "EURUSD"


def test_active_exposure_blocks_mapping_and_pair_deletion(workspace):
    w = workspace
    a = add_account(w)
    _, agent = worker_ready(w, a)
    euro(w)
    mapping(w, a)
    snap = {
        "login": agent["login"],
        "server": agent["server"],
        "mode": "demo",
        "balance": 10000,
        "equity": 10000,
        "currency": "USD",
        "positions": [{"symbol": "EURUSDm", "ticket": 123}],
        "orders": [],
    }
    w.store.heartbeat(agent, snap)
    with pytest.raises(ValueError):
        delete_mapping(w.store, w.owner, a, "EURUSD")
    with pytest.raises(ValueError):
        delete_pair(w.store, "EURUSD")


def test_default_gold_deletion_persists(workspace):
    delete_pair(workspace.store, "XAUUSD")
    assert PlatformStore(workspace.store.path).dashboard(workspace.owner)["pairs"] == []


def test_forex_precision_and_batch_grammar():
    assert parse_signal(FOREX)["entry"] == 1.08001
    assert len(parse_signals(TEXT + "\n\n" + FOREX)) == 2
    assert parse_signal(FOREX.replace("EURUSD", "BTCUSD"))["symbol"] == "BTCUSD"


def test_broker_uses_signal_symbol_and_does_not_overwrite_account_default():
    mt = BrokerMT()
    mt.tick = NS(time=time.time(), bid=1.08, ask=1.0801)
    seen = []
    mt.symbol_select = lambda symbol, visible: seen.append(symbol) or True
    mt.order_calc_profit = lambda kind, symbol, volume, entry, stop: (
        -(entry - stop) * 100000 * volume
    )
    broker = PlatformBroker(
        {
            "terminal_path": "terminal",
            "login": "123",
            "server": "demo",
            "symbol": "XAUUSDm",
            "mode": "demo",
            "allow_live": False,
            "baseline": {},
        },
        mt,
    )
    signal = parse_signal(FOREX)
    signal.update(signal_id="a" * 32, broker_symbol="EURUSDm")
    from goldcopier.platform_models import Risk

    r = Risk(max_spread=0.0003, max_entry_distance=0.001).model_dump()
    # Broker fixture gold stop level is intentionally overridden to a five-digit FX contract.
    mt.symbol_info = lambda symbol: NS(
        volume_step=0.01,
        volume_min=0.01,
        volume_max=100,
        trade_stops_level=10,
        point=0.00001,
        digits=5,
    )
    req = broker.prepare_entry(signal, r, "fx")
    assert req["symbol"] == "EURUSDm" and seen[-1] == "EURUSDm"
    assert broker.cfg["symbol"] == "XAUUSDm"
    assert req["volume"] * 0.00209 * 100000 <= 50 + 0.001
