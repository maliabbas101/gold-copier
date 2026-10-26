import time
from types import SimpleNamespace as Obj

import pytest

from test_mt5 import MT
from goldcopier.broker_v2 import PlatformBroker, MAGIC
from goldcopier.platform_models import Risk
from goldcopier.platform_agent import Ledger, execute, monitor
from goldcopier.signals_v2 import parse_signal


class BrokerMT(MT):
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    TRADE_ACTION_MODIFY = 7
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_FILLING_RETURN = 2
    ORDER_TIME_SPECIFIED = 2
    TRADE_RETCODE_PLACED = 10008
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1

    def __init__(self):
        super().__init__()
        self.account.balance = 10000
        self.account.currency = "USD"

    def orders_get(self, **kwargs):
        return self.orders

    def history_deals_get(self, *args):
        return ()

    def history_orders_get(self, *args):
        return ()


@pytest.fixture
def connected_v2():
    mt = BrokerMT()
    cfg = {
        "terminal_path": "terminal.exe",
        "login": "123",
        "server": "demo",
        "symbol": "XAUUSDm",
        "mode": "demo",
        "allow_live": False,
        "baseline": {},
        "filling_mode": 1,
    }
    return PlatformBroker(cfg, mt), mt


def signal():
    s = parse_signal(
        "XAUUSD BUY\nENTRY: 2700\nSL: 2695\nTP1: 2710\nTP2: 2720\nBE: 2705"
    )
    s["signal_id"] = "a" * 32
    return s


def job(ident="job1"):
    return {
        "id": ident,
        "signal": signal(),
        "risk": Risk().model_dump(),
        "expires": time.time() + 60,
    }


def test_v2_risk_and_fixed_cap(connected_v2):
    b, mt = connected_v2
    r = Risk().model_dump()
    request = b.prepare_entry(signal(), r, "job")
    assert request["volume"] == 0.09
    assert request["magic"] == MAGIC
    r["sizing"] = "fixed"
    r["fixed_lots"] = 0.5
    r["max_lots"] = 1
    with pytest.raises(ValueError, match="risk cap"):
        b.prepare_entry(signal(), r, "job")
    r["sizing"] = "cash"
    r["cash_risk"] = 10
    assert b.prepare_entry(signal(), r, "job")["volume"] == 0.01


def test_runner_and_pending_orders(connected_v2):
    b, mt = connected_v2
    r = Risk(tp2_close_pct=25).model_dump()
    s = signal()
    assert b.prepare_entry(s, r, "job")["tp"] == 0
    s.update(order_type="LIMIT", entry=2699, entry_low=2699, entry_high=2699)
    request = b.prepare_entry(s, r, "pending")
    assert request["type"] == mt.ORDER_TYPE_BUY_LIMIT
    assert request["action"] == mt.TRADE_ACTION_PENDING
    assert request["expiration"] > time.time()
    s.update(entry=2701, entry_low=2701, entry_high=2701)
    with pytest.raises(ValueError, match="wrong side"):
        b.prepare_entry(s, r, "wrong")


def test_aggregate_exposure_and_daily_loss(connected_v2):
    b, mt = connected_v2
    r = Risk(total_risk_pct=0.5).model_dump()
    mt.positions = (
        Obj(type=0, sl=2690, volume=0.1, price_open=2700, symbol="XAUUSDm"),
    )
    with pytest.raises(ValueError, match="aggregate"):
        b.prepare_entry(signal(), r, "job")
    mt.positions = ()
    b.protection(r, mt.account)
    mt.account.equity = 9600
    with pytest.raises(ValueError, match="Daily"):
        b.protection(r, mt.account)


def test_no_stop_exposure_blocks_new_entries(connected_v2):
    b, mt = connected_v2
    mt.positions = (Obj(sl=0),)
    with pytest.raises(ValueError, match="no stop"):
        b.prepare_entry(signal(), Risk().model_dump(), "job")


def test_foreign_position_cannot_be_managed(connected_v2):
    b, mt = connected_v2
    mt.positions = (Obj(ticket=1, magic=999),)
    with pytest.raises(ValueError, match="No matching"):
        b.prepare_control({"action": "close", "position": 1})
    assert not mt.sent


def test_durable_entry_no_duplicate_and_crash_block(connected_v2, tmp_path):
    b, mt = connected_v2
    l = Ledger(tmp_path / "state.db")
    execute(job(), b, l)
    assert len(mt.sent) == 1
    with pytest.raises(ValueError, match="Duplicate"):
        execute(job(), b, l)
    l.reserve(job("interrupted"))
    l.prepared("interrupted", [{"action": 1}])
    l.recover()
    assert l.unresolved()
    assert len(mt.sent) == 1


def test_expiry_never_sends(connected_v2, tmp_path):
    b, mt = connected_v2
    l = Ledger(tmp_path / "state.db")
    j = job()
    j["expires"] = time.time() - 1
    execute(j, b, l)
    assert not mt.sent
    assert "expired" in l.db.execute("SELECT result FROM operations").fetchone()[0]


def test_target_stage_sent_once_and_be_uses_actual_fill(connected_v2, tmp_path):
    b, mt = connected_v2
    l = Ledger(tmp_path / "state.db")
    j = job()
    execute(j, b, l)
    mt.positions = (
        Obj(
            ticket=321,
            identifier=321,
            magic=MAGIC,
            comment="gc2:" + "a" * 20,
            type=0,
            volume=0.09,
            price_open=2700.2,
            sl=2695,
            tp=2720,
            symbol="XAUUSDm",
        ),
    )
    mt.tick.bid = 2711
    mt.tick.ask = 2711.2
    monitor(b, l)
    count = len(mt.sent)
    assert any(r.get("sl") == 2700.2 for r in mt.sent[1:])
    assert (
        len([r for r in mt.sent if r.get("position") == 321 and r.get("volume")]) == 1
    )
    # Re-observe BE fill as a real terminal would, and verify the partial close is not repeated.
    mt.positions[0].sl = 2700.2
    mt.positions[0].volume = 0.05
    monitor(b, l)
    assert len(mt.sent) == count


def test_manual_sl_and_tp_are_preserved(connected_v2, tmp_path):
    b, mt = connected_v2
    l = Ledger(tmp_path / "state.db")
    execute(job(), b, l)
    p = Obj(
        ticket=321,
        identifier=321,
        magic=MAGIC,
        comment="gc2:" + "a" * 20,
        type=0,
        volume=0.09,
        price_open=2700.2,
        sl=2695,
        tp=2720,
        symbol="XAUUSDm",
    )
    mt.positions = (p,)
    monitor(b, l)
    p.sl = 2698
    p.tp = 2730
    mt.tick.bid = 2711
    mt.tick.ask = 2711.2
    before = len(mt.sent)
    monitor(b, l)
    assert len(mt.sent) == before
    command = {
        "id": "modify",
        "signal": {
            "action": "modify",
            "signal_id": "a" * 32,
            "sl": 2699,
            "tp": 2725,
            "origin": "analyst",
        },
        "risk": Risk().model_dump(),
        "expires": time.time() + 60,
    }
    execute(command, b, l)
    assert len(mt.sent) == before
