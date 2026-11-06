import threading
import time
from types import SimpleNamespace as NS

import pytest
from goldcopier.exness_broker import ExnessBroker
from goldcopier.exness_api import ExnessStreams
from goldcopier.platform_agent import Ledger, execute
from goldcopier.platform_models import Risk
from goldcopier.signals_v2 import parse_signal


class Client:
    account_id = "123"

    def __init__(self):
        self.calls = []
        self.event = True
        self.foreign = False

    def discover(self):
        return "https://ap.exness.com"

    def account(self):
        return {
            "account_id": "123",
            "settings": {
                "currency": "USD",
                "trade_mode": "enabled",
                "account_status": "active",
            },
        }

    def snapshot(self):
        return {
            "account_state": {"balance": "10000", "equity": "10000"},
            "orders": [],
            "positions": [
                {
                    "position_id": "999",
                    "state": "open",
                    "direction": "buy",
                    "instrument": "EURUSD",
                    "volume": ".01",
                    "open_price": "1.08",
                }
            ]
            if self.foreign
            else [],
        }

    def instruments(self):
        return ["EURUSD", "XAUUSD"]

    def conditions(self, s):
        return {
            "volume_min": ".01",
            "volume_max": "100",
            "volume_step": ".01",
            "point_digits": 5,
            "contract_size": "100000",
            "calc_mode": "forex",
            "quote_currency": "USD",
            "trade_mode": "enabled",
            "exec_mode": "market",
        }

    def request(self, *args):
        return {"items": [], "has_more": False}

    def mutate(self, method, resource, body, key, on_ack, events):
        self.calls.append((method, resource, body, key))
        on_ack({"operation_id": "789", "status": "accepted"})
        return {
            "status": "confirmed",
            "event": {
                "payload": {
                    "positions": [
                        {"position_id": "1234", "instrument": "EURUSD", "state": "open"}
                    ]
                }
            }
            if self.event
            else None,
        }


class Streams:
    ready = True
    events = {}
    lock = threading.Lock()

    def quote(self, s):
        return {"bid": 1.08, "ask": 1.0801, "time": time.time()}


def setup(tmp_path, enabled=True):
    client = Client()
    ledger = Ledger(tmp_path / "api-worker.db")
    cfg = {
        "login": "123",
        "server": "Exness API",
        "symbol": "XAUUSD",
        "mode": "live",
        "allow_live": enabled,
        "baseline": {},
        "api_credentials": {},
    }
    return ExnessBroker(cfg, ledger, client, Streams()), client, ledger


def test_api_trade_flows_through_durable_worker_and_correlates_owned_position(tmp_path):
    broker, client, ledger = setup(tmp_path)
    signal = parse_signal(
        "EURUSD BUY\nENTRY: 1.0801\nSL: 1.0781\nTP: 1.0841\nBE: 1.0821"
    )
    signal.update(signal_id="a" * 32, broker_symbol="EURUSD")
    job = {
        "id": "entry-job",
        "signal": signal,
        "risk": Risk(max_spread=0.0003, max_entry_distance=0.001).model_dump(),
        "expires": time.time() + 60,
    }
    execute(job, broker, ledger)
    result = ledger.db.execute("SELECT result,request FROM operations").fetchone()
    assert "executed" in result["result"] and "_idempotency" in result["request"]
    method, resource, body, key = client.calls[0]
    assert (
        method == "POST" and resource == "positions" and body["instrument"] == "EURUSD"
    )
    assert body["stop_loss_price"] == "1.0781"
    assert broker.mt.bindings["positions"]["1234"] == "gc2:" + "a" * 20
    assert ledger.db.execute(
        "SELECT 1 FROM metadata WHERE key=?", ("ack:" + key,)
    ).fetchone()
    with pytest.raises(ValueError):
        execute(job, broker, ledger)
    assert len(client.calls) == 1


def test_confirmed_without_entity_event_is_uncertain_and_does_not_adopt_a_position(
    tmp_path,
):
    broker, client, ledger = setup(tmp_path)
    client.event = False
    request = {
        "action": 1,
        "symbol": "EURUSD",
        "type": 0,
        "volume": 0.01,
        "sl": 1.07,
        "tp": 1.09,
        "comment": "gc2:ref",
        "price": 1.08,
        "deviation": 1,
    }
    broker.operation_keys([request], "job")
    assert broker.submit(request)["state"] == "uncertain"
    assert broker.mt.bindings["positions"] == {}


def test_api_read_only_gate_and_foreign_exposure_cannot_be_managed(tmp_path):
    broker, client, ledger = setup(tmp_path, False)
    with pytest.raises(ValueError, match="disabled"):
        broker.prepare_entry({}, Risk().model_dump(), "job")
    assert client.calls == []
    broker.cfg["allow_live"] = True
    client.foreign = True
    with pytest.raises(ValueError, match="No matching"):
        broker.prepare_control({"action": "close", "position": 999})
    assert client.calls == []


def test_stream_ticks_and_operation_correlation():
    stream = object.__new__(ExnessStreams)
    stream.lock = threading.Lock()
    stream.ticks = {}
    stream.events = {}
    stream.symbols = set()
    stream.ready = True
    from datetime import datetime, timezone

    stream.consume(
        {
            "instrument": "EURUSD",
            "bid": 1.08,
            "ask": 1.0801,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    assert stream.quote("EURUSD")["ask"] == 1.0801
    event = {
        "event_type": "transaction_event",
        "operation_id": "123",
        "client_request_id": "stable-key",
        "status": "success",
        "payload": {},
    }
    stream.consume(event)
    assert stream.result("stable-key", "123") == event
